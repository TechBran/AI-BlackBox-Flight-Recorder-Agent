"""M1-T3: the chat-route `use_computer` handlers route through the ToolVault executor.

`use_computer` has SEVEN call sites across the provider tool-loops in
Orchestrator/routes/chat_routes.py. Five already dispatch through
``BlackBoxToolExecutor.execute("use_computer", ...)`` (which runs the T2
per-tool module executor → class resolution, device_id, url, AND the structured
retryable failure). TWO legacy Anthropic branches (non-stream call_anthropic and
stream stream_anthropic_with_thinking) instead called ``create_task`` DIRECTLY,
building ``result_data={"url": url} if url else {}`` — dropping ``device_id`` and
``model``. Anthropic is the default provider on this box, so class routing was
inert on the primary chat path and downstream tasks got ``result_data["model"]
is None``.

T3 routes BOTH legacy branches through the executor, so all seven inherit
resolution / threading / failure-shaping from ONE source and cannot drift.

Covered here:
  * STRUCTURAL (both branches, revert-proof): every ``use_computer`` branch in
    chat_routes.py dispatches via ``.execute("use_computer", ...)`` and NONE
    calls ``create_task`` directly. The matcher is name-agnostic and
    membership-aware (a reworded branch cannot escape it — pinned by its own
    test) and the branch count is pinned at 7 to guard the matcher's coverage.
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
_CHAT_ROUTES = _REPO / "Orchestrator" / "routes" / "chat_routes.py"

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


def test_use_computer_branch_count_pins_matcher_coverage():
    """Pinned count = 7.

    The count-pin's real value is NOT the branch tally: it guards the MATCHER's
    own coverage. If a future branch drops out of AST detection (reworded past
    the matcher, or the matcher regresses), the count falls below 7 here AND the
    branch vanishes from the property test unseen — the two tests cover each
    other's blind spot. So do NOT reflexively bump 7 when this fails."""
    branches = _use_computer_branches(_CHAT_ROUTES)
    assert len(branches) == 7, (
        f"expected 7 use_computer branches, found {len(branches)} at lines "
        f"{[b.lineno for b in branches]}. This pin guards the AST matcher's "
        f"coverage, not the branch count per se: if you LEGITIMATELY added a "
        f"branch, first confirm test_every_use_computer_branch_routes_through_"
        f"executor actually SEES it (broaden _is_use_computer_test if not), then "
        f"bump this number — never bump it blind, or you disable both tests."
    )


def test_every_use_computer_branch_routes_through_executor():
    """The whole T3 fix: EVERY use_computer branch dispatches via
    ``.execute("use_computer", ...)`` and NONE calls create_task directly.

    Reverting either legacy Anthropic branch to ``create_task(...)`` fails this.
    """
    offenders_no_execute = []
    offenders_create_task = []
    for b in _use_computer_branches(_CHAT_ROUTES):
        calls = _branch_body_calls(b)
        if not any(_calls_execute_use_computer(c) for c in calls):
            offenders_no_execute.append(b.lineno)
        if any(_calls_create_task(c) for c in calls):
            offenders_create_task.append(b.lineno)
    assert not offenders_no_execute, (
        f"use_computer branch(es) at lines {offenders_no_execute} do NOT dispatch "
        f'via .execute("use_computer", ...) — class routing / device_id / model '
        f"are inert there"
    )
    assert not offenders_create_task, (
        f"use_computer branch(es) at lines {offenders_create_task} call create_task "
        f"DIRECTLY — bypassing the executor drops device_id + model (the T3 bug)"
    )


def test_stream_branch_still_calls_the_browser_task_helper():
    """Pins the seam the _browser_task_event extraction created.

    The helper's own behavior is covered below, but a tested helper proves
    nothing if the shipping branch stops calling it: delete the call site and
    every other test in this file still passes (verified). Nothing else looks
    for it — the property test above asserts only that .execute() is called.
    """
    callers = [
        b.lineno for b in _use_computer_branches(_CHAT_ROUTES)
        if any(
            isinstance(c.func, ast.Name) and c.func.id == "_browser_task_event"
            for c in _branch_body_calls(b)
        )
    ]
    assert len(callers) == 1, (
        "expected exactly ONE use_computer branch (the Anthropic stream branch) to "
        f"call _browser_task_event, found {len(callers)} at {callers}. If the call "
        "site was removed, the stream branch silently stops emitting browser_task "
        "while the helper's unit tests keep passing."
    )


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
