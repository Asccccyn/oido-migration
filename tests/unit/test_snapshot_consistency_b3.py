# B3 补充断言（2026-10-24）：RE-14e 转写快照——原探针在「create 时无转写→request 记
# no-transcript 哨兵、却断言执行文本 hash==哨兵」上结构性矛盾（任何实现都不可能满足）。
# 本文件按 03 §5 的自洽语义补版本化断言：
#   ① create 时已有转写 → 执行用同一转写（hash 一致）；
#   ② 排队后转写变化 → 拒绝执行（transcript_changed 如实失败），不静默用新文本提交旧请求。
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from oido.prosody.pipeline import ProsodyResult
from oido.service.analyses import AnalysisService, AnalysisError
from oido.service.assets import AssetService
from oido.service.ingest import IngestService
from oido.storage.db import open_db, migrate
from oido.storage.objects import ObjectStore
from oido.workers import analysis_worker


@pytest.fixture
def env(tmp_path):
    conn = open_db(tmp_path / "metadata/oido.sqlite3")
    migrate(conn)
    objects = ObjectStore(tmp_path)
    assets = AssetService(conn, objects)
    ingest = IngestService(conn, assets)
    analyses = AnalysisService(conn)
    audio_id = ingest.ingest(principal="est", audio_ref="est:synthetic", data=b"synthetic")["audio_id"]
    yield SimpleNamespace(conn=conn, objects=objects, ingest=ingest, analyses=analyses, audio_id=audio_id)
    conn.close()


VARIANT = {"sample_rate_hz": 16000, "variant_id": "var_synthetic", "timebase_id": "tb_16k_mono", "sample_count": 16000}


def _worker_env(env, seen):
    import oido.workers.analysis_worker as worker
    orig = worker.run_prosody
    def run_pipeline(**kwargs):
        seen.update(kwargs)
        return ProsodyResult(status="succeeded", facts={"sr": 16000, "duration_s": 1., "units": [], "pauses": [], "limits": []})
    worker.run_prosody = run_pipeline
    variants = SimpleNamespace(ensure_decode_main=lambda **kw: VARIANT, load_pcm=lambda v: np.ones(16000, dtype=np.float32))
    return worker, variants, orig


def test_b3sup_snapshot_present_executes_with_same_transcript(env):
    env.conn.execute(
        "INSERT INTO audio_transcripts(audio_id,text,source,created_at) VALUES (?,?,?,datetime('now'))",
        (env.audio_id, "排队前就有的转写", "audit:synthetic"))
    env.conn.commit()
    run = env.analyses.create(audio_id=env.audio_id, profile="prosody", requested_by="est")
    request = json.loads(env.conn.execute("SELECT request_json FROM analysis_runs WHERE analysis_id=?", (run["analysis_id"],)).fetchone()[0])
    seen = {}
    worker, variants, orig = _worker_env(env, seen)
    try:
        worker.process_one(conn=env.conn, analyses=env.analyses, variants=variants,
                           ingest=env.ingest, objects=env.objects, analysis_id=run["analysis_id"])
    finally:
        worker.run_prosody = orig
    used = hashlib.sha256(seen["transcript"]["text"].encode()).hexdigest()
    assert request["transcript_sha256"] == used, "执行输入必须=排队快照"
    assert env.analyses.get(run["analysis_id"], principal="est")["status"] == "succeeded"


def test_b3sup_transcript_changed_after_queue_rejects_execution(env):
    run = env.analyses.create(audio_id=env.audio_id, profile="prosody", requested_by="est")
    env.conn.execute(
        "INSERT INTO audio_transcripts(audio_id,text,source,created_at) VALUES (?,?,?,datetime('now'))",
        (env.audio_id, "后来才到的合成转写", "audit:synthetic"))
    env.conn.commit()
    seen = {}
    worker, variants, orig = _worker_env(env, seen)
    try:
        result = worker.process_one(conn=env.conn, analyses=env.analyses, variants=variants,
                                    ingest=env.ingest, objects=env.objects, analysis_id=run["analysis_id"])
    finally:
        worker.run_prosody = orig
    assert "transcript" not in seen, "排队后转写变化不得静默执行"
    row = env.conn.execute("SELECT status, error_code FROM analysis_runs WHERE analysis_id=?", (run["analysis_id"],)).fetchone()
    assert row["status"] == "failed" and row["error_code"] == "transcript_changed"
