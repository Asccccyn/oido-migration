"""Independent behavioral counterexamples against b069226. No product edits.

Uses isolated SQLite, synthetic facts and explicit injected timing/races. No provider.
"""
import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from oido.api.http import create_app
from oido.config import OidoConfig
from oido.mcp.server import handle_message
from oido.prosody.pipeline import ProsodyResult
from oido.service.analyses import AnalysisService, AnalysisError
from oido.service.assets import AssetService
from oido.service.ingest import IngestService
from oido.storage.db import open_db, migrate
from oido.storage.objects import ObjectStore
from oido.workers import analysis_worker, executor

AUTH = {"Authorization":"Bearer audit-r6prime-local"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OIDO_AUTH_TOKEN", "audit-r6prime-local")
    conn = open_db(tmp_path / "metadata/oido.sqlite3")
    migrate(conn)
    cfg = OidoConfig(data_root=tmp_path)
    cfg.flags["ingest"] = cfg.flags["offline_analysis"] = True
    objects = ObjectStore(tmp_path)
    assets = AssetService(conn, objects)
    ingest = IngestService(conn, assets)
    analyses = AnalysisService(conn)
    audio_id = ingest.ingest(principal="est", audio_ref="est:synthetic", data=b"synthetic")['audio_id']
    app = create_app(cfg, conn)
    client = TestClient(app, raise_server_exceptions=False)
    result = SimpleNamespace(conn=conn, cfg=cfg, objects=objects, assets=assets,
        ingest=ingest, analyses=analyses, audio_id=audio_id, app=app, client=client)
    yield result
    conn.close()


def completed(env, text="啊啊啊啊啊啊", key=None):
    run = env.analyses.create(audio_id=env.audio_id,profile="prosody",requested_by="est",idempotency_key=key)
    lease = env.analyses.claim(run["analysis_id"],worker="audit")
    facts = {"sr":16000,"duration_s":len(text),"voiced_total_s":len(text),"f0_median_hz":220.,
        "alignment_source":"syllable_energy_dp","limits":[],"pauses":[],
        "units":[{"text":c,"start_s":i,"end_s":i+.8} for i,c in enumerate(text)],
        "unit_pitch_st":[[0.] for c in text]}
    env.analyses.commit_result(analysis_id=run["analysis_id"],fencing_token=lease["fencing_token"],
        status="succeeded",stages=[{"capability":"prosody_facts","status":"succeeded","detail":{"facts":facts}}])
    return run["analysis_id"]


def test_find_server_issued_cursor_can_be_consumed(env):
    completed(env)
    first = env.client.post("/v1/find",headers=AUTH,json={"audio_ref":"est:synthetic","query":"啊","limit":2})
    assert first.status_code == 200
    second = env.client.post("/v1/find",headers=AUTH,json={"audio_ref":"est:synthetic","query":"啊","limit":2,
                                                               "cursor":first.json()["next_cursor"]})
    print("FIND_FIRST",first.json(),"FIND_SECOND",second.status_code,second.text)
    assert second.status_code == 200
    assert second.json()["items"][0]["range_ms"] == [2000,2800]


def test_find_exact_last_page_has_no_spurious_cursor(env):
    completed(env,text="啊啊")
    first = env.client.post("/v1/find",headers=AUTH,json={"audio_ref":"est:synthetic","query":"啊","limit":2})
    print("EXACT_FINAL_PAGE",first.json())
    assert "next_cursor" not in first.json()


def test_mcp_find_exposes_a_cursor_argument():
    listing = handle_message({"id":1,"method":"tools/list"})
    spec = next(t for t in listing["result"]["tools"] if t["name"]=="oido_find")
    assert "cursor" in spec["inputSchema"]["properties"]


def test_compare_over_budget_is_bounded_error_not_500(env):
    completed(env)
    response = env.client.post("/v1/compare",headers=AUTH,json={
        "left":{"audio_ref":"est:synthetic"},"right":{"audio_ref":"est:synthetic"},
        "metrics":["duration_s"]*30})
    print("COMPARE_BUDGET",response.status_code,response.text)
    assert response.status_code == 422
    assert response.json()["code"] == "BUDGET_EXCEEDED"


def test_compare_nested_range_is_validated_before_domain(env):
    completed(env)
    response = env.client.post("/v1/compare",headers=AUTH,json={
        "left":{"audio_ref":"est:synthetic","range_ms":["x","y"]},"right":{"audio_ref":"est:synthetic"}})
    print("COMPARE_BAD_RANGE",response.status_code,response.text)
    assert response.status_code == 422


def test_wait_poll_does_not_block_other_asgi_requests(env, monkeypatch):
    monkeypatch.setattr(executor,"EXECUTOR_ACTIVE",True)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app),base_url="http://audit") as client:
            start = time.monotonic()
            async def waiting():
                return await client.post("/v1/analyses",headers=AUTH,json={"audio_ref":"est:synthetic","wait_ms":450})
            async def lightweight():
                await asyncio.sleep(.01)
                response = await client.get("/health/live")
                return response.status_code, time.monotonic()-start
            long, quick = await asyncio.gather(waiting(),lightweight())
            return long.status_code, quick
    long_status,(quick_status,elapsed)=asyncio.run(scenario())
    print("ASGI_WAIT",long_status,"LIVE",quick_status,"LIVE_RESPONSE_FROM_START_S",elapsed)
    assert elapsed < .2, "poll sleep must yield the event loop so cancellation/health can run"


