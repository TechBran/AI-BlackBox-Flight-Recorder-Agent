"""M1-T3: the chat-route `use_computer` handlers route through the ToolVault executor.

`use_computer` used to have SEVEN call sites across the provider tool-loops in
Orchestrator/routes/chat_routes.py. Five dispatched through
``BlackBoxToolExecutor.execute("use_computer", ...)`` (which runs the T2
per-tool module executor → class resolution, device_id, url, AND the structured
retryable failure). TWO legacy Anthropic branches (non-stream call_anthropic and
stream stream_anthropic_with_thinking) instead called ``create_task`` DIRECTLY,
building ``result_data={"url": url} if url else {}`` — dropping ``device_id`` and
``model``. Anthropic is the default provider on this box, so class routing was
inert on the primary chat path and downstream tasks got ``result_data["model"]
is None``.

T3 routed BOTH legacy branches through the executor; audit rank 74 then moved
every loop's tool table into the one dispatcher (providers/tool_dispatch.py),
so all ten loops inherit resolution / threading / failure-shaping from ONE
source and cannot drift.

Covered here:
  * BEHAVIOR (every loop profile, revert-proof): dispatch_tool runs
    use_computer through ``.execute("use_computer", ...)`` for all ten loops and
    never calls ``create_task`` itself; the streams that emit browser_task do.
  * STRUCTURAL: no inline ``use_computer`` branch is back in chat_routes.py or
    the dispatcher (one would bypass the shared seam). The matcher is
    name-agnostic and membership-aware (a reworded branch cannot escape it —
    pinned by its own test).
  * BEHAVIOR — non-stream (call_anthropic, driven end-to-end): a fake Anthropic
    tool_use requesting use_computer results in a task whose result_data carries
    device_id + resolved concrete model id + url; ``model="gemini"`` stores
    Google's CU id; an unresolvable class (``model="haiku"``) surfaces the
    structured retryable failure to the model and creates NO task.
  * BEHAVIOR — stream (the browser_task guard): the stream branch's novel logic
    (success-only browser_task emit + task_id extraction from ToolResult.data) is
    factored into the pure helper chat_routes._browser_task_event and tested
    against a REAL executor ToolResult. Driving the full async SSE generator is
    deliberately avoided — faking Anthropic's wire format would exercise the
    fake, not the plumbing (same rationale as test_chat_loop_operator_scoping.py).
"""
import ast
import asyncio
import json
from pathlib import Path

import pytest

import Orchestrator.routes.chat_routes as cr
from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext

_REPO = Path(__file__).resolve().parents[2]
_TOOL_DISPATCH = _REPO / "Orchestrator" / "providers" / "tool_dispatch.py"
# The chat routes: the chat_routes namespace and its parts (audit rank 74).
_CHAT_ROUTE_FILES = [_REPO / "Orchestrator" / "routes" / "chat_routes.py",
                     *sorted((_REPO / "Orchestrator" / "routes" / "chat").glob("*.py"))]

