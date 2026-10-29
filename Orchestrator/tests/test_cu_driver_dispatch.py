"""The Anthropic Computer Use loop's BlackBox tools go through dispatch_tool
(audit rank 74: X05-architecture-11, O02-chat-routes-A-10, O03-chat-routes-B-07).

run_anthropic_cu_loop is the tool loop /chat/stream runs for provider
computer-use with a Claude model, and the one the headless runner behind
use_computer, /browser/run and the scheduler runs. When the chat loops moved to
the shared dispatcher it kept its own copy of the old tool table, so none of
the dispatcher's fixes reached it:

  * a stringified k ("3") raised TypeError in min(k, 5) and the run's outer
    except ended the whole Computer Use run after one API call; the schema's
    "limit" was ignored and a long snapshot was cut to 3,000 characters
    (against the deliver-whole rule every other surface follows);
  * extend_video sat in an executor-only tuple, so a model-started extension
    got no video_task placeholder; image_task carried no predicted_url;
  * the executor was built without the request's origin device, and an empty
    result reached the model as "".

The computer, bash and editor tools stay the driver's own; everything else is
dispatch_tool with the "cu_anthropic" profile. Hermetic: scripted httpx, a
fake executor, and the dispatcher's helpers stubbed on chat_routes.
"""
import json
import sys

import pytest

import Orchestrator.tools as tools_pkg
from Orchestrator.browser.session_manager import ComputerUseSession
from Orchestrator.routes import chat_routes as cr
from Orchestrator.tests.test_cu_thinking_signature import END_TURN, _ScriptedHTTPX, _sse
from Orchestrator.toolvault.context import ToolResult

OP = "cu-dispatch-op"


def _tool_turn(*calls):
    """One assistant turn that calls each (name, args) in order."""
    events = []
    for i, (name, args) in enumerate(calls):
        events += [
            {"type": "content_block_start",
             "content_block": {"type": "tool_use", "id": f"tu_{i}", "name": name}},
            {"type": "content_block_delta",
             "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}},
            {"type": "content_block_stop"},
        ]
    events.append({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                   "usage": {"input_tokens": 1, "output_tokens": 1}})
    return _sse(events)


class _Task:
    def __init__(self, task_id):
        self.task_id = task_id


@pytest.fixture
def env(monkeypatch):
    seen = {"executor": [], "tasks": [], "retrieve": []}

    class _Executor:
        def __init__(self, operator="system", origin_device_id=None, **kw):
            self.operator, self.origin = operator, origin_device_id

        async def execute(self, name, args):
            seen["executor"].append((name, dict(args), self.operator, self.origin))
            if name == "list_devices":
                return ToolResult(success=True, result="")
            data = {"task_id": f"exec-{name}"} if name == "extend_video" else None
            return ToolResult(success=True, result=f"{name} ran", data=data)

    def create_task(task_type, operator=None, **kw):
        seen["tasks"].append({"type": task_type, "operator": operator, **kw})
        return _Task(f"task-{len(seen['tasks'])}")

    def hybrid_retrieve(vol, query, k=3, operator=""):
        seen["retrieve"].append({"query": query, "k": k, "operator": operator})
        return [seen.get("snapshot", "SNAP one body")]

    async def _no_save(*a, **k):
        return None

    monkeypatch.setattr(tools_pkg, "BlackBoxToolExecutor", _Executor)
    monkeypatch.setattr(cr, "create_task", create_task)
    monkeypatch.setattr(cr, "generate_prompt_slug", lambda prompt: "slug")
    monkeypatch.setattr(cr, "hybrid_retrieve", hybrid_retrieve)
    monkeypatch.setattr(cr, "_cu_save_to_blackbox", _no_save)
    monkeypatch.setattr(ComputerUseSession, "capture_screenshot_bytes",
                        lambda self: b"\x89PNG-fake" * 30)
    monkeypatch.setattr("Orchestrator.browser.screenshot.save_screenshot_to_uploads",
                        lambda png, ident, step: "/ui/uploads/x.png")
    return seen


async def _run(monkeypatch, *calls, origin=None):
    """Run the CU loop over one tool turn; (api_calls, session, events)."""
    api_calls = []
    monkeypatch.setitem(sys.modules, "httpx", _ScriptedHTTPX(api_calls, [_tool_turn(*calls), END_TURN]))
    from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
    session = ComputerUseSession(OP)
    history = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
    cr._set_origin_device_id(origin)
    try:
        await run_anthropic_cu_loop(session, history, "sys", [], {}, "claude-sonnet-5", OP, "go")
    finally:
        cr._set_origin_device_id(None)
    events = []
    while not session.event_queue.empty():
        event = session.event_queue.get_nowait()
        if isinstance(event, dict):
            events.append(event)
    return api_calls, session, events


def _results(api_calls):
    """The tool_result blocks the loop sent back after the tool turn."""
    msg = api_calls[1]["payload"]["messages"][-1]
    assert msg["role"] == "user"
    return msg["content"]


# --------------------------------------------------------------------------- #
# O03-chat-routes-B-07: a stringified number never ends the run                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("args, k", [({"query": "ember", "k": "3"}, 3),
                                     ({"query": "ember", "limit": "4"}, 4),
                                     ({"query": "ember", "limit": 50}, 5)])
