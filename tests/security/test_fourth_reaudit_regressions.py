"""四审（2026-10-02，基线 7ccf055）T02 残留的回归钉。

复审闭合条件（原文）：
  持写锁使元数据不可写 + 补偿 unlink 一次失败 → 释放锁并重启 →
  能定位待恢复对象，**或此前根本未产生无索引对象** → 清理/owner 删除后无残留；
  同时保持原 UNIQUE、自愈、owner 隔离与**有效对象保护**测试通过。
"""

import pytest

from oido.service.assets import AssetService
from oido.storage.db import migrate, open_db
from oido.storage.objects import ObjectStore

OWNER = "qiaosheng"


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


class RaisingStore(ObjectStore):
    """put/delete 可分别注入一次性故障；记录 put 是否被调用过。"""

    def __init__(self, root, *, fail_put=False, fail_delete_for_unknown_once=False, conn=None):
        super().__init__(root)
        self.fail_put = fail_put
        self.fail_delete_once = fail_delete_for_unknown_once
        self.put_called = False
        self._conn = conn

    def put_bytes(self, key, data):
        self.put_called = True
        if self.fail_put:
            raise OSError("disk full")
        return super().put_bytes(key, data)

    def delete(self, key):
        if self.fail_delete_once:
            known = set()
            if self._conn is not None:
                known = {r["original_object_key"] for r in self._conn.execute(
                    "SELECT original_object_key FROM audio_assets")}
            if key not in known:
                self.fail_delete_once = False
                raise OSError("compensation unlink failed")
        return super().delete(key)


@pytest.fixture()
def env(tmp_path):
    conn = open_db(tmp_path / "meta" / "oido.sqlite3")
    migrate(conn)
    assets = AssetService(conn, ObjectStore(tmp_path / "objects"))
    return {"conn": conn, "assets": assets, "tmp": tmp_path}


def _ext(origin_id):
    return {"kind": "external", "external_id": origin_id}


