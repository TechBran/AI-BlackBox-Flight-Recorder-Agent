"""Where the gate runs (spec §4.1; rules W19, W20, W34, A9, A10): the three
offering sites (_get_tools, which builds the tools array; build_streaming_context,
which builds the system prompt; the non-stream worker) across surface x
credential x provider x enrollment, the toolvault meta-tool's search / read /
list, the dispatcher's catch-all, and every surface that must never list or
run a wellness tool: MCP, /local/tools/*, live voice, telephony, SMS, cron, CU.
The kill switch and a consent change take effect on the next turn, with no
restart."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.tests.wellness_agent_support import (  # noqa: F401 - agent_box is a fixture
    LAN_ALIAS, LAN_MODEL, OTHER, WELLNESS_TOOLS, agent_box, custom_server, edit_record,
    make_turn, run_tool, set_enabled, stamped, stub_retrieval, tool_names)
from Orchestrator.tests.wellness_support import ENR, OPERATOR, roster
from Orchestrator.wellness import access, catalog, turn as turn_mod

PROMPT = "how did I sleep last night and how is my heart rate"
FORMAT = {"anthropic": "anthropic", "google": "gemini", "openai": "openai", "xai": "grok",
          "custom": "custom"}
SIGNED = access._Granted(OPERATOR, ENR)
NOT_SHARED = "not shared with this AI"


@pytest.fixture
def sites(agent_box, monkeypatch):
    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.tasks as tasks
    monkeypatch.setattr(cr, "TOOLVAULT_ENABLED", True)
    monkeypatch.setattr(tasks, "TOOLVAULT_ENABLED", True)
    stub_retrieval(monkeypatch)
    return agent_box


# ------------------------------------------------------------------ the three sites

def _site_get_tools(provider):
    import Orchestrator.routes.chat_routes as cr
    return tool_names(cr._get_tools(FORMAT[provider], PROMPT)), None


def _site_context(provider):
    import Orchestrator.routes.chat_routes as cr
    msgs, _prov, _tel = cr.build_streaming_context([{"role": "user", "content": PROMPT}],
                                                   OPERATOR, provider)
    system = msgs[0]["content"]
    offered = {n for n in WELLNESS_TOOLS if f"Tool: {n}\n" in system}
    return offered, system


def _run_worker(monkeypatch, inp: dict, provider: str, model: str = ""):
    """process_chat_task with the provider stubbed; the stub builds the tools
    array the way an adapter does (_get_tools), inside the worker's turn."""
    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso
    seen = {}

    def fake(name):
        def call(msgs, mdl, operator="?"):
            seen.update(system=msgs[0]["content"], model=mdl, operator=operator,
                        turn=turn_mod.current(),
                        tools=tool_names(cr._get_tools(FORMAT[provider], PROMPT)))
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            if name == "gemini":
                return "answer", usage, [], "", []
            return ("answer", usage) if name == "openai" else ("answer", usage, "", [])
        return call

    for name in ("anthropic", "gemini", "openai", "xai", "custom"):
        monkeypatch.setattr(cr, f"call_{name}", fake(name))
    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "perform_mint", lambda *a, **k: {"snap_id": "SNAP-TEST"})
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)
    task_id = f"wgs-{turn_mod.new_turn_key()}"
    body = {"messages": [{"role": "user", "content": PROMPT}], "operator": inp.pop("operator",
                                                                                  OPERATOR),
            "provider": provider, "model": model, **inp}
    task = Task(task_id=task_id, task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator=body["operator"],
                result_data=body)
    task_db.save_task(task)
    tasks.process_chat_task(task)
    assert task_db.get_task(task_id).status == TaskStatus.COMPLETED
    assert turn_mod.current() is None          # the worker leaves no turn behind
    return seen


def _offered_everywhere(monkeypatch, provider, *, turn=None, inp=None, model=None):
    """{site: (wellness tools offered, system prompt or None)}"""
    out = {}
    if turn is not None:
        with stamped(turn):
            out["get_tools"] = _site_get_tools(provider)
            out["context"] = _site_context(provider)
    else:
        out["get_tools"] = _site_get_tools(provider)
        out["context"] = _site_context(provider)
    if inp is not None:
        seen = _run_worker(monkeypatch, dict(inp), provider, model or "")
        out["worker"] = (seen["tools"] & set(WELLNESS_TOOLS), seen["system"])
    return out


def _req(provider="anthropic", model=None, **kw):
    return access.request_turn(operator=kw.pop("operator", OPERATOR), provider=provider,
                               model=model, **kw)


