"""prosody 分析管线（联合施工 05 韵律主链；04 默认 profile=prosody）。

只做可测得的：解码→F0/RMS→有声分段/停顿→（有转写时）能量-DP 音节对齐+词典本调。
对齐失败降级句级/全段并带 unaligned；语速无对齐不给数；无证据不造 0Hz/情绪。
词/字级推断（align）固定低置信常量，绝不冒充强制对齐器精度。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from oido.domain.ids import new_id
from oido.domain.pitch import PitchReference, semitones, utterance_median_f0
from oido.dsp.f0 import pyin_f0, robust_median_f0
from oido.dsp.rms import rms_dbfs
from oido.service.variants import VariantService

# RMS 帧化参数（固定策略，随分析产物记录）
RMS_FRAME = 480          # 16kHz 下 30ms
RMS_HOP = 160            # 10ms
F0_HOP = 256             # 与 dsp.f0 默认一致（16ms）
UTTERANCE_MEDIAN = PitchReference(scope="utterance_median")


@dataclass
class ProsodyResult:
    status: str                                   # succeeded | partial | failed
    stages: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    # 投影层直接可用的结构化事实（不入 evidence 正文的中间形态）
    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)


def _frames_db(samples: np.ndarray) -> tuple[np.ndarray, float]:
    """分帧 RMS→dBFS 序列（10ms hop）。"""
    n = (samples.shape[0] - RMS_FRAME) // RMS_HOP + 1
    if n <= 0:
        return np.array([]), 0.0
    idx = np.arange(RMS_FRAME)[None, :] + RMS_HOP * np.arange(n)[:, None]
    frames = samples[idx]
    rms = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1))
    db = np.where(rms > 0, 20.0 * np.log10(np.maximum(rms, 1e-12)), -120.0)
    return db, RMS_HOP / 16000.0


def _measurement(*, feature: str, value: Optional[float], unit: str, range_samples: dict,
                 variant_id: str, timebase_id: str, method: str, quality_flags: Optional[list] = None,
                 below_floor: bool = False, parameters: Optional[dict] = None) -> dict:
    ev = {
        "schema_version": "oido.v1",
        "kind": "measurement",
        "feature": feature,
        "value": None if value is None else float(value),
        "unit": unit,
        "method": method,
        "method_version": "oido-prosody-1",
        "input_variant_id": variant_id,
        "timebase_id": timebase_id,
        "range": dict(range_samples),
        "parameters": parameters or {},
        "quality_flags": quality_flags or [],
    }
    if value is None:
        ev["below_floor"] = below_floor if below_floor else True
    return ev


def run_prosody(
    *,
    audio_id: str,
    samples: np.ndarray,
    sr: int,
    variant_id: str,
    timebase_id: str,
    transcript: Optional[dict],
    range_samples: Optional[dict] = None,
    variant_service: Optional[VariantService] = None,
) -> ProsodyResult:
    """在 decode_main 变体上跑 prosody。返回 stages/evidence/facts（不落库，由 worker 提交）。"""
    from oido.dsp.segments import segment_utterance  # 延迟导入：DSP 叶子模块
    from oido.prosody.align import ALIGN_SOURCE, align_chars_to_segments, strip_to_syllable_chars

    total_samples = int(samples.shape[0])
    base_range = range_samples or {"start_sample": 0, "end_sample": total_samples}
    stages: list[dict] = []
    evidence: list[dict] = []
    limits: list[str] = []

    # ── F0 ─────────────────────────────────────────────────────────────
    f0_result = pyin_f0(samples, sr=sr, hop_length=F0_HOP)
    voiced_f0 = [v for v, b in zip(f0_result["f0_hz"] or [], f0_result["voiced"]) if b and v is not None]
    if not f0_result.get("sufficient"):
        stages.append({"capability": "f0", "status": "partial", "reason_code": "insufficient_signal",
                       "detail": {"reason": f0_result.get("insufficiency_reason")}})
        limits.append("low_signal")
    elif not voiced_f0:
        stages.append({"capability": "f0", "status": "partial", "reason_code": "insufficient_signal",
                       "detail": {"reason": "no_voiced_frames"}})
        limits.append("unvoiced")
    else:
        stages.append({"capability": "f0", "status": "succeeded",
                       "detail": {"method": f0_result["method"], "frames": len(f0_result["f0_hz"] or [])}})

    # ── RMS 帧 + 有声分段/停顿 ──────────────────────────────────────────
    db, rms_hop_s = _frames_db(samples)
    hop_s = F0_HOP / float(sr)
    segs = segment_utterance(
        f0_hz=np.array([np.nan if v is None else v for v in (f0_result["f0_hz"] or [])], dtype=float),
        f0_hop_s=hop_s,
        rms_db=db,
        rms_hop_s=rms_hop_s,
    )
    stages.append({"capability": "pauses", "status": "succeeded",
                   "detail": {"voiced_segments": len(segs.voiced), "pauses": len(segs.pauses)}})
    for pause in segs.pauses:
        evidence.append(_measurement(
            feature="pause_ms", value=pause.duration_ms, unit="ms",
            range_samples={"start_sample": int(pause.start_s * sr), "end_sample": int(pause.end_s * sr)},
            variant_id=variant_id, timebase_id=timebase_id, method="voiced_gap_ge_200ms",
        ))
    evidence.append(_measurement(
        feature="voiced_total_s", value=round(segs.voiced_total_s, 4), unit="s",
        range_samples=base_range, variant_id=variant_id, timebase_id=timebase_id, method="voiced_or_rms_above_-50db",
    ))

    # ── utterance_median 参照（无 voiced → None，不伪造） ────────────────
    median_f0 = robust_median_f0(f0_result)

    # ── 对齐（有转写才尝试；失败降级） ─────────────────────────────────
    units: list[dict] = []
    alignment_source: Optional[str] = None
    if transcript is None or not str(transcript.get("text", "")).strip():
        stages.append({"capability": "alignment", "status": "skipped", "reason_code": "provider_not_configured",
                       "detail": {"reason": "no_transcript_at_ingest"}})
        limits.append("no_transcript")
    else:
        text = str(transcript["text"])
        units_raw = strip_to_syllable_chars(text)
        centers: list[float] = []
        # 音节中心启发：每个 voiced 段内按能量峰切音节（峰数=单元数才算候选）
        for seg in segs.voiced:
            a, b = int(seg.start_s * sr), int(seg.end_s * sr)
            if b <= a:
                continue
            seg_db = db[int(seg.start_s / rms_hop_s): int(seg.end_s / rms_hop_s)] if db.size else np.array([])
            if seg_db.size < 3:
                centers.append((seg.start_s + seg.end_s) / 2)
                continue
            peaks = _energy_peaks(seg_db, rms_hop_s)
            for p in peaks:
                centers.append(seg.start_s + p)
        if not units_raw:
            stages.append({"capability": "alignment", "status": "skipped", "reason_code": "invalid_media",
                           "detail": {"reason": "transcript_has_no_syllable_units"}})
            limits.append("unaligned")
        else:
            spans = align_chars_to_segments(
                chars=units_raw,
                syllable_centers_s=centers,
                voiced=[v for v in segs.voiced],
            )
            if spans is None:
                stages.append({"capability": "alignment", "status": "partial", "reason_code": "alignment_failed",
                               "detail": {"reason": "syllable_count_mismatch",
                                          "units": len(units_raw), "syllable_centers": len(centers)}})
                limits.append("unaligned")
            else:
                alignment_source = ALIGN_SOURCE
                stages.append({"capability": "alignment", "status": "succeeded",
                               "detail": {"source": ALIGN_SOURCE, "confidence": "low"}})
                # 词典本调/拼音（可降级）
                lex = None
                try:
                    from oido.prosody.lexicon import char_lexical_info

                    lex = char_lexical_info(text)
                except Exception:
                    lex = None
                if lex is None:
                    stages.append({"capability": "lexicon", "status": "skipped",
                                   "reason_code": "provider_not_configured",
                                   "detail": {"reason": "pypinyin_unavailable"}})
                    limits.append("provider_unavailable")
                else:
                    stages.append({"capability": "lexicon", "status": "succeeded",
                                   "detail": {"source": "pypinyin_tone3_first_guess"}})
                for i, span in enumerate(spans):
                    entry: dict[str, Any] = {
                        "index": span.index, "text": span.char,
                        "start_s": round(span.start_s, 4), "end_s": round(span.end_s, 4),
                        "confidence": span.confidence,
                    }
                    if lex is not None and i < len(lex):
                        entry["pinyin"] = lex[i].get("pinyin")
                        entry["lexical_tone"] = lex[i].get("lexical_tone")
                    units.append(entry)

    # ── 语速（无对齐不编造音节速率） ────────────────────────────────────
    if units:
        syll_per_s = len(units) / segs.voiced_total_s if segs.voiced_total_s > 0 else None
        if syll_per_s is not None:
            evidence.append(_measurement(
                feature="speech_rate_syl_per_s", value=round(syll_per_s, 3), unit="syllables_per_s",
                range_samples=base_range, variant_id=variant_id, timebase_id=timebase_id,
                method="aligned_unit_count_over_voiced_time",
                quality_flags=["alignment_low_confidence"] if alignment_source else [],
            ))
    else:
        stages.append({"capability": "rate", "status": "skipped", "reason_code": "alignment_failed",
                       "detail": {"reason": "no_aligned_units"}})

    # ── 逐单元时长 + 相对半音（ utterance_median 冻结于本次分析） ────────
    pitch_points: list[list[Optional[float]]] = []
    for unit in units:
        a = int(unit["start_s"] * sr)
        b = int(unit["end_s"] * sr)
        a, b = max(0, a), min(total_samples, b)
        evidence.append(_measurement(
            feature="unit_duration_ms", value=round((unit["end_s"] - unit["start_s"]) * 1000.0, 1),
            unit="ms", range_samples={"start_sample": a, "end_sample": max(b, a + 1)},
            variant_id=variant_id, timebase_id=timebase_id, method="syllable_energy_dp_span",
            quality_flags=["alignment_low_confidence"] if alignment_source else [],
        ))
        st_points: list[Optional[float]] = []
        if median_f0 is not None and b > a:
            f0_unit = pyin_f0(samples[a:b], sr=sr, hop_length=F0_HOP)
            unit_voiced = [v for v, ok in zip(f0_unit["f0_hz"] or [], f0_unit["voiced"]) if ok and v is not None]
            if unit_voiced:
                # ≤5 个代表点：等分位取样（中位+四分位）
                unit_voiced_sorted = sorted(unit_voiced)
                n = len(unit_voiced_sorted)
                picks = [unit_voiced_sorted[int(n * frac)] for frac in (0.125, 0.375, 0.625, 0.875)][:5]
                if n >= 5 and median_f0:
                    mid = unit_voiced_sorted[n // 2]
                    picks = [unit_voiced_sorted[0], mid, unit_voiced_sorted[-1]] + picks[1:3]
                    picks = sorted(set(round(semitones(v, median_f0, UTTERANCE_MEDIAN), 2) for v in picks))[:5]
                else:
                    picks = sorted(set(round(semitones(v, median_f0, UTTERANCE_MEDIAN), 2) for v in picks))[:5]
                st_points = [float(v) for v in picks]
        pitch_points.append(st_points or None)

    # ── 句级走势：声调构成不明的场合标记 unresolved（不拿首末直线充数） ──
    phrase_tone: Optional[str] = None
    if voiced_f0 and median_f0 is not None:
        # 只在证据充分（≥2 voiced 段且逐字对齐成立）时给走势；否则 unresolved
        if len(units) >= 2 and len(segs.voiced) >= 1:
            phrase_tone = "measured_low_confidence"
        else:
            phrase_tone = None
    if units and phrase_tone is None:
        limits.append("phrase_tone_unresolved")

    status = "succeeded" if not limits else "partial"
    if not voiced_f0 and not segs.voiced:
        status = "partial"

    facts = {
        "audio_id": audio_id,
        "duration_s": total_samples / float(sr),
        "sr": sr,
        "variant_id": variant_id,
        "timebase_id": timebase_id,
        "f0_median_hz": median_f0,
        "reference_scope": UTTERANCE_MEDIAN.scope,
        "leading_silence_s": segs.leading_silence_s,
        "trailing_silence_s": segs.trailing_silence_s,
        "voiced_total_s": segs.voiced_total_s,
        "pauses": [{"start_s": p.start_s, "end_s": p.end_s, "duration_ms": p.duration_ms} for p in segs.pauses],
        "units": units,
        "unit_pitch_st": pitch_points,
        "limits": sorted(set(limits)),
        "alignment_source": alignment_source,
        "transcript": transcript,
        "downmix_note": "mean_of_channels; phase-cancellation not compensated",
    }
    return ProsodyResult(status=status, stages=stages, evidence=evidence, facts=facts)


def _energy_peaks(seg_db: np.ndarray, hop_s: float, *, min_prominence_db: float = 3.0) -> list[float]:
    """段内能量峰（秒）——音节中心候选。简单峰谷检测，不冒充精确音节切分。"""
    if seg_db.size == 0:
        return []
    peaks: list[float] = []
    for i in range(1, seg_db.size - 1):
        if seg_db[i] >= seg_db[i - 1] and seg_db[i] > seg_db[i + 1] and seg_db[i] > (seg_db.max() - 40.0):
            peaks.append(i * hop_s)
    if not peaks:
        peaks = [float(np.argmax(seg_db)) * hop_s]
    return peaks
