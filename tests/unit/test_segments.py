"""有声/静音分段与停顿测试（05 韵律主链 4/8、7/8）。合成 f0/rms 帧序列，无真实音频。"""

import numpy as np
import pytest

from oido.dsp.segments import SegmentsError, segment_utterance

HOP = 0.01
VOICED_F0 = 220.0
UNVOICED_F0 = np.nan
VOICED_DB = -20.0
UNVOICED_DB = -80.0


def _frames(mask: list[bool]):
    """mask[i]=True → 有声帧（f0=220Hz 且 rms=-20dB）；否则 f0=NaN、rms=-80dB。"""
    f0 = np.array([VOICED_F0 if m else UNVOICED_F0 for m in mask], dtype=np.float64)
    rms = np.array([VOICED_DB if m else UNVOICED_DB for m in mask], dtype=np.float64)
    return f0, rms


def _mask_for(voiced_ranges: list[tuple[float, float]], total_s: float) -> list[bool]:
    n = int(round(total_s / HOP))
    t = np.arange(n) * HOP
    mask = np.zeros(n, dtype=bool)
    for lo, hi in voiced_ranges:
        mask |= (t >= lo) & (t < hi)
    return mask.tolist()


class TestSegmentUtterance:
    def test_two_blocks_gap_bridged_and_one_pause(self):
        # voiced 0–0.25 与 0.30–0.5（中间 0.05s 缺口被 gap_bridge_s=0.08 桥接），
        # 静音 0.5–0.8（0.3s ≥ 0.2s → 1 个 pause），voiced 0.8–1.2
        mask = _mask_for([(0.0, 0.25), (0.30, 0.5), (0.8, 1.2)], total_s=1.2)
        f0, rms = _frames(mask)
        seg = segment_utterance(
            f0_hz=f0, f0_hop_s=HOP, rms_db=rms, rms_hop_s=HOP,
        )
        assert len(seg.voiced) == 2, "0.05s 缺口必须被桥接（否则会是 3 段）"
        assert seg.voiced[0].start_s == pytest.approx(0.0)
        assert seg.voiced[0].end_s == pytest.approx(0.5)
        assert seg.voiced[1].start_s == pytest.approx(0.8)
        assert seg.voiced[1].end_s == pytest.approx(1.2)
        # 段内原始有声占比：第一段 45/50 帧 = 0.9（桥接帧不冒充有声）
        assert seg.voiced[0].voiced_ratio == pytest.approx(0.9)
        assert seg.voiced[1].voiced_ratio == pytest.approx(1.0)

        assert len(seg.pauses) == 1
        assert seg.pauses[0].start_s == pytest.approx(0.5)
        assert seg.pauses[0].end_s == pytest.approx(0.8)
        assert seg.pauses[0].duration_ms == pytest.approx(300.0)

        assert seg.leading_silence_s == pytest.approx(0.0)
        assert seg.trailing_silence_s == pytest.approx(0.0)
        assert seg.voiced_total_s == pytest.approx(0.9)
        assert seg.speech_time_s == seg.voiced_total_s   # 05 第 7 条：口径显式区分
        assert seg.total_time_s == pytest.approx(1.2)

    def test_all_silence_gives_no_segments_no_pauses(self):
        mask = _mask_for([], total_s=1.0)
        f0, rms = _frames(mask)
        seg = segment_utterance(f0_hz=f0, f0_hop_s=HOP, rms_db=rms, rms_hop_s=HOP)
        assert seg.voiced == []
        assert seg.pauses == []          # 首尾静音不冒充 pause
        assert seg.leading_silence_s == pytest.approx(1.0)
        assert seg.trailing_silence_s == 0.0
        assert seg.voiced_total_s == 0.0 and seg.speech_time_s == 0.0
        assert seg.total_time_s == pytest.approx(1.0)

    def test_pause_exactly_at_threshold_counts(self):
        # voiced 0–0.3、静音 0.3–0.5（恰 0.20s = pause_min_s）、voiced 0.5–0.8
        mask = _mask_for([(0.0, 0.3), (0.5, 0.8)], total_s=0.8)
        f0, rms = _frames(mask)
        seg = segment_utterance(f0_hz=f0, f0_hop_s=HOP, rms_db=rms, rms_hop_s=HOP)
        assert len(seg.voiced) == 2
        assert len(seg.pauses) == 1      # 恰好等于阈值必须算（不是严格大于）
        assert seg.pauses[0].duration_ms == pytest.approx(200.0)

    def test_short_gap_below_bridge_is_neither_bridge_nor_pause(self):
        # 缝 0.05s、gap_bridge_s=0.03（不桥接）且 0.05s < pause_min_s（不算 pause）：
        # 段间隙保留为静音，既不并入 voiced_total 也不报 pause
        mask = _mask_for([(0.0, 0.25), (0.30, 0.55)], total_s=0.55)
        f0, rms = _frames(mask)
        seg = segment_utterance(
            f0_hz=f0, f0_hop_s=HOP, rms_db=rms, rms_hop_s=HOP, gap_bridge_s=0.03,
        )
        assert len(seg.voiced) == 2
        assert seg.pauses == []
        assert seg.voiced_total_s == pytest.approx(0.25 + 0.25)
        assert seg.total_time_s == pytest.approx(0.55)

    def test_rms_only_voicing_when_f0_missing(self):
        # f0 全 NaN（如基音提取失败），rms 高于地板仍应判有声（判定是"或"）
        rms = np.array([VOICED_DB] * 30 + [UNVOICED_DB] * 30)
        f0 = np.full(60, np.nan)
        seg = segment_utterance(f0_hz=f0, f0_hop_s=HOP, rms_db=rms, rms_hop_s=HOP)
        assert len(seg.voiced) == 1
        assert seg.voiced[0].start_s == pytest.approx(0.0)
        assert seg.voiced[0].end_s == pytest.approx(0.30)

    def test_mixed_hop_streams(self):
        # f0 hop 0.02 / rms hop 0.01：并集网格下仍应得到正确边界
        n = 60
        rms = np.array([VOICED_DB] * 40 + [UNVOICED_DB] * 20)   # 0–0.4 有声
        f0 = np.array([VOICED_F0] * 20 + [np.nan] * 10)         # 0–0.4（hop 0.02）
        seg = segment_utterance(f0_hz=f0, f0_hop_s=0.02, rms_db=rms, rms_hop_s=0.01)
        assert len(seg.voiced) == 1
        assert seg.voiced[0].start_s == pytest.approx(0.0)
        assert seg.voiced[0].end_s == pytest.approx(0.4)
        assert seg.leading_silence_s == pytest.approx(0.0)
        assert seg.trailing_silence_s == pytest.approx(0.2)

    def test_explicit_validation(self):
        with pytest.raises(SegmentsError):
            segment_utterance(f0_hz=np.array([]), f0_hop_s=HOP,
                              rms_db=np.array([-30.0]), rms_hop_s=HOP)
        with pytest.raises(SegmentsError):
            segment_utterance(f0_hz=np.array([220.0]), f0_hop_s=0,
                              rms_db=np.array([-30.0]), rms_hop_s=HOP)
        with pytest.raises(SegmentsError):
            segment_utterance(f0_hz=np.array([[220.0]]), f0_hop_s=HOP,
                              rms_db=np.array([-30.0]), rms_hop_s=HOP)
        with pytest.raises(SegmentsError):
            segment_utterance(f0_hz=np.array([220.0]), f0_hop_s=HOP,
                              rms_db=np.array([-30.0]), rms_hop_s=HOP, pause_min_s=-1)
