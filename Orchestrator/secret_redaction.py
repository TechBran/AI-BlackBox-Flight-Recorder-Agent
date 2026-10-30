"""Secret redaction for everything the Orchestrator prints, logs or returns.

Audit rank 23 (O07-tts-voice-01, X03-security-backend-11, O02-chat-routes-A-07):
requests/httpx/websockets copy the full request URL into their exception text,
and most of this codebase logs with print(), so a key that ever reaches a URL,
an exception or an upstream error body lands in journald, in HTTPException
details and in tasks.db rows. Call sites send keys in headers now; this module
is the backstop for whatever still slips through.

  * redact(text)            masks the CURRENT values of secret-named env vars
                            (names ending _API_KEY/_SECRET/_SECRET_KEY/
                            _PRIVATE_KEY/_TOKEN/_PASSWORD/_AUTH/_SID, values
                            >= 12 chars) plus generic key shapes: Google
                            AIza..., sk-..., xai-..., Bearer tokens,
                            ?key=/&key= query values, and the health
                            feature's device key ("k": "..."), origin-proof
                            signature (sig=...) and surface tokens (wbx_s_...).
  * install_redaction()     wraps sys.stdout/sys.stderr and puts a
                            logging.Filter on every existing handler. Called
                            from app.py before any other Orchestrator import
                            and again from setup_logging(); idempotent.
  * upstream_error(exc)     (status, message) for a failed upstream HTTP call
  * response_error(resp)    that is safe to log and return: the upstream
                            status (`is not None`, because a 4xx requests
                            Response is falsy) and the parsed error message,
                            never str(exc) or the raw body.

Stdlib only, never imports Orchestrator.config: it must load before anything
that can print. The env snapshot refreshes lazily (env size change, or at most
every _ENV_REFRESH_S seconds), so a key saved by the onboarding wizard or a
rotated key is masked without a restart.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any, Optional, Tuple

REDACTED = "[REDACTED]"

_SECRET_ENV_SUFFIXES = ("_API_KEY", "_SECRET", "_SECRET_KEY", "_PRIVATE_KEY", "_TOKEN", "_PASSWORD", "_AUTH", "_SID")
_MIN_SECRET_LEN = 12
_ENV_REFRESH_S = 2.0
_MAX_ERROR_CHARS = 500

# Generic key shapes. The lookbehinds keep "task-...", "risk-..." and other
# hyphenated identifiers from matching the sk-/xai- prefixes.
_KEY_SHAPES = re.compile(
    r"AIza[0-9A-Za-z_-]{35}"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"
    r"|(?<![A-Za-z0-9])xai-[A-Za-z0-9]{20,}"
)
_URL_KEY_PARAM = re.compile(r"([?&](?:key|api_key|apikey|access_token)=)[^&#\s\"'<>]+", re.IGNORECASE)
_BEARER = re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/-]{16,}=*")
# Health tracking (Orchestrator/wellness): a device key as it is stored and
# sent at bind ("k": "<43 base64url>"), the origin-proof signature (sig=<64
# hex>) and an approved-surface token (wbx_s_...).
_WELLNESS_KEY = re.compile(r"""(["']k["']\s*:\s*["'])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])""")
_WELLNESS_SIG = re.compile(r"(\bsig=)[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_WELLNESS_TOKEN = re.compile(r"\bwbx_s_[A-Za-z0-9_-]{8,}")

# (env size, monotonic time, compiled alternation or None, value -> env name)
_snapshot: Tuple[int, float, Optional[re.Pattern], dict] = (-1, 0.0, None, {})


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(upper.endswith(s) or upper == s[1:] for s in _SECRET_ENV_SUFFIXES)


def refresh_secrets() -> None:
    """Rebuild the env-value snapshot now (tests; after writing a new key)."""
    global _snapshot
    try:
        items = list(os.environ.items())
    except Exception:   # env mutated mid-iteration by another thread: keep the old one
        return
    names = {}
    for name, value in items:
        if value and len(value) >= _MIN_SECRET_LEN and _is_secret_name(name):
            names.setdefault(value, name)
    rx = None
    if names:
        # Longest first so a secret that prefixes another leaves no fragment.
        rx = re.compile("|".join(re.escape(v) for v in sorted(names, key=len, reverse=True)))
    _snapshot = (len(os.environ), time.monotonic(), rx, names)


def _env_pattern():
    size, stamp, rx, names = _snapshot
    if size != len(os.environ) or time.monotonic() - stamp >= _ENV_REFRESH_S:
        refresh_secrets()
        _, _, rx, names = _snapshot
    return rx, names


def redact(text):
    """Mask secrets in a string; any other type is returned unchanged."""
    if not isinstance(text, str) or not text:
        return text
    rx, names = _env_pattern()
    if rx is not None:
        text = rx.sub(lambda m: f"[REDACTED:{names.get(m.group(0), 'secret')}]", text)
    text = _KEY_SHAPES.sub(REDACTED, text)
    text = _URL_KEY_PARAM.sub(lambda m: m.group(1) + REDACTED, text)
    text = _WELLNESS_KEY.sub(lambda m: m.group(1) + REDACTED, text)
    text = _WELLNESS_SIG.sub(lambda m: m.group(1) + REDACTED, text)
    text = _WELLNESS_TOKEN.sub(REDACTED, text)
    return _BEARER.sub(lambda m: m.group(1) + REDACTED, text)


# --------------------------------------------------------------------------- #
# Upstream HTTP errors                                                        #
# --------------------------------------------------------------------------- #

def error_message_from_body(body: Any) -> Optional[str]:
    """The provider's own error message from a JSON error body, else None.

    Handles Google/OpenAI/xAI ({"error": {"message": ...}}), {"error": "..."},
    and {"message"|"detail": ...}. A non-JSON body yields None: raw bodies are
    never echoed, they can carry request echoes.
    """
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0]   # Google streaming errors arrive as a one-element array
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("status")
    elif isinstance(err, str):
        msg = err
    else:
        msg = body.get("message") or body.get("detail")
    return str(msg) if msg else None


def response_error(resp, default_status: int = 502) -> Tuple[int, str]:
    """(status, safe message) for a non-2xx requests/httpx Response."""
    status = getattr(resp, "status_code", None) or default_status
    msg = None
    try:
        msg = error_message_from_body(resp.json())
    except Exception:
        pass
    if not msg:
        reason = getattr(resp, "reason", None) or getattr(resp, "reason_phrase", None)
        msg = f"HTTP {status}" + (f" {reason}" if reason else "")
    return status, redact(msg)[:_MAX_ERROR_CHARS]


def upstream_error(exc: BaseException, default_status: int = 502) -> Tuple[int, str]:
    """(status, safe message) for an exception from an upstream HTTP call.

    With a response: its status and parsed message. Without one (connection
    error, timeout): default_status and the exception TYPE only, since the
    message embeds the request URL.
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        return response_error(resp, default_status)
    return default_status, type(exc).__name__


