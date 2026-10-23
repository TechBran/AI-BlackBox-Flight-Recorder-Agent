"""Provider billing failures: one classifier, one error kind, one push per hour.

W09 (2026-10-09 audit): the Anthropic key ran out of credit again and again
(20 "credit balance is too low" 400s in journald, Sep 2 - Oct 6). The
provider's text did reach the task rows, but nothing recognised it. Chat showed
a raw JSON error, Computer Use and cron runs failed with a provider body,
preflight only checked that a key EXISTED, and no push reached anyone. This
module is the one place that recognises a billing failure. Every surface calls
it at the point where it has the provider's status and body:

  * ``classify(provider, status, body)`` -> ``BillingError`` or None. All the
    billing failures map to ONE error kind, ``credits_exhausted``, with a
    ``reason`` that picks the right remedy: ``no_credits`` (balance empty),
    ``spend_limit`` (a spend limit or cap was hit) or ``billing_inactive``
    (payment method / billing account). Rate limits, overloads and plain
    quota-per-minute 429s are NOT billing and never match.
  * ``report(err, surface=...)`` records the failure (preflight reads it back
    with ``last_failure``; a later successful call clears it with ``note_ok``)
    and sends ONE box-wide push per provider per hour through the
    notifications bus, deduped across restarts by a stamp in the state file.
    A push that reached no device does not use up the hour.
  * ``check`` / ``check_exception`` / ``raise_if_exhausted`` are the one-line
    call-site forms of classify + report.

User-facing text is ``err.text``: the house sentence ("Anthropic account is out
of credits - add credits or turn on auto-reload in the Anthropic console")
followed by the provider's own error MESSAGE (parsed, redacted, bounded; never
the raw body). Error events keep their plain-string ``data`` (every client
renders that) and carry the structured kind beside it in ``error_detail``.

State lives in ``Manifest/provider_billing_state.json`` (per-install, gitignored).
A missing or corrupt file reads as empty; nothing here ever raises into a
caller's error path.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from Orchestrator.secret_redaction import error_message_from_body, redact

logger = logging.getLogger(__name__)

CREDITS_EXHAUSTED = "credits_exhausted"

# One push per provider (per account scope) per hour.
NOTIFY_INTERVAL_S = 3600

# Provider label + where its billing lives. Keys are the canonical ids this
# module records under; _ALIASES maps the other spellings the callers use
# (CU backends say "google", chat says "gemini", CLI agents name the binary).
_PROVIDERS = {
    "anthropic": ("Anthropic", "Anthropic console"),
    "openai": ("OpenAI", "OpenAI console"),
    "xai": ("xAI", "xAI console"),
    "google": ("Google Gemini", "Google AI Studio console"),
}
_ALIASES = {"gemini": "google", "claude": "anthropic", "codex": "openai", "grok": "xai"}

_MESSAGES = {
    "no_credits": "{label} account is out of credits - add credits or turn on "
                  "auto-reload in the {console}",
    "spend_limit": "{label} account has reached its spend limit - raise the limit or "
                   "add credits in the {console}",
    "billing_inactive": "{label} billing is not active - update the payment method or "
                        "re-enable billing in the {console}",
}
_TITLES = {
    "no_credits": "{label} is out of credits",
    "spend_limit": "{label} spend limit reached",
    "billing_inactive": "{label} billing is not active",
}

# Matched against the lowercased body text, reason by reason in this order (a
# spend-limit body from OpenAI also says insufficient_quota, so spend_limit is
# checked before no_credits). Each reason has two lists:
#   * codes and the providers' own full sentences, trusted in any text;
#   * plain-English phrases ("spend limit", "out of credits") that a model, an
#     agent or a file can just as well write. They count only on a real
#     provider refusal status (_REFUSAL_STATUSES): a frontier driver's
#     RuntimeError or a CLI's output has no status, and the model's own words
#     must never read as a billing refusal.
# A generic word like "quota" or "billing" alone is never enough. Gemini's
# free-tier per-minute 429 says "check your plan and billing details" and is a
# RATE limit, so that sentence is deliberately not a needle.
_NEEDLES = (
    ("billing_inactive", (
        "billing_not_active",                       # OpenAI
        "billing_disabled",                         # Google ErrorInfo reason
        "requires billing to be enabled",           # Google
        "billing_error",                            # Anthropic 402 type
    ), (
        "please enable billing",                    # Google 400 FAILED_PRECONDITION
    )),
    ("spend_limit", (
        "enforced_spend_limit_reached",             # Anthropic tier spend cap (429)
        "reached your specified api usage limits",  # Anthropic self-set limit (400)
        "reached your specified workspace api usage limits",
        "reached your api usage limits",
        "billing_hard_limit_reached",               # OpenAI
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    ), (
        "spend limit", "spending limit", "spend cap", "spending cap",
    )),
    ("no_credits", (
        "credit balance is too low",                # Anthropic API and Claude Code CLI
        "credit_balance_exhausted",                 # OpenAI
        "no prepaid credits remaining",
        "insufficient_quota",
        # The Codex CLI's own text for insufficient_quota (the code never
        # reaches its output).
        "quota exceeded. check your plan and billing details",
        "prepayment credits",                       # Google prepay
        "credits are depleted",
        "doesn't have any credits",                 # xAI
        "does not have any credits",
        "used all available credits",
    ), (
        "out of credits",
        "insufficient credits",
    )),
)
# The statuses a provider refuses a call with for billing (Anthropic 400/402/
# 429, OpenAI 400/429, Google 400/402/403/429, xAI 403/429).
_REFUSAL_STATUSES = frozenset({400, 402, 403, 429})

_DETAIL_MAX_CHARS = 300
_MESSAGE_RX = re.compile(r"""['"](?:message|error)['"]\s*:\s*(['"])(.+?)\1\s*[,}]""", re.S)

# Seams (tests patch these).
STATE_PATH: Optional[Path] = None
_now = time.time

_lock = threading.Lock()
_cache: dict = {"path": None, "data": None}


@dataclass(frozen=True)
class BillingError:
    """A provider call refused for billing. ``kind`` is always credits_exhausted."""

    provider: str          # canonical: anthropic | openai | xai | google
    reason: str            # no_credits | spend_limit | billing_inactive
    message: str           # the house sentence, no trailing period
    detail: str            # the provider's own message (redacted, bounded), may be ""
    status: Optional[int]
    kind: str = CREDITS_EXHAUSTED

    @property
    def label(self) -> str:
        return _PROVIDERS[self.provider][0]

    @property
    def title(self) -> str:
        return _TITLES[self.reason].format(label=self.label)

    @property
    def text(self) -> str:
        """User-facing: the remedy first, then what the provider said."""
        if self.detail:
            return f"{self.message}. ({self.label} said: {self.detail})"
        return f"{self.message}."

    def tagged(self) -> str:
        """The text prefixed with the kind, for records that have no kind column."""
        return f"{self.kind}: {self.text}"

    def detail_payload(self) -> dict:
        return {"kind": self.kind, "provider": self.provider,
                "reason": self.reason, "message": self.message}

    def record_fields(self) -> dict:
        """Fields a task row's result_data carries for this failure."""
        return {"error_kind": self.kind, "error_provider": self.provider,
                "error_reason": self.reason}

    def event(self, wrap: bool = False) -> dict:
        """An error event for a provider stream. ``wrap`` gives the Gemini/OpenAI
        CU drivers' {"message": ...} data shape; chat streams use a plain string."""
        data = {"message": self.text} if wrap else self.text
        return {"type": "error", "data": data, "error_detail": self.detail_payload()}


