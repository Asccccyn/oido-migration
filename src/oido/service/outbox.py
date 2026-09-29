"""事务 outbox：至少一次交付 + 幂等消费（ARCH §10.3，E16）。"""

from __future__ import annotations

import json
import sqlite3

from oido.domain.ids import utc_now_rfc3339


class Outbox:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def emit(self, topic: str, key: str, payload: dict) -> int:
        """与业务写入同事务调用；key 至少含 资产ID+输入hash+pipeline版本+profile。"""
        cur = self.conn.execute(
            """INSERT INTO events_outbox (topic, key, payload, created_at) VALUES (?, ?, ?, ?)""",
            (topic, key, json.dumps(payload, ensure_ascii=False, sort_keys=True), utc_now_rfc3339()),
        )
        return cur.lastrowid

    def pending(self, limit: int = 32) -> list[dict]:
        rows = self.conn.execute(
            """SELECT outbox_id, topic, key, payload, attempts FROM events_outbox
               WHERE delivered_at IS NULL ORDER BY outbox_id LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def mark_delivered(self, outbox_id: int) -> None:
        self.conn.execute(
            "UPDATE events_outbox SET delivered_at = ? WHERE outbox_id = ?",
            (utc_now_rfc3339(), outbox_id),
        )
        self.conn.commit()

    def is_duplicate(self, topic: str, key: str) -> bool:
        """消费方幂等检查：同 topic+key 已交付即重复。"""
        row = self.conn.execute(
            "SELECT 1 FROM events_outbox WHERE topic = ? AND key = ? AND delivered_at IS NOT NULL",
            (topic, key),
        ).fetchone()
        return row is not None
