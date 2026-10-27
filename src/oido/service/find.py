"""oido_find 落地：在指定音频的已对齐转写里按文字找区间（默认 3、最大 10）。

诚实路径：只做文本匹配（精确子串；mode=fuzzy 允许等长 1 字差异），
区间来自 prosody facts 的低置信音节对齐——**对齐不可用就明确失败**，
不退化为全段猜测，也不做 embedding 语义检索（那是 est worker 的领地）。
"""

from __future__ import annotations

import base64
import hashlib
import json as _json
from typing import Optional

from oido.prosody.projections import _assert_budget
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
             limit: int = DEFAULT_LIMIT, cursor: Optional[str] = None) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise AnalysisError("invalid_input", "query must be 1..200 characters", 422)
        if limit < 1 or limit > MAX_LIMIT:
            raise AnalysisError("invalid_input", f"limit must be 1..{MAX_LIMIT}", 422)
        if mode not in ("substring", "fuzzy"):
            raise AnalysisError("invalid_input", "mode must be substring|fuzzy", 422)
        audio_id = self.ingest.resolve(audio_ref, principal=principal)
        # JV-07（联合审计 2026-10-26）：find 无 range 语义=查全文——只选**无裁剪范围**
        # 的完成分析。此前"最新完成"可能是 inspect_range 的局部分析，导致看过一段后
        # 全文 find 失效（单元只覆盖局部）。局部证据请显式用带 range 的分析结果。
        row = self.analyses.conn.execute(
            """SELECT analysis_id, status, revision FROM analysis_runs
               WHERE audio_id = ? AND requested_by = ? AND status IN ('succeeded','partial')
                 AND json_extract(request_json, '$.range') IS NULL
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
        skip = 0
        skip_total = 0  # R6′：本页之前已交付的命中总数（游标续页基数）
        truncated = False  # RE-14a：命中数达 limit 中断（末页无更多命中时不发假游标）
        if cursor:
            try:
                # RE-14a（2026-10-24 返修）：服务端签发的 cursor 可直接消费——补齐
                # base64 padding 缺失（上轮反例：自签游标第二页 422 "not readable"）
                padded = cursor + "=" * (-len(cursor) % 4)
                decoded = _json.loads(base64.urlsafe_b64decode(padded))
            except Exception:
                raise AnalysisError("invalid_cursor", "cursor is not readable", 422)
            if (decoded.get("a") != row["analysis_id"] or decoded.get("r") != row["revision"]
                    or decoded.get("q") != hashlib.sha256(q.encode()).hexdigest()[:16]
                    or not isinstance(decoded.get("skip"), int) or decoded["skip"] < 0):
                raise AnalysisError("cursor_stale", "cursor does not match this analysis/query", 409)
            skip = decoded["skip"]
            skip_total = decoded["skip"]
        while i <= n - m:
            window = seq[i:i + m]
            matched = window == q if mode == "substring" else _fuzzy_eq(window, q)
            if matched:
                u0, u1 = index_of_char[i], index_of_char[i + m - 1]
                if skip > 0:
                    skip -= 1  # R6′：游标跳过前页已交付命中（不重叠推进语义不变）
                    i += max(1, m)
                    continue
                hits.append({
                    "range_ms": [round(units[u0]["start_s"] * 1000), round(units[u1]["end_s"] * 1000)],
                    "matched_units": m,
                    "match": window,
                })
                if len(hits) >= limit:
                    truncated = True
                    break
                i += max(1, m)  # 不重叠推进
            else:
                i += 1
        result = {
            "audio_ref": audio_ref,
            "analysis_id": row["analysis_id"],
            "mode": mode,
            "state": "candidates" if hits else "no_match",
            "items": hits,
            "limits": ["unaligned"] if facts.get("alignment_source") else [],
        }
        # R6′（2026-10-23）：命中截断时给 next_cursor（命中流偏移，绑定 analysis+revision+
        # query hash——换分析/换查询=游标失效如实报）；无截断不带（读尽即止）。
        # RE-14a（2026-10-24 返修）：截断≠必有续页——向前扫描确认仍有命中才签游标
        #（上轮反例：恰好在末条命中处达 limit 也发了假 next_cursor）
        _more = False
        if truncated:
            _j = i + max(1, m)  # 越过刚交付的末条命中再向前扫描（i 停在其起点）
            while _j <= n - m:
                _w = seq[_j:_j + m]
                if _w == q if mode == "substring" else _fuzzy_eq(_w, q):
                    _more = True
                    break
                _j += 1
        if _more:
            result["next_cursor"] = base64.urlsafe_b64encode(_json.dumps({
                "a": row["analysis_id"], "r": row["revision"],
                "q": hashlib.sha256(q.encode()).hexdigest()[:16], "skip": skip_total + len(hits),
            }).encode()).decode()
        _assert_budget("media_candidates", result)
        return result