class CreditsExhaustedError(HTTPException):
    """Raised by the non-stream call_* paths. Still an HTTPException with the
    provider's status, so every existing ``except HTTPException`` keeps working;
    ``.billing`` carries the classification for the task worker."""

    def __init__(self, billing: BillingError):
        super().__init__(status_code=billing.status or 402, detail=billing.text)
        self.billing = billing


def billing_of(exc: BaseException) -> Optional[BillingError]:
    """The BillingError an exception carries, or None."""
    b = getattr(exc, "billing", None)
    return b if isinstance(b, BillingError) else None


def canonical_provider(provider: Optional[str]) -> Optional[str]:
    p = (provider or "").strip().lower()
    p = _ALIASES.get(p, p)
    return p if p in _PROVIDERS else None


# --------------------------------------------------------------------------- #
# Classification                                                              #
# --------------------------------------------------------------------------- #

def _as_text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        return body.decode("utf-8", "replace")
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, default=str)
    except (TypeError, ValueError):
        return str(body)


def _parse_object(text: str) -> Any:
    """The first JSON (or Python-repr, as SDK exceptions print) object in text."""
    start = text.find("{")
    if start < 0:
        return None
    chunk = text[start:start + 20000]
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(chunk)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
    return None


def _provider_message(body: Any, text: str) -> str:
    """The provider's own error message, redacted and bounded; "" when none."""
    msg = None
    if isinstance(body, dict):
        msg = error_message_from_body(body)
    if not msg:
        msg = error_message_from_body(text)
    if not msg:
        obj = _parse_object(text)
        if obj is not None:
            msg = error_message_from_body(obj)
    if not msg:
        m = _MESSAGE_RX.search(text)
        msg = m.group(2) if m else None
    if not msg and text and "{" not in text:
        msg = text            # a plain-text error line (CLI output)
    if not msg:
        return ""
    msg = " ".join(str(msg).split())
    return redact(msg)[:_DETAIL_MAX_CHARS]


