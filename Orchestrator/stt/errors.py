"""Machine-readable stt_error frames for the /ws/stt contract (audit rank 52:
W24, A08-cli-agent-ui-07, O08-stt-realtime-04).

An stt_error used to carry only a human message, so a client could not tell an
exhausted provider (ElevenLabs quota_exceeded: seven frames in four days) from
a transient blip. Android treated the close that followed as a transport drop
and reconnected to the same exhausted provider; nothing offered another one.

Every stt_error now carries, additively (old clients ignore unknown keys):

  * ``code``       — one of the CODES below, the reason a program can act on;
  * ``provider``   — the provider the session was using (None before one
                     resolved);
  * ``switchable`` — True when another STT provider could serve the dictation,
                     so the client offers a switch-provider prompt fed by
                     /stt/catalog.

An stt_error is TERMINAL for the client: it stops capture and does not
reconnect. The shared frame fixture tests/fixtures/stt_ws_frames.json pins the
shapes for the server, Portal and Android suites.
"""
from __future__ import annotations

from typing import Optional

QUOTA_EXCEEDED = "quota_exceeded"              # the provider account is out of quota or credit
AUTH_FAILED = "auth_failed"                    # the provider key is missing or was rejected
RATE_LIMITED = "rate_limited"                  # a rate or concurrency limit; transient
PROVIDER_UNAVAILABLE = "provider_unavailable"  # the provider or its server cannot serve now
SESSION_LIMIT = "session_limit"                # the session could not be sustained
NO_PROVIDER = "no_provider"                    # no STT provider is configured on the box
BAD_REQUEST = "bad_request"                    # the client broke the protocol
PROVIDER_ERROR = "provider_error"              # any other provider-side failure

CODES = frozenset({QUOTA_EXCEEDED, AUTH_FAILED, RATE_LIMITED, PROVIDER_UNAVAILABLE,
                   SESSION_LIMIT, NO_PROVIDER, BAD_REQUEST, PROVIDER_ERROR})

# Codes no other provider can fix: nothing else is configured, or the client
# itself is at fault.
_NOT_SWITCHABLE = frozenset({NO_PROVIDER, BAD_REQUEST})

# ElevenLabs Scribe error taxonomy (elevenlabs/client.py _ERROR_HINTS) -> code.
_ELEVENLABS = {
    "quota_exceeded": QUOTA_EXCEEDED,
    "auth_error": AUTH_FAILED,
    "rate_limited": RATE_LIMITED,
    "resource_exhausted": RATE_LIMITED,   # the plan's concurrency limit
}


def frame(message: str, code: str, **extra) -> dict:
    """An stt_error frame with an explicit code (provider/switchable are
    stamped by the endpoint, see stamp())."""
    return {"type": "stt_error", "message": message, "code": code, **extra}


def elevenlabs_code(taxonomy_code: Optional[str]) -> str:
    return _ELEVENLABS.get(taxonomy_code or "", PROVIDER_ERROR)


def stamp(data: dict, provider: Optional[str]) -> dict:
    """Return the stt_error `data` completed with code, provider and switchable.

    Explicit values win. Without a code, a billing refusal (provider_billing's
    ``error_kind: credits_exhausted``) reads as quota_exceeded and anything
    else as provider_error.
    """
    out = dict(data)
    code = out.get("code")
    if code not in CODES:
        code = QUOTA_EXCEEDED if out.get("error_kind") == "credits_exhausted" else PROVIDER_ERROR
    out["code"] = code
    out.setdefault("provider", provider or None)
    out.setdefault("switchable", code not in _NOT_SWITCHABLE)
    return out