def _grant():
    return {access.GRANT_KEY: access.issue_grant(SIGNED, OPERATOR)}


MATRIX = [
    # id, provider, model, request_turn kwargs, worker input, offered, not-shared line
    ("signed-app-ticked-cloud", "anthropic", None, {"signed": SIGNED}, "grant", True, False),
    ("signed-app-unticked-cloud", "openai", None, {"signed": SIGNED}, "grant", False, True),
    ("signed-app-google-unpaid", "google", None, {"signed": SIGNED}, "grant", False, True),
    ("signed-app-lan-server", "custom", LAN_MODEL, {"signed": SIGNED}, "grant", True, False),
    ("unsigned-portal", "anthropic", None, {}, {}, False, False),
    ("unsigned-app", "anthropic", None, {"origin_device_id": "100.100.7.7"},
     {"origin_device_id": "100.100.7.7"}, False, False),
    ("sms", "anthropic", None, {"declared": "sms"}, {"sms_mode": True}, False, False),
    ("sms-peer", "anthropic", None, {"declared": "sms"}, {"sms_mode": True, "sms_peer": True},
     False, False),
    ("cron", "anthropic", None, {"declared": "cron"},
     {"wellness_surface": "cron", "cron_job_id": "job-1"}, False, False),
    ("mcp-chat", "anthropic", None, {"declared": "mcp"}, {"wellness_surface": "mcp"},
     False, False),
    ("not-enrolled", "anthropic", None, {"operator": OTHER}, {"operator": OTHER}, False, False),
]


@pytest.mark.parametrize("case", MATRIX, ids=[c[0] for c in MATRIX])
def test_the_three_offering_sites_agree(sites, monkeypatch, case):
    _id, provider, model, kw, inp, offered, line = case
    if provider == "custom":
        custom_server()
    if provider == "google":
        edit_record(providers_allowed=["anthropic", "google"])    # ticked, key not paid
    turn = _req(provider, model, **kw)
    inp = _grant() if inp == "grant" else dict(inp)
    if kw.get("operator") == OTHER:
        # OTHER is a live human with no enrollment; their own turn gets nothing.
        out = {}
        with stamped(turn):
            import Orchestrator.routes.chat_routes as cr
            out["get_tools"] = (tool_names(cr._get_tools(FORMAT[provider], PROMPT)), None)
            msgs = cr.build_streaming_context([{"role": "user", "content": PROMPT}], OTHER,
                                              provider)[0]
            out["context"] = ({n for n in WELLNESS_TOOLS if f"Tool: {n}\n" in msgs[0]["content"]},
                              msgs[0]["content"])
        seen = _run_worker(monkeypatch, inp, provider)
        out["worker"] = (seen["tools"] & set(WELLNESS_TOOLS), seen["system"])
    else:
        out = _offered_everywhere(monkeypatch, provider, turn=turn, inp=inp, model=model)
    for site, (names, system) in out.items():
        wellness = set(names) & set(WELLNESS_TOOLS)
        if offered:
            assert "wellness_summary" in wellness, (site, names)    # pinned (tier 1)
            if site != "get_tools":
                assert wellness == set(WELLNESS_TOOLS), (site, wellness)
        else:
            assert not wellness, (site, wellness)
        if system is not None:
            assert (catalog.WELLNESS_RULES in system) is offered, site
            assert (NOT_SHARED in system) is line, (site, system[-400:])


def test_household_mode_offers_an_unsigned_turn_but_never_a_declared_one(sites, monkeypatch):
    roster(monkeypatch, OPERATOR, "Flight Recorder")                 # one human
    out = _offered_everywhere(monkeypatch, "anthropic", turn=_req(), inp={})
    assert all("wellness_summary" in names for names, _ in out.values())
    for declared, inp in (("cron", {"wellness_surface": "cron"}),
                          ("sms", {"sms_mode": True}), ("mcp", {"wellness_surface": "mcp"})):
        out = _offered_everywhere(monkeypatch, "anthropic", turn=_req(declared=declared),
                                  inp=inp)
        assert not any(set(n) & set(WELLNESS_TOOLS) for n, _ in out.values()), declared


def test_a_public_custom_server_is_a_cloud_destination(sites, monkeypatch):
    custom_server(base_url="http://8.8.8.8:8080/v1")
    out = _offered_everywhere(monkeypatch, "custom", turn=_req("custom", LAN_MODEL,
                                                                signed=SIGNED),
                              inp=_grant(), model=LAN_MODEL)
    for site, (names, system) in out.items():
        assert not set(names) & set(WELLNESS_TOOLS), site
        if system is not None:
            assert NOT_SHARED in system and "switch to Anthropic" in system


