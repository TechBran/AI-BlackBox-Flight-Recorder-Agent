"""W09: every surface that calls a provider reports a billing refusal as credits_exhausted.

The classifier itself is pinned in test_provider_billing.py. These tests pin the
wiring, one surface at a time, using the REAL Anthropic body journald recorded
(Jul-Oct 2026) and the documented OpenAI / Gemini / xAI shapes:

  * chat streams: each stream_<provider> error branch yields ONE error event
    whose data is the house sentence (still a plain string: Portal toasts it,
    Android appends it) with error_detail carrying the kind, and a 200 clears
    the provider's flag;
  * the SSE routes put an ``error_detail`` frame right before the ``error``
    frame (clients that don't know it ignore it);
  * the non-stream call_* raise CreditsExhaustedError (still an
    HTTPException with the provider's status), and the chat task worker
    records the kind on the task row;
  * Computer Use: the Anthropic driver (including the spend-cap 429, which is
    not retried), the Gemini and OpenAI loops, the headless fold, the CU task
    row, and preflight (reports the recorded failure without a provider call);
  * cron: the run record (history ``error`` column) starts with
    ``credits_exhausted:`` for chat jobs and for CU jobs;
  * CLI agents: a failed run whose output carries the billing text is
    classified under the CLI's own account, a successful one never is;
  * frontier device control and the OpenAI realtime STT error frame.

Hermetic: httpx/requests/aiohttp/SDK clients are fakes, the billing state
file is a tmp path and the push seam is a recorder.
"""
import asyncio
import json
import sys
import types

import pytest

from Orchestrator import provider_billing as pb


ANTHROPIC_CREDIT_400 = (
    '{"type":"error","error":{"type":"invalid_request_error","message":"Your credit '
    'balance is too low to access the Anthropic API. Please go to Plans & Billing to '
    'upgrade or purchase credits."},"request_id":"req_011CdMtESYTPKVGB7xBFWKw4"}'
)
ANTHROPIC_SPEND_CAP_429 = json.dumps({"type": "error", "error": {
    "type": "rate_limit_error",
    "message": "You have reached your API usage limits: your organization has crossed its "
               "monthly API usage threshold, set based on your organization's API tier. You "
               "will regain access on 2026-11-01 at 00:00 UTC.",
    "details": {"error_code": "enforced_spend_limit_reached"}}})
OPENAI_QUOTA_ERROR = {
    "message": "You exceeded your current quota, please check your plan and billing details. "
               "For more information on this error, read the docs: "
               "https://platform.openai.com/docs/guides/error-codes/api-errors.",
    "type": "insufficient_quota", "param": None, "code": "insufficient_quota"}
OPENAI_QUOTA_429 = json.dumps({"error": OPENAI_QUOTA_ERROR})
GEMINI_PREPAY_402 = json.dumps({"error": {
    "code": 402,
    "message": "Your prepayment credits are depleted. Please go to AI Studio at "
               "https://ai.studio/projects to manage your project and billing. Learn more "
               "at https://ai.google.dev/gemini-api/docs/billing#prepay.",
    "status": "RESOURCE_EXHAUSTED"}})
XAI_NO_CREDITS_403 = json.dumps({
    "code": "The caller does not have permission to execute the specified operation",
    "error": "Your newly created team doesn't have any credits yet. You can purchase credits "
             "on https://console.x.ai/team/00000000-0000-0000-0000-000000000000."})

MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    pushes = []

    def fake_push(title, body, dedup_key):
        pushes.append({"title": title, "body": body})
        return True

    monkeypatch.setattr(pb, "STATE_PATH", tmp_path / "provider_billing_state.json")
    monkeypatch.setattr(pb, "_push", fake_push)
    pb._reset_cache()
    yield pushes
    pb._reset_cache()


def _flag(provider):
    """Pre-set a provider's billing flag (as an earlier failure would)."""
    err = pb.classify(provider, 400, ANTHROPIC_CREDIT_400)
    pb.report(err, surface="earlier test")
    assert pb.last_failure(provider) is not None


