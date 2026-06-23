"""Canonical onboarding status rollup (M1).

Single source of truth for:
  * SECTIONS — the 10 hub sections (welcome/done are the hub itself, excluded).
  * GROUP_LABELS — display label per group.
  * The provider->section join + attention-derivation rules.

build_status(env, state, embeddings, cli, web_search, image, paired, operators,
restart) is PURE: it takes already-read snapshots and derives state from
PERSISTED data only — zero subprocess/provider/tailscale probes. The route layer
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
    {"key": "operator",               "group": "identity",     "label": "Operators",   "required": True},
]
# step == key (the hub links ?step=<key>); set it here so callers never re-derive.
for _s in SECTIONS:
    _s["step"] = _s["key"]

SECTION_BY_KEY = {s["key"]: s for s in SECTIONS}
