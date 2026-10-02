"""林石见独立审计（2026-10-01，基线 2bb86fb）12 项缺陷的回归钉。

每项至少一个"旧代码失败、修复后通过"的用例；并发场景用连接代理在
SELECT 与 UPDATE 之间注入另一真实连接的合法操作（审计同款手法）。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from oido.service.analyses import AnalysisError, AnalysisService
from oido.service.assets import AssetError, AssetService
from oido.storage.db import migrate, open_db
from oido.storage.objects import ObjectStore

OWNER = "qiaosheng"


class Interpose:
    """在匹配的 SQL 执行前注入一次回调（模拟并发连接的合法写）。"""

    def __init__(self, real, trigger, inject):
        self._real, self._trigger, self._inject = real, trigger, inject
        self._armed = False

    def execute(self, sql, params=()):
        if not self._armed and self._trigger(sql):
            self._armed = True
            self._inject(self._real)
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "meta" / "oido.sqlite3"
    conn = open_db(db_path)
    migrate(conn)
    conn2 = open_db(db_path)
    assets = AssetService(conn, ObjectStore(tmp_path / "objects"))
    analyses = AnalysisService(conn)
    audio_id = assets.register_upload(owner=OWNER, data=b"\x00" * 64,
                                      source_kind="user_upload")["audio_id"]
    run = analyses.create(audio_id=audio_id, profile="conversation_fast",
                          requested_by=OWNER)["analysis_id"]
    return {"db": db_path, "conn": conn, "conn2": conn2, "assets": assets,
            "analyses": analyses, "audio_id": audio_id, "run": run,
            "tmp": tmp_path}


def _commit_ok(env, run, **over):
    lease = env["analyses"].claim(run, worker="w1")
    assert lease is not None
    kwargs = {"status": "succeeded",
              "stages": [{"capability": "dsp_rms", "status": "succeeded"}]}
    kwargs.update(over)
    return env["analyses"].commit_result(
        analysis_id=run, fencing_token=lease["fencing_token"], **kwargs
    )


def _expire_lease(env, run):
    # 上次失败后租约仍在效期内；正确语义是拒绝重复认领，故测试手动过期
    env["conn"].execute(
        "UPDATE analysis_runs SET lease_expires_at = '2000-01-01T00:00:00.000Z' WHERE analysis_id = ?",
        (run,),
    )
    env["conn"].commit()


def _add_variant(env, audio_id=None, *, sample_count=24000):
    from oido.domain.ids import new_id

    audio_id = audio_id or env["audio_id"]
    vid, tb = new_id("var"), new_id("tb")
    env["conn"].execute(
        """INSERT INTO audio_variants (variant_id, audio_id, transform_kind, sample_rate_hz,
           channels, sample_count, timebase_id, transform_params, created_at)
           VALUES (?, ?, 'decode_main', 16000, 1, ?, ?, '{}', '2026-10-01T00:00:00Z')""",
        (vid, audio_id, sample_count, tb),
    )
    env["conn"].commit()
    return vid, tb


def _bind(evidence, vid, tb):
    for ev in evidence or []:
        ev.setdefault("schema_version", "oido.v1")
        ev["input_variant_id"] = vid
        ev["timebase_id"] = tb
    return evidence


# ── F01：取消/删除与提交、认领交错，终态不可复活 ─────────────────────
class TestF01Resurrection:
    def test_cancel_race_blocks_commit(self, env):
        def inject(real):
            AnalysisService(real).cancel(env["run"], principal=OWNER)
            real.commit()

        racy = AnalysisService(Interpose(
            env["conn"],
            lambda sql: sql.startswith("UPDATE analysis_runs") and "SET status = ?" in sql,
            inject,
        ))
        lease = env["analyses"].claim(env["run"], worker="w1")
        with pytest.raises(AnalysisError):
            racy.commit_result(analysis_id=env["run"], fencing_token=lease["fencing_token"],
                               status="succeeded", stages=[])
        row = env["conn"].execute(
            "SELECT status FROM analysis_runs WHERE analysis_id = ?", (env["run"],)
        ).fetchone()
        assert row["status"] == "cancelled", "取消竞态后不得复活为 succeeded"

    def test_delete_race_blocks_claim(self, env):
        def inject(real):
            AssetService(real, ObjectStore(env["tmp"] / "objects")).request_delete(
                env["audio_id"], principal=OWNER)

        racy = AnalysisService(Interpose(
            env["conn"],
            lambda sql: sql.startswith("UPDATE analysis_runs") and "lease_owner = ?" in sql,
            inject,
        ))
        assert racy.claim(env["run"], worker="w1") is None, "资产被删后 claim 不得复活任务"
        row = env["conn"].execute(
            "SELECT status FROM analysis_runs WHERE analysis_id = ?", (env["run"],)
        ).fetchone()
        assert row["status"] == "cancelled"

    def test_delete_bumps_fence(self, env):
        env["analyses"].claim(env["run"], worker="w1")
        before = env["conn"].execute(
            "SELECT fencing_token FROM analysis_runs WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["fencing_token"]
        env["assets"].request_delete(env["audio_id"], principal=OWNER)
        after = env["conn"].execute(
            "SELECT fencing_token FROM analysis_runs WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["fencing_token"]
        assert after > before, "删除必须递增 fence，否则旧 token 可复活"


# ── F02：删除意图先持久化；IO 失败不丢意图 ────────────────────────────
class TestF02DeleteIntent:
    def test_intent_persisted_before_object_cleanup(self, env):
        seen = []

        class SnoopingObjects(ObjectStore):
            def delete(self, key):
                row = env["conn"].execute(
                    "SELECT lifecycle FROM audio_assets WHERE audio_id = ?", (env["audio_id"],)
                ).fetchone()
                seen.append(row["lifecycle"])
                return super().delete(key)

        env["assets"].objects = SnoopingObjects(env["tmp"] / "objects")
        env["assets"].request_delete(env["audio_id"], principal=OWNER)
        assert seen and all(s in ("deletion_requested", "tombstoned") for s in seen), \
            "对象清理时读取必须已被阻断（先持久化意图）"

    def test_io_failure_keeps_intent_and_stays_retryable(self, env):
        class FlakyObjects(ObjectStore):
            def __init__(self, root, fail_times):
                super().__init__(root)
                self.fail_times = fail_times

            def delete(self, key):
                if self.fail_times > 0:
                    self.fail_times -= 1
                    raise OSError("disk went away")
                return super().delete(key)

        flaky = FlakyObjects(env["tmp"] / "objects", fail_times=1)
        env["assets"].objects = flaky
        outcome = env["assets"].request_delete(env["audio_id"], principal=OWNER)
        assert outcome == "deletion_requested_pending_cleanup", "IO 失败不得回滚意图，也不得假装完成"
        assert env["conn"].execute(
            "SELECT 1 FROM deletion_tombstones WHERE audio_id = ?", (env["audio_id"],)
        ).fetchone()
        with pytest.raises(AssetError):
            env["assets"].get(env["audio_id"], principal=OWNER)
        # 重入重试：故障恢复后完成清理 → tombstoned、对象消失
        assert env["assets"].request_delete(env["audio_id"], principal=OWNER) == "tombstoned"
        assert not env["assets"].objects.exists(f"originals/{env['audio_id']}")


# ── F03：删除后证据/阶段/artifact 清库且分析不可读 ─────────────────────
class TestF03EvidenceCleanup:
    def test_evidence_stages_artifacts_removed_and_get_blocked(self, env):
        conn = env["conn"]
        vid, tb = _add_variant(env)
        _commit_ok(env, env["run"], evidence=_bind([{
            "kind": "measurement", "feature": "rms_dbfs", "value": -9.03,
            "unit": "dBFS", "method": "m", "method_version": "1",
            "input_variant_id": "v", "timebase_id": "t",
            "range": {"start_sample": 0, "end_sample": 100}, "quality_flags": [],
        }], vid, tb))
        assert conn.execute(
            "SELECT COUNT(*) c FROM evidence_items WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["c"] == 1
        # 人工登记一个 artifact（renderer 未实现，行+对象合成构造，同审计手法）
        art_key = "derivatives/art_test"
        env["assets"].objects.put_bytes(art_key, b"png-bytes")
        conn.execute(
            """INSERT INTO artifacts (artifact_id, analysis_id, kind, object_key, sha256,
               media_type, render_params, created_at)
               VALUES ('art_t1', ?, 'png_overview', ?, 'x', 'image/png', '{}', 'now')""",
            (env["run"], art_key),
        )
        conn.commit()

        env["assets"].request_delete(env["audio_id"], principal=OWNER)
        assert conn.execute(
            "SELECT COUNT(*) c FROM evidence_items WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM analysis_stages WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM artifacts").fetchone()["c"] == 0
        assert not env["assets"].objects.exists(art_key)
        with pytest.raises(AnalysisError):
            env["analyses"].get(env["run"], principal=OWNER)


# ── F04/F06：情绪与非法证据在持久层被拒 ───────────────────────────────
class TestF04F06PersistenceValidation:
    def test_array_emotion_and_stage_detail_sanitized(self, env):
        conn = env["conn"]
        vid, tb = _add_variant(env)
        _commit_ok(env, env["run"],
                   stages=[{"capability": "events", "status": "succeeded",
                            "detail": {"emotion": "sad"}}],
                   evidence=_bind([{
                       "kind": "model_detection", "feature": "laugh_like",
                       "score_kind": "raw", "raw_score": 0.7, "model_version": "m1",
                       "method": "m", "method_version": "1", "input_variant_id": "v",
                       "timebase_id": "t", "quality_flags": [],
                       "range": {"start_sample": 0, "end_sample": 100},
                       "parameters": {"events": [{"emotion": "sad", "label": "happy"}]},
                   }], vid, tb))
        for row in conn.execute("SELECT payload FROM evidence_items"):
            assert "sad" not in row["payload"] and "happy" not in row["payload"]
        detail = conn.execute(
            "SELECT detail FROM analysis_stages WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["detail"]
        assert "sad" not in detail

    def test_invalid_evidence_rejected_atomically(self, env):
        bad_cases = [
            {"kind": "measurement", "feature": "rms_dbfs", "value": -9.0,
             "method": "m", "method_version": "1", "input_variant_id": "v",
             "timebase_id": "t", "quality_flags": [],
             "range": {"start_sample": 0, "end_sample": 100}},          # 缺 unit
            {"kind": "measurement", "feature": "rms_dbfs", "value": float("nan"),
             "unit": "dBFS", "method": "m", "method_version": "1",
             "input_variant_id": "v", "timebase_id": "t", "quality_flags": [],
             "range": {"start_sample": 0, "end_sample": 100}},          # NaN
            {"kind": "measurement", "feature": "rms_dbfs", "value": -9.0,
             "unit": "dBFS", "method": "m", "method_version": "1",
             "input_variant_id": "v", "timebase_id": "t", "quality_flags": [],
             "range": {"start_sample": -4, "end_sample": -10}},         # 负倒置
            {"kind": "measurement", "feature": "rms_dbfs", "value": -9.0,
             "unit": "dBFS", "method": "m", "method_version": "1",
             "input_variant_id": "v", "timebase_id": "t", "quality_flags": [],
             "range": {"start_sample": 0.5, "end_sample": 1.5}},        # 小数
        ]
        for bad in bad_cases:
            with pytest.raises(AnalysisError) as ei:
                _commit_ok(env, env["run"], evidence=[bad])
            assert ei.value.code == "invalid_evidence" and ei.value.http_status == 422
            _expire_lease(env, env["run"])
        assert env["conn"].execute(
            "SELECT COUNT(*) c FROM evidence_items WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["c"] == 0, "非法证据一条都不该落库"


# ── F05：中途失败回滚，不留半开事务 ───────────────────────────────────
class TestF05TransactionRollback:
    def test_midway_failure_rolls_back(self, env):
        conn = env["conn"]
        lease = env["analyses"].claim(env["run"], worker="w1")

        def inject(_real):
            raise sqlite3.OperationalError("disk I/O error")

        racy = AnalysisService(Interpose(
            conn,
            lambda sql: sql.startswith("INSERT INTO analysis_stages"),
            inject,
        ))
        with pytest.raises(sqlite3.OperationalError):
            racy.commit_result(analysis_id=env["run"],
                               fencing_token=lease["fencing_token"],
                               status="succeeded",
                               stages=[{"capability": "x", "status": "succeeded"}])
        assert conn.in_transaction is False, "失败后不得留半开事务"
        row = conn.execute(
            "SELECT status FROM analysis_runs WHERE analysis_id = ?", (env["run"],)
        ).fetchone()
        assert row["status"] == "running", "半成品 succeeded 不得被记为结果"
        # 后续无关请求正常工作且不会把失败操作提交掉
        assert env["analyses"].create(audio_id=env["audio_id"],
                                      profile="conversation_fast",
                                      requested_by=OWNER)["deduplicated"] is False
        assert conn.execute(
            "SELECT status FROM analysis_runs WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["status"] == "running"


# ── F07：并发幂等冲突 → 去重或 409，不抛裸 IntegrityError ─────────────
class TestF07IdempotencyRace:
    def test_concurrent_same_request_dedupes(self, env):
        payload = dict(audio_id=env["audio_id"], profile="conversation_fast",
                       requested_by=OWNER, idempotency_key="race-key")

        def inject(_real):
            # 真实并发：第二连接先插入同 key 同请求并提交
            AnalysisService(env["conn2"]).create(**payload)

        racy = AnalysisService(Interpose(
            env["conn"],
            lambda sql: sql.startswith("INSERT INTO analysis_runs"),
            inject,
        ))
        result = racy.create(**payload)
        assert result["deduplicated"] is True, "并发同请求应去重到已建任务"

    def test_concurrent_different_request_409(self, env):
        def inject(_real):
            AnalysisService(env["conn2"]).create(audio_id=env["audio_id"],
                                                 profile="conversation_fast",
                                                 requested_by=OWNER, idempotency_key="k9")

        racy = AnalysisService(Interpose(
            env["conn"],
            lambda sql: sql.startswith("INSERT INTO analysis_runs"),
            inject,
        ))
        with pytest.raises(AnalysisError) as ei:
            racy.create(audio_id=env["audio_id"], profile="full_evidence",
                        requested_by=OWNER, idempotency_key="k9")
        assert ei.value.http_status == 409


# ── F08：range/budget 持久化并可恢复 ──────────────────────────────────
class TestF08RequestPayload:
    def test_request_json_persisted_and_returned(self, env):
        created = env["analyses"].create(audio_id=env["audio_id"], profile="inspect_range",
                                         requested_by=OWNER, range_={"start": 10, "end": 20},
                                         budget={"deadline_ms": 1500})
        reopened = open_db(env["db"])
        migrate(reopened)
        row = reopened.execute(
            "SELECT request_json FROM analysis_runs WHERE analysis_id = ?",
            (created["analysis_id"],),
        ).fetchone()
        req = json.loads(row["request_json"])
        assert req["range"] == {"start": 10, "end": 20}
        assert req["budget"] == {"deadline_ms": 1500}
        assert req["profile"] == "inspect_range"
        # claim 时返回原请求，durable worker 可恢复区间与预算
        lease = AnalysisService(reopened).claim(created["analysis_id"], worker="w2")
        assert lease is not None and lease["request"]["range"] == {"start": 10, "end": 20}


# ── F11：短于门限的信号明确拒答 ────────────────────────────────────────
class TestF11InsufficientSignal:
    @pytest.mark.parametrize("sr", [16000, 24000, 44100, 48000])
    def test_10ms_rejected(self, sr):
        import numpy as np

        from oido.dsp.f0 import pyin_f0, robust_median_f0
        sig = 0.5 * np.sin(2 * np.pi * 440 * np.arange(int(sr * 0.01)) / sr)
        result = pyin_f0(sig, sr=sr)
        assert result["sufficient"] is False
        assert result["insufficiency_reason"]
        assert robust_median_f0(result) is None


# ── F12：空迁移库不 ready ─────────────────────────────────────────────
class TestF12ReadyOnEmptyMigrations:
    def test_empty_migrations_degraded(self, tmp_path):
        from fastapi.testclient import TestClient

        from oido.api.http import create_app
        from oido.config import OidoConfig
        conn = open_db(tmp_path / "empty.sqlite3")
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.commit()
        body = TestClient(create_app(config=OidoConfig(data_root=tmp_path), conn=conn)) \
            .get("/health/ready").json()
        assert body["status"] == "degraded"
        assert body["reason"] == "migrations_empty"


# ── F09/F10：探针静态回归钉（真实发图 + 作用域 + 隔离）────────────────
class TestF09F10ProbeStatic:
    PROBE = Path(__file__).resolve().parents[2] / "scripts" / "phase0_probe_estomago.mjs"

    def test_probe_sends_image_block(self):
        src = self.PROBE.read_text()
        assert "type: 'image'" in src and "media_type: 'image/png'" in src, "必须发送 image content block"
        assert "base64" in src
        assert "pngB64" in src.split("async function* input")[1], "读入的图片数据必须进入消息"

    def test_probe_resolver_at_module_scope(self):
        src = self.PROBE.read_text()
        assert "const estRequire = createRequire" in src
        # F10 的坏味道：try 块内声明、块外使用——修复后声明必须在首个 try 之前
        assert src.index("const estRequire") < src.index("let sdk;")

    def test_probe_isolated_cwd_and_tools(self):
        src = self.PROBE.read_text()
        assert "tools: []" in src and "cwd: tempHome" in src and "settingSources: []" in src


# ── 架构笔记 [P2 合同不成立]：$comment 里的不变量改为可执行约束 ────────
class TestSchemaExecutableConstraints:
    CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"

    def _validator(self, name):
        from jsonschema import Draft202012Validator
        schema = json.loads((self.CONTRACTS / name).read_text())
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def _audio(self, **over):
        doc = {
            "audio_id": "aud_x", "owner": "qiaosheng", "source_kind": "user_upload",
            "sha256": "0" * 64, "bytes": 10, "lifecycle": "active",
            "retention": "standard", "created_at": "2026-10-01T00:00:00Z",
        }
        doc.update(over)
        return doc

    def test_assistant_tts_requires_voice_profile_binding(self):
        v = self._validator("audio.v1.schema.json")
        # 缺 origin → 拒
        bad = self._audio(source_kind="assistant_tts")
        assert list(v.iter_errors(bad))
        # origin 缺 version → 拒
        bad2 = self._audio(source_kind="assistant_tts",
                           origin={"kind": "siren_voice_message", "voice_profile_id": "vp1"})
        assert list(v.iter_errors(bad2))
        # 齐备 → 过
        ok = self._audio(source_kind="assistant_tts", origin={
            "kind": "siren_voice_message", "voice_profile_id": "vp1",
            "voice_profile_version": "3"})
        assert not list(v.iter_errors(ok))
        # user_upload 不强制 origin
        assert not list(v.iter_errors(self._audio()))

    def test_partial_requires_stages(self):
        v = self._validator("analysis.v1.schema.json")
        base = {"schema_version": "oido.v1", "analysis_id": "run_x", "audio_id": "aud_x",
                "profile": "conversation_fast", "status": "partial", "revision": 1}
        assert list(v.iter_errors(base)), "partial 不带 stages 必须被 schema 拒绝"
        base["stages"] = []
        assert list(v.iter_errors(base)), "空 stages 也要拒"
        base["stages"] = [{"capability": "events", "status": "skipped",
                           "reason_code": "provider_not_configured"}]
        assert not list(v.iter_errors(base))

    def test_valence_feature_rejected_by_evidence_schema(self):
        v = self._validator("evidence.v1.schema.json")
        bad = {
            "schema_version": "oido.v1", "kind": "measurement", "feature": "valence_score",
            "value": -0.7, "unit": "unitless", "method": "m", "method_version": "1",
            "input_variant_id": "v", "timebase_id": "t",
            "range": {"start_sample": 0, "end_sample": 10}, "quality_flags": [],
        }
        assert list(v.iter_errors(bad)), "valence 是情绪维度，schema 层也要拒"