# The same live-shaped CU catalog T2 uses: anthropic opus + sonnet and one gemini
# CU model — deliberately no gpt/openai member, so "available classes" is a
# non-trivial subset ({opus, sonnet, gemini}).
_CATALOG = [
    {"id": "claude-opus-4-8", "backend": "anthropic"},
    {"id": "claude-sonnet-4-6", "backend": "anthropic"},
    {"id": "gemini-2.5-computer-use-preview-10-2025", "backend": "google"},
]
_GEMINI_ID = "gemini-2.5-computer-use-preview-10-2025"
_OPUS_ID = "claude-opus-4-8"


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Pick up on-disk schema/executor edits around every test."""
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


# --------------------------------------------------------------------------- #
# Structural: ALL use_computer branches route via the executor (revert-proof)  #
# --------------------------------------------------------------------------- #

def _mentions_use_computer(node: ast.AST) -> bool:
    """A string constant "use_computer"."""
    return isinstance(node, ast.Constant) and node.value == "use_computer"


def _is_use_computer_test(test: ast.AST) -> bool:
    """True for a branch test that selects the use_computer tool, in ANY shape:

        x == "use_computer"
        x in ("use_computer", ...) / ["use_computer", ...] / {"use_computer", ...}

    Deliberately variable-name-AGNOSTIC (a future branch may use a loop variable
    other than tool_name/func_name) and membership-AWARE (a branch may fold
    use_computer into an ``in (...)`` set). This matcher is the SINGLE point of
    failure for both the count-pin and the property test — if it can be evaded by
    rewording the branch, an unguarded use_computer branch ships while both tests
    stay green. So it keys off the "use_computer" literal appearing on the RHS of
    an ``==`` or inside an ``in`` container, never off the compared variable."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    op, comp = test.ops[0], test.comparators[0]
    if isinstance(op, ast.Eq):
        return _mentions_use_computer(comp)
    if isinstance(op, ast.In):
        return isinstance(comp, (ast.Tuple, ast.List, ast.Set)) and any(
            _mentions_use_computer(e) for e in comp.elts
        )
    return False


def _use_computer_branches(path: Path):
    """Every `if/elif <name> == "use_computer":` node in the file. `elif` is a
    nested `ast.If` in the parent's orelse, so ast.walk finds them all."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.If) and _is_use_computer_test(n.test)]


def _branch_body_calls(node: ast.If):
    """All ast.Call nodes in the branch's OWN body (not the elif/else chain)."""
    calls = []
    for stmt in node.body:
        calls.extend(c for c in ast.walk(stmt) if isinstance(c, ast.Call))
    return calls


def _calls_execute_use_computer(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "execute"
        and len(call.args) >= 1
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "use_computer"
    )


def _calls_create_task(call: ast.Call) -> bool:
    return (isinstance(call.func, ast.Name) and call.func.id == "create_task") or (
        isinstance(call.func, ast.Attribute) and call.func.attr == "create_task"
    )


def test_matcher_detects_membership_form_and_is_name_agnostic():
    """The matcher is the single point of failure for both AST tests, so pin its
    coverage directly: it must catch a branch written as ``x in ("use_computer",
    ...)`` and one using a loop variable other than tool_name/func_name, and it
    must NOT match an unrelated comparison. A regression here silently shrinks
    the property test's reach while leaving both tests green."""
    membership = ast.parse('if fn in ("foo", "use_computer"):\n    pass\n').body[0]
    assert _is_use_computer_test(membership.test), "membership `in (...)` form not detected"

    other_var = ast.parse('if whatever_name == "use_computer":\n    pass\n').body[0]
    assert _is_use_computer_test(other_var.test), "matcher wrongly depends on the variable name"

    list_form = ast.parse('if fn in ["use_computer"]:\n    pass\n').body[0]
    assert _is_use_computer_test(list_form.test), "membership list form not detected"

    unrelated = ast.parse('if fn == "search_snapshots":\n    pass\n').body[0]
    assert not _is_use_computer_test(unrelated.test), "matched an unrelated comparison"


def test_no_inline_use_computer_branch_bypasses_the_shared_dispatcher():
    """Pinned count = 0 in the chat-route modules and the dispatcher.

    Every provider loop hands use_computer to dispatch_tool, which routes it by
    table membership (no ``== "use_computer"`` branch at all). An inline branch
    reappearing in chat_routes.py, or an if-branch in the dispatcher, would be a
    second path that can drift from the executor (the T3 bug); if one is added
    on purpose it must pass the property test below first."""
    for path in (*_CHAT_ROUTE_FILES, _TOOL_DISPATCH):
        branches = _use_computer_branches(path)
        assert branches == [], (
            f"{path.name}: inline use_computer branch(es) at lines "
            f"{[b.lineno for b in branches]} bypass the shared dispatch_tool table")


