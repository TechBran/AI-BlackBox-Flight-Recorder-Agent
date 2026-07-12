"""P6 workstream 5 — Gemini affective dialog + proactive audio (2.5-native-audio family, v1alpha only).

Pins:
1. gemini_live_url() derives v1beta/v1alpha endpoints; GEMINI_LIVE_URL back-compat exact.
2. GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS = the 2.5-native-audio family EXACTLY (3.1 rejects the fields).
Later tasks (P6.11-P6.13) append session-persistence, flag-resolution, and setup-emission tests here.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.config import (
    GEMINI_LIVE_URL,
    GEMINI_LIVE_URL_TEMPLATE,
    gemini_live_url,
    GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS,
)


def test_gemini_live_url_versions():
    assert gemini_live_url() == GEMINI_LIVE_URL
    assert ".v1beta." in gemini_live_url()
    assert gemini_live_url("v1alpha") == GEMINI_LIVE_URL.replace("v1beta", "v1alpha")
    with pytest.raises(ValueError):
        gemini_live_url("v2wrong")


def test_gemini_live_url_backcompat_exact():
    # Byte-exact guard: routes + phone bridge import this constant today.
    assert GEMINI_LIVE_URL == (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )


def test_affective_capable_models_exact():
    # 2.5-native-audio family ONLY — 3.1 rejects enableAffectiveDialog/proactivity in setup.
    assert GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS == frozenset({
        "gemini-2.5-flash-native-audio-latest",
        "gemini-2.5-flash-native-audio-preview-12-2025",
    })
    assert "gemini-3.1-flash-live-preview" not in GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS


from Orchestrator.models import GeminiLiveSession
from Orchestrator.routes.gemini_live_routes import resolve_affective_flags


def test_session_persists_affective_flags_default_false():
    s = GeminiLiveSession(session_id="t1")
    assert s.affective_dialog is False
    assert s.proactive_audio is False


def test_resolve_flags_accepted_on_25():
    # JSON-bool and query-string forms both accepted
    a, p, err = resolve_affective_flags(
        "gemini-2.5-flash-native-audio-latest", "true", True)
    assert (a, p, err) == (True, True, None)


def test_resolve_flags_off_by_default():
    a, p, err = resolve_affective_flags(
        "gemini-2.5-flash-native-audio-latest", None, None)
    assert (a, p, err) == (False, False, None)


def test_resolve_flags_rejected_on_31():
    a, p, err = resolve_affective_flags(
        "gemini-3.1-flash-live-preview", "true", "false")
    assert (a, p) == (False, False)
    assert err is not None
    assert "gemini-3.1-flash-live-preview" in err
    assert "2.5" in err


def test_resolve_flags_garbage_treated_false():
    a, p, err = resolve_affective_flags(
        "gemini-2.5-flash-native-audio-latest", "DROP TABLE", {"x": 1})
    assert (a, p, err) == (False, False, None)


@pytest.mark.asyncio
async def test_connect_url_version_selection(monkeypatch):
    import Orchestrator.routes.gemini_live_routes as glr

    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return MagicMock()

    monkeypatch.setattr(glr, "websockets", SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(glr, "WEBSOCKETS_AVAILABLE", True)
    monkeypatch.setattr(glr, "GOOGLE_API_KEY", "test-key")

    # Default flags (False/False) -> v1beta
    s = GeminiLiveSession(session_id="t-beta")
    assert await glr.connect_to_gemini(s) is True
    assert ".v1beta." in captured["url"]
    assert "key=test-key" in captured["url"]

    # Either flag set -> v1alpha (real dataclass: proves P1a reconnect re-derives
    # the same URL from persisted session state, not from request plumbing)
    s2 = GeminiLiveSession(session_id="t-alpha")
    s2.affective_dialog = True
    assert await glr.connect_to_gemini(s2) is True
    assert ".v1alpha." in captured["url"]

    s3 = GeminiLiveSession(session_id="t-alpha-2")
    s3.proactive_audio = True
    assert await glr.connect_to_gemini(s3) is True
    assert ".v1alpha." in captured["url"]


@pytest.fixture
def stub_fossil_context(monkeypatch):
    """Stub snapshot retrieval (same pattern as test_live_models.py)."""
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.gemini_live_routes.build_fossil_context", _stub)


def _make_gemini_session(affective=False, proactive=False):
    session = MagicMock()
    session.gemini_ws = MagicMock()
    session.gemini_ws.send = AsyncMock()
    session.resumption_handle = None
    session.provenance = {}
    session.context_injected = False
    session.voice = ""
    # Explicit bools — a bare MagicMock attribute is truthy and would fake-enable
    session.affective_dialog = affective
    session.proactive_audio = proactive
    return session


def _sent_setup(session):
    raw = session.gemini_ws.send.await_args.args[0]
    return json.loads(raw)["setup"]


@pytest.mark.asyncio
async def test_configure_emits_affective_fields_on_25(stub_fossil_context):
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    session = _make_gemini_session(affective=True, proactive=True)
    await configure_gemini_session(
        session, "test_operator", "Charon",
        model="gemini-2.5-flash-native-audio-latest")
    setup = _sent_setup(session)
    assert setup["enableAffectiveDialog"] is True
    assert setup["proactivity"] == {"proactiveAudio": True}


@pytest.mark.asyncio
async def test_configure_emits_affective_only(stub_fossil_context):
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    session = _make_gemini_session(affective=True, proactive=False)
    await configure_gemini_session(
        session, "test_operator", "Charon",
        model="gemini-2.5-flash-native-audio-latest")
    setup = _sent_setup(session)
    assert setup["enableAffectiveDialog"] is True
    assert "proactivity" not in setup


@pytest.mark.asyncio
async def test_configure_suppresses_affective_on_31(stub_fossil_context, capsys):
    # Defense in depth: even if flags land on the session, NEVER emit at 3.1
    # (Google closes the WS on unknown setup fields — the June silent-failure class).
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    session = _make_gemini_session(affective=True, proactive=True)
    await configure_gemini_session(
        session, "test_operator", "Charon",
        model="gemini-3.1-flash-live-preview")
    setup = _sent_setup(session)
    assert "enableAffectiveDialog" not in setup
    assert "proactivity" not in setup
    assert "ignored" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_configure_default_session_emits_nothing(stub_fossil_context):
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    session = _make_gemini_session()
    await configure_gemini_session(
        session, "test_operator", "Charon",
        model="gemini-2.5-flash-native-audio-latest")
    setup = _sent_setup(session)
    assert "enableAffectiveDialog" not in setup
    assert "proactivity" not in setup
