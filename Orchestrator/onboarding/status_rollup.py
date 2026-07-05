"""Canonical onboarding status rollup (M1).

Single source of truth for:
  * SECTIONS — the 10 hub sections (welcome/done are the hub itself, excluded).
  * GROUP_LABELS — display label per group.
  * The provider->section join + attention-derivation rules.

build_status(*, env, state, embeddings, cli, web_search, image, paired,
operators, restart, is_complete=False) is PURE (keyword-only): it takes
already-read snapshots and derives state from PERSISTED data only — zero
subprocess/provider/tailscale probes. The route layer
(onboarding_routes.py) reads those snapshots cheaply (dotenv_values + persisted
state + GET-able persisted dicts) and passes them in. The SSE stream layer adds
the live probes on top.
"""
from __future__ import annotations

# State vocabulary — the ONLY four legal section states.
READY = "ready"
ATTENTION = "attention"
OPTIONAL = "optional"
CHECKING = "checking"

GROUP_LABELS = {
    "network": "Network & Access",
    "keys": "Keys & Models",
    "capabilities": "Capabilities",
    "identity": "Identity",
}

# The 10 sections, in ALL_STEPS order minus welcome/done. step == key always.
SECTIONS: list[dict] = [
    {"key": "tailscale",              "group": "network",      "label": "Tailnet",     "required": False},
    {"key": "api_keys",               "group": "keys",         "label": "API Keys",    "required": True},
    {"key": "embeddings",             "group": "keys",         "label": "Memory",      "required": True},
    {"key": "optional_integrations",  "group": "capabilities", "label": "Extras",      "required": False},
    {"key": "transcription",          "group": "capabilities", "label": "Speech",      "required": False},
    {"key": "web_search",             "group": "capabilities", "label": "Web Search",  "required": False},
    {"key": "image",                  "group": "capabilities", "label": "Image",       "required": False},
    {"key": "pair_phone",             "group": "network",      "label": "Pair Phone",  "required": False},
    {"key": "cli_agents",             "group": "capabilities", "label": "Agents",      "required": False},
    {"key": "mcp",                    "group": "network",      "label": "MCP Server",  "required": False},
    {"key": "operator",               "group": "identity",     "label": "Operators",   "required": True},
]
# step == key (the hub links ?step=<key>); set it here so callers never re-derive.
for _s in SECTIONS:
    _s["step"] = _s["key"]

SECTION_BY_KEY = {s["key"]: s for s in SECTIONS}


# Provider key env-vars that satisfy the api_keys section (the LLM provider set;
# matches onboarding_routes.current_config's provider list, minus gmail/elevenlabs
# which are surfaced under their own capability sections).
_LLM_KEY_ENV = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "XAI_API_KEY", "PERPLEXITY_API_KEY",
]
# provider id (validated_at key) -> its env var, for the present-but-unvalidated check.
# Voyage/Cohere are the M10 reranker upgrade keys — they live in the API-Keys
# step alongside the LLM keys, so the rollup tracks them the same way (a
# present-but-unvalidated key nudges the operator to click Validate). They are
# NOT in _LLM_KEY_ENV: a reranker key alone does not satisfy the "have an LLM
# key" requirement, so their absence never trips the "No API keys" attention.
_PROVIDER_KEY = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY", "xai": "XAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "voyage": "VOYAGE_API_KEY", "cohere": "COHERE_API_KEY",
}


def _present_keys(env: dict) -> list[str]:
    return [k for k in _LLM_KEY_ENV if (env.get(k) or "").strip()]


def _derive_api_keys(env, state):
    present = _present_keys(env)
    validated = state.get("validated_at", {}) or {}
    items = [
        {"key": prov, "label": prov,
         "configured": bool((env.get(var) or "").strip()),
         "validated_at": validated.get(prov)}
        for prov, var in _PROVIDER_KEY.items()
    ]
    if not present:
        return ATTENTION, "No API keys configured", items, [
            {"severity": "warn", "message": "No LLM API key configured — chat will not work"}]
    # present-but-unvalidated: any present key whose provider has no validated_at stamp
    unvalidated = [prov for prov, var in _PROVIDER_KEY.items()
                   if (env.get(var) or "").strip() and prov not in validated]
    if unvalidated:
        return ATTENTION, f"{len(present)} key(s); {len(unvalidated)} unvalidated", items, [
            {"severity": "warn",
             "message": f"Key present but never validated: {', '.join(unvalidated)}"}]
    return READY, f"{len(present)} key(s) validated", items, []


