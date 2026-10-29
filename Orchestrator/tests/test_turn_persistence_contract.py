"""The persist-and-mint tail every chat turn ends in (audit rank 74, O03-chat-routes-B-11).

Four paths record a finished turn and decide whether to mint it: POST
/chat/save (every Portal, Android, voice and CLI turn), the Computer Use loops'
_cu_save_to_blackbox, the on-device turn's persist_local_turn_and_mint, and the
/chat worker (tasks.process_chat_task: cron, SMS, MCP). These tests pin, for
each, exactly what lands in the operator state (the conversation-log items, the
counters, the snapshot metadata perform_mint reads), which mint and checkpoint
calls are made, and what the caller returns. They are the contract the shared
persist_turn_and_maybe_mint() is held to; since it decided one metadata policy
(test_turn_persistence_fixes.py) every path records the semantic ids as
relevant and sets its own location gauge.

Hermetic: perform_mint, the checkpoint task, save_operator_state and the drift
gauge are recorders; every test uses its own operator name.
"""
import asyncio
import types

import pytest

import Orchestrator.routes.chat_routes as cr
import Orchestrator.tasks as tasks
from Orchestrator.state import get_state

PROVENANCE = {"recent": ["SNAP-1"], "keyword": ["SNAP-2"], "semantic": ["SNAP-3"],
              "checkpoint": ["SNAP-4"]}
LOCATION = {"lat": 43.65, "lon": -79.38, "city": "Toronto", "state": "ON"}


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def gate(monkeypatch):
    """Auto-mint on every turn, a checkpoint due, and every side effect recorded."""
    calls = {"mint": [], "checkpoint_task": [], "saved": 0}

    def fake_mint(operator, reason):
        calls["mint"].append((operator, reason))
        return {"snap_id": f"SNAP-MINT-{len(calls['mint'])}", "dims": 8}

    def fake_create_task(task_type, operator=None, **kw):
        calls["checkpoint_task"].append((getattr(task_type, "name", task_type), operator, kw))
        return types.SimpleNamespace(task_id=f"task-ckpt-{len(calls['checkpoint_task'])}")

    def fake_save():
        calls["saved"] += 1

    for mod in (cr, tasks):
        monkeypatch.setattr(mod, "AUTO_ENABLE", True)
        monkeypatch.setattr(mod, "TURNS_THRESHOLD", 1)
        monkeypatch.setattr(mod, "TOKENS_THRESHOLD", 10 ** 9)
        monkeypatch.setattr(mod, "DEBOUNCE_MS", 0)
        monkeypatch.setattr(mod, "ON_RED", "off")
        monkeypatch.setattr(mod, "ON_YELLOW", "off")
        monkeypatch.setattr(mod, "perform_mint", fake_mint)
        monkeypatch.setattr(mod, "should_create_checkpoint", lambda op: True)
        monkeypatch.setattr(mod, "create_task", fake_create_task)
        monkeypatch.setattr(mod, "save_operator_state", fake_save)
        monkeypatch.setattr(mod, "drift_state_for", lambda s: "green")
    return calls


def _state_view(operator):
    s = get_state(operator)
    meta = s.last_context_meta
    return {
        "log": [{k: v for k, v in t.items() if k != "utc"} for t in s.conversation_log],
        "counters": [s.conv_turns_since, s.conv_tokens_since, s.total_turns, s.total_tokens,
                     s.context_used],
        "meta": {k: meta.get(k) for k in ("model", "location", "usage_last", "gm_excerpt",
                                          "recent_ids", "relevant_ids")},
    }


def test_chat_save_records_and_mints(gate):
    op = "persist-contract-save"
    resp = asyncio.run(cr.chat_save(_Req({
        "operator": op, "user_message": "Where am I?", "assistant_response": "In Toronto.",
        "reasoning": "The location rides along.", "model": "test-model",
        "tokens": {"prompt": 3, "completion": 4}, "provenance": PROVENANCE,
        "location": LOCATION})))

    assert resp == {
        "success": True, "operator": op, "total_turns": 1, "conv_turns_since": 1,
        "minted": True, "snap_id": "SNAP-MINT-1", "media_tasks": [], "modified_response": None,
        "artifacts": [], "checkpoint_triggered": True, "dims": 8}
    assert _state_view(op) == {
        "log": [
            {"role": "user", "text": "Where am I?\n[user location: 43.65,-79.38 · Toronto, ON]"},
            {"role": "assistant", "text": "In Toronto.",
             "snap_text": "[REASONING]\nThe location rides along.\n\n[RESPONSE]\nIn Toronto."},
        ],
        "counters": [1, 7, 1, 7, 0],
        "meta": {"model": "test-model", "location": "43.65,-79.38 · Toronto, ON",
                 "usage_last": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
                 "gm_excerpt": True, "recent_ids": ["SNAP-1"],
                 "relevant_ids": ["SNAP-2", "SNAP-4", "SNAP-3"]},
    }
    assert gate["mint"] == [(op, "TURNS")]
    assert gate["checkpoint_task"] == [("CHECKPOINT", op, {"result_data": {"manual": False}})]
    assert gate["saved"] == 1


