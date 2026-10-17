"""The inbound-call PIN gate fails closed (audit O11-01, X03-09, O01-08).

The phone PIN is the only thing between a PSTN caller and a tool-armed AI
(Tailscale does not cover the GSM leg). These tests pin the shared gate every
IVR uses (Orchestrator/phone/pin_gate.py):

  * config.py ships NO literal PIN default (the old one was public on GitHub);
  * an empty / malformed PIN means "unconfigured" -> every IVR refuses;
  * the per-caller lockout persists across calls (fresh reads of a JSON store
    under credentials/), backs off exponentially, and has a global backstop
    against caller-ID rotation;
  * the onboarding helper generates a random PIN only when none is set, and
    never rotates one silently.

Hermetic: the lockout store and .env live in tmp_path; no network.
"""
import ast
import json
import os
import stat
from pathlib import Path

import pytest

from Orchestrator import config
from Orchestrator.onboarding import secrets_writer
from Orchestrator.phone import pin_gate

CONFIG_PY = Path(__file__).resolve().parents[1] / "config.py"
PIN = "739104"


@pytest.fixture(autouse=True)
def gate_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", True)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", PIN)
    monkeypatch.setattr(config, "PHONE_PIN_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pin_gate, "LOCKOUT_PATH", tmp_path / "credentials" / "phone_pin_lockout.json")
    env_file = tmp_path / ".env"
    env_file.write_text("PHONE_PIN_ENABLED=true\nPHONE_PIN_CODE=\n")
    monkeypatch.setattr(secrets_writer, "ENV_FILE", env_file)
    monkeypatch.delenv("PHONE_PIN_CODE", raising=False)
    return tmp_path


# ── no public default ───────────────────────────────────────────────────────

def _getenv_default(name):
    tree = ast.parse(CONFIG_PY.read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "getenv"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == name):
            return node.args[1].value if len(node.args) > 1 else None
    raise AssertionError(f"{name} is not read via os.getenv in config.py")


def test_config_has_no_literal_pin_default():
    default = _getenv_default("PHONE_PIN_CODE")
    assert default in (None, ""), "PHONE_PIN_CODE must not ship a literal default"


# ── gate status ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["", "   ", "12ab", "123", "1234567890123", "12 34"])
def test_empty_or_malformed_pin_is_unconfigured(monkeypatch, value):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", value)
    assert pin_gate.gate_status() == "unconfigured"
    assert pin_gate.pin_length() == 0
    # Nothing a caller can type unlocks an unconfigured gate.
    for guess in ("", value, "0000", None):
        assert pin_gate.check_pin(guess) is False


def test_valid_pin_is_ready_and_length_derived():
    assert pin_gate.gate_status() == "ready"
    assert pin_gate.pin_length() == len(PIN)
    assert pin_gate.check_pin(PIN) is True
    assert pin_gate.check_pin(PIN[:-1]) is False
    assert pin_gate.check_pin(PIN + "1") is False


def test_disabled_gate_is_explicit(monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", False)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "")
    assert pin_gate.gate_status() == "disabled"


def test_unconfigured_log_is_actionable_and_secret_free(capsys, monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "12ab")
    pin_gate.log_unconfigured("TEST-IVR")
    out = capsys.readouterr().out
    assert "PHONE_PIN_CODE" in out and "REFUS" in out.upper()
    assert "12ab" not in out


# ── lockout ─────────────────────────────────────────────────────────────────

CALLER = "+15555550142"


def test_lockout_after_threshold_and_persists_across_calls(gate_env):
    now = 1_000_000.0
    assert pin_gate.record_failure(CALLER, now=now) == 0
    assert pin_gate.record_failure(CALLER, now=now) == 0
    assert pin_gate.lockout_remaining(CALLER, now=now) == 0
    locked_for = pin_gate.record_failure(CALLER, now=now)
    assert locked_for == pin_gate.LOCKOUT_BASE_S
    # A later call (fresh read of the store) is still locked.
    assert pin_gate.lockout_remaining(CALLER, now=now + 10) == pin_gate.LOCKOUT_BASE_S - 10
    # Same caller in another format maps to the same key.
    assert pin_gate.lockout_remaining("(555) 555-0142", now=now + 10) > 0
    assert pin_gate.lockout_remaining("+15555550199", now=now + 10) == 0
    store = gate_env / "credentials" / "phone_pin_lockout.json"
    assert store.exists()
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert not [p for p in store.parent.iterdir() if p.name.endswith(".tmp")]
    # The store never holds the PIN or typed digits.
    assert PIN not in store.read_text()


def test_lockout_backs_off_exponentially(gate_env):
    now = 2_000_000.0
    for _ in range(3):
        pin_gate.record_failure(CALLER, now=now)
    first = pin_gate.lockout_remaining(CALLER, now=now)
    now += first + 1
    assert pin_gate.lockout_remaining(CALLER, now=now) == 0
    for _ in range(2):
        assert pin_gate.record_failure(CALLER, now=now) == 0
    second = pin_gate.record_failure(CALLER, now=now)
    assert second == 2 * first


def test_lockout_is_capped(gate_env):
    now = 3_000_000.0
    durations = []
    for _ in range(30):
        for _ in range(3):
            d = pin_gate.record_failure(CALLER, now=now)
        durations.append(d)
        now += d + 1
    assert max(durations) == pin_gate.LOCKOUT_MAX_S


def test_success_clears_caller_lockout_state(gate_env):
    now = 4_000_000.0
    pin_gate.record_failure(CALLER, now=now)
    pin_gate.record_failure(CALLER, now=now)
    pin_gate.record_success(CALLER)
    # Counter reset: two more failures do not lock.
    assert pin_gate.record_failure(CALLER, now=now) == 0
    assert pin_gate.record_failure(CALLER, now=now) == 0
    assert pin_gate.lockout_remaining(CALLER, now=now) == 0


def test_global_backstop_against_caller_id_rotation(gate_env):
    now = 5_000_000.0
    threshold = pin_gate.max_attempts() * pin_gate.GLOBAL_THRESHOLD_FACTOR
    locked = 0
    for i in range(threshold):
        # Two wrong guesses per spoofed number, never enough to lock one number.
        caller = f"+1555555{i // 2:04d}"
        locked = pin_gate.record_failure(caller, now=now)
    assert locked > 0
    assert pin_gate.lockout_remaining("+15555550999", now=now) > 0


def test_anonymous_callers_share_one_bucket():
    assert pin_gate.caller_key("") == pin_gate.caller_key("anonymous") == pin_gate.caller_key("unknown")
    assert pin_gate.caller_key("+1 (555) 555-0142") == pin_gate.caller_key("5555550142")


def test_corrupt_store_is_quarantined_not_fatal(gate_env):
    store = gate_env / "credentials" / "phone_pin_lockout.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json")
    assert pin_gate.lockout_remaining(CALLER, now=1.0) == 0
    assert pin_gate.record_failure(CALLER, now=1.0) == 0
    assert json.loads(store.read_text())["version"] == 1
    assert list(store.parent.glob("phone_pin_lockout.json.corrupt-*"))


# ── onboarding generation ───────────────────────────────────────────────────

def test_generate_pin_is_random_digits_and_not_trivial():
    pins = {pin_gate.generate_pin() for _ in range(50)}
    assert len(pins) > 40
    for pin in pins:
        assert len(pin) == pin_gate.GENERATED_PIN_LENGTH and pin.isdigit()
        assert len(set(pin)) > 1
        assert pin not in "01234567890" and pin not in "09876543210"


def test_ensure_pin_generates_when_unconfigured(gate_env, monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "")
    result = pin_gate.ensure_pin()
    assert result["generated"] is True
    new_pin = result["pin"]
    assert pin_gate.PIN_RE.fullmatch(new_pin)
    # Live immediately (no restart) and persisted to .env.
    assert config.PHONE_PIN_CODE == new_pin
    assert os.environ["PHONE_PIN_CODE"] == new_pin
    assert f"PHONE_PIN_CODE={new_pin}\n" in (gate_env / ".env").read_text()
    assert pin_gate.gate_status() == "ready"
    # Idempotent: a second call never rotates the PIN.
    again = pin_gate.ensure_pin()
    assert again["generated"] is False and "pin" not in again
    assert config.PHONE_PIN_CODE == new_pin


def test_ensure_pin_never_rotates_an_existing_pin(gate_env):
    result = pin_gate.ensure_pin()
    assert result == {"enabled": True, "configured": True, "generated": False, "length": len(PIN)}
    assert config.PHONE_PIN_CODE == PIN
    assert "PHONE_PIN_CODE=\n" in (gate_env / ".env").read_text()


def test_ensure_pin_respects_disabled_gate(gate_env, monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", False)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "")
    result = pin_gate.ensure_pin()
    assert result["generated"] is False and result["enabled"] is False
    assert config.PHONE_PIN_CODE == ""


def test_set_pin_rejects_malformed_values(gate_env):
    for bad in ("", "12", "12ab", "1" * 13, "12\n34"):
        with pytest.raises(ValueError):
            pin_gate.set_pin(bad)
    assert config.PHONE_PIN_CODE == PIN


def test_describe_never_reveals_the_pin():
    info = pin_gate.describe()
    assert info == {"enabled": True, "status": "ready", "configured": True,
                    "length": len(PIN), "max_attempts": 3}
    assert PIN not in json.dumps(info)
