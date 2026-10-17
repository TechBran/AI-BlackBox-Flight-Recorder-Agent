"""Cellular IVR PIN stage: same fail-closed gate, no PIN or digits in logs.

Mirrors test_asterisk_ivr_pin.py for Orchestrator/cellular/ivr.py, which had
the identical expected-PIN log line (audit O11-telephony-asterisk-cellular-01).
Prompts are recorded instead of synthesized (no TTS, no network); digits are
injected through the same on_dtmf_digit entry point the modem URC handler uses.
"""
import asyncio

import pytest

from Orchestrator import config
from Orchestrator.cellular import ivr as ivr_mod
from Orchestrator.phone import pin_gate

PIN = "739104"
WRONG = "552281"
CALLER = "+15555550142"


@pytest.fixture(autouse=True)
def pin_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", True)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", PIN)
    monkeypatch.setattr(config, "PHONE_PIN_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pin_gate, "LOCKOUT_PATH", tmp_path / "phone_pin_lockout.json")


def _make_ivr(entries, caller=CALLER):
    ivr = ivr_mod.CellularIVR(send_audio=None, caller_id=caller, timeout_ms=2000)
    ivr.played = []
    script = list(entries)

    async def fake_play(text, voice=None, allow_barge_in=True):
        ivr.played.append(text)
        ivr._dtmf_buffer = ""
        ivr._dtmf_event.clear()
        if "access code" in text or "incorrect. Please try again" in text:
            if script:
                digits = script.pop(0)

                async def press():
                    for d in digits:
                        # Space presses out past the 500 ms duplicate-digit debounce.
                        ivr._last_dtmf_time -= 1.0
                        await ivr.on_dtmf_digit(d)
                        await asyncio.sleep(0)

                asyncio.get_running_loop().create_task(press())

    ivr._play_prompt = fake_play
    return ivr


def test_correct_pin_accepted_prompt_from_length(capsys):
    ivr = _make_ivr([PIN])
    assert asyncio.run(ivr.run_pin_verification()) is True
    out = capsys.readouterr().out
    assert any(f"{len(PIN)}-digit" in p for p in ivr.played)
    assert not any("4-digit" in p for p in ivr.played)
    assert PIN not in out and "got '" not in out and "expected" not in out


def test_wrong_pins_log_only_attempt_numbers(capsys):
    ivr = _make_ivr([WRONG, WRONG, WRONG])
    assert asyncio.run(ivr.run_pin_verification()) is False
    out = capsys.readouterr().out
    assert "attempt 1" in out and "attempt 3" in out
    for secret in (PIN, WRONG):
        assert secret not in out
    assert "DTMF received: '" not in out


def test_unconfigured_pin_refuses(capsys, monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "")
    ivr = _make_ivr([PIN])
    assert asyncio.run(ivr.run_pin_verification()) is False
    out = capsys.readouterr().out
    assert len(ivr.played) == 1 and "access code" not in ivr.played[0]
    assert "PHONE_PIN_CODE" in out


def test_lockout_carries_into_next_call(capsys):
    assert asyncio.run(_make_ivr([WRONG, WRONG, WRONG]).run_pin_verification()) is False
    nxt = _make_ivr([PIN])
    assert asyncio.run(nxt.run_pin_verification()) is False
    assert len(nxt.played) == 1 and "try again later" in nxt.played[0].lower()
