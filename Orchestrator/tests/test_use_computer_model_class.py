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
import re

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


def test_model_param_description_signals_capability_ordering():
    """A model told "use the best/fastest" needs a capability signal; "default"
    alone does not mean "flagship" to a reader (quality-first-default posture)."""
    _, m = _model_param()
    desc = m["description"].lower()
    assert "most capable" in desc


def test_model_param_description_has_no_concrete_model_id():
    """The whole point of the class taxonomy: CLASS names live in the schema,
    volatile provider-fact ids never do. Class names (opus/gpt/gemini) are fine;
    concrete ids (claude-*, gpt-5.x, gemini-2.x, computer-use-preview) are not."""
    _, m = _model_param()
    desc = m["description"].lower()
    for marker in ("claude-", "gpt-", "gemini-", "computer-use-preview"):
        assert marker not in desc, f"param description leaks a concrete id marker {marker!r}"
    assert not re.search(r"\d+\.\d+", desc), "param description leaks a version number"


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
    # _CATALOG serves classes, so a bad class IS retryable with a valid one.
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


def test_no_available_classes_makes_failure_non_retryable(monkeypatch):
    """Total-outage / no-provider state: the catalog yields no CU member, so
    there is nothing to retry WITH. `retryable` must be False so a compliant LLM
    does not spin re-issuing the identical call until an outer cap."""
    from Orchestrator.routes import admin_routes
    monkeypatch.setattr(admin_routes, "get_available_models",
                        lambda kind: {"models": []})
    res = _run({"prompt": "x", "model": "opus"})
    assert res.success is False
    payload = json.loads(res.result)
    assert payload["available"] == []
    assert payload["retryable"] is False
    assert res.data == payload


def test_available_classes_are_sourced_from_dispatch(monkeypatch, patched):
    """The executor's advertised `available` list comes from
    dispatch.available_classes — NOT a local literal. This is the single-source
    guard: if dispatch grew a sixth class, the executor carries it through; if
    someone reintroduced a local tuple, this delegation test fails."""
    from Orchestrator.browser import dispatch
    monkeypatch.setattr(dispatch, "available_classes", lambda cat: ["sentinel-class"])
    res = _run({"prompt": "x", "model": "banana"})
    payload = json.loads(res.result)
    assert payload["available"] == ["sentinel-class"]
    assert payload["retryable"] is True  # bool(non-empty)


def test_missing_prompt_short_circuits(patched):
    res = _run({})
    assert res.success is False
    assert "Prompt is required" in res.result


# ---------------------------------------------------------------------------
# Executor — blocking-call hazard (T1 code-review finding)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,expect_success", [
    (None, True),        # happy path: default class resolves
    ("banana", False),   # error path: unresolvable class
])
def test_single_catalog_fetch_never_blocks_the_event_loop(monkeypatch, model, expect_success):
    """The ONE catalog fetch is sync and may hit the network on a cold cache, so
    it must run OFF the event loop. It backs BOTH the resolution and the
    available-classes report, so a concurrent heartbeat must keep ticking on the
    happy AND the error path. Proof: a deliberately-slow fetch does not starve a
    10ms heartbeat."""
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
        params = {"prompt": "x"}
        if model is not None:
            params["model"] = model
        res = await ex(params, ToolContext(operator="system"))
        hb.cancel()
        return res

    res = asyncio.run(main())
    assert res.success is expect_success
    # If the sync sleep blocked the loop, the 10ms heartbeat could not tick.
    assert len(ticks) >= 5, (
        f"event loop appears blocked during fetch ({len(ticks)} ticks in {DELAY}s)")
