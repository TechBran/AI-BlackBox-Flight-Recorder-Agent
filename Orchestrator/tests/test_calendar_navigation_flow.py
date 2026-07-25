"""M4 — calendar address -> navigation push, on a schedule (Orchestrator half).

THE TARGET FLOW: at 07:30 a cron job runs a normal /chat turn; the model reads today's
first calendar event, takes its free-text ``location``, and pushes it to the phone as a
tappable notification.  Three things had to be true for that to work, and each of them
is pinned here:

  1. THE MODEL IS TOLD THE ADDRESS EXISTS.  ``list_events`` returns Google's raw event
     dicts, which DO carry ``location`` — but the schema never mentioned it, so "find the
     job address" depended on the model noticing an undocumented key inside a JSON blob.
     Now the description, ``returns`` and ``notes`` all name it, say it is FREE TEXT, and
     say it goes to ``navigate_device`` verbatim with no geocoding step.
  2. ORIGIN ROUTING IS ALIVE ON ``POST /chat``.  ``_set_origin_device_id`` was called only
     from the two /chat/stream routes, so on the non-stream path (the one CRON USES) the
     contextvar every ``call_*`` tool catch-all reads was structurally dead.  A supplied
     origin is now honoured; an ABSENT one still falls back to the operator's PRIMARY
     device — which is correct, not a bug: cron is not a request from a phone.
  3. THE SCHEDULED CASE IS UNMISSABLE.  A locked, pocketed phone cannot be made to launch
     Maps (targetSdk-36 discards the background launch silently), so a scheduled navigate
     MUST be ``delivery='notify'``.  The schema says what to do about an unattended run;
     the cron envelope says that THIS run is one.

Hermetic: no tailscale, no sockets, no Google, no phone.
"""
import asyncio
import importlib.util
import inspect
import json
from pathlib import Path

import pytest

from Orchestrator.local_provider.mesh import Node
from Orchestrator.toolvault.context import ToolContext
from Orchestrator.toolvault.schema_spec import validate_module_dict

_TOOLS = Path(__file__).resolve().parents[2] / "ToolVault" / "tools"


