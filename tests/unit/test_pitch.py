"""相对半音层与参照范围（ARCH §7.1 / EXEC §4.3）。"""

import math

import pytest

from oido.domain.pitch import (
    PitchReference, PitchReferenceError, semitones, utterance_median_f0,
)


class TestReference:
    def test_scopes_enforced(self):
        with pytest.raises(PitchReferenceError):
            PitchReference("global_440")                       # 不允许的参照
        with pytest.raises(PitchReferenceError):
            PitchReference("qiaosheng_baseline")               # 缺 version
        with pytest.raises(PitchReferenceError):
            PitchReference("utterance_median", version="v1")   # 句中位不带版本
        assert PitchReference("qiaosheng_baseline", "v3").label() == "v3"
        assert PitchReference("utterance_median").label() == "utterance_median"


class TestSemitones:
    def test_octave_is_12(self):
        ref = PitchReference("utterance_median")
        assert semitones(880.0, 440.0, ref) == pytest.approx(12.0, abs=1e-9)
        assert semitones(220.0, 440.0, ref) == pytest.approx(-12.0, abs=1e-9)

    def test_100_cents(self):
        ref = PitchReference("utterance_median")
        assert semitones(440.0 * 2 ** (1 / 1200 * 100), 440.0, ref) == pytest.approx(1.0, abs=1e-9)

    def test_invalid_f0_rejected(self):
        ref = PitchReference("utterance_median")
        for bad in (0.0, -100.0, float("nan"), float("inf")):
            with pytest.raises(PitchReferenceError):
                semitones(bad, 440.0, ref)
        with pytest.raises(PitchReferenceError):
            semitones(440.0, 0.0, ref)


class TestUtteranceMedian:
    def test_median_and_empty(self):
        assert utterance_median_f0([200.0, None, 300.0, 250.0]) == 250.0
        assert utterance_median_f0([None, 0.0, -1.0]) is None
        assert utterance_median_f0([]) is None

    def test_even_count(self):
        assert utterance_median_f0([200.0, 300.0]) == 250.0


class TestToneVsPhraseSeparation:
    """D04 边界：去声（4声）本身下降 ≈ -X 半音，不得解释为句级下沉。"""

    def test_falling_tone_is_syllable_contour(self):
        ref = PitchReference("utterance_median")
        # 一个 4 声音节起止（示例值，非真实测量）
        start = semitones(280.0, 260.0, ref)
        end = semitones(190.0, 260.0, ref)
        assert start > 0 and end < -3
        # 字内走势字段应为"下降"；这不构成任何 phrase_trend 结论
        assert start - end > 3.0
