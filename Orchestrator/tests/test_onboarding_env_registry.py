"""One registry of the .env keys onboarding may write (audit O16-onboarding-update-03,
-13, X03-security-backend-13) and no plaintext key reveal (X03-security-backend-04,
-12, O20-mcp-server-01).

- POST /onboarding/save writes ONLY registry keys; anything else (PATH, LD_*,
  PYTHON*, NODE_OPTIONS, *_BASE_URL, BLACKBOX_ROOT, ...) is a 400 and nothing
  is written. `.env` is blackbox.service's EnvironmentFile, so a free write is
  code execution (LD_PRELOAD) or key exfiltration (OPENAI_BASE_URL).
- GET /onboarding/config/{key} never returns plaintext. The old ?reveal=true
  path trusted a loopback peer, and every tailscale-serve request and every
  fetch the box makes for a model or MCP caller IS a loopback peer.
- The credential allowlist, validator key map, current-config providers,
  restart drift checks and the status rollup tables all derive from
  Orchestrator/onboarding/providers.py, and the wizard's /save callers only
  use registry keys (scanned from Portal/onboarding/steps/*.js).

Hermetic: secrets_writer.ENV_FILE points at a tmp file; the tool-embedding heal
that /save kicks is stubbed (it would otherwise embed over the network).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.onboarding import providers, secrets_writer, status_rollup
from Orchestrator.routes import onboarding_routes as ob

REPO = Path(__file__).resolve().parents[2]
FAKE_KEY = "fake-anthropic-key-for-tests-wxyz"   # obviously fake


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\nVOYAGE_API_KEY=pa-test-1234abcd\n")
    path.chmod(0o600)
    monkeypatch.setattr(secrets_writer, "ENV_FILE", path)
    return path


@pytest.fixture(autouse=True)
def no_heal(monkeypatch):
    from Orchestrator.toolvault import embeddings as tv_emb
    monkeypatch.setattr(tv_emb, "sync_tool_embeddings_bg", lambda reason="": None)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ob.router)
    # The peer every tailscale-serve / on-box request presents.
    return TestClient(app, client=("127.0.0.1", 50000))


# ------------------------------------------------------------- no plaintext

@pytest.mark.parametrize("query", ["?reveal=1", "?reveal=true", "?reveal=yes", "?reveal="])
def test_reveal_from_loopback_returns_no_plaintext(client, env_file, query):
    r = client.get(f"/onboarding/config/ANTHROPIC_API_KEY{query}")
    assert FAKE_KEY not in r.text
    assert r.status_code == 410


def test_reveal_with_forwarded_headers_returns_no_plaintext(client, env_file):
    r = client.get("/onboarding/config/ANTHROPIC_API_KEY?reveal=true",
                   headers={"X-Forwarded-For": "100.64.0.5",
                            "Tailscale-User-Login": "someone@example.com"})
    assert FAKE_KEY not in r.text


def test_config_get_is_redacted(client, env_file):
    r = client.get("/onboarding/config/ANTHROPIC_API_KEY")
    assert r.status_code == 200
    body = r.json()
    assert body["present"] is True
    assert body["value"].endswith(FAKE_KEY[-4:])
    assert FAKE_KEY not in r.text


@pytest.mark.parametrize("key", ["ELEVENLABS_API_KEY", "VOYAGE_API_KEY", "COHERE_API_KEY"])
def test_every_validated_credential_is_manageable(client, env_file, key):
    """These were saved + validated but 403'd on GET/DELETE /config/{key}."""
    assert client.get(f"/onboarding/config/{key}").status_code == 200


def test_config_get_refuses_non_credential_keys(client, env_file):
    for key in ("PATH", "DEFAULT_OPERATOR", "BLACKBOX_TAILNET_HOSTNAME", "LD_PRELOAD"):
        assert client.get(f"/onboarding/config/{key}").status_code == 403


def test_delete_cannot_remove_non_secret_prefs(client, env_file):
    env_file.write_text("DEFAULT_OPERATOR=alice\n")
    assert client.delete("/onboarding/config/DEFAULT_OPERATOR").status_code == 403
    assert "DEFAULT_OPERATOR=alice" in env_file.read_text()


def test_delete_removes_a_credential(client, env_file):
    r = client.delete("/onboarding/config/VOYAGE_API_KEY")
    assert r.status_code == 200
    assert "VOYAGE_API_KEY" not in env_file.read_text()


# ------------------------------------------------------------ write allowlist

