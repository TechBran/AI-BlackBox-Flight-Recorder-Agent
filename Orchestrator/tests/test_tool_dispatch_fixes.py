"""Fixes that land once, in the shared dispatch_tool (audit rank 74).

O02-chat-routes-A-10: the "executor-only" branches (get_task_status ...
extend_video, list_devices, gmail_*) duplicated the catch-all but skipped what
only the catch-all did: a model-started extend_video got no video placeholder
(no {kind}_task event in the Anthropic / Gemini streams, no media task for the
non-stream worker), an empty result reached the model as "" instead of the
"(no output)" note, and call_gemini answered get_current_time with its own
timezone-less local clock instead of the ToolVault tool every other provider
runs.

O03-chat-routes-B-07: the native branches read the model's arguments raw, so a
stringified or float number (numberOfImages="2", sampleCount="2", k / limit
"4") raised TypeError and the stream's outer except killed the whole turn; the
native snapshot search read a "k" the schema does not have and ignored the
schema's "limit"; the copies had drifted (image_task carried predicted_url on
three streams only, use_computer emitted browser_task on four).

Hermetic: the helpers are recorders and the executor is a fake.
"""
import asyncio

import pytest

import Orchestrator.tools as tools_pkg
from Orchestrator.providers import tool_dispatch as td
from Orchestrator.toolvault.context import ToolResult

STREAMS = sorted(n for n, p in td.PROFILES.items() if p.stream)
NATIVE = sorted(n for n, p in td.PROFILES.items() if p.native)


class _Task:
    def __init__(self, task_id):
        self.task_id = task_id


@pytest.fixture
def rec(monkeypatch):
    seen = {"executor": [], "tasks": [], "helpers": []}

    class _Executor:
        def __init__(self, operator="system", origin_device_id=None, **kw):
            pass

        async def execute(self, name, args):
            seen["executor"].append((name, dict(args)))
            if name == "list_devices":
                return ToolResult(success=True, result="")
            data = {"task_id": f"exec-{name}"} if name in ("extend_video", "use_computer") else None
            return ToolResult(success=True, result=f"{name} ran", data=data)

    monkeypatch.setattr(tools_pkg, "BlackBoxToolExecutor", _Executor)

    def create_task(task_type, operator=None, **kw):
        seen["tasks"].append(kw)
        return _Task(f"task-{len(seen['tasks'])}")

    def hybrid(vol, query, k=3, operator=""):
        seen["helpers"].append(("hybrid_retrieve", k))
        return []

    def list_media(media_type=None, limit=20):
        seen["helpers"].append(("list_media", limit))
        return {"count": 0}

    seen["services"] = td.ToolServices(
        create_task=create_task, generate_prompt_slug=lambda p: "slug",
        perform_web_fetch=None, hybrid_retrieve=hybrid, execute_get_media=None,
        execute_list_media=list_media, execute_search_media=None,
        resolve_attachment=lambda url, exts: None)
    return seen


def _dispatch(rec, loop, name, args):
    ctx = td.ToolContext(operator="op-fix", origin_device_id=None,
                         profile=td.PROFILES[loop], services=rec["services"])
    return asyncio.run(td.dispatch_tool(name, args, ctx))


# --------------------------------------------------------------------------- #
# O02-chat-routes-A-10: every loop's executor tools reach the catch-all         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("loop", STREAMS)
def test_every_stream_shows_a_placeholder_for_extend_video(rec, loop):
    out = _dispatch(rec, loop, "extend_video", {"video_url": "/ui/uploads/a.mp4", "prompt": "longer"})
    assert {"type": "video_task", "data": {"task_id": "exec-extend_video", "prompt": "longer"}} in out.events


@pytest.mark.parametrize("loop", ["call_anthropic", "call_gemini", "call_xai", "call_custom"])
def test_every_media_reporting_call_reports_extend_video(rec, loop):
    out = _dispatch(rec, loop, "extend_video", {"video_url": "/ui/uploads/a.mp4", "prompt": "longer"})
    assert out.media_task == ("exec-extend_video", "video", "longer")


@pytest.mark.parametrize("loop", ["stream_anthropic", "stream_gemini", "call_anthropic", "call_gemini"])
def test_an_empty_executor_result_reaches_the_model_as_a_note(rec, loop):
    out = _dispatch(rec, loop, "list_devices", {})
    assert out.result == "Tool 'list_devices' executed successfully (no output)."


def test_call_gemini_runs_the_toolvault_clock(rec):
    out = _dispatch(rec, "call_gemini", "get_current_time", {})
    assert rec["executor"] == [("get_current_time", {})]
    assert out.result == "get_current_time ran"