# --------------------------------------------------------------------------- #
# Chat streams                                                                #
# --------------------------------------------------------------------------- #

class _FakeStreamResp:
    def __init__(self, status_code, lines=(), body=b""):
        self.status_code = status_code
        self._lines = list(lines)
        self._body = body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _fake_async_client(resp):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, **kw):
            return _FakeStreamCtx(resp)

    return _Client


async def _drain(agen):
    return [ev async for ev in agen]


@pytest.fixture
def cr(monkeypatch):
    import Orchestrator.routes.chat_routes as cr
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setattr(cr, name, "test-key-not-real", raising=False)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    return cr


STREAMS = [
    ("stream_anthropic_with_thinking", "anthropic", 400, ANTHROPIC_CREDIT_400),
    ("stream_openai_with_reasoning", "openai", 429, OPENAI_QUOTA_429),
    ("stream_xai_with_reasoning", "xai", 403, XAI_NO_CREDITS_403),
    ("stream_gemini_with_thinking", "google", 402, GEMINI_PREPAY_402),
]


@pytest.mark.parametrize("fn,provider,status,body", STREAMS)
def test_stream_billing_refusal_is_one_classified_error_event(cr, ledger, monkeypatch,
                                                              fn, provider, status, body):
    monkeypatch.setattr(cr.httpx, "AsyncClient",
                        _fake_async_client(_FakeStreamResp(status, body=body.encode())))
    events = asyncio.run(_drain(getattr(cr, fn)(MSGS, "test-model", "BillingTester")))

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1, events
    ev = errors[0]
    assert isinstance(ev["data"], str)
    assert ev["error_detail"]["kind"] == "credits_exhausted"
    assert ev["error_detail"]["provider"] == provider
    assert ev["data"].startswith(ev["error_detail"]["message"])
    assert "out of credits" in ev["data"]
    assert "{" not in ev["data"], "the raw provider body must not reach the user"
    assert len(ledger) == 1
    assert pb.last_failure(provider)["surface"] == "chat"


def test_stream_non_billing_error_keeps_its_old_text(cr, ledger, monkeypatch):
    body = b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
    monkeypatch.setattr(cr.httpx, "AsyncClient", _fake_async_client(_FakeStreamResp(529, body=body)))
    events = asyncio.run(_drain(cr.stream_anthropic_with_thinking(MSGS, "test-model", "BillingTester")))
    errors = [e for e in events if e["type"] == "error"]
    assert errors and errors[0]["data"].startswith("Anthropic API error:")
    assert "error_detail" not in errors[0]
    assert ledger == []


def _sse(events):
    return ["data: " + json.dumps(e) for e in events]


ANTHROPIC_END_TURN = _sse([
    {"type": "content_block_start", "content_block": {"type": "text"}},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}},
    {"type": "content_block_stop"},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
     "usage": {"input_tokens": 1, "output_tokens": 1}},
    {"type": "message_stop"},
])