async def test_a_stringified_k_or_the_schemas_limit_searches_and_the_run_finishes(env, monkeypatch, args, k):
    api_calls, session, _ = await _run(monkeypatch, ("search_snapshots", args))
    assert session.status == "complete", session.error_message
    assert env["retrieve"] == [{"query": "ember", "k": k, "operator": OP}]
    assert _results(api_calls)[0]["content"].startswith("Found 1 relevant snapshot(s) for: ember")


@pytest.mark.asyncio
async def test_a_long_snapshot_is_delivered_whole(env, monkeypatch):
    env["snapshot"] = "SNAP long body " * 1000          # 15,000 characters
    api_calls, _, _ = await _run(monkeypatch, ("search_snapshots", {"query": "ember"}))
    content = _results(api_calls)[0]["content"]
    assert env["snapshot"] in content and "[truncated]" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"limit": "5"}, {"limit": 5}])
async def test_a_stringified_media_limit_lists_the_media(env, monkeypatch, args):
    listed = []
    monkeypatch.setattr(cr, "execute_list_media",
                        lambda media_type=None, limit=20: listed.append(limit) or {"count": 0})
    _, session, _ = await _run(monkeypatch, ("list_media", args))
    assert session.status == "complete", session.error_message
    assert listed == [5]


# --------------------------------------------------------------------------- #
# O02-chat-routes-A-10: the catch-all's placeholders reach Computer Use        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_extend_video_gets_its_placeholder(env, monkeypatch):
    _, _, events = await _run(monkeypatch, ("extend_video", {"video_url": "/ui/uploads/a.mp4",
                                                              "prompt": "longer"}))
    assert {"type": "video_task", "data": {"task_id": "exec-extend_video", "prompt": "longer"}} in events


@pytest.mark.asyncio
async def test_the_image_task_carries_its_predicted_url_and_the_session_operator(env, monkeypatch):
    api_calls, _, events = await _run(monkeypatch, ("gemini_image", {"prompt": "a fox", "numberOfImages": "1"}))
    image_task = next(e for e in events if e["type"] == "image_task")
    assert image_task["data"]["predicted_url"] == "/ui/uploads/slug_task-1.png"
    assert env["tasks"][0]["operator"] == OP
    assert "/ui/uploads/slug_task-1.png" in _results(api_calls)[0]["content"]


@pytest.mark.asyncio
async def test_an_empty_result_reaches_the_model_as_a_note(env, monkeypatch):
    api_calls, _, _ = await _run(monkeypatch, ("list_devices", {}))
    assert _results(api_calls) == [{"type": "tool_result", "tool_use_id": "tu_0",
                                    "content": "Tool 'list_devices' executed successfully (no output)."}]


@pytest.mark.asyncio
async def test_the_executor_learns_the_requests_origin_device(env, monkeypatch):
    await _run(monkeypatch, ("get_current_time", {}), origin="100.64.0.9")
    assert env["executor"] == [("get_current_time", {}, OP, "100.64.0.9")]


@pytest.mark.asyncio
async def test_the_call_tools_keep_every_argument_the_model_sent(env, monkeypatch):
    """Computer Use always handed the call tools the model's own arguments; the
    dispatcher's catch-all keeps that (from_number is not dropped)."""
    args = {"phone_number": "+10000000000", "message": "hi", "from_number": "+10000000001"}
    await _run(monkeypatch, ("send_sms", args))
    assert env["executor"] == [("send_sms", args, OP, None)]


@pytest.mark.asyncio
async def test_every_tool_call_in_the_turn_is_answered_in_order(env, monkeypatch):
    api_calls, session, _ = await _run(monkeypatch, ("web_fetch", {"url": "ftp://nope"}),
                                       ("roll_dice", {"sides": 6}))
    assert session.status == "complete"
    results = _results(api_calls)
    assert [r["tool_use_id"] for r in results] == ["tu_0", "tu_1"]
    assert all(set(r) == {"type", "tool_use_id", "content"} for r in results)
    assert results[1]["content"] == "roll_dice ran"


def test_the_driver_keeps_no_tool_table_of_its_own():
    """Only the computer, bash and editor tools are the driver's; the BlackBox
    tools are the shared dispatcher's (a second copy is what drifted)."""
    import ast
    import inspect
    from Orchestrator.browser import driver_anthropic

    tree = ast.parse(inspect.getsource(driver_anthropic.run_anthropic_cu_loop))
    compared = {node.comparators[0].value for node in ast.walk(tree)
                if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == "tool_name" and isinstance(node.comparators[0], ast.Constant)}
    assert compared == {"computer", "bash"}
    called = {getattr(node.func, "id", getattr(node.func, "attr", None))
              for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "dispatch_tool" in called
    assert not called & {"create_task", "hybrid_retrieve", "perform_web_fetch", "BlackBoxToolExecutor"}
