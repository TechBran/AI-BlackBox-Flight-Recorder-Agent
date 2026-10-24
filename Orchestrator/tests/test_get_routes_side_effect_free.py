# Orchestrator/tests/test_get_routes_side_effect_free.py
"""GET routes must not change state (audit M3 U20 item 1).

A cross-site <img src=...> or link sends a GET with no Origin, so the M2
Origin guard never sees it (the Sec-Fetch-Site rule now refuses it in current
browsers; this file removes the side effects themselves). Every GET route was
reviewed; the ones that spawned, sent, wrote or consumed state are fixed here:

  * GET /chat/stream ran a whole tool-executing model turn from query params.
    Nothing calls it (Portal, Android and cron all POST), so it is gone.
  * GET /internet/speed-test pulled 10 MB down and pushed up over the metered
    cellular modem. Now POST (Android already POSTed and got a 405).
  * GET /twilio/cli/{id}/transcriptions DEQUEUED the caller's speech. Now POST.
  * GET /browser/screenshot captured the desktop into a new /ui/uploads file
    on every call. Now POST (no caller used the GET).
  * GET /watchdog marked tasks failed. It reports only; startup still sweeps.
  * GET /sms/messages marked the thread read. The clients now POST
    /sms/mark-read after they show it.
  * GET /contacts, /contacts/search and /api/cron/contacts created and saved
    a phone book for ANY operator name in the query. Only a roster operator's
    book is initialized now; any other name reads an empty/seed view.

Hermetic: handlers are called directly or through TestClient with every
side-effecting dependency monkeypatched, so even a regressed (red) run never
touches the modem, the screen, Twilio or the real contacts file.
"""
import asyncio
import json
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

import Orchestrator.app  # noqa: F401 — registers every route on the shared app
from Orchestrator.checkpoint import app


def _methods(path):
    out = set()
    for r in app.router.routes:
        if isinstance(r, Route) and r.path == path:
            out |= set(r.methods or ())
    return out


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# ------------------------------------------------------------ /chat/stream

def test_chat_stream_is_post_only():
    assert "GET" not in _methods("/chat/stream")
    assert "POST" in _methods("/chat/stream")


