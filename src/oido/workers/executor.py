"""R6′（2026-10-23，她批 R6P 方案）：分析任务串行执行器。

生产由 __main__ 以独立线程启动（uvicorn 之外、进程内单写者）：
  - 领取：BEGIN IMMEDIATE 下取一条 queued（或租约过期的 running）→ 置 running+租约；
  - 执行：复用 analysis_worker.process_one（阶段间设 fence 检查点，取消后不再推进新阶段；
    已进入的 ffmpeg/pYIN 计算段不可抢占——如实等待其自然结束，不宣称 GPU 抢占）；
  - 崩溃/重启：租约过期后重新领取（fence 保证迟到提交被拒）。
测试与 MCP stdio 路径不启动线程：HTTP/MCP 同步行为维持（EXECUTOR_ACTIVE=False）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

EXECUTOR_ACTIVE = False  # __main__ 启动线程时置 True（HTTP wait_ms 据此选轮询/内联）


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_rfc3339_minus(seconds: float) -> str:
    # RE-14（2026-10-24 返修）：租约过期判定的时间下界（claim_next 依赖；此前未定义——直接调用即 NameError）
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def claim_next(conn: sqlite3.Connection, owner: str, lease_seconds: float = 120.0) -> Optional[str]:
    """语义兼容保留：直接复用 process_one 的领取（queued/租约过期）。"""
    row = conn.execute(
        "SELECT analysis_id FROM analysis_runs WHERE status='queued' "
        "OR (status='running' AND (lease_expires_at IS NULL OR lease_expires_at < ?)) "
        "ORDER BY created_at LIMIT 1",
        (_now_rfc3339_minus(lease_seconds),),
    ).fetchone()
    return None if row is None else row["analysis_id"]


def cancelled(conn: sqlite3.Connection, analysis_id: str) -> bool:
    row = conn.execute("SELECT status FROM analysis_runs WHERE analysis_id=?", (analysis_id,)).fetchone()
    return row is None or row["status"] == "cancelled"


def run_forever(db_path, poll_interval: float = 0.5) -> None:
    """生产线程体：独立连接（不动 HTTP 连接），单线程串行领任务。"""
    global EXECUTOR_ACTIVE
    from oido.config import OidoConfig
    from oido.storage.db import open_db
    from oido.storage.objects import ObjectStore
    from oido.service.analyses import AnalysisService
    from oido.service.assets import AssetService
    from oido.service.ingest import IngestService
    from oido.service.variants import VariantService
    from oido.workers import analysis_worker

    cfg = OidoConfig.from_env()
    conn = open_db(db_path)
    owner = f"executor-{threading.get_ident()}"
    EXECUTOR_ACTIVE = True
    objects = ObjectStore(cfg.data_root)
    assets = AssetService(conn, objects)
    analyses = AnalysisService(conn)
    variants = VariantService(conn, objects)
    ingest = IngestService(conn, assets)
    try:
        while True:
            try:
                # analysis_id=None：process_one 自带领取（queued/租约过期，fence+lease 复用）
                did = analysis_worker.process_one(
                    conn=conn, analyses=analyses, variants=variants, ingest=ingest,
                    objects=objects, worker_name=owner,
                )
            except Exception:
                did = None  # 内部已按 fence 终态化；异常不杀线程
            if did is None:
                time.sleep(poll_interval)
    finally:
        EXECUTOR_ACTIVE = False
        conn.close()
