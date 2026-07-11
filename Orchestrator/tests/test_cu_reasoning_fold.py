"""G3 live-narration fold: _drain_and_fold must fold the model's `content`
narration into the ACCUMULATING reasoning_text transcript (per-step, tagged),
while keeping the terse "step N/M — action" line on the progress_text pill.

Covers both driver shapes the shared fold must handle:
  * Anthropic — `content` is per-TOKEN str deltas (buffered, flushed on step
    boundaries so it isn't one DB write per token).
  * Gemini/OpenAI — `content` is a per-STEP {"text","step"} lump.

Mutation check: if the fold reverts to discarding content (the old
"thinking…"-only behavior), the reasoning assertions go RED.
"""
import asyncio
import types

import Orchestrator.browser.headless as H
import Orchestrator.tasks as T


def _run_fold(events, monkeypatch):
    reasoning, progress = [], []
    monkeypatch.setattr(T, "append_task_reasoning",
                        lambda tid, chunk: reasoning.append((tid, chunk)))
    monkeypatch.setattr(T, "append_task_progress",
                        lambda tid, line: progress.append((tid, line)))

    async def run():
        q = asyncio.Queue()
        for e in events:
            q.put_nowait(e)
        q.put_nowait(None)  # driver-finished sentinel
        session = types.SimpleNamespace(
            event_queue=q, current_step=len(events),
            total_tokens={"input": 0, "output": 0})
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(None)  # a completed, awaitable agent_task
        return await H._drain_and_fold(session, fut, [], task_id="t1")

    result = asyncio.run(run())
    return reasoning, progress, result


def test_anthropic_token_deltas_folded_per_step(monkeypatch):
    events = [
        {"type": "cu_step", "data": {"step": 1, "total": 15}},
        {"type": "content", "data": "I'll click "},
        {"type": "content", "data": "the search box"},
        {"type": "cu_action", "data": {"action": "left_click", "params": [243, 118], "step": 1}},
        {"type": "cu_step", "data": {"step": 2, "total": 15}},
        {"type": "content", "data": "now typing Tokyo"},
        {"type": "cu_action", "data": {"action": "type", "params": "Tokyo", "step": 2}},
        {"type": "done", "data": {"content": "done"}},
    ]
    reasoning, progress, _ = _run_fold(events, monkeypatch)

    joined = "".join(c for _, c in reasoning)
    # narration is folded (NOT discarded), token deltas coalesced per step
    assert "I'll click the search box" in joined
    assert "now typing Tokyo" in joined
    # tagged per step, first flush of each step only
    assert "[step 1]" in joined and "[step 2]" in joined
    # the action is ALSO folded into the transcript ("→ left_click(...)")
    assert "left_click" in joined and "→" in joined
    # the terse pill one-liner still carries the concrete action
    assert any("left_click" in line for _, line in progress)


def test_gemini_openai_lump_folded(monkeypatch):
    events = [
        {"type": "cu_step", "data": {"step": 1, "total": 10}},
        {"type": "content", "data": {"text": "Opening maps and searching for coffee", "step": 1}},
        {"type": "cu_action", "data": {"action": "click", "params": [10, 20], "step": 1}},
        {"type": "done", "data": {"content": "ok"}},
    ]
    reasoning, progress, _ = _run_fold(events, monkeypatch)
    joined = "".join(c for _, c in reasoning)
    assert "Opening maps and searching for coffee" in joined  # per-step lump folded
    assert "click" in joined                                  # action line folded too
    assert any("click" in line for _, line in progress)


def test_action_only_openai_like_still_populates_reasoning(monkeypatch):
    # OpenAI CU often emits NO reasoning summary / no `content` for a step — just
    # actions. The action fold is the floor: the transcript must STILL be non-empty
    # (readable "what it's doing") so the frontend window isn't hidden as empty.
    # Realistic dict params (what the drivers actually emit) so the transcript
    # shows WHAT it did — the coordinates and the typed text, not just the verb.
    events = [
        {"type": "cu_step", "data": {"step": 1, "total": 5}},
        {"type": "cu_action", "data": {"action": "left_click", "params": {"coordinate": [10, 20]}, "step": 1}},
        {"type": "cu_step", "data": {"step": 2, "total": 5}},
        {"type": "cu_action", "data": {"action": "type", "params": {"text": "Tokyo"}, "step": 2}},
        {"type": "done", "data": {"content": "ok"}},
    ]
    reasoning, progress, _ = _run_fold(events, monkeypatch)
    joined = "".join(c for _, c in reasoning)
    assert joined.strip() != ""          # NOT empty despite zero narration
    assert "left_click" in joined and "type" in joined
    assert "Tokyo" in joined             # the typed text is visible ("what it did")
    assert "[step 1]" in joined and "[step 2]" in joined
    assert any("left_click" in line for _, line in progress)
