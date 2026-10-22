"""有声/静音分段与停顿（05 韵律主链 4/8 与 7/8：有声区间提取 + 停顿定义）。

对应施工包条目：
- 在有声区间提取 F0/voicing（本模块消费 f0/rms 两路帧序列，产 voiced 段）；
- 停顿与语速分别定义：pause 只算相邻 voiced 段之间 ≥ pause_min_s 的静音，
  utterance 首尾静音单独给 leading/trailing，不冒充停顿。

口径说明：
- f0 有效性 = 该帧值为有限正数（NaN 表示无 F0，不得当 0 Hz）；
- rms 有效性 = rms_db > rms_floor_db（无声≠0 dBFS，见 rms.py）；
- 两路帧率可不同：在"两路帧起点的并集"网格上逐点求或（OR），
  每个网格点的帧时长按其来源流的 hop 记；
- 短缝按 gap_bridge_s 桥接为一段（桥接进段的帧不计入 voiced_ratio，
  该比值反映段内原始有声占比，供上游判断证据强度）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


class SegmentsError(ValueError):
    pass


@dataclass(frozen=True)
class VoicedSegment:
    start_s: float
    end_s: float
    voiced_ratio: float


@dataclass(frozen=True)
class Pause:
    start_s: float
    end_s: float
    duration_ms: float


@dataclass(frozen=True)
class Segments:
    voiced: list[VoicedSegment] = field(default_factory=list)
    pauses: list[Pause] = field(default_factory=list)
    leading_silence_s: float = 0.0
    trailing_silence_s: float = 0.0
    voiced_total_s: float = 0.0
    speech_time_s: float = 0.0     # = voiced_total（05 第 7 条：发音秒与含停顿总语速不可混用）
    total_time_s: float = 0.0


# 浮点构造容差：pause 恰好等于 pause_min_s 时必须算 pause（半开/闭口径的显示余量）
_EPS_S = 1e-9


def _validate_stream(values: np.ndarray, hop_s: float, name: str) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise SegmentsError(f"{name} must be 1-D, got ndim={arr.ndim}")
    if arr.size == 0:
        raise SegmentsError(f"{name} is empty")
    if not np.isfinite(hop_s) or hop_s <= 0:
        raise SegmentsError(f"{name} hop_s must be positive, got {hop_s!r}")
    return arr


def _frame_voiced_at(t: float, starts: np.ndarray, hop_s: float, mask: np.ndarray) -> bool:
    """t 是否落在某个有声帧的 [start, start+hop) 覆盖内。"""
    idx = int(np.searchsorted(starts, t, side="right")) - 1
    if idx < 0 or idx >= starts.size:
        return False
    return bool(mask[idx]) and t < starts[idx] + hop_s


def segment_utterance(
    *,
    f0_hz: np.ndarray,
    f0_hop_s: float,
    rms_db: np.ndarray,
    rms_hop_s: float,
    pause_min_s: float = 0.20,
    gap_bridge_s: float = 0.08,
    rms_floor_db: float = -50.0,
) -> Segments:
    """由 f0/rms 帧序列产出 voiced 段、pause 与首尾静音。

    voiced 判定：f0 有效（有限正数）或 rms_db > rms_floor_db。
    全程无声 → voiced/pauses 均空，leading_silence_s = total_time_s。
    """
    f0 = _validate_stream(f0_hz, f0_hop_s, "f0_hz")
    rms = _validate_stream(rms_db, rms_hop_s, "rms_db")
    if not np.isfinite(pause_min_s) or pause_min_s < 0:
        raise SegmentsError(f"pause_min_s must be >= 0, got {pause_min_s!r}")
    if not np.isfinite(gap_bridge_s) or gap_bridge_s < 0:
        raise SegmentsError(f"gap_bridge_s must be >= 0, got {gap_bridge_s!r}")

    t_f0 = np.arange(f0.size, dtype=np.float64) * f0_hop_s
    t_rms = np.arange(rms.size, dtype=np.float64) * rms_hop_s

    # NaN/非正 → 无 F0；inf 同样无效（不得把缺失基频当 0 Hz 或 inf）
    f0_voiced = np.isfinite(f0) & (f0 > 0)
    rms_voiced = np.isfinite(rms) & (rms > rms_floor_db)

    # 并集网格：两路帧起点的并；每点帧时长按来源记（同点来自两路取 max）
    times = np.unique(np.concatenate([t_f0, t_rms]))
    from_f0 = np.isin(times, t_f0)
    from_rms = np.isin(times, t_rms)
    extents = np.where(
        from_f0 & from_rms,
        max(f0_hop_s, rms_hop_s),
        np.where(from_f0, f0_hop_s, rms_hop_s),
    )

    raw = np.array(
        [
            _frame_voiced_at(t, t_f0, f0_hop_s, f0_voiced)
            or _frame_voiced_at(t, t_rms, rms_hop_s, rms_voiced)
            for t in times
        ],
        dtype=bool,
    )

    # 桥接：相邻有声帧之间的缝（按前一帧的帧尾到下一帧的帧头计）<= gap_bridge_s → 并入
    bridged = raw.copy()
    voiced_idx = np.flatnonzero(raw)
    for a, b in zip(voiced_idx[:-1], voiced_idx[1:]):
        if b - a > 1:
            gap_s = float(times[b] - (times[a] + extents[a]))
            if gap_s <= gap_bridge_s:
                bridged[a + 1:b] = True

    # 切段
    flags = bridged.astype(np.int8)
    edges = np.diff(np.concatenate(([0], flags, [0])))
    starts_idx = np.flatnonzero(edges == 1)
    ends_idx = np.flatnonzero(edges == -1) - 1

    voiced_segments: list[VoicedSegment] = []
    for s, e in zip(starts_idx, ends_idx):
        start_s = float(times[s])
        end_s = float(times[e] + extents[e])
        ratio = float(raw[s:e + 1].mean())  # 桥接帧不冒充有声：ratio 用原始 mask
        voiced_segments.append(VoicedSegment(start_s=start_s, end_s=end_s, voiced_ratio=ratio))

    total_time_s = float(times[-1] + extents[-1])

    if not voiced_segments:
        # 全程无声：首静音即全部时长；不伪造段，也不把整段当 pause
        return Segments(
            voiced=[], pauses=[], leading_silence_s=total_time_s,
            trailing_silence_s=0.0, voiced_total_s=0.0, speech_time_s=0.0,
            total_time_s=total_time_s,
        )

    # pause：相邻 voiced 段之间的静音，且 ≥ pause_min_s（±浮点容差）
    pauses: list[Pause] = []
    for prev, nxt in zip(voiced_segments[:-1], voiced_segments[1:]):
        duration_s = nxt.start_s - prev.end_s
        if duration_s >= pause_min_s - _EPS_S:
            pauses.append(
                Pause(start_s=prev.end_s, end_s=nxt.start_s, duration_ms=duration_s * 1000.0)
            )

    voiced_total_s = float(sum(seg.end_s - seg.start_s for seg in voiced_segments))
    leading_silence_s = float(voiced_segments[0].start_s - times[0])
    trailing_silence_s = float(total_time_s - voiced_segments[-1].end_s)
    return Segments(
        voiced=voiced_segments,
        pauses=pauses,
        leading_silence_s=leading_silence_s,
        trailing_silence_s=trailing_silence_s,
        voiced_total_s=voiced_total_s,
        speech_time_s=voiced_total_s,
        total_time_s=total_time_s,
    )
