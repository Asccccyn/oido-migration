"""复审（2026-10-01，基线 76d74dd）R01–R08 的回归钉 + 低优先 SDK 版本项。

每项按复审复现路径构造：失败对象保留索引、任意深度容器净化、完整类型合同、
真实 variant 归属/边界、迁移原子性与半迁移恢复、空键归一、取消竞态如实返回、
TTS/partial 持久化强制、ready 版本全集核对。
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from oido.service.analyses import AnalysisError, AnalysisService
from oido.service.assets import AssetError, AssetService
from oido.storage.db import expected_versions, migrate, open_db
from oido.storage.objects import ObjectStore

OWNER = "qiaosheng"
REPO = Path(__file__).resolve().parents[2]


class Interpose:
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


def _ev(vid, tb, **over):
    ev = {
        "schema_version": "oido.v1",
        "kind": "measurement", "feature": "rms_dbfs", "value": -9.0,
        "unit": "dBFS", "method": "m", "method_version": "1",
        "input_variant_id": vid, "timebase_id": tb,
        "range": {"start_sample": 0, "end_sample": 100}, "quality_flags": [],
    }
    ev.update(over)
    return ev


def _commit(env, evidence, *, status="succeeded", stages=None):
    # 上次失败提交后租约仍在效期（正确语义拒绝重复认领）→ 测试手动过期
    env["conn"].execute(
        "UPDATE analysis_runs SET lease_expires_at = '2000-01-01T00:00:00.000Z' "
        "WHERE analysis_id = ?", (env["run"],),
    )
    env["conn"].commit()
    lease = env["analyses"].claim(env["run"], worker="w1")
    assert lease is not None
    return env["analyses"].commit_result(
        analysis_id=env["run"], fencing_token=lease["fencing_token"],
        status=status, stages=stages if stages is not None else
        [{"capability": "dsp_rms", "status": "succeeded"}],
        evidence=evidence,
    )


# ── R01：artifact 删除失败保留索引，重试可清 ──────────────────────────
class TestR01ArtifactRetry:
    def test_failed_artifact_row_retained_until_file_gone(self, env):
        conn = env["conn"]
        vid, tb = _add_variant(env)
        _commit(env, [_ev(vid, tb)])
        art_key = "derivatives/art_r01"
        env["assets"].objects.put_bytes(art_key, b"png")
        conn.execute(
            """INSERT INTO artifacts (artifact_id, analysis_id, kind, object_key, sha256,
               media_type, render_params, created_at)
               VALUES ('art_r01', ?, 'png_overview', ?, 'x', 'image/png', '{}', 'now')""",
            (env["run"], art_key),
        )
        conn.commit()

        class Flaky(ObjectStore):
            def __init__(self, root, fail_keys):
                super().__init__(root)
                self.fail_keys = set(fail_keys)

            def delete(self, key):
                if key in self.fail_keys:
                    raise OSError("artifact unlink failed")
                return super().delete(key)

        flaky = Flaky(env["tmp"] / "objects", {art_key})
        env["assets"].objects = flaky
        assert env["assets"].request_delete(env["audio_id"], principal=OWNER) == \
            "deletion_requested_pending_cleanup"
        # 索引保留：行还在，重试有目标
        assert conn.execute("SELECT COUNT(*) c FROM artifacts").fetchone()["c"] == 1
        assert env["assets"].objects.exists(art_key)
        # 重试（故障恢复）→ 行删、文件删、终态
        env["assets"].objects = ObjectStore(env["tmp"] / "objects")
        assert env["assets"].request_delete(env["audio_id"], principal=OWNER) == "tombstoned"
        assert conn.execute("SELECT COUNT(*) c FROM artifacts").fetchone()["c"] == 0
        assert not env["assets"].objects.exists(art_key)
        assert env["assets"].request_delete(env["audio_id"], principal=OWNER) == "already_gone"


# ── R02：任意深度容器净化 ─────────────────────────────────────────────
class TestR02NestedContainers:
    def test_list_of_list_emotion_never_lands(self, env):
        from oido.domain.evidence import sanitize_provider_payload
        cleaned, dropped = sanitize_provider_payload(
            {"events": [[{"emotion": "sad", "label": "happy"}]]}
        )
        assert dropped and "sad" not in json.dumps(cleaned) and "happy" not in json.dumps(cleaned)
        assert cleaned == {"events": [[{}]]} or cleaned["events"] == [[{}]]

    def test_deep_mix_persisted_clean(self, env):
        conn = env["conn"]
        vid, tb = _add_variant(env)
        _commit(env, [_ev(vid, tb, kind="model_detection", feature="laugh_like",
                          value=None, score_kind="raw", raw_score=0.7, model_version="m1",
                          parameters={"events": [[{"emotion": "sad", "label": "happy"},
                                                  [[{"mood": "calm", "valence": 0.9}]]]]})],
                stages=[{"capability": "events", "status": "succeeded",
                         "detail": {"nested": [[{"emotion": "sad"}]]}}])
        for row in conn.execute("SELECT payload FROM evidence_items"):
            for word in ("sad", "happy", "calm", "valence", "emotion", "mood"):
                assert word not in row["payload"], f"{word} leaked"
        detail = conn.execute(
            "SELECT detail FROM analysis_stages WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["detail"]
        assert "sad" not in detail and "emotion" not in detail


# ── R03：完整类型合同 + 真实归属/边界 ──────────────────────────────────
class TestR03ContractAndProvenance:
    def test_schema_type_violations_rejected(self, env):
        vid, tb = _add_variant(env)
        bad_overrides = [
            {"value": "not-a-number"},
            {"unit": 17},
            {"method": None},
            {"method_version": ""},
            {"quality_flags": "not-an-array"},
            {"schema_version": "oido.v2"},
            {"timebase_id": ""},
        ]
        for over in bad_overrides:
            with pytest.raises(AnalysisError) as ei:
                _commit(env, [_ev(vid, tb, **over)])
            assert ei.value.code == "invalid_evidence", over
        with pytest.raises(AnalysisError):
            ev = _ev(vid, tb)
            del ev["schema_version"]
            _commit(env, [ev])

    def test_provenance_bounds_enforced(self, env):
        vid, tb = _add_variant(env, sample_count=100)
        with pytest.raises(AnalysisError) as ei:               # 越界
            _commit(env, [_ev(vid, tb, range={"start_sample": 0, "end_sample": 101})])
        assert "exceeds variant sample_count" in str(ei.value)
        with pytest.raises(AnalysisError):                     # 不存在的 variant
            _commit(env, [_ev("var_does_not_exist", tb)])
        with pytest.raises(AnalysisError):                     # timebase 不一致
            _commit(env, [_ev(vid, "tb_other")])
        other = env["assets"].register_upload(owner=OWNER, data=b"\x01" * 8,
                                              source_kind="user_upload")["audio_id"]
        other_vid, other_tb = _add_variant(env, other)         # 属于别的资产
        with pytest.raises(AnalysisError):
            _commit(env, [_ev(other_vid, other_tb)])
        # 正确归属 → 成功
        assert _commit(env, [_ev(vid, tb)])["status"] == "succeeded"


# ── R04：迁移原子 + 半迁移恢复 ─────────────────────────────────────────
class TestR04MigrationAtomic:
    def _dirs(self, tmp_path):
        src = REPO / "migrations"
        d_full = tmp_path / "full"
        d_v1 = tmp_path / "v1only"
        d_bad = tmp_path / "bad"
        for d in (d_full, d_v1, d_bad):
            d.mkdir(parents=True)
        shutil.copy(src / "0001_init.sql", d_full / "0001_init.sql")
        shutil.copy(src / "0002_request_payload.sql", d_full / "0002_request_payload.sql")
        shutil.copy(src / "0001_init.sql", d_v1 / "0001_init.sql")
        shutil.copy(src / "0001_init.sql", d_bad / "0001_init.sql")
        (d_bad / "0002_request_payload.sql").write_text(
            (src / "0002_request_payload.sql").read_text()
            + "\nINSERT INTO no_such_table VALUES (1);\n"
        )
        return d_full, d_v1, d_bad

    def test_atomic_rollback_on_mid_migration_failure(self, tmp_path):
        d_full, d_v1, d_bad = self._dirs(tmp_path)
        db = open_db(tmp_path / "m1.sqlite3")
        assert migrate(db, d_v1) == [1]
        # 0002 中途失败 → ALTER 也必须回滚（列不残留、版本不记）
        with pytest.raises(sqlite3.OperationalError):
            migrate(db, d_bad)
        cols = {r["name"] for r in db.execute("PRAGMA table_info(analysis_runs)")}
        assert "request_json" not in cols, "中断迁移不得留下半截 DDL"
        assert {r["version"] for r in db.execute("SELECT version FROM schema_migrations")} == {1}
        # 修好后正常重试成功
        assert migrate(db, d_full) == [2]

    def test_legacy_half_migrated_state_recovers(self, tmp_path):
        d_full, d_v1, _ = self._dirs(tmp_path)
        db = open_db(tmp_path / "m2.sqlite3")
        migrate(db, d_v1)
        # 复现复审场景：旧版迁移中断——列已加、版本未记
        db.execute("ALTER TABLE analysis_runs ADD COLUMN request_json TEXT")
        db.commit()
        # 新 migrate 不报 duplicate column，且补登版本
        assert migrate(db, d_full) == [2]
        assert {r["version"] for r in db.execute("SELECT version FROM schema_migrations")} == {1, 2}


# ── R05：空幂等键归一 ─────────────────────────────────────────────────
class TestR05EmptyKey:
    def test_empty_key_treated_as_no_key(self, env):
        a = env["analyses"].create(audio_id=env["audio_id"], profile="conversation_fast",
                                   requested_by=OWNER, idempotency_key="")
        b = env["analyses"].create(audio_id=env["audio_id"], profile="conversation_fast",
                                   requested_by=OWNER, idempotency_key="")
        assert a["analysis_id"] != b["analysis_id"], "空键=无键：两次独立创建，不炸裸异常"
        c = env["analyses"].create(audio_id=env["audio_id"], profile="conversation_fast",
                                   requested_by=OWNER, idempotency_key="   ")
        assert c["analysis_id"] not in (a["analysis_id"], b["analysis_id"])


# ── R06：取消竞态如实返回 ─────────────────────────────────────────────
class TestR06CancelRace:
    def test_cancel_returns_actual_state_when_commit_wins(self, env):
        def inject(real):
            other = AnalysisService(real)
            lease = other.claim(env["run"], worker="w-fast")
            other.commit_result(
                analysis_id=env["run"], fencing_token=lease["fencing_token"],
                status="succeeded", stages=[{"capability": "x", "status": "succeeded"}],
            )

        racy = AnalysisService(Interpose(
            env["conn"],
            lambda sql: "status = 'cancelled'" in sql,
            inject,
        ))
        result = racy.cancel(env["run"], principal=OWNER)
        assert result["status"] == "succeeded", "竞争失败必须如实返回真实终态"
        assert result.get("race_lost") is True
        row = env["conn"].execute(
            "SELECT status FROM analysis_runs WHERE analysis_id = ?", (env["run"],)
        ).fetchone()
        assert row["status"] == "succeeded"


# ── R07：TTS/partial 持久化强制 ────────────────────────────────────────
class TestR07PersistenceConstraints:
    def test_assistant_tts_requires_origin_binding(self, env):
        with pytest.raises(AssetError) as ei:
            env["assets"].register_upload(owner=OWNER, data=b"\x00" * 8,
                                          source_kind="assistant_tts", origin=None)
        assert ei.value.code == "invalid_input"
        with pytest.raises(AssetError):
            env["assets"].register_upload(
                owner=OWNER, data=b"\x00" * 8, source_kind="assistant_tts",
                origin={"voice_profile_id": "vp1"})          # 缺 version
        ok = env["assets"].register_upload(
            owner=OWNER, data=b"\x00" * 8, source_kind="assistant_tts",
            origin={"voice_profile_id": "vp1", "voice_profile_version": "3",
                    "kind": "siren_voice_message"})
        assert ok["lifecycle"] == "active"

    def test_partial_requires_explainable_stages(self, env):
        lease = env["analyses"].claim(env["run"], worker="w1")
        with pytest.raises(AnalysisError) as ei:
            env["analyses"].commit_result(
                analysis_id=env["run"], fencing_token=lease["fencing_token"],
                status="partial", stages=[])
        assert ei.value.http_status == 422
        with pytest.raises(AnalysisError):
            env["analyses"].commit_result(
                analysis_id=env["run"], fencing_token=lease["fencing_token"],
                status="partial",
                stages=[{"capability": "events", "status": "skipped"}])  # 缺 reason
        # 带原因 → 过
        assert env["analyses"].commit_result(
            analysis_id=env["run"], fencing_token=lease["fencing_token"],
            status="partial",
            stages=[{"capability": "events", "status": "skipped",
                     "reason_code": "provider_not_configured"}],
        )["status"] == "partial"

    def test_schema_origin_null_rejected(self):
        from jsonschema import Draft202012Validator

        schema = json.loads((REPO / "contracts" / "audio.v1.schema.json").read_text())
        v = Draft202012Validator(schema)
        doc = {"audio_id": "aud_x", "owner": "o", "source_kind": "assistant_tts",
               "sha256": "0" * 64, "bytes": 1, "lifecycle": "active",
               "retention": "standard", "created_at": "2026-10-01T00:00:00Z",
               "origin": None}
        assert list(v.iter_errors(doc)), "origin:null 必须被 schema 拒绝"

    def test_schema_stage_reason_required_for_skipped(self):
        from jsonschema import Draft202012Validator

        schema = json.loads((REPO / "contracts" / "analysis.v1.schema.json").read_text())
        v = Draft202012Validator(schema)
        base = {"schema_version": "oido.v1", "analysis_id": "run_x", "audio_id": "aud_x",
                "profile": "conversation_fast", "status": "partial", "revision": 1}
        bad = dict(base, stages=[{"capability": "events", "status": "skipped"}])
        assert list(v.iter_errors(bad)), "skipped 无 reason_code 必须被 schema 拒绝"
        ok = dict(base, stages=[{"capability": "events", "status": "skipped",
                                 "reason_code": "provider_not_configured"}])
        assert not list(v.iter_errors(ok))


# ── R08：ready 按版本全集 + 全表 ───────────────────────────────────────
class TestR08Ready:
    def _client(self, conn, tmp_path):
        from fastapi.testclient import TestClient

        from oido.api.http import create_app
        from oido.config import OidoConfig
        return TestClient(create_app(config=OidoConfig(
            data_root=tmp_path / "d", migrations_dir=REPO / "migrations"), conn=conn))

    def test_half_migrated_not_ready(self, tmp_path):
        db = open_db(tmp_path / "half.sqlite3")
        db.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        db.execute("INSERT INTO schema_migrations VALUES (1, 'now')")
        db.execute("ALTER TABLE analysis_runs ADD COLUMN request_json TEXT") \
            if False else None
        # 构造复审复现A：versions=[1] 但 request_json 列存在（列在但版本缺）
        db.execute("CREATE TABLE analysis_runs (analysis_id TEXT, request_json TEXT)")
        db.commit()
        body = self._client(db, tmp_path).get("/health/ready").json()
        assert body["status"] == "degraded"
        assert body["reason"].startswith("migrations_mismatch")

    def test_missing_business_table_not_ready(self, tmp_path):
        db = open_db(tmp_path / "missing.sqlite3")
        migrate(db)
        db.execute("DROP TABLE audio_origins")
        db.commit()
        body = self._client(db, tmp_path).get("/health/ready").json()
        assert body["status"] == "degraded"
        assert body["reason"].startswith("missing_tables")

    def test_expected_versions_matches_dir(self):
        assert expected_versions(REPO / "migrations") == {1, 2}


# ── 低优先项：探针 SDK 版本不再 unknown ────────────────────────────────
class TestProbeSdkVersion:
    def test_reads_package_json_from_resolved_dir(self):
        src = (REPO / "scripts" / "phase0_probe_estomago.mjs").read_text()
        assert "path.join(sdkDir, 'package.json')" in src, \
            "版本须从解析到的 SDK 目录读文件，而不是 ./package.json 子路径导入"
        assert "estRequire('@anthropic-ai/claude-agent-sdk/package.json')" not in src
