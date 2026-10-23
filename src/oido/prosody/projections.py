"""模型投影：预算内、分页、快照稳定（联合施工 05 返回分层 + 08 预算）。

投影≠内部事实：F0 全曲线/原数组不入投影；转写不在 overview 重复（宿主已有）。
字节预算按 UTF-8 紧凑 JSON 实测，超限抛错=CI 失败，不许静默截断。
cursor 绑定 revision+offset；跨快照（revision 变化）必须拒绝，不串原文。
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import Any, Optional

_CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "projections.oido.v1.schema.json"
_BUDGET_PATH = Path(__file__).resolve().parents[3] / "contracts" / "json_budgets.json"
PAGE_SIZE = 32          # 逐字页上限（施工包 05：每页 ≤32 音节）
PITCH_POINTS_MAX = 5    # 每音节 ≤5 代表性 F0 点


class ProjectionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _budgets() -> dict[str, int]:
    return json.loads(_BUDGET_PATH.read_text())


def compact_bytes(obj: Any) -> bytes:
    """紧凑 UTF-8 JSON（合法转义；NaN/Infinity 由 allow_nan=False 直接拒绝）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _assert_budget(name: str, obj: Any) -> bytes:
    payload = compact_bytes(obj)
    budget = _budgets()[name]
    if len(payload) > budget:
        raise ProjectionError(
            "BUDGET_EXCEEDED",
            f"{name} projection {len(payload)}B > budget {budget}B",
        )
    return payload


def _schema_validate(def_name: str, obj: Any) -> None:
    import jsonschema

    schema = json.loads(_CONTRACT_PATH.read_text())
    sub = dict(schema)
    sub["$defs"] = schema["$defs"]
    sub["$ref"] = f"#/$defs/{def_name}"
    validator = jsonschema.Draft202012Validator(sub)
    errors = list(validator.iter_errors(obj))
    if errors:
        raise ProjectionError("SCHEMA_INVALID", f"{def_name}: " + "; ".join(e.message for e in errors[:3]))


# ── cursor ─────────────────────────────────────────────────────────────
def encode_cursor(*, revision: int, offset: int, analysis_id: Optional[str] = None) -> str:
    data = {"r": revision, "o": offset}
    if analysis_id is not None:
        data["a"] = analysis_id
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, expect_revision: Optional[int] = None, analysis_id: Optional[str] = None) -> int:
    """返回 offset。revision 不匹配→SNAPSHOT_CHANGED（cursor 绑定快照）。"""
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + pad)
        data = json.loads(raw)
        offset = data["o"]
        if type(offset) is not int or offset < 0:
            raise ValueError
    except Exception as exc:
        raise ProjectionError("INVALID_CURSOR", "cursor is not a valid opaque token") from exc
    if expect_revision is not None and data.get("r") != expect_revision:
        raise ProjectionError("SNAPSHOT_CHANGED", "analysis revision changed; restart pagination")
    if analysis_id is not None and data.get("a") not in (None, analysis_id):
        raise ProjectionError("SNAPSHOT_CHANGED", "cursor belongs to another analysis; restart pagination")
    return offset


# ── 各投影构造（budget 校验后返回 dict，供 HTTP/MCP 单份送出） ─────────
def analysis_ticket(analysis_id: str, state: str) -> dict:
    ticket = {"analysis_id": analysis_id, "state": state}
    _schema_validate("analysis_ticket", ticket)
    _assert_budget("analysis_ticket", ticket)
    return ticket


def error_body(code: str, message: str, *, retryable: Optional[bool] = None) -> dict:
    body: dict[str, Any] = {"code": code, "message": message[:120]}
    if retryable is not None:
        body["retryable"] = retryable
    _schema_validate("error", body)
    _assert_budget("error", body)
    return body


