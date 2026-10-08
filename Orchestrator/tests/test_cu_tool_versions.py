"""Per-model Anthropic computer-use tool versions (2026-10-07).

Every CU request hard-coded computer_20251124 + beta computer-use-2025-11-24 +
max_tokens 128000, which 400s on the 5.5 tier ("does not support tool types:
computer_20251124"), on Sonnet/Haiku 4.5 (computer_20250124 only) and on the
64K-output models. Contracts under test:

  * both request builders (stream_computer_use + headless.run_cu_task) send the
    per-model tool entry + beta header (none for the toolset);
  * the driver speaks the toolset protocol (member tool_use blocks, in-order
    batches, stop at the first failure, toolset_name echoed) while the legacy
    computer_* path is byte-for-byte what it was;
  * max_tokens is per model; key `repeat` is honored; Claude's
    scroll_direction/scroll_amount reach the executor;
  * the computer-use -> anthropic non-stream fallback never forwards a
    non-Claude CU model id to Anthropic.
"""
import asyncio
import copy
import io
import json
import math
import sys

import pytest

from Orchestrator.browser import headless
from Orchestrator.browser.config import (
    ANTHROPIC_BETA_HEADER, ANTHROPIC_BETA_HEADER_20250124, COMPUTER_TOOL_TYPE,
    COMPUTER_TOOL_TYPE_20250124, COMPUTER_TOOLSET_TYPE,
)
from Orchestrator.browser.driver_anthropic import (
    TOOLSET_NOT_EXECUTED, build_anthropic_cu_request,
)
from Orchestrator.browser.session_manager import ComputerUseSession
from Orchestrator.routes import chat_routes


TOOLSET_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
V20250124_MODELS = ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"]
V20251124_MODELS = ["claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6",
                    "claude-opus-4-6", "claude-opus-4-5-20251101", "claude-opus-5",
                    "claude-sonnet-5", "claude-fable-5-1"]

VAULT_TOOLS = [
    {"name": "computer", "description": "stray same-named tool",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "search_snapshots", "description": "memory",
     "input_schema": {"type": "object", "properties": {}}},
]


def _expected(model):
    """(tools[0], anthropic-beta or None) the API accepts for this model."""
    if model in TOOLSET_MODELS:
        return {"type": COMPUTER_TOOLSET_TYPE, "configs": {"zoom": {"enabled": False}}}, None
    version, beta = ((COMPUTER_TOOL_TYPE_20250124, ANTHROPIC_BETA_HEADER_20250124)
                     if model in V20250124_MODELS else (COMPUTER_TOOL_TYPE, ANTHROPIC_BETA_HEADER))
    return {"type": version, "name": "computer",
            "display_width_px": 1280, "display_height_px": 720}, beta


