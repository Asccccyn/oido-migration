"""状态机与原因码（OIDO-EXEC §3.2）。

skipped ≠ 阴性；partial 必须带缺失能力与原因；
迟到 worker 靠 fencing token 失效，旧结果不得复活为 active。
"""

from __future__ import annotations

from typing import Iterable

ANALYSIS_JOB_STATES = ("queued", "running", "succeeded", "partial", "failed", "cancelled")
STAGE_STATES = ("pending", "running", "succeeded", "partial", "failed", "skipped")
ASSET_LIFECYCLE_STATES = ("active", "deletion_requested", "tombstoned", "purged")
STREAM_STATES = ("open", "draining", "finalized", "aborted", "failed")
LINK_STATES = ("pending", "active", "revoking", "revoked", "failed")

REASON_CODES = (
    "policy_denied",
    "transcript_changed",  # RE-14e（2026-10-24）：排队后转写变化——拒绝执行（非静默换输入）
    "provider_not_configured",
    "unsupported",
    "insufficient_signal",
    "unsupported_language",
    "alignment_failed",
    "resource_exhausted",
    "deadline_exceeded",
    "invalid_media",
    "source_unavailable",
    "input_gap",
    "dependency_failed",
    "client_transport_unverified",
)

# 允许的状态迁移（缺省即非法；防 "cancelled → running" 复活）
_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"succeeded", "partial", "failed", "cancelled"}),
    # 终态
    "succeeded": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

_STAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "skipped", "failed"}),
    "running": frozenset({"succeeded", "partial", "failed", "skipped"}),
    "succeeded": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "skipped": frozenset(),
}

_ASSET_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"deletion_requested"}),
    "deletion_requested": frozenset({"tombstoned", "active"}),  # active=删除被拒/撤销
    "tombstoned": frozenset({"purged"}),
    "purged": frozenset(),
}


class IllegalTransition(ValueError):
    def __init__(self, kind: str, frm: str, to: str):
        super().__init__(f"illegal {kind} transition: {frm} -> {to}")
        self.kind, self.frm, self.to = kind, frm, to


def _check(table: dict[str, frozenset[str]], states: Iterable[str], kind: str, frm: str, to: str) -> str:
    if frm not in table or to not in table:
        raise IllegalTransition(kind, frm, to)
    if to not in table[frm]:
        raise IllegalTransition(kind, frm, to)
    return to


def job_transition(frm: str, to: str) -> str:
    return _check(_JOB_TRANSITIONS, ANALYSIS_JOB_STATES, "analysis_job", frm, to)


def stage_transition(frm: str, to: str) -> str:
    return _check(_STAGE_TRANSITIONS, STAGE_STATES, "stage", frm, to)


def asset_transition(frm: str, to: str) -> str:
    return _check(_ASSET_TRANSITIONS, ASSET_LIFECYCLE_STATES, "asset", frm, to)


def is_terminal_job(state: str) -> bool:
    return state in ("succeeded", "partial", "failed", "cancelled")


def require_reason(reason_code: str) -> str:
    if reason_code not in REASON_CODES:
        raise ValueError(f"unknown reason_code: {reason_code!r}")
    return reason_code