def test_a_successful_stream_clears_the_flag(cr, ledger, monkeypatch):
    _flag("anthropic")
    monkeypatch.setattr(cr.httpx, "AsyncClient",
                        _fake_async_client(_FakeStreamResp(200, ANTHROPIC_END_TURN)))
    events = asyncio.run(_drain(cr.stream_anthropic_with_thinking(MSGS, "test-model", "BillingTester")))
    assert any(e["type"] == "content" for e in events)
    assert pb.last_failure("anthropic") is None


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def test_sse_routes_put_an_error_detail_frame_before_the_error_frame(cr, monkeypatch):
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)

    async def fake_stream(msgs, model, operator=None, **kw):
        yield err.event()

    monkeypatch.setattr(cr, "build_streaming_context",
                        lambda messages, operator, provider="openai", window_guard_tokens=None:
                        (list(messages), {"recent": []}, {}))
    monkeypatch.setattr(cr, "stream_anthropic_with_thinking", fake_stream)

    async def go_post():
        resp = await cr.chat_stream_post(_Req({"messages": MSGS, "provider": "anthropic",
                                               "model": "test-model", "operator": "BillingTester"}))
        return "".join([c if isinstance(c, str) else c.decode()
                        async for c in resp.body_iterator])

    class _GetReq:
        query_params = {"messages": json.dumps(MSGS), "provider": "anthropic",
                        "model": "test-model", "operator": "BillingTester"}

    async def go_get():
        resp = await cr.chat_stream(_GetReq())
        return "".join([c if isinstance(c, str) else c.decode()
                        async for c in resp.body_iterator])

    for text in (asyncio.run(go_post()), asyncio.run(go_get())):
        frames = [f for f in text.split("\n\n") if f.startswith("event:")]
        names = [f.split("\n", 1)[0][len("event: "):] for f in frames]
        assert "error_detail" in names and "error" in names
        assert names.index("error_detail") == names.index("error") - 1
        detail = json.loads(frames[names.index("error_detail")].split("data: ", 1)[1])
        assert detail == err.detail_payload()
        data = json.loads(frames[names.index("error")].split("data: ", 1)[1])
        assert data == err.text      # still a plain JSON string for every client


# --------------------------------------------------------------------------- #
# Non-stream call_* and the chat task worker                                  #
# --------------------------------------------------------------------------- #

class _JsonResp:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)
        self.reason = "OK" if status_code == 200 else "Error"

    def json(self):
        if isinstance(self._payload, (dict, list)):
            return self._payload
        return json.loads(self.text)


CALLS = [
    ("call_anthropic", "anthropic", 400, ANTHROPIC_CREDIT_400),
    ("call_openai", "openai", 429, OPENAI_QUOTA_429),
    ("call_xai", "xai", 403, XAI_NO_CREDITS_403),
    ("call_gemini", "google", 402, GEMINI_PREPAY_402),
]


@pytest.mark.parametrize("fn,provider,status,body", CALLS)
def test_call_paths_raise_credits_exhausted(cr, ledger, monkeypatch, fn, provider, status, body):
    from fastapi import HTTPException

    monkeypatch.setattr(cr.requests, "post",
                        lambda *a, **k: _JsonResp(None, status_code=status, text=body))
    with pytest.raises(pb.CreditsExhaustedError) as info:
        getattr(cr, fn)(MSGS, "test-model", operator="BillingTester")
    exc = info.value
    assert isinstance(exc, HTTPException) and exc.status_code == status
    assert exc.billing.provider == provider and exc.billing.kind == "credits_exhausted"
    assert exc.detail == exc.billing.text
    assert len(ledger) == 1


