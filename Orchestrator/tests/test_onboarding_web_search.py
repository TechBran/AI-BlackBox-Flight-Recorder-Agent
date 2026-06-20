"""Onboarding backend accepts + exposes the web-search provider preferences.

Covers the "web_search" onboarding step (Task 5 of Multi-Provider Web Search):
  1. "web_search" is a valid step name — state.mark_step_complete("web_search")
     does NOT raise, and it is present in state.ALL_STEPS.
  2. Save round-trip: WEB_SEARCH_ENABLED + WEB_SEARCH_DEFAULT are PREFERENCES
     (not secrets); they reach the .env writer (no allowlist drops them) and
     current-config reflects them WITHOUT a restart (availability live-reads .env).
  3. Default-when-unset: with provider keys present but WEB_SEARCH_ENABLED unset,
     the enabled set is every keyed provider + "duckduckgo".

Hermeticity: current_config() reads .env via dotenv_values(str(ENV_FILE)), while
availability.enabled_web_search_providers() reads via its OWN availability._read_env()
(stdlib, independent of dotenv — lean-venv-safe). So we point BOTH at the same tmp
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
    #    about — clear those so the file is authoritative in the test.
    for k in list(availability.PROVIDER_ENV.values()) + ["WEB_SEARCH_ENABLED", "WEB_SEARCH_DEFAULT"]:
        if k:
            monkeypatch.delenv(k, raising=False)

    def write(mapping: dict):
        secrets_writer.update_env(mapping)

    return env_file, write


def test_web_search_is_a_valid_step_name():
    """mark_step_complete('web_search') must not raise, and it's in ALL_STEPS."""
    from Orchestrator.onboarding import state
    assert "web_search" in state.ALL_STEPS
    # Position: after "transcription", before "pair_phone".
    assert state.ALL_STEPS.index("web_search") == state.ALL_STEPS.index("transcription") + 1
    assert state.ALL_STEPS.index("web_search") < state.ALL_STEPS.index("pair_phone")
    # mark_step_complete validates against ALL_STEPS and raises ValueError on unknown.
    state.get_state().mark_step_complete("web_search")  # no exception == pass


def test_save_roundtrip_enabled_and_default(tmp_env):
    """Write the two preference keys via the save path, then current-config
    reflects default == 'perplexity', perplexity enabled, openai NOT enabled
    (no openai key in the tmp env)."""
    _env_file, write = tmp_env
    write({
        "WEB_SEARCH_ENABLED": "perplexity,duckduckgo",
        "WEB_SEARCH_DEFAULT": "perplexity",
    })

    r = _client().get("/onboarding/current-config")
    assert r.status_code == 200, r.text
    ws = r.json()["web_search"]

    assert ws["default"] == "perplexity"
    assert ws["providers"]["perplexity"]["enabled"] is True
    assert ws["providers"]["openai"]["enabled"] is False  # no OPENAI_API_KEY in tmp env
    assert sorted(ws["enabled"]) == ["duckduckgo", "perplexity"]


def test_default_when_enabled_unset_includes_keyed_providers_and_duckduckgo(tmp_env):
    """With provider keys present and WEB_SEARCH_ENABLED unset, the enabled set
    is every keyed provider + 'duckduckgo' (availability's sensible default)."""
    _env_file, write = tmp_env
    # Two keyed providers present; WEB_SEARCH_ENABLED deliberately NOT written.
    write({
        "OPENAI_API_KEY": "sk-openai-test",
        "PERPLEXITY_API_KEY": "pplx-test",
    })

    r = _client().get("/onboarding/current-config")
    assert r.status_code == 200, r.text
    ws = r.json()["web_search"]

    # default unset -> ""
    assert ws["default"] == ""
    # key_present reflects the tmp env
    assert ws["providers"]["openai"]["key_present"] is True
    assert ws["providers"]["perplexity"]["key_present"] is True
    assert ws["providers"]["duckduckgo"]["key_present"] is True  # keyless
    # enabled set: keyed providers + duckduckgo
    enabled = set(ws["enabled"])
    assert "openai" in enabled
    assert "perplexity" in enabled
    assert "duckduckgo" in enabled
    # a provider with NO key present should not be auto-enabled
    assert "grok" not in enabled
    assert ws["providers"]["grok"]["enabled"] is False
