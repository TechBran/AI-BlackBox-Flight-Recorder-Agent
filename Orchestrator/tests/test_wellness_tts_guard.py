"""The speech rule's box guard (M7 wave 3b, contract e; rules W26, W31, W32):
a reply that used health data is never read aloud in v1. The on-box voice
writes a WAV under Portal/uploads/ and serves it at /ui/uploads/, and every
other voice is a service the health settings never allowed, so POST /tts,
/tts/batch and /tts/queue refuse a text that RECALLS a wellness delivery of
the body's operator with 403 {"error": "wellness_text", "detail": ...}.

The recall rule is mask.carry_for's: only the distinctive forms (a decimal,
grouped thousands, a pair, a number by a reading's own unit) of a result the
operator was delivered recently count, so ordinary numbers pass, as does a
text whose health numbers are already the mask glyph and a body that names
no operator. Nothing about the text is logged.

Hermetic: the real app in-process over a temp box with the fixture operator
enrolled and one fresh delivered row; no synthesis runs (a text the guard
lets through stops at a provider branch the test stubs to fall back).
"""
from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from Orchestrator.tests.wellness_ledger_support import ALL_ASK, deliver, envelopes
from Orchestrator.tests.wellness_support import OPERATOR, enrolled_fixture, roster
from Orchestrator.wellness import mask

DETAIL = "Health replies aren't read aloud: the voice service would get your health data."
RECALLS = ("Your sleeping heart rate was 47.3 last night.", "You walked 12,345 steps.",
           "It read 118/79 this morning.", "Your sleeping heart rate was 47 bpm.")
PASSES = ("Your sleeping heart rate was [·], a bit lower than usual.",
          "The meeting is at 3:30 and runs 45 minutes; 2 of 7 items are left.",
          "Version 47 of the plan has 12 steps.")


@pytest.fixture
def tts_box(isolated_stores, toolvault_embed_sync, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from Orchestrator.app import app
    from Orchestrator.routes import tts_routes
    monkeypatch.setenv("BLACKBOX_TESTING", "1")
    roster(monkeypatch, OPERATOR, "other-human", "Flight Recorder")
    _rec, st, _data = enrolled_fixture(tmp_path, monkeypatch, categories=ALL_ASK)
    envs = envelopes()
    now = int(time.time() * 1000)
    for key in ("night", "steps", "bp"):
        deliver(st, envs[key], f"wt_{key}", ts=now - 3600 * 1000)
    # A text the guard lets through stops at a branch that synthesises nothing.
    monkeypatch.setattr(tts_routes, "AUDIO_ENGINE", "browser")
    monkeypatch.setattr(tts_routes, "OPENAI_API_KEY", "")
    from Orchestrator import qwen_tts
    monkeypatch.setattr(qwen_tts, "_tts_available", lambda: False)
    with TestClient(app) as client:
        yield {"client": client, "store": st}


ROUTES = {
    "/tts": lambda text, op: {"text": text, "provider": "openai", "voice": "openai:alloy",
                              **({"operator": op} if op else {})},
    "/tts/batch": lambda text, op: {"text": text, "provider": "openai", "voice": "alloy",
                                    **({"operator": op} if op else {})},
    "/tts/queue": lambda text, op: {"text": text, "provider": "qwen", "voice": "qwen:serena",
                                    **({"operator": op} if op else {})},
}
# What each route answers once the guard has let a text through (no synthesis).
THROUGH = {"/tts": 200, "/tts/batch": 400, "/tts/queue": 503}


def _post(box, route, text, operator=OPERATOR):
    return box["client"].post(route, json=ROUTES[route](text, operator))


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_text_that_recalls_a_health_result_is_refused(tts_box, route):
    for text in RECALLS:
        r = _post(tts_box, route, text)
        assert r.status_code == 403, (route, text, r.text)
        assert r.json() == {"error": "wellness_text", "detail": DETAIL}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_ordinary_text_the_glyph_and_no_operator_pass(tts_box, route):
    for text in PASSES:
        r = _post(tts_box, route, text)
        assert r.status_code == THROUGH[route], (route, text, r.text)
    for operator in (None, "", "other-human", "nobody-enrolled"):
        r = _post(tts_box, route, RECALLS[0], operator)
        assert r.status_code == THROUGH[route], (route, operator, r.text)


def test_the_guard_reads_the_recall_window_and_refuses_on_doubt(tts_box, monkeypatch):
    from Orchestrator.routes import tts_routes
    assert tts_routes.wellness_text_refusal(OPERATOR, RECALLS[0]).status_code == 403
    assert tts_routes.wellness_text_refusal(OPERATOR, PASSES[1]) is None
    assert tts_routes.wellness_text_refusal(OPERATOR, "") is None
    assert tts_routes.wellness_text_refusal(None, RECALLS[0]) is None
    # Older than the recall window: nothing recent to repeat.
    real = mask._now_ms
    monkeypatch.setattr(mask, "_now_ms", lambda: real() + mask.HISTORY_WINDOW_MS + 7200 * 1000)
    assert tts_routes.wellness_text_refusal(OPERATOR, RECALLS[0]) is None
    monkeypatch.setattr(mask, "_now_ms", real)

    # A health store that cannot be read: the guard cannot tell, so it refuses.
    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(mask, "_recent_history_rows", broken)
    assert tts_routes.wellness_text_refusal(OPERATOR, PASSES[1]).status_code == 403


def test_nothing_about_the_text_is_logged(tts_box, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    for route in sorted(ROUTES):
        assert _post(tts_box, route, RECALLS[0]).status_code == 403
        assert _post(tts_box, route, RECALLS[2]).status_code == 403
    out = capsys.readouterr()
    printed = out.out + out.err + "\n".join(r.getMessage() for r in caplog.records)
    assert "47.3" not in printed and "118/79" not in printed
    assert "sleeping heart rate" not in printed
