"""Onboarding backend accepts + exposes the image-generation provider preferences.

Covers the "image" onboarding step (Task 6 of Multi-Provider Image Generation),
the analog of the shipped web-search step:
  1. "image" is a valid step name -- state.mark_step_complete("image") does NOT
     raise, it is present in state.ALL_STEPS, and it sits AFTER "web_search" and
     before "pair_phone".
  2. Save round-trip: IMAGE_ENABLED + IMAGE_DEFAULT are PREFERENCES (not secrets);
     they reach the .env writer (no allowlist drops them) and current-config
     reflects them WITHOUT a restart (availability live-reads .env).
  3. Default-when-unset: with provider keys present but IMAGE_ENABLED unset, the
     enabled set is every keyed provider (image has NO keyless floor).

Hermeticity: current_config() reads .env via dotenv_values(str(ENV_FILE)), while
availability.enabled_providers("image") reads via its OWN availability._read_env()
(stdlib, independent of dotenv -- lean-venv-safe). So we point BOTH at the same tmp
.env: monkeypatch secrets_writer.ENV_FILE AND availability._ROOT (and clear any
process-env overrides availability honors). The real .env is never touched.
"""
import pytest


def _client():
    import Orchestrator.app  # noqa: F401  -- side-effect: registers onboarding routes
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Point BOTH the onboarding .env writer and the availability reader at a
    single tmp .env, and clear process-env overrides that availability honors.

    Returns a (env_file, write) pair; write() persists a mapping via update_env.
    """
    from Orchestrator.onboarding import secrets_writer
    from Orchestrator.toolvault import availability

    env_file = tmp_path / ".env"
    env_file.write_text("")

    # 1) onboarding writer + dotenv reads target this file
    monkeypatch.setattr(secrets_writer, "ENV_FILE", env_file)
    # 2) availability._read_env() reads {_ROOT}/.env via stdlib
    monkeypatch.setattr(availability, "_ROOT", str(tmp_path))
    # 3) availability also lets process-env override .env for the keys it cares
    #    about -- clear those so the file is authoritative in the test. Cover the
    #    image feature's provider keys + its enable/default prefs (+ the gemini
    #    GEMINI_API_KEY alias availability honors).
    img_keys = list(availability.FEATURES["image"]["provider_env"].values())
    for k in img_keys + ["IMAGE_ENABLED", "IMAGE_DEFAULT", "GEMINI_API_KEY"]:
        if k:
            monkeypatch.delenv(k, raising=False)

    def write(mapping: dict):
        secrets_writer.update_env(mapping)

    return env_file, write


def test_image_is_a_valid_step_name():
    """mark_step_complete('image') must not raise; it's in ALL_STEPS after
    web_search and before pair_phone."""
    from Orchestrator.onboarding import state
    assert "image" in state.ALL_STEPS
    # Position: after "web_search", before "pair_phone".
    assert state.ALL_STEPS.index("image") == state.ALL_STEPS.index("web_search") + 1
    assert state.ALL_STEPS.index("image") < state.ALL_STEPS.index("pair_phone")
    # mark_step_complete validates against ALL_STEPS and raises ValueError on unknown.
    state.get_state().mark_step_complete("image")  # no exception == pass


def test_save_roundtrip_enabled_and_default(tmp_env):
    """Write the two preference keys via the save path, then current-config
    reflects default == 'gemini', gemini enabled, grok NOT enabled (not in the
    IMAGE_ENABLED list, and no XAI key in the tmp env)."""
    _env_file, write = tmp_env
    write({
        "IMAGE_ENABLED": "gemini,openai",
        "IMAGE_DEFAULT": "gemini",
    })

    r = _client().get("/onboarding/current-config")
    assert r.status_code == 200, r.text
    img = r.json()["image"]

    assert img["default"] == "gemini"
    assert img["providers"]["gemini"]["enabled"] is True
    assert img["providers"]["openai"]["enabled"] is True
    assert img["providers"]["grok"]["enabled"] is False  # not in IMAGE_ENABLED
    assert sorted(img["enabled"]) == ["gemini", "openai"]


def test_default_when_enabled_unset_includes_only_keyed_providers(tmp_env):
    """With provider keys present and IMAGE_ENABLED unset, the enabled set is
    every keyed provider -- image has NO keyless floor, so a provider with no
    key is never auto-enabled."""
    _env_file, write = tmp_env
    # Two keyed providers present; IMAGE_ENABLED deliberately NOT written.
    write({
        "GOOGLE_API_KEY": "google-test",   # keys gemini
        "OPENAI_API_KEY": "sk-openai-test",  # keys openai
    })

    r = _client().get("/onboarding/current-config")
    assert r.status_code == 200, r.text
    img = r.json()["image"]

    # default unset -> ""
    assert img["default"] == ""
    # key_present reflects the tmp env
    assert img["providers"]["gemini"]["key_present"] is True
    assert img["providers"]["openai"]["key_present"] is True
    assert img["providers"]["grok"]["key_present"] is False  # no XAI_API_KEY
    # enabled set: keyed providers only (NO keyless floor for image)
    enabled = set(img["enabled"])
    assert enabled == {"gemini", "openai"}
    # a provider with NO key present is NOT auto-enabled
    assert "grok" not in enabled
    assert img["providers"]["grok"]["enabled"] is False
