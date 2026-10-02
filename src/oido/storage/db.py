"""SQLite 打开与迁移（ARCH §6：初期不引入 Redis/向量库/对象存储集群）。

R04：DDL 与版本登记必须在**同一事务**内完成——中断后重试不再 duplicate column。
对历史遗留的半迁移状态（列已加、版本未记）提供可验证恢复：逐条检测
ALTER 已生效则跳过该条并照常登记版本；检测不出的错误照常抛出。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2].parents[0] / "migrations"

_ALTER_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：API 线程池会跨线程使用；调用方自行串行化写
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _split_statements(sql: str) -> list[str]:
    """按行尾分号切分；跳过纯注释块。我们的迁移文件不含字符串内分号。"""
    stmts: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        buf.append(line)
        if line.rstrip().endswith(";"):
            chunk = "\n".join(buf).strip()
            if any(l.strip() and not l.strip().startswith("--") for l in buf):
                stmts.append(chunk)
            buf = []
    tail = "\n".join(buf).strip()
    if tail and any(l.strip() and not l.strip().startswith("--") for l in buf):
        stmts.append(tail)
    return stmts


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def expected_versions(migrations_dir: Path | None = None) -> set[int]:
    """迁移目录里应当存在的全部版本号（R08：ready 按此核对，不拿列存在当代理）。"""
    mdir = migrations_dir or _MIGRATIONS_DIR
    return {int(p.name.split("_")[0]) for p in mdir.glob("*.sql")}


def migrate(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[int]:
    """按版本顺序应用迁移；DDL 与版本 INSERT 同事务（显式 BEGIN/COMMIT）。

    R04 根因：Python sqlite3 默认隔离模式下 DDL 不自动开事务，
    依赖隐式事务的 rollback 罩不住 ALTER——必须显式控制事务边界。
    """
    mdir = migrations_dir or _MIGRATIONS_DIR
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    ran: list[int] = []
    from oido.domain.ids import utc_now_rfc3339

    orig_isolation = conn.isolation_level
    conn.isolation_level = None            # 切到显式事务模式
    try:
        for sql_file in sorted(mdir.glob("*.sql")):
            version = int(sql_file.name.split("_")[0])
            # O2：每个版本都重读账本（并发迁移者可能已在此期间完成）
            if conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone():
                continue
            statements = _split_statements(sql_file.read_text())
            conn.execute("BEGIN")
            try:
                for stmt in statements:
                    alter = _ALTER_RE.search(stmt)
                    if alter and _column_exists(conn, alter.group(1), alter.group(2)):
                        # 历史半迁移恢复路径：列已在（版本未记）→ 跳过该条 DDL，
                        # 版本仍会在本事务末尾登记，恢复闭环
                        continue
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utc_now_rfc3339()),
                )
                conn.execute("COMMIT")
                ran.append(version)
            except sqlite3.IntegrityError:
                # O2：并发迁移者先完成了同一版本——假失败不得让启动崩掉。
                # 验证版本确实已登记则视为完成，否则才是真错误
                conn.execute("ROLLBACK")
                if conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone():
                    continue
                raise
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.isolation_level = orig_isolation
    return ran