def test_http_cancel_reports_authoritative_race_result(env, monkeypatch):
    run=env.analyses.create(audio_id=env.audio_id,profile="prosody",requested_by="est")
    original=AnalysisService.cancel
    def finish_before_cancel(self, analysis_id, *, principal):
        lease=self.claim(analysis_id,worker="winning-worker")
        self.commit_result(analysis_id=analysis_id,fencing_token=lease["fencing_token"],status="succeeded",stages=[])
        return original(self,analysis_id,principal=principal)
    monkeypatch.setattr(AnalysisService,"cancel",finish_before_cancel)
    response=env.client.post(f"/v1/analyses/{run['analysis_id']}/cancel",headers=AUTH)
    actual=env.analyses.get(run['analysis_id'],principal="est")["status"]
    print("CANCEL_RESPONSE",response.json(),"ACTUAL_DB_STATUS",actual)
    assert response.json()["status"] == actual
    assert response.json()["cancelled"] is False


def test_completed_idempotent_analysis_does_not_return_queued(env):
    aid=completed(env,key="audit-operation")
    response=env.client.post("/v1/analyses",headers=AUTH,json={"audio_ref":"est:synthetic","idempotency_key":"audit-operation","wait_ms":0})
    print("IDEMPOTENT_TERMINAL",response.json(),"actual",env.analyses.get(aid,principal="est")["status"])
    assert response.json()["state"] in ("ready","partial")


def test_cancelled_analysis_get_is_terminal_not_retryable(env):
    run=env.analyses.create(audio_id=env.audio_id,profile="prosody",requested_by="est")
    env.analyses.cancel(run["analysis_id"],principal="est")
    response=env.client.get(f"/v1/analyses/{run['analysis_id']}",headers=AUTH)
    print("GET_CANCELLED",response.status_code,response.json())
    assert response.json().get("retryable") is not True


