"""The per-turn wellness context (stdlib only, so the MCP server's lean venv
can import it).

A turn is stamped at its entry point, next to _ORIGIN_DEVICE_ID
(Orchestrator/routes/chat/context.py), with who is asking, from where, with
what credential, and which provider will read the answer. The contextvar
defaults to None and None means deny (gate.py): a code path that never stamps
a turn gets nothing, so a new surface stays denied until it is wired in on
purpose. Contextvars flow into the context-building and tool worker threads
(copy_context), so the stamp set by the route is the one the executors see.

    token = turn.stamp(turn.WellnessTurn(operator=op, surface=turn.SURFACE_ANDROID,
                                         credential_ok=True, provider="anthropic"))
    try:
        ...
    finally:
        turn.clear(token)
"""
from __future__ import annotations

import contextvars
import dataclasses
import secrets
from typing import Optional

# Surfaces a wellness turn can come from. Allowed ones still need a credential
# (or household mode); the never-list is refused whatever it carries.
SURFACE_ANDROID = "android"                # native app chat on the enrolled phone
SURFACE_PORTAL_WEBVIEW = "portal_webview"  # the Portal inside the app (bridge-signed)
SURFACE_PORTAL = "portal"                  # the Portal in a browser
SURFACE_CRON = "cron"                      # wellness templates (M7.3)
SURFACE_VOICE = "voice"                    # live voice opened on the enrolled phone
SURFACE_ON_DEVICE = "on_device"            # Gemma on the source phone

ALLOWED_SURFACES = frozenset({
    SURFACE_ANDROID, SURFACE_PORTAL_WEBVIEW, SURFACE_PORTAL, SURFACE_CRON,
    SURFACE_VOICE, SURFACE_ON_DEVICE,
})

# Never, in v1: carrier plaintext, spoofable caller ID, cross-operator agents.
NEVER_SURFACES = frozenset({
    "sms", "telephony", "mcp", "mcp_remote", "mcp_stdio", "mcp_gateway",
    "cli_agent", "cu_agent", "system", "flight_recorder",
})

# Allowed surfaces that read health only with their own credential, household
# mode or not: a cron job is a wellness template created from the enrolled
# phone (M7.3), a voice session is opened signed on it (M7.5), and the
# on-device model runs on the source phone (M7.5). None of them is "anyone on
# the tailnet", which is all household mode stands in for.
CREDENTIAL_ONLY_SURFACES = frozenset({SURFACE_CRON, SURFACE_VOICE, SURFACE_ON_DEVICE})

# What a request or a queued task may DECLARE about itself (cron, MCP): a
# declaration can only narrow, so only surfaces the gate refuses on their own
# are accepted; a credential comes from a verified signature, never a field.
DECLARABLE_SURFACES = NEVER_SURFACES | {SURFACE_CRON}


@dataclasses.dataclass(frozen=True)
class WellnessTurn:
    operator: str
    surface: str
    credential_ok: bool = False
    surface_id: Optional[str] = None     # enrollment id or approved-surface id
    provider: Optional[str] = None       # recorded AFTER any media auto-route
    model: Optional[str] = None
    turn_key: Optional[str] = None
    cron_job_id: Optional[str] = None

    def with_route(self, provider: Optional[str], model: Optional[str]) -> "WellnessTurn":
        """The same turn re-pointed at the provider that will really answer."""
        return dataclasses.replace(self, provider=provider, model=model)


_WELLNESS_TURN: contextvars.ContextVar = contextvars.ContextVar("_WELLNESS_TURN", default=None)


def new_turn_key() -> str:
    """A fresh turn key ("wt_" + 16 hex): the durable mask carry's join key."""
    return "wt_" + secrets.token_hex(8)


def stamp(turn: WellnessTurn) -> contextvars.Token:
    if not isinstance(turn, WellnessTurn):
        raise TypeError("stamp() takes a WellnessTurn")
    return _WELLNESS_TURN.set(turn)


def current() -> Optional[WellnessTurn]:
    return _WELLNESS_TURN.get()


def clear(token: Optional[contextvars.Token] = None) -> None:
    """Undo a stamp (with its token) or drop whatever is stamped."""
    if token is not None:
        _WELLNESS_TURN.reset(token)
    else:
        _WELLNESS_TURN.set(None)
