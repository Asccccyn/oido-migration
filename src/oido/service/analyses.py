"""分析任务：幂等创建、租约/fencing、取消与迟到结果隔离（EXEC §3.2/§3.3，E11/E12）。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Optional

from oido.domain.evidence import sanitize_provider_payload
from oido.domain.ids import new_id, utc_now_rfc3339
from oido.domain.states import IllegalTransition, is_terminal_job, job_transition

PROFILES = ("full_evidence", "conversation_fast", "inspect_range")


class AnalysisError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _digest(audio_id: str, profile: str, range_: Optional[dict], budget: Optional[dict]) -> str:
    blob = json.dumps(
        {"audio_id": audio_id, "profile": profile, "range": range_, "budget": budget},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


class AnalysisService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

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
        asset = self.conn.execute(
            "SELECT owner, lifecycle FROM audio_assets WHERE audio_id = ?", (audio_id,)
        ).fetchone()
        if asset is None or asset["owner"] != requested_by or asset["lifecycle"] != "active":
            raise AnalysisError("not_visible", "audio not found", 404)
        digest = _digest(audio_id, profile, range_, budget)
        if idempotency_key:
            row = self.conn.execute(
                """SELECT analysis_id, request_digest FROM analysis_runs
                   WHERE requested_by = ? AND idempotency_key = ?""",
                (requested_by, idempotency_key),
            ).fetchone()
            if row is not None:
                if row["request_digest"] != digest:
                    # E11：同 key 不同请求 → 409，不串用旧任务
                    raise AnalysisError(
                        "idempotency_conflict",
                        "idempotency key reused with a different request",
                        409,
                    )
                return {"analysis_id": row["analysis_id"], "deduplicated": True}
        analysis_id = new_id("run")
        now = utc_now_rfc3339()
        self.conn.execute(
            """INSERT INTO analysis_runs
               (analysis_id, audio_id, profile, requested_by, status, revision,
                idempotency_key, request_digest, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)""",
            (analysis_id, audio_id, profile, requested_by, idempotency_key, digest, now, now),
        )
        self.conn.commit()
        return {"analysis_id": analysis_id, "deduplicated": False}

    def claim(self, analysis_id: str, *, worker: str, lease_seconds: int = 300) -> Optional[dict]:
        """认领任务并发放新 fencing token。任务不可认领时返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise AnalysisError("not_found", "no such analysis", 404)
        now = utc_now_rfc3339()
        if row["status"] == "queued":
            status_to = "running"
        elif row["status"] == "running" and (
            row["lease_expires_at"] is None or row["lease_expires_at"] <= now
        ):
            status_to = "running"  # 租约过期被夺回（worker crash/reclaim）
        else:
            return None
        token = (row["fencing_token"] or 0) + 1
        from datetime import datetime, timedelta, timezone

        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        cur = self.conn.execute(
            """UPDATE analysis_runs
               SET status = ?, lease_owner = ?, lease_expires_at = ?, fencing_token = ?,
                   updated_at = ?
               WHERE analysis_id = ? AND fencing_token = ?""",
            (status_to, worker, expires, token, now, analysis_id, row["fencing_token"]),
        )
        if cur.rowcount != 1:
            # CAS 失败：另一个 worker 抢先（自审计勘误：此前仍返回租约，造成假授予）
            self.conn.rollback()
            return None
        self.conn.commit()
        return {"analysis_id": analysis_id, "fencing_token": token, "lease_expires_at": expires}

    def commit_result(
        self,
        *,
        analysis_id: str,
        fencing_token: int,
        status: str,                      # succeeded | partial | failed
        stages: list[dict],
        evidence: Optional[list[dict]] = None,
        reason_code: Optional[str] = None,
    ) -> dict:
        """带 fencing 的结果提交：token 过期即拒绝（E12：迟到 worker 不得复活）。"""
        row = self.conn.execute(
            "SELECT * FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise AnalysisError("not_found", "no such analysis", 404)
        if row["fencing_token"] != fencing_token:
            raise AnalysisError(
                "stale_fence",
                "fencing token expired; a newer lease owns this run",
                409,
            )
        if is_terminal_job(row["status"]):
            # 取消/删除后终结不可复活
            raise AnalysisError(
                "already_terminal", f"run already {row['status']}", 409
            )
        job_transition(row["status"], status)
        revision = row["revision"] + 1
        now = utc_now_rfc3339()
        self.conn.execute(
            "UPDATE analysis_runs SET status = ?, revision = ?, error_code = ?, updated_at = ? WHERE analysis_id = ?",
            (status, revision, reason_code, now, analysis_id),
        )
        for stage in stages:
            self.conn.execute(
                """INSERT INTO analysis_stages (stage_id, analysis_id, capability, status, reason_code, detail)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (analysis_id, capability) DO UPDATE SET
                     status = excluded.status, reason_code = excluded.reason_code, detail = excluded.detail""",
                (
                    new_id("stg"), analysis_id, stage["capability"], stage["status"],
                    stage.get("reason_code"), json.dumps(stage.get("detail", {}), ensure_ascii=False),
                ),
            )
        for ev in evidence or []:
            # B20 第二道闸：provider 边界净化之外的持久层兜底——
            # parameters 等嵌套结构里的情绪字段同样不得落库（值与键都不进 payload）
            ev, dropped = sanitize_provider_payload(ev)
            if dropped:
                ev = ev | {"quality_flags": sorted(set(ev.get("quality_flags", [])) | {"sanitized_at_persistence"})}
            self.conn.execute(
                """INSERT INTO evidence_items
                   (evidence_id, analysis_id, revision, kind, feature, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ev.get("evidence_id") or new_id("ev"), analysis_id, revision,
                    ev["kind"], ev["feature"],
                    json.dumps(ev, ensure_ascii=False), now,
                ),
            )
        self.conn.commit()
        return {"analysis_id": analysis_id, "status": status, "revision": revision}

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
        # 递增 fencing_token：在途 worker 的提交将因 stale_fence 被拒（E12）
        self.conn.execute(
            """UPDATE analysis_runs SET status = 'cancelled',
               fencing_token = fencing_token + 1, updated_at = ? WHERE analysis_id = ?""",
            (utc_now_rfc3339(), analysis_id),
        )
        self.conn.commit()
        return {"analysis_id": analysis_id, "status": "cancelled", "idempotent": False}

    def get(self, analysis_id: str, *, principal: str) -> dict:
        row = self.conn.execute(
            """SELECT ar.*, aa.owner AS asset_owner FROM analysis_runs ar
               JOIN audio_assets aa ON aa.audio_id = ar.audio_id
               WHERE ar.analysis_id = ?""",
            (analysis_id,),
        ).fetchone()
        if row is None or row["asset_owner"] != principal:
            raise AnalysisError("not_visible", "analysis not found", 404)
        stages = [
            dict(r)
            for r in self.conn.execute(
                "SELECT capability, status, reason_code FROM analysis_stages WHERE analysis_id = ?",
                (analysis_id,),
            )
        ]
        return {k: row[k] for k in row.keys()} | {"stages": stages}