def test_unstamped_and_mismatched_contexts_get_nothing(sites):
    import Orchestrator.routes.chat_routes as cr
    assert not tool_names(cr._get_tools("anthropic", PROMPT)) & set(WELLNESS_TOOLS)
    from Orchestrator.toolvault.context import ToolContext
    from Orchestrator.toolvault.injector import inject_for_prompt
    with stamped():
        assert access.offer(ToolContext(operator=OPERATOR, provider="anthropic")).ok
        for ctx in (ToolContext(operator=OTHER), ToolContext(operator=OPERATOR, provider="openai"),
                    ToolContext(operator=OPERATOR, surface="portal"),
                    ToolContext(operator=OPERATOR, caller="local"),
                    ToolContext(operator=OPERATOR, caller="mcp-gateway"), None):
            tools, text = inject_for_prompt(PROMPT, "anthropic", ctx=ctx)
            assert not tool_names(tools) & set(WELLNESS_TOOLS), ctx
            assert catalog.WELLNESS_RULES not in text
        # The default (no ctx) injection is the old "system" one: never these.
        tools, _ = inject_for_prompt(PROMPT, "anthropic")
        assert not tool_names(tools) & set(WELLNESS_TOOLS)


def test_kill_switch_and_consent_take_effect_on_the_next_turn(sites, monkeypatch):
    """W34 / A10: read fresh on every turn, no restart, no reload."""
    def offered():
        with stamped(_req(signed=SIGNED)):
            return "wellness_summary" in _site_get_tools("anthropic")[0]

    assert offered()
    set_enabled(False)
    assert not offered()
    set_enabled(True)
    assert offered()
    edit_record(categories={c: "off" for c in catalog.CATEGORIES})
    assert not offered()
    edit_record(categories={"sleep": "ask", "heart": "off", "activity": "off",
                            "blood_pressure": "off"})
    assert offered()
    from Orchestrator.toolvault.injector import inject_for_prompt
    with stamped(_req(signed=SIGNED)):
        assert access.offer(access.tool_context("anthropic")).categories == ("sleep",)
        tools, _ = inject_for_prompt(PROMPT, "anthropic", ctx=access.tool_context("anthropic"))
    by_name = {t["name"]: t for t in tools}
    # The x-source enums are this turn's own categories.
    assert by_name["wellness_series"]["input_schema"]["properties"]["metric"]["enum"] == \
        ["sleep_duration"]
    assert by_name["wellness_summary"]["input_schema"]["properties"]["categories"]["items"] == \
        {"type": "string", "enum": ["sleep"]}
    assert "x-source" not in json.dumps(tools)


# ------------------------------------------------------------------ discovery + execution

def _meta(action, **params):
    return run_tool("toolvault", {"action": action, **params})


def test_the_meta_tool_finds_reads_and_lists_only_what_the_turn_is_offered(sites):
    with stamped(_req(signed=SIGNED)):
        found = _meta("search", query="sleep heart rate health")
        read = _meta("read", tool_name="wellness_series")
        listed = _meta("list", category="wellness")
    assert {m["name"] for m in found.data["matches"]} >= {"wellness_summary", "wellness_series"}
    assert read.success and "wellness_metrics" not in read.result      # resolved, stripped
    assert listed.success and "wellness_history" in listed.result
    for turn in (_req(), _req("openai", signed=SIGNED), _req(declared="sms"), None):
        ctx_manager = stamped(turn) if turn is not None else _nullcontext()
        with ctx_manager:
            found = _meta("search", query="sleep heart rate health")
            read = _meta("read", tool_name="wellness_series")
            listed = _meta("list", category="wellness")
        assert not {m["name"] for m in (found.data or {}).get("matches", [])} & set(WELLNESS_TOOLS)
        assert not read.success and "not found in vault" in read.result
        assert "wellness_" not in listed.result


def _nullcontext():
    import contextlib
    return contextlib.nullcontext()


