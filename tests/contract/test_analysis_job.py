"""WP01 合同：幂等 409（E11）、fencing 防迟到提交（E12）、取消不复活、
租约回收、owner 删除链路。全部使用 synthetic fixture + 内存态 SQLite。"""

import pytest

from oido.domain.states import IllegalTransition
from oido.service.analyses import AnalysisError, AnalysisService
from oido.service.assets import AssetError, AssetService
from oido.storage.db import migrate, open_db
from oido.storage.objects import ObjectStore


@pytest.fixture()
def env(tmp_path):
    conn = open_db(tmp_path / "meta" / "oido.sqlite3")
    migrate(conn)
    objects = ObjectStore(tmp_path / "objects")
    return conn, AssetService(conn, objects), AnalysisService(conn)


OWNER = "qiaosheng"
OTHER = "someone_else"


def _upload(assets: AssetService, *, owner=OWNER, data=b"\x00\x01" * 100):
    return assets.register_upload(owner=owner, data=data, source_kind="user_upload")


def _add_variant(conn, audio_id, *, sample_count=24000, sample_rate=16000):
    """登记真实 decode_main variant（R03 后证据必须引用真实 variant/timebase）。"""
    from oido.domain.ids import new_id

    vid, tb = new_id("var"), new_id("tb")
    conn.execute(
        """INSERT INTO audio_variants (variant_id, audio_id, transform_kind, sample_rate_hz,
           channels, sample_count, timebase_id, transform_params, created_at)
           VALUES (?, ?, 'decode_main', ?, 1, ?, ?, '{}', '2026-10-01T00:00:00Z')""",
        (vid, audio_id, sample_rate, sample_count, tb),
    )
    conn.commit()
    return vid, tb


class TestIdempotencyE11:
    def test_same_key_same_request_dedupes(self, env):
        _, assets, analyses = env
        audio_id = _upload(assets)["audio_id"]
        a = analyses.create(audio_id=audio_id, profile="conversation_fast",
                            requested_by=OWNER, idempotency_key="k1")
        b = analyses.create(audio_id=audio_id, profile="conversation_fast",
                            requested_by=OWNER, idempotency_key="k1")
        assert a["deduplicated"] is False and b["deduplicated"] is True
        assert a["analysis_id"] == b["analysis_id"]

    def test_same_key_different_request_409(self, env):
        _, assets, analyses = env
        audio_id = _upload(assets)["audio_id"]
        analyses.create(audio_id=audio_id, profile="conversation_fast",
                        requested_by=OWNER, idempotency_key="k1")
        with pytest.raises(AnalysisError) as ei:
            analyses.create(audio_id=audio_id, profile="full_evidence",
                            requested_by=OWNER, idempotency_key="k1")
        assert ei.value.http_status == 409

    def test_key_scope_is_principal(self, env):
        # 同一个 key 不同 principal 不冲突（EXEC §3.3：principal+endpoint+key）
        _, assets, analyses = env
        a1 = _upload(assets)["audio_id"]
        a2 = _upload(assets, owner=OTHER)["audio_id"]
        r1 = analyses.create(audio_id=a1, profile="conversation_fast",
                             requested_by=OWNER, idempotency_key="shared")
        r2 = analyses.create(audio_id=a2, profile="conversation_fast",
                             requested_by=OTHER, idempotency_key="shared")
        assert r1["analysis_id"] != r2["analysis_id"]


