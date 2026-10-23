"""分析任务：幂等创建、租约/fencing、取消与迟到结果隔离（EXEC §3.2/§3.3，E11/E12）。

审计 F01/F05/F06/F07/F08 修复要点：
- commit_result/claim 用**条件 UPDATE**原子校验 token+状态+资产生命周期，看行数；
- 一切多语句写库 try/except 回滚，失败不留给下个请求提交（F05）；
- 证据先 sanitize + validate_evidence 再落库，JSON 禁 NaN（F04c/F06）；
- 同幂等键并发插入冲突 → 回读比对 → 去重或 409，不抛裸 IntegrityError（F07）；
- 请求的 range/budget 以 request_json 持久化，claim 时返回（F08）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from oido.domain.evidence import (
    EvidenceValidationError, sanitize_provider_payload, validate_evidence,
    validate_evidence_contract,
)
from oido.domain.ids import new_id, utc_now_rfc3339
from oido.domain.states import (
    REASON_CODES, STAGE_STATES, IllegalTransition, is_terminal_job, job_transition,
)

PROFILES = ("prosody", "full_evidence", "conversation_fast", "inspect_range")


class AnalysisError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

def _pipeline_version() -> str:
    from oido.prosody.pipeline import PIPELINE_METHOD_VERSION
    return PIPELINE_METHOD_VERSION


def _transcript_hash(conn: sqlite3.Connection, audio_id: str) -> str:
    row = conn.execute(
        "SELECT text FROM audio_transcripts WHERE audio_id = ? LIMIT 1",
        (audio_id,),
    ).fetchone()
    if row is None or row["text"] is None:
        return "no-transcript"
    return hashlib.sha256(row["text"].encode("utf-8")).hexdigest()


class AnalysisService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── 创建（幂等，F07/F08）────────────────────────────────────────────
    def create(
        self,
        *,
        audio_id: str,
        profile: str,
        requested_by: str,
        idempotency_key: Optional[str] = None,
        range_: Optional[dict] = None,
        budget: Optional[dict] = None,
    ) -> dict:
        if profile not in PROFILES:
            raise AnalysisError("invalid_profile", f"unknown profile: {profile}", 422)
        # Keep the old persisted {start,end} request contract intact. Its units
        # were unspecified; the worker must reject execution rather than guess.
        legacy_range = isinstance(range_, dict) and set(range_) == {"start", "end"}
        if range_ is not None and not legacy_range and (not isinstance(range_, dict)
                or set(range_) != {"start_ms", "end_ms"}
                or any(type(range_[k]) is not int for k in ("start_ms", "end_ms"))
                or not 0 <= range_["start_ms"] < range_["end_ms"]):
            raise AnalysisError("invalid_range", "range must be [start,end) milliseconds", 422)
        if profile == "inspect_range" and range_ is None:
            raise AnalysisError("invalid_range", "inspect_range requires range_ms", 422)
        # R05：空/白字串幂等键统一按"无键"处理，杜绝唯一索引裸异常
        if idempotency_key is not None and not idempotency_key.strip():
            idempotency_key = None
        asset = self.conn.execute(
            "SELECT owner, lifecycle FROM audio_assets WHERE audio_id = ?", (audio_id,)
        ).fetchone()
        if asset is None or asset["owner"] != requested_by or asset["lifecycle"] != "active":
            raise AnalysisError("not_visible", "audio not found", 404)
        # R6′（2026-10-23，她批 R6P 方案）：digest 纳入版本成分——pipeline 方法版本 +
        # 当前可信转写内容 hash（无转写=哨兵）。同源在新版本下产生新 digest=新分析，
        # 旧错误结果不再命中；操作幂等键语义不变（同键异 digest 仍 409，E11 反例保留）。
        pipeline_version = _pipeline_version()
        transcript_hash = _transcript_hash(self.conn, audio_id)
        request_payload = {"audio_id": audio_id, "profile": profile,
                           "range": range_, "budget": budget,
                           "pipeline_version": pipeline_version,
                           "transcript_sha256": transcript_hash}
        digest = _digest(request_payload)
        if idempotency_key:
            existing = self._find_by_key(requested_by, idempotency_key)
            if existing is not None:
                if existing["request_digest"] != digest:
                    # E11：同 key 不同请求 → 409，不串用旧任务
                    raise AnalysisError(
                        "idempotency_conflict",
                        "idempotency key reused with a different request",
                        409,
                    )
                return {"analysis_id": existing["analysis_id"], "deduplicated": True}
        analysis_id = new_id("run")
        now = utc_now_rfc3339()
        try:
            # S04：资产仍 active 作为 INSERT 的原子条件——创建与 owner 删除交错时
            # 不再产生"永远无法执行"的 queued 任务，而是规范 404
            cur = self.conn.execute(
                """INSERT INTO analysis_runs
                   (analysis_id, audio_id, profile, requested_by, status, revision,
                    idempotency_key, request_digest, request_json, created_at, updated_at)
                   SELECT ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?
                   WHERE EXISTS (SELECT 1 FROM audio_assets aa
                                 WHERE aa.audio_id = ? AND aa.owner = ?
                                   AND aa.lifecycle = 'active')""",
                (
                    analysis_id, audio_id, profile, requested_by, idempotency_key, digest,
                    json.dumps(request_payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    now, now, audio_id, requested_by,
                ),
            )
            if cur.rowcount != 1:
                self.conn.rollback()
                raise AnalysisError("not_visible", "audio not found", 404)
            self.conn.commit()
        except sqlite3.IntegrityError:
            # F07：并发下唯一索引先于我们插入——回读比对，去重或 409
            self.conn.rollback()
            existing = self._find_by_key(requested_by, idempotency_key) if idempotency_key else None
            if existing is None:
                raise
            if existing["request_digest"] != digest:
                raise AnalysisError(
                    "idempotency_conflict",
                    "idempotency key reused with a different request",
                    409,
                )
            return {"analysis_id": existing["analysis_id"], "deduplicated": True}
        return {"analysis_id": analysis_id, "deduplicated": False}

    def _find_by_key(self, principal: str, key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT analysis_id, request_digest FROM analysis_runs
               WHERE requested_by = ? AND idempotency_key = ?""",
            (principal, key),
        ).fetchone()

    # ── 认领（F01：条件 UPDATE 原子校验，删除/取消后不可复活）──────────
    def claim(self, analysis_id: str, *, worker: str, lease_seconds: int = 300) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise AnalysisError("not_found", "no such analysis", 404)
        now = utc_now_rfc3339()
        if row["status"] == "queued":
            pass
        elif row["status"] == "running" and (
            row["lease_expires_at"] is None or row["lease_expires_at"] <= now
        ):
            pass  # 租约过期夺回（worker crash/reclaim）
        else:
            return None
        token = (row["fencing_token"] or 0) + 1
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        cur = self.conn.execute(
            """UPDATE analysis_runs
               SET status = 'running', lease_owner = ?, lease_expires_at = ?, fencing_token = ?,
                   updated_at = ?
               WHERE analysis_id = ? AND fencing_token = ?
                 AND status IN ('queued', 'running')
                 AND (status = 'queued' OR lease_expires_at IS NULL OR lease_expires_at <= ?)
                 AND EXISTS (SELECT 1 FROM audio_assets aa
                             WHERE aa.audio_id = analysis_runs.audio_id AND aa.lifecycle = 'active')""",
            (worker, expires, token, now, analysis_id, row["fencing_token"], now),
        )
        if cur.rowcount != 1:
            # CAS 落败：取消/删除/他人抢先都不可复活（F1/F6 竞态闭环）
            self.conn.rollback()
            return None
        self.conn.commit()
        request = json.loads(row["request_json"]) if row["request_json"] else None
        return {"analysis_id": analysis_id, "fencing_token": token,
                "lease_expires_at": expires, "request": request}

    # ── 结果提交（F01/F04/F05/F06）─────────────────────────────────────
    def commit_result(
        self,
        *,
        analysis_id: str,
        fencing_token: int,
        status: str,
        stages: list[dict],
        evidence: Optional[list[dict]] = None,
        reason_code: Optional[str] = None,
    ) -> dict:
        # 先整体验证再动库（F05/F06：把失败挡在副作用之前）
        if status not in ("succeeded", "partial", "failed"):
            raise AnalysisError("invalid_input", f"bad commit status: {status}", 422)
        # R07/S02：partial 必须可解释——非空 stages、至少一项缺失/降级能力、
        # 降级项必带**合同枚举内**的原因（EXEC §3.2）
        degraded_statuses = ("partial", "skipped", "failed")
        seen_capabilities: set[str] = set()
        for stage in stages:
            # T01：同批重复 capability 会被 (analysis_id, capability) 的 UPSERT 合并，
            # 校验集合 ≠ 最终持久集合——直接拒绝，保证两套集合恒等
            cap = stage.get("capability")
            if not isinstance(cap, str) or not cap.strip():
                raise AnalysisError("invalid_input", "stage capability must be a non-empty string", 422)
            if cap in seen_capabilities:
                raise AnalysisError(
                    "invalid_input",
                    f"duplicate capability in one commit batch: {cap!r}", 422,
                )
            seen_capabilities.add(cap)
            if stage["status"] not in STAGE_STATES:
                raise AnalysisError("invalid_input", f"bad stage status: {stage['status']!r}", 422)
            reason = stage.get("reason_code")
            if reason is not None and reason not in REASON_CODES:
                raise AnalysisError(
                    "invalid_input",
                    f"stage {stage['capability']!r} reason_code {reason!r} not in contract enum",
                    422,
                )
            if stage["status"] in degraded_statuses and not reason:
                raise AnalysisError(
                    "invalid_input",
                    f"stage {stage['capability']!r} is {stage['status']} without reason_code",
                    422,
                )
        if status == "partial":
            if not stages:
                raise AnalysisError("invalid_input", "partial requires at least one stage", 422)
            if not any(s["status"] in degraded_statuses for s in stages):
                raise AnalysisError(
                    "invalid_input",
                    "partial requires at least one stage with missing/incomplete capability",
                    422,
                )
        clean_stages = []
        for stage in stages:
            detail, detail_dropped = sanitize_provider_payload(stage.get("detail") or {})
            clean_stages.append({
                "capability": stage["capability"], "status": stage["status"],
                "reason_code": stage.get("reason_code"),
                "detail": detail, "detail_dropped": detail_dropped,
            })
        clean_evidence: list[dict] = []
        for ev in evidence or []:
            ev, ev_dropped = sanitize_provider_payload(ev)
            if ev_dropped:
                ev = ev | {"quality_flags": sorted(set(ev.get("quality_flags", [])) | {"sanitized_at_persistence"})}
            try:
                validate_evidence(ev)          # F04c/F06/R03：持久层完整合同校验
            except EvidenceValidationError as exc:
                raise AnalysisError("invalid_evidence", str(exc), 422) from exc
            clean_evidence.append(ev)

        # R03：证据归属/边界校验——variant 必须真实存在、属于本资产、
        # timebase 一致、range 不越出实际 sample_count（用数据库真实值，不靠声明）
        row = self.conn.execute(
            "SELECT audio_id FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise AnalysisError("not_found", "no such analysis", 404)
        for ev in clean_evidence:
            variant = self.conn.execute(
                """SELECT audio_id, timebase_id, sample_count FROM audio_variants
                   WHERE variant_id = ?""",
                (ev["input_variant_id"],),
            ).fetchone()
            if variant is None:
                raise AnalysisError(
                    "invalid_evidence",
                    f"unknown input_variant_id: {ev['input_variant_id']!r}", 422,
                )
            if variant["audio_id"] != row["audio_id"]:
                raise AnalysisError(
                    "invalid_evidence", "variant belongs to a different audio", 422)
            if ev["timebase_id"] != variant["timebase_id"]:
                raise AnalysisError(
                    "invalid_evidence",
                    f"timebase mismatch: evidence {ev['timebase_id']!r} vs variant "
                    f"{variant['timebase_id']!r}", 422,
                )
            if ev["range"]["end_sample"] > variant["sample_count"]:
                raise AnalysisError(
                    "invalid_evidence",
                    f"range end {ev['range']['end_sample']} exceeds variant sample_count "
                    f"{variant['sample_count']}", 422,
                )

        row = self.conn.execute(
            "SELECT * FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise AnalysisError("not_found", "no such analysis", 404)
        if row["fencing_token"] != fencing_token:
            raise AnalysisError("stale_fence", "fencing token expired; a newer lease owns this run", 409)
        if is_terminal_job(row["status"]):
            raise AnalysisError("already_terminal", f"run already {row['status']}", 409)
        try:
            job_transition(row["status"], status)
        except IllegalTransition as exc:
            raise AnalysisError("invalid_transition", str(exc), 409) from exc
        revision = row["revision"] + 1
        now = utc_now_rfc3339()
        # S01：输入草稿 → 最终持久结果。回填权威 audio/analysis/revision/evidence_id，
        # 再对**最终形态**执行自家 schema 合同校验（此前 schema 只写在文件里不执行）
        for ev in clean_evidence:
            ev["audio_id"] = row["audio_id"]
            ev["analysis_id"] = analysis_id
            ev["revision"] = revision
            if not isinstance(ev.get("evidence_id"), str) or not ev["evidence_id"]:
                ev["evidence_id"] = new_id("ev")
            try:
                validate_evidence_contract(ev)
            except EvidenceValidationError as exc:
                raise AnalysisError("invalid_evidence", str(exc), 422) from exc
        try:
            # F01 核心：条件 UPDATE 原子验证 token + 非终态 + 资产仍 active；
            # SELECT 与 UPDATE 之间发生的取消/删除会在这里落空
            cur = self.conn.execute(
                """UPDATE analysis_runs
                   SET status = ?, revision = ?, error_code = ?, updated_at = ?
                   WHERE analysis_id = ? AND fencing_token = ? AND status NOT IN
                     ('succeeded','partial','failed','cancelled')
                     AND EXISTS (SELECT 1 FROM audio_assets aa
                                 WHERE aa.audio_id = analysis_runs.audio_id
                                   AND aa.lifecycle = 'active')""",
                (status, revision, reason_code, now, analysis_id, fencing_token),
            )
            if cur.rowcount != 1:
                self.conn.rollback()
                fresh = self.conn.execute(
                    "SELECT status, fencing_token FROM analysis_runs WHERE analysis_id = ?",
                    (analysis_id,),
                ).fetchone()
                if fresh is not None and is_terminal_job(fresh["status"]):
                    raise AnalysisError("already_terminal", f"run already {fresh['status']}", 409)
                raise AnalysisError("stale_fence", "concurrent state change; commit rejected", 409)
            for stage in clean_stages:
                self.conn.execute(
                    """INSERT INTO analysis_stages (stage_id, analysis_id, capability, status, reason_code, detail)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT (analysis_id, capability) DO UPDATE SET
                         status = excluded.status, reason_code = excluded.reason_code, detail = excluded.detail""",
                    (
                        new_id("stg"), analysis_id, stage["capability"], stage["status"],
                        stage["reason_code"],
                        json.dumps(stage["detail"], ensure_ascii=False, allow_nan=False),
                    ),
                )
            for ev in clean_evidence:
                self.conn.execute(
                    """INSERT INTO evidence_items
                       (evidence_id, analysis_id, revision, kind, feature, payload, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ev.get("evidence_id") or new_id("ev"), analysis_id, revision,
                        ev["kind"], ev["feature"],
                        json.dumps(ev, ensure_ascii=False, allow_nan=False), now,
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()      # F05：失败不留半开事务给下一个请求
            raise
        return {"analysis_id": analysis_id, "status": status, "revision": revision}

    # ── 取消（幂等；递增 fence）────────────────────────────────────────
    def cancel(self, analysis_id: str, *, principal: str) -> dict:
        row = self.conn.execute(
            """SELECT ar.*, aa.owner AS asset_owner FROM analysis_runs ar
               JOIN audio_assets aa ON aa.audio_id = ar.audio_id
               WHERE ar.analysis_id = ?""",
            (analysis_id,),
        ).fetchone()
        if row is None or row["asset_owner"] != principal:
            raise AnalysisError("not_visible", "analysis not found", 404)
        if is_terminal_job(row["status"]):
            return {"analysis_id": analysis_id, "status": row["status"], "idempotent": True}
        job_transition(row["status"], "cancelled")
        try:
            # 递增 fencing_token：在途 worker 的提交将因 stale_fence 被拒（E12）
            cur = self.conn.execute(
                """UPDATE analysis_runs SET status = 'cancelled',
                   fencing_token = fencing_token + 1, updated_at = ?
                   WHERE analysis_id = ? AND status NOT IN
                     ('succeeded','partial','failed','cancelled')""",
                (utc_now_rfc3339(), analysis_id),
            )
            if cur.rowcount != 1:
                # R06：竞争失败不得谎报 cancelled——回读真实终态如实返回
                self.conn.rollback()
                fresh = self.conn.execute(
                    "SELECT status FROM analysis_runs WHERE analysis_id = ?",
                    (analysis_id,),
                ).fetchone()
                actual = fresh["status"] if fresh is not None else "unknown"
                return {"analysis_id": analysis_id, "status": actual,
                        "idempotent": False, "race_lost": True}
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"analysis_id": analysis_id, "status": "cancelled", "idempotent": False}

    # ── 读取（F03：删除后不可读）───────────────────────────────────────
    def get(self, analysis_id: str, *, principal: str) -> dict:
        row = self.conn.execute(
            """SELECT ar.*, aa.owner AS asset_owner, aa.lifecycle AS asset_lifecycle
               FROM analysis_runs ar JOIN audio_assets aa ON aa.audio_id = ar.audio_id
               WHERE ar.analysis_id = ?""",
            (analysis_id,),
        ).fetchone()
        if (row is None or row["asset_owner"] != principal
                or row["asset_lifecycle"] != "active"):
            raise AnalysisError("not_visible", "analysis not found", 404)
        stages = [
            dict(r)
            for r in self.conn.execute(
                "SELECT capability, status, reason_code FROM analysis_stages WHERE analysis_id = ?",
                (analysis_id,),
            )
        ]
        return {k: row[k] for k in row.keys()} | {"stages": stages}