# --------------------------------------------------------------------------- #
# stdout / stderr                                                             #
# --------------------------------------------------------------------------- #

class RedactingStream:
    """Text-stream proxy that masks secrets in str writes.

    Everything else (buffer for binary writes, fileno, isatty, flush, encoding,
    reconfigure, ...) is forwarded to the wrapped stream untouched.
    """

    _blackbox_redacting = True

    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        if isinstance(s, str):
            try:
                masked = redact(s)
            except Exception:   # never lose output over a redaction bug
                masked = s
            self._stream.write(masked)
            return len(s)
        return self._stream.write(s)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_stdio_redaction() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and not getattr(stream, "_blackbox_redacting", False):
            setattr(sys, name, RedactingStream(stream))


# --------------------------------------------------------------------------- #
# logging                                                                     #
# --------------------------------------------------------------------------- #

class RedactingFilter(logging.Filter):
    """Masks secrets in a record's message, args and traceback text.

    Args are masked one by one so an args tuple keeps its shape (uvicorn's
    access formatter unpacks it); the record collapses to a pre-formatted
    message only when per-arg masking cannot clean it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._scrub(record)
        except Exception:
            pass
        return True

    @staticmethod
    def _scrub_arg(arg):
        if isinstance(arg, str):
            return redact(arg)
        if isinstance(arg, (int, float, bool)) or arg is None:
            return arg
        text = str(arg)
        masked = redact(text)
        return masked if masked != text else arg

    def _scrub(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if redact(message) != message:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(self._scrub_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self._scrub_arg(v) for k, v in record.args.items()}
            try:
                still_leaks = redact(record.getMessage()) != record.getMessage()
            except Exception:
                still_leaks = True
            if still_leaks:
                record.msg, record.args = redact(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)


_LOG_FILTER = RedactingFilter()


def install_log_redaction() -> None:
    """Attach the filter to every handler that exists now (root, uvicorn, ...)."""
    loggers = [logging.getLogger()]
    loggers += [lg for lg in list(logging.Logger.manager.loggerDict.values())
                if isinstance(lg, logging.Logger)]
    for lg in loggers:
        for handler in list(lg.handlers):
            handler.addFilter(_LOG_FILTER)   # addFilter ignores a repeat


def install_redaction() -> None:
    install_stdio_redaction()
    install_log_redaction()
