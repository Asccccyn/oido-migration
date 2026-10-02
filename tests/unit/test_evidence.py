"""B20：情绪字段边界丢弃 + 证据合同校验 + Schema 正反样本。"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oido.domain.evidence import (
    EvidenceValidationError, check_no_json_nan_inf, make_rms_measurement,
    sanitize_provider_payload, validate_evidence,
)

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


class TestEmotionBoundary:
    """B20：provider 原始响应含 happy/sad/neutral → adapter 边界丢弃。"""

    def test_flat_payload(self):
        cleaned, dropped = sanitize_provider_payload(
            {"text": "因为叫我的人是你。", "emotion": "happy", "confidence": 0.9}
        )
        assert cleaned == {"text": "因为叫我的人是你。", "confidence": 0.9}
        assert dropped == ["emotion"]
        # 值 "happy" 不得出现在净化结果里（不落库/不进日志的等价检查）
        assert "happy" not in json.dumps(cleaned)

    def test_nested_payload(self):
        cleaned, dropped = sanitize_provider_payload(
            {"asr": "文本", "prosody": {"emotion": "sad", "score": 0.81},
             "events": [{"label": "laugh_like", "start_ms": 100}]}
        )
        assert cleaned["prosody"] == {"score": 0.81}
        assert dropped == ["prosody.emotion"]
        assert "sad" not in json.dumps(cleaned)

    def test_emotion_value_samples_never_leak(self):
        for value in ("sad", "happy", "angry", "neutral", "anxious", "warm"):
            cleaned, _ = sanitize_provider_payload({"emotion": value, "ok": 1})
            assert value not in json.dumps(cleaned)

    def test_audit_20260929_variants_blocked(self):
        """自审计勘误：这些变体曾全部穿透，必须全部拦下。"""
        cases = [
            {"valence": -0.7, "arousal": 0.4, "text": "x"},   # 情效维度
            {"happy": 0.9, "sad": 0.1},                        # 情绪词当键
            {"emotional": True, "emotions": {"sad": 0.8}},     # 前缀变形
            {"excitement": 0.7, "affective": 0.3},             # 近义/前缀
            {"label": "happy"},                                # 值是独立情绪词
        ]
        for payload in cases:
            cleaned, dropped = sanitize_provider_payload(payload)
            blob = json.dumps(cleaned)
            for word in ("valence", "arousal", "happy", "sad", "emotional",
                         "emotions", "excitement", "affective", "label"):
                if word in payload:
                    assert word not in blob, f"{payload} leaked {word!r}"
            assert dropped, f"{payload} should have drops"

    def test_long_text_values_not_matched(self):
        # 长文本值里的情绪词是正常内容，不得误杀（"happy birthday" 在转写里）
        cleaned, dropped = sanitize_provider_payload(
            {"text": "they said happy birthday to her", "event": "laugh_like"}
        )
        assert cleaned == {"text": "they said happy birthday to her", "event": "laugh_like"}
        assert dropped == []

    def test_emotion_feature_rejected_at_validation(self):
        with pytest.raises(EvidenceValidationError):
            validate_evidence({
                "kind": "measurement", "feature": "mood_score",
                "method": "x", "method_version": "1", "input_variant_id": "v",
                "timebase_id": "t", "range": {"start_sample": 0, "end_sample": 10},
                "quality_flags": [], "unit": "x", "value": 0.5,
            })


class TestValidation:
    def _base(self, **over):
        ev = {
            "schema_version": "oido.v1",
            "kind": "measurement", "feature": "rms_dbfs", "value": -9.03,
            "unit": "dBFS", "method": "rms_float_pcm_reference_1",
            "method_version": "1", "input_variant_id": "v1", "timebase_id": "tb",
            "range": {"start_sample": 0, "end_sample": 24000}, "quality_flags": [],
        }
        ev.update(over)
        return ev

    def test_ok(self):
        validate_evidence(self._base())

    def test_null_value_requires_below_floor(self):
        with pytest.raises(EvidenceValidationError):
            validate_evidence(self._base(value=None))
        ev = self._base(value=None, below_floor=True)
        validate_evidence(ev)

    def test_nan_rejected(self):
        with pytest.raises(EvidenceValidationError):
            validate_evidence(self._base(value=float("nan")))
        with pytest.raises(EvidenceValidationError):
            check_no_json_nan_inf({"nested": [float("inf")]})

    def test_model_detection_fields(self):
        with pytest.raises(EvidenceValidationError):
            validate_evidence(self._base(kind="model_detection"))
        validate_evidence(self._base(
            kind="model_detection", value=None, feature="laugh_like",
            score_kind="raw_sigmoid", raw_score=0.7, model_version="panns-cnn14",
        ))


class TestSchemaFixtures:
    """EXEC §3.5 的合同示例必须能过 Schema；反例必须失败。"""

    def _validator(self):
        schema = json.loads((CONTRACTS / "evidence.v1.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def test_exec_35_fixture_passes(self):
        fixture = {
            "schema_version": "oido.v1",
            "evidence_id": "ev_fixture_rms",
            "audio_id": "aud_fixture_sine",
            "analysis_id": "run_fixture_001",
            "revision": 1,
            "kind": "measurement",
            "feature": "rms_dbfs",
            "value": -9.0308998699,
            "unit": "dBFS",
            "method": "rms_float_pcm_reference_1",
            "method_version": "fixture-spec-1",
            "input_variant_id": "var_fixture_native",
            "timebase_id": "tb_fixture_24k",
            "range": {"start_sample": 0, "end_sample": 24000},
            "parameters": {"reference_amplitude": 1.0, "floor_dbfs": -120},
            "quality_flags": [],
            "fixture_only": True,
        }
        assert not list(self._validator().iter_errors(fixture))

    def test_emotion_kind_rejected(self):
        bad = {
            "schema_version": "oido.v1", "kind": "emotion_classification",
            "feature": "mood_score", "method": "x", "method_version": "1",
            "input_variant_id": "v", "timebase_id": "t",
            "range": {"start_sample": 0, "end_sample": 1}, "quality_flags": [],
        }
        assert list(self._validator().iter_errors(bad))

    def test_missing_range_rejected(self):
        bad = {
            "schema_version": "oido.v1", "kind": "measurement",
            "feature": "rms_dbfs", "unit": "dBFS", "method": "m",
            "method_version": "1", "input_variant_id": "v", "timebase_id": "t",
            "quality_flags": [],
        }
        assert list(self._validator().iter_errors(bad))


class TestRmsMeasurementFactory:
    def test_below_floor_shape(self):
        ev = make_rms_measurement(
            value=None, below_floor=True, range_={"start_sample": 0, "end_sample": 100},
            input_variant_id="v", timebase_id="t",
        )
        assert ev["value"] is None and ev["below_floor"] is True
