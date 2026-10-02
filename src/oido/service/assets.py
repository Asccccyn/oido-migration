"""资产服务：登记、来源、生命周期与 owner 删除（ARCH §5/§12.3）。

审计 F01/F02/F03 修复后的删除语义：
1. **意图先持久化**：事务内置 deletion_requested、取消任务（递增 fence）、写墓碑；
   提交后读取立即被阻断——此后任何 IO 失败都不丢失删除意图。
2. 对象清理（原声/派生/artifact）分阶段执行，单对象失败不回滚意图（可重试）。
3. 派生数据清库：evidence、stages、artifacts 行与对象一并清理。
删除权高于 pin、缓存、派生物。
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from oido.domain.ids import new_id, utc_now_rfc3339
from oido.domain.states import asset_transition
from oido.storage.objects import ObjectStore


class AssetError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AssetService:
    def __init__(self, conn: sqlite3.Connection, objects: ObjectStore):
        self.conn = conn
        self.objects = objects

    def register_upload(
        self,
        *,
        owner: str,
        data: bytes,
        source_kind: str,
        mime: Optional[str] = None,
        retention: str = "standard",
        origin: Optional[dict] = None,
    ) -> dict:
        if source_kind not in ("user_upload", "assistant_tts", "external_import"):
            raise AssetError("invalid_input", f"bad source_kind: {source_kind}")
        # R07/S02：TTS 音色版本绑定（D02）在持久化入口强制；字段必须是真字符串
        # （SQLite 会把 True 存成 "1"——不能用 str(...) 真值判断代替类型验证）
        if source_kind == "assistant_tts":
            vp = origin.get("voice_profile_id") if isinstance(origin, dict) else None
            vv = origin.get("voice_profile_version") if isinstance(origin, dict) else None
            if not (isinstance(vp, str) and vp.strip()
                    and isinstance(vv, str) and vv.strip()):
                raise AssetError(
                    "invalid_input",
                    "assistant_tts requires string origin.voice_profile_id + voice_profile_version (D02)",
                )
        audio_id = new_id("aud")
        key = f"originals/{audio_id}"
        try:
            sha = self.objects.put_bytes(key, data)
            now = utc_now_rfc3339()
            # 审计 F05：多语句写库全程带回滚
            self.conn.execute(
                """INSERT INTO audio_assets
                   (audio_id, owner, scope, source_kind, original_object_key, sha256, bytes,
                    media_mime, lifecycle, retention, created_at)
                   VALUES (?, ?, 'private', ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (audio_id, owner, source_kind, key, sha, len(data), mime, retention, now),
            )
            if origin:
                self.conn.execute(
                    """INSERT INTO audio_origins
                       (origin_id, audio_id, origin_kind, external_id,
                        voice_profile_id, voice_profile_version, provider_model_revision)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("org"), audio_id, origin.get("kind", "direct_upload"),
                        origin.get("external_id"), origin.get("voice_profile_id"),
                        origin.get("voice_profile_version"), origin.get("provider_model_revision"),
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            # S03：元数据失败后补偿删除自己刚写的对象（只删本次 key，不扫目录）。
            # T02：补偿再失败不得静默吞掉——登记带归属的孤儿记录，供自愈/手动清扫。
            try:
                self.objects.delete(key)
            except OSError:
                try:
                    self.conn.execute(
                        """INSERT OR IGNORE INTO orphan_objects
                           (object_key, audio_id, owner, created_at) VALUES (?, ?, ?, ?)""",
                        (key, audio_id, owner, utc_now_rfc3339()),
                    )
                    self.conn.commit()
                except sqlite3.Error:
                    pass   # 连登记都失败：无可再做之事；绝不掩盖原始异常
            raise
        # 自愈：本次上传成功，顺手清掉该 owner 此前补偿失败留下的孤儿
        # （有界、只动登记过的键；清扫失败不影响本次成功结果）
        try:
            self.cleanup_orphans(owner)
        except Exception:
            pass
        return {"audio_id": audio_id, "sha256": sha, "bytes": len(data), "lifecycle": "active"}

    def cleanup_orphans(self, owner: str) -> dict:
        """清扫该 owner 的未解决孤儿对象：删文件成功才标记 resolved。"""
        rows = self.conn.execute(
            """SELECT orphan_id, object_key FROM orphan_objects
               WHERE owner = ? AND resolved_at IS NULL""",
            (owner,),
        ).fetchall()
        resolved = 0
        for row in rows:
            try:
                self.objects.delete(row["object_key"])
            except OSError:
                continue
            self.conn.execute(
                "UPDATE orphan_objects SET resolved_at = ? WHERE orphan_id = ?",
                (utc_now_rfc3339(), row["orphan_id"]),
            )
            resolved += 1
        self.conn.commit()
        return {"candidates": len(rows), "resolved": resolved}

    def get(self, audio_id: str, *, principal: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM audio_assets WHERE audio_id = ?", (audio_id,)
        ).fetchone()
        if row is None:
            # 统一口径：不存在与无权同形，不泄露存在性（EXEC §4.1）
            raise AssetError("not_visible", "audio not found")
        if row["owner"] != principal:
            raise AssetError("not_visible", "audio not found")
        if row["lifecycle"] != "active":
            raise AssetError("not_visible", "audio not found")
        return dict(row)

    def request_delete(self, audio_id: str, *, principal: str) -> str:
        """owner 删除。幂等；IO 失败不丢意图。"""
        row = self.conn.execute(
            "SELECT * FROM audio_assets WHERE audio_id = ?", (audio_id,)
        ).fetchone()
        if row is None:
            return "already_gone"
        if row["owner"] != principal:
            raise AssetError("forbidden", "not owner")
        if row["lifecycle"] in ("purged", "tombstoned"):
            return "already_gone"

        # ── 第 1 步：意图先持久化（F02：此后任何失败都不回滚删除）──────────
        if row["lifecycle"] == "active":
            asset_transition("active", "deletion_requested")
        try:
            self.conn.execute(
                "UPDATE audio_assets SET lifecycle = 'deletion_requested', deleted_at = ? WHERE audio_id = ?",
                (utc_now_rfc3339(), audio_id),
            )
            # F01：取消任务必须同时递增 fencing_token，否则已删除资产的任务可被 claim 复活
            self.conn.execute(
                """UPDATE analysis_runs
                   SET status = 'cancelled', fencing_token = fencing_token + 1, updated_at = ?
                   WHERE audio_id = ? AND status IN ('queued', 'running')""",
                (utc_now_rfc3339(), audio_id),
            )
            self.conn.execute(
                """INSERT OR REPLACE INTO deletion_tombstones (audio_id, owner, deleted_at)
                   VALUES (?, ?, ?)""",
                (audio_id, principal, utc_now_rfc3339()),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        # ── 第 2 步：清理对象（单对象失败不回滚意图；下次重入可重试）────────
        # R01：失败对象的**数据库索引必须保留**，否则重试失去枚举目标。
        # variant 行不删（本来就保留）；artifact 行只在文件删除成功后删。
        cleanup_failed = False
        artifact_ids_removed: list[str] = []
        for variant in self.conn.execute(
            "SELECT object_key FROM audio_variants WHERE audio_id = ?", (audio_id,)
        ):
            if variant["object_key"]:
                try:
                    self.objects.delete(variant["object_key"])
                except OSError:
                    cleanup_failed = True
        artifact_rows = self.conn.execute(
            """SELECT a.artifact_id, a.object_key FROM artifacts a
               JOIN analysis_runs r ON r.analysis_id = a.analysis_id
               WHERE r.audio_id = ?""",
            (audio_id,),
        ).fetchall()
        for artifact in artifact_rows:
            try:
                self.objects.delete(artifact["object_key"])
                artifact_ids_removed.append(artifact["artifact_id"])
            except OSError:
                cleanup_failed = True          # 行保留：重试可再次枚举该对象
        try:
            self.objects.delete(row["original_object_key"])
        except OSError:
            cleanup_failed = True

        # ── 第 3 步：派生数据清库 + 终态（F03/R01）─────────────────────────
        try:
            self.conn.execute(
                """DELETE FROM evidence_items WHERE analysis_id IN
                   (SELECT analysis_id FROM analysis_runs WHERE audio_id = ?)""",
                (audio_id,),
            )
            self.conn.execute(
                """DELETE FROM analysis_stages WHERE analysis_id IN
                   (SELECT analysis_id FROM analysis_runs WHERE audio_id = ?)""",
                (audio_id,),
            )
            if artifact_ids_removed:
                marks = ",".join("?" for _ in artifact_ids_removed)
                self.conn.execute(
                    f"DELETE FROM artifacts WHERE artifact_id IN ({marks})",
                    artifact_ids_removed,
                )
            if cleanup_failed:
                # 清理未完成：保持 deletion_requested（可重试），不假装 tombstoned；
                # 读取与任务已被第 1 步阻断
                self.conn.commit()
                return "deletion_requested_pending_cleanup"
            self.conn.execute(
                "UPDATE audio_assets SET lifecycle = 'tombstoned' WHERE audio_id = ?",
                (audio_id,),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return "tombstoned"
