"""The gate decides on the AI that will read the answer, never on how a client
spelled it (spec §4.1, §4.6; W19).

/chat/stream runs every provider string it does not know by name on Gemini
("local", "claude", "custom:<alias>", "Anthropic", ...), so the WellnessTurn
it stamps names Gemini too (stream.dispatched_provider is the one rule both
read). The non-stream worker stamps no provider for a string it cannot run.
And every chat loop's tool context names the loop's own provider, so the
dispatcher, the executor and the toolvault meta-tool refuse a wellness tool to
a loop the turn was not stamped for, whatever the stamp says."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from Orchestrator.tests.test_wellness_chat_e2e import (  # noqa: F401 - enrolled_phone is a fixture
    QUESTION, SIM, _events, enrolled_phone)
from Orchestrator.tests.wellness_agent_support import (  # noqa: F401 - agent_box is a fixture
    FX, LAN_ALIAS, WELLNESS_TOOLS, agent_box, edit_record, make_turn, sentinel_hits, stamped,
    stub_retrieval)
from Orchestrator.tests.wellness_support import ENR, OPERATOR, roster
from Orchestrator.wellness import access, catalog, enroll, turn as turn_mod

SIGNED = access._Granted(OPERATOR, ENR)
# Strings the gate would read as an allowed destination (the phone, a ticked
# cloud, a LAN server) but /chat/stream runs on Gemini.
SPOOFS = ("local", "LOCAL", "claude", "Anthropic", f"custom:{LAN_ALIAS}", "openai_rest", "grok")


# ------------------------------------------------------------------ the stamp

def test_dispatched_provider_is_the_rule_the_stream_switch_runs(monkeypatch):
    """One rule: for every string, the stream _open_provider_stream opens is
    the one dispatched_provider names."""
    from Orchestrator.routes.chat import stream
    runs = {"stream_openai_with_reasoning": "openai",
            "stream_anthropic_with_thinking": "anthropic",
            "stream_xai_with_reasoning": "xai", "stream_custom_with_reasoning": "custom",
            "stream_gemini_with_thinking": "google", "stream_computer_use": "computer-use",
            "stream_gemini_computer_use": "computer-use",
            "stream_openai_computer_use": "computer-use"}
    for name, provider in runs.items():
        monkeypatch.setattr(stream, name, lambda *a, _p=provider, **k: _p)
    for spelled in ("anthropic", "openai", "xai", "custom", "computer-use", "google", "gemini",
                    "", "Google") + SPOOFS:
        req = stream.StreamRequest(messages=[], provider=spelled, model="m", operator=OPERATOR,
                                   guard=None, cu_session_id=None, cu_device_id="blackbox",
                                   location=None)
        assert stream._open_provider_stream(req, []) == stream.dispatched_provider(spelled), \
            spelled
    assert {stream.dispatched_provider(s) for s in SPOOFS} == {"google"}


@pytest.mark.parametrize("spelled", SPOOFS)
def test_the_stream_turn_names_the_provider_that_runs(agent_box, spelled):
    from Orchestrator import config
    from Orchestrator.routes.chat import stream
    body = {"messages": [{"role": "user", "content": QUESTION}], "provider": spelled,
            "operator": OPERATOR}
    try:
        req = stream._resolve_stream_request(body, SIGNED)
        t = turn_mod.current()
    finally:
        turn_mod.clear()
    assert t.provider == "google" and t.credential_ok
    assert t.model == req.model == config.resolve_default_model(spelled, None)
    assert req.provider == spelled                      # stream_start still echoes the client


@pytest.mark.parametrize("honest", ["anthropic", "openai", "xai"])
def test_an_honest_provider_is_stamped_as_it_runs(agent_box, honest):
    from Orchestrator.routes.chat import stream
    body = {"messages": [{"role": "user", "content": QUESTION}], "provider": honest,
            "operator": OPERATOR}
    try:
        stream._resolve_stream_request(body, SIGNED)
        t = turn_mod.current()
    finally:
        turn_mod.clear()
    assert t.provider == honest


# ------------------------------------------------------------------ the real route

class FakeGemini:
    """streamGenerateContent that calls wellness_summary whether or not it was
    declared (a model told about it, or guessing), then answers."""

    def __init__(self, answer: str = "ok"):
        self.requests = []
        self.answer = answer

    def client_class(self):
        fake = self

        class _Resp:
            status_code = 200

            def __init__(self, chunks):
                self._chunks = chunks

            async def aiter_lines(self):
                for c in self._chunks:
                    yield "data: " + json.dumps(c)

            async def aread(self):
                return b""

        class _Stream:
            def __init__(self, resp):
                self.resp = resp

            async def __aenter__(self):
                return self.resp

            async def __aexit__(self, *exc):
                return False

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url, headers=None, json=None, **kw):
                fake.requests.append({"url": url, "payload": copy.deepcopy(json)})
                answered = any("functionResponse" in p for c in json["contents"]
                               for p in c.get("parts", []))
                part = ({"text": fake.answer} if answered else
                        {"functionCall": {"name": "wellness_summary",
                                          "args": {"period": "last_30_days"}}})
                return _Stream(_Resp([{"candidates": [{"content": {"parts": [part]}}]}]))

        return _Client

    def declared(self) -> set:
        first = self.requests[0]["payload"]
        return {fd["name"] for t in first.get("tools") or []
                for fd in (t.get("functionDeclarations") or t.get("function_declarations") or [])}

    def system(self) -> str:
        return json.dumps(self.requests[0]["payload"].get("system_instruction"))

    def function_response(self) -> str:
        last = self.requests[-1]["payload"]
        return json.dumps([p["functionResponse"] for c in last["contents"]
                           for p in c.get("parts", []) if "functionResponse" in p])


def _gemini_turn(box, monkeypatch, provider, *, signed=True, answer="ok"):
    import Orchestrator.routes.chat_routes as cr
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "test-google-key")
    gem = FakeGemini(answer)
    monkeypatch.setattr(httpx, "AsyncClient", gem.client_class())
    raw = json.dumps({"messages": [{"role": "user", "content": QUESTION}], "provider": provider,
                      "operator": OPERATOR}).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        st = box["state"].load()
        headers["X-BlackBox-Origin"] = SIM.origin_header(st["key"], st["enrollment_id"], "POST",
                                                         "/chat/stream", raw)
    r = box["client"].post("/chat/stream", content=raw, headers=headers)
    assert r.status_code == 200
    assert gem.requests and all(":streamGenerateContent" in q["url"] for q in gem.requests)
    return gem, _events(r.text)


def _rows():
    return enroll.store_for(enroll.get_enrollment(OPERATOR), create=False).disclosures()


def _assert_nothing_reached_gemini(gem, events):
    assert not gem.declared() & set(WELLNESS_TOOLS)
    system = gem.system()
    assert catalog.WELLNESS_RULES[:60] not in system
    assert not any(f"Tool: {n}" in system for n in WELLNESS_TOOLS)
    response = gem.function_response()
    assert "provider_not_allowed" in response, response
    assert "sleeping_hr_median" not in response and "bpm" not in response
    assert not [d for e, d in events if e == "wellness_used"]
    assert _rows() == []


@pytest.mark.parametrize("spelled", ["local", "LOCAL", "claude", "Anthropic",
                                     f"custom:{LAN_ALIAS}"])
def test_a_spoofed_provider_string_gets_gemini_nothing(enrolled_phone, monkeypatch, spelled):
    """Google is not ticked; the strings below read as the phone, Anthropic
    (ticked) or the LAN server, and all run on Gemini."""
    edit_record(providers_allowed=["anthropic"])
    gem, events = _gemini_turn(enrolled_phone, monkeypatch, spelled)
    _assert_nothing_reached_gemini(gem, events)


def test_household_mode_does_not_open_gemini_either(enrolled_phone, monkeypatch):
    """One human on the box: an unsigned turn is enough for the gate, and the
    same spoof still reaches nothing."""
    roster(monkeypatch, OPERATOR, "Flight Recorder")
    gem, events = _gemini_turn(enrolled_phone, monkeypatch, "local", signed=False)
    _assert_nothing_reached_gemini(gem, events)


def test_gemini_reads_health_data_when_google_is_really_allowed(enrolled_phone, monkeypatch):
    """The control: the same fake, Google ticked with a paid key."""
    edit_record(providers_allowed=["anthropic", "google"], google_paid_key_confirmed=True)
    gem, events = _gemini_turn(enrolled_phone, monkeypatch, "google")
    assert "wellness_summary" in gem.declared()
    assert catalog.WELLNESS_RULES[:60] in gem.system()
    assert "sleeping_hr_median" in gem.function_response()
    [row] = _rows()
    assert (row["provider"], row["tool"]) == ("google", "wellness_summary")
    [frame] = [d for e, d in events if e == "wellness_used"]
    assert frame["provider"] == "google" and frame["ref"] == row["ref"]


def test_the_gemini_loop_never_logs_the_answer_to_a_health_tool(enrolled_phone, monkeypatch,
                                                                 capsys):
    """After a tool call the Gemini stream logs each part it reads; the answer
    to a health tool quotes values, so the log carries the text's length only."""
    edit_record(providers_allowed=["google"], google_paid_key_confirmed=True)
    answer = f"You took {FX.SENTINEL_STRINGS[2]} steps that day."
    gem, events = _gemini_turn(enrolled_phone, monkeypatch, "google", answer=answer)
    assert answer in json.dumps([d for e, d in events if e == "content"])
    assert len(_rows()) == 1
    out = capsys.readouterr()
    printed = out.out + out.err
    assert "[GEMINI-STREAM] Iter 1 part:" in printed
    assert not sentinel_hits(printed), sentinel_hits(printed)


