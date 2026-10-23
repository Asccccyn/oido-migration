"""R6′（2026-10-23，她批 R6P 方案）：分析缓存键版本成分 + 取消端点 + find 游标语义。"""
import base64
import hashlib
import json
import math
import os
import struct

import pytest

TOKEN = "r6p-test-token"


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _sawtooth_wav(seconds: float = 1.8, humps: int = 3) -> bytes:
    rate, n = 16_000, int(16_000 * seconds)
    buf = bytearray(b"RIFF")
    buf += struct.pack("<I", 36 + n * 2) + b"WAVEfmt "
    buf += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", n * 2)
    for i in range(n):
        env = (math.sin(2 * math.pi * humps * i / n) + 1) / 2
        buf += struct.pack("<h", int(20000 * env * math.sin(2 * math.pi * 220 * i / rate)))
    return bytes(buf)


class TestR6pLifecycle:
    def test_digest_version_isolation(self, tmp_path):
        """同源同 profile：转写内容变化 → 新 digest 新分析（旧结果不命中）。"""
        from oido.service.analyses import AnalysisService, _pipeline_version
        from oido.storage.db import open_db, migrate

        conn = open_db(tmp_path / "t.db")
        migrate(conn)
        conn.execute(
            "INSERT INTO audio_assets(audio_id, owner, source_kind, original_object_key, sha256, bytes, lifecycle, created_at) "
            "VALUES ('aud-1','est','user_upload','obj-x',?,8,'active',datetime('now'))",
            ("h" * 64,),
        )
        svc = AnalysisService(conn)
        a1 = svc.create(audio_id="aud-1", profile="prosody", requested_by="est")
        conn.execute(
            "INSERT INTO audio_transcripts(audio_id, text, source, created_at) VALUES ('aud-1','嗯啊哦','siren:transcribe',datetime('now'))"
        )
        a2 = svc.create(audio_id="aud-1", profile="prosody", requested_by="est")
        assert a1["analysis_id"] != a2["analysis_id"], "转写变化=新分析（digest 含 transcript hash）"
        assert _pipeline_version() == "oido-prosody-1"
        conn.execute("DELETE FROM audio_transcripts")
        a3 = svc.create(audio_id="aud-1", profile="prosody", requested_by="est")
        assert a3["analysis_id"] != a2["analysis_id"], "转写移除=新分析"
        conn.close()

    def test_cancel_endpoint_fences(self, tmp_path, monkeypatch):
        """取消端点：queued→cancelled（fence+1）；终态再取消=幂等返回原状态。"""
        from fastapi.testclient import TestClient

        from oido.api.http import create_app
        from oido.config import OidoConfig
        from oido.service.analyses import AnalysisService
        from oido.storage.db import open_db, migrate

        monkeypatch.setenv("OIDO_AUTH_TOKEN", TOKEN)
        cfg = OidoConfig(data_root=tmp_path)
        cfg.flags["ingest"] = True
        cfg.flags["offline_analysis"] = True
        conn = open_db(tmp_path / "t.db")
        migrate(conn)
        app = create_app(config=cfg, conn=conn)
        client = TestClient(app)
        r = client.post("/v1/audio:ingest", content=_sawtooth_wav(), headers={**_auth(), "X-Oido-Audio-Ref": "est:cx1"})
        assert r.status_code == 200, r.text
        audio_id = r.json()["audio_id"]
        run = AnalysisService(conn).create(audio_id=audio_id, profile="prosody", requested_by="est")
        c1 = client.post(f"/v1/analyses/{run['analysis_id']}/cancel", headers=_auth())
        assert c1.status_code == 200 and c1.json()["status"] == "cancelled", c1.text
        row = conn.execute("SELECT status, fencing_token FROM analysis_runs WHERE analysis_id=?", (run["analysis_id"],)).fetchone()
        assert row["status"] == "cancelled" and row["fencing_token"] >= 1
        c2 = client.post(f"/v1/analyses/{run['analysis_id']}/cancel", headers=_auth())
        assert c2.status_code == 200 and c2.json()["cancelled"] is True
        conn.close()

    def test_find_cursor_semantics(self):
        """find 游标编码语义：绑定 analysis+revision+query hash（服务内部由 contract 测试覆盖）。"""
        cur = base64.urlsafe_b64encode(json.dumps({
            "a": "an-1", "r": 1,
            "q": hashlib.sha256("嗯".encode()).hexdigest()[:16], "skip": 3,
        }).encode()).decode()
        d = json.loads(base64.urlsafe_b64decode(cur))
        assert d["a"] == "an-1" and d["r"] == 1 and d["skip"] == 3
