"""oído 服务启动入口（JOINT-VOICE-20261022 生产接线用）。

环境变量（全部有缺省）：
  OIDO_BIND_HOST（默认 127.0.0.1）
  OIDO_BIND_PORT（默认 0=不监听；生产给 8791）
  OIDO_DATA_ROOT（默认 ~/Data/oido）
  OIDO_AUTH_TOKEN（必需：Bearer 令牌；生产从 secrets 文件读进环境，不落 plist）
  OIDO_FLAG_INGEST=1 OIDO_FLAG_OFFLINE_ANALYSIS=1（分面开关）
启动：python -m oido   （经 launchd 包装脚本注入 env）
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from oido.api.http import create_app
    from oido.config import OidoConfig
    from oido.storage.db import migrate, open_db

    cfg = OidoConfig.from_env()
    host = os.environ.get("OIDO_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("OIDO_BIND_PORT", "0") or "0")
    if port <= 0:
        raise SystemExit("OIDO_BIND_PORT 未配置：拒绝以不监听状态作为服务启动")
    if not os.environ.get("OIDO_AUTH_TOKEN"):
        raise SystemExit("OIDO_AUTH_TOKEN 未配置：无鉴权不放行")
    cfg.ensure_dirs()
    db_path = cfg.data_root / "metadata" / "oido.sqlite3"
    conn = open_db(db_path)
    migrate(conn)
    # R6′（2026-10-23）：分析任务串行执行器——独立线程+独立连接（此前 wait_ms=0 只落
    # queued 无消费者；正值内联跑完 DSP 不守期限）。EXECUTOR_ACTIVE=True 后 HTTP wait_ms
    # 走轮询语义。
    import threading
    from oido.workers.executor import run_forever
    threading.Thread(target=run_forever, args=(db_path,), daemon=True, name="oido-executor").start()
    uvicorn.run(create_app(config=cfg, conn=conn), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
