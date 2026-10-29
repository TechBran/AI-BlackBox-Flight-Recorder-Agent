"""Asterisk IVR PIN stage: fails closed and never logs the PIN or DTMF digits.

Audit O11-telephony-asterisk-cellular-01 / X03-security-backend-09: the IVR
printed the caller's digits AND the expected PIN on every wrong attempt, and
the ARI client printed every DTMF digit, so the journal (served by
/onboarding/logs/stream) held the PIN. These tests drive AsteriskIVR._run_pin
with a fake ARI client that "presses" digits, capture stdout, and assert:

  * no PIN configured -> a spoken refusal and False, never a digit prompt;
  * the digit count and the prompt come from the PIN length;
  * neither the PIN nor any typed digit string ever reaches stdout;
  * wrong PINs lock the caller out across calls (the next call is refused
    before it can guess again).
"""
import asyncio

import pytest

from Orchestrator import config
from Orchestrator.asterisk import client as ari_client_mod
from Orchestrator.asterisk import ivr as ivr_mod
from Orchestrator.phone import pin_gate

PIN = "739104"
WRONG = "552281"
CHANNEL = "chan-1"
CALLER = "+15555550142"


class FakeARI:
    """Plays nothing; 'presses' the next scripted entry when a PIN prompt plays.
    ``on_prompt`` runs before each PIN prompt (to simulate a concurrent call)."""

    def __init__(self, entries, on_prompt=None):
        self.entries = list(entries)
        self.played = []
        self.on_dtmf = None
        self.on_prompt = on_prompt

    async def play_sound(self, channel_id, media):
        self.played.append(media)
        if "pin_prompt" in media or "pin_retry" in media:
            if self.on_prompt:
                self.on_prompt(media)
            if self.entries:
                digits = self.entries.pop(0)

                async def press():
                    for d in digits:
                        await self.on_dtmf(channel_id, d)
                        await asyncio.sleep(0)

                asyncio.get_running_loop().create_task(press())
        return None  # no playback id -> _play returns immediately

    async def stop_playback(self, playback_id):
        return True

    async def wait_for_playback(self, playback_id, timeout=15.0):
        return True

    # The ARI client's per-channel routing (Orchestrator/asterisk/client.py)
    def register_dtmf_handler(self, channel_id, handler):
        self.on_dtmf = handler

    def unregister_dtmf_handler(self, channel_id):
        self.on_dtmf = None

    def watch_hangup(self, channel_id):
        return asyncio.Event()  # the caller never hangs up here

    def unwatch_hangup(self, channel_id):
        pass


@pytest.fixture(autouse=True)
def pin_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", True)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", PIN)
    monkeypatch.setattr(config, "PHONE_PIN_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pin_gate, "LOCKOUT_PATH", tmp_path / "phone_pin_lockout.json")
    requested = []

    def fake_pin_prompt_name(length):
        # Every prompt "installed": the real resolution is in test_asterisk_ivr_prompts.py.
        requested.append(length)
        return f"pin_prompt_{length}digit"

    monkeypatch.setattr(ivr_mod, "pin_prompt_name", fake_pin_prompt_name)
    monkeypatch.setattr(ivr_mod, "resolve_prompt", lambda name, *fallbacks: name)
    monkeypatch.setattr(ivr_mod.asyncio, "sleep", _fast_sleep)
    return requested


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *a, **kw):
    await _real_sleep(0)


def _run_pin(entries, caller=CALLER, on_prompt=None):
    fake = FakeARI(entries, on_prompt=on_prompt)
    ivr = ivr_mod.AsteriskIVR(ari_client=fake, channel_id=CHANNEL, caller_id=caller)
    fake.on_dtmf = ivr._on_dtmf
    ok = asyncio.run(ivr._run_pin())
    return ok, fake


def _assert_no_secrets(out, *secrets):
    for s in secrets:
        assert s not in out, f"{s!r} leaked to stdout"
    # No per-digit echo either (the old "DTMF: '7'" / "got '...'" lines).
    assert "DTMF: '" not in out and "got '" not in out and "expected" not in out


def test_correct_pin_accepted_without_logging_it(capsys, pin_env):
    ok, fake = _run_pin([PIN])
    out = capsys.readouterr().out
    assert ok is True
    assert pin_env == [len(PIN)], "prompt must be derived from the PIN length"
    assert "sound:custom/ivr/pin_prompt_6digit" in fake.played
    assert "sound:custom/ivr/pin_accepted" in fake.played
    _assert_no_secrets(out, PIN)