class TestFourthReauditT02:
    def test_db_unwritable_never_creates_file(self, env):
        """闭合选项 B：写锁先挡住写前意图 → 根本不产生无索引对象。"""
        import sqlite3

        originals = env["tmp"] / "objects" / "originals"
        env["assets"].register_upload(owner=OWNER, data=b"first",
                                      source_kind="external_import", origin=_ext("e4"))

        def inject(_real):
            pass  # 由包装直接抛出，模拟持锁连接导致的 SQLITE_BUSY

        class BusyOnIntent(Interpose):
            def execute(self, sql, params=()):
                if sql.startswith("INSERT INTO orphan_objects"):
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, params)

        store = RaisingStore(env["tmp"] / "objects")
        env["assets"].objects = store
        busy = AssetService(BusyOnIntent(env["conn"], lambda s: False, inject), store)
        with pytest.raises(sqlite3.OperationalError):
            busy.register_upload(owner=OWNER, data=b"doomed-bytes",
                                 source_kind="external_import", origin=_ext("e4"))
        # 关键断言：文件从未写入（写前意图先于 put_bytes）
        assert store.put_called is False
        assert not list(originals.glob("aud_*")) or all(
            p.name != "aud_new" for p in originals.glob("*"))
        # 目录里只有第一次成功上传的那一个对象
        files = [p.name for p in originals.glob("*")]
        assert len(files) == 1

    def test_lock_between_intent_and_metadata_orphan_locatable(self, env):
        """闭合选项 A：意图已持久化后元数据 BUSY + 补偿 unlink 失败 → 可定位可清理。"""
        import sqlite3

        env["assets"].register_upload(owner=OWNER, data=b"first",
                                      source_kind="external_import", origin=_ext("e5"))
        originals = env["tmp"] / "objects" / "originals"

        class BusyOnAssetInsert(Interpose):
            def execute(self, sql, params=()):
                if sql.startswith("INSERT INTO audio_assets"):
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, params)

        store = RaisingStore(env["tmp"] / "objects",
                             fail_delete_for_unknown_once=True, conn=env["conn"])
        env["assets"].objects = store
        busy = AssetService(BusyOnAssetInsert(env["conn"], lambda s: False, lambda r: None), store)
        with pytest.raises(sqlite3.OperationalError):
            busy.register_upload(owner=OWNER, data=b"locked-meta",
                                 source_kind="external_import", origin=_ext("e5"))

        # 释放锁后（新连接视角）：写前登记的意图行可见、带归属、未解决
        fresh = open_db(env["tmp"] / "meta" / "oido.sqlite3")
        rows = fresh.execute(
            "SELECT object_key, audio_id, owner, resolved_at FROM orphan_objects"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["owner"] == OWNER and rows[0]["resolved_at"] is None
        assert (env["tmp"] / "objects" / rows[0]["object_key"]).exists()

        # 显式清理（宽限 0）→ 文件删、行 resolved；owner 全删后无残留
        AssetService(fresh, ObjectStore(env["tmp"] / "objects")).cleanup_orphans(OWNER)
        assert not (env["tmp"] / "objects" / rows[0]["object_key"]).exists()
        for row in fresh.execute("SELECT audio_id FROM audio_assets WHERE owner = ?", (OWNER,)):
            AssetService(fresh, ObjectStore(env["tmp"] / "objects")).request_delete(
                row["audio_id"], principal=OWNER)
        assert not list(originals.glob("*"))

    def test_cleanup_never_deletes_live_asset_object(self, env):
        """引用核对：被 active 资产引用的陈旧意图行不删文件，仅标 resolved。"""
        good = env["assets"].register_upload(owner=OWNER, data=b"live-object",
                                             source_kind="user_upload")
        conn = env["conn"]
        conn.execute(
            """INSERT INTO orphan_objects (object_key, audio_id, owner, created_at)
               VALUES (?, 'aud_ghost', ?, '2020-01-01T00:00:00.000Z')""",
            (f"originals/{good['audio_id']}", OWNER),
        )
        conn.commit()
        result = env["assets"].cleanup_orphans(OWNER)
        assert result["resolved"] == 0, "被引用对象不得进入删除集"
        assert (env["tmp"] / "objects" / "originals" / good["audio_id"]).exists()
        assert conn.execute(
            "SELECT COUNT(*) c FROM orphan_objects WHERE resolved_at IS NULL"
        ).fetchone()["c"] == 0, "陈旧行应标记 resolved（对象有效）"

    def test_self_heal_grace_protects_fresh_intents(self, env):
        """宽限：刚登记的未解决行（可能是进行中上传）不被自愈误删。"""
        env["assets"].register_upload(owner=OWNER, data=b"first",
                                      source_kind="external_import", origin=_ext("e6"))
        conn = env["conn"]
        # 人为造一个"新鲜"孤儿（模拟另一进行中/刚失败的意图）
        conn.execute(
            """INSERT INTO orphan_objects (object_key, audio_id, owner, created_at)
               VALUES ('originals/aud_fresh', 'aud_fresh', ?, '2999-01-01T00:00:00.000Z')""",
            (OWNER,),
        )
        conn.commit()
        (env["tmp"] / "objects" / "originals" / "aud_fresh").write_bytes(b"mid-flight")
        env["assets"].register_upload(owner=OWNER, data=b"ok",
                                      source_kind="user_upload")
        # 新鲜行未被自愈触碰（owner 隔离 + 宽限双重保护下保留）
        row = env["conn"].execute(
            "SELECT resolved_at FROM orphan_objects WHERE object_key = 'originals/aud_fresh'"
        ).fetchone()
        assert row["resolved_at"] is None
        assert (env["tmp"] / "objects" / "originals" / "aud_fresh").exists()

    def test_unique_conflict_path_still_cleans(self, env):
        """回归：原 UNIQUE+补偿成功路径不受重构影响（文件与意图行都清掉）。"""
        env["assets"].register_upload(owner=OWNER, data=b"first",
                                      source_kind="external_import", origin=_ext("e7"))
        with pytest.raises(Exception):
            env["assets"].register_upload(owner=OWNER, data=b"second",
                                          source_kind="external_import", origin=_ext("e7"))
        assert env["conn"].execute("SELECT COUNT(*) c FROM orphan_objects").fetchone()["c"] == 0
        files = [p.name for p in (env["tmp"] / "objects" / "originals").glob("*")]
        assert len(files) == 1