def _reason_for(lowered: str, status: Optional[int]) -> Optional[str]:
    refused = status in _REFUSAL_STATUSES
    for reason, needles, phrases in _NEEDLES:
        if any(n in lowered for n in needles):
            return reason
        if refused and any(p in lowered for p in phrases):
            return reason
    if status == 402:   # Payment Required with no known phrase
        return "no_credits"
    return None


def classify(provider: Optional[str], status: Optional[int], body: Any) -> Optional[BillingError]:
    """A BillingError when ``body``/``status`` is a billing refusal, else None.

    ``body`` may be bytes, a JSON or Python-repr string (with or without a
    "400: "/"API error 400: " prefix), or an already-parsed dict. With no
    status (a CLI's error line, a realtime error event) only provider codes
    and sentences match, never the plain-English phrases. Never raises.
    """
    try:
        canon = canonical_provider(provider)
        if canon is None:
            return None
        text = _as_text(body)
        if not text and status != 402:
            return None
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        reason = _reason_for(text.lower(), status)
        if reason is None:
            return None
        label, console = _PROVIDERS[canon]
        return BillingError(
            provider=canon, reason=reason,
            message=_MESSAGES[reason].format(label=label, console=console),
            detail=_provider_message(body, text), status=status)
    except Exception as e:  # noqa: BLE001 — a classifier bug must never mask the real error
        logger.warning("[BILLING] classify failed (non-fatal): %r", e)
        return None


def classify_exception(provider: Optional[str], exc: BaseException) -> Optional[BillingError]:
    """classify() for an SDK exception (openai/anthropic APIStatusError,
    google-genai APIError, httpx HTTPStatusError): reads its status and body
    attributes and its text.

    An exception with no provider HTTP status is never a billing refusal: a
    driver's own RuntimeError (frontier_agent_loop raises one when Claude stops
    for max_tokens or refusal) carries the MODEL's text, which may say anything.
    """
    status = getattr(exc, "status_code", None)
    if status is None and isinstance(getattr(exc, "code", None), int):
        status = exc.code
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if not isinstance(status, int):
        return None
    parts = [str(exc)]
    for attr in ("body", "details"):
        val = getattr(exc, attr, None)
        if isinstance(val, (dict, list)):
            parts.append(_as_text(val))
    return classify(provider, status, "\n".join(parts))


# --------------------------------------------------------------------------- #
# State (last failure per provider + hourly push stamps)                       #
# --------------------------------------------------------------------------- #

def _state_path() -> Path:
    if STATE_PATH is not None:
        return Path(STATE_PATH)
    from Orchestrator.utils.paths import resolve
    return resolve("Manifest", "provider_billing_state.json")


def _empty() -> dict:
    return {"exhausted": {}, "notified": {}}


def _load() -> dict:
    """The cached state (call under _lock). Re-reads when the path changes.

    Read once per process: the Orchestrator is the only writer (every caller
    runs in it; the MCP server never imports this module).
    """
    path = _state_path()
    if _cache["path"] == path and _cache["data"] is not None:
        return _cache["data"]
    data = _empty()
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict):
            for key in ("exhausted", "notified"):
                if isinstance(raw.get(key), dict):
                    data[key] = raw[key]
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning("[BILLING] state file unreadable (%s) - treating as empty", e)
    _cache["path"], _cache["data"] = path, data
    return data