class TestFencingE12:
    def test_stale_token_cannot_commit(self, env):
        _, assets, analyses = env
        audio_id = _upload(assets)["audio_id"]
        run = analyses.create(audio_id=audio_id, profile="conversation_fast",
                              requested_by=OWNER)["analysis_id"]
        lease1 = analyses.claim(run, worker="w1")
        # w1 崩溃后租约到期，w2 夺回 → token 前进
        env[0].execute("UPDATE analysis_runs SET lease_expires_at = '2000-01-01T00:00:00.000Z' WHERE analysis_id = ?", (run,))
        lease2 = analyses.claim(run, worker="w2")
        assert lease2["fencing_token"] > lease1["fencing_token"]
        # 迟到的 w1 拿旧 token 提交 → 拒绝
        with pytest.raises(AnalysisError) as ei:
            analyses.commit_result(
                analysis_id=run, fencing_token=lease1["fencing_token"],
                status="succeeded", stages=[{"capability": "dsp_rms", "status": "succeeded"}],
            )
        assert ei.value.code == "stale_fence" and ei.value.http_status == 409

    def test_cancel_bumps_fence_and_blocks_resurrect(self, env):
        _, assets, analyses = env
        audio_id = _upload(assets)["audio_id"]
        run = analyses.create(audio_id=audio_id, profile="conversation_fast",
                              requested_by=OWNER)["analysis_id"]
        lease = analyses.claim(run, worker="w1")
        analyses.cancel(run, principal=OWNER)
        # 取消后 worker 用"自己拿到的"token 提交 → stale_fence（token 已被 cancel 递增）
        with pytest.raises(AnalysisError):
            analyses.commit_result(
                analysis_id=run, fencing_token=lease["fencing_token"],
                status="succeeded", stages=[],
            )
        # 即使读到了新 token，终态不可复活
        row = env[0].execute(
            "SELECT fencing_token, status FROM analysis_runs WHERE analysis_id = ?", (run,)
        ).fetchone()
        with pytest.raises(AnalysisError) as ei:
            analyses.commit_result(
                analysis_id=run, fencing_token=row["fencing_token"],
                status="succeeded", stages=[],
            )
        assert ei.value.code == "already_terminal"

    def test_cancel_is_idempotent(self, env):
        _, assets, analyses = env
        audio_id = _upload(assets)["audio_id"]
        run = analyses.create(audio_id=audio_id, profile="conversation_fast",
                              requested_by=OWNER)["analysis_id"]
        first = analyses.cancel(run, principal=OWNER)
        second = analyses.cancel(run, principal=OWNER)
        assert first["idempotent"] is False and second["idempotent"] is True

    def test_successful_commit_records_stages_and_evidence(self, env):
        conn, assets, analyses = env
        audio_id = _upload(assets)["audio_id"]
        vid, tb = _add_variant(conn, audio_id)
        run = analyses.create(audio_id=audio_id, profile="conversation_fast",
                              requested_by=OWNER)["analysis_id"]
        lease = analyses.claim(run, worker="w1")
        result = analyses.commit_result(
            analysis_id=run, fencing_token=lease["fencing_token"], status="partial",
            stages=[
                {"capability": "dsp_rms", "status": "succeeded"},
                {"capability": "events", "status": "skipped", "reason_code": "provider_not_configured"},
            ],
            evidence=[{
                "schema_version": "oido.v1",
                "kind": "measurement", "feature": "rms_dbfs", "value": -9.03,
                "unit": "dBFS", "method": "rms_float_pcm_reference_1",
                "method_version": "1", "input_variant_id": vid, "timebase_id": tb,
                "range": {"start_sample": 0, "end_sample": 24000}, "quality_flags": [],
            }],
        )
        assert result["status"] == "partial" and result["revision"] == 1
        got = analyses.get(result["analysis_id"], principal=OWNER)
        by_cap = {s["capability"]: s for s in got["stages"]}
        assert by_cap["events"]["reason_code"] == "provider_not_configured"


class TestVisibility:
    def test_cross_owner_denied_uniformly(self, env):
        # E07：他人 ID 与不存在的 ID 同口径，不泄露存在性
        _, assets, _ = env
        audio_id = _upload(assets)["audio_id"]
        with pytest.raises(AssetError):
            assets.get(audio_id, principal=OTHER)
        with pytest.raises(AssetError) as e_missing:
            assets.get("aud_does_not_exist", principal=OWNER)
        with pytest.raises(AssetError) as e_other:
            assets.get(audio_id, principal=OTHER)
        assert e_missing.value.code == e_other.value.code == "not_visible"

    def test_analysis_of_invisible_asset_rejected(self, env):
        _, assets, analyses = env
        audio_id = _upload(assets)["audio_id"]
        with pytest.raises(AnalysisError) as ei:
            analyses.create(audio_id=audio_id, profile="full_evidence", requested_by=OTHER)
        assert ei.value.http_status == 404


class TestOwnerDelete:
    def test_delete_cancels_runs_and_tombstones(self, env):
        conn, assets, analyses = env
        audio_id = _upload(assets)["audio_id"]
        run = analyses.create(audio_id=audio_id, profile="conversation_fast",
                              requested_by=OWNER)["analysis_id"]
        analyses.claim(run, worker="w1")
        outcome = assets.request_delete(audio_id, principal=OWNER)
        assert outcome == "tombstoned"
        # 读取被阻断
        with pytest.raises(AssetError):
            assets.get(audio_id, principal=OWNER)
        # 在途任务已取消；对象已清
        row = conn.execute("SELECT status FROM analysis_runs WHERE analysis_id = ?", (run,)).fetchone()
        assert row["status"] == "cancelled"
        assert not (tmp_keys := assets.objects.exists(f"originals/{audio_id}"))
        # 墓碑在案（E18：备份恢复先施加墓碑）
        assert conn.execute("SELECT 1 FROM deletion_tombstones WHERE audio_id = ?", (audio_id,)).fetchone()
        # 幂等
        assert assets.request_delete(audio_id, principal=OWNER) == "already_gone"

    def test_non_owner_cannot_delete(self, env):
        _, assets, _ = env
        audio_id = _upload(assets)["audio_id"]
        with pytest.raises(AssetError):
            assets.request_delete(audio_id, principal=OTHER)
