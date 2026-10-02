"""三次复审（2026-10-02，基线 7a8d65a）T01/T02/O02 残留的回归钉。

- T01：同批重复 capability 拒绝——校验集合与最终持久集合恒等（读回验证）
- T02：补偿失败登记孤儿（带归属）→ 恢复 → 可定位并清理（闭环），含自愈清扫
- O02：并发迁移 BEGIN IMMEDIATE——B 先完成 A 静默返回 []；双方争锁一方胜出
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from oido.service.analyses import AnalysisError, AnalysisService
from oido.service.assets import AssetService
from oido.storage.db import migrate, open_db
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
    assets = AssetService(conn, ObjectStore(tmp_path / "objects"))
    analyses = AnalysisService(conn)
    audio_id = assets.register_upload(owner=OWNER, data=b"\x00" * 64,
                                      source_kind="user_upload")["audio_id"]
    run = analyses.create(audio_id=audio_id, profile="conversation_fast",
                          requested_by=OWNER)["analysis_id"]
    return {"db": db_path, "conn": conn, "assets": assets, "analyses": analyses,
            "audio_id": audio_id, "run": run, "tmp": tmp_path}


def _claim(env):
    env["conn"].execute(
        "UPDATE analysis_runs SET lease_expires_at = '2000-01-01T00:00:00.000Z' "
        "WHERE analysis_id = ?", (env["run"],))
    env["conn"].commit()
    lease = env["analyses"].claim(env["run"], worker="w1")
    assert lease is not None
    return lease


# ── T01：校验集合 = 最终持久集合 ───────────────────────────────────────
class TestT01DuplicateCapability:
    @pytest.mark.parametrize("order", [
        [{"capability": "events", "status": "skipped",
          "reason_code": "provider_not_configured"},
         {"capability": "events", "status": "succeeded"}],
        [{"capability": "events", "status": "succeeded"},
         {"capability": "events", "status": "skipped",
          "reason_code": "provider_not_configured"}],
    ])
    def test_duplicate_batch_rejected_zero_persist(self, env, order):
        lease = _claim(env)
        with pytest.raises(AnalysisError) as ei:
            env["analyses"].commit_result(
                analysis_id=env["run"], fencing_token=lease["fencing_token"],
                status="partial", stages=order)
        assert "duplicate capability" in str(ei.value)
        # 读回验证：任务未终结、无任何 stage 行（最终集合没有被合并污染）
        row = env["conn"].execute(
            "SELECT status FROM analysis_runs WHERE analysis_id = ?", (env["run"],)
        ).fetchone()
        assert row["status"] == "running"
        assert env["conn"].execute(
            "SELECT COUNT(*) c FROM analysis_stages WHERE analysis_id = ?",
            (env["run"],)).fetchone()["c"] == 0

    def test_final_set_carries_missing_reason(self, env):
        # 正向：不同 capability 的 succeeded+skipped → 最终库里两行都在，缺失原因可读
        lease = _claim(env)
        env["analyses"].commit_result(
            analysis_id=env["run"], fencing_token=lease["fencing_token"],
            status="partial",
            stages=[{"capability": "dsp_rms", "status": "succeeded"},
                    {"capability": "events", "status": "skipped",
                     "reason_code": "provider_not_configured"}])
        stages = {r["capability"]: r for r in env["conn"].execute(
            "SELECT capability, status, reason_code FROM analysis_stages WHERE analysis_id = ?",
            (env["run"],))}
        assert set(stages) == {"dsp_rms", "events"}
        assert stages["events"]["reason_code"] == "provider_not_configured"


# ── T02：补偿失败 → 登记 → 恢复 → 可定位并清理 ────────────────────────
class TestT02OrphanRegistry:
    def test_compensation_failure_recorded_then_cleaned(self, env):
        originals = env["tmp"] / "objects" / "originals"
        first = env["assets"].register_upload(
            owner=OWNER, data=b"first", source_kind="external_import",
            origin={"kind": "external", "external_id": "ext-t02"})

        class CompensateFails(ObjectStore):
            """元数据失败后的补偿 unlink 恰好失败一次（T02 复现路径）。"""

            def __init__(self, root):
                super().__init__(root)
                self.failed_once = False

            def delete(self, key):
                # 只挡"补偿"：本次失败上传的 key 尚未入库，不在已登记集合里
                known = {r["original_object_key"] for r in env["conn"].execute(
                    "SELECT original_object_key FROM audio_assets")}
                if key not in known and not self.failed_once:
                    self.failed_once = True
                    raise OSError("compensation unlink failed")
                return super().delete(key)

        flaky = CompensateFails(env["tmp"] / "objects")
        env["assets"].objects = flaky
        with pytest.raises(Exception):
            env["assets"].register_upload(
                owner=OWNER, data=b"second", source_kind="external_import",
                origin={"kind": "external", "external_id": "ext-t02"})

        # 登记在案：带归属、未解决
        orphans = env["conn"].execute(
            "SELECT object_key, audio_id, owner, resolved_at FROM orphan_objects"
        ).fetchall()
        assert len(orphans) == 1
        assert orphans[0]["owner"] == OWNER and orphans[0]["resolved_at"] is None
        orphan_key = orphans[0]["object_key"]
        assert (env["tmp"] / "objects" / orphan_key).exists()
        # 原对象不受影响
        assert (originals / first["audio_id"]).exists()

        # 恢复后：显式清扫闭环——文件删、行标 resolved
        env["assets"].objects = ObjectStore(env["tmp"] / "objects")
        result = env["assets"].cleanup_orphans(OWNER)
        assert result == {"candidates": 1, "resolved": 1}
        assert not (env["tmp"] / "objects" / orphan_key).exists()

    def test_next_successful_upload_self_heals(self, env):
        class CompensateFails(ObjectStore):
            def __init__(self, root):
                super().__init__(root)
                self.failed_once = False

            def delete(self, key):
                known = {r["original_object_key"] for r in env["conn"].execute(
                    "SELECT original_object_key FROM audio_assets")}
                if key not in known and not self.failed_once:
                    self.failed_once = True
                    raise OSError("compensation unlink failed")
                return super().delete(key)

        env["assets"].register_upload(
            owner=OWNER, data=b"first", source_kind="external_import",
            origin={"kind": "external", "external_id": "ext-t02b"})
        env["assets"].objects = CompensateFails(env["tmp"] / "objects")
        with pytest.raises(Exception):
            env["assets"].register_upload(
                owner=OWNER, data=b"doomed", source_kind="external_import",
                origin={"kind": "external", "external_id": "ext-t02b"})
        assert env["conn"].execute(
            "SELECT COUNT(*) c FROM orphan_objects WHERE resolved_at IS NULL"
        ).fetchone()["c"] == 1

        # 同 owner 下一次成功上传自动清扫（自愈），无需人工介入
        env["assets"].objects = ObjectStore(env["tmp"] / "objects")
        env["assets"].register_upload(owner=OWNER, data=b"ok-bytes",
                                      source_kind="user_upload")
        assert env["conn"].execute(
            "SELECT COUNT(*) c FROM orphan_objects WHERE resolved_at IS NULL"
        ).fetchone()["c"] == 0


# ── O02：BEGIN IMMEDIATE 后的两种并发交错 ─────────────────────────────
class TestO02Immediate:
    def _dirs(self, tmp_path):
        d_full, d_v1 = tmp_path / "full", tmp_path / "v1"
        for d in (d_full, d_v1):
            d.mkdir()
        shutil.copy(REPO / "migrations" / "0001_init.sql", d_v1 / "0001_init.sql")
        shutil.copy(REPO / "migrations" / "0001_init.sql", d_full / "0001_init.sql")
        shutil.copy(REPO / "migrations" / "0002_request_payload.sql",
                    d_full / "0002_request_payload.sql")
        return d_full, d_v1

    def test_other_migrator_finished_first_returns_empty(self, tmp_path):
        d_full, d_v1 = self._dirs(tmp_path)
        db = open_db(tmp_path / "a.sqlite3")
        conn2 = open_db(tmp_path / "a.sqlite3")
        assert migrate(db, d_v1) == [1]

        def inject(_real):
            conn2.execute("BEGIN IMMEDIATE")
            conn2.execute("ALTER TABLE analysis_runs ADD COLUMN request_json TEXT")
            conn2.execute("INSERT INTO schema_migrations VALUES (2, '2026-10-02T00:00:00Z')")
            conn2.execute("COMMIT")

        class OtherFirst(Interpose):
            def __init__(self, real, inject):
                super().__init__(real, lambda sql: False, inject)
                self._fired = False

            def execute(self, sql, params=()):
                if sql.startswith("BEGIN") and not self._fired:
                    self._fired = True
                    self._inject(self._real)
                return self._real.execute(sql, params)

        assert migrate(OtherFirst(db, inject), d_full) == []
        assert {r["version"] for r in db.execute("SELECT version FROM schema_migrations")} == {1, 2}

    def test_both_race_one_wins_no_corruption(self, tmp_path):
        d_full, d_v1 = self._dirs(tmp_path)
        db = open_db(tmp_path / "b.sqlite3")
        conn2 = open_db(tmp_path / "b.sqlite3")
        assert migrate(db, d_v1) == [1]

        def inject(_real):
            # B 在 A 已取写锁后抢着提交：B 失败（被吞，模拟另一进程重试）
            try:
                conn2.execute("BEGIN IMMEDIATE")
                conn2.execute("ALTER TABLE analysis_runs ADD COLUMN request_json TEXT")
                conn2.execute("INSERT INTO schema_migrations VALUES (2, '2026-10-02T00:00:00Z')")
                conn2.execute("COMMIT")
            except sqlite3.OperationalError:
                pass

        class AfterLock(Interpose):
            """A 取到锁、执行 PRAGMA 之前，B 试图插入（将 busy 失败）。"""

            def __init__(self, real, inject):
                super().__init__(real, lambda sql: False, inject)
                self._locked = False
                self._fired = False

            def execute(self, sql, params=()):
                if sql.startswith("BEGIN"):
                    self._locked = True
                elif self._locked and not self._fired:
                    self._fired = True
                    self._inject(self._real)
                return self._real.execute(sql, params)

        result = migrate(AfterLock(db, inject), d_full)
        # A 正常完成 v2（B 失败不碍事），无快照假失败
        assert result == [2]
        cols = {r["name"] for r in db.execute("PRAGMA table_info(analysis_runs)")}
        assert "request_json" in cols
        assert {r["version"] for r in db.execute("SELECT version FROM schema_migrations")} == {1, 2}
