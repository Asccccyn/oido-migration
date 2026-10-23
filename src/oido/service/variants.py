"""解码变体：decode_main 的唯一生产写入方（audio_variants 表首次有真实写入）。

联合施工 05：原文件不可覆盖；每个分析变体记录解码/重采样/声道策略及源 revision。
PCM 正本入对象存储（derivatives/），库里只放行——大数组不进库（0001 schema 注释）。
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import numpy as np

from oido.domain.ids import new_id, utc_now_rfc3339
from oido.dsp.decode import DecodeError, decode_mono_pcm
from oido.storage.objects import ObjectStore

TRANSFORM_KIND = "decode_main"


class VariantError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 500):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class VariantService:
    def __init__(self, conn: sqlite3.Connection, objects: ObjectStore):
        self.conn = conn
        self.objects = objects

    def ensure_decode_main(self, *, audio_id: str, original_bytes: bytes) -> dict:
        """取该资产现行 decode_main 变体；没有则解码并持久化（幂等：已有即复用）。"""
        row = self.conn.execute(
            """SELECT v.* FROM audio_variants v JOIN audio_assets a ON a.audio_id = v.audio_id
               WHERE v.audio_id = ? AND transform_kind = ? AND parent_variant_id IS NULL
                 AND a.lifecycle = 'active'""",
            (audio_id, TRANSFORM_KIND),
        ).fetchone()
        if row is not None:
            return dict(row)
        try:
            result = decode_mono_pcm(original_bytes)
        except DecodeError as exc:
            raise VariantError(exc.reason_code, str(exc)) from exc
        pcm_bytes = result.samples.astype("<f4").tobytes()
        variant_id = new_id("var")
        object_key = f"derivatives/{variant_id}.f32"
        intent = self.conn.execute(
            """INSERT INTO orphan_objects (object_key, audio_id, owner, created_at)
               SELECT ?, audio_id, owner, ? FROM audio_assets WHERE audio_id = ? AND lifecycle = 'active'""",
            (object_key, utc_now_rfc3339(), audio_id))
        self.conn.commit()
        if intent.rowcount != 1:
            raise VariantError("source_unavailable", "audio was deleted during decoding", 404)
        try:
            sha = self.objects.put_bytes(object_key, pcm_bytes)
            inserted = self.conn.execute(
                """INSERT INTO audio_variants
                   (variant_id, audio_id, parent_variant_id, transform_kind,
                    sample_rate_hz, channels, sample_count, timebase_id,
                    transform_params, object_key, created_at)
                   SELECT ?, ?, NULL, ?, ?, 1, ?, ?, ?, ?, ?
                   WHERE EXISTS (SELECT 1 FROM audio_assets WHERE audio_id = ? AND lifecycle = 'active')""",
                (
                    variant_id,
                    audio_id,
                    TRANSFORM_KIND,
                    result.sample_rate_hz,
                    int(result.samples.shape[0]),
                    result.timebase_id,
                    __import__("json").dumps(result.transform_params, ensure_ascii=False,
                                             sort_keys=True, allow_nan=False),
                    object_key,
                    utc_now_rfc3339(),
                    audio_id,
                ),
            )
            if inserted.rowcount != 1:
                raise VariantError("source_unavailable", "audio was deleted during decoding", 404)
            self.conn.execute("DELETE FROM orphan_objects WHERE object_key = ?", (object_key,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            try:
                self.objects.delete(object_key)
                self.conn.execute("DELETE FROM orphan_objects WHERE object_key = ?", (object_key,))
                self.conn.commit()
            except OSError:
                pass
            except sqlite3.Error:
                self.conn.rollback()
            raise
        return {
            "variant_id": variant_id,
            "sample_rate_hz": result.sample_rate_hz,
            "sample_count": int(result.samples.shape[0]),
            "timebase_id": result.timebase_id,
            "transform_params": result.transform_params,
            "object_key": object_key,
            "sha256": sha,
        }

    def load_pcm(self, variant: dict) -> np.ndarray:
        """按变体行读回 PCM（float32 单声道）。行必须是本服务写入的 decode_main。"""
        raw = self.objects.read_bytes(variant["object_key"])
        samples = np.frombuffer(raw, dtype="<f4")
        expected = variant["sample_count"]
        if samples.shape[0] != expected:
            raise VariantError(
                "invalid_media",
                f"variant object corrupt: {samples.shape[0]} samples != {expected}",
            )
        return samples.astype(np.float32, copy=False)
