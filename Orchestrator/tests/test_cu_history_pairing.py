"""CU conversation-history trimming keeps tool calls paired with their results
(audit O13-computer-use-05).

The old trim_history kept history[:2] + history[-198:]. A CU turn alternates
assistant(tool_use) / user(tool_result), so the cut either opened on a
tool_result whose tool_use was dropped or left history[1]'s tool_use followed by
a message that does not answer it. Anthropic rejects both shapes, so every
session past 200 messages (two long turns at the 150-step cap) 400'd on the
next request. The Gemini session had the same shape for function_call /
function_response pairs.

Contracts under test (both providers):
  * the trimmed history fits the cap;
  * it opens on a user message, and every tool_result answers a tool_use in the
    assistant message right before it (and vice versa);
  * whole old turns are dropped first; a single turn longer than the cap keeps
    its opening prompt plus the newest whole steps;
  * thinking blocks are stripped from a trimmed history (an edited prefix
    invalidates every later signed block on the preserved-thinking models).
"""
import pytest

from google.genai import types

from Orchestrator.browser.session_manager import ComputerUseSession
from Orchestrator.gemini_cu.session_manager import GeminiCUSession
from Orchestrator.utils import cu_history


# ── Anthropic-format synthetic history ──────────────────────────────────────

def _prompt(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _step(turn, i, *, thinking=False):
    tid = f"toolu_{turn}_{i}"
    blocks = []
    if thinking:
        blocks.append({"type": "thinking", "thinking": "", "signature": f"sig-{tid}"})
    blocks.append({"type": "tool_use", "id": tid, "name": "computer",
                   "input": {"action": "screenshot"}})
    return [
        {"role": "assistant", "content": blocks},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                      "content": [{"type": "text", "text": "ok"}]}]},
    ]


def _turn(turn, steps, *, thinking=False):
    msgs = [_prompt(f"task {turn}")]
    for i in range(steps):
        msgs.extend(_step(turn, i, thinking=thinking))
    msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"done {turn}"}]})
    return msgs


def _assert_paired(history):
    assert history, "trim must never empty the history"
    assert history[0]["role"] == "user", "the first message must be a user turn"
    for idx, msg in enumerate(history):
        content = msg["content"] if isinstance(msg["content"], list) else []
        results = [b["tool_use_id"] for b in content if b.get("type") == "tool_result"]
        if results:
            prev = history[idx - 1] if idx else None
            assert prev is not None and prev["role"] == "assistant", (idx, results)
            uses = {b["id"] for b in prev["content"] if b.get("type") == "tool_use"}
            assert set(results) <= uses, f"orphan tool_result at {idx}: {results}"
        uses = [b["id"] for b in content if b.get("type") == "tool_use"]
        if uses:
            nxt = history[idx + 1] if idx + 1 < len(history) else None
            assert nxt is not None and nxt["role"] == "user", f"unanswered tool_use at {idx}"
            answered = {b.get("tool_use_id") for b in nxt["content"]
                        if b.get("type") == "tool_result"}
            assert set(uses) <= answered, f"unanswered tool_use at {idx}: {uses}"


def test_old_trim_shape_orphans_tool_results():
    """The regression the fix closes: the head+tail slice of a 300-message CU
    history is not a valid request."""
    history = _turn(1, 150)          # 1 + 300 + 1 = 302 messages
    session = ComputerUseSession.__new__(ComputerUseSession)
    session.conversation_history = list(history)
    session.trim_history()
    _assert_paired(session.conversation_history)
    assert len(session.conversation_history) <= 200


def test_whole_old_turns_are_dropped_first():
    history = _turn(1, 60) + _turn(2, 60) + _turn(3, 20)   # 122 + 122 + 42 = 286
    out = cu_history.trim_anthropic_history(history, max_messages=200)
    assert len(out) <= 200
    _assert_paired(out)
    # Turn 1 is gone entirely; turns 2 and 3 survive verbatim.
    assert out[0] == _prompt("task 2")
    assert out[-1] == {"role": "assistant", "content": [{"type": "text", "text": "done 3"}]}
    assert len(out) == 122 + 42


def test_single_turn_longer_than_the_cap_keeps_prompt_and_newest_steps():
    history = _turn(1, 150)
    out = cu_history.trim_anthropic_history(history, max_messages=200)
    assert len(out) <= 200
    _assert_paired(out)
    assert out[0]["role"] == "user"
    assert out[0]["content"][0] == {"type": "text", "text": "task 1"}
    # The newest step and the final answer survive.
    assert out[-1] == history[-1]
    assert out[-3] == history[-3]


def test_trim_under_the_cap_is_identity():
    history = _turn(1, 10)
    assert cu_history.trim_anthropic_history(history, max_messages=200) is history


def test_trimmed_history_drops_thinking_blocks():
    history = _turn(1, 150, thinking=True)
    out = cu_history.trim_anthropic_history(history, max_messages=200)
    for msg in out:
        if isinstance(msg["content"], list):
            assert not any(b.get("type") in ("thinking", "redacted_thinking")
                           for b in msg["content"])
    _assert_paired(out)


def test_untrimmed_history_keeps_thinking_blocks():
    history = _turn(1, 5, thinking=True)
    out = cu_history.trim_anthropic_history(history, max_messages=200)
    assert out is history