def test_call_non_billing_error_is_unchanged(cr, ledger, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(cr.requests, "post",
                        lambda *a, **k: _JsonResp(None, status_code=500, text="upstream boom"))
    with pytest.raises(HTTPException) as info:
        cr.call_anthropic(MSGS, "test-model", operator="BillingTester")
    assert not isinstance(info.value, pb.CreditsExhaustedError)
    assert info.value.detail == "upstream boom"
    assert ledger == []


def test_a_successful_call_clears_the_flag(cr, ledger, monkeypatch):
    _flag("anthropic")
    ok = {"content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn",
          "usage": {"input_tokens": 1, "output_tokens": 1}}
    monkeypatch.setattr(cr.requests, "post", lambda *a, **k: _JsonResp(ok))
    out = cr.call_anthropic(MSGS, "test-model", operator="BillingTester")
    assert out[0] == "hello"
    assert pb.last_failure("anthropic") is None


def test_chat_task_row_records_the_kind(ledger, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskDatabase, TaskStatus, TaskType
    from Orchestrator.routes import chat_routes
    from Orchestrator.volume import now_utc_iso

    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)

    def refusing_call_anthropic(messages, model, operator="x"):
        raise pb.CreditsExhaustedError(err)

    db = TaskDatabase(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(tasks, "task_db", db)
    monkeypatch.setattr(chat_routes, "call_anthropic", refusing_call_anthropic)
    for name in ("get_recent_fossils_for_operator", "keyword_retrieve_for_operator",
                 "semantic_retrieve", "get_recent_checkpoints_for_operator", "hybrid_retrieve"):
        monkeypatch.setattr(tasks, name, lambda *a, **k: [])
    monkeypatch.setattr(tasks, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)

    task = Task(task_id="t-bill", task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator="BillingTester",
                result_data={"messages": MSGS, "operator": "BillingTester",
                             "provider": "anthropic", "model": "claude-test"})
    db.save_task(task)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(tasks.process_chat_task, task).result()
    row = db.get_task("t-bill")

    assert row.status == "failed"
    assert row.error_message == err.text
    assert row.result_data["error_kind"] == "credits_exhausted"
    assert row.result_data["error_provider"] == "anthropic"


# --------------------------------------------------------------------------- #
# Computer Use                                                                #
# --------------------------------------------------------------------------- #

class _ScriptedHTTPX:
    """httpx stand-in for the Anthropic CU driver (which imports httpx per call)."""

    class TimeoutException(Exception):
        pass

    class ConnectError(Exception):
        pass

    def __init__(self, status, body, calls):
        self._status, self._body, self._calls = status, body, calls

    def AsyncClient(self, **kwargs):
        status, body, calls = self._status, self._body, self._calls

        class _Resp:
            status_code = status
            headers = {}

            async def aiter_lines(self):
                if False:
                    yield ""

            async def aread(self):
                return body.encode()

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *exc):
                return False

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url, headers=None, json=None):
                calls.append(url)
                return _Ctx()

        return _Client()


@pytest.fixture
def cu_session(monkeypatch):
    from Orchestrator.browser.session_manager import ComputerUseSession
    from Orchestrator.routes import chat_routes

    monkeypatch.setattr(ComputerUseSession, "capture_screenshot_bytes",
                        lambda self: b"\x89PNG-fake" * 30)
    monkeypatch.setattr("Orchestrator.browser.screenshot.save_screenshot_to_uploads",
                        lambda png, ident, step: "/ui/uploads/x.png")

    async def _no_save(*a, **k):
        return None
    monkeypatch.setattr(chat_routes, "_cu_save_to_blackbox", _no_save)

    slept = []

    async def _no_sleep(secs):
        slept.append(secs)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    session = ComputerUseSession("billing-cu-op")
    session._slept = slept
    return session


def _drain_queue(session):
    events = []
    while not session.event_queue.empty():
        ev = session.event_queue.get_nowait()
        if ev is not None:
            events.append(ev)
    return events


@pytest.mark.parametrize("status,body,reason", [
    (400, ANTHROPIC_CREDIT_400, "no_credits"),
    (429, ANTHROPIC_SPEND_CAP_429, "spend_limit"),
])
def test_anthropic_cu_driver_reports_and_stops(cu_session, ledger, monkeypatch, status, body, reason):
    calls = []
    monkeypatch.setitem(sys.modules, "httpx", _ScriptedHTTPX(status, body, calls))
    from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop

    history = [{"role": "user", "content": [{"type": "text", "text": "click A"}]}]
    asyncio.run(run_anthropic_cu_loop(cu_session, history, "sys", [], {},
                                      "claude-sonnet-5", "billing-cu-op", "click A"))

    assert len(calls) == 1, "a billing refusal is not retried"
    assert cu_session._slept == [], "no retry-after wait on a spend cap"
    errors = [e for e in _drain_queue(cu_session) if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["error_detail"]["reason"] == reason
    assert cu_session.status == "error"
    assert cu_session.error_message == errors[0]["data"]
    assert pb.last_failure("anthropic")["surface"] == "computer use"
    assert len(ledger) == 1


def test_headless_fold_carries_the_kind_to_the_task_result():
    from Orchestrator.browser.headless import _drain_and_fold

    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)

    async def go():
        session = types.SimpleNamespace(event_queue=asyncio.Queue(), current_step=1)
        await session.event_queue.put(err.event())
        await session.event_queue.put(None)

        async def _done():
            return None
        task = asyncio.ensure_future(_done())
        return await _drain_and_fold(session, task, [])

    result = asyncio.run(go())
    assert result["success"] is False
    assert result["result_text"] == err.text
    assert result["error_kind"] == "credits_exhausted"
    assert result["error_provider"] == "anthropic"


