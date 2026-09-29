"""SQLite 打开与迁移（ARCH §6：初期不引入 Redis/向量库/对象存储集群）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2].parents[0] / "migrations"


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：API 线程池会跨线程使用；调用方自行串行化写
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[int]:
    """按版本顺序应用迁移；已应用跳过。返回本次应用的版本列表。"""
    mdir = migrations_dir or _MIGRATIONS_DIR
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    ran: list[int] = []
    for sql_file in sorted(mdir.glob("*.sql")):
        version = int(sql_file.name.split("_")[0])
        if version in applied:
            continue
        with open(sql_file) as f:
            conn.executescript(f.read())
        from oido.domain.ids import utc_now_rfc3339

        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, utc_now_rfc3339()),
        )
        conn.commit()
        ran.append(version)
    return ran