# ------------------------------------------------------------------ the loop's own provider

def test_every_chat_loop_names_its_provider():
    from Orchestrator.providers.tool_dispatch import PROFILES
    from Orchestrator.wellness.destinations import CLOUD_PROVIDERS
    for loop, profile in PROFILES.items():
        assert profile.provider in CLOUD_PROVIDERS + ("custom",), loop


@pytest.mark.parametrize("loop", ["stream_gemini", "call_gemini", "stream_openai", "call_xai",
                                  "stream_custom", "call_custom"])
def test_a_loop_the_turn_was_not_stamped_for_cannot_find_read_or_run_one(agent_box, loop):
    """The stamp says Anthropic (ticked); the loop handing over the call is
    another provider's. Nothing is found, read or run, and the store is
    never touched."""
    from Orchestrator.providers import tool_dispatch as td
    from Orchestrator.routes.chat.providers import _tool_context
    before = agent_box["store"].disclosures_count()
    with stamped(make_turn(provider="anthropic")):
        ctx = _tool_context(OPERATOR, loop)
        ran = asyncio.run(td.dispatch_tool("wellness_summary", {"period": "last_7_days"}, ctx))
        read = asyncio.run(td.dispatch_tool("toolvault", {"action": "read",
                                                          "tool_name": "wellness_summary"}, ctx))
        found = asyncio.run(td.dispatch_tool("toolvault", {"action": "search",
                                                           "query": "sleep heart rate"}, ctx))
    assert "not_allowed" in str(ran.result) and "bpm" not in str(ran.result)
    assert not any(e.get("type") == "wellness_used" for e in ran.events)
    assert "not found in vault" in str(read.result)
    assert not any(n in str(found.result) for n in WELLNESS_TOOLS)
    assert agent_box["store"].disclosures_count() == before