def test_headless_fold_without_billing_keeps_the_golden_shape():
    from Orchestrator.browser.headless import _drain_and_fold

    async def go():
        session = types.SimpleNamespace(event_queue=asyncio.Queue(), current_step=1)
        await session.event_queue.put({"type": "error", "data": "API error 500: boom"})
        await session.event_queue.put(None)

        async def _done():
            return None
        return await _drain_and_fold(session, asyncio.ensure_future(_done()), [])

    result = asyncio.run(go())
    assert set(result) == {"success", "result_text", "screenshots", "final_screenshot",
                           "steps", "tokens"}


def test_cu_task_row_records_the_kind(monkeypatch):
    import Orchestrator.tasks as tasks
    from Orchestrator.browser import headless
    from Orchestrator.models import TaskStatus, TaskType

    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    updates = []

    async def fake_run_cu_task(**kw):
        return {"success": False, "result_text": err.text, "screenshots": [],
                "final_screenshot": None, "steps": 1, "tokens": {"input": 0, "output": 0},
                **err.record_fields()}

    monkeypatch.setattr(headless, "run_cu_task", fake_run_cu_task)
    monkeypatch.setattr(tasks, "update_task", lambda tid, **kw: updates.append(kw))
    monkeypatch.setattr(tasks, "register_cancel_handle", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "is_cancel_requested", lambda tid: False)
    monkeypatch.setattr(tasks, "_mint_cu_failure_snapshot", lambda *a, **k: None)
    task = types.SimpleNamespace(task_id="cu-bill", prompt="do it", operator="BillingTester",
                                 task_type=TaskType.USE_COMPUTER, status=TaskStatus.PROCESSING,
                                 result_data={})
    tasks.process_browser_use(task)

    failed = [u for u in updates if u.get("status") == TaskStatus.FAILED]
    assert failed and failed[0]["error_message"] == err.text
    assert failed[0]["result_data"]["error_kind"] == "credits_exhausted"


class _GenaiClientError(Exception):
    def __init__(self, code, response_json):
        super().__init__(f"{code} {response_json['error']['status']}. {response_json!r}")
        self.code = code
        self.status = response_json["error"]["status"]
        self.details = response_json


def test_gemini_cu_loop_reports_a_prepay_refusal(ledger, monkeypatch):
    from Orchestrator.gemini_cu import agent_loop as G

    class _Models:
        async def generate_content(self, **kw):
            raise _GenaiClientError(402, json.loads(GEMINI_PREPAY_402))

    class _Client:
        def __init__(self, api_key=None, **kw):
            self.aio = types.SimpleNamespace(models=_Models())

    async def _shot(session):
        return b"\x89PNG-fake"

    monkeypatch.setattr(G.genai, "Client", _Client)
    monkeypatch.setattr(G, "retrieve_for_agent", lambda **kw: ("", {"recent": []}))
    monkeypatch.setattr(G, "resolve_operator", lambda op, tag: op)
    monkeypatch.setattr(G, "_capture_screenshot", _shot)
    monkeypatch.setattr(G, "_save_screenshot", lambda png, session: "/ui/uploads/g.png")
    session = types.SimpleNamespace(
        status="idle", current_step=0, operator="BillingTester", environment="desktop",
        conversation_history=[], stop_requested=False, ensure_display=lambda start_url=None: True)

    events = asyncio.run(_drain(G.run_gemini_cu_loop(session, "click A", "gemini-cu-test")))
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["data"]["message"].startswith("Google Gemini account is out of credits")
    assert errors[0]["error_detail"]["kind"] == "credits_exhausted"
    assert session.status == "error"
    assert pb.last_failure("google") is not None and len(ledger) == 1


