"""F-06（跨仓预审 2026-10-25）整改验证：MCP oido_find 续页游标在桥接层转发。

真实 HTTP 入口 ingest/transcript/analyses 建状态（合成锯齿波+离线 DSP），
再调真实 MCP dispatcher（handle_message → _Ctx → 真实 finder）：
limit=1 首页 → next_cursor 回传 → 第二页必须推进（旧缺陷：MCP 永远重复首页）。
同一 cursor 走 HTTP /v1/find 作同数据对照。
"""

from __future__ import annotations

import io
import json
import math
import wave

import pytest
from fastapi.testclient import TestClient

from oido.api.http import create_app
from oido.config import OidoConfig
from oido.mcp.server import handle_message
from oido.storage.db import migrate, open_db

TOKEN = "test-token-123"


def _sawtooth_wav(seconds: float = 1.8, sr: int = 16000, f0: float = 220.0, humps: int = 3) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(sr * seconds)
        frames = bytearray()
        period = seconds / humps
        for i in range(n):
            t = i / sr
            v = math.sin(2 * math.pi * f0 * t) + 0.5 * math.sin(4 * math.pi * f0 * t) + 0.25 * math.sin(6 * math.pi * f0 * t)
            hump_t = (t % period) / period
            env = math.sin(math.pi * hump_t) ** 2 if hump_t < 1 else 0
            frames += int(12000 * 0.9 * v * env * 0.5).to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OIDO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("OIDO_DATA_ROOT", str(tmp_path / "data"))
    # MCP _Ctx 从 env 重建 config——离线 DSP 开关也走 env（同 test_mcp_stdio）
    monkeypatch.setenv("OIDO_FLAG_OFFLINE_ANALYSIS", "1")
    # 与 MCP _Ctx 同一数据库路径（root/metadata/oido.sqlite3）：HTTP 建的状态
    # 对同进程 dispatcher 可见——两运输面共用同一业务真源。
    conn = open_db(tmp_path / "data" / "metadata" / "oido.sqlite3")
    migrate(conn)
    cfg = OidoConfig(data_root=tmp_path / "data")
    cfg.flags["ingest"] = True
    cfg.flags["offline_analysis"] = True
    app = create_app(config=cfg, conn=conn)
    return TestClient(app)


def _mcp_find(audio_ref: str, query: str, *, cursor: str | None = None, limit: int = 1) -> dict:
    args: dict = {"audio_ref": audio_ref, "query": query, "limit": limit}
    if cursor:
        args["cursor"] = cursor
    resp = handle_message({"jsonrpc": "2.0", "id": 91, "method": "tools/call",
                           "params": {"name": "oido_find", "arguments": args}})
    assert resp is not None
    return json.loads(resp["result"]["content"][0]["text"])


def test_mcp_find_cursor_advances_page(client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    r = client.post("/v1/audio:ingest", content=_sawtooth_wav(),
                    headers={**headers, "X-Oido-Audio-Ref": "est:p05mcp"})
    assert r.status_code == 200, r.text
    audio_id = r.json()["audio_id"]
    rt = client.post(f"/v1/audio/{audio_id}/transcript",
                     json={"text": "嗯啊嗯", "source": "test:phase05"}, headers=headers)
    assert rt.status_code == 200
    r2 = client.post("/v1/analyses", json={"audio_ref": "est:p05mcp", "wait_ms": 15000}, headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["state"] in ("ready", "partial"), r2.json()

    page1 = _mcp_find("est:p05mcp", "嗯")
    assert page1["state"] == "candidates", page1
    assert len(page1["items"]) == 1
    first_range = page1["items"][0]["range_ms"]
    cursor = page1.get("next_cursor")
    assert cursor, "两个命中 limit=1：首页必须签发续页游标"

    page2 = _mcp_find("est:p05mcp", "嗯", cursor=cursor)
    assert page2["state"] == "candidates", page2
    assert len(page2["items"]) == 1
    second_range = page2["items"][0]["range_ms"]
    # 修复断言：第二页推进（旧缺陷：MCP 不转发 cursor，第二页重复首页区间）
    assert second_range[0] > first_range[0], (first_range, second_range)
    assert page2.get("next_cursor") is None, "共 2 个命中：第二页即末页（不发假游标）"

    # 同 cursor 的 HTTP 面对照：同一数据源两边一致
    http2 = client.post("/v1/find", json={"audio_ref": "est:p05mcp", "query": "嗯", "limit": 1, "cursor": cursor},
                        headers=headers)
    assert http2.status_code == 200, http2.text
    assert http2.json()["items"][0]["range_ms"] == second_range
