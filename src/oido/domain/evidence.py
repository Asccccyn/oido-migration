"""标准证据合同与情绪字段边界（OIDO-EXEC §3.5 / D04 / B20）。

三类 kind：measurement / model_detection / model_description。
emotion / affect / mood 不是 oído v1 的合法证据：provider 返回即弃，
不落库、不进 API/MCP/日志。允许的是带时间的可听事件。
"""

from __future__ import annotations

import math
from typing import Any, Iterable

EVIDENCE_KINDS = ("measurement", "model_detection", "model_description")

# 允许的可听事件候选（D04）：描述"发生了什么声音"，不推断心理状态
ALLOWED_SOUND_EVENTS = (
    "laugh_like",
    "sigh_like",
    "sob_like",
    "breath",
    "cough",
    "meow",
    "purr",
    "rain_like",
    "doorbell_like",
    "music_like",
    "speech",
    "silence",
    "other_audible",
)

# 禁用词根：首个 token 以任一词根开头即视为情绪分类，边界处丢弃（B20 测试覆盖）
# 2026-09-29 自审计加固：valence/arousal（情效维度）也是情绪推断；
# 前缀匹配堵住 emotional/emotions/affective 等变形。
_FORBIDDEN_KEY_STEMS = (
    "emotion", "mood", "affect", "sentiment", "feeling",
    "valence", "arousal",
)
# 情绪词直接当键名（provider 常见 {"happy": 0.9} 形态）
_EMOTION_WORD_KEYS = {
    "happy", "sad", "angry", "neutral", "anxious", "calm", "excited",
    "excitement", "surprised", "surprise", "fearful", "fear", "disgusted",
    "disgust", "joy", "joyful", "cheerful", "upset", "stressed", "annoyed",
    "teasing", "sleepy", "warm",
}
# 已知 provider 情绪取值（含 Siren SIREN_EMOTIONS 的交集），仅用于日志计数，绝不落值
_EMOTION_VALUE_SAMPLES = {
    "sad", "happy", "angry", "neutral", "anxious", "calm", "excited",
    "surprised", "fearful", "disgusted", "warm", "teasing", "sleepy",
}


class EvidenceValidationError(ValueError):
    pass


def is_emotion_key(key: str) -> bool:
    token = key.lower().split("_")[0]
    return token.startswith(_FORBIDDEN_KEY_STEMS) or token in _EMOTION_WORD_KEYS


def sanitize_provider_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """provider 边界净化：剥离情绪分类字段（键名命中 / 情绪词当键 / 独立情绪词值）。

    返回 (净化后的浅拷贝, 被丢弃的字段名列表)。丢弃只计数/记名，
    永不保存被丢弃的值（B20：DB/API/MCP/log 均不得出现该字段值）。
    长文本值不做匹配（转写正文里的"happy birthday"不受影响）。
    """
    cleaned: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in payload.items():
        if is_emotion_key(key):
            dropped.append(key)
            continue
        if isinstance(value, dict):
            sub, sub_dropped = sanitize_provider_payload(value)
            cleaned[key] = sub
            dropped.extend(f"{key}.{k}" for k in sub_dropped)
        elif isinstance(value, str) and value.strip().lower() in _EMOTION_VALUE_SAMPLES:
            dropped.append(key)          # 值本身是独立情绪词 → 整字段丢弃
        else:
            cleaned[key] = value
    return cleaned, dropped


def check_no_json_nan_inf(value: Any) -> None:
    """JSON 不允许 NaN/Infinity（EXEC §3.5）。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise EvidenceValidationError(f"non-finite float in evidence: {value}")
    if isinstance(value, dict):
        for v in value.values():
            check_no_json_nan_inf(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            check_no_json_nan_inf(v)


def validate_evidence(ev: dict[str, Any], *, known_features: Iterable[str] = ()) -> None:
    """最小合同校验：kind/方法版本/区间/单位/质量标记齐备。"""
    for field in ("kind", "method", "method_version", "input_variant_id", "timebase_id", "range", "quality_flags"):
        if field not in ev:
            raise EvidenceValidationError(f"evidence missing required field: {field}")
    if ev["kind"] not in EVIDENCE_KINDS:
        raise EvidenceValidationError(f"illegal kind: {ev['kind']!r}")
    if is_emotion_key(str(ev.get("feature", ""))):
        raise EvidenceValidationError(f"emotion-classified feature forbidden: {ev['feature']!r}")
    rng = ev["range"]
    if not (isinstance(rng, dict) and "start_sample" in rng and "end_sample" in rng):
        raise EvidenceValidationError("range must carry start_sample/end_sample")
    if ev["kind"] == "measurement":
        if "unit" not in ev:
            raise EvidenceValidationError("measurement requires a unit")
        value = ev.get("value")
        if value is None and not ev.get("below_floor"):
            raise EvidenceValidationError("null value requires below_floor=true")
    if ev["kind"] == "model_detection":
        for field in ("score_kind", "raw_score", "model_version"):
            if field not in ev:
                raise EvidenceValidationError(f"model_detection requires {field}")
    check_no_json_nan_inf(ev)


# 便捷：构造一条合规 RMS measurement（测试与 fixture 共用）
def make_rms_measurement(
    *, value: float | None, unit: str = "dBFS", range_: dict[str, int],
    input_variant_id: str, timebase_id: str, below_floor: bool = False,
    method: str = "rms_float_pcm_reference_1", method_version: str = "oido-1",
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ev = {
        "schema_version": "oido.v1",
        "evidence_id": None,  # 由存储层分配
        "kind": "measurement",
        "feature": "rms_dbfs",
        "value": None if below_floor else (None if value is None else float(value)),
        "unit": unit,
        "method": method,
        "method_version": method_version,
        "input_variant_id": input_variant_id,
        "timebase_id": timebase_id,
        "range": range_,
        "parameters": parameters or {},
        "quality_flags": [],
    }
    if below_floor:
        ev["below_floor"] = True
    validate_evidence(ev)
    return ev
