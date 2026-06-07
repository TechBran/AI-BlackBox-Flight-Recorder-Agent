"""x-source resolution in the converter paths (Tasks 4.2 + 4.3).

Task 4.1 made every ``get_*_tools`` getter derive from the toolvault registry.
This combined task additionally runs each canonical schema through
``resolve_schema`` BEFORE conversion (via ``tool_registry._resolved_group``), so
dynamic ``x-source`` fields resolve identically to the chat injector for EVERY
non-injector consumer (MCP server + chat static fallback arrays + any direct
converter use).

No shipped module carries ``x-source`` yet, so resolution is a no-op today and
``test_registry_parity.py`` stays byte-identical. These tests PROVE the wiring
works by INJECTING a fake canonical tool that DOES carry ``x-source: operators``
(via monkeypatching ``tool_registry.get_tools_by_group`` — the single chokepoint
``_resolved_group`` calls) and asserting the converter output is resolved +
stripped. State is fully hermetic; nothing touches disk or the real registry.
"""

import sys
import types

import pytest


# ---------------------------------------------------------------------------
# mcp.types.Tool stub — the real ``mcp`` package isn't installed in this venv
# (MCP server runs in its own env). ``to_mcp`` lazy-imports ``mcp.types.Tool``
# and only reads .name/.description/.inputSchema back. Same shim pattern as
# test_registry_parity.py. Install BEFORE importing tool_registry consumers.
# ---------------------------------------------------------------------------
def _install_mcp_stub() -> None:
    if "mcp.types" in sys.modules or "mcp" in sys.modules:
        return
    try:
        import mcp.types  # noqa: F401
        return
    except Exception:
        pass

    class Tool:
        def __init__(self, name, description, inputSchema):
            self.name = name
            self.description = description
            self.inputSchema = inputSchema

    mcp_mod = types.ModuleType("mcp")
    types_mod = types.ModuleType("mcp.types")
    types_mod.Tool = Tool
    mcp_mod.types = types_mod
    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.types"] = types_mod


_install_mcp_stub()

from Orchestrator.tools import tool_registry as tr  # noqa: E402
from Orchestrator.toolvault import resolvers  # noqa: E402


_FAKE_GROUP = "chat"


def _fake_canonical_tool() -> dict:
    """A canonical tool whose ``operator`` property carries x-source: operators."""
    return {
        "name": "fake_snapshot",
        "description": "Fake tool with a dynamic operator field.",
        "groups": [_FAKE_GROUP, "mcp"],
        "parameters": {
            "type": "object",
            "properties": {
                "operator": {
                    "type": "string",
                    "description": "Whose work to record.",
                    "x-source": "operators",
                },
                "message": {
                    "type": "string",
                    "description": "Plain field, no resolution.",
                },
            },
            "required": ["operator"],
        },
    }


@pytest.fixture
def fake_registry(monkeypatch):
    """Make every converter see exactly one x-source-bearing canonical tool, and
    pin the live operator list deterministically (no backend access)."""
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: ["Brandon", "system"])
    monkeypatch.setattr(
        tr, "get_tools_by_group", lambda group: [_fake_canonical_tool()]
    )


def test_anthropic_converter_resolves_x_source(fake_registry):
    tools = tr.get_anthropic_tools(_FAKE_GROUP)
    assert len(tools) == 1
    op = tools[0]["input_schema"]["properties"]["operator"]
    assert op["enum"] == ["Brandon", "system"]
    assert "x-source" not in op
    # Plain property survives untouched.
    assert tools[0]["input_schema"]["properties"]["message"] == {
        "type": "string",
        "description": "Plain field, no resolution.",
    }


def test_openai_rest_converter_resolves_x_source(fake_registry):
    tools = tr.get_openai_rest_tools(_FAKE_GROUP)
    assert len(tools) == 1
    op = tools[0]["function"]["parameters"]["properties"]["operator"]
    assert op["enum"] == ["Brandon", "system"]
    assert "x-source" not in op


def test_mcp_converter_resolves_x_source(fake_registry):
    # _resolved_group("mcp") is hardwired in get_mcp_tools; the monkeypatched
    # get_tools_by_group ignores the group arg and returns our fake tool.
    tools = tr.get_mcp_tools()
    assert len(tools) == 1
    op = tools[0].inputSchema["properties"]["operator"]
    assert op["enum"] == ["Brandon", "system"]
    assert "x-source" not in op


def test_x_source_does_not_leak_into_real_registry():
    """Sanity: with NO monkeypatch, no real shipped tool carries x-source, so
    converter output is clean (this is what keeps parity a no-op today)."""
    for t in tr.get_anthropic_tools("chat"):
        for prop in t["input_schema"].get("properties", {}).values():
            assert "x-source" not in prop


# ---------------------------------------------------------------------------
# Fallback is registry-sourced: chat_routes CHAT_TOOLS_* == getter output.
# ---------------------------------------------------------------------------
def test_chat_fallback_arrays_are_registry_derived():
    from Orchestrator.routes import chat_routes as cr

    assert cr.CHAT_TOOLS_ANTHROPIC == tr.get_anthropic_tools("chat")
    assert cr.CHAT_TOOLS_OPENAI == tr.get_openai_rest_tools("chat")
    assert cr.CHAT_TOOLS_GEMINI == tr.get_gemini_rest_tools("chat")
    assert cr.CHAT_TOOLS_ANTHROPIC_CU == tr.get_anthropic_tools("chat_cu")
    # xAI reuses the OpenAI array object.
    assert cr.CHAT_TOOLS_XAI == cr.CHAT_TOOLS_OPENAI