def test_openai_cu_loop_reports_insufficient_quota(ledger, monkeypatch):
    from Orchestrator.openai_cu import agent_loop as O
    from Orchestrator.tests.test_openai_cu import FakeSession

    class _StatusError(Exception):
        def __init__(self):
            super().__init__(f"Error code: 429 - {{'error': {OPENAI_QUOTA_ERROR!r}}}")
            self.status_code = 429
            self.body = {"error": OPENAI_QUOTA_ERROR}

    class _Raising:
        def __init__(self, api_key=None, **kw):
            self.responses = self

        async def create(self, **kwargs):
            raise _StatusError()

    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(O, "OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(O, "capture_screenshot", lambda: b"\x89PNGfake", raising=False)
    monkeypatch.setattr(O, "resize_screenshot", lambda png, w, h: png)
    monkeypatch.setattr(O, "save_screenshot_to_uploads", lambda png, tid, step: "/ui/uploads/o.png")
    monkeypatch.setattr(O.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(O, "AsyncOpenAI", _Raising)

    session = FakeSession()
    events = asyncio.run(_drain(O.run_openai_cu_loop(session, "task")))
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["data"]["message"].startswith("OpenAI account is out of credits")
    assert errors[0]["error_detail"]["provider"] == "openai"
    assert session.status == "error"
    assert len(ledger) == 1


def test_preflight_reports_the_recorded_failure_without_a_provider_call(ledger, monkeypatch):
    import httpx
    import requests

    from Orchestrator.browser import preflight

    def no_network(*a, **k):
        raise AssertionError("preflight must not call a provider")

    monkeypatch.setattr(requests, "post", no_network)
    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    monkeypatch.setattr(httpx, "Client", no_network)
    monkeypatch.setattr(preflight, "ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setattr(preflight, "GOOGLE_API_KEY", "g-key")
    monkeypatch.setattr(preflight, "OPENAI_API_KEY", "")

    assert preflight.check_billing()["status"] == "ok"

    _flag("anthropic")
    c = preflight.check_billing()
    assert c["id"] == "billing" and c["status"] == "warn"      # Gemini CU still works
    assert c["kind"] == "credits_exhausted" and c["providers"] == ["anthropic"]
    assert "Anthropic account is out of credits" in c["detail"]
    assert c["remediation"]

    g = pb.classify("google", 402, GEMINI_PREPAY_402)
    pb.report(g, surface="computer use")
    assert preflight.check_billing()["status"] == "fail"       # every keyed backend refused

    pb.note_ok("anthropic")
    pb.note_ok("google")
    assert preflight.check_billing()["status"] == "ok"
    ids = {x["id"] for x in preflight.run_preflight(skip_screenshot=True)["checks"]}
    assert "billing" in ids


# --------------------------------------------------------------------------- #
# Cron run records                                                            #
# --------------------------------------------------------------------------- #

class _AioResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


def _failed_task_session(err):
    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, timeout=None):
            return _AioResp({"task_id": "task-bill"})

        def get(self, url, timeout=None):
            return _AioResp({"status": "failed", "error_message": err.text,
                             "result_data": {"error": err.text, **err.record_fields()}})

    return _Session


def _job(**over):
    job = {"id": "cron_bill", "name": "Morning Brief", "prompt": "brief me",
           "operator": "BillingTester", "delivery": "snapshot", "delivery_target": "",
           "model": "claude-test", "provider": "anthropic"}
    job.update(over)
    return job


def test_cron_chat_job_failure_is_tagged_credits_exhausted(monkeypatch):
    from Orchestrator.scheduler import executor as executor_mod

    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    monkeypatch.setattr(executor_mod, "_POLL_INTERVAL_SECS", 0)
    monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _failed_task_session(err))

    with pytest.raises(RuntimeError) as info:
        asyncio.run(executor_mod.execute_cron_job(_job()))
    assert str(info.value).startswith("credits_exhausted: ")
    assert err.message in str(info.value)
    assert getattr(info.value, "error_kind", None) == "credits_exhausted"


class _SSEContent:
    def __init__(self, lines):
        self._lines = [(ln + "\n").encode() for ln in lines]

    def __aiter__(self):
        async def gen():
            for ln in self._lines:
                yield ln
        return gen()


def test_cron_cu_job_failure_is_tagged_credits_exhausted(monkeypatch):
    from Orchestrator.scheduler import executor as executor_mod

    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    lines = ["event: stream_start", "data: {}", "",
             "event: error_detail", "data: " + json.dumps(err.detail_payload()), "",
             "event: error", "data: " + json.dumps(err.text), ""]

    class _StreamResp(_AioResp):
        def __init__(self):
            super().__init__({})
            self.content = _SSEContent(lines)

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, timeout=None):
            return _StreamResp()

    monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _Session)
    with pytest.raises(RuntimeError) as info:
        asyncio.run(executor_mod.execute_cron_job(_job(provider="computer-use", model="")))
    assert str(info.value).startswith("credits_exhausted: ")
    assert getattr(info.value, "error_kind", None) == "credits_exhausted"