def _assert_request(model, tools, headers, api_key="test-key"):
    entry, beta = _expected(model)
    assert tools[0] == entry
    assert tools[1] == {"type": "bash_20250124", "name": "bash"}
    assert tools[2] == {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"}
    assert headers.get("anthropic-beta") == beta
    if beta is None:
        assert "anthropic-beta" not in headers
    assert headers["x-api-key"] == api_key
    assert headers["anthropic-version"] == "2023-06-01"
    names = [t.get("name") for t in tools[1:]]
    assert "search_snapshots" in names                    # other tools survive
    if model in TOOLSET_MODELS:
        assert "computer" not in names                    # toolset owns the name


# ═══════════════════════════════════════════════════════════════════════════
# Request builders
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("model", TOOLSET_MODELS + V20250124_MODELS + V20251124_MODELS)
def test_shared_builder_per_model(model):
    tools, headers = build_anthropic_cu_request(model, "test-key", VAULT_TOOLS)
    _assert_request(model, tools, headers)


def test_shared_builder_does_not_mutate_vault_tools():
    vault = copy.deepcopy(VAULT_TOOLS)
    build_anthropic_cu_request("claude-opus-5-5", "k", vault)
    assert vault == VAULT_TOOLS


def test_legacy_builder_output_unchanged_for_opus_4_8():
    """The pre-fix request for a computer_20251124 model, byte for byte."""
    tools, headers = build_anthropic_cu_request("claude-opus-4-8", "k", [])
    assert tools == [
        {"type": "computer_20251124", "name": "computer",
         "display_width_px": 1280, "display_height_px": 720},
        {"type": "bash_20250124", "name": "bash"},
        {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
    ]
    assert headers == {"x-api-key": "k", "anthropic-version": "2023-06-01",
                       "anthropic-beta": "computer-use-2025-11-24",
                       "content-type": "application/json"}


class _FakeHandle:
    display_num = 100

    def get_env(self):
        return {"DISPLAY": ":100"}

    def touch(self):
        pass


@pytest.fixture
def launch_env(monkeypatch):
    """Stub display/screenshot/context seams shared by both builders."""
    async def _ensure_browser(self, url="about:blank", backend="anthropic"):
        self.display = _FakeHandle()
        return True

    async def _instant(_s):
        return None

    monkeypatch.setattr(ComputerUseSession, "ensure_browser", _ensure_browser)
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)
    monkeypatch.setattr("Orchestrator.browser.screenshot.capture_screenshot_display",
                        lambda n, native=None: b"\x89PNG-fake")
    monkeypatch.setattr("Orchestrator.browser.screenshot.save_screenshot_to_uploads",
                        lambda png, ident, step: "/ui/uploads/x.png")
    monkeypatch.setattr(headless, "save_screenshot_to_uploads",
                        lambda png, ident, step: "/ui/uploads/x.png")
    monkeypatch.setattr(chat_routes, "_get_tools", lambda *a, **k: copy.deepcopy(VAULT_TOOLS))
    monkeypatch.setattr(chat_routes, "build_cu_context", lambda *a, **k: ("", {}))
    monkeypatch.setattr(headless.asyncio, "sleep", _instant)

    captured = {}

    async def fake_driver(session, history, system_prompt, tools, headers,
                          model, operator, user_text):
        captured.update(tools=tools, headers=headers, model=model)
        await session.event_queue.put({"type": "done", "data": {"thinking": "", "content": "ok"}})
        await session.event_queue.put(None)

    return captured, fake_driver


@pytest.mark.asyncio
@pytest.mark.parametrize("model", TOOLSET_MODELS + V20250124_MODELS + ["claude-opus-4-8"])
async def test_stream_computer_use_sends_per_model_tool(launch_env, monkeypatch, model):
    captured, fake_driver = launch_env
    monkeypatch.setattr("Orchestrator.browser.config.ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_routes, "_cu_agent_loop", fake_driver)
    monkeypatch.setattr("Orchestrator.browser.session_manager.get_or_create_session",
                        lambda operator, session_id=None, device_id="blackbox", force_new=False:
                        ComputerUseSession(operator, device_id=device_id))

    events = [e async for e in chat_routes.stream_computer_use(
        [{"role": "user", "content": "click the button"}], model, "tv-op")]

    assert not [e for e in events if e["type"] == "error"], events
    assert captured["model"] == model
    _assert_request(model, captured["tools"], captured["headers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("model", TOOLSET_MODELS + V20250124_MODELS + ["claude-opus-4-8"])
async def test_headless_runner_sends_per_model_tool(launch_env, monkeypatch, model):
    captured, fake_driver = launch_env
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", fake_driver)
    monkeypatch.setattr(headless, "get_or_create_session",
                        lambda operator, session_id=None, device_id="blackbox":
                        ComputerUseSession(operator, device_id=device_id))

    result = await headless.run_cu_task("tv-task", "tv-op", "click the button", model=model)

    assert result["success"] is True, result
    assert captured["model"] == model
    _assert_request(model, captured["tools"], captured["headers"])


@pytest.mark.asyncio
async def test_headless_default_model_builds_tool_for_that_model(launch_env, monkeypatch):
    """model="" resolves to CU_MODEL_DEFAULT — the tool entry must match IT."""
    captured, fake_driver = launch_env
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(headless, "CU_MODEL_DEFAULT", "claude-opus-5-5")
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", fake_driver)
    monkeypatch.setattr(headless, "get_or_create_session",
                        lambda operator, session_id=None, device_id="blackbox":
                        ComputerUseSession(operator, device_id=device_id))

    result = await headless.run_cu_task("tv-task", "tv-op", "click", model="")

    assert result["success"] is True, result
    assert captured["model"] == "claude-opus-5-5"
    _assert_request("claude-opus-5-5", captured["tools"], captured["headers"])


# ═══════════════════════════════════════════════════════════════════════════
# Driver: scripted SSE turns through the REAL run_anthropic_cu_loop
# ═══════════════════════════════════════════════════════════════════════════

def _png(w=1280, h=720):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def _tool(tid, name, inp=None, toolset=True):
    return {"kind": "tool", "id": tid, "name": name, "input": inp or {}, "toolset": toolset}


def _turn(*blocks, stop="tool_use"):
    events = []
    for b in blocks:
        if b["kind"] == "thinking":
            events += [
                {"type": "content_block_start",
                 "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta",
                 "delta": {"type": "signature_delta", "signature": b["sig"]}},
                {"type": "content_block_stop"},
            ]
        elif b["kind"] == "text":
            events += [
                {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": b["text"]}},
                {"type": "content_block_stop"},
            ]
        else:
            start = {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}
            if b["toolset"]:
                start["toolset_name"] = "computer"
                start["caller"] = {"type": "direct"}
            events += [
                {"type": "content_block_start", "content_block": start},
                {"type": "content_block_delta",
                 "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}},
                {"type": "content_block_stop"},
            ]
    events.append({"type": "message_delta", "delta": {"stop_reason": stop},
                   "usage": {"input_tokens": 1, "output_tokens": 1}})
    return ["data: " + json.dumps(e) for e in events]


END_TURN = _turn({"kind": "text", "text": "DONE"}, stop="end_turn")


class _ScriptedHTTPX:
    """httpx stand-in replaying one scripted SSE body per API call."""

    class TimeoutException(Exception):
        pass

    class ConnectError(Exception):
        pass

    def __init__(self, api_calls, turns):
        self._api_calls = api_calls
        self._turns = turns

    def AsyncClient(self, **kwargs):
        api_calls, turns = self._api_calls, self._turns

        class _Resp:
            status_code = 200

            def __init__(self, lines):
                self._lines = lines

            async def aiter_lines(self):
                for line in self._lines:
                    yield line

            async def aread(self):
                return b""

        class _Ctx:
            def __init__(self, lines):
                self._lines = lines

            async def __aenter__(self):
                return _Resp(self._lines)

            async def __aexit__(self, *exc):
                return False

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url, headers=None, json=None):
                api_calls.append({"payload": copy.deepcopy(json), "headers": dict(headers or {})})
                return _Ctx(turns[min(len(api_calls) - 1, len(turns) - 1)])

        return _Client()


class _RecordingActions:
    """Fake executor: records calls; `fail` names actions that report failure."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def execute(self, action, **params):
        self.calls.append((action, params))
        if action in self.fail:
            return {"success": False, "message": f"Action '{action}' failed: boom"}
        return {"success": True, "message": f"did {action}"}


@pytest.fixture
def driver_env(monkeypatch):
    shots = {"n": 0, "png": _png(), "raise": False}

    def _capture(self):
        shots["n"] += 1
        if shots["raise"]:
            raise RuntimeError("display gone")
        return shots["png"]

    async def _instant(_s):
        return None

    async def _no_save(*a, **k):
        return None

    monkeypatch.setattr(ComputerUseSession, "capture_screenshot_bytes", _capture)
    monkeypatch.setattr("Orchestrator.browser.screenshot.save_screenshot_to_uploads",
                        lambda png, ident, step: "/ui/uploads/x.png")
    monkeypatch.setattr(chat_routes, "_cu_save_to_blackbox", _no_save)
    monkeypatch.setattr(asyncio, "sleep", _instant)
    return shots


async def _run(monkeypatch, turns, model="claude-opus-5-5", actions=None):
    api_calls = []
    monkeypatch.setitem(sys.modules, "httpx", _ScriptedHTTPX(api_calls, turns))
    from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
    session = ComputerUseSession("tv-op")
    session.actions = actions or _RecordingActions()
    history = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
    tools, headers = build_anthropic_cu_request(model, "k", [])
    await run_anthropic_cu_loop(session, history, "sys", tools, headers, model, "tv-op", "go")
    return api_calls, session


def _last_user_results(payload):
    msg = payload["messages"][-1]
    assert msg["role"] == "user"
    return msg["content"]


def _assistant(payload, index=-2):
    msg = payload["messages"][index]
    assert msg["role"] == "assistant"
    return msg["content"]


def _is_image_result(r):
    return isinstance(r["content"], list) and r["content"][0]["type"] == "image"


@pytest.mark.asyncio
async def test_member_block_translates_to_legacy_action(driver_env, monkeypatch):
    actions = _RecordingActions()
    api_calls, session = await _run(monkeypatch, [
        _turn(_tool("tu_1", "left_click", {"coordinate": [640, 360]})), END_TURN],
        actions=actions)

    assert actions.calls == [("left_click", {"coordinate": [640, 360]})]
    results = _last_user_results(api_calls[1]["payload"])
    assert len(results) == 1
    r = results[0]
    assert r["tool_use_id"] == "tu_1" and r["toolset_name"] == "computer"
    assert "is_error" not in r
    assert _is_image_result(r)                          # lone member carries the screenshot
    # The assistant replay keeps the toolset marker on the tool_use block.
    tu = _assistant(api_calls[1]["payload"])[0]
    assert tu == {"type": "tool_use", "id": "tu_1", "name": "left_click",
                  "input": {"coordinate": [640, 360]}, "toolset_name": "computer"}
    assert session.final_response == "DONE"


@pytest.mark.asyncio
async def test_batch_runs_in_order_and_screenshot_rides_the_last_result(driver_env, monkeypatch):
    actions = _RecordingActions()
    api_calls, _ = await _run(monkeypatch, [
        _turn(_tool("a", "left_click", {"coordinate": [10, 20]}),
              _tool("b", "type", {"text": "hello"}),
              _tool("c", "key", {"text": "Return", "repeat": 2})),
        END_TURN], actions=actions)

    assert [c[0] for c in actions.calls] == ["left_click", "type", "key"]
    assert actions.calls[2][1] == {"text": "Return", "repeat": 2}
    results = _last_user_results(api_calls[1]["payload"])
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
    assert all(r["toolset_name"] == "computer" for r in results)
    assert not any(r.get("is_error") for r in results)
    assert results[0]["content"] == [{"type": "text", "text": "did left_click"}]
    assert results[1]["content"] == [{"type": "text", "text": "did type"}]
    assert _is_image_result(results[2])
    assert driver_env["n"] == 1                          # one capture for the batch


@pytest.mark.asyncio
async def test_batch_stops_at_first_failure(driver_env, monkeypatch):
    actions = _RecordingActions(fail={"type"})
    api_calls, _ = await _run(monkeypatch, [
        _turn(_tool("a", "left_click", {"coordinate": [10, 20]}),
              _tool("b", "type", {"text": "hello"}),
              _tool("c", "key", {"text": "Return"}),
              _tool("d", "screenshot")),
        END_TURN], actions=actions)

    assert [c[0] for c in actions.calls] == ["left_click", "type"]   # c, d never ran
    results = _last_user_results(api_calls[1]["payload"])
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c", "d"]
    assert all(r["toolset_name"] == "computer" for r in results)
    assert "is_error" not in results[0]
    assert results[1]["is_error"] is True
    assert results[1]["content"].startswith("Error: ")
    for r in results[2:]:
        assert r == {"type": "tool_result", "tool_use_id": r["tool_use_id"],
                     "toolset_name": "computer", "is_error": True,
                     "content": TOOLSET_NOT_EXECUTED}
    assert TOOLSET_NOT_EXECUTED == "Not executed: an earlier computer action in this turn failed."


@pytest.mark.asyncio
async def test_screenshot_failure_also_stops_the_batch(driver_env, monkeypatch):
    driver_env["raise"] = True
    api_calls, _ = await _run(monkeypatch, [
        _turn(_tool("a", "screenshot"), _tool("b", "left_click", {"coordinate": [1, 2]})),
        END_TURN])

    results = _last_user_results(api_calls[1]["payload"])
    assert results[0]["is_error"] is True and results[0]["toolset_name"] == "computer"
    assert results[1]["content"] == TOOLSET_NOT_EXECUTED


@pytest.mark.asyncio
async def test_non_computer_tool_in_same_turn_still_runs(driver_env, monkeypatch):
    """A failed member stops later MEMBERS only; a custom tool in the same turn
    is still executed and every block is answered, in order."""
    actions = _RecordingActions(fail={"left_click"})
    api_calls, _ = await _run(monkeypatch, [
        _turn(_tool("a", "left_click", {"coordinate": [1, 2]}),
              _tool("s", "search_snapshots", {}, toolset=False),
              _tool("b", "screenshot")),
        END_TURN], actions=actions)

    results = _last_user_results(api_calls[1]["payload"])
    assert [r["tool_use_id"] for r in results] == ["a", "s", "b"]
    assert results[0]["is_error"] is True
    assert results[1] == {"type": "tool_result", "tool_use_id": "s",
                          "content": "Error: No search query provided."}
    assert results[2]["content"] == TOOLSET_NOT_EXECUTED


@pytest.mark.asyncio
async def test_signed_thinking_round_trips_unchanged_with_toolset(driver_env, monkeypatch):
    """5.5 models always think (display omitted -> empty text + signature)."""
    api_calls, _ = await _run(monkeypatch, [
        _turn({"kind": "thinking", "sig": "sig-55"}, _tool("a", "screenshot")), END_TURN])

    replay = _assistant(api_calls[1]["payload"])
    assert replay[0] == {"type": "thinking", "thinking": "", "signature": "sig-55"}
    assert replay[1]["toolset_name"] == "computer"


@pytest.mark.asyncio
async def test_legacy_computer_block_path_unchanged(driver_env, monkeypatch):
    """computer_20251124 tool_use: no toolset_name anywhere, every action gets a
    screenshot, and an executor failure is NOT surfaced (pre-fix behavior)."""
    actions = _RecordingActions(fail={"left_click"})
    api_calls, _ = await _run(monkeypatch, [
        _turn(_tool("a", "computer", {"action": "left_click", "coordinate": [5, 6]}, toolset=False),
              _tool("b", "computer", {"action": "type", "text": "x"}, toolset=False)),
        END_TURN], model="claude-opus-4-8", actions=actions)

    assert actions.calls == [("left_click", {"coordinate": [5, 6]}), ("type", {"text": "x"})]
    results = _last_user_results(api_calls[1]["payload"])
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    for r in results:
        assert set(r) == {"type", "tool_use_id", "content"}
        assert _is_image_result(r)
    assert driver_env["n"] == 2
    replay = _assistant(api_calls[1]["payload"])
    assert all("toolset_name" not in b for b in replay)


@pytest.mark.asyncio
async def test_unanswered_tool_use_is_filled_after_display_dies(driver_env, monkeypatch):
    """3 consecutive screenshot failures break the tool loop; any block it
    skipped still gets a result so the next request is not rejected."""
    driver_env["raise"] = True
    blocks = [_tool(t, "computer", {"action": "screenshot"}, toolset=False)
              for t in ("a", "b", "c", "d")]
    api_calls, _ = await _run(monkeypatch, [_turn(*blocks), END_TURN], model="claude-opus-4-8")

    results = _last_user_results(api_calls[1]["payload"])
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c", "d"]
    assert results[3]["is_error"] is True and "toolset_name" not in results[3]


@pytest.mark.asyncio
@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5-5", 128000), ("claude-sonnet-5-5", 128000),
    ("claude-opus-4-8", 128000), ("claude-sonnet-4-6", 128000),
    ("claude-sonnet-4-5-20250929", 64000), ("claude-haiku-4-5-20251001", 64000),
    ("claude-opus-4-5-20251101", 64000),
])
async def test_max_tokens_is_per_model(driver_env, monkeypatch, model, expected):
    api_calls, _ = await _run(monkeypatch, [END_TURN], model=model)
    assert api_calls[0]["payload"]["max_tokens"] == expected
    entry, beta = _expected(model)
    assert api_calls[0]["payload"]["tools"][0] == entry
    assert api_calls[0]["headers"].get("anthropic-beta") == beta


TOOLSET_HISTORY = [
    {"role": "user", "content": [{"type": "text", "text": "click"}]},
    {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "t1", "name": "left_click", "toolset_name": "computer",
         "input": {"coordinate": [1, 2]}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "toolset_name": "computer",
         "content": [{"type": "text", "text": "[Screenshot omitted]"}]}]},
    {"role": "assistant", "content": [{"type": "text", "text": "Clicked."}]},
    {"role": "user", "content": [{"type": "text", "text": "again"}]},
]


def test_toolset_history_rewritten_for_legacy_model():
    """Live 400 on claude-opus-4-8 replaying toolset blocks: "toolset_name
    'computer' on a tool_use block is not the family of a declared toolset
    entry". The rewrite is the legacy shape and leaves the input untouched."""
    from Orchestrator.browser.driver_anthropic import toolset_history_to_legacy
    before = copy.deepcopy(TOOLSET_HISTORY)
    out = toolset_history_to_legacy(TOOLSET_HISTORY)
    assert TOOLSET_HISTORY == before
    assert out[1]["content"] == [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "t1", "name": "computer",
         "input": {"action": "left_click", "coordinate": [1, 2]}}]
    assert out[2]["content"] == [{"type": "tool_result", "tool_use_id": "t1",
                                  "content": [{"type": "text", "text": "[Screenshot omitted]"}]}]
    legacy_only = [TOOLSET_HISTORY[0], TOOLSET_HISTORY[3]]
    assert toolset_history_to_legacy(legacy_only) is legacy_only


@pytest.mark.asyncio
@pytest.mark.parametrize("model,keeps_toolset", [("claude-opus-4-8", False),
                                                 ("claude-sonnet-4-5-20250929", False),
                                                 ("claude-opus-5-5", True)])
async def test_driver_replays_history_in_the_models_protocol(driver_env, monkeypatch,
                                                             model, keeps_toolset):
    api_calls = []
    monkeypatch.setitem(sys.modules, "httpx", _ScriptedHTTPX(api_calls, [END_TURN]))
    from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
    session = ComputerUseSession("tv-op")
    tools, headers = build_anthropic_cu_request(model, "k", [])
    await run_anthropic_cu_loop(session, copy.deepcopy(TOOLSET_HISTORY), "sys", tools, headers,
                                model, "tv-op", "again")
    sent = json.dumps(api_calls[0]["payload"]["messages"])
    assert ('"toolset_name"' in sent) is keeps_toolset


# ═══════════════════════════════════════════════════════════════════════════
# Screenshot sizing (toolset: the API does not downscale)
# ═══════════════════════════════════════════════════════════════════════════

def test_anthropic_screenshot_frame_fits_toolset_limits():
    from Orchestrator.browser.config import (
        CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT)
    from Orchestrator.browser.display import resolution_for_backend
    for w, h in {(CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT), (DISPLAY_WIDTH, DISPLAY_HEIGHT),
                 resolution_for_backend("anthropic")}:
        assert max(w, h) <= 2576
        assert math.ceil(w / 28) * math.ceil(h / 28) <= 4784


# ═══════════════════════════════════════════════════════════════════════════
# Executor: key repeat + scroll aliases
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def executor(monkeypatch):
    from Orchestrator.browser import actions as act
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    ex = act.ActionExecutor(native_mode=False)
    ex.use_ydotool = False
    xdo = []
    monkeypatch.setattr(act, "_run_xdotool", lambda *a, **k: xdo.append(a))
    return ex, xdo


@pytest.mark.parametrize("repeat,presses", [(None, 1), (1, 1), (5, 5), (100, 100),
                                            (500, 100), (0, 1), ("3", 3), ("x", 1)])
def test_key_repeat_is_honored_and_clamped(executor, repeat, presses):
    ex, xdo = executor
    params = {"text": "Down"} if repeat is None else {"text": "Down", "repeat": repeat}
    result = ex.execute("key", **params)
    assert result["success"] is True
    assert xdo == [("key", "--clearmodifiers", "Down")] * presses


def test_claude_scroll_direction_and_amount_reach_xdotool(executor):
    ex, xdo = executor
    ex.execute("scroll", coordinate=[10, 10], scroll_direction="up", scroll_amount=2)
    clicks = [a for a in xdo if a[0] == "click"]
    assert clicks == [("click", "4"), ("click", "4")]


def test_scroll_direction_amount_callers_unchanged(executor):
    ex, xdo = executor
    ex.execute("scroll", direction="left", amount=3)
    assert [a for a in xdo if a[0] == "click"] == [("click", "6")] * 3
    xdo.clear()
    ex.execute("scroll")
    assert [a for a in xdo if a[0] == "click"] == [("click", "5")] * 3


@pytest.mark.asyncio
async def test_remote_key_repeat(monkeypatch):
    from Orchestrator.browser import actions as act
    pressed = []

    class _Dev:
        tailscale_ip, vnc_port, metadata = "100.0.0.1", 5900, {}

    class _Reg:
        def get_device(self, _id):
            return _Dev()

    class _VNC:
        def __init__(self, *a):
            pass

        async def key(self, text):
            pressed.append(text)

    monkeypatch.setattr("Orchestrator.device_registry.get_registry", lambda: _Reg())
    monkeypatch.setattr("Orchestrator.remote_desktop.VNCClient", _VNC)
    result = await act.execute_remote_action("dev-1", "key", text="Tab", repeat=4)
    assert result["success"] is True
    assert pressed == ["Tab"] * 4


# ═══════════════════════════════════════════════════════════════════════════
# tasks.py: computer-use -> anthropic non-stream fallback
# ═══════════════════════════════════════════════════════════════════════════

def _run_chat_task(monkeypatch, tmp_path, model):
    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskDatabase, TaskStatus, TaskType
    from Orchestrator.volume import now_utc_iso
    from Orchestrator.config import ANTHROPIC_MODEL_DEFAULT

    seen = {}

    def fake_call_anthropic(messages, model, operator="x"):
        seen["model"] = model
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, ""

    db = TaskDatabase(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(tasks, "task_db", db)
    monkeypatch.setattr(chat_routes, "call_anthropic", fake_call_anthropic)
    for name in ("get_recent_fossils_for_operator", "keyword_retrieve_for_operator",
                 "semantic_retrieve", "get_recent_checkpoints_for_operator", "hybrid_retrieve"):
        monkeypatch.setattr(tasks, name, lambda *a, **k: [])
    monkeypatch.setattr(tasks, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "perform_mint", lambda *a, **k: {"snap_id": "SNAP-TEST"})
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)

    task = Task(task_id="cu-fallback", task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator="CuFallbackTester",
                result_data={"messages": [{"role": "user", "content": "hi"}],
                             "operator": "CuFallbackTester", "provider": "computer-use",
                             "model": model})
    db.save_task(task)
    tasks.process_chat_task(task)
    return seen.get("model"), ANTHROPIC_MODEL_DEFAULT


@pytest.mark.parametrize("model", ["gemini-2.5-computer-use-preview-10-2025", "gpt-5.5", ""])
def test_cu_fallback_never_forwards_non_claude_model(monkeypatch, tmp_path, model):
    sent, default = _run_chat_task(monkeypatch, tmp_path, model)
    assert sent == default


def test_cu_fallback_keeps_a_claude_model(monkeypatch, tmp_path):
    sent, _ = _run_chat_task(monkeypatch, tmp_path, "claude-sonnet-4-6")
    assert sent == "claude-sonnet-4-6"
