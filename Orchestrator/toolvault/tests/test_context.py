"""Tests for ToolVault v2 ToolContext + ToolResult contract (Task 0.1)."""

from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.tools.blackbox_tools import ToolResult as BBToolResult


def test_toolcontext_explicit_values():
    ctx = ToolContext(operator="x", base_url="y")
    assert ctx.operator == "x"
    assert ctx.base_url == "y"


def test_toolcontext_defaults():
    ctx = ToolContext()
    assert ctx.operator == "system"
    assert ctx.base_url == "http://localhost:9091"


def test_toolresult_is_reexport_same_object():
    # Re-export identity: the class object must be THE SAME, not a copy.
    assert ToolResult is BBToolResult
