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
