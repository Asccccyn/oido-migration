"""时间基准：半开样本区间是唯一真相源（OIDO-ARCH §6.2）。

秒/毫秒一律由 [start_sample, end_sample) + sample_rate_hz 计算，
禁止把毫秒显示值反算回样本再当定位依据。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any


class TimebaseError(ValueError):
    """样本区间或时间基准不合法。"""


@dataclass(frozen=True)
class SampleRange:
    """解码主视图上的半开样本区间 [start_sample, end_sample)。"""

    start_sample: int
    end_sample: int

    def __post_init__(self) -> None:
        if self.start_sample < 0 or self.end_sample <= self.start_sample:
            raise TimebaseError(
                f"invalid half-open range: [{self.start_sample}, {self.end_sample})"
            )

    @property
    def sample_count(self) -> int:
        return self.end_sample - self.start_sample

    def duration_seconds(self, sample_rate_hz: int) -> Fraction:
        if sample_rate_hz <= 0:
            raise TimebaseError(f"sample_rate_hz must be positive, got {sample_rate_hz}")
        return Fraction(self.sample_count, sample_rate_hz)

    def duration_ms(self, sample_rate_hz: int) -> float:
        # 显示值：由整数样本精确计算后取一次浮点，不做往返舍入
        return float(self.duration_seconds(sample_rate_hz) * 1000)

    def to_json(self) -> dict[str, int]:
        return {"start_sample": self.start_sample, "end_sample": self.end_sample}

    @staticmethod
    def clamp_to(range_: "SampleRange", sample_count: int) -> "SampleRange":
        """把区间截到 [0, sample_count) 内；越界部分丢弃而不是报错前先算错。"""
        start = max(0, min(range_.start_sample, sample_count))
        end = max(start, min(range_.end_sample, sample_count))
        if end <= start:
            raise TimebaseError(f"range empty after clamping to {sample_count} samples")
        return SampleRange(start, end)


@dataclass(frozen=True)
class Timebase:
    """一个解码主视图的标识与其采样率。"""

    id: str
    sample_rate_hz: int
    sample_count: int

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise TimebaseError(f"sample_rate_hz must be positive, got {self.sample_rate_hz}")
        if self.sample_count < 0:
            raise TimebaseError(f"sample_count must be >= 0, got {self.sample_count}")

    def validate_range(self, range_: SampleRange) -> SampleRange:
        """EXEC §3.4：0 <= start < end <= sample_count。"""
        if range_.end_sample > self.sample_count:
            raise TimebaseError(
                f"range end {range_.end_sample} exceeds sample_count {self.sample_count}"
            )
        return range_

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sample_rate_hz": self.sample_rate_hz,
            "sample_count": self.sample_count,
        }


def seconds_to_samples(seconds: float, sample_rate_hz: int, *, rounding: str = "nearest") -> int:
    """有理数映射 + 明确舍入规则（EXEC §3.4）。rounding: nearest | floor。"""
    if sample_rate_hz <= 0:
        raise TimebaseError("sample_rate_hz must be positive")
    exact = Fraction(seconds).limit_denominator(10**9) * sample_rate_hz
    if rounding == "nearest":
        # half-up，避免银行家舍入造成的 ±1 样本漂移
        return int((exact * 2 + 1) // 2) if exact >= 0 else -int((-exact * 2 + 1) // 2)
    if rounding == "floor":
        return int(exact // 1)
    raise TimebaseError(f"unknown rounding: {rounding}")
