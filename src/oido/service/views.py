"""oido_view 落地：渲染产物入 artifacts（唯一所有者），同参数幂等复用。

联合施工 05：图从真实数值生成；range 样本级精确；渲染参数全文记录
（render_params）供复用与对账。F0 全曲线不入默认 JSON——本服务是它
的"按需展开"出口。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Optional

import numpy as np

from oido.domain.ids import new_id, utc_now_rfc3339
from oido.dsp.f0 import pyin_f0
from oido.dsp.render import VIEWS, RenderError, render_energy, render_overview, render_pitch, render_spectrogram, render_waveform
from oido.service.variants import VariantService
from oido.storage.objects import ObjectStore

RMS_FRAME, RMS_HOP = 480, 160          # 与 prosody/pipeline 同口径（16k 下 30ms/10ms）
F0_HOP = 256


def _frames_db(samples: np.ndarray) -> np.ndarray:
    n = (samples.shape[0] - RMS_FRAME) // RMS_HOP + 1
    if n <= 0:
        return np.array([])
    idx = np.arange(RMS_FRAME)[None, :] + RMS_HOP * np.arange(n)[:, None]
    frames = samples[idx]
    rms = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1))
    return np.where(rms > 0, 20.0 * np.log10(np.maximum(rms, 1e-12)), -120.0)


class ViewService:
    def __init__(self, conn: sqlite3.Connection, variants: VariantService, objects: ObjectStore):
        self.conn = conn
        self.variants = variants
        self.objects = objects

    def render(self, *, analysis_id: str, view: str, range_ms: Optional[list[int]] = None) -> dict:
        if view not in VIEWS:
            raise RenderError("unsupported", f"view must be one of {VIEWS}")
        run = self.conn.execute(
            "SELECT audio_id, revision, status FROM analysis_runs WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()
        if run is None or run["status"] not in ("succeeded", "partial"):
            raise RenderError("source_unavailable", f"analysis not terminal: {run['status'] if run else 'missing'}")
        variant = self.conn.execute(
            """SELECT * FROM audio_variants
               WHERE audio_id = ? AND transform_kind = 'decode_main' AND parent_variant_id IS NULL""",
            (run["audio_id"],),
        ).fetchone()
        if variant is None:
            raise RenderError("source_unavailable", "decode_main variant missing")
        render_params = {"view": view, "range_ms": range_ms, "revision": run["revision"],
                         "render_version": "oido-render-1"}
        key_digest = hashlib.sha256(json.dumps(render_params, sort_keys=True).encode()).hexdigest()
        existing = self.conn.execute(
            "SELECT artifact_id FROM artifacts WHERE analysis_id = ? AND kind = ? AND render_params = ?",
            (analysis_id, f"png_{view}", json.dumps(render_params, sort_keys=True)),
        ).fetchone()
        if existing is not None:
            png = self.objects.read_bytes(
                self.conn.execute(
                    "SELECT object_key FROM artifacts WHERE artifact_id = ?",
                    (existing["artifact_id"],),
                ).fetchone()["object_key"])
            return {"artifact_id": existing["artifact_id"], "reused": True, "png": png,
                    "view": view, "range_ms": range_ms}

        samples = self.variants.load_pcm(dict(variant))
        sr = variant["sample_rate_hz"]
        t0_s, t1_s = 0.0, samples.shape[0] / sr
        if range_ms is not None:
            if len(range_ms) != 2 or range_ms[0] < 0 or range_ms[1] <= range_ms[0]:
                raise RenderError("invalid_media", "range_ms must be [start,end) ms")
            t0_s, t1_s = range_ms[0] / 1000.0, range_ms[1] / 1000.0
            a = max(0, int(t0_s * sr))
            b = min(samples.shape[0], int(t1_s * sr))
            if b <= a:
                raise RenderError("insufficient_signal", "range covers no samples")
            samples = samples[a:b]
            t1_s = t0_s + samples.shape[0] / sr
        f0 = pyin_f0(samples, sr=sr, hop_length=F0_HOP)
        hop_s = F0_HOP / sr
        if view == "waveform":
            png = render_waveform(samples, sr, t0_s, t1_s)
        elif view == "spectrogram":
            png = render_spectrogram(samples, sr, t0_s, t1_s)
        elif view == "pitch":
            png = render_pitch(f0["f0_hz"] or [], hop_s, t0_s, t1_s)
        elif view == "energy":
            png = render_energy(_frames_db(samples), RMS_HOP / sr, t0_s, t1_s)
        else:
            png = render_overview(samples, sr, f0["f0_hz"] or [], hop_s, t0_s, t1_s)

        artifact_id = new_id("art")
        object_key = f"derivatives/{artifact_id}.png"
        sha = self.objects.put_bytes(object_key, png)
        try:
            self.conn.execute(
                """INSERT INTO artifacts (artifact_id, analysis_id, kind, object_key, sha256,
                                          media_type, render_params, created_at)
                   VALUES (?, ?, ?, ?, ?, 'image/png', ?, ?)""",
                (artifact_id, analysis_id, f"png_{view}", object_key, sha,
                 json.dumps(render_params, sort_keys=True), utc_now_rfc3339()),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            try:
                self.objects.delete(object_key)
            except OSError:
                pass
            raise
        return {"artifact_id": artifact_id, "reused": False, "png": png,
                "view": view, "range_ms": range_ms, "sha256": sha}
