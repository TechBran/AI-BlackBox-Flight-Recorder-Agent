"""Task 6.3 — UGV tool surface removal guard.

The UGV Beast tool surface has been retired (to be revisited later from git
history). These tests assert that NO ``ugv_*`` tool survives anywhere on the
surface — the canonical registry, the per-backend converters, or the executor
class — and that calling a retired UGV tool fails gracefully rather than
crashing.
"""

import asyncio

from Orchestrator.toolvault import registry
from Orchestrator.tools.tool_registry import (
    get_anthropic_tools,
    get_openai_rest_tools,
)
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


def _is_ugv(name: str) -> bool:
    return bool(name) and name.startswith("ugv_")


def test_no_ugv_in_canonical():
    canonical = registry.load_canonical()
    assert not any(_is_ugv(t["name"]) for t in canonical), \
        "ugv_* tool leaked into the canonical registry"


def test_no_ugv_in_anthropic_chat():
    tools = get_anthropic_tools("chat")
    assert not any(_is_ugv(t["name"]) for t in tools), \
        "ugv_* tool leaked into the Anthropic chat surface"


def test_no_ugv_in_openai_rest_chat():
    tools = get_openai_rest_tools("chat")
    # OpenAI REST tools nest the name under ``function.name``.
    names = [t.get("function", {}).get("name", t.get("name")) for t in tools]
    assert not any(_is_ugv(n) for n in names), \
        "ugv_* tool leaked into the OpenAI REST chat surface"


def test_executor_has_no_ugv_members():
    members = dir(BlackBoxToolExecutor)
    assert not any(a.startswith("_execute_ugv_") for a in members), \
        "a _execute_ugv_* executor method still exists on BlackBoxToolExecutor"
    assert not hasattr(BlackBoxToolExecutor, "_ugv_call"), \
        "_ugv_call proxy helper still exists"
    assert not hasattr(BlackBoxToolExecutor, "_ugv_er_call"), \
        "_ugv_er_call proxy helper still exists"
    assert not hasattr(BlackBoxToolExecutor, "UGV_BASE_URL"), \
        "UGV_BASE_URL class attr still exists"
    assert not hasattr(BlackBoxToolExecutor, "UGV_ER_BASE_URL"), \
        "UGV_ER_BASE_URL class attr still exists"


def test_calling_retired_ugv_tool_fails_gracefully():
    ex = BlackBoxToolExecutor(operator="x")
    result = asyncio.run(ex.execute("ugv_motion_stop", {}))
    assert result.success is False, \
        "retired ugv_motion_stop should return success=False, not crash"
