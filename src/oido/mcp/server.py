"""最小 MCP stdio 服务器（联合施工 P3：HTTP/MCP 共用业务服务）。

newline-delimited JSON-RPC；不引 MCP SDK 依赖，只实现 initialize/tools/list/
tools/call/ping。与 HTTP API 走完全相同的 service 层（单一业务真源）。
工具清单来自 contracts/mcp-tools.v2.json；view/find/compare 如实返回
NOT_IMPLEMENTED，不拿空壳报完成。旧 v1 audio_id 参数单层映射。
输出单份 JSON 文本（不 text+structuredContent 双份重复）。

启动：OIDO_DATA_ROOT=... OIDO_AUTH_TOKEN=... python -m oido.mcp.server
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from oido import providers
from oido.config import OidoConfig
from oido.prosody import projections
from oido.service.analyses import AnalysisError, AnalysisService
from oido.service.assets import AssetService
from oido.service.ingest import OWNER_ORIGIN_KIND, IngestService, RefError
from oido.service.variants import VariantService
from oido.storage.db import migrate, open_db
from oido.storage.objects import ObjectStore
from oido.workers import analysis_worker

_CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "mcp-tools.v2.json"
IMPLEMENTED = {"oido_capabilities", "oido_analyze", "oido_get"}
NOT_IMPLEMENTED = {"oido_view", "oido_find", "oido_compare"}
PROTOCOL_VERSION = "2024-11-05"


class _Ctx:
    def __init__(self) -> None:
        self.cfg = OidoConfig.from_env()
        root = Path(os.environ.get("OIDO_DATA_ROOT", self.cfg.data_root))
        self.cfg.data_root = root
        self.conn = open_db(root / "metadata" / "oido.sqlite3")
        migrate(self.conn)
        self.objects = ObjectStore(root)
        self.assets = AssetService(self.conn, self.objects)
        self.analyses = AnalysisService(self.conn)
        self.variants = VariantService(self.conn, self.objects)
        self.ingest = IngestService(self.conn, self.assets)


def _tool_list() -> list[dict]:
    contract = json.loads(_CONTRACT.read_text())
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": t["input"],
        }
        for t in contract["tools"]
    ]


def _resolve_ref_arg(args: dict, ctx: _Ctx) -> str:
    """v2 audio_ref 为规范参数；v1 audio_id（^aud_）单层兼容映射。"""
    ref = args.get("audio_ref")
    if isinstance(ref, str) and ref:
        return ref
    legacy = args.get("audio_id")
    if isinstance(legacy, str) and legacy.startswith("aud_"):
        # 内部 ID → 反查 owner:external；查不到就用内部 owner 命名空间直映射
        row = ctx.conn.execute(
            "SELECT origin_kind, external_id FROM audio_origins WHERE audio_id = ?",
            (legacy,),
        ).fetchone()
        if row is not None:
            owner = {v: k for k, v in OWNER_ORIGIN_KIND.items()}.get(row["origin_kind"])
            if owner and row["external_id"]:
                return f"{owner}:{row['external_id']}"
        return f"oido:{legacy}"
    raise RefError("invalid_audio_ref", "audio_ref (or legacy audio_id) is required")


def _call_tool(name: str, args: dict, ctx: _Ctx) -> dict:
    principal = "mcp"
    if name == "oido_capabilities":
        cap = args.get("capability")
        caps = providers.capabilities()
        if isinstance(cap, str) and cap:
            caps = [c for c in caps if c.get("name") == cap]
        return {"providers": caps, "flags": ctx.cfg.flags}
    if name == "oido_analyze":
        ref = _resolve_ref_arg(args, ctx)
        profile = args.get("profile", "prosody")
        audio_id = ctx.ingest.resolve(ref, principal="est")  # 资产命名空间按 owner 校验
        created = ctx.analyses.create(
            audio_id=audio_id, profile=profile, requested_by="est",
            idempotency_key=args.get("idempotency_key"),
        )
        # 有界同步等待：单次内联执行，完成即回概览，未完成回票据
        analysis_worker.process_one(
            conn=ctx.conn, analyses=ctx.analyses, variants=ctx.variants,
            ingest=ctx.ingest, objects=ctx.objects, analysis_id=created["analysis_id"],
        )
        run = ctx.analyses.get(created["analysis_id"], principal="est")
        if run["status"] in ("succeeded", "partial"):
            facts = analysis_worker.load_facts(ctx.conn, created["analysis_id"])
            if facts is not None:
                return projections.evidence_overview(
                    analysis_id=created["analysis_id"], revision=run["revision"], facts=facts)
        if run["status"] == "failed":
            return projections.error_body("ANALYSIS_FAILED", run.get("error_code") or "pipeline_failed")
        return projections.analysis_ticket(
            created["analysis_id"], "running" if run["status"] == "running" else "queued")
    if name == "oido_get":
        analysis_id = args.get("analysis_id")
        if not isinstance(analysis_id, str) or not analysis_id:
            raise RefError("invalid_input", "analysis_id is required")
        run = ctx.analyses.get(analysis_id, principal="est")
        if run["status"] not in ("succeeded", "partial"):
            return projections.error_body("NOT_READY_YET", f"analysis is {run['status']}", retryable=True)
        facts = analysis_worker.load_facts(ctx.conn, analysis_id)
        if facts is None:
            return projections.error_body("FACTS_MISSING", "terminal run has no facts artifact")
        if args.get("detail") == "prosody_page":
            try:
                return projections.prosody_page(
                    analysis_id=analysis_id, revision=run["revision"], facts=facts,
                    cursor=args.get("cursor"))
            except projections.ProjectionError as exc:
                return projections.error_body(exc.code, str(exc))
        return projections.evidence_overview(
            analysis_id=analysis_id, revision=run["revision"], facts=facts)
    if name in NOT_IMPLEMENTED:
        # 如实未实装：走 isError 通道（工具级失败），不拿空成功冒充
        raise RefError("not_implemented", f"{name} is not implemented in this round")
    raise RefError("unknown_tool", f"unknown tool: {name}")


def handle_message(msg: dict, ctx: Optional[_Ctx] = None) -> Optional[dict]:
    """单条消息处理（测试可直接调）。返回响应 dict 或 None（通知不回）。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "oido", "version": "0.2.0"},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tool_list()}}
    if method == "tools/call":
        try:
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if ctx is None:
                ctx = _Ctx()
            output = _call_tool(name, args, ctx)
            text = json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }}
        except (RefError, AnalysisError, projections.ProjectionError) as exc:
            code = getattr(exc, "code", "TOOL_ERROR").upper().replace("-", "_")
            body = projections.error_body(code, str(exc)[:120])
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
                "isError": True,
            }}
        except Exception as exc:  # noqa: BLE001
            body = projections.error_body("TOOL_ERROR", str(exc)[:120])
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
                "isError": True,
            }}
    if msg_id is not None:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}}
    return None


def main() -> None:
    ctx: Optional[_Ctx] = None  # 惰性初始化：initialize 后才碰数据库
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            print(json.dumps(resp), flush=True)
            continue
        if msg.get("method") == "initialize" and ctx is None:
            ctx = _Ctx()
        resp = handle_message(msg, ctx)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
