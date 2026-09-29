"""RMS / dBFS 测量（ARCH §7.1：数字 RMS 与 dBFS 并存；无声≠0 dBFS）。

reference_amplitude=1.0 为 float PCM 满幅（EXEC §3.5 fixture 口径）。
"""

from __future__ import annotations

import math

import numpy as np

FLOOR_DBFS = -120.0


def rms_dbfs(samples: np.ndarray, *, reference_amplitude: float = 1.0) -> dict:
    """返回合同化 measurement 值对象。无声返回 value=None + below_floor。"""
    arr = np.asarray(samples, dtype=np.float64)
    if arr.size == 0:
        raise ValueError("empty signal")
    if not np.isfinite(arr).all():
        raise ValueError("non-finite samples")
    rms = float(np.sqrt(np.mean(np.square(arr))))
    if rms <= 0.0:
        return {"value": None, "below_floor": True, "rms_linear": 0.0, "unit": "dBFS",
                "method": "rms_float_pcm_reference_1", "parameters": {"reference_amplitude": reference_amplitude, "floor_dbfs": FLOOR_DBFS}}
    dbfs = 20.0 * math.log10(rms / reference_amplitude)
    if dbfs < FLOOR_DBFS:
        return {"value": None, "below_floor": True, "rms_linear": rms, "unit": "dBFS",
                "method": "rms_float_pcm_reference_1", "parameters": {"reference_amplitude": reference_amplitude, "floor_dbfs": FLOOR_DBFS}}
    return {"value": dbfs, "below_floor": False, "rms_linear": rms, "unit": "dBFS",
            "method": "rms_float_pcm_reference_1", "parameters": {"reference_amplitude": reference_amplitude, "floor_dbfs": FLOOR_DBFS}}