def test_get_chat_stream_never_starts_a_turn(client, monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    started = []

    async def fake_context(*a, **k):
        started.append("context")
        raise RuntimeError("must not be reached")
        yield  # pragma: no cover

    monkeypatch.setattr(cr, "_streaming_context_off_loop", fake_context)
    r = client.get("/chat/stream", params={
        "messages": json.dumps([{"role": "user", "content": "send an sms"}]),
        "provider": "gemini"})
    assert r.status_code == 405
    assert started == []


# ------------------------------------------------------- /internet/speed-test

class _FakeInternet:
    def __init__(self):
        self.runs = 0

    async def run_speed_test(self):
        self.runs += 1
        return {"download_mbps": 1.0, "upload_mbps": 1.0, "latency_ms": 1.0}


def test_speed_test_is_post_only(client, monkeypatch):
    import Orchestrator.routes.internet_routes as ir

    mgr = _FakeInternet()
    monkeypatch.setattr(ir, "_get_manager", lambda: mgr)
    assert client.get("/internet/speed-test").status_code == 405
    assert mgr.runs == 0
    r = client.post("/internet/speed-test")
    assert r.status_code == 200
    assert r.json()["download_mbps"] == 1.0
    assert mgr.runs == 1


# ------------------------------------- /twilio/cli/{id}/transcriptions (poll)

def test_cli_transcription_poll_is_post_only_and_get_never_drains(client, monkeypatch):
    import Orchestrator.routes.twilio_routes as tr

    sess = tr.CLIPhoneSession(session_id="cli-test", phone_session_id="p", operator="op")
    sess.transcription_queue = deque(["hello", "world"])
    monkeypatch.setitem(tr.CLI_PHONE_SESSIONS, "cli-test", sess)

    assert client.get("/twilio/cli/cli-test/transcriptions").status_code == 405
    assert list(sess.transcription_queue) == ["hello", "world"]

    r = client.post("/twilio/cli/cli-test/transcriptions")
    assert r.status_code == 200
    assert r.json()["transcriptions"] == ["hello", "world"]
    assert list(sess.transcription_queue) == []


def test_cli_call_instructions_advertise_post_poll():
    import inspect
    import Orchestrator.routes.twilio_routes as tr

    src = inspect.getsource(tr)
    assert "GET /twilio/cli/{" not in src
    assert src.count("POST /twilio/cli/{") >= 2


# ------------------------------------------------------- /browser/screenshot

def test_browser_screenshot_is_post_only(client, monkeypatch):
    import Orchestrator.browser.screenshot as shot

    taken = []
    monkeypatch.setattr(shot, "capture_screenshot", lambda *a, **k: taken.append(1) or b"\x89PNG")
    monkeypatch.setattr(shot, "save_screenshot_to_uploads",
                        lambda png, task_id, step: f"/ui/uploads/{task_id}_{step}.png")
    assert client.get("/browser/screenshot").status_code == 405
    assert taken == []
    r = client.post("/browser/screenshot")
    assert r.status_code == 200
    assert r.json()["screenshot_url"].startswith("/ui/uploads/")
    assert taken == [1]


# ------------------------------------------------------------- /watchdog

def test_watchdog_reports_without_marking_tasks_failed(monkeypatch):
    import Orchestrator.monitoring as monitoring
    import Orchestrator.routes.admin_routes as ar

    swept = []
    sweep = lambda **k: swept.append(k) or {"marked_failed": 0}  # noqa: E731
    monkeypatch.setattr(monitoring, "cleanup_stuck_tasks", sweep)
    monkeypatch.setattr(ar, "cleanup_stuck_tasks", sweep, raising=False)
    try:
        ar.watchdog()
    except Exception:
        pass   # an unhealthy sandbox may 503; only the side effect matters here
    assert swept == []


# ---------------------------------------------------------- /sms/messages

class _FakeStore:
    def __init__(self):
        self.marked = []

    def get_conversation(self, operator, phone, limit=50, offset=0, **window):
        return [{"id": 1, "body": "hi"}]

    def mark_all_read(self, operator, phone):
        self.marked.append((operator, phone))


def test_sms_messages_read_does_not_mark_read(monkeypatch):
    import Orchestrator.routes.sms_routes as sr

    store = _FakeStore()
    monkeypatch.setattr(sr, "_require_sms", lambda: None)
    monkeypatch.setattr(sr, "get_message_store", lambda: store)
    out = asyncio.run(sr.get_messages(operator="op", phone="+15555550123", limit=50, offset=0))
    assert out["messages"] == [{"id": 1, "body": "hi"}]
    assert store.marked == []

    asyncio.run(sr.mark_read(sr.SMSMarkReadRequest(operator="op", phone="+15555550123")))
    assert store.marked == [("op", "+15555550123")]


def _js(path):
    from Orchestrator.utils.paths import blackbox_root
    return (blackbox_root() / path).read_text(encoding="utf-8")


def test_portal_sms_inbox_marks_read_explicitly():
    src = _js("Portal/modules/sms-inbox.js")
    assert "/sms/mark-read" in src


def test_android_sms_marks_read_explicitly():
    src = _js("AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/"
              "app/src/main/java/com/aiblackbox/portal/ui/sms/SmsViewModel.kt")
    assert "/sms/mark-read" in src


def test_portal_speed_test_posts():
    src = _js("Portal/modules/cellular-manager.js")
    assert "fetch('/internet/speed-test', { method: 'POST' })" in src


# ------------------------------------------------- contacts: no phone book for
# an arbitrary query-string operator

@pytest.fixture
def contacts_store(tmp_path, monkeypatch):
    import Orchestrator.config as cfg
    import Orchestrator.contacts as contacts_mod

    cdir = tmp_path / "Contacts"
    cdir.mkdir()
    cfile = cdir / "contacts.json"
    cfile.write_text("{}")
    monkeypatch.setattr(contacts_mod, "CONTACTS_DIR", cdir)
    monkeypatch.setattr(contacts_mod, "CONTACTS_FILE", cfile)
    monkeypatch.setattr(cfg, "USERS_LIST", ["Ann"])
    return cfile


def _books(cfile):
    return set(json.loads(cfile.read_text()))


def test_contacts_get_for_unknown_operator_writes_nothing(contacts_store):
    from Orchestrator.routes.contacts_routes import list_contacts, search_contacts_endpoint
    from Orchestrator.routes.cron_routes import list_cron_contacts

    out = asyncio.run(list_contacts(operator="attacker-made-this"))
    assert out["operator"] == "attacker-made-this"
    asyncio.run(search_contacts_endpoint(operator="also-made-up", query="x"))
    asyncio.run(list_cron_contacts(operator="third-made-up"))
    assert _books(contacts_store) == set()


def test_contacts_get_still_seeds_a_roster_operator_once(contacts_store):
    """A real operator's first visit still creates the editable seed contact
    (its notes tell the operator to edit it), exactly once."""
    from Orchestrator.routes.contacts_routes import list_contacts

    first = asyncio.run(list_contacts(operator="Ann"))
    assert _books(contacts_store) == {"Ann"}
    again = asyncio.run(list_contacts(operator="Ann"))
    assert [c["id"] for c in first["contacts"]] == [c["id"] for c in again["contacts"]]