@pytest.mark.parametrize("turn_kw, code", [
    ({}, "surface_not_allowed"),
    ({"provider": "openai", "signed": SIGNED}, "provider_not_allowed"),
    ({"declared": "sms"}, "surface_not_allowed"),
    (None, "not_allowed"),
])
def test_the_catch_all_refuses_a_denied_tool_with_the_gates_code(sites, turn_kw, code):
    """A model that found a wellness tool by searching (or made the name up)
    gets the gate's code from the dispatcher, and the store is never read."""
    from Orchestrator.providers import tool_dispatch as td
    from Orchestrator.routes.chat.providers import _tool_context
    st = sites["store"]
    before = st.disclosures_count()
    ctx_manager = stamped(_req(**turn_kw)) if turn_kw is not None else _nullcontext()
    with ctx_manager:
        for loop in ("stream_anthropic", "call_openai", "stream_custom"):
            outcome = asyncio.run(td.dispatch_tool("wellness_summary", {"period": "today"},
                                                   _tool_context(OPERATOR, loop)))
            assert code in outcome.result and access.gate.FIXES[code] in outcome.result
            assert not any(e.get("type") == "wellness_used" for e in outcome.events)
            assert all("health data not available" in e["data"]
                       for e in outcome.events if e.get("type") == "tool_result")
    assert st.disclosures_count() == before


# ------------------------------------------------------------------ never-surfaces

def test_mcp_never_lists_them(sites):
    from Orchestrator.tools import tool_registry
    with stamped(_req(signed=SIGNED)):
        for group in ("mcp", "phone", "chat_cu", "realtime", "gemini_live", "grok_live", "chat"):
            # The static, group-derived arrays (MCP catalog, phone, voice,
            # fallbacks) carry no context, so never a wellness tool.
            assert not set(tool_registry.get_group_tool_names(group)) & set(WELLNESS_TOOLS)
    for name in WELLNESS_TOOLS:
        assert "mcp" not in tool_registry.get_tool_by_name(name)["groups"]


@pytest.mark.parametrize("caller", ["local", "mcp-gateway"])
def test_the_on_device_bridge_and_the_mcp_gateway_never_run_them(sites, caller):
    with stamped(_req(signed=SIGNED)):                       # even inside an allowed turn
        r = run_tool("wellness_summary", {"period": "today"}, caller=caller)
    assert not r.success and r.data["error"] == "surface_not_allowed"


def test_local_tool_routes_never_list_or_run_them(sites, monkeypatch):
    from fastapi.testclient import TestClient

    from Orchestrator.app import app
    with TestClient(app) as client:
        for headers in ({}, {"X-BlackBox-Caller": "mcp-gateway"}):
            r = client.post("/local/tools/execute", headers=headers,
                            json={"tool": "wellness_summary", "params": {"period": "today"},
                                  "operator": OPERATOR})
            assert r.status_code == 200 and r.json()["success"] is False
            assert "not_allowed" in r.json()["result"] or "surface_not_allowed" in \
                r.json()["result"]
        found = client.post("/local/tools/search", json={"query": "sleep heart rate", "k": 10})
        assert not {t["name"] for t in found.json()["tools"]} & set(WELLNESS_TOOLS)
        prep = client.post("/local/turn/prepare", json={"prompt": PROMPT, "operator": OPERATOR})
        assert prep.json()["success"]
        assert not {t["name"] for t in prep.json()["tools"]} & set(WELLNESS_TOOLS)
    assert sites["store"].disclosures_count() == 0


def _voice_session(ws_attr):
    s = MagicMock()
    setattr(s, ws_attr, MagicMock())
    getattr(s, ws_attr).send = AsyncMock()
    s.provenance = {}
    s.context_injected = False
    s.voice = ""
    s.resumption_handle = None
    return s


def _sent(session, ws_attr) -> str:
    return "\n".join(str(c.args[0]) for c in getattr(session, ws_attr).send.await_args_list)


