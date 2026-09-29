"""资产服务：登记、来源、生命周期与 owner 删除（ARCH §5/§12.3）。

删除权高于 pin、缓存、派生物；先阻断读取，后清对象；tombstone 先于备份恢复。
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
        source_kind: str,               # user_upload | assistant_tts | external_import
        mime: Optional[str] = None,
        retention: str = "standard",    # standard | temporary（store=false 语义）
        origin: Optional[dict] = None,
    ) -> dict:
        if source_kind not in ("user_upload", "assistant_tts", "external_import"):
            raise AssetError("invalid_input", f"bad source_kind: {source_kind}")
        audio_id = new_id("aud")
        key = f"originals/{audio_id}"
        sha = self.objects.put_bytes(key, data)
        now = utc_now_rfc3339()
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
        return {"audio_id": audio_id, "sha256": sha, "bytes": len(data), "lifecycle": "active"}

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
        """owner 删除：阻断读取 → 清派生物与对象 → tombstone。幂等。"""
        row = self.conn.execute(
            "SELECT * FROM audio_assets WHERE audio_id = ?", (audio_id,)
        ).fetchone()
        if row is None:
            return "already_gone"
        if row["owner"] != principal:
            raise AssetError("forbidden", "not owner")
        if row["lifecycle"] in ("purged", "tombstoned"):
            return "already_gone"
        # pending 任务先取消（fencing 会在提交侧挡住迟到 worker）
        self.conn.execute(
            """UPDATE analysis_runs SET status = 'cancelled', updated_at = ?
               WHERE audio_id = ? AND status IN ('queued', 'running')""",
            (utc_now_rfc3339(), audio_id),
        )
        if row["lifecycle"] == "active":
            asset_transition("active", "deletion_requested")
        self.conn.execute(
            "UPDATE audio_assets SET lifecycle = 'deletion_requested', deleted_at = ? WHERE audio_id = ?",
            (utc_now_rfc3339(), audio_id),
        )
        # 派生物与原声对象清理
        for variant in self.conn.execute(
            "SELECT object_key FROM audio_variants WHERE audio_id = ?", (audio_id,)
        ):
            if variant["object_key"]:
                self.objects.delete(variant["object_key"])
        self.objects.delete(row["original_object_key"])
        self.conn.execute(
            "UPDATE audio_assets SET lifecycle = 'tombstoned' WHERE audio_id = ?", (audio_id,)
        )
        self.conn.execute(
            """INSERT OR REPLACE INTO deletion_tombstones (audio_id, owner, deleted_at)
               VALUES (?, ?, ?)""",
            (audio_id, principal, utc_now_rfc3339()),
        )
        self.conn.commit()
        return "tombstoned"
