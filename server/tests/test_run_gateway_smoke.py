"""Smoke test for ``scripts/run_gateway.py`` wiring.

``run_gateway.py`` is a script (not a package module), so pytest can't
import it directly. This test asserts at the ``build_app`` level — the
seam that ``/device/status`` and the rest of the HTTP API read from —
that a ``ReportedCapabilityProvider`` lands in
``app["sense_capability_provider"]``. The full ``serve()``/``GatewayCore``
wiring (capability_provider + settings forwarded into the per-connection
core) is exercised by the T11 integration test.

Mirrors P1 T12's dummy-model + temp-DB accommodation so the test is
hermetic and doesn't trip the real Ollama/Whisper boot path.
"""
import pytest

from openrecall_server.agent.capability import (
    ConstantCapabilityProvider,
    ReportedCapabilityProvider,
)
from openrecall_server.auth import load_or_create_token
from openrecall_server.http.app import build_app


def test_build_app_stores_reported_capability_provider(tmp_path):
    """build_app must stash the passed capability_provider on the app."""
    token = load_or_create_token(tmp_path / "tok")
    provider = ReportedCapabilityProvider()
    app = build_app(
        token=token,
        get_pubkey=lambda: bytes(range(32)),
        capability_provider=provider,
    )
    assert app["sense_capability_provider"] is provider
    assert isinstance(app["sense_capability_provider"], ReportedCapabilityProvider)


def test_reported_capability_provider_defaults_match_constant():
    """The default CapabilitySet + resources of ReportedCapabilityProvider
    must match ConstantCapabilityProvider so swapping providers in
    run_gateway.py does not regress command admission on the capability
    axis (controller-verified parity, guarded here as a regression net)."""
    reported = ReportedCapabilityProvider()
    constant = ConstantCapabilityProvider()
    assert reported.capabilities() == constant.capabilities()
    # resources() both default to battery_pct=1.0; full parity on every
    # field (the controller verified this — guarded here as a regression
    # net so a future change to either default doesn't silently regress
    # command admission on the capability axis).
    r_res = reported.resources()
    c_res = constant.resources()
    assert r_res.battery_pct == c_res.battery_pct == 1.0
    assert r_res == c_res


def test_run_gateway_source_uses_reported_provider():
    """Source-level guard: run_gateway.py must construct a
    ReportedCapabilityProvider (not ConstantCapabilityProvider) for the
    planner, reconciler, and build_app call sites. Reads the file as text
    since the script isn't importable from pytest."""
    from pathlib import Path

    server_root = Path(__file__).resolve().parents[1]
    src = (server_root / "scripts" / "run_gateway.py").read_text()
    assert "ReportedCapabilityProvider" in src
    # The three call sites that previously used ConstantCapabilityProvider()
    # must now use the shared reported provider.
    assert src.count("ConstantCapabilityProvider()") == 0, (
        "run_gateway.py still constructs ConstantCapabilityProvider() — "
        "all three call sites should use the shared ReportedCapabilityProvider"
    )
    # Settings store must be forwarded into serve() so on_telemetry can
    # clear desired sleep_mode on a button wake.
    assert "settings=settings_store" in src
    assert "capability_provider=capability_provider" in src


def test_run_gateway_source_wires_vision_pipeline():
    """Source-level guard: run_gateway.py must construct the P3 vision deps
    (blob store, vision pipeline, session timeline, rel_ts_sink, provider
    vision_enabled, retention sweep) and pass them to build_app. Reads the
    file as text since the script isn't importable from pytest."""
    from pathlib import Path

    server_root = Path(__file__).resolve().parents[1]
    src = (server_root / "scripts" / "run_gateway.py").read_text()
    assert "FilesystemBlobStore" in src
    assert "VisionPipeline" in src
    assert "SessionTimelineIndex" in src
    assert "rel_ts_sink=session_timeline.record" in src
    assert "vision_enabled=" in src
    assert "sweep_vision_retention" in src
    # the new build_app kwargs are passed
    assert "vision=vision_pipeline" in src
    assert "session_timeline=session_timeline" in src
    assert "blob_store=blob_store" in src


def test_build_app_stashes_vision_deps(tmp_path):
    """build_app must accept + stash the P3 deps on the app (the route reads
    them via sense_* keys)."""
    from openrecall_server.media.blob import InMemoryBlobStore
    from openrecall_server.sessions.timeline import SessionTimelineIndex
    from openrecall_server.http.app import build_app

    blobs = InMemoryBlobStore()
    tl = SessionTimelineIndex()
    app = build_app(
        token="t", get_pubkey=lambda: b"\x00" * 32,
        blob_store=blobs, vision=None, session_timeline=tl,
    )
    assert app["sense_blob_store"] is blobs
    assert app["sense_session_timeline"] is tl
    assert app["sense_vision"] is None   # None when no model configured


def test_run_device_sim_source_wires_video_upload():
    """Source guard: run_device_sim.py wires --video to POST /media/videos."""
    from pathlib import Path
    server_root = Path(__file__).resolve().parents[1]
    src = (server_root / "scripts" / "run_device_sim.py").read_text()
    assert '"--video"' in src
    assert "upload_video_request" in src
    assert "/media/videos" in src
    assert "args.video" in src