@pytest.mark.parametrize("override", [None, "chat", "phone"])
def test_live_voice_and_telephony_configure_no_wellness_tool(sites, monkeypatch, override):
    """The three live-voice routes (and telephony, which configures them with
    the phone group) stamp no turn; even inside an allowed one, the session
    tools they configure hold no wellness tool."""
    from Orchestrator.routes import gemini_live_routes, grok_live_routes, realtime_routes

    def no_fossils(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    for mod in (realtime_routes, gemini_live_routes, grok_live_routes):
        monkeypatch.setattr(mod, "build_fossil_context", no_fossils)
    with stamped(_req(signed=SIGNED)):
        s1 = _voice_session("openai_ws")
        asyncio.run(realtime_routes.configure_openai_session(
            session=s1, operator=OPERATOR, voice="ash", tool_group_override=override))
        s2 = _voice_session("gemini_ws")
        asyncio.run(gemini_live_routes.configure_gemini_session(
            session=s2, operator=OPERATOR, voice="Charon", tool_group_override=override))
        s3 = _voice_session("grok_ws")
        asyncio.run(grok_live_routes.configure_grok_session(
            session=s3, operator=OPERATOR, voice="Ara", tool_group_override=override))
    for session, attr in ((s1, "openai_ws"), (s2, "gemini_ws"), (s3, "grok_ws")):
        sent = _sent(session, attr)
        assert "get_current_time" in sent, attr          # the session did get its tools
        assert not any(name in sent for name in WELLNESS_TOOLS), attr
        assert catalog.WELLNESS_RULES not in sent


def test_a_voice_turn_without_a_stamp_cannot_run_one(sites):
    r = run_tool("wellness_summary", {"period": "today"})
    assert not r.success and r.data["error"] == "not_allowed"


def test_computer_use_turns_are_cu_agents(sites):
    t = _req("computer-use", signed=SIGNED)
    assert t.surface == "cu_agent" and not t.credential_ok
    with stamped(t):
        import Orchestrator.routes.chat_routes as cr
        assert not tool_names(cr._get_tools("anthropic", PROMPT, group="chat_cu")) & \
            set(WELLNESS_TOOLS)
        assert not run_tool("wellness_summary", {"period": "today"}).success


def test_a_pooled_worker_thread_carries_no_turn_into_the_next_task(sites):
    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskStatus, TaskType
    from Orchestrator.volume import now_utc_iso
    turn_mod.stamp(make_turn())
    try:
        task = Task(task_id="wgs-media", task_type=TaskType.CHECKPOINT, status=TaskStatus.PENDING,
                    created_at=now_utc_iso(), updated_at=now_utc_iso(), operator=OPERATOR,
                    result_data={})
        seen = []
        orig = tasks.process_checkpoint_task
        tasks.process_checkpoint_task = lambda t: seen.append(turn_mod.current())
        try:
            tasks.process_task(task)
        finally:
            tasks.process_checkpoint_task = orig
        assert seen == [None]
    finally:
        turn_mod.clear()


def test_the_stream_route_stamps_the_turn_before_its_generator_runs(sites, monkeypatch):
    """/chat/stream stamps the WellnessTurn while resolving the request, after
    the media auto-route and the model resolution; the context build's thread
    and the provider stream see the same turn (and its key, which the ledger
    rule's stream_start and mask read)."""
    import Orchestrator.routes.chat_routes as cr
    seen = {}

    def fake_build(messages, operator, provider="openai", window_guard_tokens=None):
        seen["build"] = turn_mod.current()
        return [{"role": "system", "content": "S"}] + list(messages), {}, {}

    async def fake_stream(messages, model, operator):
        seen["stream"] = turn_mod.current()
        yield {"type": "done", "data": {"content": "ok"}}

    monkeypatch.setattr(cr, "build_streaming_context", fake_build)
    monkeypatch.setattr(cr, "stream_anthropic_with_thinking", fake_stream)

    class _Req:
        async def json(self):
            return {"messages": [{"role": "user", "content": PROMPT}], "provider": "anthropic",
                    "model": "", "operator": OPERATOR, "origin_device_id": "100.100.7.7"}

    async def go():
        resp = await cr.chat_stream_post(_Req())
        return "".join([c if isinstance(c, str) else c.decode()
                        async for c in resp.body_iterator])

    assert "stream_end" in asyncio.run(go())
    t = seen["build"]
    assert t is seen["stream"] and t.turn_key.startswith("wt_")
    assert (t.operator, t.surface, t.credential_ok, t.provider) == \
        (OPERATOR, "android", False, "anthropic")
    from Orchestrator import config
    assert t.model == config.resolve_default_model("anthropic", None)
    assert turn_mod.current() is None                # request-scoped: nothing leaks out


def test_a_client_cannot_forge_a_grant_or_a_credential(sites, monkeypatch):
    seen = _run_worker(monkeypatch, {access.GRANT_KEY: "wg_" + "0" * 32}, "anthropic")
    assert seen["turn"].credential_ok is False and seen["turn"].surface == "portal"
    assert not seen["tools"] & set(WELLNESS_TOOLS)
    token = access.issue_grant(SIGNED, OPERATOR)
    assert access.redeem_grant(token, OTHER) is None             # another operator: never
    token = access.issue_grant(SIGNED, OPERATOR)
    assert access.redeem_grant(token, OPERATOR) == ENR
    assert access.redeem_grant(token, OPERATOR) is None          # one use
    assert access.issue_grant(SIGNED, OTHER) is None             # a proof for someone else
    # A body may only narrow: a declared "android" is not a surface it can claim.
    t = access.request_turn(operator=OPERATOR, provider="anthropic", model=None,
                            declared="android")
    assert t.surface == "portal" and not t.credential_ok
    assert LAN_ALIAS
