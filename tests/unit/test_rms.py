"""A02/A03 + EXEC §3.5 fixture 值：RMS/dBFS 测量正确性。"""

import numpy as np
import pytest

from oido.dsp.rms import rms_dbfs


def _sine(freq_hz: float, sr: int, seconds: float, amplitude: float) -> np.ndarray:
    # 整周期截断，避免频谱泄漏引入的微小误差（EXEC §3.5"整周期、无量化误差"口径）
    n = int(round(sr * seconds))
    cycles = round(freq_hz * n / sr)
    t = np.arange(n) / sr
    return amplitude * np.sin(2 * np.pi * (cycles * sr / n) * t)


class TestRms:
    def test_exec_35_fixture_value(self):
        # 幅度 0.5 正弦 → RMS=0.5/√2 → -9.0308998699 dBFS（文档口径，非真实语音）
        sig = _sine(440, 24000, 2.0, 0.5)
        out = rms_dbfs(sig)
        assert out["value"] == pytest.approx(-9.0308998699, abs=0.01)
        assert out["below_floor"] is False

    def test_a02_halving_is_minus_6_0206_db(self):
        loud = rms_dbfs(_sine(440, 16000, 2.0, 0.8))
        quiet = rms_dbfs(_sine(440, 16000, 2.0, 0.4))
        delta = quiet["value"] - loud["value"]
        assert delta == pytest.approx(20 * np.log10(0.5), abs=0.01)   # ≈ -6.0206 dB
        assert delta == pytest.approx(-6.0206, abs=0.01)

    def test_a03_zeros_below_floor_not_fake_zero_dbfs(self):
        out = rms_dbfs(np.zeros(24000))
        # 无声：value=None + below_floor；绝不能用 0 dBFS 表示无声
        assert out["value"] is None
        assert out["below_floor"] is True

    def test_nan_input_rejected(self):
        bad = np.zeros(1000)
        bad[500] = np.nan
        with pytest.raises(ValueError):
            rms_dbfs(bad)

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            rms_dbfs(np.array([]))

    def test_reference_amplitude_parameterized(self):
        sig = _sine(440, 48000, 1.0, 0.5)
        a = rms_dbfs(sig, reference_amplitude=1.0)
        b = rms_dbfs(sig, reference_amplitude=0.5)
        assert b["value"] - a["value"] == pytest.approx(6.0206, abs=0.01)
