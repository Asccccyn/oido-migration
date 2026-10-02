"""HTTP API v0.1：health/live、health/ready、/v1/capabilities（EXEC §4.1 子集）。

只绑定 loopback；feature flags 默认全关。
更完整的 /v1/audio、/v1/analyses 等 REST 在 WP02+ 随 ingest 打开。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from oido import providers
from oido.config import OidoConfig


def create_app(config: OidoConfig | None = None, conn: sqlite3.Connection | None = None) -> FastAPI:
    cfg = config or OidoConfig.from_env()
    app = FastAPI(title="oido", version="0.1.0")
    _state = {"conn": conn}

    class ErrorBody(BaseModel):
        code: str
        message: str
        retryable: bool = False

    @app.get("/health/live")
    def live() -> dict:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> dict:
        # R8/F12：按真实迁移版本集合 + 全部业务关键表核对，不拿"列存在"当迁移成功代理
        required_tables = (
            "audio_assets", "audio_origins", "audio_variants", "analysis_runs",
            "analysis_stages", "evidence_items", "artifacts", "events_outbox",
            "idempotency_records", "deletion_tombstones", "acoustic_baselines",
            "access_audit",
        )
        db_ok, reason = False, "no_storage"
        if _state["conn"] is not None:
            try:
                from oido.storage.db import expected_versions

                applied = {
                    r["version"] for r in _state["conn"].execute(
                        "SELECT version FROM schema_migrations"
                    )
                }
                expected = expected_versions(cfg.migrations_dir)
                if not applied:
                    reason = "migrations_empty"
                elif applied != expected:
                    reason = f"migrations_mismatch:missing={sorted(expected - applied)}"
                else:
                    present = {
                        r["name"] for r in _state["conn"].execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    missing = [t for t in required_tables if t not in present]
                    if missing:
                        reason = f"missing_tables:{','.join(missing)}"
                    else:
                        # O1：恢复关键列检查——版本账本与表都在，不代表列没被
                        # 错误还原弄丢（结构损坏时代价低，值得多这一道）
                        cols = {
                            r["name"] for r in _state["conn"].execute(
                                "PRAGMA table_info(analysis_runs)"
                            )
                        }
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
        # 如实报告：not_configured 不是 available（EXEC §4.1）
        return {"providers": providers.capabilities(), "flags": cfg.flags}

    @app.get("/v1/audio/{audio_id}")
    def audio(audio_id: str) -> dict:
        if _state["conn"] is None:
            raise HTTPException(503, {"code": "not_ready", "message": "storage not attached"})
        raise HTTPException(501, {"code": "not_implemented",
                                  "message": "audio metadata endpoint lands with WP02 ingest"})

    return app
