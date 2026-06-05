"""Onboarding backend accepts + exposes the STT provider preference.

Covers the contract the (later-phase) onboarding wizard relies on:
  1. POST /onboarding/save PERSISTS STT_PROVIDER (and the optional model-override
     keys) — i.e. the key reaches the .env writer and is NOT silently dropped by
     any allowlist.  /save is a free-write path (secrets_writer.update_env only
     validates the env-var NAME shape), so this asserts the key isn't filtered.
  2. The wizard can READ the selected provider back via GET
     /onboarding/current-config — which reads .env FRESH (E8 pattern), so the
     value is visible immediately after save without a service restart.

The real .env file is never mutated: the writer is patched.  current-config's
fresh read is exercised by patching dotenv_values to return a synthetic env.
"""
from unittest.mock import patch


def _client():
    import Orchestrator.app  # noqa: F401  -- side-effect: registers onboarding routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


def _written_keys(mock) -> dict:
    """Extract the dict passed to update_env(updates) from the patched call."""
    assert mock.call_args is not None, "update_env was never called"
    if mock.call_args.args:
        return mock.call_args.args[0]
    return mock.call_args.kwargs.get("updates", {})


def test_save_persists_stt_provider():
    """STT_PROVIDER must reach update_env (not dropped by an allowlist)."""
    with patch("Orchestrator.routes.onboarding_routes.update_env") as m:
        m.return_value = {"backup": None, "updated_keys": ["STT_PROVIDER"]}
        r = _client().post("/onboarding/save", json={"secrets": {"STT_PROVIDER": "google"}})
        assert r.status_code == 200, r.text
        written = _written_keys(m)
        assert written.get("STT_PROVIDER") == "google"


def test_save_persists_stt_model_overrides():
    """The optional model-override keys also flow through the free-write path."""
    payload = {
        "secrets": {
            "STT_PROVIDER": "openai",
            "STT_OPENAI_FILE": "gpt-4o-transcribe",
            "STT_GOOGLE_MODEL": "chirp_2",
        }
    }
    with patch("Orchestrator.routes.onboarding_routes.update_env") as m:
        m.return_value = {"backup": None, "updated_keys": list(payload["secrets"])}
        r = _client().post("/onboarding/save", json=payload)
        assert r.status_code == 200, r.text
        written = _written_keys(m)
        for k, v in payload["secrets"].items():
            assert written.get(k) == v, f"{k} was dropped before reaching update_env"


def test_current_config_exposes_selected_stt_provider():
    """Wizard reads the selected provider back via current-config (fresh .env)."""
    fake_env = {"STT_PROVIDER": "google", "STT_GOOGLE_MODEL": "chirp_2"}
    # current-config calls dotenv_values(str(ENV_FILE)) at the import site inside
    # the handler: `from dotenv import dotenv_values`. Patch it at the source.
    with patch("dotenv.dotenv_values", return_value=fake_env):
        r = _client().get("/onboarding/current-config")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "stt" in body, "current-config must expose an stt block"
        assert body["stt"]["provider"] == "google"
        assert body["stt"]["google_model"] == "chirp_2"


def test_current_config_stt_provider_empty_means_auto():
    """Absent STT_PROVIDER surfaces as '' (== auto), never a hard-coded default."""
    with patch("dotenv.dotenv_values", return_value={}):
        r = _client().get("/onboarding/current-config")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stt"]["provider"] == ""
        assert body["stt"]["openai_file"] is None
        assert body["stt"]["google_model"] is None
