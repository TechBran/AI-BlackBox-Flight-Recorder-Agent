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
import threading
import time
from typing import Dict, Optional, Tuple, Union

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


# ------------------------------------------------------------------ issued keys
#
# A turn key is announced to any client (stream_start carries it for every
# operator who has a health store, signed or not), so the key alone proves
# nothing. What the box knows at stamp time is whether the turn COULD receive
# a wellness delivery (a credential, or household mode) and for WHOM. The
# ledger's fallback and recall rules (mask.carry_for) and the history scrub's
# store reads (mask.scrub_history) run only for such a key, under that
# operator: a save or a stream that proves nothing is treated without ever
# reading the store, so an unsigned caller, or a household member replaying
# their own key under someone else's name, can never learn from what the box
# kept, hid, masked or sent whether a text matched another operator's results.
# Only keys that may deliver are kept, in process, for KEY_MEMORY_MS (a save
# follows its stream within seconds; a long tool loop within minutes); a
# restart forgets them, and a key whose rows are on disk still matches them
# exactly without it (W28).
KEY_MEMORY_MS = 60 * 60 * 1000
BY_CREDENTIAL = "credential"      # the operator's own phone signed the turn
BY_HOUSEHOLD = "household"        # household mode let an unsigned turn through
_ISSUED: Dict[str, Tuple[int, str, str]] = {}
_ISSUED_LOCK = threading.Lock()


def remember(turn: Union[WellnessTurn, str, None], how: Union[str, bool, None], *,
             operator: Optional[str] = None, now_ms: Optional[int] = None) -> None:
    """Remember an issued turn key (a WellnessTurn's, or the key itself with
    its `operator`) when its turn could receive a delivery: `how` is
    BY_CREDENTIAL or BY_HOUSEHOLD (True means BY_CREDENTIAL). A key that could
    not (False, None) is not kept: unknown and unproven keys answer alike."""
    if isinstance(turn, WellnessTurn):
        key, op = turn.turn_key, turn.operator
    else:
        key, op = turn, operator
    how = BY_CREDENTIAL if how is True else how
    if how not in (BY_CREDENTIAL, BY_HOUSEHOLD) or not isinstance(key, str) or not key or not op:
        return
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    with _ISSUED_LOCK:
        for old in [k for k, (until, _o, _h) in _ISSUED.items() if until < now]:
            del _ISSUED[old]
        _ISSUED[key] = (now + KEY_MEMORY_MS, op, how)


def issued(key, operator: Optional[str], *, now_ms: Optional[int] = None) -> Optional[str]:
    """How `key` was proven for `operator` (BY_CREDENTIAL, BY_HOUSEHOLD), or
    None for an unknown, expired or non-string key and for another
    operator's key (nothing is ever proven by a key the box does not remember
    issuing for such a turn of this operator). A household-proven key is
    only as good as household mode is NOW: callers re-check it
    (mask.key_may_deliver), so narrowing or disconnecting revokes such keys."""
    if not isinstance(key, str) or not key or not operator:
        return None
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    with _ISSUED_LOCK:
        entry = _ISSUED.get(key)
    return entry[2] if entry and entry[0] >= now and entry[1] == operator else None


def may_deliver(key, operator: Optional[str], *, now_ms: Optional[int] = None) -> bool:
    """Was `key` issued to a turn of `operator` that could receive a delivery?
    (The memory alone; mask.key_may_deliver adds the household re-check.)"""
    return issued(key, operator, now_ms=now_ms) is not None


def forget_issued() -> None:
    """Drop the issued-key memory (tests; a box restart does the same)."""
    with _ISSUED_LOCK:
        _ISSUED.clear()


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