def test_every_use_computer_branch_routes_through_executor():
    """The whole T3 fix, kept as a guard: any use_computer branch (there are
    none today) dispatches via ``.execute("use_computer", ...)`` and NONE calls
    create_task directly."""
    offenders_no_execute = []
    offenders_create_task = []
    for path in (*_CHAT_ROUTE_FILES, _TOOL_DISPATCH):
        for b in _use_computer_branches(path):
            calls = _branch_body_calls(b)
            if not any(_calls_execute_use_computer(c) for c in calls):
                offenders_no_execute.append((path.name, b.lineno))
            if any(_calls_create_task(c) for c in calls):
                offenders_create_task.append((path.name, b.lineno))
    assert not offenders_no_execute, (
        f"use_computer branch(es) at {offenders_no_execute} do NOT dispatch "
        f'via .execute("use_computer", ...) — class routing / device_id / model '
        f"are inert there"
    )
    assert not offenders_create_task, (
        f"use_computer branch(es) at {offenders_create_task} call create_task "
        f"DIRECTLY — bypassing the executor drops device_id + model (the T3 bug)"
    )


def _dispatch_use_computer(monkeypatch, loop, data):
    """dispatch_tool(use_computer) for ``loop``'s profile with a recording
    executor; returns (outcome, executor_calls, create_task_calls)."""
    from Orchestrator.providers import tool_dispatch as td
    from Orchestrator.toolvault.context import ToolResult
    import Orchestrator.tools as tools_pkg

    executed, created = [], []

    class _Executor:
        def __init__(self, operator="system", origin_device_id=None, **kw):
            self.operator = operator

        async def execute(self, name, args):
            executed.append((name, dict(args), self.operator))
            return ToolResult(success=True, result="Computer Use task started", data=data)

    monkeypatch.setattr(tools_pkg, "BlackBoxToolExecutor", _Executor)
    services = td.ToolServices(
        create_task=lambda *a, **k: created.append((a, k)), generate_prompt_slug=str,
        perform_web_fetch=None, hybrid_retrieve=None, execute_get_media=None,
        execute_list_media=None, execute_search_media=None, resolve_attachment=None)
    ctx = td.ToolContext(operator="op-1", origin_device_id=None,
                         profile=td.PROFILES[loop], services=services)
    outcome = asyncio.run(td.dispatch_tool("use_computer", {"prompt": "open the site"}, ctx))
    return outcome, executed, created


@pytest.mark.parametrize("loop", sorted(cr._TOOL_PROFILES))
def test_every_loop_runs_use_computer_through_the_executor(monkeypatch, loop):
    outcome, executed, created = _dispatch_use_computer(monkeypatch, loop, {"task_id": "t-1"})
    assert executed == [("use_computer", {"prompt": "open the site"}, "op-1")]
    assert created == [], "the dispatcher must never create the CU task itself"
    assert outcome.result == "Computer Use task started"


def test_stream_loops_still_emit_browser_task_through_the_helper(monkeypatch):
    """M3 (2026-07-23) extended the browser_task emit from the single Anthropic
    stream to EVERY provider's use_computer branch (openai/gemini/xai +
    anthropic), so the in-chat agent pill appears no matter which model launched
    the run, and audit rank 74 gave the custom stream the one it lacked. A loop
    that silently stops emitting it fails here, and a failure (no task_id) never
    emits one."""
    emitting = []
    for loop, profile in sorted(cr._TOOL_PROFILES.items()):
        if not profile.stream:
            continue
        outcome, _, _ = _dispatch_use_computer(monkeypatch, loop, {"task_id": "t-1"})
        browser = [e for e in outcome.events if e["type"] == "browser_task"]
        if browser:
            assert browser == [{"type": "browser_task",
                                "data": {"task_id": "t-1", "prompt": "open the site"}}]
            emitting.append(loop)
        failed, _, _ = _dispatch_use_computer(monkeypatch, loop, {"retryable": True})
        assert not [e for e in failed.events if e["type"] == "browser_task"]
    assert set(emitting) == {"stream_anthropic", "stream_openai", "stream_gemini", "stream_xai",
                             "stream_custom"}, emitting