def _derive_operator(operators):
    items = [{"key": op, "label": op, "configured": True, "validated_at": None}
             for op in operators]
    if not operators:
        return ATTENTION, "No operators configured", items, [
            {"severity": "warn", "message": "Add at least one operator"}]
    return READY, f"{len(operators)} operator(s)", items, []


# Dispatch table: key -> deriver returning (state, summary, items, attention_list).
# Sections not yet wired fall back to the optional/persisted default.
def _derive_default(section, env, state):
    """Persisted fallback: completed -> ready, skipped -> optional, else optional."""
    completed = set(state.get("completed_steps", []))
    skipped = set(state.get("skipped_steps", []))
    key = section["key"]
    if key in completed:
        return READY, "Configured", [], []
    if key in skipped:
        return OPTIONAL, "Skipped", [], []
    if section["required"]:
        return ATTENTION, "Not configured", [], [
            {"severity": "warn", "message": f"{section['label']} required"}]
    return OPTIONAL, "Not set up", [], []


def _derive_embeddings(embeddings):
    active = embeddings.get("active")
    health = embeddings.get("health") or {}
    hstate = health.get("state", "ok")
    items = [{"key": "active", "label": active or "(none)",
              "configured": bool(active), "validated_at": None}]
    if not active:
        return ATTENTION, "No memory model active", items, [
            {"severity": "warn", "message": "Memory index not initialized"}]
    if hstate == "broken":
        return ATTENTION, "Memory index broken", items, [
            {"severity": "error",
             "message": f"Memory health broken: {health.get('detail', '')}".strip()}]
    if hstate == "superseded":
        succ = health.get("successor") or "a newer model"
        return ATTENTION, f"Upgrade available → {succ}", items, [
            {"severity": "warn", "message": f"Memory model superseded by {succ}"}]
    # caught-up check against the active store's `missing` count
    behind = 0
    for store in (embeddings.get("stores") or []):
        if store.get("slug") == active:
            behind = store.get("missing") or 0
            break
    if behind:
        return ATTENTION, f"{behind} snapshot(s) behind", items, [
            {"severity": "warn", "message": f"Memory index {behind} snapshot(s) behind"}]
    return READY, f"Active: {active}", items, []


def _derive_feature(feature, label):
    """Shared deriver for web_search + image (same enabled/key shape)."""
    enabled = feature.get("enabled") or []
    providers = feature.get("providers") or {}
    items = [{"key": p, "label": p, "configured": meta.get("enabled", False),
              "validated_at": None} for p, meta in providers.items()]
    if not enabled:
        return OPTIONAL, "Not enabled", items, []
    # any enabled provider whose key is absent -> attention
    missing = [p for p in enabled
               if p in providers and not providers[p].get("key_present", False)]
    if missing:
        return ATTENTION, f"{', '.join(missing)} missing key", items, [
            {"severity": "warn",
             "message": f"{label}: {', '.join(missing)} enabled but key missing"}]
    return READY, f"{len(enabled)} provider(s)", items, []


def _derive_cli_agents(cli):
    providers = cli.get("providers") or {}
    installed = {p: m for p, m in providers.items() if m.get("installed")}
    items = [{"key": p, "label": p, "configured": m.get("installed", False),
              "validated_at": None} for p, m in providers.items()]
    if not installed:
        return OPTIONAL, "No agents installed", items, []
    # authenticated may be None (antigravity: implicit) — only False is a blocker
    not_authed = [p for p, m in installed.items() if m.get("authenticated") is False]
    if not_authed:
        return ATTENTION, f"{', '.join(not_authed)} not signed in", items, [
            {"severity": "warn",
             "message": f"Agent installed but not authenticated: {', '.join(not_authed)}"}]
    return READY, f"{len(installed)} agent(s) ready", items, []


