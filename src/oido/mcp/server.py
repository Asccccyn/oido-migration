"""最小 MCP stdio 服务器（联合施工 P3：HTTP/MCP 共用业务服务）。

newline-delimited JSON-RPC；不引 MCP SDK 依赖，只实现 initialize/tools/list/
tools/call/ping。与 HTTP API 走完全相同的 service 层（单一业务真源）。
工具清单与入参校验来自 contracts/mcp-tools.v2.json。view 返回原生图片内容。
旧 v1 audio_id 参数单层映射。
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
        from oido.service.compare import CompareService
        from oido.service.find import FindService
        from oido.service.views import ViewService
        self.views = ViewService(self.conn, self.variants, self.objects)
        self.finder = FindService(self.analyses, self.ingest)
        self.comparer = CompareService(self.analyses, self.ingest)


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
    if name == "oido_capabilities":
        cap = args.get("capability")
        caps = providers.capabilities()
        if isinstance(cap, str) and cap:
            caps = [c for c in caps if c.get("name") == cap]
        return {"providers": [{k: v for k, v in c.items() if k in ("name", "category", "status", "version") and v is not None}
                              for c in caps],
                "analysis": "enabled" if ctx.cfg.enabled("offline_analysis") else "disabled"}
    if name in ("oido_analyze", "oido_view", "oido_find", "oido_compare") and not ctx.cfg.enabled("offline_analysis"):
        raise RefError("service_disabled", "offline analysis is disabled", 503)
    if name == "oido_analyze":
        ref = _resolve_ref_arg(args, ctx)
        profile = args.get("profile", "prosody")
        audio_id = ctx.ingest.resolve(ref, principal="est")  # 资产命名空间按 owner 校验
        created = ctx.analyses.create(
            audio_id=audio_id, profile=profile, requested_by="est",
            idempotency_key=args.get("idempotency_key"),
            range_={"start_ms": args["range_ms"][0], "end_ms": args["range_ms"][1]} if args.get("range_ms") is not None else None,
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
        # JV-10（联合审计 2026-10-26）：cancelled 是终态（与 HTTP 合同一致）——
        # 此前落入 else 被当 queued 重放，模型会永远轮询已取消任务
        if run["status"] == "cancelled":
            return projections.error_body("ANALYSIS_CANCELLED", "analysis was cancelled", retryable=False)
        return projections.analysis_ticket(
            created["analysis_id"], "running" if run["status"] == "running" else "queued")
    if name == "oido_get":
        analysis_id = args.get("analysis_id")
        if not isinstance(analysis_id, str) or not analysis_id:
            raise RefError("invalid_input", "analysis_id is required")
        run = ctx.analyses.get(analysis_id, principal="est")
        if args.get("revision") is not None and args["revision"] != run["revision"]:
            raise RefError("snapshot_changed", "requested analysis revision is unavailable", 409)
        if run["status"] == "failed":
            return projections.error_body("ANALYSIS_FAILED", "analysis failed (terminal)", retryable=False)
        if run["status"] == "cancelled":
            return projections.error_body("ANALYSIS_CANCELLED", "analysis was cancelled", retryable=False)
        if run["status"] not in ("succeeded", "partial"):
            return projections.error_body("NOT_READY_YET", f"analysis is {run['status']}", retryable=True)
        facts = analysis_worker.load_facts(ctx.conn, analysis_id)
        if facts is None:
            return projections.error_body("FACTS_MISSING", "terminal run has no facts artifact")
        if args.get("range_ms") is not None:
            stored_range = facts.get("range_ms") or [0, round(facts["duration_s"] * 1000)]
            if args["range_ms"] != stored_range:
                raise RefError("range_requires_analysis", "use oido_analyze with inspect_range for a different range", 422)
        if args.get("detail") == "prosody_page":
            try:
                return projections.prosody_page(
                    analysis_id=analysis_id, revision=run["revision"], facts=facts,
                    cursor=args.get("cursor"))
            except projections.ProjectionError as exc:
                return projections.error_body(exc.code, str(exc))
        return projections.evidence_overview(
            analysis_id=analysis_id, revision=run["revision"], facts=facts)
    if name == "oido_view":
        analysis_id = args.get("analysis_id")
        view = args.get("view", "overview")
        if not isinstance(analysis_id, str) or not analysis_id:
            raise RefError("invalid_input", "analysis_id is required")
        rng = args.get("range_ms")
        if rng is not None and not (isinstance(rng, list) and len(rng) == 2
                                    and all(isinstance(v, int) for v in rng)):
            raise RefError("invalid_input", "range_ms must be [start,end)")
        run = ctx.analyses.get(analysis_id, principal="est")
        if args.get("revision") is not None and args["revision"] != run["revision"]:
            raise RefError("snapshot_changed", "requested analysis revision is unavailable", 409)
        if run["status"] == "failed":
            return projections.error_body("ANALYSIS_FAILED", "analysis failed (terminal)", retryable=False)
        if run["status"] == "cancelled":
            return projections.error_body("ANALYSIS_CANCELLED", "analysis was cancelled", retryable=False)
        if run["status"] not in ("succeeded", "partial"):
            return projections.error_body("NOT_READY_YET", f"analysis is {run['status']}", retryable=True)
        out = ctx.views.render(analysis_id=analysis_id, view=view, range_ms=rng)
        return {"__image_content__": out["artifact_id"], "__png_b64__": __import__("base64").b64encode(out["png"]).decode(),
                "analysis_id": analysis_id, "view": view, "revision": run["revision"],
                "artifact_id": out["artifact_id"], "media_type": "image/png"}
    if name == "oido_find":
        # F-06（跨仓预审 2026-10-25）：cursor 是已声明合同（mcp-tools.v2.json）——
        # 此前未转发，MCP 续页永远重复首页；HTTP 面（/v1/find）与算法本身一直支持。
        cursor = args.get("cursor")
        return ctx.finder.find(
            principal="est", audio_ref=_resolve_ref_arg(args, ctx),
            query=str(args.get("query", "")), mode=str(args.get("mode", "substring")),
            limit=int(args.get("limit", 3)) if isinstance(args.get("limit", 3), int) else 3,
            cursor=cursor if isinstance(cursor, str) and cursor else None)
    if name == "oido_compare":
        left, right = args.get("left"), args.get("right")
        if not isinstance(left, dict) or not isinstance(right, dict):
            raise RefError("invalid_input", "left/right objects required")
        for side in (left, right):
            if isinstance(side.get("audio_ref"), str):
                side["audio_ref"] = _resolve_ref_arg(side | {"audio_ref": side["audio_ref"]}, ctx)
            elif isinstance(side.get("audio_id"), str):
                side["audio_ref"] = _resolve_ref_arg(side, ctx)
        return ctx.comparer.compare(
            principal="est", left=left, right=right,
            metrics=args.get("metrics") if isinstance(args.get("metrics"), list) else None)
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
            import jsonschema
            definition = next((t for t in _tool_list() if t["name"] == name), None)
            if definition is None:
                raise RefError("unknown_tool", "unknown tool")
            if not isinstance(args, dict):
                raise RefError("invalid_input", "arguments must be an object")
            args = dict(args)
            if "audio_id" in args and "audio_ref" not in args:
                args["audio_ref"] = _resolve_ref_arg(args, ctx)
                del args["audio_id"]
            if not jsonschema.Draft202012Validator(definition["inputSchema"]).is_valid(args):
                raise RefError("invalid_input", "arguments do not match the tool schema")
            output = _call_tool(name, args, ctx)
            if isinstance(output, dict) and "__png_b64__" in output:
                png_b64 = output.pop("__png_b64__")
                output.pop("__image_content__", None)
                text = json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                return {"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [
                        {"type": "image", "data": png_b64, "mimeType": "image/png"},
                        {"type": "text", "text": text},
                    ],
                    "isError": False,
                }}
            text = json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": isinstance(output, dict) and "code" in output,
            }}
        except (RefError, AnalysisError, projections.ProjectionError) as exc:
            code = getattr(exc, "code", "TOOL_ERROR").upper().replace("-", "_")
            body = projections.error_body(code, str(exc)[:120])
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
                "isError": True,
            }}
        except Exception as exc:  # noqa: BLE001
            body = projections.error_body("TOOL_ERROR", "tool execution failed")
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
