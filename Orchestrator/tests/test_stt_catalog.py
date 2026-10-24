from Orchestrator.stt import catalog as stt_catalog
from Orchestrator.stt.catalog import build_stt_catalog

def test_three_providers_in_order(monkeypatch):
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: False)  # hermetic vs registry
    monkeypatch.setattr(stt_catalog, "onbox_stt_available", lambda: False)  # hermetic vs on-box stack
    assert [p["id"] for p in build_stt_catalog()] == ["openai", "google", "elevenlabs"]

def test_provider_shape():
    for p in build_stt_catalog():
        assert p["label"] and p["blurb"]
        assert "available" in p and isinstance(p["available"], bool)
        assert "streaming" in p["models"] and "file" in p["models"]

def test_models_reflect_config():
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["openai"]["models"]["streaming"] == "gpt-realtime-whisper"
    assert g["openai"]["models"]["file"] == "gpt-4o-transcribe"
    assert g["google"]["models"]["streaming"] == "chirp_2"
    assert g["google"]["models"]["file"] == "chirp_2"
    assert g["elevenlabs"]["models"]["streaming"] == "scribe_v2_realtime"
    assert g["elevenlabs"]["models"]["file"] == "scribe_v2"

def test_available_flags_follow_stt_availability(monkeypatch):
    monkeypatch.setattr(stt_catalog, "stt_availability", lambda: (True, False, False))
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["openai"]["available"] is True
    assert g["google"]["available"] is False
    assert g["elevenlabs"]["available"] is False
    monkeypatch.setattr(stt_catalog, "stt_availability", lambda: (False, True, True))
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["openai"]["available"] is False
    assert g["google"]["available"] is True
    assert g["elevenlabs"]["available"] is True

def test_onbox_provider_emitted_when_available(monkeypatch):
    # SoT: the on-box card's availability is catalog-driven (not hardcoded false).
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: False)  # hermetic vs registry
    monkeypatch.setattr(stt_catalog, "onbox_stt_available", lambda: True)
    g = {p["id"]: p for p in build_stt_catalog()}
    assert "onbox" in g, "onbox provider must be emitted when the on-box stack is live"
    assert g["onbox"]["available"] is True
    assert g["onbox"]["models"]["streaming"] and g["onbox"]["models"]["file"]


def test_onbox_provider_absent_when_unavailable(monkeypatch):
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: False)
    monkeypatch.setattr(stt_catalog, "onbox_stt_available", lambda: False)
    assert "onbox" not in {p["id"] for p in build_stt_catalog()}


def test_onbox_provider_marked_degraded_when_the_gpu_driver_is_dead(monkeypatch):
    """STT still works on CPU, so the entry stays (unlike the TTS group) but
    says so: the Aug outage looked like a healthy GPU box for four days
    (plan Task 10). Additive keys; the blurb carries the reason too."""
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: False)
    monkeypatch.setattr(stt_catalog, "onbox_stt_available", lambda: True)
    monkeypatch.setattr(stt_catalog, "onbox_stt_degraded",
                        lambda: "running on CPU: NVIDIA driver not responding")
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["onbox"]["available"] is True
    assert g["onbox"]["degraded"] is True
    assert g["onbox"]["degraded_reason"] == "running on CPU: NVIDIA driver not responding"
    assert "NVIDIA driver not responding" in g["onbox"]["blurb"]


def test_onbox_provider_not_degraded_normally(monkeypatch):
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: False)
    monkeypatch.setattr(stt_catalog, "onbox_stt_available", lambda: True)
    monkeypatch.setattr(stt_catalog, "onbox_stt_degraded", lambda: None)
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["onbox"]["degraded"] is False and g["onbox"]["degraded_reason"] is None


def test_catalog_route_ok(monkeypatch):
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: False)  # hermetic vs registry
    monkeypatch.setattr(stt_catalog, "onbox_stt_available", lambda: False)  # hermetic vs on-box stack
    import Orchestrator.app  # noqa: F401  -- side-effect: registers routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    r = TestClient(app).get("/stt/catalog")
    assert r.status_code == 200
    body = r.json()
    assert [p["id"] for p in body["providers"]] == ["openai", "google", "elevenlabs"]
    assert "resolved" in body and "default" in body


# ── D-3: the file path and the mic path do not share an answer ──────────────

def _pin(monkeypatch, pin, availability=(True, False, False)):
    """Pin .env STT_PROVIDER + cloud availability inside the RESOLVER module
    (build_stt_resolution resolves live, so patching the catalog module's own
    re-exports is not enough)."""
    from Orchestrator.stt import resolve as stt_resolve
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: availability)
    monkeypatch.setattr(stt_resolve, "_fresh_stt_provider", lambda: pin)


