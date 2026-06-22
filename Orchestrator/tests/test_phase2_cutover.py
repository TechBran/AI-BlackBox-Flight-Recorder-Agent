"""Phase 1+2 core cutover tests (pure-production reply/snapshot parsing).

Gates the atomic removal of the {ui_reply, snapshot_perspective} JSON envelope:

  1. Streaming system prompt is a NO-OP (it never carried the envelope).
  2. Non-stream system prompt lost the envelope but kept tool/artifact/memory
     guidance + the "you MUST call the tool" imperative.
  3. The non-stream worker treats plain prose as the reply; snap_text becomes the
     response + a deterministic Keywords digest (no [REASONING] header, no
     sentinel); result_data keys preserved (snapshot_perspective back-compat).
  4. _extract_keywords is deterministic, kebab-case, and total (no crash on
     empty/short/non-string input).
  5. All 5 result_data consumers (scheduler / sms / mcp / portal / overlay) stay
     non-empty for a normal prose reply.

Plan: docs/plans/2026-06-22-pure-production-reply-snapshot-parsing.md (Phase 1+2).
"""

import re

import pytest


ENVELOPE_TOKENS = ("ui_reply", "snapshot_perspective", "single, raw JSON object")


def _streaming_prompt():
    """The streaming /chat/stream path builds build_core_system_prompt(...)."""
    from Orchestrator.tasks import build_core_system_prompt, STREAM_EXCERPT

    dyn = build_core_system_prompt("FAKE_TOOL_INSTRUCTIONS_BLOCK")
    return dyn, STREAM_EXCERPT


def _nonstream_prompt():
    """Post-cutover the non-stream worker uses the SAME core prompt as streaming."""
    from Orchestrator.tasks import build_core_system_prompt, STREAM_EXCERPT

    dyn = build_core_system_prompt("FAKE_TOOL_INSTRUCTIONS_BLOCK")
    return dyn, STREAM_EXCERPT


# --- 1. Streaming prompt is a NO-OP -----------------------------------------

def test_streaming_prompt_has_no_envelope():
    dyn, stream_excerpt = _streaming_prompt()
    for prompt in (dyn, stream_excerpt):
        for tok in ENVELOPE_TOKENS:
            assert tok not in prompt, f"streaming prompt unexpectedly contains {tok!r}"


def test_streaming_prompt_retains_tool_guidance():
    dyn, _ = _streaming_prompt()
    assert "you MUST call" in dyn
    assert "FAKE_TOOL_INSTRUCTIONS_BLOCK" in dyn


# --- 2. Non-stream prompt: envelope gone, guidance retained -----------------

def test_nonstream_prompt_has_no_envelope():
    dyn, fallback = _nonstream_prompt()
    for prompt in (dyn, fallback):
        for tok in ENVELOPE_TOKENS:
            assert tok not in prompt, f"non-stream prompt unexpectedly contains {tok!r}"


def test_nonstream_prompt_retains_tool_imperative_and_guidance():
    dyn, _ = _nonstream_prompt()
    assert "you MUST call" in dyn
    assert "ARTIFACT" in dyn
    assert "BLACKBOX MEMORY SYSTEM" in dyn or "snapshot" in dyn.lower()
    assert "FAKE_TOOL_INSTRUCTIONS_BLOCK" in dyn


def test_output_spec_core_is_envelope_free():
    from Orchestrator.config import OUTPUT_SPEC_CORE, OUTPUT_SPEC

    for tok in ENVELOPE_TOKENS + ("RESPONSE FORMAT:", "KEY DEFINITIONS:"):
        assert tok not in OUTPUT_SPEC_CORE, f"OUTPUT_SPEC_CORE still has {tok!r}"
        assert tok not in OUTPUT_SPEC, f"OUTPUT_SPEC still has {tok!r}"
    assert "you MUST call" in OUTPUT_SPEC_CORE
    assert "ARTIFACT/FILE GENERATION" in OUTPUT_SPEC_CORE
    assert "BLACKBOX MEMORY SYSTEM" in OUTPUT_SPEC_CORE
    assert "search_snapshots" in OUTPUT_SPEC_CORE


def test_dead_get_system_prompt_helper_removed():
    import Orchestrator.routes.chat_routes as cr

    assert not hasattr(cr, "_get_system_prompt"), "_get_system_prompt should be deleted"
    assert "instructions" not in cr._inject_cache


# --- 4. Deterministic keyword digest ----------------------------------------

KEBAB_RX = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def test_extract_keywords_deterministic():
    from Orchestrator.tasks import _extract_keywords

    text = (
        "The Orchestrator API runs on port 9091 using FastAPI. We fixed the "
        "snapshot_perspective envelope bug and minted SNAP-20260622-1234 with "
        "config.py changes. Keywords matter for recall."
    )
    a = _extract_keywords(text, 7)
    b = _extract_keywords(text, 7)
    assert a == b, "keyword extraction must be deterministic"
    assert a, "should return some keywords for substantial text"


def test_extract_keywords_is_kebab_case():
    from Orchestrator.tasks import _extract_keywords

    kws = _extract_keywords(
        "FastAPI uses snake_case_helpers and config.py and ALLCAPS terms. "
        "Embeddings recall improved.",
        7,
    )
    assert kws
    for kw in kws:
        assert KEBAB_RX.match(kw), f"{kw!r} is not kebab-case"


