"""Gemini CU context budget (2026-07-23 follow-up to the Anthropic 413 fix).

Field failure (Brandon's chess game on MS02, run 18:11): fossil retrieval
injected 311,810 chars into system_instruction (~76K tokens before the model
saw the board) and the loop accumulated a full-res screenshot per step in
`contents` — at step 22 (43 content blocks) Gemini 400'd: "The input token
count exceeds the maximum number of tokens allowed 131072", killing the game
mid-run. The gemini twin of the Anthropic per-request caps.

Contracts under test: retrieve_for_agent honors a max_chars budget, and the
gemini loop budgets screenshots in `contents` every iteration (keep last K).
"""
import pytest

from google.genai import types

from Orchestrator import agent_context
from Orchestrator.gemini_cu.agent_loop import _budget_contents_images


def _img_part(tag: bytes) -> types.Part:
    return types.Part.from_bytes(data=b"PNG" + tag, mime_type="image/png")


def _count_images(contents) -> list:
    found = []
    for c in contents:
        for p in (c.parts or []):
            inline = getattr(p, "inline_data", None)
            if inline is not None and (inline.mime_type or "").startswith("image/"):
                found.append(bytes(inline.data))
    return found


def _synthetic_contents(steps: int = 20) -> list:
    contents = [types.Content(role="user", parts=[
        types.Part.from_text(text="play chess"), _img_part(b"initial")])]
    for i in range(steps):
        contents.append(types.Content(role="model", parts=[
            types.Part.from_function_call(name="click_at", args={"x": i, "y": i})]))
        contents.append(types.Content(role="user", parts=[
            types.Part.from_function_response(
                name="click_at", response={"url": "desktop://blackbox"}),
            _img_part(f"step{i}".encode())]))
    return contents


def test_budget_keeps_last_k_images():
    contents = _synthetic_contents(20)
    _budget_contents_images(contents, keep_images=3)
    imgs = _count_images(contents)
    assert len(imgs) == 3
    assert imgs == [b"PNGstep17", b"PNGstep18", b"PNGstep19"]


def test_budget_preserves_function_responses_and_count():
    contents = _synthetic_contents(10)
    n_before = len(contents)
    _budget_contents_images(contents, keep_images=3)
    assert len(contents) == n_before
    fn_responses = [p for c in contents for p in (c.parts or [])
                    if getattr(p, "function_response", None) is not None]
    assert len(fn_responses) == 10  # every tool-result pairing survives


def test_budget_replaces_elided_images_with_text():
    contents = _synthetic_contents(5)
    _budget_contents_images(contents, keep_images=2)
    first_texts = [p.text for p in contents[0].parts
                   if getattr(p, "text", None)]
    assert any("elided" in t for t in first_texts)


def test_budget_noop_under_cap():
    contents = _synthetic_contents(2)  # 3 images total
    _budget_contents_images(contents, keep_images=3)
    assert len(_count_images(contents)) == 3


def test_budget_is_wired_into_the_send_loop():
    import inspect
    from Orchestrator.gemini_cu.agent_loop import run_gemini_cu_loop
    src = inspect.getsource(run_gemini_cu_loop)
    assert "_budget_contents_images" in src


def test_retrieve_for_agent_honors_max_chars(monkeypatch):
    monkeypatch.setattr(agent_context, "build_fossil_context",
                        lambda **kw: ("F" * 300_000, None))
    text, prov = agent_context.retrieve_for_agent(
        "q", "op", "[T]", max_chars=20_000)
    assert len(text) <= 20_000 + 100  # budget + truncation marker slack
    assert "truncated" in text[-100:]


def test_retrieve_for_agent_unbounded_by_default(monkeypatch):
    monkeypatch.setattr(agent_context, "build_fossil_context",
                        lambda **kw: ("F" * 50_000, None))
    text, prov = agent_context.retrieve_for_agent("q", "op", "[T]")
    assert len(text) == 50_000  # CLI agents keep today's behavior


def test_gemini_cu_loop_passes_a_fossil_budget():
    import inspect
    from Orchestrator.gemini_cu import agent_loop
    src = inspect.getsource(agent_loop.run_gemini_cu_loop)
    assert "max_chars" in src
