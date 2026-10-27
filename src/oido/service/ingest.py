"""受信本地导入与 audio_ref 解析（联合施工 03：带所有者的引用，不造万能资源平台）。

audio_ref = "<owner>:<opaque_id>"，owner ∈ {est, siren}。
解析只做精确登记匹配（origin_kind+external_id），不做语义检索、不接受 URL/路径。
导入幂等按 (origin_kind, external_id)：重放同字节返回原 audio_id；内容不符 409。
转写随导入一次性带入（来源明确），供对齐用——不作为"模型已听过声音"的证明。
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional

from oido.domain.ids import new_id, utc_now_rfc3339
from oido.service.assets import AssetError, AssetService

# owner → audio_origins.origin_kind 的固定映射（不开放任意 kind）
OWNER_ORIGIN_KIND = {"est": "est_asset", "siren": "siren_voice_message"}


class RefError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def parse_audio_ref(audio_ref: str) -> tuple[str, str]:
    """解析 owner:opaque_id。非法形态直接拒（不猜、不剥空白、不接受 URL）。"""
    if not isinstance(audio_ref, str) or ":" not in audio_ref:
        raise RefError("invalid_audio_ref", "audio_ref must be '<owner>:<opaque_id>'")
    owner, _, opaque = audio_ref.partition(":")
    if owner not in OWNER_ORIGIN_KIND:
        raise RefError("invalid_audio_ref", f"unknown owner: {owner!r}")
    if not opaque or len(opaque) > 128 or not all(c.isalnum() or c in "_-" for c in opaque):
        raise RefError("invalid_audio_ref", "opaque id must be 1-128 chars of [A-Za-z0-9_-]")
    return owner, opaque


class IngestService:
    def __init__(self, conn: sqlite3.Connection, assets: AssetService):
        self.conn = conn
        self.assets = assets

    def resolve(self, audio_ref: str, *, principal: str) -> str:
        """audio_ref → audio_id（精确匹配；不可见同 not_visible，不泄露存在性）。"""
        owner, opaque = parse_audio_ref(audio_ref)
        row = self.conn.execute(
            """SELECT aa.audio_id FROM audio_assets aa
               JOIN audio_origins og ON og.audio_id = aa.audio_id
               WHERE og.origin_kind = ? AND og.external_id = ?""",
            (OWNER_ORIGIN_KIND[owner], opaque),
        ).fetchone()
        if row is None:
            raise RefError("not_visible", "audio not found", 404)
        try:
            self.assets.get(row["audio_id"], principal=principal)  # owner/lifecycle 复核
        except AssetError as exc:
            raise RefError("not_visible", str(exc), 404) from exc
        return row["audio_id"]

    def ingest(
        self,
        *,
        principal: str,
        audio_ref: str,
        data: bytes,
        content_sha256: Optional[str] = None,
        mime: Optional[str] = None,
        transcript: Optional[dict] = None,
    ) -> dict:
        """受信导入：宿主（est）校验过读取权限后推送受限字节。

        transcript: {"text": str非空, "source": str, "source_revision": str|None}
        """
        owner, opaque = parse_audio_ref(audio_ref)
        if owner != principal:
            # 只允许宿主导入自己命名空间的资产（est 导入 est:...）
            raise RefError("forbidden", "cannot ingest into another owner's namespace", 403)
        if not data:
            raise RefError("invalid_input", "empty audio payload")
        actual_sha = hashlib.sha256(data).hexdigest()
        kind = OWNER_ORIGIN_KIND[owner]
        existing = self.conn.execute(
            "SELECT og.audio_id, aa.sha256 FROM audio_origins og "
            "JOIN audio_assets aa ON aa.audio_id = og.audio_id "
            "WHERE og.origin_kind = ? AND og.external_id = ?",
            (kind, opaque),
        ).fetchone()
        if existing is not None:
            if existing["sha256"] != actual_sha or (content_sha256 and content_sha256 != actual_sha):
                raise RefError(
                    "content_conflict",
                    "same external id already ingested with different bytes",
                    409,
                )
            return {"audio_id": existing["audio_id"], "deduplicated": True}

        if content_sha256 and content_sha256 != actual_sha:
            raise RefError("content_conflict", "declared digest does not match audio bytes", 409)
        if transcript is not None:
            if not isinstance(transcript.get("text"), str) or not transcript["text"].strip():
                raise RefError("invalid_input", "transcript.text must be a non-empty string")
            if not isinstance(transcript.get("source"), str) or not transcript["source"].strip():
                raise RefError("invalid_input", "transcript.source is required")

        registered = self.assets.register_upload(
            owner=principal,
            data=data,
            source_kind="external_import",
            mime=mime,
            origin={
                "kind": kind,
                "external_id": opaque,
                "provider_model_revision": None,
            },
        )
        audio_id = registered["audio_id"]
        if transcript is not None:
            # JV-Q1：入口已统一校验 text/source（上方 transcript is not None 分支），
            # 此处不再第二遍重复校验；register_upload 已写 audio_origins，只补转写行
            text = transcript["text"]
            source = transcript["source"]
            try:
                self.conn.execute(
                    """INSERT INTO audio_transcripts (audio_id, text, source, source_revision, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        audio_id,
                        text,
                        source,
                        transcript.get("source_revision"),
                        utc_now_rfc3339(),
                    ),
                )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise
        return {"audio_id": audio_id, "sha256": registered["sha256"], "deduplicated": False}

    def transcript_of(self, audio_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT text, source, source_revision FROM audio_transcripts WHERE audio_id = ?",
            (audio_id,),
        ).fetchone()
        return dict(row) if row is not None else None
