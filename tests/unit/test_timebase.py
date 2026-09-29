"""A04 类：时间基准数学（EXEC §6.1 A01–A04 的纯数学部分）。"""

import pytest

from oido.domain.timebase import (
    SampleRange, Timebase, TimebaseError, seconds_to_samples,
)


class TestSampleRange:
    def test_half_open_invariants(self):
        with pytest.raises(TimebaseError):
            SampleRange(5, 5)          # 空区间
        with pytest.raises(TimebaseError):
            SampleRange(-1, 10)        # 负起点
        with pytest.raises(TimebaseError):
            SampleRange(10, 5)         # 倒置

    def test_duration_exact_rational(self):
        # 24000 样本 @ 24 kHz = 1 s；320 样本 @ 16 kHz = 20 ms —— 无浮点往返
        assert SampleRange(0, 24000).duration_seconds(24000) == 1
        assert SampleRange(0, 320).duration_ms(16000) == pytest.approx(20.0, abs=1e-12)

    def test_duration_rejects_bad_sr(self):
        with pytest.raises(TimebaseError):
            SampleRange(0, 100).duration_seconds(0)

    def test_clamp(self):
        tb = Timebase("tb", 16000, 16000)
        r = SampleRange(8000, 99999)
        clamped = SampleRange.clamp_to(r, tb.sample_count)
        assert (clamped.start_sample, clamped.end_sample) == (8000, 16000)
        tb.validate_range(clamped)


class TestTimebase:
    def test_range_must_fit(self):
        tb = Timebase("tb", 44100, 44100)
        tb.validate_range(SampleRange(0, 44100))
        with pytest.raises(TimebaseError):
            tb.validate_range(SampleRange(0, 44101))

    def test_bad_sr(self):
        with pytest.raises(TimebaseError):
            Timebase("tb", 0, 10)


class TestSecondsToSamples:
    @pytest.mark.parametrize(
        "seconds,sr,rounding,expected",
        [
            (1.0, 16000, "nearest", 16000),
            (0.5, 44100, "nearest", 22050),
            (0.001, 48000, "nearest", 48),
            # 44.1k 下的 1/3 秒 = 14700 整（有理数精确，不漂）
            (1 / 3, 44100, "nearest", 14700),
            # half-up：0.5 样本向上，不出现银行家舍入的来回漂移
            (0.5 / 48000, 48000, "nearest", 1),
            (0.5 / 48000, 48000, "floor", 0),
        ],
    )
    def test_mapping(self, seconds, sr, rounding, expected):
        assert seconds_to_samples(seconds, sr, rounding=rounding) == expected

    def test_roundtrip_no_drift(self):
        # A10 精神：映射-反映射不累计漂移（有理数路径）
        for i in range(0, 48000, 7):
            n = seconds_to_samples(i / 48000, 48000)
            assert n == i
