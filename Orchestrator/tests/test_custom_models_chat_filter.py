"""Image models (z-image) are filtered OUT of the custom CHAT model catalog.

They can't chat (the endpoint routes them to /v1/images/generations), so they
must not appear as selectable chat models -- they belong on the generation
screen. last_models in the registry KEEPS them (the image subsystem reads it);
only the chat-catalog OUTPUT is filtered."""
import httpx


def test_zimage_excluded_from_chat_catalog(monkeypatch):
    from Orchestrator.routes import admin_routes
    from Orchestrator.onboarding import custom_servers

    monkeypatch.setattr(custom_servers, "list_servers",
        lambda enabled_only=False: [{"id": "s1", "alias": "box",
            "base_url": "http://h/v1", "api_key": "",
            "last_models": ["gemma-31b", "z-image"]}])

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "gemma-31b"}, {"id": "z-image"}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())

    out = admin_routes._fetch_custom_models()
    ids = [m["id"] for m in out["models"]]
    assert "box::gemma-31b" in ids            # chat model survives
    assert "box::z-image" not in ids          # image model filtered out
    assert out["default_id"] != "box::z-image"  # and never the chat default
