"""Anthropic CU 413 guard (production-readiness plan 2026-07-23, M0/M3).

Root cause (proved on MS02): the CU loop re-sends the ENTIRE accumulated
history every iteration and only stripped screenshots at task-save — never
inside the send loop. With CU_MAX_ITERATIONS=150 the request accumulates up to
~150 PNGs and dies with 413 request_too_large (Anthropic caps ~100 images /
~32MB per request). Brandon experienced this as "Anthropic says I'm giving it
too much to do" — a payload-size failure, not task complexity.

The fix contract under test: budget_screenshots_in_history keeps only the most
recent K image blocks and replaces older ones with text placeholders, and the
driver applies it EVERY iteration before building the payload.
"""
import copy
import inspect

import pytest

from Orchestrator.browser.session_manager import budget_screenshots_in_history


def _img(tag: str) -> dict:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": "PNG" + tag}}


def _assistant_tool_use(i: int) -> dict:
    return {"role": "assistant",
            "content": [{"type": "tool_use", "id": f"toolu_{i}",
                         "name": "computer", "input": {"action": "screenshot"}}]}


def _tool_result_with_image(i: int) -> dict:
    return {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"toolu_{i}",
                         "content": [_img(f"step{i}")]}]}


def _synthetic_history(steps: int = 150) -> list:
    history = [{"role": "user",
                "content": [{"type": "text", "text": "do the task"},
                            _img("initial")]}]
    for i in range(steps):
        history.append(_assistant_tool_use(i))
        history.append(_tool_result_with_image(i))
    return history


def _image_datas(history) -> list:
    """Every image block's data tag, in message order (incl. tool_result inners)."""
    found = []
    for msg in history:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "image":
                found.append(block["source"]["data"])
            elif block.get("type") == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    for item in inner:
                        if item.get("type") == "image":
                            found.append(item["source"]["data"])
    return found


def test_budget_keeps_at_most_k_images():
    out = budget_screenshots_in_history(_synthetic_history(150), keep_images=3)
    assert len(_image_datas(out)) == 3


def test_budget_keeps_the_most_recent_images():
    out = budget_screenshots_in_history(_synthetic_history(150), keep_images=3)
    assert _image_datas(out) == ["PNGstep147", "PNGstep148", "PNGstep149"]


def test_budget_replaces_older_images_with_text_placeholders():
    out = budget_screenshots_in_history(_synthetic_history(10), keep_images=3)
    # The elided initial image becomes a text block in the same position.
    first_content = out[0]["content"]
    assert first_content[0] == {"type": "text", "text": "do the task"}
    assert first_content[1]["type"] == "text"
    assert "elided" in first_content[1]["text"] or "omitted" in first_content[1]["text"]


def test_budget_preserves_tool_pairing_and_message_count():
    h = _synthetic_history(20)
    out = budget_screenshots_in_history(h, keep_images=3)
    assert len(out) == len(h)
    # Every tool_use id still has its tool_result partner in the same position.
    for i, msg in enumerate(h):
        for block in msg.get("content", []):
            if block.get("type") == "tool_result":
                out_block = out[i]["content"][0]
                assert out_block["type"] == "tool_result"
                assert out_block["tool_use_id"] == block["tool_use_id"]


def test_budget_does_not_mutate_the_input():
    h = _synthetic_history(6)
    snapshot = copy.deepcopy(h)
    budget_screenshots_in_history(h, keep_images=2)
    assert h == snapshot


def test_budget_is_idempotent():
    once = budget_screenshots_in_history(_synthetic_history(30), keep_images=3)
    twice = budget_screenshots_in_history(once, keep_images=3)
    assert once == twice


def test_budget_noop_when_under_the_cap():
    h = _synthetic_history(2)  # 3 images total
    assert budget_screenshots_in_history(h, keep_images=3) == h


def test_driver_send_loop_applies_the_budget():
    """The fast tripwire: the send loop must reference the budget function.
    (Weak on its own — mutation testing showed it survives a disabled CALL —
    hence the behavioral request-level test below.)"""
    from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
    src = inspect.getsource(run_anthropic_cu_loop)
    assert "budget_screenshots_in_history" in src


@pytest.mark.asyncio
async def test_outgoing_requests_carry_bounded_images(monkeypatch):
    """The behavioral guard: run the REAL driver loop through 8 screenshot
    tool-turns against a scripted API and assert every OUTGOING request body
    stays at <= 3 images. This is the assertion that actually re-fails if the
    per-iteration budget call is dropped (the 413 regression)."""
    import sys
    from Orchestrator.tests.test_cu_thinking_signature import (
        _ScriptedHTTPX, END_TURN, _sse)
    from Orchestrator.browser.session_manager import ComputerUseSession

    monkeypatch.setattr(ComputerUseSession, "capture_screenshot_bytes",
                        lambda self: b"\x89PNG" * 40)
    monkeypatch.setattr(
        "Orchestrator.browser.screenshot.save_screenshot_to_uploads",
        lambda png, ident, step: "/ui/uploads/x.png")
    from Orchestrator.routes import chat_routes

    async def _no_save(*a, **k):
        return None
    monkeypatch.setattr(chat_routes, "_cu_save_to_blackbox", _no_save)

    def tool_turn(i):
        return _sse([
            {"type": "content_block_start",
             "content_block": {"type": "tool_use", "id": f"tu_{i}",
                               "name": "computer"}},
            {"type": "content_block_delta",
             "delta": {"type": "input_json_delta",
                       "partial_json": "{\"action\": \"screenshot\"}"}},
            {"type": "content_block_stop"},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"input_tokens": 1, "output_tokens": 1}},
        ])

    api_calls = []
    turns = [tool_turn(i) for i in range(8)] + [END_TURN]
    monkeypatch.setitem(sys.modules, "httpx", _ScriptedHTTPX(api_calls, turns))
    from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
    session = ComputerUseSession("budget-op")
    history = [{"role": "user", "content": [{"type": "text", "text": "task"}]}]
    await run_anthropic_cu_loop(session, history, "sys", [], {},
                                "claude-sonnet-5", "budget-op", "task")

    assert len(api_calls) == 9
    for k, call in enumerate(api_calls):
        n = len(_image_datas(call["payload"]["messages"]))
        assert n <= 3, f"request {k} carried {n} images (budget not applied)"
    # The budget must actually be DOING work by the end (cap reached).
    assert len(_image_datas(api_calls[-1]["payload"]["messages"])) == 3