def _save(data: dict) -> None:
    """Atomic write (call under _lock). A failed write only logs."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("[BILLING] state write failed (non-fatal): %s", e)


def _reset_cache() -> None:
    """Forget the in-memory copy (tests; the next call re-reads the file)."""
    with _lock:
        _cache["path"], _cache["data"] = None, None


def last_failure(provider: str) -> Optional[dict]:
    """The recorded billing failure for this provider's API key, or None.

    Read by CU preflight: no provider call, no tokens. Cleared by note_ok.
    """
    canon = canonical_provider(provider)
    if canon is None:
        return None
    with _lock:
        rec = _load()["exhausted"].get(canon)
    return dict(rec) if isinstance(rec, dict) else None


def note_ok(provider: str) -> None:
    """A call to this provider succeeded: clear its billing flag. Cheap when
    nothing is flagged (no I/O), so success paths call it on every response."""
    canon = canonical_provider(provider)
    if canon is None:
        return
    try:
        with _lock:
            data = _load()
            if canon not in data["exhausted"]:
                return
            del data["exhausted"][canon]
            _save(data)
        logger.info("[BILLING] %s answered again - billing flag cleared", canon)
    except Exception as e:  # noqa: BLE001 — never break a success path
        logger.warning("[BILLING] note_ok failed (non-fatal): %r", e)


def _push(title: str, body: str, dedup_key: str, on_result=None) -> bool:
    """Schedule one box-wide push. Seam; True when it was actually scheduled.
    ``on_result`` gets the push's NotifyResult (None if it failed) once sent."""
    from Orchestrator.notifications.bridge import notify_box_in_background
    return notify_box_in_background(title, body, category="alert", dedup_key=dedup_key,
                                    on_result=on_result)


def _give_back_hour(key: str, claimed: float, previous: Any) -> None:
    """Un-claim the hour ``report`` stamped at ``claimed``, so the next failure
    pushes again. A newer claim (a later hour) is left alone. Never raises."""
    try:
        with _lock:
            data = _load()
            if data["notified"].get(key) != claimed:
                return
            if isinstance(previous, (int, float)):
                data["notified"][key] = previous
            else:
                data["notified"].pop(key, None)
            _save(data)
    except Exception as e:  # noqa: BLE001
        logger.warning("[BILLING] hour give-back failed (non-fatal): %r", e)


def report(err: BillingError, *, surface: str, scope: str = "api") -> bool:
    """Record a billing failure and push at most once per provider per hour.

    ``scope`` "api" is the box's own key (what preflight reads); "cli" is a CLI
    agent's own login, a different account: it gets its own hourly push and
    never sets the API-key flag. Returns True when a push was scheduled.
    Never raises.
    """
    try:
        key = err.provider if scope == "api" else f"{err.provider}:{scope}"
        now = _now()
        with _lock:
            data = _load()
            if scope == "api":
                data["exhausted"][err.provider] = {
                    "kind": err.kind, "reason": err.reason, "message": err.message,
                    "detail": err.detail, "status": err.status, "surface": surface,
                    "at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "ts": now,
                }
            previous = data["notified"].get(key)
            due = not isinstance(previous, (int, float)) or now - previous >= NOTIFY_INTERVAL_S
            if due:
                data["notified"][key] = now   # claim the hour before sending (no double push)
            _save(data)
        logger.warning("[BILLING] %s %s on %s: %s", key, err.reason, surface, err.detail or err.message)
        if not due:
            return False
        body = f"{err.message}. Seen on {surface}."

        def _delivered(result) -> None:
            # A push no device received (the audit's NOTIFY delivered=0/0: every
            # phone offline) must not use up the hour: give it back, so the next
            # refusal tries again once a device is reachable.
            if not getattr(result, "delivered", None):
                _give_back_hour(key, now, previous)

        sent = False
        try:
            sent = bool(_push(err.title, body, f"billing:{key}:{int(now // NOTIFY_INTERVAL_S)}",
                              on_result=_delivered))
        except Exception as e:  # noqa: BLE001
            logger.warning("[BILLING] push failed (non-fatal): %r", e)
        if not sent:
            _give_back_hour(key, now, previous)   # dropped (no loop): try again next time
        return sent
    except Exception as e:  # noqa: BLE001 — never mask the caller's real error
        logger.warning("[BILLING] report failed (non-fatal): %r", e)
        return False


# --------------------------------------------------------------------------- #
# Call-site forms                                                             #
# --------------------------------------------------------------------------- #

def check(provider: Optional[str], status: Optional[int], body: Any, *, surface: str,
          scope: str = "api") -> Optional[BillingError]:
    """classify + report. Returns the BillingError (already reported) or None."""
    err = classify(provider, status, body)
    if err is not None:
        report(err, surface=surface, scope=scope)
    return err


def check_exception(provider: Optional[str], exc: BaseException, *, surface: str,
                    scope: str = "api") -> Optional[BillingError]:
    """classify_exception + report."""
    err = classify_exception(provider, exc)
    if err is not None:
        report(err, surface=surface, scope=scope)
    return err


def raise_if_exhausted(provider: Optional[str], status: Optional[int], body: Any, *,
                       surface: str) -> None:
    """For the non-stream paths: raise CreditsExhaustedError on a billing refusal."""
    err = check(provider, status, body, surface=surface)
    if err is not None:
        raise CreditsExhaustedError(err)
