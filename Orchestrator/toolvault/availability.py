"""Tool-availability gate for ToolVault (v1: web-search presence-gating).

Lean-venv-safe: reads .env / config.ini with STDLIB ONLY (never import
Orchestrator.config -- the MCP server's lean venv lacks fastapi). See the
feedback-mcp-lean-venv lesson (resolvers._list_operators uses the same approach).
"""
import os

_ROOT = os.environ.get("BLACKBOX_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# provider key -> env var that must be present (duckduckgo: none, it is keyless)
PROVIDER_ENV = {
    "perplexity": "PERPLEXITY_API_KEY", "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY", "grok": "XAI_API_KEY",
    "grok_x": "XAI_API_KEY", "duckduckgo": None,
}


def _read_env() -> dict:
    env = {}
    p = os.path.join(_ROOT, ".env")
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    # process env overrides .env for the keys we care about
    for k in list(PROVIDER_ENV.values()) + ["WEB_SEARCH_ENABLED", "WEB_SEARCH_DEFAULT"]:
        if k and os.environ.get(k):
            env[k] = os.environ[k]
    return env


def enabled_web_search_providers() -> set:
    env = _read_env()
    raw = (env.get("WEB_SEARCH_ENABLED") or "").strip()
    if raw:
        return {p.strip() for p in raw.split(",") if p.strip()}
    # Unset -> sensible default: every provider whose key is present, + duckduckgo
    enabled = {"duckduckgo"}
    for prov, key in PROVIDER_ENV.items():
        if key and env.get(key):
            enabled.add(prov)
    return enabled


def is_available(entry: dict, enabled: set = None, env: dict = None) -> bool:
    gate = entry.get("x-availability")
    if not gate:
        return True  # ungated tools are always available
    if env is None:
        env = _read_env()
    if enabled is None:
        enabled = enabled_web_search_providers()
    for k in (gate.get("requires_env") or []):
        if not env.get(k):
            return False
    return gate.get("provider") in enabled


def filter_available(entries: list, ctx=None) -> list:
    enabled = enabled_web_search_providers()
    env = _read_env()
    return [e for e in entries if is_available(e, enabled, env)]
