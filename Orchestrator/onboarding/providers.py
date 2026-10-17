"""Declarative registry of the .env keys onboarding may write.

`.env` is blackbox.service's EnvironmentFile and config.py load_dotenv()s it,
so a key written there reaches the service process and every child at the
next restart. POST /onboarding/save used to accept any name that LOOKED like
an env var (PATH, LD_PRELOAD, PYTHONPATH, OPENAI_BASE_URL ...): code execution
or key exfiltration from anything that could reach the endpoint (audit
O16-onboarding-update-03, X03-security-backend-13).

ONE table now answers, per key: may /save write it, is it a credential
(redacted on read, removable via DELETE /onboarding/config/{key}), which
provider validates it, does /restart-status track it, and does it satisfy the
"have an LLM key" check. onboarding_routes (write allowlist, credential
allowlist, _VALIDATE_KEY_ENV, current-config providers, restart drift checks)
and status_rollup derive from it, so a new provider is one entry here instead
of six hand-copied tables (O16-onboarding-update-13).

A new key the wizard writes MUST be added here, or /save returns 400 for it
(test_onboarding_env_registry scans Portal/onboarding/steps/*.js for drift).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EnvKey:
    name: str                       # the .env variable
    section: str                    # wizard step that owns it
    provider: str | None = None     # validator / validated_at id
    kind: str = "pref"              # "api_key" | "oauth" | "path" | "pref"
    secret: bool = False            # credential: redacted on read, DELETE-able
    saveable: bool = True           # POST /onboarding/save may write it
    drift_tracked: bool = False     # config.py snapshots it at import
    llm: bool = False               # satisfies the api_keys "have an LLM key"


ENV_KEYS: tuple[EnvKey, ...] = (
    # API Keys step — LLM providers (config.py snapshots these at import).
    EnvKey("OPENAI_API_KEY", "api_keys", "openai", "api_key", secret=True, drift_tracked=True, llm=True),
    EnvKey("ANTHROPIC_API_KEY", "api_keys", "anthropic", "api_key", secret=True, drift_tracked=True, llm=True),
    EnvKey("GOOGLE_API_KEY", "api_keys", "google", "api_key", secret=True, drift_tracked=True, llm=True),
    EnvKey("XAI_API_KEY", "api_keys", "xai", "api_key", secret=True, drift_tracked=True, llm=True),
    EnvKey("PERPLEXITY_API_KEY", "api_keys", "perplexity", "api_key", secret=True, drift_tracked=True, llm=True),
    # API Keys step — reranker upgrade keys (rerank.py reads them fresh, .env first).
    EnvKey("VOYAGE_API_KEY", "api_keys", "voyage", "api_key", secret=True),
    EnvKey("COHERE_API_KEY", "api_keys", "cohere", "api_key", secret=True),
    # Extras step (elevenlabs/client.py reads its key fresh, .env first).
    EnvKey("ELEVENLABS_API_KEY", "optional_integrations", "elevenlabs", "api_key", secret=True),
    EnvKey("GOOGLE_OAUTH_CLIENT_ID", "optional_integrations", "gmail", "oauth", secret=True, drift_tracked=True),
    EnvKey("GOOGLE_OAUTH_CLIENT_SECRET", "optional_integrations", "gmail", "oauth", secret=True, drift_tracked=True),
    # Service-account path: written ONLY by the credentials upload route, which
    # checks the file it points at. /save must never point it at an arbitrary path.
    EnvKey("GOOGLE_APPLICATION_CREDENTIALS", "optional_integrations", kind="path",
           saveable=False, drift_tracked=True),
    # Preferences (not secrets).
    EnvKey("BLACKBOX_TAILNET_HOSTNAME", "tailscale", drift_tracked=True),
    EnvKey("DEFAULT_OPERATOR", "operator"),
    EnvKey("STT_PROVIDER", "transcription"),
    EnvKey("STT_OPENAI_FILE", "transcription"),
    EnvKey("STT_GOOGLE_MODEL", "transcription"),
    EnvKey("WEB_SEARCH_ENABLED", "web_search"),
    EnvKey("WEB_SEARCH_DEFAULT", "web_search"),
    EnvKey("IMAGE_ENABLED", "image"),
    EnvKey("IMAGE_DEFAULT", "image"),
)

BY_NAME: dict[str, EnvKey] = {k.name: k for k in ENV_KEYS}

# What POST /onboarding/save may write.
SAVEABLE_KEYS: frozenset[str] = frozenset(k.name for k in ENV_KEYS if k.saveable)
# Credentials: GET /onboarding/config/{key} (redacted) and DELETE may touch these.
CREDENTIAL_KEYS: frozenset[str] = frozenset(k.name for k in ENV_KEYS if k.secret)
# provider id -> env var, for providers validated with a single api_key.
API_KEY_ENV: dict[str, str] = {k.provider: k.name for k in ENV_KEYS if k.kind == "api_key"}
# The api_keys section's provider cards (status rollup items).
API_KEYS_SECTION_ENV: dict[str, str] = {
    k.provider: k.name for k in ENV_KEYS if k.kind == "api_key" and k.section == "api_keys"}
# Any one of these satisfies the "have an LLM key" requirement.
LLM_KEY_ENV: tuple[str, ...] = tuple(k.name for k in ENV_KEYS if k.llm)
# Compared against the running config.py constants by /restart-status.
DRIFT_TRACKED: tuple[str, ...] = tuple(k.name for k in ENV_KEYS if k.drift_tracked)


def unsaveable(names) -> list[str]:
    """The names POST /onboarding/save must refuse, sorted."""
    return sorted(n for n in names if n not in SAVEABLE_KEYS)


def read_env_value(name: str) -> str:
    """A key read FRESH: the .env file first (what the wizard just wrote),
    then os.environ (boot-time / systemd-provided). Same order as
    elevenlabs.client.resolve_api_key; /save never mirrors into os.environ."""
    try:
        from dotenv import dotenv_values
        from Orchestrator.onboarding import secrets_writer
        value = dotenv_values(str(secrets_writer.ENV_FILE)).get(name)
    except Exception:
        value = None
    return (value or os.getenv(name) or "").strip()
