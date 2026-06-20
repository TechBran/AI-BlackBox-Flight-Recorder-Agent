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

# provider id -> the ToolVault tool name that implements it
PROVIDER_TOOL = {
    "perplexity": "perplexity_web_search",
    "openai": "openai_web_search",
    "gemini": "gemini_web_search",
    "grok": "grok_web_search",
    "grok_x": "grok_x_search",
    "duckduckgo": "duckduckgo_web_search",
}
WEB_SEARCH_TOOLS = set(PROVIDER_TOOL.values())


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
    for k in list(PROVIDER_ENV.values()) + ["GEMINI_API_KEY", "WEB_SEARCH_ENABLED", "WEB_SEARCH_DEFAULT"]:
        if k and os.environ.get(k):
            env[k] = os.environ[k]
    # Gemini key alias: the executor uses GEMINI_API_KEY (config derives it as
    # GEMINI_API_KEY or GOOGLE_API_KEY), so the gemini gate -- keyed on
    # GOOGLE_API_KEY -- must also pass when ONLY GEMINI_API_KEY is set. Mirror
    # that fallback so gate-availability matches the executor's effective key.
    if env.get("GEMINI_API_KEY") and not env.get("GOOGLE_API_KEY"):
        env["GOOGLE_API_KEY"] = env["GEMINI_API_KEY"]
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


def default_web_search_hint(tool_names) -> str:
    """Return a one-paragraph web-search guidance hint for the system prompt,
    or "" if no web-search tool is in tool_names. Built from WEB_SEARCH_DEFAULT
    + the provided injected tool set. Stdlib-only (lean-venv-safe)."""
    present = [t for t in tool_names if t in WEB_SEARCH_TOOLS]
    if not present:
        return ""
    env = _read_env()
    default_provider = (env.get("WEB_SEARCH_DEFAULT") or "").strip()
    default_tool = PROVIDER_TOOL.get(default_provider)
    parts = []
    if default_tool and default_tool in present:
        parts.append(f"For web search, prefer `{default_tool}`.")
    else:
        parts.append("Several web search tools are available.")
    if len([t for t in present if t != "grok_x_search"]) > 1:
        parts.append("Other web search engines are available too \u2014 you may run more than one to cross-check results.")
    if "grok_x_search" in present:
        parts.append("Use `grok_x_search` for real-time X (Twitter) discussion.")
    return "WEB SEARCH GUIDANCE: " + " ".join(parts)
