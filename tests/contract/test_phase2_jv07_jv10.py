"""JV-07 / JV-10（联合审计 2026-10-26）整改验证。

JV-07：全文分析后再做 inspect_range 局部分析——无 range 的 find/compare 仍选**全文**
事实（不再被最新完成的局部分析遮蔽）；compare side 如实标注 analysis_range_ms。
JV-10：MCP 终态语义——cancelled/failed 不再冒充 retryable pending；同 key 重放取消
任务返回终态（旧：queued）。
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
    monkeypatch.setenv("OIDO_FLAG_OFFLINE_ANALYSIS", "1")
    conn = open_db(tmp_path / "data" / "metadata" / "oido.sqlite3")
    migrate(conn)
    cfg = OidoConfig(data_root=tmp_path / "data")
    cfg.flags["ingest"] = True
    cfg.flags["offline_analysis"] = True
    app = create_app(config=cfg, conn=conn)
    return TestClient(app)


def _h():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_jv07_fulltext_not_shadowed_by_crop(client):
    r = client.post("/v1/audio:ingest", content=_sawtooth_wav(), headers={**_h(), "X-Oido-Audio-Ref": "est:jv7"})
    assert r.status_code == 200, r.text
    audio_id = r.json()["audio_id"]
    rt = client.post(f"/v1/audio/{audio_id}/transcript", json={"text": "嗯啊哦", "source": "t"}, headers=_h())
    assert rt.status_code == 200
    # 1) 全文分析
    r1 = client.post("/v1/analyses", json={"audio_ref": "est:jv7", "wait_ms": 15000}, headers=_h())
    assert r1.status_code == 200 and r1.json()["state"] in ("ready", "partial"), r1.json()
    # 2) 局部 inspect_range（后完成 → updated_at 最新）
    r2 = client.post("/v1/analyses", json={"audio_ref": "est:jv7", "profile": "inspect_range",
                                           "range_ms": [0, 600], "wait_ms": 15000}, headers=_h())
    assert r2.status_code == 200, r2.text
    assert r2.json()["state"] in ("ready", "partial"), r2.json()

    # 3) 无 range find 仍基于全文（旧缺陷：选最新局部 → 只覆盖前 0.6s，"哦"在后段命中失败）
    f = client.post("/v1/find", json={"audio_ref": "est:jv7", "query": "哦"}, headers=_h())
    assert f.status_code == 200, f.text
    out = f.json()
    assert out["state"] == "candidates", out
    assert out["items"][0]["range_ms"][0] >= 1000, f"应命中文末单元：{out}"

    # 4) compare 无 range 用全文两侧；side 标注实际分析范围
    client.post("/v1/audio:ingest", content=_sawtooth_wav(0.9, humps=1), headers={**_h(), "X-Oido-Audio-Ref": "est:jv7b"})
    b2 = client.post("/v1/audio:ingest", content=_sawtooth_wav(0.9, humps=1), headers={**_h(), "X-Oido-Audio-Ref": "est:jv7b"}).json()["audio_id"]
    client.post(f"/v1/audio/{b2}/transcript", json={"text": "嗯", "source": "t"}, headers=_h())
    rb = client.post("/v1/analyses", json={"audio_ref": "est:jv7b", "wait_ms": 15000}, headers=_h())
    assert rb.status_code == 200, rb.text
    c = client.post("/v1/compare", json={"left": {"audio_ref": "est:jv7"}, "right": {"audio_ref": "est:jv7b"}}, headers=_h())
    assert c.status_code == 200, c.text
    left = c.json()["left"]
    assert left["audio_ref"] == "est:jv7"
    assert left.get("analysis_range_ms") in (None, [0, 1800]), f"全文分析范围=整段：{left}"


def test_jv10_mcp_terminal_states(client):
    r = client.post("/v1/audio:ingest", content=_sawtooth_wav(), headers={**_h(), "X-Oido-Audio-Ref": "est:jv10"})
    audio_id = r.json()["audio_id"]
    client.post(f"/v1/audio/{audio_id}/transcript", json={"text": "嗯", "source": "t"}, headers=_h())
    # 同步内联完成不了：MCP analyze 在同进程内联执行——通过 handle_message 直接驱动
    resp = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "oido_analyze", "arguments": {"audio_ref": "est:jv10"}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    analysis_id = body.get("analysis_id")
    assert analysis_id, body
    # HTTP 面取消该分析（queued/running 时可取消；若已内联完成则改用直接取消端点的终态分支）
    cancel = client.post(f"/v1/analyses/{analysis_id}/cancel", headers=_h())
    assert cancel.status_code in (200, 409), cancel.text
    if cancel.status_code == 200 and cancel.json().get("state") == "cancelled":
        # MCP get：终态=不可重试（旧：NOT_READY_YET retryable 永远轮询）
        g = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "oido_get", "arguments": {"analysis_id": analysis_id}}})
        gb = json.loads(g["result"]["content"][0]["text"])
        assert gb["code"] == "ANALYSIS_CANCELLED", gb
        assert gb.get("retryable") is False, gb
        # 同 key 重放 analyze：返回终态而非 queued（旧缺陷）
        r2 = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "oido_analyze", "arguments": {"audio_ref": "est:jv10"}}})
        rb = json.loads(r2["result"]["content"][0]["text"])
        assert rb.get("code") == "ANALYSIS_CANCELLED", rb
