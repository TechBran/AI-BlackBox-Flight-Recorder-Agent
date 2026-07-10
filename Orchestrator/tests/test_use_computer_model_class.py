"""M1-T2: use_computer schema + executor — model-CLASS parameter.

The `use_computer` tool routes to one of three computer-use backends
(anthropic / google / openai). Model ids are provider facts that churn every
release, so the SCHEMA carries a stable model CLASS name and the concrete id is
resolved at task-creation time against the live CU catalog
(Orchestrator.browser.dispatch.resolve_model_class).

Verified here:
  * schema: an OPTIONAL plain-string `model` param (no enum / nested object /
    x-source / min-max), a class-naming param description, and a
    provider-agnostic TOOL description (no vendor / version).
  * executor: class/id resolves to a concrete id threaded into the task's
    result_data["model"]; device_id + url still threaded; an unresolvable class
    yields a STRUCTURED, retryable failure the calling LLM can act on; and the
    (possibly cold-cache network) resolve never blocks the event loop.
"""
import asyncio
import json

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext, ToolResult


# A representative live-shaped CU catalog: anthropic opus + sonnet and one
# gemini CU model — deliberately NO fable and NO gpt/openai member, so
# "available classes" is a non-trivial subset ({opus, sonnet, gemini}).
_CATALOG = [
    {"id": "claude-opus-4-8", "backend": "anthropic"},
    {"id": "claude-sonnet-4-6", "backend": "anthropic"},
    {"id": "gemini-2.5-computer-use-preview-10-2025", "backend": "google"},
]