def test_cron_history_row_carries_the_kind(monkeypatch, tmp_path):
    from Orchestrator.scheduler import executor as executor_mod
    from Orchestrator.scheduler import manager as manager_mod
    from Orchestrator.scheduler.manager import CronJobManager

    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron_bill.db")
    monkeypatch.setattr(manager_mod, "RETRY_BACKOFF_SECONDS", [0] * (manager_mod.MAX_RETRIES + 1))
    alerts = []

    async def rec(*a, **k):
        alerts.append(k)
    monkeypatch.setattr(manager_mod, "notify", rec)
    monkeypatch.setattr(executor_mod, "_POLL_INTERVAL_SECS", 0)
    monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _failed_task_session(err))

    mgr = CronJobManager()
    job = mgr.create_job(name="Morning Brief", prompt="brief me", schedule="0 9 * * *",
                         operator="BillingTester", model="claude-test", provider="anthropic")
    asyncio.run(mgr._execute_job(job["id"]))

    history = mgr.get_job_history(job["id"])
    assert history and all(r["error"].startswith("credits_exhausted: ") for r in history)
    assert all(r["delivery_status"] == "error" for r in history)
    cron_alerts = [a for a in alerts if a.get("category") == "alert"]
    assert cron_alerts and cron_alerts[0]["body"].startswith("credits_exhausted: ")


# --------------------------------------------------------------------------- #
# CLI agents, frontier device control, realtime STT                           #
# --------------------------------------------------------------------------- #

@pytest.fixture
def cli_db(monkeypatch, tmp_path):
    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskStatus, TaskType
    from Orchestrator.volume import now_utc_iso

    store = {}

    class FakeDB:
        def get_task(self, task_id):
            return store.get(task_id)

        def save_task(self, task):
            store[task.task_id] = task

        def get_all_tasks(self, operator=None):
            return list(store.values())

    monkeypatch.setattr(tasks, "task_db", FakeDB())
    monkeypatch.setenv("HOME", str(tmp_path))

    def seed(task_id, provider):
        now = now_utc_iso()
        store[task_id] = Task(task_id=task_id, task_type=TaskType.CLI_AGENT,
                              status=TaskStatus.PROCESSING, created_at=now, updated_at=now,
                              operator="BillingTester", prompt="refactor x",
                              result_data={"provider": provider})
        return store[task_id]
    return store, seed