def test_cu_save_records_and_mints(gate):
    op = "persist-contract-cu"
    session = types.SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 5},
                                    provenance=PROVENANCE)
    asyncio.run(cr._cu_save_to_blackbox(op, "Open the docs", "Opened them.", session))

    assert _state_view(op) == {
        "log": [
            {"role": "user", "text": "Open the docs"},
            {"role": "assistant", "text": "Opened them.", "snap_text": "Opened them."},
        ],
        "counters": [1, 15, 1, 15, 0],
        "meta": {"model": "computer-use", "location": "",
                 "usage_last": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                 "gm_excerpt": True, "recent_ids": ["SNAP-1"],
                 "relevant_ids": ["SNAP-2", "SNAP-4", "SNAP-3"]},
    }
    assert gate["mint"] == [(op, "TURNS")]
    assert gate["checkpoint_task"] == [("CHECKPOINT", op, {"result_data": {"manual": False}})]
    assert gate["saved"] == 1


def test_cu_save_refuses_an_unsafe_operator(gate):
    session = types.SimpleNamespace(usage={}, provenance={})
    asyncio.run(cr._cu_save_to_blackbox("../escape", "x", "y", session))
    assert gate["mint"] == [] and gate["saved"] == 0


def test_local_turn_records_and_mints(gate):
    op = "persist-contract-local"
    out = asyncio.run(cr.persist_local_turn_and_mint(
        op, "Roll a die", "I rolled a 3.",
        tool_transcript=[{"name": "roll_dice", "args": {"sides": 6}, "result": "Rolled 3"}],
        provenance=PROVENANCE))

    assert out == {"snap_id": "SNAP-MINT-1", "minted": True, "checkpoint_triggered": True}
    composed = 'I rolled a 3.\n\n[TOOLS USED]\n- roll_dice({"sides": 6}) -> Rolled 3'
    assert _state_view(op) == {
        "log": [
            {"role": "user", "text": "Roll a die"},
            {"role": "assistant", "text": composed, "snap_text": composed},
        ],
        "counters": [1, 0, 1, 0, 0],
        "meta": {"model": "local", "location": "",
                 "usage_last": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                 "gm_excerpt": True, "recent_ids": ["SNAP-1"],
                 "relevant_ids": ["SNAP-2", "SNAP-4", "SNAP-3"]},
    }
    assert gate["mint"] == [(op, "TURNS")]
    assert gate["checkpoint_task"] == [("CHECKPOINT", op, {"result_data": {"manual": False}})]
    assert gate["saved"] == 1


def test_worker_turn_records_and_mints(gate, monkeypatch):
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso

    op = "persist-contract-worker"
    monkeypatch.setattr(tasks, "TOOLVAULT_ENABLED", False)  # no tool-injection embedding
    monkeypatch.setattr(tasks, "build_fossil_context", lambda *a, **k: ("", dict(PROVENANCE)))
    monkeypatch.setattr(cr, "call_anthropic", lambda msgs, model, operator: (
        "Briefing done.", {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26},
        "Thought about it.", []))
    task = Task(task_id="persist-contract-1", task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator=op,
                result_data={"messages": [{"role": "user", "content": "Brief me"}],
                             "operator": op, "provider": "anthropic", "model": "claude-opus-5-5"})
    task_db.save_task(task)
    tasks.process_chat_task(task)

    final = task_db.get_task("persist-contract-1")
    assert final.status == TaskStatus.COMPLETED, final.result_data
    view = _state_view(op)
    assert view["log"][0] == {"role": "user", "text": "Brief me"}
    assert view["log"][1]["text"] == "Briefing done."
    assert view["log"][1]["snap_text"].startswith("Briefing done.\n\nKeywords: ")
    assert view["log"][1]["snap_text"].endswith("\n\n[REASONING]\nThought about it.")
    assert view["counters"] == [1, 26, 1, 26, 26]
    assert view["meta"] == {
        "model": "claude-opus-5-5", "location": "",
        "usage_last": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26},
        "gm_excerpt": True, "recent_ids": ["SNAP-1"],
        "relevant_ids": ["SNAP-2", "SNAP-4", "SNAP-3"]}
    assert gate["mint"] == [(op, "TURNS")]
    assert gate["checkpoint_task"] == [("CHECKPOINT", op, {"result_data": {"manual": False}})]
    assert gate["saved"] == 1