# --------------------------------------------------------------------------- #
# Behavior: the non-stream Anthropic loop, driven end-to-end                   #
# --------------------------------------------------------------------------- #

class _FakeResp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "fake"

    def json(self):
        return self._payload


def _tool_use_resp(tool_input):
    return _FakeResp({
        "content": [{
            "type": "tool_use", "id": "tu_1", "name": "use_computer",
            "input": tool_input,
        }],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


def _end_turn_resp():
    return _FakeResp({
        "content": [{"type": "text", "text": "done"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


@pytest.fixture
def driven(monkeypatch):
    """Drive call_anthropic through a use_computer tool_use, serving a fixed CU
    catalog and capturing whatever create_task the executor would build.

    Returns a callable ``run(tool_input) -> {captured, model_saw}`` where
    ``captured`` is the create_task kwargs (empty dict if never called) and
    ``model_saw`` is the tool_result content the branch fed back to the model.
    """
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.routes import admin_routes
    from Orchestrator import tasks as tasks_mod

    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic", raising=False)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "read_text_safe", lambda *a, **k: "")

    def fake_get_available_models(kind):
        assert kind == "computer-use"
        return {"models": [dict(m) for m in _CATALOG], "default_id": _OPUS_ID}

    monkeypatch.setattr(admin_routes, "get_available_models", fake_get_available_models)

    captured = {}

    class _FakeTask:
        task_id = "task-fake-001"

    def fake_create_task(task_type, operator=None, prompt=None, result_data=None, **kw):
        captured["task_type"] = task_type
        captured["operator"] = operator
        captured["prompt"] = prompt
        captured["result_data"] = result_data
        return _FakeTask()

    # Patch BOTH bindings: the executor imports fresh from tasks; a reverted
    # branch would use chat_routes' module-level create_task. Patching both means
    # a revert is CAUGHT (captured shows the old no-model result_data) without
    # ever touching the real task store.
    monkeypatch.setattr(tasks_mod, "create_task", fake_create_task)
    monkeypatch.setattr(cr, "create_task", fake_create_task, raising=False)

    def run(tool_input):
        responses = iter([_tool_use_resp(tool_input), _end_turn_resp()])
        posted = []

        def fake_post(*a, **k):
            posted.append(k.get("json"))
            return next(responses)

        monkeypatch.setattr(cr.requests, "post", fake_post)
        cr.call_anthropic(
            [{"role": "user", "content": "drive the browser"}],
            "claude-test-model",
            operator="op-1",
        )
        # The tool_result content the branch fed back to the model lives in the
        # (mutated-in-place) payload messages posted on the follow-up turn.
        model_saw = None
        for msg in (posted[-1] or {}).get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_result" \
                            and part.get("tool_use_id") == "tu_1":
                        model_saw = part.get("content")
        return {"captured": captured, "model_saw": model_saw}

    return run


def test_anthropic_nonstream_threads_device_id_model_and_url(driven):
    """model="gemini" + url -> task result_data carries device_id, the resolved
    Google CU id, and the url (the exact keys the legacy branch dropped)."""
    out = driven({"prompt": "open the site", "url": "https://example.com", "model": "gemini"})
    rd = out["captured"].get("result_data")
    assert rd is not None, "no task created — use_computer branch never ran the executor"
    assert rd["device_id"] == "blackbox"
    assert rd["model"] == _GEMINI_ID, "class routing inert: model not resolved on Anthropic path"
    assert rd["url"] == "https://example.com"
    assert out["captured"]["operator"] == "op-1"


def test_anthropic_nonstream_default_class_resolves_to_opus(driven):
    """Omitted model -> default class (opus) resolves to a concrete id, never None."""
    out = driven({"prompt": "just do it"})
    rd = out["captured"].get("result_data")
    assert rd is not None
    assert rd["model"] == _OPUS_ID
    assert rd["device_id"] == "blackbox"


def test_anthropic_nonstream_unresolvable_class_surfaces_structured_failure(driven):
    """model="haiku" (no CU support) -> structured retryable failure string to the
    model, and NO task created (not raised, not silently succeeded)."""
    out = driven({"prompt": "x", "model": "haiku"})
    assert "result_data" not in out["captured"], "a task was created for an unresolvable class"
    payload = json.loads(out["model_saw"])
    assert payload["success"] is False
    assert payload["retryable"] is True  # catalog serves opus/sonnet/gemini
    assert "haiku" in payload["reason"]
    assert set(payload["available"]) == {"opus", "sonnet", "gemini"}


# --------------------------------------------------------------------------- #
# Behavior: the STREAM branch's browser_task guard                             #
# --------------------------------------------------------------------------- #
# The stream branch (stream_anthropic_with_thinking) is an async generator that
# consumes an Anthropic httpx SSE stream. Faking that wire format would exercise
# the fake, not the plumbing — the same reason test_chat_loop_operator_scoping.py
# covers the streaming loops structurally rather than driving them. So the
# branch's ONLY novel logic — the success-only browser_task emit + task_id
# extraction from ToolResult.data — is factored into the pure helper
# chat_routes._browser_task_event and behavior-tested HERE against a REAL
# executor ToolResult (not a hand-built stub): a future executor that nested
# task_id under a sub-key, mirrored the wrong payload into .data, or inverted the
# guard fails these. (The ticker `yield` that precedes the guard is unconditional
# plain code with no branch, so it carries no regression risk of its own.)


@pytest.fixture
def cu_executor(monkeypatch):
    """Run the REAL use_computer executor against the fixed CU catalog and return
    ``(run, captured)``: ``run(tool_input)`` -> the ToolResult the stream branch
    feeds to _browser_task_event; ``captured`` holds the create_task kwargs so a
    test can confirm the success path genuinely resolved a model."""
    from Orchestrator.routes import admin_routes
    from Orchestrator import tasks as tasks_mod

    monkeypatch.setattr(
        admin_routes, "get_available_models",
        lambda kind: {"models": [dict(m) for m in _CATALOG], "default_id": _OPUS_ID},
    )
    captured = {}

    class _FakeTask:
        task_id = "task-fake-001"

    def fake_create_task(task_type, operator=None, prompt=None, result_data=None, **kw):
        captured["result_data"] = result_data
        return _FakeTask()

    monkeypatch.setattr(tasks_mod, "create_task", fake_create_task)

    def run(tool_input):
        ex = registry.get_executor("use_computer")
        assert ex is not None, "use_computer executor failed to load"
        return asyncio.run(ex(tool_input, ToolContext(operator="op-1")))

    return run, captured


def test_stream_browser_task_emitted_on_success(cu_executor):
    """SUCCESS -> exactly one browser_task event carrying the created task_id and
    the prompt. The underlying create_task got the RESOLVED Google CU id, proving
    this is the real resolved success path, not a stub."""
    run, captured = cu_executor
    cu_result = run({"prompt": "open the site", "url": "https://x", "model": "gemini"})
    assert cu_result.success is True
    assert captured["result_data"]["model"] == _GEMINI_ID  # real resolution happened

    event = cr._browser_task_event(cu_result, {"prompt": "open the site"})
    assert event == {
        "type": "browser_task",
        "data": {"task_id": "task-fake-001", "prompt": "open the site"},
    }


def test_stream_no_browser_task_on_unresolvable_class(cu_executor):
    """model="haiku" -> the executor fails (structured retryable payload mirrored
    into .data, which carries no task_id), so the guard emits NO browser_task."""
    run, _captured = cu_executor
    cu_result = run({"prompt": "x", "model": "haiku"})
    # Sanity: this really is the failure path the guard must suppress.
    assert cu_result.success is False
    assert json.loads(cu_result.result)["success"] is False
    assert "task_id" not in (cu_result.data or {})

    assert cr._browser_task_event(cu_result, {"prompt": "x"}) is None
