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
  * STRUCTURAL (both branches, revert-proof): every ``== "use_computer"`` branch
    in chat_routes.py dispatches via ``.execute("use_computer", ...)`` and NONE
    of them calls ``create_task`` directly. The site count is pinned (7) so a new
    branch must consciously update this test. This is the guard the streaming
    branch relies on — faking Anthropic SSE would exercise the fake, not the
    plumbing (same rationale as test_chat_loop_operator_scoping.py).
  * BEHAVIOR (non-stream call_anthropic, driven end-to-end): a fake Anthropic
    tool_use requesting use_computer results in a task whose result_data carries
    device_id + resolved concrete model id + url; ``model="gemini"`` stores
    Google's CU id; an unresolvable class (``model="haiku"``) surfaces the
    structured retryable failure to the model and creates NO task.
"""
import ast
import json
from pathlib import Path

import pytest

from Orchestrator.toolvault import registry

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

def _is_use_computer_test(test: ast.AST) -> bool:
    """True for a `<tool_name|func_name> == "use_computer"` comparison."""
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id in ("tool_name", "func_name")
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "use_computer"
    )


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


def test_seven_use_computer_branches_pinned():
    """Pinned count: adding/removing a use_computer branch must consciously
    update this test (and route the new branch through the executor)."""
    branches = _use_computer_branches(_CHAT_ROUTES)
    assert len(branches) == 7, (
        f"expected 7 use_computer branches, found {len(branches)} at lines "
        f"{[b.lineno for b in branches]}"
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
