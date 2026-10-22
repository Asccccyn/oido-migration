"""音节-字符对齐测试（05 韵律主链 2/8：不满足条件必须 None，不许硬凑）。"""

import pytest

from oido.dsp.segments import VoicedSegment
from oido.prosody.align import (
    ALIGN_SOURCE,
    LOW_CONFIDENCE,
    align_chars_to_segments,
    strip_to_syllable_chars,
)

SEGS = [
    VoicedSegment(start_s=0.0, end_s=0.5, voiced_ratio=1.0),
    VoicedSegment(start_s=0.8, end_s=1.2, voiced_ratio=1.0),
]


class TestAlignCharsToSegments:
    def test_matching_count_gives_monotonic_spans_containing_centers(self):
        chars = ["你", "好", "吗", "呢"]
        centers = [0.1, 0.3, 0.9, 1.1]
        spans = align_chars_to_segments(chars=chars, syllable_centers_s=centers, voiced=SEGS)
        assert spans is not None and len(spans) == 4
        for i, s in enumerate(spans):
            assert s.index == i and s.char == chars[i]
            assert s.confidence == LOW_CONFIDENCE   # 低置信常量，不是对齐器置信度
        # 边界单调
        starts = [s.start_s for s in spans]
        ends = [s.end_s for s in spans]
        assert starts == sorted(starts) and ends == sorted(ends)
        assert all(s.start_s < s.end_s for s in spans)
        # 每个音节中心落在自己的 span 内
        for c, s in zip(centers, spans):
            assert s.start_s <= c < s.end_s
        # 同段内相邻中心取中点；跨段（0.5–0.8 的 pause）以段边界为界，不停顿分摊
        assert spans[0].start_s == pytest.approx(0.0)          # 段首
        assert spans[0].end_s == pytest.approx(0.2)            # (0.1+0.3)/2
        assert spans[1].end_s == pytest.approx(0.5)            # 段尾（下一中心在另一段）
        assert spans[2].start_s == pytest.approx(0.8)          # 段首（上一中心在另一段）
        assert spans[3].end_s == pytest.approx(1.2)            # 段尾

    def test_count_mismatch_returns_none(self):
        with_ = align_chars_to_segments(
            chars=["你", "好", "吗"], syllable_centers_s=[0.1, 0.3, 0.9, 1.1], voiced=SEGS,
        )
        assert with_ is None   # 数量差一 → unaligned，不硬凑

    def test_empty_inputs_return_none(self):
        assert align_chars_to_segments(chars=[], syllable_centers_s=[], voiced=SEGS) is None
        assert align_chars_to_segments(chars=["你"], syllable_centers_s=[], voiced=SEGS) is None
        assert align_chars_to_segments(chars=[], syllable_centers_s=[0.1], voiced=SEGS) is None

    def test_center_outside_voiced_returns_none(self):
        # 中心落在 pause（0.5–0.8）里 → 条件不满足 → None
        assert align_chars_to_segments(
            chars=["你", "好"], syllable_centers_s=[0.1, 0.6], voiced=SEGS,
        ) is None
        # 中心落在全部 voiced 段之外 → None
        assert align_chars_to_segments(
            chars=["你", "好"], syllable_centers_s=[0.1, 2.0], voiced=SEGS,
        ) is None

    def test_non_increasing_centers_return_none(self):
        assert align_chars_to_segments(
            chars=["你", "好"], syllable_centers_s=[0.3, 0.1], voiced=SEGS,
        ) is None
        assert align_chars_to_segments(
            chars=["你", "好"], syllable_centers_s=[0.2, 0.2], voiced=SEGS,
        ) is None

    def test_single_unit_covers_whole_segment(self):
        spans = align_chars_to_segments(
            chars=["嗯"], syllable_centers_s=[0.25], voiced=SEGS,
        )
        assert spans is not None
        assert spans[0].start_s == pytest.approx(0.0)
        assert spans[0].end_s == pytest.approx(0.5)

    def test_source_constant_marks_heuristic_provenance(self):
        assert ALIGN_SOURCE == "syllable_energy_dp"   # 启发式来源显式可追溯


class TestStripToSyllableChars:
    def test_mixed_han_and_latin(self):
        assert strip_to_syllable_chars("我today好") == ["我", "today", "好"]

    def test_latin_words_and_digits_are_units(self):
        assert strip_to_syllable_chars("mp3 files 好") == ["mp3", "files", "好"]
        assert strip_to_syllable_chars("第3次") == ["第", "3", "次"]

    def test_punctuation_and_whitespace_filtered(self):
        assert strip_to_syllable_chars("行啊？") == ["行", "啊"]
        assert strip_to_syllable_chars("你好，世界！……") == ["你", "好", "世", "界"]
        assert strip_to_syllable_chars("  ") == []
        assert strip_to_syllable_chars("") == []

    def test_han_only_string_is_per_char(self):
        assert strip_to_syllable_chars("没事") == ["没", "事"]
