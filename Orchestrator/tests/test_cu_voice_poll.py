"""M1-T7: voice agents can drive AND poll computer use.

Two halves, both guarded here:

  1. GROUPS — ``get_task_status`` is now exposed to all three voice surfaces
     (groups ``realtime`` / ``gemini_live`` / ``grok_live``) IN ADDITION to its
     original four (``chat`` / ``chat_cu`` / ``phone`` / ``mcp``). Without it,
     a voice agent could launch a ``use_computer`` task and then had no way to
     learn whether it finished. It must serialize to the FLAT shape each voice
     surface accepts (plain string params — no enum / nested object).

  2. PROMPTS — all SIX voice ``system_instructions`` branches (a ``custom_role``
     branch + a default branch, in each of the three routes) carry the
     COMPUTER CONTROL guidance. This is the guard against the "edit one branch,
     miss its twin" bug shape that has bitten this repo before: the structural
     test finds every ``system_instructions`` f-string by AST and asserts each
     one contains the distinctive marker.
"""
import ast
import pathlib

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.tools.tool_registry import (
    get_openai_realtime_tools,
    get_gemini_live_tools,
    reset_cache,
)

ROUTES = pathlib.Path(__file__).resolve().parents[1] / "routes"
MARKER = "COMPUTER CONTROL"

VOICE_ROUTE_FILES = [
    "realtime_routes.py",
    "gemini_live_routes.py",
    "grok_live_routes.py",
]


@pytest.fixture(autouse=True)
def _fresh():
    """Pick up on-disk schema edits around every test (both cache layers)."""
    registry.invalidate_cache()
    reset_cache()
    yield
    registry.invalidate_cache()
    reset_cache()


# ---------------------------------------------------------------------------
# 1. get_task_status groups + flat serialization
# ---------------------------------------------------------------------------

def test_get_task_status_has_all_voice_groups_and_keeps_originals():
    tool = registry.get_tool("get_task_status")
    assert tool is not None
    groups = set(tool["groups"])
    # Original four must survive.
    assert {"chat", "chat_cu", "phone", "mcp"}.issubset(groups), (
        f"lost an original group: {groups}")
    # Three voice groups added.
    assert {"realtime", "gemini_live", "grok_live"}.issubset(groups), (
        f"missing a voice group: {groups}")


def _assert_flat(param_schema):
    """No enum / nested object anywhere in the properties (the voice-flat shape
    the OpenAI-realtime and Gemini-live flatteners require)."""
    for name, p in param_schema.get("properties", {}).items():
        assert "enum" not in p, f"{name} carries an enum (rejected by voice surfaces)"
        assert "properties" not in p, f"{name} is a nested object (rejected)"
        assert p.get("type") != "object", f"{name} is an object type (rejected)"


def test_get_task_status_in_openai_realtime_group_and_flat():
    tools = {t["name"]: t for t in get_openai_realtime_tools("realtime")}
    assert "get_task_status" in tools
    _assert_flat(tools["get_task_status"]["parameters"])


def test_get_task_status_in_grok_live_group_and_flat():
    # Grok Live reuses the OpenAI-realtime flat shape.
    tools = {t["name"]: t for t in get_openai_realtime_tools("grok_live")}
    assert "get_task_status" in tools
    _assert_flat(tools["get_task_status"]["parameters"])


def test_get_task_status_in_gemini_live_group_and_flat():
    decls = get_gemini_live_tools("gemini_live")
    # Gemini Live shape: [{"functionDeclarations": [...]}].
    fns = {f["name"]: f for f in decls[0]["functionDeclarations"]}
    assert "get_task_status" in fns
    _assert_flat(fns["get_task_status"]["parameters"])


# ---------------------------------------------------------------------------
# 2. Six-branch prompt guard (AST scan of every system_instructions f-string)
# ---------------------------------------------------------------------------

def _system_instruction_literals(path):
    """Return the concatenated LITERAL text of every ``system_instructions = f"..."``
    assignment in a source file (interpolations dropped; escaped braces collapsed
    by the AST, so the rendered marker text is what we match)."""
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "system_instructions" in targets and isinstance(node.value, ast.JoinedStr):
                literal = "".join(
                    v.value for v in node.value.values if isinstance(v, ast.Constant)
                )
                out.append(literal)
    return out


def test_exactly_six_system_instruction_branches():
    total = sum(len(_system_instruction_literals(ROUTES / fn)) for fn in VOICE_ROUTE_FILES)
    assert total == 6, f"expected 6 system_instructions branches across voice routes, found {total}"


def test_all_six_branches_carry_computer_control_guidance():
    for fn in VOICE_ROUTE_FILES:
        blocks = _system_instruction_literals(ROUTES / fn)
        assert len(blocks) == 2, f"{fn}: expected 2 system_instructions branches, got {len(blocks)}"
        for i, text in enumerate(blocks):
            assert MARKER in text, f"{fn} branch #{i} is missing the {MARKER!r} section"
            # The whole point: poll the launched task instead of going silent.
            assert "get_task_status" in text, (
                f"{fn} branch #{i} COMPUTER CONTROL section omits the get_task_status poll directive")