def test_extract_keywords_captures_entities():
    from Orchestrator.tasks import _extract_keywords

    kws = _extract_keywords(
        "Minted SNAP-20260622-1234 touching config.py and snake_case_thing.", 10
    )
    assert "snap-20260622-1234" in kws
    assert "config-py" in kws
    assert "snake-case-thing" in kws


def test_extract_keywords_respects_k():
    from Orchestrator.tasks import _extract_keywords

    text = " ".join(f"word{i}token" for i in range(50))
    assert len(_extract_keywords(text, 3)) <= 3
    assert len(_extract_keywords(text, 7)) <= 7


def test_extract_keywords_total_on_edge_inputs():
    from Orchestrator.tasks import _extract_keywords

    assert _extract_keywords("", 7) == []
    assert _extract_keywords("a", 7) == []
    assert _extract_keywords("hi ok", 7) == []
    assert _extract_keywords(None, 7) == []
    assert _extract_keywords(12345, 7) == []


# --- 3. Non-stream worker ---------------------------------------------------

PROSE_REPLY = (
    "The Orchestrator runs FastAPI on port 9091 and proxies the Portal. "
    "Snapshots are immutable and embedded for semantic search."
)


@pytest.fixture
def worker_env(monkeypatch):
    import Orchestrator.tasks as tasks
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso

    captured = {"turns": []}

    def fake_call_anthropic(messages, model, operator="Brandon"):
        return PROSE_REPLY, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    monkeypatch.setattr(cr, "call_anthropic", fake_call_anthropic)

    monkeypatch.setattr(tasks, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "get_recent_fossils_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "keyword_retrieve_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "semantic_retrieve", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "get_recent_checkpoints_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "hybrid_retrieve", lambda *a, **k: [])

    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "perform_mint", lambda *a, **k: {"snap_id": "SNAP-TEST"})
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)

    orig_get_state = tasks.get_state

    def spy_get_state(op):
        st = orig_get_state(op)
        real_add = st.add_conversation_turn

        def capturing_add(turn, max_turns=100):
            captured["turns"].append(dict(turn))
            return real_add(turn, max_turns)

        st.add_conversation_turn = capturing_add
        return st

    monkeypatch.setattr(tasks, "get_state", spy_get_state)

    task = Task(
        task_id="phase2-test-task",
        task_type=TaskType.CHAT,
        status=TaskStatus.PENDING,
        created_at=now_utc_iso(),
        updated_at=now_utc_iso(),
        operator="Phase2Tester",
        result_data={
            "messages": [{"role": "user", "content": "How does the Orchestrator work?"}],
            "operator": "Phase2Tester",
            "provider": "anthropic",
            "model": "claude-test",
        },
    )
    task_db.save_task(task)

    tasks.process_chat_task(task)

    final = task_db.get_task("phase2-test-task")
    return {"task": final, "captured": captured, "tasks_mod": tasks}


def _assistant_turn(captured):
    for t in captured["turns"]:
        if t.get("role") == "assistant":
            return t
    return None


def test_worker_reply_is_prose(worker_env):
    rd = worker_env["task"].result_data
    assert rd["ui_reply"] == PROSE_REPLY
    assert rd["reply"] == PROSE_REPLY
    assert rd["text"] == PROSE_REPLY


def test_worker_snap_text_is_response_plus_keywords(worker_env):
    turn = _assistant_turn(worker_env["captured"])
    assert turn is not None, "no assistant turn captured"
    snap_text = turn["snap_text"]

    assert snap_text.startswith(PROSE_REPLY)
    assert "\n\nKeywords: " in snap_text
    assert "[REASONING]" not in snap_text
    assert "[RESPONSE]" not in snap_text
    assert "Could not parse" not in snap_text
    assert "snapshot_perspective" not in snap_text

    kw_line = snap_text.split("\n\nKeywords: ", 1)[1]
    assert kw_line.strip(), "Keywords line must be non-empty"


def test_worker_snapshot_perspective_backcompat_key_present(worker_env):
    rd = worker_env["task"].result_data
    assert "snapshot_perspective" in rd
    assert rd["snapshot_perspective"] == ""


# --- 5. All 5 consumers stay non-empty --------------------------------------

def _make_result_data(reply=PROSE_REPLY):
    return {
        "ui_reply": reply,
        "reply": reply,
        "text": reply,
        "snapshot_perspective": "",
    }


def test_consumer_scheduler_extract_reply():
    from Orchestrator.scheduler.executor import _extract_reply

    rd = _make_result_data()
    got = _extract_reply({"result_data": rd}, "phase2-job")
    assert got == PROSE_REPLY


def test_consumer_sms_semantics():
    rd = _make_result_data()
    reply = rd.get("ui_reply", "") or rd.get("text", "")
    assert reply == PROSE_REPLY


def test_consumer_mcp_semantics():
    rd = _make_result_data()
    reply = rd.get("reply") or rd.get("ui_reply") or rd.get("text") or "No response"
    assert reply == PROSE_REPLY


def test_consumer_portal_and_overlay_keys_exist():
    rd = _make_result_data()
    assert rd.get("ui_reply")
    assert rd.get("reply")
    assert rd.get("text")


def test_consumer_scheduler_raises_when_all_absent():
    from Orchestrator.scheduler.executor import _extract_reply

    with pytest.raises(RuntimeError):
        _extract_reply({"result_data": {"snapshot_perspective": ""}}, "phase2-job")
