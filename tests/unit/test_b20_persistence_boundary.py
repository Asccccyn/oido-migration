"""B20 持久层兜底：情绪字段即使藏进 parameters 嵌套也不得落库（周char裁定收紧）。"""

import json

import pytest

from oido.service.analyses import AnalysisService
from oido.service.assets import AssetService
from oido.storage.db import migrate, open_db
from oido.storage.objects import ObjectStore


@pytest.fixture()
def env(tmp_path):
    conn = open_db(tmp_path / "meta" / "oido.sqlite3")
    migrate(conn)
    return conn, AssetService(conn, ObjectStore(tmp_path / "objects")), AnalysisService(conn)


def _add_variant(conn, audio_id, *, sample_count=24000):
    from oido.domain.ids import new_id

    vid, tb = new_id("var"), new_id("tb")
    conn.execute(
        """INSERT INTO audio_variants (variant_id, audio_id, transform_kind, sample_rate_hz,
           channels, sample_count, timebase_id, transform_params, created_at)
           VALUES (?, ?, 'decode_main', 16000, 1, ?, ?, '{}', '2026-10-01T00:00:00Z')""",
        (vid, audio_id, sample_count, tb),
    )
    conn.commit()
    return vid, tb


def _run_with_evidence(env, evidence):
    conn, assets, analyses = env
    audio_id = assets.register_upload(owner="qiaosheng", data=b"\x00" * 64,
                                      source_kind="user_upload")["audio_id"]
    vid, tb = _add_variant(conn, audio_id)
    for ev in evidence:
        ev.setdefault("schema_version", "oido.v1")
        ev["input_variant_id"] = vid
        ev["timebase_id"] = tb
        rng = ev.setdefault("range", {"start_sample": 0, "end_sample": 24000})
        if rng["end_sample"] > 24000:
            rng["end_sample"] = 24000
    run = analyses.create(audio_id=audio_id, profile="conversation_fast",
                          requested_by="qiaosheng")["analysis_id"]
    lease = analyses.claim(run, worker="w1")
    analyses.commit_result(
        analysis_id=run, fencing_token=lease["fencing_token"], status="succeeded",
        stages=[{"capability": "events", "status": "succeeded"}],
        evidence=evidence,
    )
    return conn, run


class TestB20PersistenceBoundary:
    def test_nested_emotion_in_parameters_never_lands(self, env):
        conn, run = _run_with_evidence(env, [{
            "kind": "model_detection", "feature": "laugh_like",
            "score_kind": "raw_sigmoid", "raw_score": 0.7, "model_version": "fixture",
            "method": "fixture", "method_version": "1", "input_variant_id": "v",
            "timebase_id": "t", "range": {"start_sample": 0, "end_sample": 100},
            "quality_flags": [],
            "parameters": {"window_s": 2.0, "emotion": "happy", "emotion_score": 0.81},
        }])
        payload = conn.execute(
            "SELECT payload FROM evidence_items WHERE analysis_id = ?", (run,)
        ).fetchone()["payload"]
        # 键与值都不在落库 JSON 里
        assert "emotion" not in payload
        assert "happy" not in payload
        assert "0.81" not in payload
        # 合法字段保留；打上兜底净化标记
        obj = json.loads(payload)
        assert obj["parameters"]["window_s"] == 2.0
        assert "sanitized_at_persistence" in obj["quality_flags"]

    def test_clean_evidence_untouched(self, env):
        conn, run = _run_with_evidence(env, [{
            "kind": "measurement", "feature": "rms_dbfs", "value": -9.03,
            "unit": "dBFS", "method": "m", "method_version": "1",
            "input_variant_id": "v", "timebase_id": "t",
            "range": {"start_sample": 0, "end_sample": 100}, "quality_flags": [],
        }])
        obj = json.loads(conn.execute(
            "SELECT payload FROM evidence_items WHERE analysis_id = ?", (run,)
        ).fetchone()["payload"])
        assert "sanitized_at_persistence" not in obj["quality_flags"]
        assert obj["value"] == -9.03

    def test_whole_table_scan_no_emotion_values(self, env):
        """全库扫描：已知情绪取值样例在任何 payload 中零出现（B20 全域断言）。"""
        _run_with_evidence(env, [{
            "kind": "model_description", "feature": "ambient_caption",
            "method": "m", "method_version": "1", "input_variant_id": "v",
            "timebase_id": "t", "range": {"start_sample": 0, "end_sample": 100},
            "quality_flags": [], "value": None, "below_floor": False,
            "text": "一段带笑声的录音",
            "parameters": {"mood": "cheerful", "nested": {"feeling": "joy"}},
        }])
        for row in env[0].execute("SELECT payload FROM evidence_items"):
            for sample in ("happy", "sad", "cheerful", "joy", "mood", "feeling"):
                assert sample not in row["payload"], f"{sample} leaked into DB"
