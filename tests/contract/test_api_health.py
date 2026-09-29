"""API 健康与能力如实报告（EXEC §4.1 子集；mock 等级：内存 SQLite）。"""

import pytest
from fastapi.testclient import TestClient

from oido.api.http import create_app
from oido.config import OidoConfig
from oido.storage.db import migrate, open_db


@pytest.fixture()
def client(tmp_path):
    conn = open_db(tmp_path / "meta" / "oido.sqlite3")
    migrate(conn)
    cfg = OidoConfig(data_root=tmp_path / "data")
    app = create_app(config=cfg, conn=conn)
    return TestClient(app)


class TestHealth:
    def test_live(self, client):
        r = client.get("/health/live")
        assert r.status_code == 200 and r.json() == {"status": "alive"}

    def test_ready_with_db(self, client):
        r = client.get("/health/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready" and body["db"] is True

    def test_ready_without_storage_is_degraded(self, tmp_path):
        # 自审计勘误锚定：没挂存储必须 degraded，不得报 ready
        app = create_app(config=OidoConfig(data_root=tmp_path), conn=None)
        body = TestClient(app).get("/health/ready").json()
        assert body["status"] == "degraded" and body["db"] is False

    def test_live_leaks_nothing(self, client):
        # EXEC §4.1：不返回密钥、路径、模型库清单
        text = client.get("/health/live").text
        for secret_marker in ("token", "key", "/Users/", ".env"):
            assert secret_marker not in text.lower()


class TestCapabilities:
    def test_not_configured_is_honest(self, client):
        r = client.get("/v1/capabilities")
        assert r.status_code == 200
        providers = {p["name"]: p for p in r.json()["providers"]}
        # 重模型未安装：必须报 not_configured，不得报 available
        for name in ("whisper.cpp", "sensevoice", "panns", "clap", "dac", "pyannote"):
            assert providers[name]["status"] == "not_configured", name
        # 本地推理 provider 的网络属性必须是 local_only（D05）
        for p in providers.values():
            if p["status"] != "available":
                continue
            assert p["network"] in ("none", "local_only")

    def test_flags_default_off(self, client):
        flags = client.get("/health/ready").json()["flags"]
        assert flags and not any(flags.values()), "上线开关默认必须全关"
