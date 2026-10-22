"""音节-字符对齐：能量-DP 低置信启发式（05 韵律主链 2/8：强制对齐失败不制造逐字精度）。

对应施工包条目：用对齐器得到词/字/音节边界；强制对齐失败不制造逐字精度，
降级句级/全段并附 unaligned。本模块不是强制对齐器（GMM/HMM 那类）——
ALIGN_SOURCE="syllable_energy_dp" 标明来源是能量音节中心的启发式切分：

- 前提条件（字符数 == 音节中心数、所有中心落在 voiced 段内、中心严格递增）
  任一不满足 → 返回 None，由上游记 unaligned/alignment_failed，绝不硬凑；
- 满足时边界取相邻音节中心的中点；中心分属不同 voiced 段（中间隔 pause）
  时以段边界为界，不把停顿时间分摊给字；
- confidence 固定 LOW_CONFIDENCE=0.4：启发式来源，不是对齐器输出的置信度。
"""

from __future__ import annotations

from dataclasses import dataclass

from oido.dsp.segments import VoicedSegment

ALIGN_SOURCE = "syllable_energy_dp"
LOW_CONFIDENCE = 0.4


@dataclass(frozen=True)
class CharSpan:
    index: int
    char: str
    start_s: float
    end_s: float
    confidence: float


def _is_han(ch: str) -> bool:
    return "\u3400" <= ch <= "\u4dbf" or "\u4e00" <= ch <= "\u9fff"


def _is_ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def strip_to_syllable_chars(text: str) -> list[str]:
    """输出对齐单元口径：汉字逐字 + 连续 ASCII 字母/数字串为一个单元。

    "我today好" → ["我", "today", "好"]；标点/空白/全角符号一律过滤；
    空串或纯标点 → []。lexicon.char_lexical_info 复用同一口径。
    """
    units: list[str] = []
    run: list[str] = []
    for ch in text:
        if _is_ascii_alnum(ch):
            run.append(ch)
            continue
        if run:
            units.append("".join(run))
            run = []
        if _is_han(ch):
            units.append(ch)
    if run:
        units.append("".join(run))
    return units


def _containing_index(center_s: float, voiced: list[VoicedSegment]) -> int | None:
    for j, seg in enumerate(voiced):
        if seg.start_s <= center_s < seg.end_s:   # 半开区间（timebase 口径）
            return j
    return None


def align_chars_to_segments(
    *,
    chars: list[str],
    syllable_centers_s: list[float],
    voiced: list[VoicedSegment],
) -> list[CharSpan] | None:
    """把字符单元铺到能量音节中心上。

    条件不满足（数量不齐、中心越出 voiced 段、中心非严格递增、空输入）
    一律返回 None——上游记 unaligned，不许硬凑精度。
    """
    if not chars or not syllable_centers_s:
        return None
    if len(chars) != len(syllable_centers_s):
        return None
    centers = [float(c) for c in syllable_centers_s]
    if any(b <= a for a, b in zip(centers, centers[1:])):
        return None
    seg_idx = [_containing_index(c, voiced) for c in centers]
    if any(j is None for j in seg_idx):
        return None

    spans: list[CharSpan] = []
    n = len(centers)
    for i, (ch, center, j) in enumerate(zip(chars, centers, seg_idx)):
        seg = voiced[j]
        if i == 0:
            start = seg.start_s
        elif seg_idx[i - 1] == j:
            start = (centers[i - 1] + center) / 2.0
        else:
            start = seg.start_s      # 跨段：不把停顿分摊给字，从段首起算
        if i == n - 1:
            end = seg.end_s
        elif seg_idx[i + 1] == j:
            end = (center + centers[i + 1]) / 2.0
        else:
            end = seg.end_s          # 跨段：字界止于段尾
        spans.append(CharSpan(index=i, char=ch, start_s=start, end_s=end,
                              confidence=LOW_CONFIDENCE))
    return spans
