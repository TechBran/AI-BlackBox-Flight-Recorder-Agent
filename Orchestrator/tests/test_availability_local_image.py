"""Registry-gated 'local' image provider in the availability layer."""
from Orchestrator.toolvault import availability as av


def test_local_enabled_when_registry_has_image_model(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})              # no cloud keys
    monkeypatch.setattr(av, "_local_image_available", lambda: True)
    assert "local" in av.enabled_providers("image")


def test_local_absent_when_registry_empty(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"GOOGLE_API_KEY": "g"})
    monkeypatch.setattr(av, "_local_image_available", lambda: False)
    enabled = av.enabled_providers("image")
    assert "local" not in enabled and "gemini" in enabled


def test_local_image_tool_available(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})
    monkeypatch.setattr(av, "_local_image_available", lambda: True)
    entry = {"x-availability": {"feature": "image", "provider": "local"}}
    assert av.is_available(entry) is True


def test_local_not_leaking_into_web_search(monkeypatch):
    # The registry gate must be image-only; web_search must be unaffected.
    monkeypatch.setattr(av, "_read_env", lambda: {})
    monkeypatch.setattr(av, "_local_image_available", lambda: True)
    assert "local" not in av.enabled_providers("web_search")


def test_local_image_available_is_failsoft(monkeypatch):
    # A raising dependency inside _local_image_available must return False, never raise.
    import Orchestrator.onboarding.custom_servers as cs
    monkeypatch.setattr(cs, "list_servers",
                        lambda enabled_only=False: (_ for _ in ()).throw(RuntimeError("boom")))
    assert av._local_image_available() is False
