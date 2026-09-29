"""A01（P0）：pYIN 显式真实采样率——16/24/44.1/48 kHz 同源信号，
频率 Hz 与时间轴必须同时不按 22050 错缩放（D04 / R02 / R03）。"""

import numpy as np
import pytest

from oido.dsp.f0 import F0Error, pyin_f0, robust_median_f0

SRS = [16000, 24000, 44100, 48000]


def _tone_burst(sr: int, *, f0_hz=440.0, total_s=2.0, burst_start_s=1.0) -> np.ndarray:
    """前半静音、后半 440 Hz：同时测频率正确性与时间轴定位。"""
    n = int(sr * total_s)
    t = np.arange(n) / sr
    sig = np.zeros(n)
    mask = t >= burst_start_s
    sig[mask] = 0.5 * np.sin(2 * np.pi * f0_hz * t[mask])
    return sig


class TestExplicitSrEnforced:
    def test_missing_sr_raises(self):
        # 不传 sr 直接抛错——R02 类缺陷（librosa 默认 22050）在类型层面被封死
        with pytest.raises(F0Error):
            pyin_f0(np.zeros(16000))                # sr 缺省 None → 拒绝
        with pytest.raises(F0Error):
            pyin_f0(np.zeros(16000), sr=0)          # 非法值
        with pytest.raises(F0Error):
            pyin_f0(np.zeros(16000), sr="24000")    # 字符串不是显式采样率


class TestA01FrequencyAndTimeAcrossSampleRates:
    @pytest.mark.parametrize("sr", SRS)
    def test_hz_and_time_both_correct(self, sr):
        result = pyin_f0(_tone_burst(sr), sr=sr)
        assert result["sr_hz"] == sr

        voiced_times = []
        voiced_f0 = []
        for i, (v, f0) in enumerate(zip(result["voiced"], result["f0_hz"])):
            if v and f0 is not None:
                voiced_times.append(i * result["hop_length"] / sr)   # center=True 帧中心
                voiced_f0.append(f0)
        assert voiced_f0, f"no voiced frames at {sr} Hz"

        # 频率：中位 F0 距 440 Hz ≤2%（若 sr 被 22050 误代，48k 会差 ~2.2×，必炸）
        med = float(np.median(voiced_f0))
        assert abs(med - 440.0) / 440.0 <= 0.02, f"sr={sr}: median F0 {med:.1f} Hz"

        # 时间轴：440 Hz 只在 1.0s 后出现；若时间被错缩放（48k 当 22.05k → 慢 2.18×），
        # 首个 voiced 帧会落在 ~0.46s 而不是 ~1s
        first_voiced = min(voiced_times)
        assert 0.85 <= first_voiced <= 1.15, f"sr={sr}: first voiced at {first_voiced:.3f}s"

    def test_if_sr_were_22050_it_would_fail(self):
        """防回归的反向证明：把 48 kHz 信号喂给错误的 sr=22050，必须测出异常。

        48k 信号按 22050 解释时时间轴被拉长 2.18×：真实在 1.0s 的 burst
        onset 会落到 ~2.18s；同时 Hz 标尺同比例失真（440 Hz 被报成 ~202 Hz）。
        """
        sig = _tone_burst(48000)
        bad = pyin_f0(sig, sr=22050)
        voiced = [
            (i * bad["hop_length"] / 22050, f)
            for i, (v, f) in enumerate(zip(bad["voiced"], bad["f0_hz"])) if v and f
        ]
        onset = min(t for t, _ in voiced)
        med = float(np.median([f for _, f in voiced]))
        # 真实 onset 1.0s：错 sr 下必然明显错位（拉开 >0.3s）
        assert abs(onset - 1.0) > 0.3, f"wrong-sr run unexpectedly well-placed: {onset:.3f}s"
        # Hz 标尺同错：440 Hz 不可能还在 2% 内
        assert abs(med - 440.0) / 440.0 > 0.02, f"wrong-sr run kept correct Hz: {med:.1f}"

    def test_silence_gives_no_fake_f0(self):
        # A03 精神：全静音不得伪造 F0
        result = pyin_f0(np.zeros(24000), sr=24000)
        assert robust_median_f0(result) is None
        assert not any(result["voiced"])

    def test_metadata_carries_sr_and_versions(self):
        result = pyin_f0(np.zeros(16000), sr=16000)
        assert result["method"] == "librosa.pyin"
        assert result["librosa_version"]
        assert result["center"] is True and result["hop_length"] == 256