def test_queued_transcript_snapshot_matches_execution(env, monkeypatch):
    run=env.analyses.create(audio_id=env.audio_id,profile="prosody",requested_by="est")
    request=json.loads(env.conn.execute("SELECT request_json FROM analysis_runs WHERE analysis_id=?",(run["analysis_id"],)).fetchone()[0])
    env.conn.execute("INSERT INTO audio_transcripts(audio_id,text,source,created_at) VALUES (?,?,?,datetime('now'))",
                     (env.audio_id,"后来才到的合成转写","audit:synthetic"));env.conn.commit()
    seen={}
    def run_pipeline(**kwargs):
        seen.update(kwargs)
        return ProsodyResult(status="succeeded",facts={"sr":16000,"duration_s":1.,"units":[],"pauses":[],"limits":[]})
    monkeypatch.setattr(analysis_worker,"run_prosody",run_pipeline)
    variant={"sample_rate_hz":16000,"variant_id":"var_synthetic","timebase_id":"tb_16k_mono","sample_count":16000}
    variants=SimpleNamespace(ensure_decode_main=lambda **kw:variant,load_pcm=lambda v:np.ones(16000,dtype=np.float32))
    result=analysis_worker.process_one(conn=env.conn,analyses=env.analyses,variants=variants,
        ingest=env.ingest,objects=env.objects,analysis_id=run["analysis_id"])
    used=hashlib.sha256(seen["transcript"]["text"].encode()).hexdigest()
    print("SNAPSHOT_HASH",request["transcript_sha256"],"EXECUTED_HASH",used,"COMMIT",result)
    assert request["transcript_sha256"] == used


def test_legacy_same_operation_retry_reports_approved_409(env):
    key="previous-version-operation"
    run=env.analyses.create(audio_id=env.audio_id,profile="prosody",requested_by="est",idempotency_key=key)
    legacy={"audio_id":env.audio_id,"profile":"prosody","range":None,"budget":None}
    digest=hashlib.sha256(json.dumps(legacy,sort_keys=True).encode()).hexdigest()
    env.conn.execute("UPDATE analysis_runs SET request_digest=?,request_json=? WHERE analysis_id=?",
        (digest,json.dumps(legacy),run["analysis_id"]));env.conn.commit()
    # R6P premise line 41 explicitly adopts conflict after a version change;
    # the initial audit expectation of transparent replay is therefore withdrawn.
    with pytest.raises(AnalysisError) as caught:
        env.analyses.create(audio_id=env.audio_id,profile="prosody",requested_by="est",idempotency_key=key)
    print("APPROVED_LEGACY_OPERATION",caught.value.code,caught.value.http_status)
    assert caught.value.code == "idempotency_conflict" and caught.value.http_status == 409


def test_claim_next_helper_is_callable(env):
    assert executor.claim_next(env.conn,"audit") is None


def test_permissions_and_source_deletion_still_block_reads(env):
    aid=completed(env)
    wrong=env.client.get(f"/v1/analyses/{aid}",headers={"Authorization":"Bearer wrong"})
    assert wrong.status_code == 401
    env.assets.request_delete(env.audio_id,principal="est")
    assert env.client.get(f"/v1/analyses/{aid}",headers=AUTH).status_code == 404
    assert env.client.post("/v1/find",headers=AUTH,json={"audio_ref":"est:synthetic","query":"啊"}).status_code == 404


def test_cancel_during_pcm_load_does_not_start_next_dsp_stage(env, monkeypatch):
    run=env.analyses.create(audio_id=env.audio_id,profile="prosody",requested_by="est")
    seen=[]
    variant={"sample_rate_hz":16000,"variant_id":"var_synthetic","timebase_id":"tb_16k_mono","sample_count":16000}
    def load_after_cancel(v):
        env.analyses.cancel(run["analysis_id"],principal="est")
        return np.ones(16000,dtype=np.float32)
    variants=SimpleNamespace(ensure_decode_main=lambda **kw:variant,load_pcm=load_after_cancel)
    def next_stage(**kwargs):
        seen.append("new_dsp_after_cancel")
        return ProsodyResult(status="succeeded",facts={"sr":16000,"duration_s":1.,"units":[],"pauses":[],"limits":[]})
    monkeypatch.setattr(analysis_worker,"run_prosody",next_stage)
    try:
        analysis_worker.process_one(conn=env.conn,analyses=env.analyses,variants=variants,
            ingest=env.ingest,objects=env.objects,analysis_id=run["analysis_id"])
    except AnalysisError as error:
        print("LATE_COMMIT_REJECTED",error.code)
    print("STAGES_AFTER_CANCEL",seen)
    assert seen == []
