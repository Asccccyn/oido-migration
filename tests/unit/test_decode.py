"""隔离解码子进程测试（05 基础补完）。全部合成信号，不联网、不读生产数据。

NOT_TESTED_real_container：mp4/webm/aac-opus 等真实容器嗅探未测——
本仓不携带真实媒体夹具（验收样本须用户选定/同意，见 05 验收节），
此处仅用 python wave 合成的 WAV 覆盖探测/解码/限额/超时路径。
"""

import io
import math
import wave

import numpy as np
import pytest

from oido.dsp.decode import (
    DECODE_PROFILE,
    DecodeError,
    decode_mono_pcm,
    probe,
)


def _make_wav(*, seconds: float = 1.0, sr: int = 44100, f0_hz: float = 440.0) -> bytes:
    """python wave 合成立体声 16-bit WAV（前半静音、后半正弦无必要，这里全程有声）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        t = np.arange(int(sr * seconds)) / sr
        tone = (0.5 * 32767 * np.sin(2 * np.pi * f0_hz * t)).astype(np.int16)
        w.writeframes(np.column_stack([tone, tone]).tobytes())
    return buf.getvalue()


GARBAGE = b"\x00\x01not audio"


class TestProbe:
    def test_wav_probe_reports_stream_facts(self):
        data = _make_wav(seconds=1.0)
        info = probe(data)
        assert info.sample_rate_hz == 44100
        assert info.channels == 2
        assert info.container == "wav"
        assert info.codec.startswith("pcm_s16")
        assert info.decode_profile == DECODE_PROFILE
        # 管道输入不可 seek：ffprobe 给不出 format.duration，记 NaN 而不是伪造
        assert math.isnan(info.duration_s) or info.duration_s > 0

    def test_garbage_bytes_rejected(self):
        with pytest.raises(DecodeError) as ei:
            probe(GARBAGE)
        assert ei.value.reason_code == "invalid_media"

    def test_empty_input_rejected(self):
        with pytest.raises(DecodeError) as ei:
            probe(b"")
        assert ei.value.reason_code == "invalid_media"

    def test_reason_code_must_come_from_registry(self):
        with pytest.raises(ValueError):
            DecodeError("made_up_code", "x")   # REASON_CODES 之外不许自造


class TestDecodeMonoPcm:
    def test_roundtrip_length_and_finiteness(self):
        data = _make_wav(seconds=1.0)
        result = decode_mono_pcm(data)
        # 1s 44.1k → 16k：长度 ≈16000±160；不测峰值频率（重采样链路不在此验证）
        assert 16000 - 160 <= result.samples.size <= 16000 + 160
        assert result.samples.dtype == np.float32
        assert np.isfinite(result.samples).all()
        assert float(np.abs(result.samples).max()) <= 1.0
        assert result.sample_rate_hz == 16000
        assert result.timebase_id == "tb_16k_mono"
        assert result.channels_source == 2
        assert result.decode_profile == DECODE_PROFILE
        # 真实时长来自解码样本数，不是探测段
        assert result.duration_s == pytest.approx(1.0, abs=0.02)
        tp = result.transform_params
        assert tp["source_sr"] == 44100 and tp["source_channels"] == 2
        assert tp["ffmpeg_version_hint"].startswith("9.")
        # 立体声相消风险必须随结果可见（05：不能平均相消后当"没说话"）
        assert "phase-cancellation not compensated" in tp["downmix_note"]

    def test_garbage_bytes_invalid_media(self):
        with pytest.raises(DecodeError) as ei:
            decode_mono_pcm(GARBAGE)
        assert ei.value.reason_code == "invalid_media"

    def test_input_size_limit(self):
        data = _make_wav(seconds=0.1)
        with pytest.raises(DecodeError) as ei:
            decode_mono_pcm(data, max_input_bytes=10)
        assert ei.value.reason_code == "resource_exhausted"

    def test_duration_limit(self):
        # probe 时长未知（NaN）→ 流式输出上限兜底：0.01s 配 1s WAV 必超限
        data = _make_wav(seconds=1.0)
        with pytest.raises(DecodeError) as ei:
            decode_mono_pcm(data, max_seconds=0.01)
        assert ei.value.reason_code == "resource_exhausted"

    def test_small_timeout_kills_process_group(self):
        # 本机（Apple Silicon）ffmpeg 9 spawn 地板 ~16ms、纯 WAV 解码 300s 也 <50ms，
        # 0.05s 会真解完；取 0.02s（低于 spawn 地板，5/5 稳定触发）验证墙钟超时杀进程组
        data = _make_wav(seconds=10.0)
        with pytest.raises(DecodeError) as ei:
            decode_mono_pcm(data, timeout_s=0.02)
        assert ei.value.reason_code == "deadline_exceeded"
