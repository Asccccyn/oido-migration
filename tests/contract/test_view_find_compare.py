"""view/find/compare 三工具合同（JOINT-VOICE-20261022 收尾；合成信号）。

view=真实数值渲染（PNG 魔数/artifacts 幂等/range 裁剪）；find=对齐区间文字匹配
（精确/模糊/无命中/无转写拒绝）；compare=可比量差异+无基线拒绝。
全部合成锯齿波（有谐波，pYIN 可判 voiced；纯正弦无谐波会 unvoiced——生产烟测实测结论）。
"""

from __future__ import annotations

import io
import json
import wave

import pytest
from fastapi.testclient import TestClient

from oido.api.http import create_app
from oido.config import OidoConfig
from oido.storage.db import migrate, open_db

TOKEN = "test-token-123"


def _sawtooth_wav(seconds: float = 1.8, sr: int = 16000, f0: float = 220.0, humps: int = 3) -> bytes:
    """humps 个起伏的锯齿波（同一有声段内多个能量峰→多音节对齐候选）。"""
    import math

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
            # 三角谐波近似锯齿：基频+2/3 次谐波叠加
            v = math.sin(2 * math.pi * f0 * t) + 0.5 * math.sin(4 * math.pi * f0 * t) + 0.25 * math.sin(6 * math.pi * f0 * t)
            hump_t = (t % period) / period
            env = math.sin(math.pi * hump_t) ** 2 if hump_t < 1 else 0  # 各 hump 平滑起落
            frames += int(12000 * 0.9 * v * env * 0.5).to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OIDO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("OIDO_DATA_ROOT", str(tmp_path / "data"))
    conn = open_db(tmp_path / "meta" / "oido.sqlite3")
    migrate(conn)
    cfg = OidoConfig(data_root=tmp_path / "data")
    cfg.flags["ingest"] = True
    cfg.flags["offline_analysis"] = True
    app = create_app(config=cfg, conn=conn)
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _ingest_analyzed(client, ref: str, *, transcript: str | None, data: bytes | None = None):
    r = client.post("/v1/audio:ingest", content=data or _sawtooth_wav(),
                    headers={**_auth(), "X-Oido-Audio-Ref": ref})
    assert r.status_code == 200, r.text
    audio_id = r.json()["audio_id"]
    if transcript is not None:
        rt = client.post(f"/v1/audio/{audio_id}/transcript",
                         json={"text": transcript, "source": "test:fixture"}, headers=_auth())
        assert rt.status_code == 200
    r2 = client.post("/v1/analyses", json={"audio_ref": ref, "wait_ms": 15000}, headers=_auth())
    assert r2.status_code == 200, r2.text
    return r2.json()


class TestView:
    def test_all_five_views_png_with_artifacts(self, client):
        body = _ingest_analyzed(client, "est:vw1", transcript="嗯")
        aid = body["analysis_id"]
        seen = set()
        for view in ("overview", "waveform", "spectrogram", "pitch", "energy"):
            r = client.get(f"/v1/analyses/{aid}/view?view={view}", headers=_auth())
            assert r.status_code == 200, (view, r.text[:200])
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n", view
            art = r.headers.get("x-oido-artifact-id")
            assert art and art not in seen
            seen.add(art)

    def test_reuse_same_artifact_and_range_crop_differs(self, client):
        body = _ingest_analyzed(client, "est:vw2", transcript="嗯")
        aid = body["analysis_id"]
        r1 = client.get(f"/v1/analyses/{aid}/view?view=waveform", headers=_auth())
        r2 = client.get(f"/v1/analyses/{aid}/view?view=waveform", headers=_auth())
        assert r1.headers["x-oido-artifact-id"] == r2.headers["x-oido-artifact-id"], "同参数幂等复用"
        r3 = client.get(f"/v1/analyses/{aid}/view?view=waveform&range_ms=0,500", headers=_auth())
        assert r3.status_code == 200
        assert r3.headers["x-oido-artifact-id"] != r1.headers["x-oido-artifact-id"], "不同 range=不同产物"

    def test_bad_view_rejected(self, client):
        body = _ingest_analyzed(client, "est:vw3", transcript="嗯")
        r = client.get(f"/v1/analyses/{body['analysis_id']}/view?view=thermal", headers=_auth())
        assert r.status_code == 422


