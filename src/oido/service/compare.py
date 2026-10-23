"""oido_compare 落地：两条已分析音频的可比较量差异（无情绪结论）。

联合施工 05：无个人基线时明确拒绝"比平时"表述（baseline_comparison=
unavailable + insufficient_baseline）；跨设备/采集处理不可比时带限制。
只比较**已存在分析**的测量值（side_effects: none——不触发新分析）。
"""

from __future__ import annotations

from typing import Optional

from oido.prosody.projections import _assert_budget
from oido.service.analyses import AnalysisError
from oido.workers.analysis_worker import load_facts

# 可比较量（全部为已批准的非身份统计；无绝对音量）
_METRICS = ("duration_s", "voiced_total_s", "pauses_count", "pauses_total_s",
            "f0_median_hz", "speech_rate_syl_per_s")


class CompareService:
    def __init__(self, analyses, ingest):
        self.analyses = analyses
        self.ingest = ingest

    def _side(self, principal: str, audio_ref: str, range_ms: Optional[list[int]]) -> dict:
        audio_id = self.ingest.resolve(audio_ref, principal=principal)
        row = self.analyses.conn.execute(
            """SELECT analysis_id, status, revision FROM analysis_runs
               WHERE audio_id = ? AND requested_by = ? AND status IN ('succeeded','partial')
               ORDER BY updated_at DESC LIMIT 1""",
            (audio_id, principal),
        ).fetchone()
        if row is None:
            raise AnalysisError("not_found", f"no completed analysis for {audio_ref}; run oido_analyze first", 409)
        facts = load_facts(self.analyses.conn, row["analysis_id"])
        if facts is None:
            raise AnalysisError("dependency_failed", f"analysis {row['analysis_id']} has no facts", 500)
        pauses = facts.get("pauses") or []
        units = facts.get("units") or []
        m: dict[str, Optional[float]] = {
            "duration_s": round(facts.get("duration_s", 0.0), 3),
            "voiced_total_s": round(facts.get("voiced_total_s", 0.0), 3),
            "pauses_count": float(len(pauses)),
            "pauses_total_s": round(sum(p["duration_ms"] for p in pauses) / 1000.0, 3),
            "f0_median_hz": facts.get("f0_median_hz"),
            "speech_rate_syl_per_s": (
                round(len(units) / facts["voiced_total_s"], 3)
                if units and facts.get("voiced_total_s", 0) > 0 else None
            ),
        }
        if range_ms is not None:
            # RE-14c（2026-10-24 返修）：嵌套 range 类型先于域校验（上轮 ["x","y"] 在
            # 比较 0 时抛 TypeError→500；非法输入一律 422）
            if (not isinstance(range_ms, list) or len(range_ms) != 2
                    or any(type(v) is not int for v in range_ms)):
                raise AnalysisError("invalid_input", "range_ms must be [start,end) integer ms", 422)
            if range_ms[0] < 0 or range_ms[1] <= range_ms[0]:
                raise AnalysisError("invalid_input", "range_ms must be [start,end) ms", 422)
            a, b = range_ms[0] / 1000.0, range_ms[1] / 1000.0
            source_range = facts.get("range_ms") or [0, round(facts["duration_s"] * 1000)]
            if not source_range[0] <= range_ms[0] < range_ms[1] <= source_range[1]:
                raise AnalysisError("invalid_input", "range exceeds analyzed source interval", 422)
            pauses = [p for p in pauses if p["start_s"] >= a and p["end_s"] <= b]
            units = [u for u in units if u["start_s"] >= a and u["end_s"] <= b]
            voiced = sum(u["end_s"] - u["start_s"] for u in units)
            m.update({
                "duration_s": round(b - a, 3),
                "voiced_total_s": round(voiced, 3) if units else None,
                # The stored median is for the full analysis, not this crop.
                "f0_median_hz": None,
                "pauses_count": float(len(pauses)),
                "pauses_total_s": round(sum(p["duration_ms"] for p in pauses) / 1000.0, 3),
                "speech_rate_syl_per_s": round(len(units) / voiced, 3) if units and voiced > 0 else None,
            })
        return {"audio_ref": audio_ref, "analysis_id": row["analysis_id"],
                "revision": row["revision"], "metrics": m,
                "alignment_source": facts.get("alignment_source"),
                "range_ms": range_ms,
                "downmix_note": facts.get("downmix_note")}

    def compare(self, *, principal: str, left: dict, right: dict, metrics: Optional[list[str]] = None) -> dict:
        for bad in (metrics or []):
            if bad not in _METRICS:
                raise AnalysisError("invalid_input", f"unknown metric {bad!r}; allowed: {list(_METRICS)}", 422)
        want = metrics or list(_METRICS)
        ls = self._side(principal, left.get("audio_ref", ""), left.get("range_ms"))
        rs = self._side(principal, right.get("audio_ref", ""), right.get("range_ms"))
        # Equal decoder notes do not establish equal microphones/AGC/routes.
        limits: list[str] = ["capture_processing_unknown"]
        d_left = ls["metrics"]["duration_s"] or 0.0
        d_right = rs["metrics"]["duration_s"] or 0.0
        if d_left > 0 and d_right > 0 and (max(d_left, d_right) / max(1e-6, min(d_left, d_right))) > 3:
            limits.append("low_signal")  # 时长悬殊：语速/停顿密度可比性下降（如实标注）
        differences: list[dict] = []
        for k in want:
            a, b = ls["metrics"].get(k), rs["metrics"].get(k)
            entry: dict = {"metric": k, "left": a, "right": b}
            if a is None or b is None:
                entry["quality"] = "unavailable"
                entry["reason"] = "low_signal" if ls["range_ms"] or rs["range_ms"] else "unvoiced" if k == "f0_median_hz" else "no_transcript" if "rate" in k else "low_signal"
            else:
                entry["diff"] = round(b - a, 4)
                if a != 0:
                    entry["ratio"] = round(b / a, 3)
                entry["quality"] = ("uncertain" if k in ("voiced_total_s", "speech_rate_syl_per_s")
                                    and (ls["alignment_source"] or rs["alignment_source"]) else "measured")
            differences.append(entry)
        result = {
            "left": {k: ls[k] for k in ("audio_ref", "analysis_id", "revision")},
            "right": {k: rs[k] for k in ("audio_ref", "analysis_id", "revision")},
            "metrics": differences,
            "limits": limits,
            # 无批准个人基线：明确拒绝"比平时"表述（05），不做任何心理比较
            "baseline_comparison": {"state": "unavailable", "reason": "insufficient_baseline"},
        }
        # R6′（2026-10-23）：结果预算门——此前 compare 无预算键（新键=收紧不是放宽）
        _assert_budget("compare_result", result)
        return result