HOSTILE_KEYS = [
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONSTARTUP",
    "PYTHONHOME", "NODE_OPTIONS", "DYLD_INSERT_LIBRARIES", "BROWSER", "HOME",
    "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "OLLAMA_BASE_URL", "BLACKBOX_ROOT",
    "HTTPS_PROXY", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",   # written ONLY by the credentials upload route
    "SOME_RANDOM_KEY",
]


@pytest.mark.parametrize("key", HOSTILE_KEYS)
def test_save_rejects_keys_outside_the_registry(client, env_file, key):
    before = env_file.read_text()
    r = client.post("/onboarding/save", json={"secrets": {key: "/tmp/x"}})
    assert r.status_code == 400, r.text
    assert key in r.json()["detail"]
    assert env_file.read_text() == before


def test_save_with_one_bad_key_writes_nothing(client, env_file):
    before = env_file.read_text()
    r = client.post("/onboarding/save", json={"secrets": {
        "OPENAI_API_KEY": "sk-test-new", "OPENAI_BASE_URL": "https://attacker.example/v1"}})
    assert r.status_code == 400
    assert env_file.read_text() == before


WIZARD_WRITES = {
    "OPENAI_API_KEY": "sk-test", "ANTHROPIC_API_KEY": "sk-ant-test",
    "GOOGLE_API_KEY": "test-google", "XAI_API_KEY": "xai-test",
    "PERPLEXITY_API_KEY": "pplx-test", "VOYAGE_API_KEY": "pa-test",
    "COHERE_API_KEY": "co-test", "ELEVENLABS_API_KEY": "el-test",
    "GOOGLE_OAUTH_CLIENT_ID": "id.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "test-secret",
    "BLACKBOX_TAILNET_HOSTNAME": "box.example.ts.net",
    "DEFAULT_OPERATOR": "alice",
    "STT_PROVIDER": "google", "STT_OPENAI_FILE": "gpt-4o-transcribe",
    "STT_GOOGLE_MODEL": "chirp_2",
    "WEB_SEARCH_ENABLED": "duckduckgo,perplexity", "WEB_SEARCH_DEFAULT": "duckduckgo",
    "IMAGE_ENABLED": "gemini", "IMAGE_DEFAULT": "gemini",
}


@pytest.mark.parametrize("key,value", sorted(WIZARD_WRITES.items()))
def test_save_accepts_every_key_the_wizard_writes(client, env_file, key, value):
    r = client.post("/onboarding/save", json={"secrets": {key: value}})
    assert r.status_code == 200, r.text
    assert f"{key}={value}\n" in env_file.read_text()


def test_wizard_save_callers_only_use_registry_keys():
    """Drift guard: every key a wizard step POSTs to /onboarding/save (literal
    payloads + the api_keys PROVIDERS envVar table) is saveable."""
    steps = REPO / "Portal" / "onboarding" / "steps"
    found: set[str] = set()
    for js in steps.glob("*.js"):
        src = js.read_text()
        if "/onboarding/save" not in src:
            continue
        found |= set(re.findall(r"secrets\.([A-Z][A-Z0-9_]+)\s*=", src))
        found |= set(re.findall(r"envVar:\s*\"([A-Z][A-Z0-9_]+)\"", src))
        for block in re.findall(r"secrets:\s*\{([^}]*)\}", src):
            found |= set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\s*:", block))
    assert found, "scanner found no wizard /save keys — the regexes rotted"
    unknown = sorted(found - providers.SAVEABLE_KEYS)
    assert unknown == [], f"wizard writes keys the registry rejects: {unknown}"
    assert set(WIZARD_WRITES) <= providers.SAVEABLE_KEYS


# ------------------------------------------------------------- derivations

def test_registry_names_are_unique_and_well_formed():
    names = [k.name for k in providers.ENV_KEYS]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", n) for n in names)


def test_no_saveable_key_is_an_interpreter_or_endpoint_override():
    bad = re.compile(r"^(LD_|DYLD_|PYTHON|NODE_|PATH$|HOME$)|_BASE_URL$|_PROXY$")
    assert not [n for n in providers.SAVEABLE_KEYS if bad.search(n)]


def test_validate_key_map_derives_from_registry():
    assert ob._VALIDATE_KEY_ENV == {
        "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
        "google": "GOOGLE_API_KEY", "xai": "XAI_API_KEY",
        "perplexity": "PERPLEXITY_API_KEY", "voyage": "VOYAGE_API_KEY",
        "cohere": "COHERE_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY",
    }
    assert set(ob._VALIDATE_KEY_ENV.values()) <= ob.CREDENTIAL_KEYS


