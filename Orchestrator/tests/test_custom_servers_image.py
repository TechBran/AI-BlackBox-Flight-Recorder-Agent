"""Local-image classification + resolution over the custom-server registry."""
from Orchestrator.onboarding import custom_servers as cs


# --- is_image_model -------------------------------------------------------
def test_is_image_model_matches_known_families():
    for mid in ["z-image", "Z-Image", "flux.2-klein-4b", "qwen-image",
                "sdxl-turbo", "sd3-medium", "stable-diffusion-xl", "my-cool-image"]:
        assert cs.is_image_model(mid) is True, mid


def test_is_image_model_rejects_chat_models():
    for mid in ["gemma-12b", "gemma-26b", "gemma-31b", "qwen3-8b",
                "llama-3.3-70b", "mistral-small"]:
        assert cs.is_image_model(mid) is False, mid


def test_is_image_model_non_string():
    assert cs.is_image_model(None) is False
    assert cs.is_image_model(123) is False


# --- resolve_image_server / list_image_models -----------------------------
def _fake_servers(monkeypatch, servers):
    monkeypatch.setattr(cs, "list_servers", lambda enabled_only=False: list(servers))


def test_resolve_image_server_picks_first_image_model(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "api_key": "k",
         "enabled": True, "last_models": ["gemma-31b", "z-image"]},
    ])
    srv, model = cs.resolve_image_server()
    assert srv["base_url"] == "http://h/v1" and model == "z-image"


def test_resolve_image_server_none_when_no_image_model(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True,
         "last_models": ["gemma-31b", "gemma-12b"]},
    ])
    assert cs.resolve_image_server() is None


def test_list_image_models_qualified(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True,
         "last_models": ["gemma-31b", "z-image", "flux.2-klein-4b"]},
    ])
    assert cs.list_image_models() == ["box::z-image", "box::flux.2-klein-4b"]
