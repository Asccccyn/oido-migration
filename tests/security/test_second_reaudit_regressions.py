"""二次复审（2026-10-01，基线 fddb093）S01–S04 + O01/O02 的回归钉。

按复审建议的申报粒度逐项给关闭证据：
- S01：正常 make_rms_measurement → commit → 回读 payload → 自家 schema 通过 + 可选字段反例
- S02：全 succeeded 的 partial 被拒、非法 reason 被拒、TTS 布尔绑定被拒、合法 partial 通过
- S03：失败上传不留无索引孤儿对象
- S04：删除赢在创建之前 → 无新 queued 任务（两连接证据）
- O1/O2：结构损坏 ready 拒绝、并发迁移者假失败恢复
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oido.domain.evidence import make_rms_measurement
from oido.service.analyses import AnalysisError, AnalysisService
from oido.service.assets import AssetError, AssetService
from oido.storage.db import migrate, open_db
from oido.storage.objects import ObjectStore

OWNER = "qiaosheng"
REPO = Path(__file__).resolve().parents[2]
_SCHEMA = json.loads((REPO / "contracts" / "evidence.v1.schema.json").read_text())
_VALIDATOR = Draft202012Validator(_SCHEMA)


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


def _variant(env, *, sample_count=24000):
    from oido.domain.ids import new_id

    vid, tb = new_id("var"), new_id("tb")
    env["conn"].execute(
        """INSERT INTO audio_variants (variant_id, audio_id, transform_kind, sample_rate_hz,
           channels, sample_count, timebase_id, transform_params, created_at)
           VALUES (?, ?, 'decode_main', 16000, 1, ?, ?, '{}', '2026-10-01T00:00:00Z')""",
        (vid, env["audio_id"], sample_count, tb),
    )
    env["conn"].commit()
    return vid, tb


def _commit(env, evidence, *, status="succeeded", stages=None):
    env["conn"].execute(
        "UPDATE analysis_runs SET lease_expires_at = '2000-01-01T00:00:00.000Z' "
        "WHERE analysis_id = ?", (env["run"],))
    env["conn"].commit()
    lease = env["analyses"].claim(env["run"], worker="w1")
    assert lease is not None
    return env["analyses"].commit_result(
        analysis_id=env["run"], fencing_token=lease["fencing_token"], status=status,
        stages=stages if stages is not None else [{"capability": "dsp_rms", "status": "succeeded"}],
        evidence=evidence,
    )


def _payload(env):
    return env["conn"].execute(
        "SELECT payload FROM evidence_items WHERE analysis_id = ?", (env["run"],)
    ).fetchone()["payload"]


# ── S01：最终 payload 必须过自家 schema；正常 helper 链路全绿 ──────────
class TestS01FinalContract:
    def test_normal_helper_chain_passes_own_schema(self, env):
        vid, tb = _variant(env)
        ev = make_rms_measurement(value=-9.0, range_={"start_sample": 0, "end_sample": 100},
                                  input_variant_id=vid, timebase_id=tb)
        assert "evidence_id" not in ev, "草稿不再预置 null evidence_id"
        _commit(env, [ev])
        payload = json.loads(_payload(env))
        # 回填了权威字段，且最终形态通过自家 schema
        assert payload["evidence_id"].startswith("ev_")
        assert payload["audio_id"] == env["audio_id"]
        assert payload["analysis_id"] == env["run"]
        assert payload["revision"] >= 1
        assert not list(_VALIDATOR.iter_errors(payload)), \
            [e.message for e in _VALIDATOR.iter_errors(payload)]

    @pytest.mark.parametrize("over", [
        {"parameters": "not-an-object"},
        {"fixture_only": "false"},
        {"calibration": {"alpha": 1}},
        {"evidence_id": 17},
        {"audio_id": 17},
        {"analysis_id": ["bogus"]},
        {"revision": 0},
        {"raw_score": "not-a-number"},
    ])
    def test_optional_field_violations_rejected(self, env, over):
        vid, tb = _variant(env)
        ev = make_rms_measurement(value=-9.0, range_={"start_sample": 0, "end_sample": 100},
                                  input_variant_id=vid, timebase_id=tb)
        ev.update(over)
        with pytest.raises(AnalysisError) as ei:
            _commit(env, [ev])
        assert ei.value.code == "invalid_evidence" and ei.value.http_status == 422
        assert env["conn"].execute(
            "SELECT COUNT(*) c FROM evidence_items WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["c"] == 0


# ── S02：partial 语义与输入合同完整执行 ────────────────────────────────
class TestS02PartialSemantics:
    def test_all_succeeded_partial_rejected(self, env):
        vid, tb = _variant(env)
        with pytest.raises(AnalysisError) as ei:
            _commit(env, [], status="partial",
                    stages=[{"capability": "events", "status": "succeeded"}])
        assert "missing/incomplete capability" in str(ei.value)

    def test_unknown_reason_rejected(self, env):
        vid, tb = _variant(env)
        with pytest.raises(AnalysisError) as ei:
            _commit(env, [], status="partial",
                    stages=[{"capability": "events", "status": "skipped",
                             "reason_code": "not_a_contract_reason"}])
        assert "contract enum" in str(ei.value)

    def test_legal_partial_passes(self, env):
        vid, tb = _variant(env)
        result = _commit(env, [], status="partial",
                         stages=[{"capability": "dsp_rms", "status": "succeeded"},
                                 {"capability": "events", "status": "skipped",
                                  "reason_code": "provider_not_configured"}])
        assert result["status"] == "partial"

    def test_tts_boolean_binding_rejected(self, env):
        with pytest.raises(AssetError):
            env["assets"].register_upload(
                owner=OWNER, data=b"\x00" * 8, source_kind="assistant_tts",
                origin={"voice_profile_id": True, "voice_profile_version": "3"})


# ── S03：上传失败不留孤儿对象 ──────────────────────────────────────────
class TestS03UploadOrphan:
    def test_failed_metadata_upload_cleans_its_object(self, env):
        originals = env["tmp"] / "objects" / "originals"
        first = env["assets"].register_upload(
            owner=OWNER, data=b"first-bytes", source_kind="external_import",
            origin={"kind": "external", "external_id": "ext-42"})
        before = {p.name for p in originals.glob("*")}
        with pytest.raises(Exception):
            env["assets"].register_upload(
                owner=OWNER, data=b"second-bytes", source_kind="external_import",
                origin={"kind": "external", "external_id": "ext-42"})   # UNIQUE 冲突
        after = {p.name for p in originals.glob("*")}
        assert after == before, f"失败上传留下了孤儿对象：{after - before}"
        assert first["audio_id"] in after
        # owner 删除全部已登记资产后，目录干净
        for row in env["conn"].execute(
                "SELECT audio_id FROM audio_assets WHERE owner = ?", (OWNER,)):
            env["assets"].request_delete(row["audio_id"], principal=OWNER)
        assert not list(originals.glob("*"))


# ── S04：删除赢在创建之前 → 无新 queued 任务 ──────────────────────────
class TestS04CreateDeleteRace:
    def test_create_after_delete_returns_404_no_queued_garbage(self, env):
        def inject(real):
            AssetService(real, ObjectStore(env["tmp"] / "objects")).request_delete(
                env["audio_id"], principal=OWNER)

        racy = AnalysisService(Interpose(
            env["conn"],
            lambda sql: sql.startswith("INSERT INTO analysis_runs"),
            inject,
        ))
        with pytest.raises(AnalysisError) as ei:
            racy.create(audio_id=env["audio_id"], profile="conversation_fast",
                        requested_by=OWNER)
        assert ei.value.http_status == 404
        queued = env["conn"].execute(
            """SELECT COUNT(*) c FROM analysis_runs ar JOIN audio_assets aa
               ON aa.audio_id = ar.audio_id
               WHERE aa.lifecycle != 'active' AND ar.status = 'queued'"""
        ).fetchone()["c"]
        assert queued == 0, "不得给已删除资产留下永远无法执行的任务"
        # 夹具任务已被删除链路取消（非 queued）
        fixture_run = env["conn"].execute(
            "SELECT status FROM analysis_runs WHERE analysis_id = ?", (env["run"],)
        ).fetchone()["status"]
        assert fixture_run == "cancelled"


# ── O1：结构损坏（列被错误还原弄丢）→ ready 拒绝 ──────────────────────
class TestO01ColumnCheck:
    def test_missing_column_not_ready(self, tmp_path):
        from fastapi.testclient import TestClient

        from oido.api.http import create_app
        from oido.config import OidoConfig
        conn = open_db(tmp_path / "col.sqlite3")
        migrate(conn)
        conn.execute("ALTER TABLE analysis_runs DROP COLUMN request_json")
        conn.commit()
        body = TestClient(create_app(
            config=OidoConfig(data_root=tmp_path / "d", migrations_dir=REPO / "migrations"),
            conn=conn)).get("/health/ready").json()
        assert body["status"] == "degraded"
        assert body["reason"].startswith("missing_column")


# ── O2：并发迁移者假失败恢复 ───────────────────────────────────────────
class TestO02ConcurrentMigrator:
    def test_unique_race_treated_as_done(self, tmp_path):
        import shutil

        d_full = tmp_path / "full"
        d_v1 = tmp_path / "v1"
        for d in (d_full, d_v1):
            d.mkdir()
        shutil.copy(REPO / "migrations" / "0001_init.sql", d_full / "0001_init.sql")
        shutil.copy(REPO / "migrations" / "0002_request_payload.sql", d_full / "0002_request_payload.sql")
        shutil.copy(REPO / "migrations" / "0001_init.sql", d_v1 / "0001_init.sql")

        db = open_db(tmp_path / "race.sqlite3")
        conn2 = open_db(tmp_path / "race.sqlite3")
        assert migrate(db, d_v1) == [1]

        def inject(_real):
            # 另一个迁移者抢先完成 v2 并登记
            conn2.execute("BEGIN")
            conn2.execute("ALTER TABLE analysis_runs ADD COLUMN request_json TEXT")
            conn2.execute("INSERT INTO schema_migrations VALUES (2, '2026-10-01T00:00:00Z')")
            conn2.execute("COMMIT")

        class FirstBegin(Interpose):
            """v1 已应用，migrate 里第一个 BEGIN IMMEDIATE 就是 v2 的——此刻注入竞争者。"""

            def __init__(self, real, inject):
                super().__init__(real, lambda sql: False, inject)
                self._fired = False

            def execute(self, sql, params=()):
                if sql.startswith("BEGIN") and not self._fired:
                    self._fired = True
                    self._inject(self._real)
                return self._real.execute(sql, params)

        result = migrate(FirstBegin(db, inject), d_full)
        assert result == [], "并发完成同一版本应静默视为已完成"
        versions = {r["version"] for r in db.execute("SELECT version FROM schema_migrations")}
        assert versions == {1, 2}
