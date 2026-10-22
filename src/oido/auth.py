"""认证与访问审计（联合施工 09：每次调用检查，不因带了 ID 就放行）。

最小口径：单一内部 bearer token（env 注入，绝不落日志/响应）绑定一个 principal。
capability/health 匿名只读；其余 /v1/* 全部要求 token。
每次 allow/deny 落 access_audit（只记 principal/action/object_id/decision，不记正文）。
"""

from __future__ import annotations

import hmac
import sqlite3
from typing import Optional

from oido.domain.ids import utc_now_rfc3339

# 需要认证的动作名单之外的匿名只读面
ANONYMOUS_PATHS = ("/health/live", "/health/ready", "/v1/capabilities")


class AuthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def token_from_env() -> Optional[str]:
    import os

    return os.environ.get("OIDO_AUTH_TOKEN") or None


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def verify_bearer(header_value: Optional[str], expected_token: Optional[str]) -> str:
    """校验 Authorization: Bearer <token>。返回 principal（当前单租户=token 绑定名）。"""
    if not expected_token:
        # 未配置 token：显式拒绝而不是"无鉴权放行"——服务本就默认不监听
        raise AuthError("auth_not_configured", "server has no auth token configured")
    if not header_value or not header_value.startswith("Bearer "):
        raise AuthError("unauthorized", "missing bearer token")
    provided = header_value[len("Bearer "):].strip()
    if not provided or not _constant_time_eq(provided, expected_token):
        raise AuthError("unauthorized", "invalid token")
    principal = "est"  # 本轮唯一内部调用方；多租户时改为 token→principal 映射表
    return principal


def audit(conn: sqlite3.Connection, *, principal: str, action: str, object_id: str, decision: str) -> None:
    """落一行访问审计。失败不吞主流程语义，但也不静默：异常上抛由调用方决定。"""
    if decision not in ("allow", "deny"):
        raise ValueError(f"bad audit decision: {decision!r}")
    conn.execute(
        "INSERT INTO access_audit (principal, action, object_id, decision, at) VALUES (?, ?, ?, ?, ?)",
        (principal, action, object_id, decision, utc_now_rfc3339()),
    )
    conn.commit()
