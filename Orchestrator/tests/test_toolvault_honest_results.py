"""ToolVault executors report what actually happened (audit rank 57: O18-toolvault-modules-01,
-04, -07, -08, O12-messaging-15).

  * analyze_image / analyze_video call the model themselves: they used to POST to
    /chat, which now only queues a chat turn ({task_id, status}), so they
    returned an EMPTY analysis as success while the queued turn later minted a
    "Describe this image" exchange into the operator's ledger;
  * analyze_audio waits for its task and returns the analysis;
  * a Google token refreshed by the API transport (on a 401) is persisted;
    gmail_reply never sends to an empty address; the Gmail calls run off the
    event loop and the journal gets no recipient or subject;
  * make_voice_call renders no TTS it then throws away, nothing dials the
    /cellular/call route that does not exist, "auto" picks Asterisk when it is
    enabled, numbers are normalized to E.164 (or refused), and a call route's
    200 {"error": ...} is a failure.

Hermetic: the BlackBox routes are a stub HTTP server on 127.0.0.1, the model and
Google are monkeypatched, uploads live in a throwaway BLACKBOX_ROOT.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests

from Orchestrator import config
from Orchestrator.toolvault.context import ToolContext
from Orchestrator.utils import paths

pytestmark = pytest.mark.timeout(60)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def _executor(name):
    return importlib.import_module(f"ToolVault.tools.{name}.executor")


# ── fixtures: a fresh box, a stub BlackBox API, a fake model ──────────────────────

@pytest.fixture
def box(tmp_path):
    root = tmp_path / "box"
    uploads = root / "Portal" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "red.png").write_bytes(PNG)
    (uploads / "clip.mp4").write_bytes(MP4)
    (uploads / "memo.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    (root / ".env").write_text("NOT_FOR_THE_MODEL=1\n")
    old = os.environ.get("BLACKBOX_ROOT")
    os.environ["BLACKBOX_ROOT"] = str(root)
    paths.blackbox_root.cache_clear()
    try:
        yield root
    finally:
        if old is None:
            os.environ.pop("BLACKBOX_ROOT", None)
        else:
            os.environ["BLACKBOX_ROOT"] = old
        paths.blackbox_root.cache_clear()


class _Stub:
    """A tiny BlackBox API on 127.0.0.1: routes map (method, path) to a callable
    returning (status, body); every request is recorded."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                stub.requests.append((method, self.path, body))
                handler = stub.routes.get((method, self.path))
                status, payload = handler(body) if handler else (404, {"detail": "Not Found"})
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self._serve("POST")

            def do_GET(self):
                self._serve("GET")

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [p for _, p, _ in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    servers = []

    def make(routes):
        s = _Stub(routes)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


_CHAT_QUEUE_SHAPE = {"task_id": "chat-1", "status": "pending", "message": "Chat request queued for processing"}


class _ModelReply:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.reason = "OK" if status == 200 else "Bad Request"

    def json(self):
        return self._body


@pytest.fixture
def model(monkeypatch):
    """The Gemini REST endpoint, faked at requests.post."""
    calls = []
    reply = {"body": {"candidates": [{"content": {"parts": [{"text": "A small red square."}]}}]},
             "status": 200}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        calls.append(SimpleNamespace(url=url, headers=headers or {}, json=json))
        return _ModelReply(reply["body"], reply["status"])

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-google-key")
    return SimpleNamespace(calls=calls, reply=reply)


def _ctx(base_url, operator="Alice"):
    return ToolContext(operator=operator, base_url=base_url)


# ── analyze_image / analyze_video ─────────────────────────────────────────────

def test_analyze_image_returns_the_models_answer_and_queues_no_chat_turn(box, stub, model):
    api = stub({("POST", "/chat"): lambda body: (200, _CHAT_QUEUE_SHAPE)})
    res = asyncio.run(_executor("analyze_image").execute(
        {"image_url": "/ui/uploads/red.png", "prompt": "What colour is it?"}, _ctx(api.url)))
    assert res.success is True
    assert res.result == "A small red square."
    assert "/chat" not in api.paths(), "a chat turn was queued (it mints into the ledger)"
    (call,) = model.calls
    assert config.resolve_default_model("google") in call.url
    assert call.headers.get("x-goog-api-key") == "test-google-key"
    parts = call.json["contents"][0]["parts"]
    assert parts[0] == {"text": "What colour is it?"}
    assert parts[1]["inline_data"] == {"mime_type": "image/png", "data": base64.b64encode(PNG).decode()}


def test_analyze_image_with_no_answer_is_a_failure(box, stub, model):
    api = stub({("POST", "/chat"): lambda body: (200, _CHAT_QUEUE_SHAPE)})
    model.reply["body"] = {"promptFeedback": {"blockReason": "SAFETY"}}
    res = asyncio.run(_executor("analyze_image").execute({"image_url": "/ui/uploads/red.png"}, _ctx(api.url)))
    assert res.success is False
    assert "SAFETY" in res.result


def test_analyze_image_reports_a_provider_error(box, stub, model):
    api = stub({("POST", "/chat"): lambda body: (200, _CHAT_QUEUE_SHAPE)})
    model.reply.update(status=400, body={"error": {"message": "Unsupported MIME type"}})
    res = asyncio.run(_executor("analyze_image").execute({"image_url": "/ui/uploads/red.png"}, _ctx(api.url)))
    assert res.success is False
    assert "Unsupported MIME type" in res.result


@pytest.mark.parametrize("ref", ["/ui/uploads/../../.env", "/etc/passwd", "file:///etc/passwd"])
def test_analyze_image_refuses_anything_but_an_upload_or_public_url(box, stub, model, ref):
    api = stub({("POST", "/chat"): lambda body: (200, _CHAT_QUEUE_SHAPE)})
    res = asyncio.run(_executor("analyze_image").execute({"image_url": ref}, _ctx(api.url)))
    assert res.success is False
    assert model.calls == [] and api.requests == []


def test_analyze_image_fetches_a_public_url_through_the_ssrf_guard(box, stub, model, monkeypatch):
    from Orchestrator.utils import safe_fetch
    fetched = []

    def fake_safe_get(url, **kw):
        fetched.append(url)
        return safe_fetch.FetchResult(url=url, status_code=200,
                                      headers={"Content-Type": "image/png"}, content=PNG)

    monkeypatch.setattr(safe_fetch, "safe_get", fake_safe_get)
    api = stub({("POST", "/chat"): lambda body: (200, _CHAT_QUEUE_SHAPE)})
    res = asyncio.run(_executor("analyze_image").execute(
        {"image_url": "https://example.com/photo.png"}, _ctx(api.url)))
    assert res.success is True and res.result == "A small red square."
    assert fetched == ["https://example.com/photo.png"]
    assert "/chat" not in api.paths()


def test_analyze_image_without_a_google_key_says_so(box, stub, model, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    api = stub({("POST", "/chat"): lambda body: (200, _CHAT_QUEUE_SHAPE)})
    res = asyncio.run(_executor("analyze_image").execute({"image_url": "/ui/uploads/red.png"}, _ctx(api.url)))
    assert res.success is False
    assert "API key" in res.result
    assert model.calls == []


def test_the_analysis_tools_answer_within_the_mcp_gateways_proxy_timeout():
    """Over MCP (BLACKBOX_MCP_PROXY_TIMEOUT, 120 s by default) a longer wait is a
    gateway timeout: the model call is paid for and its answer thrown away."""
    from Orchestrator import media_analysis
    audio = _executor("analyze_audio")
    gateway = 120
    assert media_analysis.ANALYSIS_BUDGET_SECONDS + 2 < gateway
    assert audio.POLL_BUDGET_SECONDS + 1 + audio.POLL_INTERVAL_SECONDS < gateway
    assert audio.QUEUE_TIMEOUT_SECONDS < audio.POLL_BUDGET_SECONDS


def test_a_model_call_that_overruns_the_budget_is_a_clear_failure(box, stub, monkeypatch):
    """A socket timeout restarts on every byte, so the tool's own deadline is what
    bounds a server that trickles its answer."""
    import time as _time
    from Orchestrator import media_analysis
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setattr(media_analysis, "_MIN_MODEL_SECONDS", 0.0)
    release = threading.Event()

    def trickling_post(url, headers=None, json=None, timeout=None, **kw):
        release.wait(30)
        raise requests.ConnectionError("released")

    monkeypatch.setattr(requests, "post", trickling_post)

    async def run():
        started = _time.monotonic()
        try:
            await media_analysis.analyze_media("/ui/uploads/clip.mp4", "describe", "video", timeout=1.0)
        except media_analysis.MediaAnalysisError as e:
            return str(e), _time.monotonic() - started
        finally:
            release.set()
        raise AssertionError("no failure")

    message, elapsed = asyncio.run(run())
    assert "did not finish within 1s" in message and "shorter clip" in message
    assert elapsed < 1.0 + 2.0 + 1.0


def test_media_that_takes_the_whole_budget_to_load_skips_the_model_call(box, stub, model, monkeypatch):
    from Orchestrator import media_analysis
    with pytest.raises(media_analysis.MediaAnalysisError, match="did not finish within"):
        media_analysis.analyze_blocking("/ui/uploads/red.png", "describe", "image",
                                        timeout=media_analysis._MIN_MODEL_SECONDS / 2)
    assert model.calls == []


def test_analyze_video_returns_the_models_answer_and_queues_no_chat_turn(box, stub, model):
    model.reply["body"] = {"candidates": [{"content": {"parts": [{"text": "A cat jumps."}]}}]}
    api = stub({("POST", "/chat"): lambda body: (200, _CHAT_QUEUE_SHAPE)})
    res = asyncio.run(_executor("analyze_video").execute({"video_url": "/ui/uploads/clip.mp4"}, _ctx(api.url)))
    assert res.success is True and res.result == "A cat jumps."
    assert "/chat" not in api.paths()
    assert model.calls[0].json["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "video/mp4"


# ── analyze_audio: waits for its task ────────────────────────────────────────────

def _audio_api(stub, final):
    polls = {"n": 0}

    def task(body):
        polls["n"] += 1
        if polls["n"] < 3:
            return 200, {"task_id": "a1", "status": "processing"}
        return 200, final

    return stub({
        ("POST", "/analyze/audio"): lambda body: (200, {"task_id": "a1", "status": "pending",
                                                        "message": "Audio analysis queued"}),
        ("GET", "/tasks/a1"): task,
    })


def test_analyze_audio_returns_the_analysis(box, stub, monkeypatch):
    ex = _executor("analyze_audio")
    monkeypatch.setattr(ex, "POLL_INTERVAL_SECONDS", 0.01, raising=False)
    api = _audio_api(stub, {"task_id": "a1", "status": "completed",
                            "result_data": {"transcript": "Two people discuss the weather."}})
    res = asyncio.run(ex.execute({"file_path": "/ui/uploads/memo.wav"}, _ctx(api.url)))
    assert res.success is True
    assert res.result == "Two people discuss the weather."


def test_analyze_audio_reports_a_failed_task(box, stub, monkeypatch):
    ex = _executor("analyze_audio")
    monkeypatch.setattr(ex, "POLL_INTERVAL_SECONDS", 0.01, raising=False)
    api = _audio_api(stub, {"task_id": "a1", "status": "failed",
                            "error_message": "Gemini Files API returned 403"})
    res = asyncio.run(ex.execute({"file_path": "/ui/uploads/memo.wav"}, _ctx(api.url)))
    assert res.success is False
    assert "403" in res.result


def test_analyze_audio_treats_a_slow_status_check_as_still_running(box, stub, monkeypatch):
    import time as _time
    ex = _executor("analyze_audio")
    monkeypatch.setattr(ex, "POLL_INTERVAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(ex, "POLL_BUDGET_SECONDS", 1.5, raising=False)

    def slow_task(body):
        _time.sleep(2.5)                  # longer than the 1 s floor of a clipped check
        return 200, {"task_id": "a1", "status": "processing"}

    api = stub({
        ("POST", "/analyze/audio"): lambda body: (200, {"task_id": "a1", "status": "pending"}),
        ("GET", "/tasks/a1"): slow_task,
    })
    res = asyncio.run(ex.execute({"file_path": "/ui/uploads/memo.wav"}, _ctx(api.url)))
    assert res.success is True
    assert "still running" in res.result and res.data["task_id"] == "a1"


def test_analyze_audio_hands_back_the_task_when_it_runs_long(box, stub, monkeypatch):
    ex = _executor("analyze_audio")
    monkeypatch.setattr(ex, "POLL_INTERVAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(ex, "POLL_BUDGET_SECONDS", 0.05, raising=False)
    api = stub({
        ("POST", "/analyze/audio"): lambda body: (200, {"task_id": "a1", "status": "pending"}),
        ("GET", "/tasks/a1"): lambda body: (200, {"task_id": "a1", "status": "processing"}),
    })
    res = asyncio.run(ex.execute({"file_path": "/ui/uploads/memo.wav"}, _ctx(api.url)))
    assert res.success is True
    assert "still running" in res.result and res.data["task_id"] == "a1"


# ── Gmail ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def token_dir(tmp_path, monkeypatch):
    from Orchestrator.gmail import service
    d = tmp_path / "gmail_tokens"
    monkeypatch.setattr(service, "TOKEN_DIR", d)
    monkeypatch.setattr(service, "GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(service, "GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    return d


def test_a_token_the_api_transport_refreshes_on_a_401_is_saved(token_dir, monkeypatch):
    """The transport refreshes the credentials object itself when Google answers
    401; that new token used to live only in the one call's object."""
    from Orchestrator.gmail import service
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
    service.save_tokens("Alice", {"access_token": "still-valid", "refresh_token": "r1",
                                  "expiry": future.isoformat() + "Z"})
    refreshed_expiry = future + timedelta(minutes=30)

    def google_refresh(self, request):
        self.token = "rotated-by-401"
        self.expiry = refreshed_expiry

    monkeypatch.setattr(service.Credentials, "refresh", google_refresh)
    creds = service._get_credentials("Alice")
    assert creds.token == "still-valid"            # not due: no refresh here
    creds.refresh(object())                       # what the transport does on a 401
    saved = service.load_tokens("Alice")
    assert saved["access_token"] == "rotated-by-401"
    assert saved["expiry"] == refreshed_expiry.isoformat() + "Z"
    assert saved["refresh_token"] == "r1"


def _stub_gmail(monkeypatch, **fns):
    import Orchestrator.gmail.service as gmail
    for name, fn in fns.items():
        monkeypatch.setattr(gmail, name, fn)


def test_gmail_reply_never_sends_to_an_empty_address(monkeypatch):
    sent = []
    _stub_gmail(monkeypatch,
                get_message=lambda op, mid: {"id": mid, "from": "", "subject": "Hi"},
                send_email=lambda *a, **k: sent.append((a, k)) or {"success": True})
    res = asyncio.run(_executor("gmail_reply").execute(
        {"message_id": "m1", "thread_id": "t1", "body": "Thanks"}, ToolContext(operator="Alice")))
    assert res.success is False
    assert sent == []


def test_gmail_calls_run_off_the_event_loop(monkeypatch):
    threads = []

    def record(*a, **k):
        threads.append(threading.current_thread())
        return {"id": "m1", "from": "a@example.invalid", "subject": "s"}

    _stub_gmail(monkeypatch, get_message=record, send_email=record,
                list_messages=lambda *a, **k: threads.append(threading.current_thread()) or [])
    ctx = ToolContext(operator="Alice")
    asyncio.run(_executor("gmail_read").execute({"message_id": "m1"}, ctx))
    asyncio.run(_executor("gmail_reply").execute({"message_id": "m1", "thread_id": "t", "body": "b"}, ctx))
    asyncio.run(_executor("gmail_search").execute({"query": "x"}, ctx))
    asyncio.run(_executor("gmail_send").execute({"to": "a@example.invalid", "subject": "s", "body": "b"}, ctx))
    assert len(threads) == 5
    assert all(t is not threading.main_thread() for t in threads)


def test_gmail_labels_unknown_action_is_a_failure_not_a_no_op(monkeypatch):
    modified = []
    _stub_gmail(monkeypatch, modify_message=lambda *a, **k: modified.append(a) or {"success": True})
    res = asyncio.run(_executor("gmail_labels").execute(
        {"action": "delete_forever", "message_id": "m1"}, ToolContext(operator="Alice")))
    assert res.success is False
    assert modified == []


def test_gmail_send_logs_no_recipient_or_subject(monkeypatch, capsys):
    _stub_gmail(monkeypatch, send_email=lambda *a, **k: {"success": True, "messageId": "x"})
    asyncio.run(_executor("gmail_send").execute(
        {"to": "friend@example.invalid", "subject": "Private plans", "body": "b"},
        ToolContext(operator="Alice")))
    out = capsys.readouterr().out
    assert "friend@example.invalid" not in out
    assert "Private plans" not in out


# ── telephony ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("+15551234567", "+15551234567"),
    ("+1 (555) 123-4567", "+15551234567"),
    ("555-123-4567", "+15551234567"),
    ("5551234567", "+15551234567"),
    ("15551234567", "+15551234567"),
    ("0044 20 7946 0958", "+442079460958"),
    ("+44 20 7946 0958", "+442079460958"),
    ("12345", None),
    ("call me", None),
    ("+0123456789", None),
    ("0123456789", None),              # no North American number starts with 0
    ("", None),
    (None, None),
    ("+1234567890123456", None),       # 16 digits: longer than E.164 allows
    ("\uff15\uff15\uff15\uff11\uff12\uff13\uff14\uff15\uff16\uff17", None),  # fullwidth digits
    ("\u0665\u0665\u0665\u0661\u0662\u0663\u0664\u0665\u0666\u0667", None),  # Arabic-Indic digits
    ("+1\uff15\uff15\uff15\uff11\uff12\uff13\uff14\uff15\uff16\uff17", None),
])
def test_normalize_e164(raw, expected):
    from Orchestrator.telephony import normalize_e164
    assert normalize_e164(raw) == expected


def test_normalize_e164_uses_the_configured_country_for_national_numbers(monkeypatch):
    from Orchestrator.telephony import normalize_e164
    monkeypatch.setenv("TELEPHONY_DEFAULT_COUNTRY_CODE", "44")
    assert normalize_e164("020 7946 0958") == "+442079460958"
    assert normalize_e164("7946095800") == "+447946095800"


@pytest.mark.parametrize("provider, asterisk, cellular, path", [
    ("asterisk", True, False, "/asterisk/call"),
    ("auto", True, False, "/asterisk/call"),       # auto never chose Asterisk
    ("auto", True, True, "/asterisk/call"),
    ("cellular", False, True, "/twilio/call"),     # there is no /cellular/call route
    ("auto", False, True, "/twilio/call"),
    ("asterisk", False, False, "/twilio/call"),
    ("twilio", True, False, "/twilio/call"),
])
def test_outbound_call_path(monkeypatch, provider, asterisk, cellular, path):
    from Orchestrator.telephony import outbound_call_path
    monkeypatch.setattr(config, "TELEPHONY_PROVIDER", provider)
    monkeypatch.setattr(config, "ASTERISK_ENABLED", asterisk)
    monkeypatch.setattr(config, "CELLULAR_ENABLED", cellular)
    assert outbound_call_path() == path


def _telephony(monkeypatch, provider, asterisk=False, cellular=False):
    monkeypatch.setattr(config, "TELEPHONY_PROVIDER", provider)
    monkeypatch.setattr(config, "ASTERISK_ENABLED", asterisk)
    monkeypatch.setattr(config, "CELLULAR_ENABLED", cellular)


def test_make_voice_call_renders_no_tts_and_uses_the_boxs_gateway(stub, monkeypatch):
    _telephony(monkeypatch, "auto", asterisk=True, cellular=True)
    api = stub({
        ("POST", "/asterisk/call"): lambda body: (200, {"status": "initiated", "session_id": "ast-out-1"}),
        ("POST", "/cellular/call"): lambda body: (404, {"detail": "Not Found"}),
        ("POST", "/v1/audio/speech"): lambda body: (200, b"\x00" * 64),
    })
    monkeypatch.setattr(config, "OPENAI_TTS_URL", api.url + "/v1/audio/speech")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-openai-key")
    res = asyncio.run(_executor("make_voice_call").execute(
        {"phone_number": "555-010-0199", "message": "Your package arrived."}, _ctx(api.url)))
    assert "/v1/audio/speech" not in api.paths(), "a TTS render nobody hears"
    assert api.paths() == ["/asterisk/call"]
    body = api.requests[0][2]
    assert body["to"] == "+15550100199"
    assert body["greeting"] == "Your package arrived."
    assert "backend" not in body                  # the route picks the voice backend
    assert res.success is True
    assert res.data["call_sid"] == "ast-out-1"


def test_make_phone_call_never_dials_the_missing_cellular_route(stub, monkeypatch):
    _telephony(monkeypatch, "cellular", asterisk=False, cellular=True)
    api = stub({("POST", "/twilio/call"): lambda body: (200, {"error": "Twilio credentials not configured"})})
    res = asyncio.run(_executor("make_phone_call").execute(
        {"phone_number": "+15550100199", "greeting": "hi"}, _ctx(api.url)))
    assert api.paths() == ["/twilio/call"]
    assert res.success is False
    assert "Twilio credentials not configured" in res.result


def test_make_phone_call_refuses_a_number_it_cannot_dial(stub, monkeypatch):
    _telephony(monkeypatch, "twilio")
    api = stub({("POST", "/twilio/call"): lambda body: (200, {"status": "initiated", "call_sid": "CA1"})})
    res = asyncio.run(_executor("make_phone_call").execute({"phone_number": "12345"}, _ctx(api.url)))
    assert res.success is False
    assert api.requests == []


def test_make_phone_call_success_carries_the_call_id(stub, monkeypatch):
    _telephony(monkeypatch, "twilio")
    api = stub({("POST", "/twilio/call"): lambda body: (200, {"status": "initiated", "call_sid": "CA123"})})
    res = asyncio.run(_executor("make_phone_call").execute({"phone_number": "5550100199"}, _ctx(api.url)))
    assert res.success is True and res.data["call_sid"] == "CA123"
    assert api.requests[0][2]["to"] == "+15550100199"


def test_send_sms_with_auto_uses_the_tg200_router(monkeypatch):
    import Orchestrator.sms as sms
    _telephony(monkeypatch, "auto", asterisk=True)
    sent = []

    class Router:
        async def send_manual(self, **kw):
            sent.append(kw)
            return {"success": True}

    monkeypatch.setattr(sms, "get_router", lambda: Router())
    res = asyncio.run(_executor("send_sms").execute(
        {"phone_number": "(555) 010-0199", "message": "hello"}, ToolContext(operator="Alice")))
    assert res.success is True
    assert sent and sent[0]["to"] == "+15550100199"


def test_send_sms_refuses_an_invalid_number(monkeypatch):
    import Orchestrator.sms as sms
    _telephony(monkeypatch, "asterisk", asterisk=True)
    sent = []

    class Router:
        async def send_manual(self, **kw):
            sent.append(kw)
            return {"success": True}

    monkeypatch.setattr(sms, "get_router", lambda: Router())
    res = asyncio.run(_executor("send_sms").execute(
        {"phone_number": "not-a-number", "message": "hello"}, ToolContext(operator="Alice")))
    assert res.success is False
    assert sent == []


def test_a_phone_callback_the_route_refuses_is_not_reported_as_placed(stub, monkeypatch):
    from Orchestrator.phone.bridge import PhoneAIBridge
    _telephony(monkeypatch, "twilio")
    api = stub({("POST", "/twilio/call"): lambda body: (200, {"error": "Phone service is not enabled"})})
    monkeypatch.setattr(config, "orchestrator_url", lambda path="", **kw: api.url + path)
    bridge = PhoneAIBridge.__new__(PhoneAIBridge)
    result = asyncio.run(bridge._initiate_callback("+15550100199", "done", "Alice", "sess-1"))
    assert result is None
    assert api.paths() == ["/twilio/call"]
