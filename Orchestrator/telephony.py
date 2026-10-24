"""Outbound telephony helpers: phone-number normalization and which route places a call.

Audit rank 57 (O18-toolvault-modules-08, -15, O10-telephony-twilio-18). The
make_phone_call, make_voice_call and send_sms tools and the phone bridge's
callbacks each carried their own copy of both decisions, and the copies were
wrong in the same ways:

  * a number that was neither 10 digits nor 1+10 went out untouched (no "+"),
    to fail at the gateway or reach the wrong place;
  * TELEPHONY_PROVIDER=cellular (or auto with a modem) picked /cellular/call,
    a route that does not exist (cellular_routes only reports modem status), so
    every call 404'd; and auto never chose Asterisk, so a TG200 box with
    TELEPHONY_PROVIDER=auto silently dialled through Twilio.

normalize_e164() and outbound_call_path() are the one answer. Config is read on
each call (module attributes), so a test or a wizard change takes effect.
"""
from __future__ import annotations

import os
import re
from typing import Optional

# The country a national-format number belongs to. The BlackBox has always
# treated a bare 10-digit number as North American (+1); a box elsewhere sets
# TELEPHONY_DEFAULT_COUNTRY_CODE (digits only, e.g. 44).
_DEFAULT_COUNTRY_CODE = "1"

# E.164: a "+", then a country code that never starts with 0, at most 15 digits
# in all. The shortest real international numbers are 7-8 digits.
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_SEPARATORS_RE = re.compile(r"[\s\-.() ]")

ASTERISK_CALL_PATH = "/asterisk/call"
TWILIO_CALL_PATH = "/twilio/call"


def default_country_code() -> str:
    code = (os.environ.get("TELEPHONY_DEFAULT_COUNTRY_CODE") or "").strip().lstrip("+")
    return code if code.isdigit() and not code.startswith("0") else _DEFAULT_COUNTRY_CODE


def normalize_e164(number, country_code: Optional[str] = None) -> Optional[str]:
    """``number`` as E.164 ("+15551234567"), or None when it cannot be one.

    Accepted: E.164 with or without spaces, dashes, dots or parentheses; the
    "00" international prefix; a 10-digit national number (prefixed with
    ``country_code``, by default default_country_code()); outside North
    America, a national number with a leading trunk "0" (the 0 becomes the
    country code); 11-15 digits that already start with a country code
    ("15551234567"). Anything else (letters, too short or long, a country code
    starting with 0) is refused rather than sent on.
    """
    if not isinstance(number, str):
        return None
    cc = country_code or default_country_code()
    raw = _SEPARATORS_RE.sub("", number.strip())
    if not raw:
        return None
    if raw.startswith("+"):
        candidate = raw
    elif raw.startswith("00"):
        candidate = "+" + raw[2:]
    elif not raw.isdigit():
        return None
    elif raw.startswith("0"):
        if cc == "1":
            return None  # no North American number starts with 0
        candidate = "+" + cc + raw[1:]
    elif len(raw) == 10:
        candidate = "+" + cc + raw
    elif 11 <= len(raw) <= 15:
        candidate = "+" + raw
    else:
        return None
    return candidate if _E164_RE.match(candidate) else None


def uses_asterisk() -> bool:
    """True when outbound calls and SMS go through the Asterisk/TG200 gateway:
    TELEPHONY_PROVIDER is "asterisk" or "auto" and Asterisk is enabled."""
    from Orchestrator import config
    provider = str(getattr(config, "TELEPHONY_PROVIDER", "") or "").strip().lower()
    return bool(getattr(config, "ASTERISK_ENABLED", False)) and provider in ("asterisk", "auto")


def outbound_call_path() -> str:
    """The route that places an outbound call on this box: /asterisk/call when
    the TG200 gateway is the provider (uses_asterisk), else /twilio/call, which
    answers with a clear error when Twilio is not configured. There is no
    cellular-modem call route, so "cellular" falls back like any other value."""
    return ASTERISK_CALL_PATH if uses_asterisk() else TWILIO_CALL_PATH


def call_endpoint(base_url: str) -> str:
    """``base_url`` + outbound_call_path()."""
    return base_url.rstrip("/") + outbound_call_path()


def call_result_ok(result) -> bool:
    """Whether a call route's JSON says the call was placed. Both routes answer
    HTTP 200 with {"error": ...} when they refuse, so status alone proves nothing."""
    return isinstance(result, dict) and result.get("status") == "initiated" and not result.get("error")


def call_reference(result) -> str:
    """The id of a placed call: Twilio's call_sid, else the BlackBox session id
    (/asterisk/call answers with session_id only)."""
    if not isinstance(result, dict):
        return ""
    return str(result.get("call_sid") or result.get("session_id") or "")


def call_error(result, status: Optional[int] = None) -> str:
    """The reason a call route refused, for the tool result."""
    if isinstance(result, dict):
        detail = result.get("error") or result.get("detail")
        if detail:
            return str(detail)
    return f"HTTP {status}" if status is not None else "Unknown error"
