"""HTTP API v1：health/capabilities + 受信导入/分析/投影（联合施工 P3）。

只绑定 loopback（config.bind_port，默认 0=不监听）；/v1/* 除 capabilities 外
要求 bearer token（auth.py），每次 allow/deny 落 access_audit。
ingest/offline_analysis 两个 flag 分别控制导入与分析面（可独立关闭）。
错误一律走预算内 error 投影（code 大写化 + 安全 message）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from oido import providers
from oido.auth import AuthError, audit, token_from_env, verify_bearer
from oido.config import OidoConfig
from oido.prosody import projections
from oido.prosody.projections import ProjectionError
from oido.domain.ids import utc_now_rfc3339
from oido.service.analyses import PROFILES, AnalysisError, AnalysisService
from oido.service.assets import AssetError, AssetService
from oido.service.ingest import IngestService, RefError
from oido.service.variants import VariantService
from oido.storage.objects import ObjectStore
from oido.workers import analysis_worker


class TranscriptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    source: str
    source_revision: Optional[str] = None


class FindIn(BaseModel):
    """R6′（2026-10-23）：HTTP find 入参收紧——对齐 MCP 合同（未知字段拒绝，类型强校验）。"""
    model_config = {"extra": "forbid"}
    audio_ref: str
    query: str = Field(min_length=1, max_length=200)
    mode: Literal["substring", "fuzzy"] = "substring"
    limit: int = Field(default=3, ge=1, le=10)
    cursor: str | None = None


class CompareIn(BaseModel):
    """R6′（2026-10-23）：HTTP compare 入参收紧（同上）。"""
    model_config = {"extra": "forbid"}
    left: dict
    right: dict
    metrics: list[str] | None = None


class AnalyzeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_ref: str
    profile: str = "prosody"
    range_ms: Optional[list[int]] = None
    idempotency_key: Optional[str] = None
    wait_ms: int = 0


def _error_response(status: int, code: str, message: str, retryable: Optional[bool] = None):
    try:
        body = projections.error_body(code.upper().replace("-", "_"), message, retryable=retryable)
    except ProjectionError:
        body = {"code": "INTERNAL_ERROR", "message": "error projection failed"}
    return JSONResponse(body, status_code=status)


def create_app(config: OidoConfig | None = None, conn: sqlite3.Connection | None = None) -> FastAPI:
    cfg = config or OidoConfig.from_env()
    app = FastAPI(title="oido", version="0.2.0")
    token = token_from_env()

    objects = ObjectStore(cfg.data_root) if conn is not None else None
    assets = AssetService(conn, objects) if conn is not None and objects else None
    analyses = AnalysisService(conn) if conn is not None else None
    variants = VariantService(conn, objects) if conn is not None and objects else None
    ingest = IngestService(conn, assets) if conn is not None and assets else None
    views = None
    finder = None
    comparer = None
    if conn is not None and analyses is not None and ingest is not None:
        from oido.service.compare import CompareService
        from oido.service.find import FindService
        from oido.service.views import ViewService
        if variants is not None and objects is not None:
            views = ViewService(conn, variants, objects)
        finder = FindService(analyses, ingest)
        comparer = CompareService(analyses, ingest)

    # ── 鉴权依赖 ────────────────────────────────────────────────────────
    async def principal_of(request: Request) -> str:
        try:
            return verify_bearer(request.headers.get("authorization"), token)
        except AuthError as exc:
            audit_deny(conn, request, exc.code)
            raise HTTPException(401, detail=exc.code) from exc

    def audit_deny(db, request: Request, code: str) -> None:
        if db is not None:
            try:
                audit(db, principal="anonymous", action=str(request.url.path),
                      object_id=str(request.url.path), decision="deny")
            except sqlite3.Error:
                pass

    @app.get("/health/live")
    def live() -> dict:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> dict:
        required_tables = (
            "audio_assets", "audio_origins", "audio_variants", "analysis_runs",
            "analysis_stages", "evidence_items", "artifacts", "events_outbox",
            "idempotency_records", "deletion_tombstones", "acoustic_baselines",
            "access_audit", "orphan_objects", "audio_transcripts",
        )
        db_ok, reason = False, "no_storage"
        if conn is not None:
            try:
                from oido.storage.db import expected_versions

                applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
                expected = expected_versions(cfg.migrations_dir)
                if not applied:
                    reason = "migrations_empty"
                elif applied != expected:
                    reason = f"migrations_mismatch:missing={sorted(expected - applied)}"
                else:
                    present = {
                        r["name"] for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    missing = [t for t in required_tables if t not in present]
                    if missing:
                        reason = f"missing_tables:{','.join(missing)}"
                    else:
                        cols = {r["name"] for r in conn.execute("PRAGMA table_info(analysis_runs)")}
                        if "request_json" not in cols:
                            reason = "missing_column:analysis_runs.request_json"
                        else:
                            db_ok, reason = True, "ok"
            except sqlite3.Error as exc:
                reason = f"db_error:{exc.__class__.__name__}"
        return {"status": "ready" if db_ok else "degraded", "db": db_ok,
                "reason": reason, "flags": cfg.flags}

    @app.get("/v1/capabilities")
    def caps() -> dict:
        return {"providers": providers.capabilities(), "flags": cfg.flags}

    # ── 受信导入（est 宿主推送；metadata 与字节分开发送） ────────────────
    @app.post("/v1/audio:ingest")
    async def audio_ingest(request: Request):
        principal = await principal_of(request)
        if conn is None or ingest is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        if not cfg.enabled("ingest"):
            audit(conn, principal=principal, action="ingest", object_id="-", decision="deny")
            return _error_response(503, "SERVICE_DISABLED", "ingest flag is off", retryable=True)
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            return _error_response(422, "INVALID_INPUT", "invalid content length")
        if declared > 60 * 1024 * 1024:
            return _error_response(413, "BODY_TOO_LARGE", "audio payload exceeds 60MB limit")
        ref = request.headers.get("x-oido-audio-ref", "")
        sha = request.headers.get("x-oido-content-sha256") or None
        mime = request.headers.get("x-oido-mime") or None
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 60 * 1024 * 1024:
                return _error_response(413, "BODY_TOO_LARGE", "audio payload exceeds 60MB limit")
            data.extend(chunk)
        try:
            result = ingest.ingest(
                principal=principal, audio_ref=ref, data=data,
                content_sha256=sha, mime=mime,
            )
            audit(conn, principal=principal, action="ingest", object_id=result["audio_id"], decision="allow")
            return result
        except RefError as exc:
            audit(conn, principal=principal, action="ingest", object_id=ref, decision="deny")
            return _error_response(exc.http_status, exc.code, str(exc))
        except AssetError as exc:
            audit(conn, principal=principal, action="ingest", object_id=ref, decision="deny")
            return _error_response(400, exc.code, str(exc))

    @app.post("/v1/audio/{audio_id}/transcript")
    async def audio_transcript(audio_id: str, body: TranscriptIn, request: Request):
        principal = await principal_of(request)
        if conn is None or ingest is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        if not cfg.enabled("ingest"):
            return _error_response(503, "SERVICE_DISABLED", "ingest flag is off", retryable=True)
        try:
            assets.get(audio_id, principal=principal)
        except AssetError as exc:
            audit(conn, principal=principal, action="transcript", object_id=audio_id, decision="deny")
            return _error_response(404, exc.code, str(exc))
        if not body.text.strip():
            return _error_response(422, "INVALID_INPUT", "transcript.text must be non-empty")
        try:
            inserted = conn.execute(
                """INSERT INTO audio_transcripts (audio_id, text, source, source_revision, created_at)
                   SELECT ?, ?, ?, ?, ? WHERE EXISTS
                   (SELECT 1 FROM audio_assets WHERE audio_id = ? AND owner = ? AND lifecycle = 'active')""",
                (audio_id, body.text, body.source, body.source_revision, utc_now_rfc3339(), audio_id, principal),
            )
            if inserted.rowcount != 1:
                conn.rollback()
                return _error_response(404, "NOT_VISIBLE", "audio not found")
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return _error_response(409, "ALREADY_EXISTS", "transcript already set; revisions not supported here")
        audit(conn, principal=principal, action="transcript", object_id=audio_id, decision="allow")
        return {"audio_id": audio_id, "stored": True}

    @app.get("/v1/audio/{audio_id}")
    async def audio_meta(audio_id: str, request: Request):
        principal = await principal_of(request)
        if conn is None or assets is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        try:
            row = assets.get(audio_id, principal=principal)
        except AssetError as exc:
            audit(conn, principal=principal, action="read", object_id=audio_id, decision="deny")
            return _error_response(404, exc.code, str(exc))
        audit(conn, principal=principal, action="read", object_id=audio_id, decision="allow")
        return {
            "audio_id": row["audio_id"], "lifecycle": row["lifecycle"],
            "media_mime": row["media_mime"], "bytes": row["bytes"],
            "sha256": row["sha256"], "created_at": row["created_at"],
        }

    @app.delete("/v1/audio/{audio_id}")
    async def audio_delete(audio_id: str, request: Request):
        principal = await principal_of(request)
        if conn is None or assets is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        try:
            state = assets.request_delete(audio_id, principal=principal)
        except AssetError as exc:
            audit(conn, principal=principal, action="delete", object_id=audio_id, decision="deny")
            return _error_response(403, exc.code, str(exc))
        audit(conn, principal=principal, action="delete", object_id=audio_id, decision="allow")
        return {"audio_id": audio_id, "lifecycle": state}

    # ── 分析 ────────────────────────────────────────────────────────────
    @app.post("/v1/analyses")
    async def create_analysis(body: AnalyzeIn, request: Request):
        principal = await principal_of(request)
        if conn is None or analyses is None or ingest is None or variants is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        if not cfg.enabled("offline_analysis"):
            audit(conn, principal=principal, action="analyze", object_id=body.audio_ref, decision="deny")
            return _error_response(503, "SERVICE_DISABLED", "offline_analysis flag is off", retryable=True)
        if body.profile not in PROFILES:
            return _error_response(422, "INVALID_PROFILE", f"unknown profile: {body.profile}")
        try:
            audio_id = ingest.resolve(body.audio_ref, principal=principal)
            range_ = None
            if body.range_ms is not None:
                if (len(body.range_ms) != 2 or body.range_ms[0] < 0
                        or body.range_ms[1] <= body.range_ms[0]):
                    return _error_response(422, "INVALID_RANGE", "range_ms must be [start,end) with 0<=start<end")
                range_ = {"start_ms": body.range_ms[0], "end_ms": body.range_ms[1]}
            created = analyses.create(
                audio_id=audio_id, profile=body.profile, requested_by=principal,
                idempotency_key=body.idempotency_key, range_=range_,
            )
            audit(conn, principal=principal, action="analyze", object_id=created["analysis_id"], decision="allow")
        except RefError as exc:
            audit(conn, principal=principal, action="analyze", object_id=body.audio_ref, decision="deny")
            return _error_response(exc.http_status, exc.code, str(exc))
        except AnalysisError as exc:
            audit(conn, principal=principal, action="analyze", object_id=body.audio_ref, decision="deny")
            return _error_response(exc.http_status, exc.code, str(exc))
        if body.wait_ms > 0:
            # R6′（2026-10-23）：wait_ms 恢复真实等待上限——执行器线程在跑时轮询至期限，
            # 到限未完成=返回票据（不再内联跑完 DSP）；无执行器（测试/MCP stdio）维持同步内联。
            from oido.workers.executor import EXECUTOR_ACTIVE
            if EXECUTOR_ACTIVE:
                import time as _time
                deadline = _time.monotonic() + body.wait_ms / 1000.0
                while _time.monotonic() < deadline:
                    if analyses.get(created["analysis_id"], principal=principal)["status"] in (
                        "succeeded", "partial", "failed", "cancelled",
                    ):
                        break
                    _time.sleep(0.05)
            else:
                analysis_worker.process_one(
                    conn=conn, analyses=analyses, variants=variants, ingest=ingest,
                    objects=objects, analysis_id=created["analysis_id"],
                )
            run = analyses.get(created["analysis_id"], principal=principal)
            if run["status"] in ("succeeded", "partial"):
                facts = analysis_worker.load_facts(conn, created["analysis_id"])
                if facts is not None:
                    return projections.evidence_overview(
                        analysis_id=created["analysis_id"], revision=run["revision"], facts=facts)
            if run["status"] == "failed":
                return _error_response(422, "ANALYSIS_FAILED", run.get("error_code") or "pipeline_failed")
        state = "running" if analyses.get(created["analysis_id"], principal=principal)["status"] == "running" else "queued"
        return projections.analysis_ticket(created["analysis_id"], state)

    @app.post("/v1/analyses/{analysis_id}/cancel")
    async def cancel_analysis(analysis_id: str, request: Request):
        """R6′（2026-10-23）：取消——DB fence（status→cancelled+fence+1），迟到提交被拒；
        已进入的计算段不可抢占（如实等待自然结束）。取消后新阶段不再推进。"""
        principal = await principal_of(request)
        if conn is None or analyses is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        try:
            run = analyses.get(analysis_id, principal=principal)
        except AnalysisError as exc:
            return _error_response(exc.http_status, exc.code, str(exc))
        if run["status"] in ("succeeded", "partial", "failed", "cancelled"):
            return {"analysis_id": analysis_id, "status": run["status"], "cancelled": run["status"] == "cancelled"}
        analyses.cancel(analysis_id, principal=principal)
        audit(conn, principal=principal, action="cancel", object_id=analysis_id, decision="allow")
        return {"analysis_id": analysis_id, "status": "cancelled", "cancelled": True}

    @app.get("/v1/analyses/{analysis_id}")
    async def get_overview(analysis_id: str, request: Request):
        principal = await principal_of(request)
        if conn is None or analyses is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        try:
            run = analyses.get(analysis_id, principal=principal)
        except AnalysisError as exc:
            return _error_response(exc.http_status, exc.code, str(exc))
        if run["status"] not in ("succeeded", "partial"):
            return _error_response(409, "NOT_READY_YET", f"analysis is {run['status']}", retryable=True)
        facts = analysis_worker.load_facts(conn, analysis_id)
        if facts is None:
            return _error_response(500, "FACTS_MISSING", "terminal run has no facts artifact")
        audit(conn, principal=principal, action="get", object_id=analysis_id, decision="allow")
        return projections.evidence_overview(
            analysis_id=analysis_id, revision=run["revision"], facts=facts)

    @app.get("/v1/analyses/{analysis_id}/prosody")
    async def get_page(analysis_id: str, request: Request, cursor: Optional[str] = None):
        principal = await principal_of(request)
        if conn is None or analyses is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        try:
            run = analyses.get(analysis_id, principal=principal)
        except AnalysisError as exc:
            return _error_response(exc.http_status, exc.code, str(exc))
        if run["status"] not in ("succeeded", "partial"):
            return _error_response(409, "NOT_READY_YET", f"analysis is {run['status']}", retryable=True)
        facts = analysis_worker.load_facts(conn, analysis_id)
        if facts is None:
            return _error_response(500, "FACTS_MISSING", "terminal run has no facts artifact")
        audit(conn, principal=principal, action="get_page", object_id=analysis_id, decision="allow")
        try:
            return projections.prosody_page(
                analysis_id=analysis_id, revision=run["revision"], facts=facts, cursor=cursor)
        except ProjectionError as exc:
            return _error_response(_projection_status(exc.code), exc.code, str(exc))

    # ── view / find / compare（JOINT-VOICE 收尾三工具） ──────────────────
    @app.get("/v1/analyses/{analysis_id}/view")
    async def get_view(analysis_id: str, request: Request, view: str = "overview", range_ms: Optional[str] = None):
        from fastapi import Response as FastResponse
        from oido.dsp.render import RenderError

        principal = await principal_of(request)
        if views is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        if not cfg.enabled("offline_analysis"):
            return _error_response(503, "SERVICE_DISABLED", "offline_analysis flag is off", retryable=True)
        try:
            run = analyses.get(analysis_id, principal=principal)
        except AnalysisError as exc:
            return _error_response(exc.http_status, exc.code, str(exc))
        rng = None
        if range_ms:
            parts = range_ms.split(",")
            try:
                rng = [int(parts[0]), int(parts[1])] if len(parts) == 2 else None
                if rng is not None and not (0 <= rng[0] < rng[1]):
                    rng = None
            except ValueError:
                rng = None
            if rng is None:
                return _error_response(422, "INVALID_RANGE", "range_ms must be start,end")
        try:
            out = views.render(analysis_id=analysis_id, view=view, range_ms=rng)
        except RenderError as exc:
            return _error_response(422, exc.reason_code.upper(), str(exc))
        audit(conn, principal=principal, action="view", object_id=analysis_id, decision="allow")
        return FastResponse(out["png"], media_type="image/png", headers={
            "X-Oido-Artifact-Id": out["artifact_id"], "X-Oido-View": view})

    @app.post("/v1/find")
    async def find_audio(body: FindIn, request: Request):
        principal = await principal_of(request)
        if finder is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        if not cfg.enabled("offline_analysis"):
            return _error_response(503, "SERVICE_DISABLED", "offline_analysis flag is off", retryable=True)
        try:
            out = finder.find(
                principal=principal, audio_ref=body.audio_ref,
                query=body.query, mode=body.mode, limit=body.limit,
                cursor=body.cursor)
        except AnalysisError as exc:
            audit(conn, principal=principal, action="find", object_id=body.audio_ref, decision="deny")
            return _error_response(exc.http_status, exc.code, str(exc))
        except RefError as exc:
            return _error_response(exc.http_status, exc.code, str(exc))
        except ProjectionError as exc:
            return _error_response(422, exc.code, "result exceeds budget; use a shorter query or smaller limit")
        except (TypeError, ValueError):
            return _error_response(422, "INVALID_INPUT", "bad find request")
        audit(conn, principal=principal, action="find", object_id=out["audio_ref"], decision="allow")
        return out

    @app.post("/v1/compare")
    async def compare_audio(body: CompareIn, request: Request):
        principal = await principal_of(request)
        if comparer is None:
            return _error_response(503, "NOT_READY", "storage not attached")
        if not cfg.enabled("offline_analysis"):
            return _error_response(503, "SERVICE_DISABLED", "offline analysis is disabled", retryable=True)
        left, right = body.left, body.right
        try:
            out = comparer.compare(
                principal=principal, left=left, right=right, metrics=body.metrics)
        except AnalysisError as exc:
            audit(conn, principal=principal, action="compare",
                  object_id=str(left.get("audio_ref", "-")), decision="deny")
            return _error_response(exc.http_status, exc.code, str(exc))
        except RefError as exc:
            return _error_response(exc.http_status, exc.code, str(exc))
        audit(conn, principal=principal, action="compare",
              object_id=f"{left.get('audio_ref')}|{right.get('audio_ref')}", decision="allow")
        return out

    return app


def _projection_status(code: str) -> int:
    if code in ("NO_CHAR_ALIGNMENT",):
        return 409
    if code in ("INVALID_CURSOR",):
        return 422
    if code == "SNAPSHOT_CHANGED":
        return 409
    return 422