_ALLOWED_PARAM_KEYS = {"type", "description"}  # flat shape voice surfaces accept


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Pick up on-disk schema/executor edits around every test."""
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


@pytest.fixture
def patched(monkeypatch):
    """Serve a fixed CU catalog and capture the task create_task() would build."""
    captured = {}

    def fake_get_available_models(kind):
        assert kind == "computer-use"
        return {"models": [dict(m) for m in _CATALOG], "default_id": "claude-opus-4-8"}

    class _FakeTask:
        task_id = "task-fake-001"

    def fake_create_task(task_type, operator=None, prompt=None, result_data=None, **kw):
        captured["task_type"] = task_type
        captured["operator"] = operator
        captured["prompt"] = prompt
        captured["result_data"] = result_data
        return _FakeTask()

    from Orchestrator.routes import admin_routes
    from Orchestrator import tasks as tasks_mod
    monkeypatch.setattr(admin_routes, "get_available_models", fake_get_available_models)
    monkeypatch.setattr(tasks_mod, "create_task", fake_create_task)
    return captured


def _run(params, ctx=None):
    ex = registry.get_executor("use_computer")
    assert ex is not None, "use_computer executor failed to load"
    return asyncio.run(ex(params, ctx or ToolContext(operator="system")))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _model_param():
    tool = registry.get_tool("use_computer")
    assert tool is not None
    return tool, tool["parameters"]["properties"].get("model")


def test_model_param_is_optional_plain_string():
    tool, m = _model_param()
    assert m is not None, "use_computer schema must expose a `model` parameter"
    assert m.get("type") == "string"
    # OPTIONAL — omitting it selects the configured default.
    assert "model" not in tool["parameters"].get("required", [])
    # Plain string ONLY — the MCP lean venv cannot resolve an x-source enum, and
    # the voice surfaces need a flat shape (no enum / nested object / min-max).
    assert set(m).issubset(_ALLOWED_PARAM_KEYS), f"unexpected keys on model param: {set(m)}"


def test_model_param_description_names_the_closed_class_set():
    _, m = _model_param()
    desc = m["description"].lower()
    for cls in ("opus", "sonnet", "fable", "gemini", "gpt"):
        assert cls in desc, f"model param description must name class {cls!r}"
    assert "haiku" not in desc, "haiku has NO computer-use support — must not be offered"
    assert "class" in desc
    assert "default" in desc
    # Must make clear the BACKEND is derived from the class, not chosen directly.
    assert "backend" in desc


def test_tool_description_is_provider_agnostic():
    tool, _ = _model_param()
    desc = tool["description"].lower()
    for banned in ("claude", "opus", "anthropic", "gemini", "gpt",
                   "openai", "sonnet", "fable", "4.6"):
        assert banned not in desc, (
            f"tool description must be provider-agnostic; found {banned!r}")
    # ...but it still describes the capability.
    assert "computer" in desc
    assert "get_task_status" in desc


def test_schema_serializes_flat_for_voice_surfaces():
    """The tool must survive the Gemini/OpenAI-realtime flattening the voice
    surfaces apply (no enum, no nested object, no default/min/max)."""
    from Orchestrator.tools.tool_registry import _strip_for_gemini
    tool = registry.get_tool("use_computer")
    stripped = _strip_for_gemini(tool["parameters"])
    m = stripped["properties"]["model"]
    assert m["type"] == "string"
    assert "enum" not in m and "properties" not in m


# ---------------------------------------------------------------------------
# Executor — resolution + threading
# ---------------------------------------------------------------------------

def test_omitted_model_resolves_to_default_class_concrete_id(patched):
    res = _run({"prompt": "do a thing"})
    assert isinstance(res, ToolResult)
    assert res.success is True
    rd = patched["result_data"]
    # Default class is opus -> newest opus in the catalog.
    assert rd["model"] == "claude-opus-4-8"
    assert rd["device_id"] == "blackbox"


def test_class_alias_resolves_to_concrete_id(patched):
    res = _run({"prompt": "x", "model": "gemini"})
    assert res.success is True
    assert patched["result_data"]["model"] == "gemini-2.5-computer-use-preview-10-2025"


def test_concrete_gate_passing_id_passthrough(patched):
    res = _run({"prompt": "x", "model": "claude-sonnet-4-6"})
    assert res.success is True
    assert patched["result_data"]["model"] == "claude-sonnet-4-6"


def test_url_and_device_id_are_threaded(patched):
    res = _run({"prompt": "x", "url": "https://example.com", "device_id": "laptop"})
    assert res.success is True
    rd = patched["result_data"]
    assert rd["url"] == "https://example.com"
    assert rd["device_id"] == "laptop"
    assert rd["model"] == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# Executor — structured, retryable failure
# ---------------------------------------------------------------------------

def _assert_structured_failure(res, bad_token):
    assert res.success is False
    # STRUCTURED, not a prose blob: the main chat path forwards res.result (the
    # string) to the model, so the machine-actionable payload lives THERE.
    payload = json.loads(res.result)
    assert payload["success"] is False
    assert payload["retryable"] is True
    assert bad_token in payload["reason"]
    assert set(payload["available"]) == {"opus", "sonnet", "gemini"}  # per _CATALOG
    # data mirrors the payload (voice surfaces read rich_result()/data).
    assert res.data == payload


def test_unknown_class_returns_structured_retryable_failure(patched):
    res = _run({"prompt": "x", "model": "banana"})
    _assert_structured_failure(res, "banana")
    # No task was created for an unresolvable class.
    assert "result_data" not in patched


def test_known_class_the_catalog_cannot_serve_lists_alternatives(patched):
    # `gpt` is a valid class, but this catalog has no openai CU member.
    res = _run({"prompt": "x", "model": "gpt"})
    _assert_structured_failure(res, "gpt")
    assert "result_data" not in patched


def test_missing_prompt_short_circuits(patched):
    res = _run({})
    assert res.success is False
    assert "Prompt is required" in res.result


# ---------------------------------------------------------------------------
# Executor — blocking-call hazard (T1 code-review finding)
# ---------------------------------------------------------------------------

def test_resolution_does_not_block_the_event_loop(monkeypatch):
    """resolve_model_class is SYNC and may do a cached network fetch on a cold
    cache. The executor must keep it OFF the event loop. Proof: a concurrent
    heartbeat keeps ticking while a deliberately-slow catalog fetch runs."""
    import time
    from Orchestrator.routes import admin_routes
    from Orchestrator import tasks as tasks_mod

    DELAY = 0.30

    def slow_get_available_models(kind):
        time.sleep(DELAY)  # simulate a cold-cache vendor fetch
        return {"models": [dict(m) for m in _CATALOG]}

    class _T:
        task_id = "t-slow"

    monkeypatch.setattr(admin_routes, "get_available_models", slow_get_available_models)
    monkeypatch.setattr(tasks_mod, "create_task", lambda *a, **k: _T())

    ex = registry.get_executor("use_computer")
    ticks = []

    async def heartbeat():
        while True:
            ticks.append(1)
            await asyncio.sleep(0.01)

    async def main():
        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)  # let the heartbeat start
        res = await ex({"prompt": "x"}, ToolContext(operator="system"))
        hb.cancel()
        return res

    res = asyncio.run(main())
    assert res.success is True
    # If the sync sleep blocked the loop, the 10ms heartbeat could not tick.
    assert len(ticks) >= 5, (
        f"event loop appears blocked during resolve ({len(ticks)} ticks in {DELAY}s)")