def test_every_api_key_provider_has_a_validator_and_a_request_literal():
    import typing
    from Orchestrator.onboarding import validators
    allowed = set(typing.get_args(ob.ValidateRequest.model_fields["provider"].annotation))
    for provider in providers.API_KEY_ENV:
        assert provider in allowed, provider
        assert callable(getattr(validators, f"validate_{provider}", None)), provider


@pytest.mark.parametrize("provider", sorted(providers.API_KEY_ENV))
def test_validate_dispatches_every_api_key_provider(monkeypatch, provider):
    from Orchestrator.onboarding.validators import ValidationResult
    called = {}

    def _fake(api_key):
        called["api_key"] = api_key
        return ValidationResult(ok=True, latency_ms=1)
    monkeypatch.setattr(ob.validators, f"validate_{provider}", _fake)
    monkeypatch.setattr(ob._state, "record_validation", lambda p: called.setdefault("stamped", p))
    out = ob.validate(ob.ValidateRequest(provider=provider, credentials={"api_key": "k-1"}))
    assert out.ok is True
    assert called == {"api_key": "k-1", "stamped": provider}


def test_credential_allowlist_is_the_registry_secret_set():
    assert ob.CREDENTIAL_KEYS == providers.CREDENTIAL_KEYS
    assert {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY",
            "PERPLEXITY_API_KEY", "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET"} <= ob.CREDENTIAL_KEYS
    assert "DEFAULT_OPERATOR" not in ob.CREDENTIAL_KEYS


def test_current_config_providers_include_reranker_keys(env_file):
    """api_keys.js rehydrates every PROVIDERS card from providers[p.id];
    voyage/cohere were missing, so a saved reranker key looked absent."""
    from dotenv import dotenv_values
    snap = ob._providers_snapshot(dotenv_values(str(env_file)), {"voyage": "2026-01-01"})
    assert snap["voyage"]["present"] is True
    assert snap["voyage"]["last4"].endswith("abcd")
    assert snap["voyage"]["validated_at"] == "2026-01-01"
    assert snap["cohere"]["present"] is False
    for prov in ("openai", "anthropic", "google", "xai", "perplexity", "elevenlabs"):
        assert set(snap[prov]) == {"present", "last4", "validated_at"}
    assert snap["anthropic"]["last4"].endswith(FAKE_KEY[-4:])
    assert FAKE_KEY not in str(snap)
    assert set(snap["gmail"]) == {"present", "client_id", "secret_last4", "validated_at"}


def test_restart_drift_checks_cover_the_same_keys():
    """The same 9 keys the hand-written checks dict compared (order is cosmetic)."""
    assert set(providers.DRIFT_TRACKED) == {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY",
        "PERPLEXITY_API_KEY", "BLACKBOX_TAILNET_HOSTNAME",
        "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
    }


def test_restart_status_uses_the_registry(env_file, monkeypatch):
    from Orchestrator import config as cfg
    env_file.write_text("")
    for name in providers.DRIFT_TRACKED:
        monkeypatch.setattr(cfg, name, "", raising=False)
    assert ob.restart_status().needs_restart is False
    env_file.write_text("BLACKBOX_TAILNET_HOSTNAME=new.example.ts.net\n")
    out = ob.restart_status()
    assert out.needs_restart is True
    assert out.drifted_keys == ["BLACKBOX_TAILNET_HOSTNAME"]


def test_status_rollup_tables_derive_from_registry():
    assert status_rollup._LLM_KEY_ENV == [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
        "XAI_API_KEY", "PERPLEXITY_API_KEY",
    ]
    assert status_rollup._PROVIDER_KEY == {
        "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
        "google": "GOOGLE_API_KEY", "xai": "XAI_API_KEY",
        "perplexity": "PERPLEXITY_API_KEY",
        "voyage": "VOYAGE_API_KEY", "cohere": "COHERE_API_KEY",
    }


def test_read_env_value_prefers_dotenv_then_process_env(env_file, monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-stale-process-value")
    assert providers.read_env_value("VOYAGE_API_KEY") == "pa-test-1234abcd"
    monkeypatch.setenv("COHERE_API_KEY", "co-from-process")
    assert providers.read_env_value("COHERE_API_KEY") == "co-from-process"
    monkeypatch.delenv("COHERE_API_KEY")
    assert providers.read_env_value("COHERE_API_KEY") == ""
