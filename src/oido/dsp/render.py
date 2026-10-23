"""视图渲染：从真实数值画图（JOINT-VOICE-20261022 · 05 返回分层 · view 工具）。

全部图形由测量值直接绘制（波形包络/STFT 频谱/F0 曲线/RMS 曲线），
无任何生成式内容伪造频谱。纯 PIL+numpy，无 CJK 字体依赖（刻度仅 ASCII）。
图尺寸固定 1000×320；range 裁剪在样本级精确执行。
"""

from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image, ImageDraw

W, H = 1000, 320
ML, MR, MT, MB = 44, 12, 14, 26          # 边距（左/右/上/下）
PW, PH = W - ML - MR, H - MT - MB        # 绘图区
VIEWS = ("overview", "waveform", "spectrogram", "pitch", "energy")


class RenderError(Exception):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _canvas(title: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((ML, 1), title, fill="#444444")
    return img, d


def _draw_axis(d: ImageDraw.ImageDraw, t0_s: float, t1_s: float, *, y_labels: list[tuple[float, str]] | None = None) -> None:
    d.rectangle([ML, MT, ML + PW, MT + PH], outline="#999999")
    # 时间刻度：4 段
    for i in range(5):
        t = t0_s + (t1_s - t0_s) * i / 4
        x = ML + PW * i / 4
        d.line([x, MT + PH, x, MT + PH + 4], fill="#999999")
        d.text((x - 10, MT + PH + 6), f"{t:.1f}s", fill="#666666")
    for frac, label in (y_labels or []):
        y = MT + PH * (1 - frac)
        d.line([ML - 4, y, ML, y], fill="#999999")
        d.text((2, y - 5), label, fill="#666666")


def render_waveform(samples: np.ndarray, sr: int, t0_s: float, t1_s: float) -> bytes:
    img, d = _canvas("waveform (float PCM)")
    n = samples.shape[0]
    if n == 0:
        _draw_axis(d, t0_s, t1_s, y_labels=[(0.5, "0")])
        return _png(img)
    per = max(1, n // PW)
    env_max, env_min = [], []
    for x in range(PW):
        a = x * n // PW
        b = max(a + 1, min(n, a + per))
        seg = samples[a:b]
        env_max.append(float(seg.max()))
        env_min.append(float(seg.min()))
    peak = max(1e-6, max(abs(v) for v in env_max + env_min))
    for x, (vmax, vmin) in enumerate(zip(env_max, env_min)):
        y1 = MT + PH * (0.5 - 0.48 * vmax / peak)
        y2 = MT + PH * (0.5 - 0.48 * vmin / peak)
        d.line([ML + x, y1, ML + x, y2], fill="#1a5fb4")
    _draw_axis(d, t0_s, t1_s, y_labels=[(1.0, f"{peak:.2f}"), (0.5, "0"), (0.0, f"-{peak:.2f}")])
    return _png(img)


def render_spectrogram(samples: np.ndarray, sr: int, t0_s: float, t1_s: float) -> bytes:
    img, d = _canvas("spectrogram (STFT log magnitude)")
    if samples.shape[0] < 512:
        _draw_axis(d, t0_s, t1_s)
        return _png(img)
    from scipy.signal import spectrogram as stft

    f, tt, Sxx = stft(samples.astype(np.float64), fs=sr, nperseg=512, noverlap=384)
    mag = np.log10(np.maximum(Sxx, 1e-12))
    lo, hi = float(mag.min()), float(mag.max()) + 1e-9
    span = max(1e-9, float(hi) - float(lo))
    # 只画语音频段（<=8kHz 或 Nyquist）
    keep = f <= min(8000.0, sr / 2)
    fk = f[keep]
    mk = mag[keep]
    spec = Image.new("RGB", (PW, PH))
    px = spec.load()
    for x in range(PW):
        col = int(x * mk.shape[1] / PW)
        for y in range(PH):
            row = int((PH - 1 - y) * mk.shape[0] / PH)
            v = (float(mk[row, col]) - lo) / span
            px[x, y] = _heat(v)
    img.paste(spec, (ML, MT))
    fmax = fk[-1] if fk.size else sr / 2
    _draw_axis(d, t0_s, t1_s, y_labels=[(1.0, f"{fmax/1000:.0f}k"), (0.5, f"{fmax/2000:.1f}k"), (0.0, "0")])
    return _png(img)


def render_pitch(f0_hz: list[float | None], hop_s: float, t0_s: float, t1_s: float) -> bytes:
    img, d = _canvas("F0 (pYIN voiced frames)")
    voiced = [v for v in f0_hz if v is not None and math.isfinite(v) and v > 0]
    if voiced:
        lo, hi = min(voiced) * 0.9, max(voiced) * 1.1
    else:
        lo, hi = 60.0, 600.0
    span = hi - lo
    prev: tuple[float, float] | None = None
    for i, v in enumerate(f0_hz):
        x = ML + PW * i / max(1, len(f0_hz) - 1)
        if v is not None and math.isfinite(v) and v > 0:
            y = MT + PH * (1 - (v - lo) / span)
            if prev:
                d.line([prev, (x, y)], fill="#c01c28", width=2)
            prev = (x, y)
        else:
            prev = None  # unvoiced 断开——不伪造连线
    _draw_axis(d, t0_s, t1_s, y_labels=[(1.0, f"{hi:.0f}Hz"), (0.5, f"{(lo+hi)/2:.0f}"), (0.0, f"{lo:.0f}Hz")])
    return _png(img)


def render_energy(db_frames: np.ndarray, hop_s: float, t0_s: float, t1_s: float) -> bytes:
    img, d = _canvas("RMS energy (dBFS)")
    if db_frames.size:
        lo = max(-120.0, float(db_frames.min()) - 3)
        hi = min(0.0, float(db_frames.max()) + 3)
    else:
        lo, hi = -60.0, 0.0
    span = max(1e-6, hi - lo)
    pts = []
    for i, v in enumerate(db_frames):
        x = ML + PW * i / max(1, db_frames.size - 1)
        y = MT + PH * (1 - (float(v) - lo) / span)
        pts.append((x, y))
    if len(pts) >= 2:
        d.line(pts, fill="#26a269", width=2)
    _draw_axis(d, t0_s, t1_s, y_labels=[(1.0, f"{hi:.0f}dB"), (0.5, f"{(lo+hi)/2:.0f}"), (0.0, f"{lo:.0f}dB")])
    return _png(img)


def render_overview(samples: np.ndarray, sr: int, f0_hz: list[float | None], hop_s: float, t0_s: float, t1_s: float) -> bytes:
    png = render_waveform(samples, sr, t0_s, t1_s)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    d = ImageDraw.Draw(img)
    d.text((ML + 6, MT + 4), "+F0", fill="#c01c28")
    voiced = [v for v in f0_hz if v is not None and v > 0]
    if voiced:
        lo, hi = min(voiced) * 0.9, max(voiced) * 1.1
        span = max(1e-6, hi - lo)
        for i, v in enumerate(f0_hz):
            if v is None or v <= 0:
                continue
            x = ML + PW * i / max(1, len(f0_hz) - 1)
            y = MT + PH * 0.15 + PH * 0.3 * (1 - (v - lo) / span)  # 上部 30% 区域叠 F0
            d.point((x, y), fill="#c01c28")
    return _png(img)


def _heat(v: float) -> tuple[int, int, int]:
    v = min(1.0, max(0.0, v))
    # 深蓝→紫→红→黄的简单渐变（确定性映射）
    if v < 0.35:
        k = v / 0.35
        return (int(20 + 80 * k), int(30 + 20 * k), int(120 + 60 * k))
    if v < 0.7:
        k = (v - 0.35) / 0.35
        return (int(100 + 120 * k), int(50 + 40 * k), int(180 - 150 * k))
    k = (v - 0.7) / 0.3
    return (int(220 + 35 * k), int(90 + 130 * k), int(30 + 20 * k))


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
