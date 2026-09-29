"""相对音高：半音换算与参照范围（OIDO-ARCH §7.1 / EXEC §4.3）。

公式固定：semitone = 12 * log2(f0_hz / reference_f0_hz)。
每个结果必须携带 reference_scope；不同参照的半音不可直接横比。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# 允许的参照范围（EXEC §4.3：只允许这三种）
REFERENCE_SCOPES = ("qiaosheng_baseline", "jiaming_voice_profile", "utterance_median")


class PitchReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class PitchReference:
    scope: str
    # baseline@version 或 voice_profile_id@version；utterance_median 时为 None
    version: Optional[str] = None

    def __post_init__(self) -> None:
        if self.scope not in REFERENCE_SCOPES:
            raise PitchReferenceError(
                f"reference_scope must be one of {REFERENCE_SCOPES}, got {self.scope!r}"
            )
        if self.scope in ("qiaosheng_baseline", "jiaming_voice_profile") and not self.version:
            raise PitchReferenceError(f"{self.scope} requires an explicit version")
        if self.scope == "utterance_median" and self.version is not None:
            raise PitchReferenceError("utterance_median must not carry a version")

    def label(self) -> str:
        return self.version if self.version else self.scope


def semitones(f0_hz: float, reference_f0_hz: float, reference: PitchReference) -> float:
    """把 Hz 换算成相对半音。无可靠 F0（None/NaN/非正数）必须走调用方的空值路径。"""
    if not (math.isfinite(f0_hz) and f0_hz > 0):
        raise PitchReferenceError(f"f0_hz must be finite and positive, got {f0_hz}")
    if not (math.isfinite(reference_f0_hz) and reference_f0_hz > 0):
        raise PitchReferenceError(
            f"reference_f0_hz must be finite and positive, got {reference_f0_hz}"
        )
    return 12.0 * math.log2(f0_hz / reference_f0_hz)


def utterance_median_f0(f0_track: list[float]) -> Optional[float]:
    """本句基线：voiced 帧的中位数。空/全部无效返回 None，不伪造。"""
    voiced = sorted(v for v in f0_track if v is not None and math.isfinite(v) and v > 0)
    if not voiced:
        return None
    n = len(voiced)
    mid = n // 2
    return float(voiced[mid]) if n % 2 == 1 else 0.5 * (voiced[mid - 1] + voiced[mid])