def test_unanswered_trailing_tool_use_is_answered():
    """A run cancelled between the assistant turn and its results leaves a
    dangling tool_use; the next user prompt would 400 without an answer."""
    history = _turn(1, 3)[:-1] + [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "dangling", "name": "left_click",
         "toolset_name": "computer", "input": {}}]}]
    out = cu_history.repair_anthropic_pairing(history)
    _assert_paired(out)
    tail = out[-1]["content"][0]
    assert tail["tool_use_id"] == "dangling"
    assert tail["is_error"] is True and tail["toolset_name"] == "computer"


def test_trim_of_300_message_toolset_history_stays_valid():
    """300+ messages of 5.5 toolset batches (several members per assistant turn)."""
    history = [_prompt("go")]
    for i in range(110):
        ids = [f"m{i}a", f"m{i}b"]
        history.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": t, "name": "left_click", "toolset_name": "computer",
             "input": {}} for t in ids]})
        history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": t, "toolset_name": "computer",
             "content": "OK"} for t in ids]})
    history += _turn(2, 50)
    out = cu_history.trim_anthropic_history(history, max_messages=200)
    assert len(out) <= 200
    _assert_paired(out)


# ── Gemini (google-genai Content) ───────────────────────────────────────────

def _g_prompt(text):
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def _g_turn(turn, steps):
    out = [_g_prompt(f"task {turn}")]
    for i in range(steps):
        out.append(types.Content(role="model", parts=[
            types.Part.from_function_call(name="click_at", args={"x": i, "y": turn})]))
        out.append(types.Content(role="user", parts=[
            types.Part.from_function_response(name="click_at", response={"url": "x"})]))
    out.append(types.Content(role="model", parts=[types.Part.from_text(text=f"done {turn}")]))
    return out


def _g_assert_paired(contents):
    assert contents and contents[0].role == "user"
    assert not any(getattr(p, "function_response", None) for p in contents[0].parts or [])
    for idx, c in enumerate(contents):
        responses = [p for p in (c.parts or []) if getattr(p, "function_response", None)]
        if responses:
            prev = contents[idx - 1]
            calls = [p for p in (prev.parts or []) if getattr(p, "function_call", None)]
            assert prev.role == "model" and len(calls) == len(responses), idx
        calls = [p for p in (c.parts or []) if getattr(p, "function_call", None)]
        if calls:
            nxt = contents[idx + 1]
            assert nxt.role == "user"
            assert len([p for p in nxt.parts if getattr(p, "function_response", None)]) == len(calls)


def test_gemini_trim_drops_whole_turns():
    contents = _g_turn(1, 60) + _g_turn(2, 60) + _g_turn(3, 20)
    out = cu_history.trim_genai_history(contents, max_messages=200)
    assert len(out) <= 200
    _g_assert_paired(out)
    assert out[0].parts[0].text == "task 2"


def test_gemini_session_trim_of_one_long_turn_stays_paired():
    session = GeminiCUSession("op", "blackbox", "desktop")
    session.conversation_history = _g_turn(1, 150)
    session.trim_history()
    _g_assert_paired(session.conversation_history)
    assert len(session.conversation_history) <= 200
    assert session.conversation_history[0].parts[0].text == "task 1"


def test_gemini_session_uses_the_single_sourced_limits():
    """O13-computer-use-13: the session manager must not re-hardcode 50/300."""
    from Orchestrator.config import CU_MAX_ITERATIONS, CU_SESSION_TIMEOUT
    import Orchestrator.gemini_cu.session_manager as gsm
    session = GeminiCUSession("op", "blackbox", "desktop")
    assert session.total_steps == CU_MAX_ITERATIONS
    session.last_activity -= CU_SESSION_TIMEOUT - 5
    assert session.is_expired() is False
    session.last_activity -= 10
    assert session.is_expired() is True
    assert gsm.MAX_ITERATIONS == CU_MAX_ITERATIONS
    assert gsm.SESSION_TIMEOUT == CU_SESSION_TIMEOUT


def test_gemini_loop_trims_carried_history():
    import inspect
    from Orchestrator.gemini_cu.agent_loop import run_gemini_cu_loop
    assert "trim_history" in inspect.getsource(run_gemini_cu_loop)


# ── one shared screenshot budget for every CU driver ─────────────────────────

def test_budget_helpers_are_shared():
    from Orchestrator.browser import session_manager
    from Orchestrator.gemini_cu import agent_loop
    assert session_manager.budget_screenshots_in_history is cu_history.budget_screenshots_in_history
    assert agent_loop._budget_contents_images is cu_history.budget_genai_images


@pytest.mark.parametrize("edited", [True, False])
def test_request_budget_strips_thinking_only_after_an_edit(edited):
    history = [{"role": "user", "content": [
        {"type": "text", "text": "go"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A"}}]}]
    steps = 6 if edited else 1
    for i in range(steps):
        history.append({"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": f"s{i}"},
            {"type": "tool_use", "id": f"t{i}", "name": "computer", "input": {}}]})
        history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                        "content": [{"type": "image", "source": {
                            "type": "base64", "media_type": "image/png", "data": f"S{i}"}}]}]})
    out = cu_history.budget_anthropic_request(history, keep_images=3)
    thinking = [b for m in out for b in m["content"] if b.get("type") == "thinking"]
    if edited:
        assert thinking == []
        _assert_paired(out)
    else:
        assert out is history and len(thinking) == 1
