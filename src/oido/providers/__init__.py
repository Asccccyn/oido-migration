"""Provider 注册表与能力状态（ARCH §2.3：未装=not_configured，不是"不可用谎言"）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProviderStatus:
    name: str
    category: str          # asr | align | event | diarize | caption | embed | codec | voice_quality
    status: str            # available | not_configured | unsupported | blocked
    network: str           # local_only | none
    version: str | None = None
    note: str = ""


# v0.1 现状：只登记，不安装。capability 查询必须如实返回。
_REGISTRY: dict[str, ProviderStatus] = {
    "librosa-pyin": ProviderStatus("librosa-pyin", "dsp_f0", "available", "none", None, "DSP 库非推理 provider"),
    "whisper.cpp": ProviderStatus("whisper.cpp", "asr", "not_configured", "local_only"),
    "sensevoice": ProviderStatus("sensevoice", "asr+event", "not_configured", "local_only",
                                 note="若采用：仅取 ASR/事件字段，情绪 head 边界丢弃"),
    "panns": ProviderStatus("panns", "event", "not_configured", "local_only"),
    "efficientat": ProviderStatus("efficientat", "event", "not_configured", "local_only"),
    "silero-vad": ProviderStatus("silero-vad", "vad", "not_configured", "local_only"),
    "pyannote": ProviderStatus("pyannote", "diarize", "not_configured", "local_only",
                               note="遥测须关闭并实测网络行为"),
    "clap": ProviderStatus("clap", "embed", "not_configured", "local_only"),
    "dac": ProviderStatus("dac", "codec", "not_configured", "local_only"),
    "qwen3-omni-captioner": ProviderStatus("qwen3-omni-captioner", "caption", "not_configured", "local_only"),
    "opensmile": ProviderStatus("opensmile", "voice_quality", "not_configured", "local_only",
                                note="双许可；用途条款未定前不装"),
}


def capabilities() -> list[dict]:
    return [
        {
            "name": p.name,
            "category": p.category,
            "status": p.status,
            "network": p.network,
            "version": p.version,
            "note": p.note,
        }
        for p in _REGISTRY.values()
    ]
