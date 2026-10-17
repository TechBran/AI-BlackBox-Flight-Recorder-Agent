"""pin_gate.py - the inbound-call PIN gate shared by every IVR.

The phone PIN is the only thing between a PSTN caller and an AI that runs
with operator context and tools (Tailscale does not cover the GSM leg), so the
gate fails CLOSED:

  * No PIN default ships in source (the old literal was public on GitHub).
    An empty or malformed PHONE_PIN_CODE means "unconfigured", and every IVR
    (Asterisk, cellular, Twilio) refuses the call with a spoken apology.
    PHONE_PIN_ENABLED=false is the only way to skip the gate, and it has to
    be set on purpose.
  * The PIN, the digits a caller types and the expected value are never
    logged. Callers of this module log attempt numbers only.
  * Wrong PINs feed a per-caller lockout that persists across calls (JSON
    store under credentials/, 0600, atomic writes) and backs off
    exponentially. A global backstop bounds guessing from rotating
    (spoofed) caller IDs. A number that entered the right PIN recently is
    exempt from the global lock (never from its own), so a guesser cannot
    lock the owner out; reset_lockouts() is the operator's reset.
  * ensure_pin() is the onboarding hook: when telephony is configured and no
    PIN is set it generates a random one into .env and makes it live in the
    running process. It never rotates an existing PIN.

Config is read FRESH from Orchestrator.config on every call, so a PIN set by
the wizard (or monkeypatched in tests) takes effect without a restart.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone

from Orchestrator import config
from Orchestrator.utils.paths import resolve

# 4-12 decimal digits: what a phone keypad can enter and every IVR can gather.
PIN_RE = re.compile(r"^[0-9]{4,12}$")
GENERATED_PIN_LENGTH = 6

LOCKOUT_PATH = resolve("credentials", "phone_pin_lockout.json")
LOCKOUT_BASE_S = 60                 # first lockout
LOCKOUT_MAX_S = 24 * 3600           # backoff cap
LOCKOUT_FORGET_S = 24 * 3600        # quiet period before a caller's history is dropped
GLOBAL_KEY = "*"                    # all callers together (caller-ID rotation backstop)
GLOBAL_THRESHOLD_FACTOR = 10        # global lock after 10x the per-caller threshold
TRUST_S = 30 * 24 * 3600            # a right PIN exempts that number from the global lock this long

# Serializes read-modify-write of the lockout store within this process; the
# Orchestrator is the store's only writer.
_LOCK = threading.Lock()
_EMPTY_STORE = {"version": 1, "callers": {}}


# ---------------------------------------------------------------------------
# Gate state
# ---------------------------------------------------------------------------

def pin_enabled() -> bool:
    return bool(getattr(config, "PHONE_PIN_ENABLED", True))


def _configured_value() -> str:
    return str(getattr(config, "PHONE_PIN_CODE", "") or "").strip()


def pin_configured() -> bool:
    return PIN_RE.fullmatch(_configured_value()) is not None


def gate_status() -> str:
    """'disabled' (PHONE_PIN_ENABLED=false), 'unconfigured' or 'ready'."""
    if not pin_enabled():
        return "disabled"
    return "ready" if pin_configured() else "unconfigured"


def pin_length() -> int:
    """Digits to gather from the caller (0 when no valid PIN is configured)."""
    return len(_configured_value()) if pin_configured() else 0


def max_attempts() -> int:
    try:
        return max(1, int(getattr(config, "PHONE_PIN_MAX_ATTEMPTS", 3)))
    except (TypeError, ValueError):
        return 3


def check_pin(entered) -> bool:
    """Constant-time PIN check. Always False when no valid PIN is configured."""
    if not pin_configured() or not isinstance(entered, str) or not entered:
        return False
    return hmac.compare_digest(entered.encode(), _configured_value().encode())


def log_unconfigured(tag: str) -> None:
    """The loud, actionable line every IVR prints when it refuses a call."""
    print(
        f"[{tag}] REFUSING inbound call: phone PIN is not configured "
        f"(PHONE_PIN_CODE must be 4-12 digits). Fix: run the Telephony setup "
        f"wizard's Apply step (it generates one), POST /phone/pin/generate, or "
        f"set PHONE_PIN_CODE in .env and restart. To run with no PIN on "
        f"purpose, set PHONE_PIN_ENABLED=false."
    )


def describe() -> dict:
    """Gate status for UIs and status endpoints. Never includes the PIN."""
    return {
        "enabled": pin_enabled(),
        "status": gate_status(),
        "configured": pin_configured(),
        "length": pin_length(),
        "max_attempts": max_attempts(),
    }


# ---------------------------------------------------------------------------
# Per-caller lockout (persists across calls)
# ---------------------------------------------------------------------------

def caller_key(caller_id) -> str:
    """Normalize a caller ID to its last 10 digits; no digits -> 'anonymous'."""
    digits = "".join(ch for ch in str(caller_id or "") if ch.isdigit())
    if not digits:
        return "anonymous"
    return digits[-10:]


def mask_caller(caller_id) -> str:
    """Log-safe caller label (last 4 digits)."""
    key = caller_key(caller_id)
    return key if key == "anonymous" else f"...{key[-4:]}"


def _quarantine(path: str) -> None:
    dest = f"{path}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        os.replace(path, dest)
    except OSError:
        pass


def _read_store() -> dict:
    """Load the lockout store. Fail-soft: never raises; corrupt files are quarantined."""
    path = str(LOCKOUT_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(_EMPTY_STORE))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _quarantine(path)
        print(f"[PHONE-PIN] Corrupt lockout store quarantined: {path}")
        return json.loads(json.dumps(_EMPTY_STORE))
    except OSError as e:
        print(f"[PHONE-PIN] Lockout store unreadable ({e.__class__.__name__}); starting empty")
        return json.loads(json.dumps(_EMPTY_STORE))
    if not isinstance(data, dict) or not isinstance(data.get("callers"), dict):
        _quarantine(path)
        return json.loads(json.dumps(_EMPTY_STORE))
    return data


def _write_store(data: dict) -> None:
    """Atomic write (tmp + os.replace), 0600. A failed write is logged, not raised:
    the per-call attempt limit still holds without persistence."""
    path = str(LOCKOUT_PATH)
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".phone_pin_lockout.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        print(f"[PHONE-PIN] WARNING: could not persist lockout store ({e.__class__.__name__})")


def _prune(callers: dict, now: float) -> None:
    for key in list(callers):
        entry = callers[key]
        if float(entry.get("trusted_until", 0)) > now:
            continue
        last = max(float(entry.get("updated", 0)), float(entry.get("locked_until", 0)))
        if now - last > LOCKOUT_FORGET_S:
            del callers[key]


def lockout_remaining(caller_id, now: float | None = None) -> int:
    """Seconds until this caller may try a PIN again (0 = not locked).

    Includes the global backstop, except for a number that entered the right
    PIN within TRUST_S (its own lock still applies)."""
    now = time.time() if now is None else now
    with _LOCK:
        callers = _read_store()["callers"]
    own = callers.get(caller_key(caller_id)) or {}
    keys = [caller_key(caller_id)]
    if float(own.get("trusted_until", 0)) <= now:
        keys.append(GLOBAL_KEY)
    remaining = 0.0
    for key in keys:
        entry = callers.get(key) or {}
        remaining = max(remaining, float(entry.get("locked_until", 0)) - now)
    return math.ceil(remaining) if remaining > 0 else 0


def record_failure(caller_id, now: float | None = None) -> int:
    """Count one wrong PIN entry. Returns the lockout (seconds) it triggered, else 0.

    A caller is locked after max_attempts() consecutive wrong entries (across
    calls); each further lockout doubles, capped at LOCKOUT_MAX_S. All
    callers together are locked after GLOBAL_THRESHOLD_FACTOR times that.
    """
    now = time.time() if now is None else now
    per_caller = max_attempts()
    thresholds = ((caller_key(caller_id), per_caller),
                  (GLOBAL_KEY, per_caller * GLOBAL_THRESHOLD_FACTOR))
    locked_for = 0
    with _LOCK:
        data = _read_store()
        callers = data["callers"]
        _prune(callers, now)
        for key, threshold in thresholds:
            entry = callers.setdefault(key, {"failures": 0, "lockouts": 0, "locked_until": 0})
            entry.setdefault("lockouts", 0)
            entry.setdefault("locked_until", 0)
            entry["failures"] = int(entry.get("failures", 0)) + 1
            entry["updated"] = now
            if entry["failures"] >= threshold:
                entry["lockouts"] = int(entry.get("lockouts", 0)) + 1
                duration = min(LOCKOUT_BASE_S * (2 ** (entry["lockouts"] - 1)), LOCKOUT_MAX_S)
                entry["locked_until"] = max(float(entry.get("locked_until", 0)), now + duration)
                entry["failures"] = 0
                locked_for = max(locked_for, duration)
        _write_store(data)
    if locked_for:
        print(f"[PHONE-PIN] Caller {mask_caller(caller_id)} locked out for {locked_for}s "
              f"after repeated wrong PINs")
    return locked_for


def record_success(caller_id, now: float | None = None) -> None:
    """A correct PIN clears the caller's history and the global failure run,
    and trusts the number (not a withheld one) past the global lock."""
    now = time.time() if now is None else now
    key = caller_key(caller_id)
    with _LOCK:
        data = _read_store()
        callers = data["callers"]
        changed = callers.pop(key, None) is not None
        if key != "anonymous":
            callers[key] = {"failures": 0, "updated": now, "trusted_until": now + TRUST_S}
            changed = True
        glob = callers.get(GLOBAL_KEY)
        if glob and glob.get("failures"):
            glob["failures"] = 0
            changed = True
        if changed:
            _write_store(data)


def reset_lockouts() -> int:
    """Clear every lockout and failure count (the operator's reset). Returns
    how many locked or failing entries were cleared; trust is kept."""
    with _LOCK:
        data = _read_store()
        callers = data["callers"]
        cleared = 0
        for key in list(callers):
            entry = callers[key]
            if float(entry.get("locked_until", 0)) or int(entry.get("failures", 0)):
                cleared += 1
            if float(entry.get("trusted_until", 0)):
                callers[key] = {"failures": 0, "updated": entry.get("updated", 0),
                                "trusted_until": entry["trusted_until"]}
            else:
                del callers[key]
        if cleared:
            _write_store(data)
    if cleared:
        print(f"[PHONE-PIN] Operator reset cleared {cleared} lockout entr{'y' if cleared == 1 else 'ies'}")
    return cleared


# ---------------------------------------------------------------------------
# Onboarding: generate / set the PIN
# ---------------------------------------------------------------------------

def _is_trivial(pin: str) -> bool:
    return (len(set(pin)) == 1
            or pin in "01234567890123456789"
            or pin in "98765432109876543210")


def generate_pin(length: int = GENERATED_PIN_LENGTH) -> str:
    """A uniformly random PIN, re-drawn if it is a run or a repeated digit."""
    while True:
        pin = "".join(secrets.choice("0123456789") for _ in range(length))
        if not _is_trivial(pin):
            return pin


def set_pin(pin: str) -> None:
    """Persist PHONE_PIN_CODE to .env (atomic, 0600) and make it live now."""
    if not isinstance(pin, str) or not PIN_RE.fullmatch(pin):
        raise ValueError("PIN must be 4-12 digits")
    from Orchestrator.onboarding import secrets_writer
    secrets_writer.update_env({"PHONE_PIN_CODE": pin})
    os.environ["PHONE_PIN_CODE"] = pin
    config.PHONE_PIN_CODE = pin


def ensure_pin() -> dict:
    """Generate a PIN when the gate is enabled and none is configured.

    Returns {"enabled", "configured", "generated", "length"} plus "pin" ONLY
    when one was generated by this call, so the setup UI can show it once.
    """
    result = {"enabled": pin_enabled(), "configured": pin_configured(),
              "generated": False, "length": pin_length()}
    if not result["enabled"] or result["configured"]:
        return result
    pin = generate_pin()
    set_pin(pin)
    print(f"[PHONE-PIN] Generated a new {len(pin)}-digit phone PIN "
          f"(saved to .env as PHONE_PIN_CODE)")
    return {"enabled": True, "configured": True, "generated": True,
            "length": len(pin), "pin": pin}