class TestFind:
    def test_exact_and_span_and_no_match(self, client):
        # 3 humps ↔ 转写 3 字（对齐成立：音节峰数=单元数）
        body = _ingest_analyzed(client, "est:fd1", transcript="嗯啊哦")
        assert body["state"] in ("ready", "partial"), body
        r = client.post("/v1/find", json={"audio_ref": "est:fd1", "query": "啊"}, headers=_auth())
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["state"] == "candidates" and len(out["items"]) == 1
        rng = out["items"][0]["range_ms"]
        assert 300 <= rng[0] <= 1100, f"中段单元应落在第二个 hump：{rng}"  # 1.8s/3 humps
        r2 = client.post("/v1/find", json={"audio_ref": "est:fd1", "query": "嗯啊"}, headers=_auth())
        out2 = r2.json()
        assert out2["state"] == "candidates" and out2["items"][0]["matched_units"] == 2
        r3 = client.post("/v1/find", json={"audio_ref": "est:fd1", "query": "不存在的话"}, headers=_auth())
        assert r3.json()["state"] == "no_match" and r3.json()["items"] == []

    def test_fuzzy_one_char_diff(self, client):
        _ingest_analyzed(client, "est:fd2", transcript="嗯啊哦")
        r = client.post("/v1/find", json={"audio_ref": "est:fd2", "query": "嗯嗄", "mode": "fuzzy"}, headers=_auth())
        assert r.json()["state"] == "candidates"
        r2 = client.post("/v1/find", json={"audio_ref": "est:fd2", "query": "嗯嗄"}, headers=_auth())
        assert r2.json()["state"] == "no_match", "substring 模式不许模糊命中"

    def test_no_transcript_rejected_honestly(self, client):
        _ingest_analyzed(client, "est:fd3", transcript=None)
        r = client.post("/v1/find", json={"audio_ref": "est:fd3", "query": "啊"}, headers=_auth())
        assert r.status_code == 409

    def test_unanalyzed_audio_rejected(self, client):
        client.post("/v1/audio:ingest", content=_sawtooth_wav(),
                    headers={**_auth(), "X-Oido-Audio-Ref": "est:fd4"})
        r = client.post("/v1/find", json={"audio_ref": "est:fd4", "query": "啊"}, headers=_auth())
        assert r.status_code == 409


class TestCompare:
    def test_metric_differences_and_no_baseline(self, client):
        _ingest_analyzed(client, "est:cp1", transcript="嗯啊哦", data=_sawtooth_wav(1.8, humps=3))
        _ingest_analyzed(client, "est:cp2", transcript="嗯", data=_sawtooth_wav(0.9, humps=1))
        r = client.post("/v1/compare", json={
            "left": {"audio_ref": "est:cp1"}, "right": {"audio_ref": "est:cp2"},
        }, headers=_auth())
        assert r.status_code == 200, r.text
        out = r.json()
        names = [m["metric"] for m in out["metrics"]]
        assert "duration_s" in names and "f0_median_hz" in names
        dur = next(m for m in out["metrics"] if m["metric"] == "duration_s")
        assert dur["left"] > dur["right"] and dur["quality"] == "measured"
        # 无个人基线：明确拒绝"比平时"（05）
        assert out["baseline_comparison"]["state"] == "unavailable"
        assert out["baseline_comparison"]["reason"] == "insufficient_baseline"
        # 全输出无情绪键
        assert "emotion" not in json.dumps(out).lower() and "情绪" not in json.dumps(out)

    def test_unanalyzed_side_rejected(self, client):
        _ingest_analyzed(client, "est:cp3", transcript="嗯")
        client.post("/v1/audio:ingest", content=_sawtooth_wav(),
                    headers={**_auth(), "X-Oido-Audio-Ref": "est:cp4"})
        r = client.post("/v1/compare", json={
            "left": {"audio_ref": "est:cp3"}, "right": {"audio_ref": "est:cp4"}}, headers=_auth())
        assert r.status_code == 409

    def test_metrics_filter_and_unknown_metric(self, client):
        _ingest_analyzed(client, "est:cp5", transcript="嗯")
        _ingest_analyzed(client, "est:cp6", transcript="嗯")
        r = client.post("/v1/compare", json={
            "left": {"audio_ref": "est:cp5"}, "right": {"audio_ref": "est:cp6"},
            "metrics": ["voiced_total_s"]}, headers=_auth())
        assert [m["metric"] for m in r.json()["metrics"]] == ["voiced_total_s"]
        r2 = client.post("/v1/compare", json={
            "left": {"audio_ref": "est:cp5"}, "right": {"audio_ref": "est:cp6"},
            "metrics": ["sadness"]}, headers=_auth())
        assert r2.status_code == 422