def test_wrong_pins_log_only_attempt_numbers(capsys):
    ok, fake = _run_pin([WRONG, WRONG, WRONG])
    out = capsys.readouterr().out
    assert ok is False
    assert "attempt 1" in out and "attempt 3" in out
    assert fake.played[-1] in ("sound:custom/ivr/pin_failed", "sound:custom/ivr/pin_locked")
    _assert_no_secrets(out, PIN, WRONG)


def test_eight_digit_pin_collects_eight_digits(monkeypatch, pin_env):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "73910482")
    ok, _ = _run_pin(["73910482"])
    assert ok is True
    assert pin_env == [8]


def test_unconfigured_pin_refuses_the_call(capsys, monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "")
    ok, fake = _run_pin([PIN])
    out = capsys.readouterr().out
    assert ok is False
    assert fake.played == ["sound:custom/ivr/pin_unavailable"], "refuse before any digit prompt"
    assert "PHONE_PIN_CODE" in out


def test_lockout_persists_into_the_next_call(capsys):
    ok, _ = _run_pin([WRONG, WRONG, WRONG])
    assert ok is False
    # Next call from the same number, even with the right PIN, is refused
    # before it is prompted for digits.
    ok2, fake2 = _run_pin([PIN])
    out = capsys.readouterr().out
    assert ok2 is False
    assert fake2.played == ["sound:custom/ivr/pin_locked"]
    _assert_no_secrets(out, PIN, WRONG)


def test_ivr_run_hangs_up_on_refusal(monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "")
    fake = FakeARI([])
    ivr = ivr_mod.AsteriskIVR(ari_client=fake, channel_id=CHANNEL, caller_id=CALLER)
    assert asyncio.run(ivr.run()) is None
    assert "sound:custom/ivr/backend_menu" not in fake.played


def test_ari_client_does_not_log_dtmf_digits(capsys):
    client = ari_client_mod.AsteriskARIClient(
        ari_url="http://127.0.0.1:1", ari_ws_url="ws://127.0.0.1:1/ari/events",
        username="u", password="p", app_name="blackbox")
    seen = []

    async def on_dtmf(channel_id, digit):
        seen.append(digit)

    client.register_dtmf_handler(CHANNEL, on_dtmf)

    async def drive():
        for d in PIN:
            await client._handle_event({"type": "ChannelDtmfReceived",
                                        "channel": {"id": CHANNEL}, "digit": d})
        await _real_sleep(0.01)

    asyncio.run(drive())
    out = capsys.readouterr().out
    assert "".join(seen) == PIN, "digits must still be delivered to the IVR"
    assert "digit=" not in out
    _assert_no_secrets(out, PIN)


def test_lock_tripped_by_a_concurrent_call_stops_this_call(capsys):
    """Each attempt re-checks the lockout: three parallel calls from one number
    must not each get max_attempts guesses (record_failure resets the count
    when it locks, so only the stored lock reveals the other call's lock)."""
    rounds = []

    def other_call_locks_us(media):
        rounds.append(media)
        if len(rounds) == 2:  # while this call is on its second attempt
            for _ in range(pin_gate.max_attempts()):
                pin_gate.record_failure(CALLER)

    ok, fake = _run_pin([WRONG, WRONG, WRONG], on_prompt=other_call_locks_us)
    assert ok is False
    assert len(rounds) == 2, "no further PIN prompt once the number is locked"
    assert fake.played[-1] == "sound:custom/ivr/pin_locked"
    _assert_no_secrets(capsys.readouterr().out, PIN, WRONG)


def test_parallel_calls_from_one_number_get_max_attempts_guesses_in_total(monkeypatch):
    """Three simultaneous calls from one number: entries already typed when the
    lock trips are refused, never judged, so at most max_attempts are checked."""
    judged = []
    real_check = pin_gate.check_pin

    def counting_check(entered):
        judged.append(entered)
        return real_check(entered)

    monkeypatch.setattr(ivr_mod.pin_gate, "check_pin", counting_check)

    async def three_calls():
        ivrs = []
        for ch in ("a", "b", "c"):
            fake = FakeARI([WRONG, WRONG, WRONG])
            ivr = ivr_mod.AsteriskIVR(ari_client=fake, channel_id=ch, caller_id=CALLER)
            fake.on_dtmf = ivr._on_dtmf
            ivrs.append(ivr)
        return await asyncio.gather(*(ivr._run_pin() for ivr in ivrs))

    assert asyncio.run(three_calls()) == [False, False, False]
    assert len(judged) <= pin_gate.max_attempts()
