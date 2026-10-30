"""The speech rule's box guard (M7 wave 3b, contract e; rules W17, W26, W31,
W32): a reply that used health data is never read aloud in v1. The on-box
voice writes a WAV under Portal/uploads/ and serves it at /ui/uploads/, and
every other voice is a service the health settings never allowed, so POST
/tts, /tts/batch and /tts/queue refuse such a text with 403 {"error":
"wellness_text", "detail": ...}.

Two rules decide, and the answer to a caller who proves nothing never
depends on anyone's stored values (review of wave 3b: a guard that checked
the text against the stored deliveries of whatever operator an UNSIGNED body
named let any tailnet caller read another operator's values back, one guess
at a time):

  * the text rule (mask.says_health_reading), for every request on a box
    where someone has health data: a number by a reading's own unit or noun
    ("47 bpm", "heart rate 52", "blood pressure 120 over 77", "12,345
    steps"), a blood-pressure pair ("118/79"), a sleep duration ("you slept
    6 h 47 min") or a count of awakenings. The same answer whoever asks and
    whoever the body names; inert on a box where nobody enrolled (D19);
  * the recall rule (mask.speech_recalls: mask.carry_for's distinctive
    quotes of the operator's deliveries of the last 24 hours), only for a
    request the operator's own active phone signed (X-BlackBox-Origin, the
    check every signed route makes). A bad proof is refused as GET
    /wellness/status refuses it; a body naming another operator is 400
    operator_mismatch.

Ordinary numbers pass, as does a text whose health numbers are already the
mask glyph. Nothing about the text is logged.

Hermetic: the real app in-process over a temp box; no synthesis runs (a text
the guard lets through stops at a provider branch the test stubs to fall
back).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

import pytest

from Orchestrator.tests.wellness_ledger_support import ALL_ASK, deliver, envelopes
from Orchestrator.tests.wellness_support import (OPERATOR, close_box, enrolled_fixture, make_box,
                                                 roster)
from Orchestrator.wellness import enroll, mask

DETAIL = "Health replies aren't read aloud: the voice service would get your health data."
# Health readings in any reply, whoever's they are: refused for every caller.
READINGS = (
    "Your sleeping heart rate was 47.3 last night.", "You walked 12,345 steps.",
    "It read 118/79 this morning.", "Your sleeping heart rate was 47 bpm.",
    "You slept 6 h 47 min last night, about 35 min less than usual. Deep sleep was 1 h 47 min "
    "and REM 1 h 49 min.",
    "You slept about seven hours. You went to bed at 23:34 and woke at 06:26.",
    "Your blood pressure averaged 120 over 77 yesterday, within your usual range.",
    "Resting heart rate: 47. Sleeping heart rate: 46.",
    "Your sleeping heart rate was forty-seven beats per minute, lower than usual.",
    "That was 118 over 79.", "twelve thousand three hundred forty-five steps",
    "You woke up 3 times last night.", "You were woken up 3 times last night.",
    "There were 4 awakenings.", "Your SpO2 averaged 96% and your HRV was 43 ms.",
    "Dormiste 7 horas y 2 minutos.", "Te despertaste 3 veces anoche.",
    "Tu presión arterial fue 118/79.", "Caminaste 12.345 pasos.", "Tu pulso fue 47 lpm.",
    "You walked 12 345 steps.", "Tu frecuencia cardíaca en reposo fue 52.",
    "Blood pressure 118 / 79 this morning.", "Systolic 118, diastolic 79.",
)
# Ordinary text: passes for every caller, with or without a proof.
ORDINARY = (
    "Your sleeping heart rate was [·], a bit lower than usual.",
    "Your blood pressure was [·]/[·] and you slept [·].",
    "The meeting is at 3:30 and runs 45 minutes; 2 of 7 items are left.",
    "Version 47 of the plan has 12 steps.",
    "The team raised 10,000 dollars and hired 1,000 people.",
    "Install Python 3.12.7 before the meeting.", "The invoice total is 1,200 euros.",
    "We shipped 12,000 units this quarter.", "Our server handles 1.2k requests a second.",
    "CPU sits at 95% for 120 ms bursts.", "The build takes 30 minutes.",
    "We're under pressure to ship 3 features by Friday.", "Step 12 of the plan is done.",
    "I called the office 3 times today.", "Walk through the demo in 30 minutes.",
    "The run took 20 min.", "The 5 km loop by the river is closed.",
)
# A health value with nothing around it that says so: only the operator's
# own signed request can tell it is theirs (the recall rule).
BARE = ("It was 47.3.", "You did 12,345 that day.")


@pytest.fixture
def tts_box(isolated_stores, toolvault_embed_sync, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from Orchestrator.app import app
    monkeypatch.setenv("BLACKBOX_TESTING", "1")
    roster(monkeypatch, OPERATOR, "other-human", "Flight Recorder")
    _rec, st, _data = enrolled_fixture(tmp_path, monkeypatch, categories=ALL_ASK)
    envs = envelopes()
    now = int(time.time() * 1000)
    for key in ("night", "steps", "bp"):
        deliver(st, envs[key], f"wt_{key}", ts=now - 3600 * 1000)
    _no_synthesis(monkeypatch)
    with TestClient(app) as client:
        yield {"client": client, "store": st, "envs": envs}


def _no_synthesis(monkeypatch):
    """A text the guard lets through stops at a branch that synthesises nothing."""
    from Orchestrator import qwen_tts
    from Orchestrator.routes import tts_routes
    monkeypatch.setattr(tts_routes, "AUDIO_ENGINE", "browser")
    monkeypatch.setattr(tts_routes, "OPENAI_API_KEY", "")
    monkeypatch.setattr(qwen_tts, "_tts_available", lambda: False)


ROUTES = {
    "/tts": lambda text, op: {"text": text, "provider": "openai", "voice": "openai:alloy",
                              **({"operator": op} if op is not None else {})},
    "/tts/batch": lambda text, op: {"text": text, "provider": "openai", "voice": "alloy",
                                    **({"operator": op} if op is not None else {})},
    "/tts/queue": lambda text, op: {"text": text, "provider": "qwen", "voice": "qwen:serena",
                                    **({"operator": op} if op is not None else {})},
}
# What each route answers once the guard has let a text through (no synthesis).
THROUGH = {"/tts": 200, "/tts/batch": 400, "/tts/queue": 503}
CALLERS = (OPERATOR, "other-human", "nobody-enrolled", "", None)


def _post(box, route, text, operator=OPERATOR):
    return box["client"].post(route, json=ROUTES[route](text, operator))


# ------------------------------------------------------------------ the text rule

@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_health_reading_is_refused_whoever_asks(tts_box, route):
    for text in READINGS:
        for operator in CALLERS:
            r = _post(tts_box, route, text, operator)
            assert r.status_code == 403, (route, text, operator, r.text)
            assert r.json() == {"error": "wellness_text", "detail": DETAIL}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_ordinary_text_the_glyph_and_a_bare_value_pass_unsigned(tts_box, route):
    for text in ORDINARY + BARE:
        for operator in CALLERS:
            r = _post(tts_box, route, text, operator)
            assert r.status_code == THROUGH[route], (route, text, operator, r.text)


def test_an_unproven_caller_learns_nothing_from_the_answer(tts_box):
    """The review's exploit as a regression: an unsigned body names the
    enrolled operator and sweeps guesses. Every answer is the one a caller
    naming anyone else (or nobody) gets, and stays the same when the store
    holds other values: the guard is no way to read a value back, nor to
    tell whether that operator is enrolled."""
    values = mask.value_set(tts_box["envs"][k] for k in ("night", "steps", "bp"))
    guesses = ([f"It was {t / 10:.1f}." for t in range(400, 560)]
               + [f"{n} bpm" for n in range(40, 60)]
               + [f"You did {n:,} that day." for n in (12300, 12340, 12345, 12346)]
               + [f"BP {s}/{d}" for s, d in ((117, 79), (118, 79), (119, 80))])
    assert {"47.3", "12,345"} <= values and "It was 47.3." in guesses

    def sweep():
        return {g: tuple(_post(tts_box, "/tts/queue", g, op).status_code
                         for op in (OPERATOR, "other-human", None)) for g in guesses}

    first = sweep()
    for guess, answers in first.items():
        assert len(set(answers)) == 1, (guess, answers)
    assert first["It was 47.3."] == (THROUGH["/tts/queue"],) * 3
    assert first["47 bpm"] == first["58 bpm"] == (403,) * 3
    # More deliveries, other values: not one answer moves.
    now = int(time.time() * 1000)
    for key in ("week", "series", "sleeps"):
        deliver(tts_box["store"], tts_box["envs"][key], f"wt_more_{key}", ts=now - 60_000)
    assert sweep() == first


def test_the_text_rule_is_inert_until_someone_enrolls(isolated_stores, toolvault_embed_sync,
                                                     monkeypatch):
    """D19: on a box where nobody has health data, TTS is what it was."""
    from fastapi.testclient import TestClient

    from Orchestrator.app import app
    monkeypatch.setenv("BLACKBOX_TESTING", "1")
    roster(monkeypatch, OPERATOR, "other-human")
    _no_synthesis(monkeypatch)
    assert not enroll.referenced_op_keys()
    with TestClient(app) as client:
        box = {"client": client}
        for route in sorted(ROUTES):
            for text in READINGS[:4]:
                assert _post(box, route, text).status_code == THROUGH[route], (route, text)


def test_the_text_rule_reads_the_words_alone():
    """mask.says_health_reading: no store, no operator, no clock."""
    for text in READINGS:
        assert mask.says_health_reading(text), text
    for text in ORDINARY + BARE + ("", "   ", "forty-seven point three"):
        assert not mask.says_health_reading(text), text
    assert not mask.says_health_reading(None)
    # Steps are thousands; a blood-pressure pair is systolic over diastolic.
    assert not mask.says_health_reading("Take 300 steps to the left.")
    assert mask.says_health_reading("You took 9,800 steps.")
    assert mask.says_health_reading("About 12.3k steps today.")
    for pair in ("Split it 3/4.", "Due 12/05.", "Scored 98/100.", "Ratio 1920/1080."):
        assert not mask.says_health_reading(pair), pair


# ------------------------------------------------------------------ the recall rule (signed)

@pytest.fixture
def signed_box(isolated_stores, toolvault_embed_sync, monkeypatch):
    """The owner's phone enrolled through the real handshake, one fresh
    heart delivery (47.3) in the store."""
    monkeypatch.setenv("BLACKBOX_TESTING", "1")
    box = make_box(isolated_stores, monkeypatch)
    assert box.enroll().status_code == 200
    _no_synthesis(monkeypatch)
    box.store = enroll.store_for(enroll.get_enrollment(OPERATOR), create=False)
    box.deliver = lambda ref, hours_ago: box.store.record_delivery(
        ref, None, "android", "anthropic", "model-test", "wellness_summary", {}, ["heart"],
        {"ok": True, "tool": "wellness_summary", "ref": ref,
         "metrics": [{"metric": "sleeping_hr", "unit": "bpm", "value": 47.3,
                      "display": "47.3 bpm"},
                     {"metric": "steps", "unit": "steps", "value": 12345.0}]},
        ts=int(time.time() * 1000) - int(hours_ago * 3600 * 1000))
    box.deliver("w-20261030-0a0a", 1)
    yield box
    close_box(box)


def _say(box, route, text, *, operator=OPERATOR, signed=True, headers=None):
    return box.post(route, ROUTES[route](text, operator), signed=signed, headers=headers)


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_the_owner_s_signed_request_is_held_to_their_own_values(signed_box, route):
    for text in BARE:
        assert _say(signed_box, route, text).status_code == 403, (route, text)
        assert _say(signed_box, route, text, operator=None).status_code == 403, (route, text)
        assert _say(signed_box, route, text, signed=False).status_code == THROUGH[route]
    for text in ORDINARY:
        assert _say(signed_box, route, text).status_code == THROUGH[route], (route, text)
    assert _say(signed_box, route, READINGS[3]).status_code == 403


def test_a_bad_proof_is_refused_like_the_status_and_a_named_stranger_is_400(signed_box):
    body = json.dumps(ROUTES["/tts/batch"](BARE[0], OPERATOR)).encode()
    # Signed for another path: the status route gives this header 401 too.
    bad = signed_box.phone.headers("POST", "/tts/queue", body)
    r = signed_box.post("/tts/batch", raw=body, headers=bad)
    assert r.status_code == 401 and r.json() == {"error": "bad_signature"}
    status = signed_box.get(f"/wellness/status?operator={OPERATOR}",
                            headers={"X-BlackBox-Origin": "v1 nonsense"})
    r = signed_box.post("/tts/batch", raw=body, headers={"X-BlackBox-Origin": "v1 nonsense"})
    assert (r.status_code, r.json()) == (status.status_code, status.json())
    assert r.status_code == 401
    other = _say(signed_box, "/tts/batch", BARE[0], operator="other-human")
    assert other.status_code == 400 and other.json() == {"error": "operator_mismatch"}


def test_the_recall_window_is_24_hours_and_doubt_refuses(signed_box, monkeypatch):
    now = int(time.time() * 1000)
    assert mask.SPEECH_WINDOW_MS == 24 * 3600 * 1000
    assert mask.speech_recalls(OPERATOR, BARE[0], now_ms=now)
    assert mask.speech_recalls(OPERATOR, BARE[0], now_ms=now + 22 * 3600 * 1000)
    assert not mask.speech_recalls(OPERATOR, BARE[0], now_ms=now + 24 * 3600 * 1000)
    assert not mask.speech_recalls(OPERATOR, ORDINARY[2], now_ms=now)
    assert not mask.speech_recalls("other-human", BARE[0], now_ms=now)
    assert not mask.speech_recalls(None, BARE[0], now_ms=now)
    # A delivery 25 hours old: the owner's signed request speaks it.
    with signed_box.store.write() as conn:
        conn.execute("DELETE FROM delivered")
    signed_box.deliver("w-20261029-0b0b", 25)
    assert _say(signed_box, "/tts/batch", BARE[0]).status_code == THROUGH["/tts/batch"]
    signed_box.deliver("w-20261030-0c0c", 23)
    assert _say(signed_box, "/tts/batch", BARE[0]).status_code == 403

    # A health store that cannot be read: the guard cannot tell, so it refuses.
    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(mask, "_recent_history_rows", broken)
    assert mask.speech_recalls(OPERATOR, ORDINARY[2], now_ms=now)
    assert _say(signed_box, "/tts/batch", ORDINARY[2]).status_code == 403
    assert _say(signed_box, "/tts/batch", ORDINARY[2], signed=False).status_code == \
        THROUGH["/tts/batch"]


def test_with_the_kill_switch_off_only_the_text_rule_runs(signed_box, monkeypatch):
    from Orchestrator import config
    monkeypatch.setattr(config, "wellness_enabled", lambda: False)
    assert _say(signed_box, "/tts/batch", BARE[0]).status_code == THROUGH["/tts/batch"]
    assert _say(signed_box, "/tts/batch", READINGS[3]).status_code == 403


def test_the_guard_refuses_in_process_callers_by_the_text_alone(tts_box):
    """A direct call (no request, no proof): the text rule only."""
    from Orchestrator.routes import tts_routes
    refusal = tts_routes.wellness_text_refusal(READINGS[0], operator=OPERATOR)
    assert refusal.status_code == 403
    assert json.loads(refusal.body) == {"error": "wellness_text", "detail": DETAIL}
    for text in (ORDINARY[2], BARE[0], "", None):
        assert tts_routes.wellness_text_refusal(text, operator=OPERATOR) is None, text


def test_nothing_about_the_text_is_logged(signed_box, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    for route in sorted(ROUTES):
        assert _say(signed_box, route, READINGS[0], signed=False).status_code == 403
        assert _say(signed_box, route, READINGS[2], signed=False).status_code == 403
        assert _say(signed_box, route, BARE[0]).status_code == 403
    out = capsys.readouterr()
    printed = out.out + out.err + "\n".join(r.getMessage() for r in caplog.records)
    assert "47.3" not in printed and "118/79" not in printed
    assert "sleeping heart rate" not in printed and "It was" not in printed
