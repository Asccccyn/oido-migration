"""MCP stdio 服务器：子进程级合同（联合施工 P3；六工具清单+如实未实装）。

不起真实 MCP 宿主；只验证协议行为本身。真实宿主兼容 = OFFLINE_VERIFIED 之外
状态，如实 NOT_EXECUTED。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


@pytest.fixture(scope="module")
def server_proc(tmp_path_factory):
    root = tmp_path_factory.mktemp("oido-mcp")
    env = dict(os.environ)
    env["OIDO_DATA_ROOT"] = str(root)
    env["PYTHONPATH"] = str(_repo_root() / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.pop("OIDO_AUTH_TOKEN", None)
    for k in list(env):
        if k.startswith("OIDO_FLAG_"):
            env.pop(k)
    # This fixture exercises enabled tools; disabled behavior has its own audit regression.
    env["OIDO_FLAG_OFFLINE_ANALYSIS"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "oido.mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=str(_repo_root()), text=True, bufsize=1,
    )
    yield proc
    proc.stdin.close()
    proc.wait(timeout=10)


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]  # tests/contract/ → 仓根


def _rpc(proc, payload: dict) -> dict:
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line.strip():
        try:
            proc.stdin.close()
            rc = proc.wait(timeout=10)
        except Exception:
            rc = "unknown"
        raise AssertionError(f"no response from server; rc={rc}; stderr={proc.stderr.read()[:1500]}")
    return json.loads(line)


class TestMcpStdio:
    def test_initialize(self, server_proc):
        resp = _rpc(server_proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {}})
        assert resp["id"] == 1
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert "tools" in resp["result"]["capabilities"]

    def test_tools_list_six_tools(self, server_proc):
        resp = _rpc(server_proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in resp["result"]["tools"]]
        assert set(names) == {"oido_capabilities", "oido_analyze", "oido_get",
                              "oido_view", "oido_find", "oido_compare"}
        # 自审修复：inputSchema 必须自包含（外部 URN $ref 宿主解析不了）
        for tool in resp["result"]["tools"]:
            assert "urn:" not in json.dumps(tool["inputSchema"]), tool["name"]

    def test_capabilities_call(self, server_proc):
        resp = _rpc(server_proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "oido_capabilities", "arguments": {}}})
        assert resp["result"]["isError"] is False
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert "providers" in payload

    def test_analyze_missing_ref_error(self, server_proc):
        resp = _rpc(server_proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "oido_analyze",
                                             "arguments": {"audio_ref": "est:nope"}}})
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["code"] == "NOT_VISIBLE"

    def test_view_and_find_wired(self, server_proc):
        # view/find 已实装：对不存在的分析/资产走正常错误通道（不再是 NOT_IMPLEMENTED）
        resp = _rpc(server_proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                  "params": {"name": "oido_view",
                                             "arguments": {"analysis_id": "run_missing", "view": "pitch"}}})
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["code"] == "NOT_VISIBLE"
        resp2 = _rpc(server_proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                   "params": {"name": "oido_find",
                                              "arguments": {"audio_ref": "est:none", "query": "啊"}}})
        assert resp2["result"]["isError"] is True
        assert json.loads(resp2["result"]["content"][0]["text"])["code"] == "NOT_VISIBLE"

    def test_notification_no_response(self, server_proc):
        proc = server_proc
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        # 通知不回包；紧接着的 ping 应该拿到下一条响应（无残留）
        resp = _rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "ping"})
        assert resp["id"] == 6 and "result" in resp