@pytest.mark.parametrize("loop", ["stream_anthropic", "call_anthropic"])
def test_the_loop_the_turn_was_stamped_for_runs_it(agent_box, loop):
    from Orchestrator.providers import tool_dispatch as td
    from Orchestrator.routes.chat.providers import _tool_context
    with stamped(make_turn(provider="anthropic")):
        ctx = _tool_context(OPERATOR, loop)
        ran = asyncio.run(td.dispatch_tool("wellness_summary", {"period": "last_7_days"}, ctx))
        read = asyncio.run(td.dispatch_tool("toolvault", {"action": "read",
                                                          "tool_name": "wellness_summary"}, ctx))
    assert "wellness_summary (ref" in str(ran.result)
    assert "wellness_summary" in str(read.result) and "not found" not in str(read.result)
    assert agent_box["store"].disclosures_count() == 1


def test_an_executor_that_names_no_provider_runs_nothing(agent_box):
    """A ToolContext must name the provider that will read the answer."""
    from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor
    from Orchestrator.toolvault.context import ToolContext
    with stamped(make_turn(provider="anthropic")):
        assert not access.offer(ToolContext(operator=OPERATOR)).ok
        r = asyncio.run(BlackBoxToolExecutor(operator=OPERATOR).execute(
            "wellness_summary", {"period": "last_7_days"}))
    assert not r.success and r.data["error"] == "not_allowed"


# ------------------------------------------------------------------ the non-stream worker

@pytest.mark.parametrize("spelled", ["local", "claude", f"custom:{LAN_ALIAS}"])
def test_the_worker_stamps_no_provider_it_cannot_run(agent_box, monkeypatch, spelled):
    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.toolvault import injector
    from Orchestrator.volume import now_utc_iso
    monkeypatch.setattr(cr, "TOOLVAULT_ENABLED", True)
    monkeypatch.setattr(tasks, "TOOLVAULT_ENABLED", True)
    stub_retrieval(monkeypatch)
    seen = {}
    real = injector.inject_for_prompt

    def spy(*a, **k):
        tools, text = real(*a, **k)
        seen.update(turn=turn_mod.current(), text=text)
        return tools, text

    monkeypatch.setattr(injector, "inject_for_prompt", spy)
    task_id = f"wpr-{turn_mod.new_turn_key()}"
    body = {"messages": [{"role": "user", "content": QUESTION}], "operator": OPERATOR,
            "provider": spelled, "model": "", access.GRANT_KEY: access.issue_grant(SIGNED,
                                                                                   OPERATOR)}
    task = Task(task_id=task_id, task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator=OPERATOR,
                result_data=body)
    task_db.save_task(task)
    tasks.process_chat_task(task)
    assert task_db.get_task(task_id).status == TaskStatus.FAILED       # unknown provider
    assert seen["turn"].provider is None and seen["turn"].credential_ok
    assert catalog.WELLNESS_RULES not in seen["text"]
    assert agent_box["store"].disclosures_count() == 0
    assert turn_mod.current() is None
