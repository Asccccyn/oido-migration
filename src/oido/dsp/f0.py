"""F0 提取：pYIN 必须显式传真实采样率（D04/R02/R03——本模块存在的唯一理由）。

教训来源：参考项目 librosa.pyin(...) 不传 sr → 默认 22050 → 24/44.1/48 kHz
输入上频率与时间双双错缩放且"数值看似合理"。本封装在类型层面就拒绝无 sr 调用，
并有 A01 防回归测试（16/24/44.1/48 kHz 同源夹具）。
"""

from __future__ import annotations

from typing import Optional

import numpy as np


class F0Error(ValueError):
    pass


def validate_sample_rate(sr: int) -> int:
    if sr is None or not isinstance(sr, (int, np.integer)) or int(sr) <= 0:
        raise F0Error(f"explicit integer sample rate required, got {sr!r}")
    return int(sr)


def pyin_f0(
    samples: np.ndarray,
    *,
    sr: Optional[int] = None,          # 故意默认 None：不传就炸，杜绝 R02 类缺陷
    frame_length: int = 2048,
    hop_length: int = 256,
) -> dict:
    """显式 sr 的 pYIN。返回 Hz 序列 + voiced mask + 元数据。

    返回结构中的时间轴由 librosa 的 times 定义（帧起始约定随 center 参数，
    默认 center=True 为帧中心；元数据里如实记录）。
    """
    validate_sample_rate(sr)
    import librosa

    arr = np.asarray(samples, dtype=np.float64)
    if arr.size == 0:
        raise F0Error("empty signal")
    f0, voiced_flag, voiced_prob = librosa.pyin(
        arr,
        sr=int(sr),                      # ← 显式真实采样率（不可省略的关键行）
        fmin=60.0,
        fmax=600.0,                      # 人声域；出域不伪造
        frame_length=int(frame_length),
        hop_length=int(hop_length),
        center=True,
    )
    return {
        "sr_hz": int(sr),
        "f0_hz": None if f0 is None else [None if not np.isfinite(v) else float(v) for v in f0],
        "voiced": [bool(v) for v in voiced_flag],
        "voiced_prob": [None if not np.isfinite(p) else float(p) for p in voiced_prob],
        "frame_length": int(frame_length),
        "hop_length": int(hop_length),
        "center": True,
        "method": "librosa.pyin",
        "librosa_version": librosa.__version__,
    }


def robust_median_f0(f0_result: dict) -> Optional[float]:
    """voiced 帧中位 F0——供 utterance_median 参照使用；无 voiced 返回 None。"""
    pairs = [
        v for v, voiced in zip(f0_result["f0_hz"] or [], f0_result["voiced"])
        if voiced and v is not None
    ]
    if not pairs:
        return None
    return float(np.median(pairs))