def test_resolution_reports_file_and_streaming_separately(monkeypatch):
    _pin(monkeypatch, "local")
    r = stt_catalog.build_stt_resolution(local_ok=True, streaming_local_ok=False,
                                         onbox_ok=False)
    assert r["resolved_file"] == "local"
    assert r["resolved_streaming"] == "openai"
    assert r["overridden_from_file"] is None
    assert r["overridden_from_streaming"] == "local"


def test_resolution_agrees_when_local_speaks_realtime(monkeypatch):
    _pin(monkeypatch, "local")
    r = stt_catalog.build_stt_resolution(local_ok=True, streaming_local_ok=True,
                                         onbox_ok=False)
    assert r["resolved_file"] == "local" and r["resolved_streaming"] == "local"
    assert r["overridden_from_file"] is None and r["overridden_from_streaming"] is None


def test_catalog_marks_which_path_each_provider_actually_serves(monkeypatch):
    _pin(monkeypatch, "local")
    monkeypatch.setattr(stt_catalog, "stt_availability", lambda: (True, False, False))
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: True)
    monkeypatch.setattr(stt_catalog, "local_streaming_stt_available", lambda: False)
    monkeypatch.setattr(stt_catalog, "onbox_stt_available", lambda: False)
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["local"]["resolves_file"] is True
    assert g["local"]["resolves_streaming"] is False
    assert g["openai"]["resolves_file"] is False
    assert g["openai"]["resolves_streaming"] is True


def test_catalog_route_carries_the_per_path_resolution(monkeypatch):
    # The route builds {"providers", "resolved", "default"}; the split lives on the
    # provider entries so it reaches clients without changing the existing fields.
    _pin(monkeypatch, "local")
    monkeypatch.setattr(stt_catalog, "stt_availability", lambda: (True, False, False))
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: True)
    monkeypatch.setattr(stt_catalog, "local_streaming_stt_available", lambda: False)
    monkeypatch.setattr(stt_catalog, "onbox_stt_available", lambda: False)
    import Orchestrator.app  # noqa: F401  -- side-effect: registers routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    body = TestClient(app).get("/stt/catalog").json()
    assert "resolved" in body and "default" in body          # untouched
    g = {p["id"]: p for p in body["providers"]}
    assert g["local"]["resolves_file"] is True
    assert g["local"]["resolves_streaming"] is False
    assert g["openai"]["resolves_streaming"] is True


# ── D-3: the mic's provider must be readable off the RESPONSE, not inferred ──

def _pin_everywhere(monkeypatch, pin, *, file_local, streaming_local,
                    availability=(True, False, False)):
    """Pin every input the route resolves from. Two modules, because the route
    resolves twice: the legacy "resolved" through Orchestrator.stt.resolve, the
    per-path split through Orchestrator.stt.catalog's own re-exported gates."""
    from Orchestrator.stt import resolve as stt_resolve
    _pin(monkeypatch, pin, availability)
    for mod in (stt_resolve, stt_catalog):
        monkeypatch.setattr(mod, "local_stt_available", lambda: file_local)
        monkeypatch.setattr(mod, "local_streaming_stt_available", lambda: streaming_local)
        monkeypatch.setattr(mod, "onbox_stt_available", lambda: False)
    monkeypatch.setattr(stt_catalog, "stt_availability", lambda: availability)


def _catalog_body():
    import Orchestrator.app  # noqa: F401  -- side-effect: registers routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app).get("/stt/catalog").json()


def test_route_reports_the_streaming_resolution_not_only_the_file_one(monkeypatch):
    """The defect: the body said resolved='local' while the mic streamed to OpenAI.
    Both answers, and the pin the mic dropped, must be in the response itself."""
    _pin_everywhere(monkeypatch, "local", file_local=True, streaming_local=False)
    body = _catalog_body()
    assert body["resolved_file"] == "local"
    assert body["resolved_streaming"] == "openai"
    assert body["overridden_from_file"] is None
    assert body["overridden_from_streaming"] == "local"


def test_route_resolutions_agree_when_local_speaks_realtime(monkeypatch):
    _pin_everywhere(monkeypatch, "local", file_local=True, streaming_local=True)
    body = _catalog_body()
    assert body["resolved_file"] == "local"
    assert body["resolved_streaming"] == "local"
    assert body["overridden_from_file"] is None
    assert body["overridden_from_streaming"] is None


def test_route_legacy_keys_unchanged(monkeypatch):
    """Additive only: "resolved" stays the FILE resolution and "default" stays the
    saved pin, so a client built before the split reads exactly what it read before."""
    from Orchestrator import config as cfg
    _pin_everywhere(monkeypatch, "local", file_local=True, streaming_local=False)
    body = _catalog_body()
    assert body["resolved"] == "local"
    assert body["default"] == (cfg.STT_PROVIDER or "local")
    assert [p["id"] for p in body["providers"]] == ["openai", "google", "elevenlabs", "local"]