def evidence_overview(*, analysis_id: str, revision: int, facts: dict) -> dict:
    """默认证据概览：2-8 条有定位声学观察 + 限制。转写不重复。"""
    sr = facts["sr"]
    ms = lambda s: int(round(s * 1000.0))
    full_range = facts.get("range_ms") or [0, max(1, ms(facts.get("duration_s", 0.0)))]
    observations: list[dict] = []

    def rng(start_s: float, end_s: float) -> list[int]:
        return [max(0, ms(start_s)), max(1, ms(end_s))]

    for pause in sorted(facts.get("pauses", []), key=lambda p: -p["duration_ms"])[:4]:
        observations.append({
            "kind": "pause", "range_ms": rng(pause["start_s"], pause["end_s"]),
            "value": round(pause["duration_ms"], 1), "unit": "ms", "quality": "measured",
        })
    units = facts.get("units", [])
    if units:
        longest = max(units, key=lambda u: u["end_s"] - u["start_s"])
        observations.append({
            "kind": "duration", "range_ms": rng(longest["start_s"], longest["end_s"]),
            "value": round((longest["end_s"] - longest["start_s"]) * 1000.0, 1),
            "unit": "ms",
            "quality": "uncertain" if facts.get("alignment_source") else "measured",
        })
        rate_val = None
        if facts.get("voiced_total_s", 0) > 0:
            rate_val = round(len(units) / facts["voiced_total_s"], 3)
        obs: dict[str, Any] = {
            "kind": "rate", "range_ms": full_range, "value": rate_val,
            "unit": "syllables_per_s",
            "quality": "uncertain" if facts.get("alignment_source") else "measured",
        }
        if rate_val is None:
            obs["quality"] = "unavailable"
            obs["reason"] = "unvoiced"
        observations.append(obs)
    pitch_st = facts.get("unit_pitch_st") or []
    voiced_st = [p for pts in pitch_st for p in (pts or [])]
    span = facts.get("pitch_span_st")
    if span is None and voiced_st:  # legacy facts retain their original measurements
        span = max(voiced_st) - min(voiced_st)
    if span is not None:
        observations.append({
            "kind": "pitch", "range_ms": full_range,
            "value": round(span, 2), "unit": "semitone", "quality": "uncertain",
        })
    else:
        observations.append({
            "kind": "pitch", "range_ms": full_range,
            "value": None, "unit": "semitone", "quality": "unavailable", "reason": "unvoiced",
        })
    observations = observations[:8]

    overview: dict[str, Any] = {
        "analysis_id": analysis_id,
        "revision": revision,
        "state": "ready" if not facts.get("limits") else "partial",
        "range_ms": full_range,
        "observations": observations,
    }
    limits = sorted(set(facts.get("limits", [])))[:8]
    if limits:
        overview["limits"] = limits
    if units and len(units) > 0:
        overview["next_cursor"] = encode_cursor(revision=revision, offset=0, analysis_id=analysis_id)
    _schema_validate("evidence_overview", overview)
    _assert_budget("evidence_overview", overview)
    return overview


def prosody_page(*, analysis_id: str, revision: int, facts: dict, cursor: Optional[str] = None) -> dict:
    """逐字谱分页。无对齐（units 空）由调用方走 error 路径，本函数不造假行。"""
    units = facts.get("units", [])
    pitch_st = facts.get("unit_pitch_st") or []
    if not units:
        raise ProjectionError("NO_CHAR_ALIGNMENT", "no aligned syllables; overview limits explain why")
    offset = decode_cursor(cursor, expect_revision=revision, analysis_id=analysis_id) if cursor else 0
    if offset >= len(units):
        raise ProjectionError("INVALID_CURSOR", "cursor beyond end of syllable list")
    sr = facts["sr"]
    ms = lambda s: int(round(s * 1000.0))
    window = units[offset: offset + PAGE_SIZE]
    syllables: list[dict] = []
    for i, unit in enumerate(window):
        if len(unit["text"]) > 6:
            raise ProjectionError("UNSUPPORTED_ALIGNMENT_UNIT", "aligned unit exceeds six characters; use the source transcript")
        idx = offset + i
        pts = pitch_st[idx] if idx < len(pitch_st) else None
        if pts:
            pts = [round(p, 2) for p in pts[:PITCH_POINTS_MAX]]
            syll: dict[str, Any] = {
                "text": unit["text"], "range_ms": [ms(unit["start_s"]), ms(unit["end_s"])],
                "pitch_st": pts,
                "quality": "uncertain" if facts.get("alignment_source") else "measured",
            }
        else:
            syll = {
                "text": unit["text"], "range_ms": [ms(unit["start_s"]), ms(unit["end_s"])],
                "pitch_st": None, "quality": "unavailable", "reason": "unvoiced",
            }
        if unit.get("pinyin"):
            syll["pinyin"] = unit["pinyin"][:40]
        if unit.get("lexical_tone") in (1, 2, 3, 4, 5):
            syll["lexical_tone"] = unit["lexical_tone"]
        syllables.append(syll)

    page: dict[str, Any] = {
        "analysis_id": analysis_id,
        "revision": revision,
        "range_ms": [ms(window[0]["start_s"]), ms(window[-1]["end_s"])],
        "pitch_reference": "utterance_median",
        "syllables": syllables,
    }
    limits = sorted(set(facts.get("limits", [])))[:8]
    if limits:
        page["limits"] = limits
    next_offset = offset + PAGE_SIZE
    if next_offset < len(units):
        page["next_cursor"] = encode_cursor(revision=revision, offset=next_offset, analysis_id=analysis_id)
    _schema_validate("prosody_page", page)
    _assert_budget("prosody_page", page)
    return page
