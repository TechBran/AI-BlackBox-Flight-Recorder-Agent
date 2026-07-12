"""Tool-availability gate for ToolVault (v1: web-search presence-gating).

Lean-venv-safe: reads .env / config.ini with STDLIB ONLY (never import
Orchestrator.config -- the MCP server's lean venv lacks fastapi). See the
feedback-mcp-lean-venv lesson (resolvers._list_operators uses the same approach).
"""
import os

_ROOT = os.environ.get("BLACKBOX_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Feature registry: each feature carries its enable/default preference env vars,
# a provider->env map, and a keyless-floor set (providers always enabled, no key).
# Extensible to video/music later -- add a feature entry and tag tool schemas with
# x-availability.feature. web_search is the DEFAULT feature (back-compat: shipped
# web tools have no `feature` key and must resolve to web_search).
FEATURES = {
    "web_search": {
        "enabled_pref": "WEB_SEARCH_ENABLED",
        "default_pref": "WEB_SEARCH_DEFAULT",
        "provider_env": {
            "perplexity": "PERPLEXITY_API_KEY", "openai": "OPENAI_API_KEY",
            "gemini": "GOOGLE_API_KEY", "grok": "XAI_API_KEY",
            "grok_x": "XAI_API_KEY", "duckduckgo": None,
        },
        "provider_tool": {
            "perplexity": "perplexity_web_search", "openai": "openai_web_search",
            "gemini": "gemini_web_search", "grok": "grok_web_search",
            "grok_x": "grok_x_search", "duckduckgo": "duckduckgo_web_search",
        },
        "hint_noun": "web search",
        "hint_prefix": "WEB SEARCH GUIDANCE: ",
        "keyless_floor": {"duckduckgo"},   # always-enabled, keyless (preserves current behavior)
    },
    "image": {
        "enabled_pref": "IMAGE_ENABLED",
        "default_pref": "IMAGE_DEFAULT",
        "provider_env": {
            "gemini": "GOOGLE_API_KEY", "openai": "OPENAI_API_KEY", "grok": "XAI_API_KEY",
        },
        "provider_tool": {
            "gemini": "gemini_image", "openai": "openai_image", "grok": "grok_image",
            "local": "local_image",
        },
        "hint_noun": "image generation",
        "hint_prefix": "IMAGE GENERATION GUIDANCE: ",
        "keyless_floor": set(),            # NO free image provider
    },
}

# provider key -> env var that must be present (duckduckgo: none, it is keyless).
# Derived from the web_search feature so there is no dual source of truth; the
# name stays exported (onboarding_routes + others import it).
PROVIDER_ENV = dict(FEATURES["web_search"]["provider_env"])

# provider id -> the ToolVault tool name that implements it. Derived from the
# web_search feature's provider_tool map so there is no dual source of truth; the
# name stays exported (existing importers + tests use it).
PROVIDER_TOOL = dict(FEATURES["web_search"]["provider_tool"])
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
    # process env overrides .env for the keys we care about: every feature's
    # provider env vars + enable/default prefs, plus the GEMINI_API_KEY alias.
    keys = {"GEMINI_API_KEY"}
    for spec in FEATURES.values():
        keys.update(v for v in spec["provider_env"].values() if v)
        keys.add(spec["enabled_pref"])
        keys.add(spec["default_pref"])
    for k in keys:
        if k and os.environ.get(k):
            env[k] = os.environ[k]
    # Gemini key alias: the executor uses GEMINI_API_KEY (config derives it as
    # GEMINI_API_KEY or GOOGLE_API_KEY), so the gemini gate -- keyed on
    # GOOGLE_API_KEY -- must also pass when ONLY GEMINI_API_KEY is set. Mirror
    # that fallback so gate-availability matches the executor's effective key.
    if env.get("GEMINI_API_KEY") and not env.get("GOOGLE_API_KEY"):
        env["GOOGLE_API_KEY"] = env["GEMINI_API_KEY"]
    return env


def _local_image_available() -> bool:
    """True iff an enabled custom server hosts a name-matched image model.

    Lazy import + fail-soft: a heavy/absent dep in the lean MCP venv must NOT
    raise here (it would break enabled_providers for EVERY tool, per the
    feedback-mcp-lean-venv lesson). Any failure -> False (local image simply off
    in that context)."""
    try:
        from Orchestrator.onboarding.custom_servers import has_modality_model
        return has_modality_model("image")   # persisted-map-aware (wizard-confirmed wins)
    except Exception:
        return False


def enabled_providers(feature: str = "web_search") -> set:
    """Resolve the enabled provider set for ``feature``.

    Explicit ``<FEATURE>_ENABLED`` pref wins for the env-keyed cloud providers;
    otherwise default to every provider whose key is present, plus the feature's
    keyless floor (web_search -> duckduckgo; image -> none). The image feature
    ADDITIONALLY includes the registry-gated 'local' provider whenever a custom
    server hosts an image model -- additive regardless of the pref, since local is
    governed by the custom-server registry, not the cloud enable list."""
    spec = FEATURES[feature]
    env = _read_env()
    raw = (env.get(spec["enabled_pref"]) or "").strip()
    if raw:
        enabled = {p.strip() for p in raw.split(",") if p.strip()}
    else:
        enabled = set(spec["keyless_floor"])
        for prov, key in spec["provider_env"].items():
            if key and env.get(key):
                enabled.add(prov)
    # Registry-gated providers (no env key): the local image server is enabled iff
    # a registered+enabled custom server hosts an image model -- ADDITIVE even under
    # an explicit IMAGE_ENABLED, so configuring cloud image providers in the wizard
    # can't accidentally disable local image generation.
    if feature == "image" and _local_image_available():
        enabled.add("local")
    return enabled


def enabled_web_search_providers() -> set:
    """Back-compat alias (existing callers + tests use this name)."""
    return enabled_providers("web_search")


def is_available(entry: dict, enabled: set = None, env: dict = None) -> bool:
    gate = entry.get("x-availability")
    if not gate:
        return True  # ungated tools are always available
    if env is None:
        env = _read_env()
    if enabled is None:
        # Resolve the enabled set against THIS entry's feature (default
        # web_search -> shipped web tools, which carry no `feature` key).
        feature = gate.get("feature", "web_search")
        enabled = enabled_providers(feature)
    for k in (gate.get("requires_env") or []):
        if not env.get(k):
            return False
    return gate.get("provider") in enabled


def filter_available(entries: list, ctx=None) -> list:
    # A mixed catalog (image + web tools) needs DIFFERENT enabled sets per
    # feature, so we must NOT pass a single enabled set. Resolve env once, then
    # let is_available() resolve the per-feature enabled set for each entry.
    # Per-feature enabled sets are memoized so we only compute each once.
    env = _read_env()
    _enabled_cache: dict = {}

    def _available(e: dict) -> bool:
        gate = e.get("x-availability")
        if not gate:
            return True
        feature = gate.get("feature", "web_search")
        if feature not in _enabled_cache:
            _enabled_cache[feature] = enabled_providers(feature)
        return is_available(e, _enabled_cache[feature], env)

    return [e for e in entries if _available(e)]


def default_provider_hint(tool_names, feature: str) -> str:
    """Return a one-paragraph default-provider guidance hint for the system
    prompt, or "" if no tool of ``feature`` is in ``tool_names``. Built from the
    feature's default-preference env var (e.g. WEB_SEARCH_DEFAULT / IMAGE_DEFAULT)
    + the provided injected tool set. Stdlib-only (lean-venv-safe).

    Nudges toward the operator's preferred default provider while leaving the
    others available to compare. ``feature`` is a key of ``FEATURES``."""
    spec = FEATURES[feature]
    provider_tool = spec["provider_tool"]
    of_feature = set(provider_tool.values())
    present = [t for t in tool_names if t in of_feature]
    if not present:
        return ""
    noun = spec["hint_noun"]
    env = _read_env()
    default_provider = (env.get(spec["default_pref"]) or "").strip()
    default_tool = provider_tool.get(default_provider)
    parts = []
    if default_tool and default_tool in present:
        parts.append(f"For {noun}, prefer `{default_tool}`.")
    else:
        parts.append(f"Several {noun} tools are available.")
    # Cross-check sentence when more than one of-feature tool is injected.
    # web_search keeps its historical wording ("cross-check results") and counts
    # the X/Twitter-specific tool separately; other features use "compare".
    if feature == "web_search":
        if len([t for t in present if t != "grok_x_search"]) > 1:
            parts.append("Other web search engines are available too \u2014 you may run more than one to cross-check results.")
        if "grok_x_search" in present:
            parts.append("Use `grok_x_search` for real-time X (Twitter) discussion.")
    else:
        if len(present) > 1:
            parts.append(f"Other {noun} providers are available too \u2014 you may run more than one to compare.")
    return spec["hint_prefix"] + " ".join(parts)


def default_web_search_hint(tool_names) -> str:
    """Back-compat alias -> default_provider_hint(tool_names, "web_search").

    Kept so existing callers + tests using this name (and the byte-identical web
    hint output) continue to work unchanged."""
    return default_provider_hint(tool_names, "web_search")


def default_provider_tool(feature: str = "web_search") -> str:
    """Resolve a RUNNABLE provider tool name for ``feature``.

    The configured default (``<FEATURE>_DEFAULT``) wins IF its key is present;
    else the first keyed synthesized-answer provider; else the keyless floor tool.
    ALWAYS returns a tool whose key requirement is satisfied (web_search ->
    duckduckgo_web_search at worst), so an explicit ``<FEATURE>_ENABLED`` override
    that names a key-less provider can't yield an unrunnable tool. Used by the
    on-device /local/tools/execute `web_search` alias so the phone model's headless
    search resolves to the operator's default without a catalog `web_search` tool
    (which would leak a generic alias into every surface's discovery). Stdlib-only."""
    spec = FEATURES[feature]
    env = _read_env()
    provider_tool = spec["provider_tool"]
    provider_env = spec["provider_env"]

    def _keyed(prov: str) -> bool:
        key = provider_env.get(prov)
        return key is None or bool(env.get(key))  # key is None => keyless provider

    default = (env.get(spec["default_pref"]) or "").strip()
    if default in provider_tool and _keyed(default):
        return provider_tool[default]
    # Prefer a concise synthesized-answer provider that is keyed (best for a small
    # on-device context window) over a raw-snippet provider.
    for prov in ("perplexity", "gemini", "openai", "grok"):
        if prov in provider_tool and _keyed(prov):
            return provider_tool[prov]
    for prov in spec["keyless_floor"]:
        if prov in provider_tool:
            return provider_tool[prov]
    return next(iter(provider_tool.values()))
