"""Non-stream /chat tells the client a turn used health data (M7 wave 3b,
contract b): when the worker's own turn delivered a wellness result,
result_data gains `wellness_used` (the SAME object the stream's
wellness_used frame carries for the turn's newest delivery), `wellness_refs`
(every ref of the turn, oldest first, at most 8) and `wellness_ref` (the
first), so the Portal with streaming off and any POST /chat client render the
footer and offer Show numbers as they do for a streamed turn. Every other
turn's result_data is byte-identical to before.

The worker (tasks.process_chat_task) runs for real on a temp box with the
fixture operator enrolled; the provider call is a stub that runs the wellness
tools through the real dispatcher inside the worker's own turn, on the
stream loop of the same provider, so the frame a stream would have sent for
the same delivery is captured and compared.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from Orchestrator.tests.wellness_agent_support import (  # noqa: F401 - agent_box is a fixture
    FX, agent_box, make_turn, sentinel_hits, stamped, stub_retrieval)
from Orchestrator.tests.wellness_support import ENR, OPERATOR
from Orchestrator.wellness import access, api, turn as turn_mod

# The result_data a non-stream chat turn has always had (tasks.process_chat_task).
BASE_KEYS = {"ui_reply", "reply", "text", "snapshot_perspective", "usage", "conv_turns_since",
             "conv_tokens_since", "ctx_tokens_since", "drift", "retrieved", "inline_media",
             "media_tasks", "artifacts", "checkpoint_triggered", "tool_outcomes"}
WELLNESS_KEYS = {"wellness_used", "wellness_refs", "wellness_ref"}
DAY = FX.day_of(FX.SENTINEL_DAYS["sleeping_hr"])
CALLS = {"summary": ("wellness_summary", {"period": "custom", "start": DAY, "end": DAY}),
         "series": ("wellness_series", {"metric": "heart_rate", "range": "last_7_days"}),
         "sessions": ("wellness_sessions", {"kind": "sleep", "range": "last_7_days"}),
         "history": ("wellness_history", {"start": FX.day_of(-6), "end": FX.day_of(0)}),
         "sources": ("wellness_sources", {})}


@pytest.fixture
def worker(agent_box, monkeypatch):
    import Orchestrator.tasks as tasks
    stub_retrieval(monkeypatch)
    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "perform_mint", lambda *a, **k: {"snap_id": "SNAP-TEST"})
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)
    return agent_box


def _dispatch(name, args, loop="stream_anthropic"):
    from Orchestrator.providers import tool_dispatch as td
    from Orchestrator.routes.chat.providers import _tool_context
    return asyncio.run(td.dispatch_tool(name, dict(args), _tool_context(OPERATOR, loop)))


def _run(monkeypatch, *, calls=(), reply="Here is the plan for today.", inp=None):
    """One process_chat_task turn on Anthropic; the stub runs `calls` (keys of
    CALLS) mid-turn, then answers `reply`. Returns (result_data, the frames a
    stream would have sent, the worker's turn)."""
    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso
    frames, seen = [], {}

    def fake(msgs, model, operator="?"):
        seen["turn"] = turn_mod.current()
        for key in calls:
            name, args = CALLS[key]
            out = _dispatch(name, args)
            frames.extend(e["data"] for e in out.events if e.get("type") == "wellness_used")
        return reply, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, "", []

    monkeypatch.setattr(cr, "call_anthropic", fake)
    body = {"messages": [{"role": "user", "content": "how did I sleep?"}], "operator": OPERATOR,
            "provider": "anthropic", "model": "claude-test", **(inp or {})}
    task = Task(task_id=f"wns-{turn_mod.new_turn_key()}", task_type=TaskType.CHAT,
                status=TaskStatus.PENDING, created_at=now_utc_iso(), updated_at=now_utc_iso(),
                operator=OPERATOR, result_data=body)
    task_db.save_task(task)
    tasks.process_chat_task(task)
    final = task_db.get_task(task.task_id)
    assert final.status == TaskStatus.COMPLETED, final.result_data
    return final.result_data, frames, seen.get("turn")


def _granted():
    return {access.GRANT_KEY: access.issue_grant(access._Granted(OPERATOR, ENR), OPERATOR)}


# ------------------------------------------------------------------ the worker

def test_a_delivered_non_stream_turn_names_its_refs_and_the_stream_s_frame(worker, monkeypatch):
    data, frames, t = _run(monkeypatch, calls=("summary", "series"), inp=_granted(),
                           reply="Your sleeping heart rate was 47.3 bpm on that night.")
    rows = worker["store"].deliveries_for_turn(t.turn_key)
    assert len(rows) == 2 and len(frames) == 2
    assert set(data) == BASE_KEYS | WELLNESS_KEYS
    assert data["wellness_refs"] == [r["ref"] for r in rows] == [f["ref"] for f in frames]
    assert data["wellness_ref"] == rows[0]["ref"]
    assert data["wellness_used"] == frames[-1]            # the newest delivery's frame, as is
    assert set(data["wellness_used"]) == {"ref", "categories", "as_of", "provider",
                                          "provider_label"}
    assert (data["wellness_used"]["provider"], data["wellness_used"]["provider_label"]) == \
        ("anthropic", "Anthropic")
    # The reply the client gets is masked, and nothing in result_data is a value.
    assert "[·]" in data["ui_reply"] and not sentinel_hits(json.dumps(data))


def test_an_ordinary_turn_gains_nothing(worker, monkeypatch):
    data, frames, _t = _run(monkeypatch, inp=_granted())
    assert set(data) == BASE_KEYS and frames == []
    assert data["ui_reply"] == "Here is the plan for today."


def test_an_unsigned_turn_is_refused_the_tool_and_gains_nothing(worker, monkeypatch):
    data, frames, t = _run(monkeypatch, calls=("summary",))
    assert t.surface == "portal" and not t.credential_ok
    assert frames == [] and worker["store"].deliveries_for_turn(t.turn_key) == []
    assert set(data) == BASE_KEYS


def test_a_reply_recalling_an_earlier_result_gains_nothing(worker, monkeypatch):
    """A heuristic carry (the reply quotes another turn's result): the ledger
    copy is masked, but this turn delivered nothing, so no footer."""
    earlier = api.summary(OPERATOR, "custom", start=DAY, end=DAY, now_ms=FX.now_ms(),
                          delivery=api.Delivery("wt_earlier", "android", "anthropic"))
    assert earlier["ok"]
    data, _frames, _t = _run(monkeypatch, inp=_granted(),
                             reply=f"As I said this morning, it was {FX.SENTINEL_SLEEPING_HR}.")
    assert set(data) == BASE_KEYS


def test_the_scheduler_copies_none_of_it(worker, monkeypatch):
    """A cron row keeps the reply text (and tool_outcomes); the refs ride in
    result_data only (tasks.db), and say no value."""
    from Orchestrator.scheduler.executor import _extract_reply
    data, _frames, _t = _run(monkeypatch, calls=("summary",), inp=_granted(),
                             reply="You slept a bit less than usual.")
    assert WELLNESS_KEYS <= set(data)
    assert _extract_reply({"result_data": data}, "job") == data["ui_reply"]


# ------------------------------------------------------------------ one builder

@pytest.mark.parametrize("key", sorted(CALLS))
def test_used_fields_is_the_stream_frame_for_every_tool(agent_box, key):
    name, args = CALLS[key]
    with stamped() as t:
        out = _dispatch(name, args)
    [frame] = [e["data"] for e in out.events if e.get("type") == "wellness_used"]
    rows = agent_box["store"].deliveries_for_turn(t.turn_key)
    fields = access.used_fields(rows, t)
    assert fields == {"wellness_used": frame, "wellness_refs": [frame["ref"]],
                      "wellness_ref": frame["ref"]}


def test_used_fields_caps_the_refs_and_keeps_their_order():
    rows = [{"ref": f"w-20261030-{i:04x}", "categories": ["sleep"],
             "payload": {"ok": True, "tool": "wellness_series", "as_of": "2026-10-30T07:10"}}
            for i in range(11)]
    rows.insert(3, dict(rows[2]))                          # one ref twice: named once
    t = make_turn(provider="anthropic")
    fields = access.used_fields(rows, t)
    assert fields["wellness_refs"] == [r["ref"] for r in rows if r is not rows[3]][:8]
    assert fields["wellness_ref"] == rows[0]["ref"]
    assert fields["wellness_used"] == {"ref": rows[-1]["ref"], "categories": ["sleep"],
                                       "as_of": "2026-10-30T07:10", "provider": "anthropic",
                                       "provider_label": "Anthropic"}
    assert access.used_fields([], t) == {} and access.used_fields(None, t) == {}
    assert access.used_fields([{"ref": None}], t) == {}