# --------------------------------------------------------------------------- #
# O03-chat-routes-B-07: native arguments are coerced, never a dead turn         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("loop", NATIVE)
@pytest.mark.parametrize("raw", ["2", 2.0, " 2 "])
def test_a_stringified_or_float_image_count_still_creates_the_images(rec, loop, raw):
    out = _dispatch(rec, loop, "gemini_image", {"prompt": "a fox", "numberOfImages": raw})
    assert rec["tasks"][0]["result_data"]["options"]["numberOfImages"] == 2
    assert "Creating 2 images" in out.result
    assert "slug_task-1_2.png" in out.result


@pytest.mark.parametrize("loop", NATIVE)
def test_an_out_of_range_image_count_is_clamped(rec, loop):
    _dispatch(rec, loop, "gemini_image", {"prompt": "a fox", "numberOfImages": 0})
    _dispatch(rec, loop, "openai_image", {"prompt": "a fox", "numberOfImages": "nine"})
    assert [t["result_data"]["options"]["numberOfImages"] for t in rec["tasks"]] == [1, 1]


@pytest.mark.parametrize("loop", NATIVE)
def test_a_stringified_sample_count_and_duration_are_numbers(rec, loop):
    music = _dispatch(rec, loop, "lyria_music", {"prompt": "piano", "sampleCount": "2"})
    _dispatch(rec, loop, "generate_video", {"prompt": "waves", "duration": "6"})
    assert rec["tasks"][0]["result_data"]["sample_count"] == 2
    assert "(2 variations)" in music.result
    assert rec["tasks"][1]["result_data"]["options"]["duration"] == 6


@pytest.mark.parametrize("loop", NATIVE)
def test_snapshot_search_reads_the_schemas_limit(rec, loop):
    _dispatch(rec, loop, "search_snapshots", {"query": "ember", "limit": "4"})
    _dispatch(rec, loop, "search_snapshots", {"query": "ember", "k": 2.0})
    _dispatch(rec, loop, "search_snapshots", {"query": "ember", "limit": 50})
    assert [h for h in rec["helpers"] if h[0] == "hybrid_retrieve"] == [
        ("hybrid_retrieve", 4), ("hybrid_retrieve", 2), ("hybrid_retrieve", 5)]


@pytest.mark.parametrize("loop", NATIVE)
def test_a_stringified_media_limit_is_a_number(rec, loop):
    _dispatch(rec, loop, "list_media", {"limit": "5"})
    assert rec["helpers"] == [("list_media", 5)]


# --------------------------------------------------------------------------- #
# O03-chat-routes-B-07: the stream copies no longer drift                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("loop", STREAMS)
def test_every_stream_image_task_carries_its_predicted_url(rec, loop):
    out = _dispatch(rec, loop, "gemini_image", {"prompt": "a fox"})
    image_task = next(e for e in out.events if e["type"] == "image_task")
    assert image_task["data"]["predicted_url"] == "/ui/uploads/slug_task-1.png"


@pytest.mark.parametrize("loop", STREAMS)
def test_every_stream_emits_browser_task_for_use_computer(rec, loop):
    out = _dispatch(rec, loop, "use_computer", {"prompt": "open the docs"})
    assert {"type": "browser_task",
            "data": {"task_id": "exec-use_computer", "prompt": "open the docs"}} in out.events


# --------------------------------------------------------------------------- #
# The non-stream entry point                                                  #
# --------------------------------------------------------------------------- #

def test_the_blocking_dispatch_also_works_inside_a_running_loop(rec, monkeypatch):
    """call_* used to asyncio.run each executor tool, which raises inside a
    running loop; dispatch_tool_blocking runs it on a helper thread instead,
    keeping the caller's context (the origin device, the tool-outcome log)."""
    import contextvars

    marker = contextvars.ContextVar("marker", default="unset")
    seen = []

    class _Executor:
        def __init__(self, *a, **k):
            pass

        async def execute(self, name, args):
            seen.append(marker.get())
            return ToolResult(success=True, result="ran")

    monkeypatch.setattr(tools_pkg, "BlackBoxToolExecutor", _Executor)
    ctx = td.ToolContext(operator="op-fix", origin_device_id=None,
                         profile=td.PROFILES["call_anthropic"], services=rec["services"])

    async def caller():
        marker.set("from-the-caller")
        return td.dispatch_tool_blocking("roll_dice", {"sides": 6}, ctx)

    assert asyncio.run(caller()).result == "ran"
    assert seen == ["from-the-caller"]
