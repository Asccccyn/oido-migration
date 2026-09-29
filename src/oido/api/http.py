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
        db_ok = False
        if _state["conn"] is not None:
            try:
                _state["conn"].execute("SELECT 1 FROM schema_migrations LIMIT 1")
                db_ok = True
            except sqlite3.Error:
                db_ok = False
        # 没挂存储就是 degraded，不放水（自审计勘误：此前 conn 为 None 也报 ready）
        return {"status": "ready" if db_ok else "degraded", "db": db_ok,
                "flags": cfg.flags}

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
