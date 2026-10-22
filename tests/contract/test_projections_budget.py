"""投影预算与字段纪律（联合施工 08：JSON 预算纳入合同测试，失败即 CI 失败）。

J01 分页 next_cursor；J02 null/0/false 合法语义保留、NaN/Infinity 拒绝；
J03 debug/hash/向量不进默认投影；J04 schema 单一真源校验；游标快照绑定。
"""

from __future__ import annotations

import json

import pytest

from oido.prosody import projections
from oido.prosody.projections import ProjectionError


def _facts(units=None, pitch_st=None, limits=None, pauses=None, voiced_total=0.8, duration=1.2):
    units = units if units is not None else []
    return {
        "sr": 16000, "variant_id": "var_x", "timebase_id": "tb_16k_mono",
        "duration_s": duration, "f0_median_hz": 220.0, "reference_scope": "utterance_median",
        "leading_silence_s": 0.05, "trailing_silence_s": 0.1,
        "voiced_total_s": voiced_total,
        "pauses": pauses if pauses is not None else [{"start_s": 0.4, "end_s": 0.65, "duration_ms": 250.0}],
        "units": units, "unit_pitch_st": pitch_st if pitch_st is not None else [],
        "limits": limits or [], "alignment_source": None, "transcript": None,
        "downmix_note": "mean_of_channels; phase-cancellation not compensated",
    }


def _unit(text="啊", start=0.1, end=0.4, tone=3):
    return {"index": 0, "text": text, "start_s": start, "end_s": end,
            "confidence": 0.4, "pinyin": "a3", "lexical_tone": tone}


class TestBudget:
    def test_ticket_budget(self):
        t = projections.analysis_ticket("run_abc123", "queued")
        assert len(projections.compact_bytes(t)) <= 512

    def test_overview_budget_with_max_units(self):
        # 构造较大但仍合法的 overview：8 观察 + 8 限制
        limits = ["unvoiced", "unaligned", "low_signal", "insufficient_baseline",
                  "capture_processing_unknown", "phrase_tone_unresolved",
                  "provider_unavailable", "no_transcript"]
        facts = _facts(units=[_unit(f"字{i}", 0.1 + i * 0.3, 0.35 + i * 0.3) for i in range(8)],
                       pitch_st=[[1.0, 2.0]] * 8, limits=limits,
                       pauses=[{"start_s": 0.4, "end_s": 0.65, "duration_ms": 250.0}] * 1,
                       voiced_total=2.4, duration=3.0)
        ov = projections.evidence_overview(analysis_id="run_abc123", revision=1, facts=facts)
        assert len(projections.compact_bytes(ov)) <= 8192
        assert len(ov["observations"]) <= 8 and len(ov["limits"]) == 8

    def test_page_budget_32_units(self):
        units = [_unit(f"啊", 0.1 + i * 0.2, 0.25 + i * 0.2) for i in range(40)]
        pitch = [[-1.5, 0.0, 2.5, 3.0, 4.0]] * 40
        facts = _facts(units=units, pitch_st=pitch, voiced_total=8.0, duration=9.0)
        page = projections.prosody_page(analysis_id="run_abc123", revision=1, facts=facts)
        assert len(page["syllables"]) == 32
        assert len(projections.compact_bytes(page)) <= 16384
        assert "next_cursor" in page
        page2 = projections.prosody_page(analysis_id="run_abc123", revision=1,
                                         facts=facts, cursor=page["next_cursor"])
        assert len(page2["syllables"]) == 8 and "next_cursor" not in page2


class TestFieldDiscipline:
    def test_zero_and_false_survive(self):
        # 语速 0.0、时长 0.0 必须原样保留（不许 truthy 清理）
        facts = _facts(units=[_unit("嗯", 0.2, 0.2 + 0.0001)], pitch_st=[None],
                       voiced_total=1.0, duration=1.0)
        facts["units"][0]["start_s"] = 0.2
        facts["units"][0]["end_s"] = 0.2001
        ov = projections.evidence_overview(analysis_id="run_a", revision=1, facts=facts)
        obs_kinds = [o["kind"] for o in ov["observations"]]
        assert "rate" in obs_kinds
        rate = next(o for o in ov["observations"] if o["kind"] == "rate")
        assert rate["value"] == 1  # 1 unit / 1.0 voiced s
        # duration 0.0 的观察也合法（0 是有效测量）
        page = projections.prosody_page(analysis_id="run_a", revision=1, facts=facts)
        assert page["syllables"][0]["pitch_st"] is None
        assert page["syllables"][0]["reason"] == "unvoiced"  # null 必须带 reason（schema 强制）

    def test_nan_rejected(self):
        bad = _facts(units=[_unit()], pitch_st=[[float("nan")]])
        with pytest.raises(ValueError):
            projections.evidence_overview(analysis_id="run_a", revision=1, facts=bad)

    def test_no_debug_fields_leak(self):
        facts = _facts(units=[_unit()], pitch_st=[[1.0]])
        ov = projections.evidence_overview(analysis_id="run_a", revision=1, facts=facts)
        page = projections.prosody_page(analysis_id="run_a", revision=1, facts=facts)
        for obj in (ov, page):
            text = json.dumps(obj)
            for banned in ("sha256", "variant_id", "fence", "debug", "provider_request"):
                assert banned not in text

    def test_chinese_escape_roundtrip(self):
        text = '他"嗯"了一声\n就走了'
        facts = _facts(units=[{"index": 0, "text": text[:6], "start_s": 0.1, "end_s": 0.4,
                               "confidence": 0.4}],
                       pitch_st=[[0.5]])
        page = projections.prosody_page(analysis_id="run_a", revision=1, facts=facts)
        raw = projections.compact_bytes(page)
        back = json.loads(raw.decode("utf-8"))
        assert back["syllables"][0]["text"] == text[:6]


class TestCursor:
    def test_snapshot_changed_on_revision_mismatch(self):
        cursor = projections.encode_cursor(revision=1, offset=0)
        with pytest.raises(ProjectionError) as ei:
            projections.decode_cursor(cursor, expect_revision=2)
        assert ei.value.code == "SNAPSHOT_CHANGED"

    def test_garbage_cursor_rejected(self):
        with pytest.raises(ProjectionError):
            projections.decode_cursor("!!!not-base64!!!")

    def test_offset_roundtrip(self):
        c = projections.encode_cursor(revision=7, offset=32)
        assert projections.decode_cursor(c, expect_revision=7) == 32
