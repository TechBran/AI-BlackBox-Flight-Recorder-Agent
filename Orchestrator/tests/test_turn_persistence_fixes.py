"""One policy for what a turn's snapshot records (audit rank 74, O03-chat-routes-B-11).

The four copies of the persist tail disagreed on the snapshot metadata
perform_mint reads, and now that they are one function the policy is decided
once:

  * provenance: "relevant" is every retrieval channel that informed the turn —
    keyword, checkpoint AND semantic — on every path. /chat/save, the CU loops
    and the /chat worker dropped the semantic ids (the worker kept keyword
    only), so the snapshots of the turns semantic retrieval informed most never
    said so; the on-device path already recorded them.
  * location: every turn sets the LOCATION gauge, "" when it carried none. Only
    /chat/save did, so a CU, on-device or worker turn minted after a located
    phone turn inherited that phone's fix.
  * usage: every turn records its own token usage. The on-device turn did not,
    so its snapshot showed the previous cloud turn's tokens.
"""
import asyncio
import types

import pytest

import Orchestrator.routes.chat_routes as cr
import Orchestrator.tasks as tasks
from Orchestrator.state import get_state
from Orchestrator.tests.test_turn_persistence_contract import LOCATION, PROVENANCE, _Req
from Orchestrator.tests.test_turn_persistence_contract import gate  # noqa: F401 - fixture

ALL_RELEVANT = ["SNAP-2", "SNAP-4", "SNAP-3"]  # keyword, checkpoint, semantic


def _located_phone_turn(op):
    """A /chat/save turn carrying a location, minted (so the state holds its gauge)."""
    asyncio.run(cr.chat_save(_Req({
        "operator": op, "user_message": "hi", "assistant_response": "hello",
        "model": "m", "tokens": {"prompt": 9, "completion": 9}, "location": LOCATION})))
    assert get_state(op).last_context_meta["location"]


def test_chat_save_records_semantic_provenance(gate):
    op = "persist-fix-save"
    asyncio.run(cr.chat_save(_Req({
        "operator": op, "assistant_response": "ok", "model": "m", "provenance": PROVENANCE})))
    assert get_state(op).last_context_meta["relevant_ids"] == ALL_RELEVANT


def test_cu_turn_records_semantic_provenance_and_no_inherited_location(gate, monkeypatch):
    op = "persist-fix-cu"
    monkeypatch.setattr(cr, "AUTO_ENABLE", False)  # the phone turn's gauge stays in the state
    _located_phone_turn(op)
    session = types.SimpleNamespace(usage={"prompt_tokens": 1, "completion_tokens": 1},
                                    provenance=PROVENANCE)
    asyncio.run(cr._cu_save_to_blackbox(op, "open it", "opened", session))
    meta = get_state(op).last_context_meta
    assert meta["relevant_ids"] == ALL_RELEVANT
    assert meta["location"] == ""


def test_local_turn_records_its_own_usage_and_no_inherited_location(gate, monkeypatch):
    op = "persist-fix-local"
    monkeypatch.setattr(cr, "AUTO_ENABLE", False)
    _located_phone_turn(op)
    asyncio.run(cr.persist_local_turn_and_mint(op, "roll", "3", provenance=PROVENANCE))
    meta = get_state(op).last_context_meta
    assert meta["usage_last"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert meta["location"] == ""
    assert meta["relevant_ids"] == ALL_RELEVANT


def test_worker_turn_records_every_relevant_channel_and_no_inherited_location(gate, monkeypatch):
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso

    op = "persist-fix-worker"
    monkeypatch.setattr(cr, "AUTO_ENABLE", False)
    _located_phone_turn(op)
    monkeypatch.setattr(tasks, "TOOLVAULT_ENABLED", False)  # no tool-injection embedding
    monkeypatch.setattr(tasks, "build_fossil_context", lambda *a, **k: ("", dict(PROVENANCE)))
    monkeypatch.setattr(cr, "call_anthropic", lambda msgs, model, operator: (
        "Done.", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}, "", []))
    task = Task(task_id="persist-fix-1", task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator=op,
                result_data={"messages": [{"role": "user", "content": "Brief me"}],
                             "operator": op, "provider": "anthropic", "model": "claude-opus-5-5"})
    task_db.save_task(task)
    tasks.process_chat_task(task)

    assert task_db.get_task("persist-fix-1").status == TaskStatus.COMPLETED
    meta = get_state(op).last_context_meta
    assert meta["relevant_ids"] == ALL_RELEVANT
    assert meta["location"] == ""
    assert meta["usage_last"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}


@pytest.mark.parametrize("name", ["relevant_keys", "record_usage"])
def test_the_policy_is_not_a_per_caller_choice(name):
    import inspect
    from Orchestrator import turn_persistence
    assert name not in inspect.signature(turn_persistence.persist_turn_and_maybe_mint).parameters
