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


# 文本承载键：值里的单词是"人说的话/写的描述"，不做情绪词匹配
# （审计补充点7：{"text":"happy"} 是合法转写，不得误删）
_TEXT_BEARING_KEYS = {
    "text", "transcript", "caption", "description", "script", "content", "title", "note",
}


def is_emotion_key(key: str) -> bool:
    token = key.lower().split("_")[0]
    return token.startswith(_FORBIDDEN_KEY_STEMS) or token in _EMOTION_WORD_KEYS


def sanitize_provider_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """provider 边界净化：剥离情绪分类字段。

    覆盖：键名命中（含前缀变形）、情绪词当键、独立情绪词值、**任意深度的
    dict 与 list 容器**（审计 F04a：parameters.events=[...] 也要递归）。
    文本承载键（text/transcript/...）的值不做匹配，避免误删合法原文。
    返回 (净化后的浅拷贝, 被丢弃的字段名列表)；丢弃只记名不记值。
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
        elif isinstance(value, list):
            cleaned[key] = _sanitize_list(value, dropped, key)
        elif (
            isinstance(value, str)
            and key.lower() not in _TEXT_BEARING_KEYS
            and value.strip().lower() in _EMOTION_VALUE_SAMPLES
        ):
            dropped.append(key)          # 值本身是独立情绪词 → 整字段丢弃
        else:
            cleaned[key] = value
    return cleaned, dropped


def _sanitize_list(items: list, dropped: list[str], path: str) -> list:
    out: list[Any] = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            sub, sub_dropped = sanitize_provider_payload(item)
            out.append(sub)
            dropped.extend(f"{path}[{i}].{k}" for k in sub_dropped)
        elif isinstance(item, list):
            # R02：list 套 list 也要递归——任何深度的容器都不给情绪分类留缝
            out.append(_sanitize_list(item, dropped, f"{path}[{i}]"))
        elif isinstance(item, str) and item.strip().lower() in _EMOTION_VALUE_SAMPLES:
            dropped.append(f"{path}[{i}]")
        else:
            out.append(item)
    return out


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


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def validate_evidence(ev: dict[str, Any], *, known_features: Iterable[str] = ()) -> None:
    """完整合同校验（R03：类型/版本/结构，归属与边界由 service 层用真实 DB 值再验）。"""
    for field in ("schema_version", "kind", "feature", "method", "method_version",
                  "input_variant_id", "timebase_id", "range", "quality_flags"):
        if field not in ev:
            raise EvidenceValidationError(f"evidence missing required field: {field}")
    if ev["schema_version"] != "oido.v1":
        raise EvidenceValidationError(f"schema_version must be 'oido.v1', got {ev['schema_version']!r}")
    if ev["kind"] not in EVIDENCE_KINDS:
        raise EvidenceValidationError(f"illegal kind: {ev['kind']!r}")
    if not _is_nonempty_str(ev["feature"]):
        raise EvidenceValidationError("feature must be a non-empty string")
    if is_emotion_key(str(ev.get("feature", ""))):
        raise EvidenceValidationError(f"emotion-classified feature forbidden: {ev['feature']!r}")
    for field in ("method", "method_version", "input_variant_id", "timebase_id"):
        if not _is_nonempty_str(ev[field]):
            raise EvidenceValidationError(f"{field} must be a non-empty string, got {ev[field]!r}")
    if not isinstance(ev["quality_flags"], list) or not all(
        isinstance(f, str) for f in ev["quality_flags"]
    ):
        raise EvidenceValidationError(f"quality_flags must be a list of strings, got {ev['quality_flags']!r}")
    if "below_floor" in ev and not isinstance(ev["below_floor"], bool):
        raise EvidenceValidationError("below_floor must be a boolean")
    if "value" in ev:
        value = ev["value"]
        if value is not None and not _is_number(value):
            raise EvidenceValidationError(f"value must be a finite number or null, got {value!r}")

    rng = ev["range"]
    if not (isinstance(rng, dict) and "start_sample" in rng and "end_sample" in rng):
        raise EvidenceValidationError("range must carry start_sample/end_sample")
    start, end = rng["start_sample"], rng["end_sample"]
    # 审计 F06：样本区间必须是整数、非负、起点<终点（bool 是 int 子类，一并排除）
    for v in (start, end):
        if isinstance(v, bool) or not isinstance(v, int):
            raise EvidenceValidationError(f"range bounds must be integers, got {v!r}")
    if start < 0 or end <= start:
        raise EvidenceValidationError(f"range must satisfy 0 <= start < end, got [{start}, {end})")

    if ev["kind"] == "measurement":
        if not _is_nonempty_str(ev.get("unit")):
            raise EvidenceValidationError(f"measurement requires a non-empty string unit, got {ev.get('unit')!r}")
        if ev.get("value") is None and not ev.get("below_floor"):
            raise EvidenceValidationError("null value requires below_floor=true")
    if ev["kind"] == "model_detection":
        for field in ("score_kind", "raw_score", "model_version"):
            if field not in ev:
                raise EvidenceValidationError(f"model_detection requires {field}")
        if not _is_nonempty_str(ev["score_kind"]):
            raise EvidenceValidationError("score_kind must be a non-empty string")
        if not _is_number(ev["raw_score"]):
            raise EvidenceValidationError(f"raw_score must be a finite number, got {ev['raw_score']!r}")
        if not _is_nonempty_str(ev["model_version"]):
            raise EvidenceValidationError("model_version must be a non-empty string")
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
