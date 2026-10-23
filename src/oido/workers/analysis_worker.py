"""分析 worker：认领→解码→韵律管线→提交（联合施工 P3：worker 本体与恢复）。

租约/fencing 全部复用 AnalysisService（claim 过期夺回、commit 条件 UPDATE）。
facts 随 stages.detail 原子落库（capability=prosody_facts）；F0 全曲线尽力落
对象存储（view 未实装，失败不阻断）。启动即扫 queued/过期 running——崩溃
恢复不需要额外状态表。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from oido.domain.states import REASON_CODES
from oido.prosody import pipeline
from oido.prosody.pipeline import run_prosody
from oido.service.analyses import AnalysisError, AnalysisService
from oido.service.ingest import IngestService
from oido.service.variants import VariantError, VariantService
from oido.storage.objects import ObjectStore

FACTS_CAPABILITY = "prosody_facts"


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def process_one(
    *,
    conn: sqlite3.Connection,
    analyses: AnalysisService,
    variants: VariantService,
    ingest: IngestService,
    objects: ObjectStore,
    analysis_id: Optional[str] = None,
    worker_name: str = "worker-1",
) -> Optional[dict]:
    """处理一个任务；无任务返回 None。analysis_id 指定时只试该任务（HTTP 同步等待用）。"""
    if analysis_id is None:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        row = conn.execute(
            """SELECT analysis_id FROM analysis_runs
               WHERE status = 'queued'
                  OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= ?))
               ORDER BY created_at LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return None
        analysis_id = row["analysis_id"]
    lease = analyses.claim(analysis_id, worker=worker_name)
    if lease is None:
        return None
    audio_id = lease["request"]["audio_id"]

    def _cancelled() -> bool:
        # R6′（2026-10-23）：取消检查点——已取消不再推进新阶段（在途 ffmpeg/pYIN 段
        # 不可抢占，如实等它自然结束；迟到提交仍被 fence 拒）
        row = conn.execute("SELECT status FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)).fetchone()
        return row is not None and row["status"] == "cancelled"

    if _cancelled():
        return None
    try:
        asset = conn.execute(
            "SELECT original_object_key FROM audio_assets WHERE audio_id = ?", (audio_id,)
        ).fetchone()
        original = objects.read_bytes(asset["original_object_key"])
        variant = variants.ensure_decode_main(audio_id=audio_id, original_bytes=original)
        if _cancelled():
            return None
        samples = variants.load_pcm(variant)
        transcript = ingest.transcript_of(audio_id)
        requested_range = lease["request"].get("range")
        start_sample, end_sample = 0, len(samples)
        if requested_range is not None:
            if set(requested_range) != {"start_ms", "end_ms"}:
                raise VariantError("unsupported", "legacy range has no explicit time unit; resubmit range_ms")
            sr = variant["sample_rate_hz"]
            start_sample = requested_range["start_ms"] * sr // 1000
            end_sample = requested_range["end_ms"] * sr // 1000
            if not 0 <= start_sample < end_sample <= len(samples):
                raise VariantError("invalid_media", "range exceeds the source audio")
            samples = samples[start_sample:end_sample]
            # A whole-recording transcript has no trustworthy word boundaries
            # for this crop. Do not force its full text onto a shorter signal.
            if start_sample or end_sample != variant["sample_count"]:
                transcript = None
        result = run_prosody(
            audio_id=audio_id,
            samples=samples,
            sr=variant["sample_rate_hz"],
            variant_id=variant["variant_id"],
            timebase_id=variant["timebase_id"],
            transcript=transcript,
        )
        offset_s = start_sample / variant["sample_rate_hz"]
        result.facts["range_ms"] = [round(start_sample * 1000 / variant["sample_rate_hz"]),
                                    round(end_sample * 1000 / variant["sample_rate_hz"])]
        for item in result.facts.get("units", []) + result.facts.get("pauses", []):
            item["start_s"] += offset_s
            item["end_s"] += offset_s
        for evidence in result.evidence:
            evidence["range"]["start_sample"] += start_sample
            evidence["range"]["end_sample"] += start_sample
        decode_stage = {"capability": "decode", "status": "succeeded",
                        "detail": {"decode_profile": "pcm_f32le_16000_mono",
                                   "sr": variant["sample_rate_hz"],
                                   "sample_count": variant["sample_count"]}}
        stages = [decode_stage] + result.stages + [{
            "capability": FACTS_CAPABILITY, "status": "succeeded",
            "detail": {"facts": result.facts},
        }]
        committed = analyses.commit_result(
            analysis_id=analysis_id,
            fencing_token=lease["fencing_token"],
            status=result.status,
            stages=stages,
            evidence=result.evidence,
        )
    except VariantError as exc:
        committed = _fail(conn, analyses, analysis_id, lease["fencing_token"], exc.code, str(exc))
    except AnalysisError:
        raise     # 认证/状态类错误：不改任务状态，让调用方处理
    except Exception as exc:  # noqa: BLE001——worker 必须把一切失败落成任务终态
        committed = _fail(conn, analyses, analysis_id, lease["fencing_token"], "dependency_failed", str(exc)[:200])
    return committed


def _fail(conn, analyses, analysis_id, token, reason, detail) -> dict:
    if reason not in REASON_CODES:
        reason = "dependency_failed"
    try:
        return analyses.commit_result(
            analysis_id=analysis_id,
            fencing_token=token,
            status="failed",
            stages=[{"capability": "pipeline", "status": "failed",
                     "reason_code": reason, "detail": {"error": detail[:200]}}],
            evidence=[],
            reason_code=reason,
        )
    except AnalysisError:
        # 已被取消/删除/换租约：迟到失败提交被拒是正确行为
        row = conn.execute("SELECT status FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)).fetchone()
        return {"analysis_id": analysis_id, "status": row["status"] if row else "unknown",
                "late_failure_rejected": True}


def load_facts(conn: sqlite3.Connection, analysis_id: str) -> Optional[dict]:
    """读取 prosody_facts stage detail（oido_get 投影数据源）。"""
    row = conn.execute(
        "SELECT detail FROM analysis_stages WHERE analysis_id = ? AND capability = ?",
        (analysis_id, FACTS_CAPABILITY),
    ).fetchone()
    if row is None or not row["detail"]:
        return None
    detail = json.loads(row["detail"])
    return detail.get("facts") if isinstance(detail, dict) else None
