"""/validate detects per-model modality; registration persists the confirmed map."""
from Orchestrator.onboarding import validators


class _M:
    def __init__(self, id):
        self.id = id


class _FakeClient:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    class models:
        @staticmethod
        def list():
            class _R:
                data = [_M("gemma-31b"), _M("z-image"), _M("whisper-1"), _M("kokoro-tts")]
            return _R()


def test_validate_custom_returns_modalities(monkeypatch):
    import openai
    monkeypatch.setattr(openai, "OpenAI", _FakeClient)
    res = validators.validate_custom("http://h/v1", "k")
    assert res.ok, res
    d = res.detail
    assert d["model_modalities"] == {
        "gemma-31b": "chat", "z-image": "image", "whisper-1": "stt", "kokoro-tts": "tts"}
    assert set(d["capabilities"]) == {"chat", "image", "stt", "tts"}


def test_add_custom_server_passes_modalities(monkeypatch):
    from Orchestrator.routes import onboarding_routes
    captured = {}
    monkeypatch.setattr(onboarding_routes.custom_servers, "add_server",
                        lambda **kw: captured.update(kw) or {"id": "srv-x", "alias": kw["alias"]})
    monkeypatch.setattr(onboarding_routes.custom_servers, "redact", lambda s: s)
    req = onboarding_routes.CustomServerCreate(
        alias="box", base_url="http://h/v1", api_key="k",
        model_modalities={"z-image": "image"})
    onboarding_routes.add_custom_server(req)
    assert captured["model_modalities"] == {"z-image": "image"}


def test_revalidate_merges_seed_under_corrections(monkeypatch):
    """Re-validate PRESERVES a user's persisted correction (existing wins) while
    seeding models discovered since (the H2/M1 fix)."""
    from Orchestrator.routes import onboarding_routes
    from Orchestrator.onboarding.validators import ValidationResult

    captured = {}
    stored = {"id": "srv-x", "base_url": "http://h/v1", "api_key": "k",
              "model_modalities": {"z-image": "chat"}}  # user corrected z-image -> chat
    monkeypatch.setattr(onboarding_routes.custom_servers, "get_server", lambda sid: dict(stored))
    monkeypatch.setattr(onboarding_routes.custom_servers, "update_server",
                        lambda sid, patch: captured.update(patch) or dict(stored, **patch))
    monkeypatch.setattr(onboarding_routes.validators, "validate_custom",
                        lambda base_url, api_key="": ValidationResult(
                            ok=True, latency_ms=1, error=None,
                            detail={"models": ["z-image", "whisper-1"],
                                    "model_modalities": {"z-image": "image", "whisper-1": "stt"},
                                    "capabilities": ["image", "stt"]}))
    monkeypatch.setattr(onboarding_routes._state, "record_validation", lambda *a, **k: None)

    import Orchestrator.app  # noqa: F401
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    r = TestClient(app).post("/onboarding/validate",
                             json={"provider": "custom", "credentials": {"server_id": "srv-x"}})
    assert r.status_code == 200, r.text
    # correction z-image->chat PRESERVED; whisper-1 newly seeded
    assert captured["model_modalities"] == {"z-image": "chat", "whisper-1": "stt"}
