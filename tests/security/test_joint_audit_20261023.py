"""Independent audit regressions. Synthetic persisted facts; no real audio/provider."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from oido.config import OidoConfig
from oido.mcp.server import handle_message
from oido.prosody import pipeline, projections
from oido.service.analyses import AnalysisService
from oido.service.assets import AssetService
from oido.service.find import FindService
from oido.service.ingest import IngestService, RefError
from oido.storage.db import migrate, open_db
from oido.storage.objects import ObjectStore


@pytest.fixture
def ctx(tmp_path):
    conn = open_db(tmp_path / "meta.sqlite")
    migrate(conn)
    objects = ObjectStore(tmp_path / "objects")
    assets = AssetService(conn, objects)
    ingest = IngestService(conn, assets)
    analyses = AnalysisService(conn)
    cfg = OidoConfig(data_root=tmp_path)
    return SimpleNamespace(conn=conn, objects=objects, assets=assets,
                           ingest=ingest, analyses=analyses, cfg=cfg,
                           finder=FindService(analyses, ingest))


def seed(ctx, text="啊" * 100, ref="est:a"):
    asset = ctx.ingest.ingest(principal="est", audio_ref=ref, data=b"synthetic",
                              transcript={"text": text, "source": "test:synthetic"})
    run = ctx.analyses.create(audio_id=asset["audio_id"], requested_by="est", profile="prosody")
    lease = ctx.analyses.claim(run["analysis_id"], worker="test")
    facts = {"sr": 16000, "duration_s": len(text), "voiced_total_s": len(text), "f0_median_hz":220.,
             "alignment_source": "syllable_energy_dp", "limits": [],
             "units": [{"text": c, "start_s": i, "end_s": i + .8} for i, c in enumerate(text)],
             "unit_pitch_st": [[1, 0, -1] for _ in text]}
    ctx.analyses.commit_result(analysis_id=run["analysis_id"], fencing_token=lease["fencing_token"],
        status="succeeded", stages=[{"capability": "prosody_facts", "status": "succeeded", "detail": {"facts": facts}}])
    return asset["audio_id"], run["analysis_id"], facts


def test_reingest_different_bytes_without_claimed_digest_is_conflict(ctx):
    ctx.ingest.ingest(principal="est", audio_ref="est:bytes", data=b"one")
    with pytest.raises(RefError, match="different bytes"):
        ctx.ingest.ingest(principal="est", audio_ref="est:bytes", data=b"two")


def test_declared_digest_does_not_override_actual_bytes(ctx):
    import hashlib
    with pytest.raises(RefError):
        ctx.ingest.ingest(principal="est", audio_ref="est:hash", data=b"one",
                          content_sha256=hashlib.sha256(b"two").hexdigest())
    assert ctx.conn.execute("SELECT count(*) FROM audio_assets").fetchone()[0] == 0


def test_delete_removes_transcript_plaintext(ctx):
    audio_id, _, _ = seed(ctx)
    ctx.assets.request_delete(audio_id, principal="est")
    assert ctx.ingest.transcript_of(audio_id) is None


def test_find_does_not_echo_query_or_redundant_item_analysis(ctx):
    seed(ctx)
    result = ctx.finder.find(principal="est", audio_ref="est:a", query="啊")
    print("FIND_JSON", json.dumps(result, ensure_ascii=False))
    assert "query" not in result
    assert all("analysis_id" not in i for i in result["items"])


def test_find_rejects_overlong_query(ctx):
    seed(ctx)
    from oido.service.analyses import AnalysisError
    with pytest.raises(AnalysisError):
        ctx.finder.find(principal="est", audio_ref="est:a", query="啊" * 10000)


def test_mcp_enforces_declared_schema_before_access(ctx):
    result = handle_message({"jsonrpc":"2.0", "id":1,"method":"tools/call",
        "params":{"name":"oido_capabilities","arguments":{"undeclared":True}}}, ctx)
    assert result["result"]["isError"] is True


def test_mcp_respects_disabled_analysis(ctx):
    result = handle_message({"jsonrpc":"2.0", "id":1,"method":"tools/call",
        "params":{"name":"oido_analyze","arguments":{"audio_ref":"est:a"}}}, ctx)
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["code"] == "SERVICE_DISABLED"


def test_falling_pitch_samples_keep_time_order(monkeypatch):
    # Deterministic DSP fixture: voiced pitch descends in time, same envelope/text.
    f0 = [400., 350., 300., 250., 200., 150.]
    monkeypatch.setattr(pipeline, "pyin_f0", lambda *a, **k: {
        "f0_hz":f0, "voiced":[True]*len(f0), "sufficient":True,"method":"synthetic"})
    monkeypatch.setattr(pipeline, "_energy_peaks", lambda *a, **k:[.3])
    from oido.dsp.segments import Segments, VoicedSegment
    import oido.dsp.segments as segments
    monkeypatch.setattr(segments, "segment_utterance", lambda **k: Segments(
        voiced=[VoicedSegment(0., .8, 1.)], pauses=[], leading_silence_s=0., trailing_silence_s=0., voiced_total_s=.8))
    result = pipeline.run_prosody(audio_id="aud_synthetic", samples=np.ones(16000, dtype=np.float32),
        sr=16000, variant_id="var_synthetic", timebase_id="tb_16k_mono",
        transcript={"text":"啊","source":"test:synthetic"})
    points = result.facts["unit_pitch_st"][0]
    print("FALLING_F0", f0, "PITCH_ST", points)
    assert points[0] > points[-1]


def test_cursor_cannot_be_reused_for_another_analysis(ctx):
    _, _, facts = seed(ctx)
    first = projections.prosody_page(analysis_id="run_first", revision=1, facts=facts)
    with pytest.raises(projections.ProjectionError):
        projections.prosody_page(analysis_id="run_second", revision=1, facts=facts, cursor=first["next_cursor"])


def test_range_is_processed_in_source_coordinates(ctx, monkeypatch):
    from oido.workers import analysis_worker
    audio_id = ctx.ingest.ingest(principal="est", audio_ref="est:range", data=b"synthetic")["audio_id"]
    run = ctx.analyses.create(audio_id=audio_id, requested_by="est", profile="inspect_range",
                              range_={"start_ms": 200, "end_ms": 500})
    seen = {}
    def fake_pipeline(**kwargs):
        seen.update(kwargs)
        return pipeline.ProsodyResult(status="succeeded", facts={"sr":16000,
            "duration_s":len(kwargs["samples"])/16000, "limits":[],"units":[],"pauses":[]})
    monkeypatch.setattr(analysis_worker, "run_prosody", fake_pipeline)
    variant = {"sample_rate_hz":16000, "variant_id":"var_synth", "timebase_id":"tb_16k_mono", "sample_count":16000}
    variants = SimpleNamespace(ensure_decode_main=lambda **k:variant,
                               load_pcm=lambda v:np.ones(16000, dtype=np.float32))
    analysis_worker.process_one(conn=ctx.conn, analyses=ctx.analyses, variants=variants,
        ingest=ctx.ingest, objects=ctx.objects, analysis_id=run["analysis_id"])
    facts = analysis_worker.load_facts(ctx.conn, run["analysis_id"])
    out = projections.evidence_overview(analysis_id=run["analysis_id"], revision=1, facts=facts)
    print("RANGE_SAMPLE_COUNT", len(seen["samples"]), "RANGE_OVERVIEW", out)
    assert len(seen["samples"]) == 4800
    assert out["range_ms"] == [200,500]


def test_compare_range_does_not_claim_whole_audio_pitch(ctx):
    from oido.service.compare import CompareService
    seed(ctx)
    out = CompareService(ctx.analyses, ctx.ingest).compare(principal="est",
        left={"audio_ref":"est:a", "range_ms":[0,500]}, right={"audio_ref":"est:a"},
        metrics=["f0_median_hz"])
    metric = out["metrics"][0]
    assert metric["quality"] != "measured"


def test_late_decode_cannot_restore_deleted_derivative(ctx, monkeypatch):
    import oido.service.variants as variant_module
    audio_id = ctx.ingest.ingest(principal="est", audio_ref="est:late", data=b"synthetic")["audio_id"]
    def late_decode(data):
        ctx.assets.request_delete(audio_id, principal="est")
        return SimpleNamespace(samples=np.zeros(16000, dtype=np.float32), sample_rate_hz=16000,
                               timebase_id="tb_16k_mono", transform_params={})
    monkeypatch.setattr(variant_module, "decode_mono_pcm", late_decode)
    with pytest.raises(variant_module.VariantError):
        variant_module.VariantService(ctx.conn, ctx.objects).ensure_decode_main(audio_id=audio_id, original_bytes=b"synthetic")
    assert ctx.conn.execute("SELECT count(*) FROM audio_variants").fetchone()[0] == 0


def test_late_render_cannot_restore_deleted_artifact(ctx, monkeypatch):
    import oido.service.views as view_module
    import oido.service.variants as variant_module
    from oido.dsp.render import RenderError
    audio_id, analysis_id, _ = seed(ctx)
    monkeypatch.setattr(variant_module, "decode_mono_pcm", lambda data: SimpleNamespace(
        samples=np.zeros(16000, dtype=np.float32), sample_rate_hz=16000,
        timebase_id="tb_16k_mono", transform_params={}))
    variants = variant_module.VariantService(ctx.conn, ctx.objects)
    variants.ensure_decode_main(audio_id=audio_id, original_bytes=b"synthetic")
    def late_measure(*a, **k):
        ctx.assets.request_delete(audio_id, principal="est")
        return {"f0_hz":[]}
    monkeypatch.setattr(view_module, "pyin_f0", late_measure)
    with pytest.raises(RenderError):
        view_module.ViewService(ctx.conn, variants, ctx.objects).render(analysis_id=analysis_id, view="pitch")
    assert ctx.conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_untranscribed_voiced_signal_is_not_called_unvoiced(monkeypatch):
    monkeypatch.setattr(pipeline, "pyin_f0", lambda *a, **k: {
        "f0_hz":[200.,220.,240.], "voiced":[True]*3, "sufficient":True,"method":"synthetic"})
    result = pipeline.run_prosody(audio_id="aud_synth",samples=np.ones(16000,dtype=np.float32),
        sr=16000,variant_id="var_synth",timebase_id="tb_16k_mono",transcript=None)
    out = projections.evidence_overview(analysis_id="run_synth",revision=1,facts=result.facts)
    print("UNTRANSCRIBED_VOICED",out)
    pitch = next(o for o in out["observations"] if o["kind"]=="pitch")
    assert pitch["value"] is not None
    assert pitch.get("reason") != "unvoiced"


def test_frame_tail_alignment_stays_inside_actual_samples(monkeypatch):
    monkeypatch.setattr(pipeline, "pyin_f0", lambda *a, **k: {
        "f0_hz":[220.]*114, "voiced":[True]*114, "sufficient":True,"method":"synthetic"})
    monkeypatch.setattr(pipeline, "_energy_peaks", lambda *a, **k:[.3,.9,1.5])
    result = pipeline.run_prosody(audio_id="aud_synth",samples=np.ones(28800,dtype=np.float32),
        sr=16000,variant_id="var_synth",timebase_id="tb_16k_mono",transcript={"text":"嗯啊哦"})
    out = projections.prosody_page(analysis_id="run_synth",revision=1,facts=result.facts)
    print("ACTUAL_SAMPLES",28800,"PAGE_RANGE",out["range_ms"])
    assert out["range_ms"][1] <= 1800
    assert all(e["range"]["end_sample"] <= 28800 for e in result.evidence)


def test_supervisor_unobservable_memory_fails_closed(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts/supervise_tests.py"
    spec = importlib.util.spec_from_file_location("audit_supervisor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def unavailable(pgid):
        raise RuntimeError("fixture: ps denied")
    monkeypatch.setattr(module, "_tree_rss_kib", unavailable)
    assert module.supervise([sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_seconds=2, rss_limit_mib=512, grace_seconds=.2) == 5


def test_model_error_does_not_expose_internal_exception(ctx, monkeypatch):
    import oido.mcp.server as server
    def internal(*args):
        raise RuntimeError("private/path/internal-provider-debug")
    monkeypatch.setattr(server, "_call_tool", internal)
    out = server.handle_message({"id":1,"method":"tools/call",
        "params":{"name":"oido_capabilities","arguments":{}}},ctx)
    assert "private/path" not in json.dumps(out)


def test_get_does_not_ignore_requested_revision(ctx):
    _, analysis_id, _ = seed(ctx)
    out = handle_message({"id":1,"method":"tools/call", "params":{"name":"oido_get",
        "arguments":{"analysis_id":analysis_id,"revision":2}}}, ctx)
    assert out["result"]["isError"] is True


def test_get_does_not_ignore_requested_range(ctx):
    _, analysis_id, _ = seed(ctx)
    out = handle_message({"id":1,"method":"tools/call", "params":{"name":"oido_get",
        "arguments":{"analysis_id":analysis_id,"range_ms":[100,200]}}}, ctx)
    assert out["result"]["isError"] is True


def test_page_does_not_silently_cut_long_mixed_language_unit(ctx):
    _, _, facts = seed(ctx)
    facts["units"][0]["text"] = "tomorrow"
    with pytest.raises(projections.ProjectionError):
        projections.prosody_page(analysis_id="run_x",revision=1,facts=facts)
