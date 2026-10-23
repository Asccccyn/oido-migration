"""oido_find 落地：在指定音频的已对齐转写里按文字找区间（默认 3、最大 10）。

诚实路径：只做文本匹配（精确子串；mode=fuzzy 允许等长 1 字差异），
区间来自 prosody facts 的低置信音节对齐——**对齐不可用就明确失败**，
不退化为全段猜测，也不做 embedding 语义检索（那是 est worker 的领地）。
"""

from __future__ import annotations

from typing import Optional

from oido.service.analyses import AnalysisError
from oido.workers.analysis_worker import load_facts

DEFAULT_LIMIT = 3
MAX_LIMIT = 10
_PUNCT = set("，。！？；：、""''（）,.!?;:()\u3000 ")


def _strip(text: str) -> str:
    return "".join(c for c in text if c not in _PUNCT)


def _fuzzy_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    diff = sum(1 for x, y in zip(a, b) if x != y)
    return diff <= 1 and len(a) >= 2


class FindService:
    def __init__(self, analyses, ingest):
        self.analyses = analyses
        self.ingest = ingest

    def find(self, *, principal: str, audio_ref: str, query: str, mode: str = "substring",
             limit: int = DEFAULT_LIMIT) -> dict:
        if not query.strip():
            raise AnalysisError("invalid_input", "query must be non-empty", 422)
        if limit < 1 or limit > MAX_LIMIT:
            raise AnalysisError("invalid_input", f"limit must be 1..{MAX_LIMIT}", 422)
        if mode not in ("substring", "fuzzy"):
            raise AnalysisError("invalid_input", "mode must be substring|fuzzy", 422)
        audio_id = self.ingest.resolve(audio_ref, principal=principal)
        row = self.analyses.conn.execute(
            """SELECT analysis_id, status, revision FROM analysis_runs
               WHERE audio_id = ? AND requested_by = ? AND status IN ('succeeded','partial')
               ORDER BY updated_at DESC LIMIT 1""",
            (audio_id, principal),
        ).fetchone()
        if row is None:
            raise AnalysisError("not_found", "no completed analysis for this audio; run oido_analyze first", 409)
        facts = load_facts(self.analyses.conn, row["analysis_id"])
        if facts is None or not facts.get("units"):
            raise AnalysisError(
                "dependency_failed", "no aligned transcript spans; find requires char alignment", 409)
        units = facts["units"]
        q = _strip(query)
        if not q:
            raise AnalysisError("invalid_input", "query has no matchable characters", 422)
        # 拼接单元序列（与转写顺序一致），建立单元索引
        seq = "".join(_strip(u["text"]) for u in units)
        index_of_char: list[int] = []
        for i, u in enumerate(units):
            index_of_char.extend([i] * len(_strip(u["text"])))
        if len(index_of_char) != len(seq):
            raise AnalysisError("dependency_failed", "alignment units malformed", 500)

        hits: list[dict] = []
        n, m = len(seq), len(q)
        i = 0
        while i <= n - m:
            window = seq[i:i + m]
            matched = window == q if mode == "substring" else _fuzzy_eq(window, q)
            if matched:
                u0, u1 = index_of_char[i], index_of_char[i + m - 1]
                hits.append({
                    "analysis_id": row["analysis_id"],
                    "range_ms": [round(units[u0]["start_s"] * 1000), round(units[u1]["end_s"] * 1000)],
                    "matched_units": m,
                    "match": window,
                })
                i += max(1, m)  # 不重叠推进
            else:
                i += 1
        hits = hits[:limit]
        return {
            "audio_ref": audio_ref,
            "analysis_id": row["analysis_id"],
            "query": query,
            "mode": mode,
            "state": "candidates" if hits else "no_match",
            "items": hits,
            "limits": ["unaligned"] if facts.get("alignment_source") else [],
        }