def test_cli_agent_billing_failure_is_classified_under_its_own_login(ledger, cli_db, monkeypatch):
    import requests

    import Orchestrator.tasks as tasks
    from Orchestrator.cli_agent import headless

    store, seed = cli_db
    task = seed("cli-bill", "claude")
    tail = "Starting...\nCredit balance is too low"
    monkeypatch.setattr(headless, "run_cli_agent", lambda **kw: {
        "success": False, "exit_code": 1, "cancelled": False, "timed_out": False,
        "result_text": tail, "tail": tail, "events": 0, "provider": "claude"})
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("a failed run never mints"))

    tasks.process_cli_agent(task)

    row = store["cli-bill"]
    assert row.status == "failed"
    assert "Anthropic account is out of credits" in row.error_message
    assert row.result_data["error_kind"] == "credits_exhausted"
    assert len(ledger) == 1 and "CLI agent" in ledger[0]["body"]
    assert pb.last_failure("anthropic") is None, "the CLI login is not the box's API key"


def test_cli_agent_success_mentioning_the_phrase_is_not_classified(ledger, cli_db, monkeypatch):
    import requests

    import Orchestrator.tasks as tasks
    from Orchestrator.cli_agent import headless

    store, seed = cli_db
    task = seed("cli-ok", "claude")
    out = "Added a check for 'credit balance is too low' to the classifier."
    monkeypatch.setattr(headless, "run_cli_agent", lambda **kw: {
        "success": True, "exit_code": 0, "cancelled": False, "timed_out": False,
        "result_text": out, "tail": out, "events": 1, "provider": "claude"})
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: types.SimpleNamespace(raise_for_status=lambda: None))

    tasks.process_cli_agent(task)

    assert store["cli-ok"].status == "completed"
    assert "error_kind" not in store["cli-ok"].result_data
    assert ledger == []


def test_frontier_loop_ends_with_credits_exhausted(ledger, monkeypatch):
    from Orchestrator import frontier_agent_loop as fal

    obs = {"msg": "observation", "ui_tree": [], "device_capability": {
        "formFactor": "phone", "hasScreenshot": True, "supportsCoordinateGesture": True,
        "displayId": 0}, "timestamp": 1}

    class _AnthropicSDKError(Exception):
        def __init__(self):
            super().__init__("Error code: 400 - " + repr(json.loads(ANTHROPIC_CREDIT_400)))
            self.status_code = 400
            self.body = json.loads(ANTHROPIC_CREDIT_400)

    class _Driver:
        async def next_action(self, observation, last_result):
            raise _AnthropicSDKError()

    async def fake_pull(base_url, task_id, operator, timeout, headers=None):
        return obs

    for name, val in (("_retry_max", 0), ("_retry_backoff_secs", 0.0), ("_per_action_secs", 5.0),
                      ("_per_turn_secs", 5.0), ("_session_base_secs", 300.0),
                      ("_session_max_secs", 600.0), ("_max_steps", 5)):
        monkeypatch.setattr(fal, name, lambda _v=val: _v)
    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: _Driver())

    res = asyncio.run(fal.run_frontier_loop("http://phone:8765", "open the app",
                                            "BillingTester", provider="anthropic"))
    assert res.success is False
    assert res.error_kind == "credits_exhausted"
    assert res.message.startswith("Anthropic account is out of credits")
    assert len(ledger) == 1


def test_realtime_stt_error_frame_names_the_billing_failure(ledger):
    from Orchestrator.routes.stt_ws_routes import _openai_stt_error

    event = {"type": "error", "event_id": "event_E6potORfbqHyglrT9h37k",
             "error": {**OPENAI_QUOTA_ERROR, "event_id": None}}
    frame = _openai_stt_error(event)
    assert frame["type"] == "stt_error"
    assert frame["message"].startswith("OpenAI account is out of credits")
    assert frame["error_kind"] == "credits_exhausted"
    assert len(ledger) == 1

    other = _openai_stt_error({"type": "error", "error": {"message": "bad session config"}})
    assert other == {"type": "stt_error", "message": "OpenAI: bad session config"}