def _load(tool_name):
    spec = importlib.util.spec_from_file_location(
        f"{tool_name}_executor_m4", _TOOLS / tool_name / "executor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _schema(tool_name):
    return json.loads((_TOOLS / tool_name / "schema.json").read_text())


def _run(coro):
    return asyncio.run(coro)


LIST_EVENTS = _load("list_events")
NAV = _load("navigate_device")
LE_SCHEMA = _schema("list_events")
NAV_SCHEMA = _schema("navigate_device")

# Documented Google example coordinates / an obviously-fake job address — never a real one
# (the plan's guardrail: the ledger is gitignored, test files are not).
JOB_ADDRESS = "1247 Main St W, Hamilton, ON"

NODE = Node(hostname="brandon-fold6", dns_name="brandon-fold6.tailnet-abc.ts.net",
            ip="100.88.0.7", online=True, os="android")

# What the device answers for a notification-delivered navigate (M3 wire contract).
NOTIFY_OK = {"msg": "action_result", "success": True,
             "detail": "queued navigation notification"}


# ═══════════════════════════════════════════════════════════════════════════════════════
# 1. The schema advertises `location`
# ═══════════════════════════════════════════════════════════════════════════════════════

def test_list_events_schema_still_passes_the_ci_validator():
    assert validate_module_dict(LE_SCHEMA, "list_events",
                                known_sources={"operators"}) == []


def test_list_events_description_names_location_as_a_free_text_address():
    """The whole point of M4.1: a model reading only the schema must learn that an
    address is in the result, and what shape it is."""
    desc = LE_SCHEMA["description"].lower()
    assert "location" in desc
    assert "free-text" in desc or "free text" in desc
    assert "address" in desc


def test_list_events_returns_documents_location_and_forbids_geocoding():
    """`returns` is what the model consults to interpret the blob it got back. It must say
    location is there AND that it is passed to navigate_device untouched — the free-text
    hand-off is the reason no geocoder exists anywhere in this flow."""
    returns = LE_SCHEMA["returns"]
    low = returns.lower()
    assert "location" in low
    assert "navigate_device" in low
    # never convert an address to coordinates first
    assert "coordinates" in low
    assert "never" in low or "not" in low


def test_list_events_notes_spell_out_the_address_hand_off():
    notes = LE_SCHEMA["notes"].lower()
    assert "location" in notes and "navigate_device" in notes
    # and that a missing location is a normal state, not an error to retry
    joined = (LE_SCHEMA["description"] + LE_SCHEMA["returns"] + LE_SCHEMA["notes"]).lower()
    assert "absent" in joined or "empty" in joined or "missing" in joined


def test_ordering_guarantee_is_stated_so_first_event_means_first_event():
    """'today's FIRST job' is only well-defined because the executor asks Google for
    singleEvents + orderBy=startTime. The model has to be told that index 0 is real."""
    notes = LE_SCHEMA["notes"].lower()
    assert "orderby='starttime'" in notes or "orderby=\"starttime\"" in notes
    assert "chronological" in notes or "first event" in notes


@pytest.mark.parametrize("tool", ["create_event", "update_event"])
def test_write_side_calendar_tools_describe_location_the_same_way(tool):
    """Consistency check across the sibling calendar tools: the field the model WRITES is
    the field it later READS, so both sides must describe free text, not coordinates."""
    schema = _schema(tool)
    assert validate_module_dict(schema, tool, known_sources={"operators"}) == []
    desc = schema["parameters"]["properties"]["location"]["description"].lower()
    assert "free text" in desc or "free-text" in desc
    assert "address" in desc
    assert "navigate_device" in desc
    assert "coordinates" in desc          # explicitly says NOT coordinates


def test_delete_and_list_calendars_have_no_location_to_document():
    """The consistency sweep is bounded: these two carry no place field, so silence there
    is correct rather than an omission."""
    for tool in ("delete_event", "list_calendars"):
        props = _schema(tool)["parameters"]["properties"]
        assert "location" not in props


# ═══════════════════════════════════════════════════════════════════════════════════════
# 1b. The executor keeps every field the model needs
# ═══════════════════════════════════════════════════════════════════════════════════════

RAW_EVENT = {
    "kind": "calendar#event",
    "etag": "\"3412345678901234\"",
    "id": "e1",
    "status": "confirmed",
    "htmlLink": "https://calendar.google.com/event?eid=e1",
    "created": "2026-07-20T12:00:00.000Z",
    "updated": "2026-07-21T12:00:00.000Z",
    "summary": "Panel install",
    "description": "Bring the 12ft ladder",
    "location": JOB_ADDRESS,
    "creator": {"email": "op@example.com", "self": True},
    "organizer": {"email": "op@example.com", "self": True},
    "start": {"dateTime": "2026-07-25T08:00:00-04:00"},
    "end": {"dateTime": "2026-07-25T11:00:00-04:00"},
    "iCalUID": "abc123def456@google.com",
    "sequence": 0,
    "attendees": [{"email": "client@example.com", "responseStatus": "accepted"}],
    "reminders": {"useDefault": True},
    "eventType": "default",
    "hangoutLink": "https://meet.google.com/xxx-yyyy-zzz",
}


def _fake_calendar_env(monkeypatch, items):
    """Patch the two lazily-imported collaborators of the list_events executor."""
    import Orchestrator.gmail.service as svc
    import Orchestrator.google_workspace.calendar as cal
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    monkeypatch.setattr(cal, "list_events", lambda *a, **k: items)


def test_location_survives_the_executor_verbatim(monkeypatch):
    """The load-bearing assertion of the whole milestone: the address the user typed into
    Google reaches the model byte-for-byte, ready to hand to navigate_device."""
    _fake_calendar_env(monkeypatch, [RAW_EVENT])
    res = _run(LIST_EVENTS.execute({"time_min": "2026-07-25T00:00:00Z",
                                    "time_max": "2026-07-26T00:00:00Z"},
                                   ToolContext(operator="Brandon")))
    assert res.success is True
    events = json.loads(res.result)
    assert events[0]["location"] == JOB_ADDRESS


def test_every_human_meaningful_field_is_preserved(monkeypatch):
    """The slim projection is a DENY-list of four protocol keys, never a whitelist — so a
    future question about any real field ('who is coming?', 'what's the meet link?',
    'when was it added?') can never have been broken by it."""
    _fake_calendar_env(monkeypatch, [RAW_EVENT])
    res = _run(LIST_EVENTS.execute({"time_min": "a", "time_max": "b"},
                                   ToolContext(operator="Brandon")))
    got = json.loads(res.result)[0]
    for key, value in RAW_EVENT.items():
        if key in LIST_EVENTS._NOISE_KEYS:
            continue
        assert got[key] == value, f"{key} was altered or dropped"


def test_only_the_four_inert_protocol_keys_are_dropped(monkeypatch):
    _fake_calendar_env(monkeypatch, [RAW_EVENT])
    res = _run(LIST_EVENTS.execute({"time_min": "a", "time_max": "b"},
                                   ToolContext(operator="Brandon")))
    got = json.loads(res.result)[0]
    assert set(RAW_EVENT) - set(got) == {"kind", "etag", "iCalUID", "sequence"}


def test_an_event_with_no_location_is_returned_not_rejected(monkeypatch):
    """Many meetings have no place. That must read as 'no address known', never an error —
    the model has to be able to say so instead of inventing one."""
    bare = {"id": "e2", "summary": "1:1", "start": {"dateTime": "2026-07-25T09:00:00Z"}}
    _fake_calendar_env(monkeypatch, [bare])
    res = _run(LIST_EVENTS.execute({"time_min": "a", "time_max": "b"},
                                   ToolContext(operator="Brandon")))
    assert res.success is True
    assert "location" not in json.loads(res.result)[0]


def test_an_error_dict_is_passed_through_untrimmed(monkeypatch):
    _fake_calendar_env(monkeypatch, {"error": "Calendar: reconnect Google Workspace"})
    res = _run(LIST_EVENTS.execute({"time_min": "a", "time_max": "b"},
                                   ToolContext(operator="Brandon")))
    assert res.success is False
    assert json.loads(res.result) == {"error": "Calendar: reconnect Google Workspace"}


# ═══════════════════════════════════════════════════════════════════════════════════════
# 2. origin_device_id on the non-stream POST /chat path
# ═══════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean_origin():
    """Leave the contextvar exactly as found — these tests set it deliberately."""
    from Orchestrator.routes import chat_routes
    token = chat_routes._ORIGIN_DEVICE_ID.set(None)
    yield
    chat_routes._ORIGIN_DEVICE_ID.reset(token)


def test_chat_in_accepts_an_origin_device_id():
    """The non-stream twin of the field both /chat/stream routes already take."""
    from Orchestrator.startup import ChatIn
    inp = ChatIn(messages=[{"role": "user", "content": "hi"}],
                 origin_device_id="brandon-fold6")
    assert inp.origin_device_id == "brandon-fold6"
    # and stays optional — cron/Portal/MCP send no such field
    assert ChatIn(messages=[{"role": "user", "content": "hi"}]).origin_device_id is None


def test_supplied_origin_is_honoured_on_the_non_stream_path():
    from Orchestrator.routes import chat_routes
    from Orchestrator.startup import ChatIn
    from Orchestrator.tasks import stamp_origin_device

    stamp_origin_device(ChatIn(messages=[{"role": "user", "content": "go"}],
                               origin_device_id="brandon-fold6"))
    assert chat_routes._ORIGIN_DEVICE_ID.get() == "brandon-fold6"


def test_absent_origin_leaves_the_primary_device_fallback():
    """Cron legitimately has NO origin — it is not a request from a phone. None is the
    correct value: mesh.resolve_device then falls through to the operator's PRIMARY."""
    from Orchestrator.routes import chat_routes
    from Orchestrator.startup import ChatIn
    from Orchestrator.tasks import stamp_origin_device

    stamp_origin_device(ChatIn(messages=[{"role": "user", "content": "briefing"}]))
    assert chat_routes._ORIGIN_DEVICE_ID.get() is None


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_origin_normalizes_to_the_fallback_not_an_empty_target(blank):
    from Orchestrator.routes import chat_routes
    from Orchestrator.startup import ChatIn
    from Orchestrator.tasks import stamp_origin_device

    stamp_origin_device(ChatIn(messages=[{"role": "user", "content": "x"}],
                               origin_device_id=blank))
    assert chat_routes._ORIGIN_DEVICE_ID.get() is None


def test_a_previous_tasks_origin_cannot_leak_into_the_next_one():
    """Chat tasks run on REUSED ThreadPoolExecutor workers and pool.submit does not hand
    out a fresh context, so the stamp must be unconditional. If it were only applied when
    present, a cron briefing scheduled after a phone turn would inherit that phone and
    fire someone else's device."""
    from Orchestrator.routes import chat_routes
    from Orchestrator.startup import ChatIn
    from Orchestrator.tasks import stamp_origin_device

    stamp_origin_device(ChatIn(messages=[{"role": "user", "content": "phone turn"}],
                               origin_device_id="brandon-fold6"))
    assert chat_routes._ORIGIN_DEVICE_ID.get() == "brandon-fold6"
    stamp_origin_device(ChatIn(messages=[{"role": "user", "content": "cron turn"}]))
    assert chat_routes._ORIGIN_DEVICE_ID.get() is None


def test_process_chat_task_stamps_before_it_dispatches_to_a_provider():
    """Order matters: the call_* tool loops read the contextvar when they build their
    executor, so the stamp is worthless if it lands after the dispatch."""
    from Orchestrator import tasks
    src = inspect.getsource(tasks.process_chat_task)
    assert "stamp_origin_device(inp)" in src
    assert src.index("stamp_origin_device(inp)") < src.index("call_anthropic")


def test_the_queueing_route_does_not_pretend_to_stamp():
    """POST /chat only ENQUEUES; the turn runs later on a worker thread in a different
    context. A _set_origin_device_id call in the route would look right and do nothing —
    the exact trap this milestone existed to close."""
    from Orchestrator.routes import chat_routes
    src = inspect.getsource(chat_routes.chat_async)
    assert "_set_origin_device_id" not in src.replace(
        "# M4 origin-aware device routing: do NOT call _set_origin_device_id here.", "")


_NON_STREAM_LOOPS = ["call_openai", "call_anthropic", "call_gemini", "call_xai", "call_custom"]


@pytest.mark.parametrize("fn_name", _NON_STREAM_LOOPS)
def test_non_stream_loops_read_the_now_live_contextvar(fn_name):
    """These already threaded the origin; until M4 nothing on this path ever set it."""
    from Orchestrator.routes import chat_routes
    src = inspect.getsource(getattr(chat_routes, fn_name))
    assert "origin_device_id=_ORIGIN_DEVICE_ID.get()" in src


# ═══════════════════════════════════════════════════════════════════════════════════════
# 3 + 4. Flow: a scheduled navigate resolves to notify delivery
# ═══════════════════════════════════════════════════════════════════════════════════════

def _resolves_to(node, seen=None):
    def _f(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return node
    return _f


def _capture_post(calls, response=NOTIFY_OK):
    async def _f(base_url, frame):
        calls.append({"base_url": base_url, "frame": frame})
        return response
    return _f


def test_cron_envelope_tells_the_model_the_run_is_unattended():
    """The schema can say what to do ABOUT an unattended run; only the cron layer knows
    that THIS run is one. Without it, 'unattended' is an inference from a job name."""
    from Orchestrator.scheduler.executor import _build_prompt
    built = _build_prompt("Morning job route", "check my calendar and send me the address",
                          "snapshot", "")
    low = built.lower()
    assert built.startswith("[Scheduled Task: Morning job route]")   # unchanged prefix
    assert "unattended" in low
    assert "locked" in low
    assert "notification" in low and "tap" in low
    assert "check my calendar and send me the address" in built


@pytest.mark.parametrize("delivery,target", [("snapshot", ""), ("sms", "+15551234567"),
                                             ("voice_call", "+15551234567")])
def test_the_unattended_notice_is_on_every_delivery_mode(delivery, target):
    """It describes the RUN, not the delivery channel, so no branch may skip it."""
    from Orchestrator.scheduler.executor import _build_prompt
    assert "UNATTENDED" in _build_prompt("J", "body", delivery, target)


def test_the_notice_is_situational_not_coupled_to_one_tool():
    """Naming navigate_device here would make the generic cron path depend on one tool's
    vocabulary; the tool's own schema is where the parameter is taught."""
    from Orchestrator.scheduler.executor import _UNATTENDED_NOTICE
    assert "navigate_device" not in _UNATTENDED_NOTICE
    assert "delivery=" not in _UNATTENDED_NOTICE


def test_navigate_schema_teaches_the_model_to_recognize_a_scheduled_run():
    """The other half of the pair: having been told the run is unattended, the model must
    find the rule in the tool it is about to call. It lives on the `delivery` PARAMETER
    (and notes) — see the selection-neutrality test below for why not the description."""
    delivery = NAV_SCHEMA["parameters"]["properties"]["delivery"]["description"].lower()
    assert "scheduled task" in delivery      # the exact marker the cron envelope emits
    assert "unattended" in delivery
    assert "notify" in delivery
    assert "background" in delivery          # WHY: Android discards the background launch
    assert "notes" in NAV_SCHEMA and "delivery='notify'" in NAV_SCHEMA["notes"]


def test_the_scheduled_rule_stays_out_of_the_tool_selection_embedding():
    """MEASURED REGRESSION, caught before it shipped. Only `description` is embedded and
    keyword-searched for tool SELECTION (toolvault/embeddings.py + injector); parameters,
    example and notes are rendered into AVAILABLE TOOLS but never scored. Putting the cron
    vocabulary ('[Scheduled Task: ...]', 'unattended scheduled run') into the description
    made navigate_device score 0.773 on a cron SMS prompt — ABOVE send_sms at 0.769 — i.e.
    it would have been injected into every scheduled run of any topic and started
    displacing the tool the job actually needed. Moving the same words onto the `delivery`
    parameter put selection back to its shipped 0.639 with the guidance fully intact.

    So: the description keeps exactly the one shipped sentence it always had, and the
    literal cron header — the token that binds to EVERY scheduled prompt — never enters
    the embedded text."""
    desc = NAV_SCHEMA["description"].lower()
    assert "[scheduled task" not in desc
    # the expanded recognition rule lives on the parameter, and only there
    rule = "how to tell which one you need"
    assert rule not in desc
    assert rule in NAV_SCHEMA["parameters"]["properties"]["delivery"]["description"].lower()


def test_scheduled_navigate_puts_notify_on_the_wire_and_targets_the_primary(monkeypatch):
    """THE FLOW, end to end on the Orchestrator side: cron turn (no origin device) ->
    calendar location -> navigate_device(delivery='notify'). Two invariants at once —
    the notification path is what goes out, and with no origin the resolver is asked for
    the operator's PRIMARY device rather than nothing."""
    from Orchestrator.routes import chat_routes
    from Orchestrator.startup import ChatIn
    from Orchestrator.tasks import stamp_origin_device
    from Orchestrator.tools import BlackBoxToolExecutor

    # A cron turn: POST /chat with no originating phone.
    stamp_origin_device(ChatIn(messages=[{"role": "user", "content": "cron"}]))
    ctx_origin = chat_routes._ORIGIN_DEVICE_ID.get()
    assert ctx_origin is None

    seen, calls = {}, []
    monkeypatch.setattr(NAV.mesh, "resolve_device", _resolves_to(NODE, seen))
    monkeypatch.setattr(NAV, "_post_intent", _capture_post(calls))

    ctx = ToolContext(operator="Brandon",
                      base_url=BlackBoxToolExecutor(
                          operator="Brandon", origin_device_id=ctx_origin).base_url,
                      origin_device_id=ctx_origin)
    res = _run(NAV.execute({"destination": JOB_ADDRESS, "delivery": "notify"}, ctx))

    # no origin -> the resolver falls through to the operator's PRIMARY (precedence 3)
    assert seen["origin_device_id"] is None
    assert seen["target_device_id"] in (None, "")
    assert seen["operator"] == "Brandon"
    # exactly ONE intent frame, carrying the calendar address verbatim, delivery=notify
    assert len(calls) == 1
    params = calls[0]["frame"]["params"]
    assert params["destination"] == JOB_ADDRESS
    assert params["delivery"] == "notify"
    assert res.success is True
    assert res.data["delivery"] == "notify"


def test_a_scheduled_notify_is_never_reported_as_started_navigation(monkeypatch):
    """The measured 07:30 defect in one assertion: a queued notification must not be
    describable as a navigation that started."""
    monkeypatch.setattr(NAV.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(NAV, "_post_intent", _capture_post([]))
    res = _run(NAV.execute({"destination": JOB_ADDRESS, "delivery": "notify"},
                           ToolContext(operator="Brandon")))
    assert res.data["navigating"] is False
    assert res.data["awaiting_user_tap"] is True
    assert "started navigation" not in res.result.lower()


def test_a_phone_originated_turn_targets_that_phone_instead(monkeypatch):
    """The other half of the routing contract on the same path: a caller that DOES supply
    an origin gets it honoured all the way to the resolver."""
    from Orchestrator.routes import chat_routes
    from Orchestrator.startup import ChatIn
    from Orchestrator.tasks import stamp_origin_device

    stamp_origin_device(ChatIn(messages=[{"role": "user", "content": "take me there"}],
                               origin_device_id="brandon-fold6"))
    seen = {}
    monkeypatch.setattr(NAV.mesh, "resolve_device", _resolves_to(NODE, seen))
    monkeypatch.setattr(NAV, "_post_intent", _capture_post([]))
    _run(NAV.execute({"destination": JOB_ADDRESS},
                     ToolContext(operator="Brandon",
                                 origin_device_id=chat_routes._ORIGIN_DEVICE_ID.get())))
    assert seen["origin_device_id"] == "brandon-fold6"
