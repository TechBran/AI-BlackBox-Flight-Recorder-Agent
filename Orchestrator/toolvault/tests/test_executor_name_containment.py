"""get_executor refuses tool names that are not plain identifiers or whose
executor would load from outside ToolVault/tools/ (O17-toolvault-runtime-01).

The name reaches the loader straight from a model's tool call (every chat
catch-all) and from the raw body of POST /local/tools/execute.
``TOOLS_DIR / name / "executor.py"`` with ``name='../../Portal/uploads/sessions/x'``
— or an absolute ``'/tmp/evil'``, which pathlib lets REPLACE the left operand —
imported and ran whatever executor.py sat there, and session uploads keep the
client's original filename. Hermetic: TOOLS_DIR points at tmp.
"""
import asyncio

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext

_EVIL = (
    "import pathlib\n"
    "from Orchestrator.toolvault.context import ToolResult\n"
    "pathlib.Path(__file__).with_name('PWNED').write_text('ran at import')\n"
    "async def execute(params, ctx):\n"
    "    return ToolResult(True, 'pwned')\n"
)
_GOOD = (
    "from Orchestrator.toolvault.context import ToolResult\n"
    "async def execute(params, ctx):\n"
    "    return ToolResult(True, 'ok')\n"
)


@pytest.fixture
def box(tmp_path, monkeypatch):
    tools = tmp_path / "ToolVault" / "tools"
    tools.mkdir(parents=True)
    good = tools / "good_tool"
    good.mkdir()
    (good / "schema.json").write_text(
        '{"name": "good_tool", "description": "A tool.", "category": "communication",'
        ' "groups": ["chat"], "tier": 2, "parameters": {"type": "object", "properties": {}}}')
    (good / "executor.py").write_text(_GOOD)
    # An attacker-planted executor where session uploads land.
    planted = tmp_path / "Portal" / "uploads" / "sessions" / "s1"
    planted.mkdir(parents=True)
    (planted / "executor.py").write_text(_EVIL)
    monkeypatch.setattr(registry, "TOOLS_DIR", tools)
    registry.invalidate_cache()
    yield tmp_path
    registry.invalidate_cache()


@pytest.mark.parametrize("name", [
    "../../Portal/uploads/sessions/s1",
    "../../../Portal/uploads/sessions/s1",
    "good_tool/../../../Portal/uploads/sessions/s1",
    "../x",
    "",
    ".",
    "..",
    "good-tool",
    "good tool",
    "good_tool\x00",
    "good_tool\n",
])
def test_get_executor_refuses_non_identifier_names(box, name):
    assert registry.get_executor(name) is None
    assert not (box / "Portal" / "uploads" / "sessions" / "s1" / "PWNED").exists()


def test_get_executor_refuses_absolute_names(box):
    planted = str(box / "Portal" / "uploads" / "sessions" / "s1")
    assert registry.get_executor(planted) is None
    assert not (box / "Portal" / "uploads" / "sessions" / "s1" / "PWNED").exists()


@pytest.mark.parametrize("name", [None, 3, ["good_tool"]])
def test_get_executor_refuses_non_strings(box, name):
    assert registry.get_executor(name) is None


def test_get_executor_refuses_a_symlinked_tool_folder_escaping_tools_dir(box):
    (registry.TOOLS_DIR / "linked_tool").symlink_to(box / "Portal" / "uploads" / "sessions" / "s1")
    assert registry.get_executor("linked_tool") is None
    assert not (box / "Portal" / "uploads" / "sessions" / "s1" / "PWNED").exists()


def test_get_executor_still_loads_a_plain_name(box):
    ex = registry.get_executor("good_tool")
    assert ex is not None
    assert asyncio.run(ex({}, ToolContext())).result == "ok"


def test_dispatch_reports_unknown_tool_for_a_traversal_name(box):
    """Every chat surface dispatches through BlackBoxToolExecutor.execute."""
    from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor
    res = asyncio.run(BlackBoxToolExecutor(operator="Tester").execute(
        "../../Portal/uploads/sessions/s1", {}))
    assert res.success is False and "Unknown tool" in res.result
    assert not (box / "Portal" / "uploads" / "sessions" / "s1" / "PWNED").exists()