def _derive_pair_phone(paired):
    items = [{"key": d.get("name", "device"), "label": d.get("name", "device"),
              "configured": True, "validated_at": None} for d in paired]
    if paired:
        return READY, f"{len(paired)} device(s) paired", items, []
    return OPTIONAL, "No phone paired", items, []


def _derive_tailscale(env, state):
    """FAST read: persisted-only. Never probes. Live state arrives via SSE."""
    validated = (state.get("validated_at", {}) or {}).get("tailscale")
    serve_hint = bool((env.get("BLACKBOX_TAILNET_HOSTNAME") or "").strip())
    items = [{"key": "tailnet", "label": env.get("BLACKBOX_TAILNET_HOSTNAME") or "(unset)",
              "configured": serve_hint, "validated_at": validated}]
    if not validated:
        return OPTIONAL, "Not connected", items, []
    if not serve_hint:
        return ATTENTION, "HTTPS serve not set", items, [
            {"severity": "warn",
             "message": "Tailnet up but serve/HTTPS not set — phone pairing will fail"}]
    return READY, "Connected", items, []


def _derive_mcp(mcp):
    """MCP remote server. Fast path gets only {tokens_present}; the SSE live probe
    adds mcp_up/funnel_up/oauth_ready. Required=False, so no-token is OPTIONAL."""
    mcp = mcp or {}
    up = mcp.get("mcp_up")          # None in the fast (probe-free) path
    funnel = mcp.get("funnel_up")
    if up is False:
        return ATTENTION, "Server not running", [], [
            {"severity": "warn", "message": "MCP server installed but not running"}]
    if not mcp.get("tokens_present"):
        return OPTIONAL, "No token yet", [], []
    if funnel is False:
        return READY, "Token set \u00b7 not public yet", [], []
    return READY, "Configured", [], []


def build_status(*, env, state, embeddings, cli, web_search, image,
                 paired, operators, restart, mcp=None, rerank=None,
                 is_complete=False):
    """PURE rollup from persisted snapshots. No probes. See module docstring."""
    sections_out = []
    attention_out = []
    skipped = set(state.get("skipped_steps", []))
    for section in SECTIONS:
        key = section["key"]
        if key == "tailscale":
            st, summary, items, atts = _derive_tailscale(env, state)
        elif key == "api_keys":
            st, summary, items, atts = _derive_api_keys(env, state)
        elif key == "operator":
            st, summary, items, atts = _derive_operator(operators)
        elif key == "embeddings":
            st, summary, items, atts = _derive_embeddings(embeddings)
        elif key == "web_search":
            st, summary, items, atts = _derive_feature(web_search, "Web Search")
        elif key == "image":
            st, summary, items, atts = _derive_feature(image, "Image")
        elif key == "cli_agents":
            st, summary, items, atts = _derive_cli_agents(cli)
        elif key == "pair_phone":
            st, summary, items, atts = _derive_pair_phone(paired)
        elif key == "mcp":
            st, summary, items, atts = _derive_mcp(mcp)
        else:
            st, summary, items, atts = _derive_default(section, env, state)
        sections_out.append({
            "key": key, "group": section["group"], "label": section["label"],
            "state": st, "required": section["required"], "summary": summary,
            "step": section["step"], "skipped": key in skipped, "items": items,
        })
        for a in atts:
            attention_out.append({
                "section": key, "severity": a["severity"],
                "message": a["message"], "cta_step": key,
            })
    if restart.get("needs_restart"):
        drifted = restart.get("drifted_keys") or []
        attention_out.append({
            "section": None, "severity": "warn",
            "message": f"Service restart needed — {len(drifted)} setting(s) drifted",
            "cta_step": "api_keys",
        })
    ready_count = sum(1 for s in sections_out if s["state"] == READY)
    return {
        "ready_count": ready_count,
        "total": len(SECTIONS),
        "is_complete": is_complete,
        "sections": sections_out,
        "attention": attention_out,
        # M13 ADDITIVE: verbatim GET /rerank/status block (None when the
        # collection failed) — no section of its own, the wizard's Memory &
        # Search step renders it inside the embeddings step.
        "rerank": rerank,
    }
