"""
Test suite for tool_registry.py - verifies format conversion and group assignments.

Run: cd Orchestrator && python -m pytest tools/test_tool_registry.py -v
Or:  python -m pytest Orchestrator/tools/test_tool_registry.py -v
"""
import sys
import os

# Ensure Orchestrator is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from Orchestrator.tools.tool_registry import (
    TOOL_DEFINITIONS,
    get_tools_by_group,
    get_tool_by_name,
    resolve_alias,
    resolve_executor_name,
    get_anthropic_tools,
    get_openai_rest_tools,
    get_openai_realtime_tools,
    get_gemini_rest_tools,
    get_gemini_live_tools,
    get_all_tool_names,
    get_group_tool_names,
)


# =============================================================================
# Canonical Definition Tests
# =============================================================================

def test_all_tools_have_required_fields():
    """Every tool must have name, description, parameters, groups."""
    for tool in TOOL_DEFINITIONS:
        assert "name" in tool, f"Tool missing 'name': {tool}"
        assert "description" in tool, f"Tool {tool['name']} missing 'description'"
        assert "parameters" in tool, f"Tool {tool['name']} missing 'parameters'"
        assert "groups" in tool, f"Tool {tool['name']} missing 'groups'"
        assert tool["parameters"]["type"] == "object", f"Tool {tool['name']} parameters must be type 'object'"
        assert "properties" in tool["parameters"], f"Tool {tool['name']} missing 'properties' in parameters"
        assert isinstance(tool["groups"], list), f"Tool {tool['name']} groups must be a list"


def test_no_duplicate_names():
    """Every tool name must be unique."""
    names = [t["name"] for t in TOOL_DEFINITIONS]
    assert len(names) == len(set(names)), f"Duplicate tool names: {[n for n in names if names.count(n) > 1]}"


def test_tool_count():
    """Total tool count should match expected range."""
    assert len(TOOL_DEFINITIONS) >= 38, f"Expected 38+ tools, got {len(TOOL_DEFINITIONS)}"


# =============================================================================
# Group Tests
# =============================================================================

def test_chat_group_count():
    """Chat group should have ~32 tools (full set)."""
    tools = get_tools_by_group("chat")
    assert len(tools) >= 30, f"Chat group: expected 30+, got {len(tools)}"


def test_chat_cu_excludes_use_computer():
    """chat_cu group must NOT include use_computer."""
    names = get_group_tool_names("chat_cu")
    assert "use_computer" not in names, "chat_cu should not include use_computer"


def test_chat_cu_includes_other_tools():
    """chat_cu (the toolset a Computer-Use agent sees) = chat MINUS the
    autonomous-agent-spawning tools. The invariant is NOT "minus use_computer"
    specifically — it's "an agent already running an autonomous task must not
    spawn MORE autonomous agents." That excludes use_computer (no recursive CU)
    AND the YOLO CLI-agent tools (a screen-driving agent shouldn't fire off a
    background coding agent — consistent with the display arbiter + sandbox + the
    no-mcp/RCE posture). If you WANT a CU session to be able to launch a CLI agent,
    add "chat_cu" to those tools' groups and widen this set — it's a product call.
    (Was "== {use_computer}"; that described the state before the CLI-agent tools
    existed, not the principle.)"""
    chat = set(get_group_tool_names("chat"))
    chat_cu = set(get_group_tool_names("chat_cu"))
    diff = chat - chat_cu
    agent_spawning = {"use_computer", "claude_code_task", "gemini_cli_task", "codex_cli_task"}
    assert diff == agent_spawning, (
        f"chat vs chat_cu diff should be exactly the agent-spawning tools "
        f"{agent_spawning}, got: {diff}"
    )


def test_realtime_group_count():
    """Realtime group should have ~21 tools."""
    tools = get_tools_by_group("realtime")
    assert len(tools) >= 19, f"Realtime group: expected 19+, got {len(tools)}"


def test_mcp_group_count():
    """MCP group should have ~30 tools including MCP-only internals."""
    tools = get_tools_by_group("mcp")
    assert len(tools) >= 28, f"MCP group: expected 28+, got {len(tools)}"


def test_mcp_only_tools_exist():
    """MCP-only tools should be in mcp group but not in chat."""
    mcp_names = set(get_group_tool_names("mcp"))
    chat_names = set(get_group_tool_names("chat"))
    mcp_only = mcp_names - chat_names
    expected_mcp_only = {
        "seek_snapshot_direct", "mint_snapshot", "get_context",
        "list_operators", "get_index_stats", "browse_index",
        "chat_with_context", "refresh_index", "get_music_status",
    }
    assert expected_mcp_only.issubset(mcp_only), f"Missing MCP-only tools: {expected_mcp_only - mcp_only}"


def test_phone_group_count():
    """Phone group should have ~24 tools."""
    tools = get_tools_by_group("phone")
    assert len(tools) >= 22, f"Phone group: expected 22+, got {len(tools)}"


def test_live_groups_parity():
    """Realtime, gemini_live, and grok_live should have the same tools."""
    realtime = set(get_group_tool_names("realtime"))
    gemini = set(get_group_tool_names("gemini_live"))
    grok = set(get_group_tool_names("grok_live"))
    assert realtime == gemini, f"Realtime vs Gemini Live diff: {realtime.symmetric_difference(gemini)}"
    assert realtime == grok, f"Realtime vs Grok Live diff: {realtime.symmetric_difference(grok)}"


# =============================================================================
# Alias Tests
# =============================================================================

def test_alias_resolution():
    """Known aliases should resolve to canonical names."""
    assert resolve_alias("search_memory") == "search_snapshots"
    assert resolve_alias("get_recent_snapshots") == "list_recent_snapshots"


def test_alias_passthrough():
    """Non-aliases should pass through unchanged."""
    assert resolve_alias("web_search") == "web_search"
    assert resolve_alias("send_sms") == "send_sms"


def test_executor_name_resolution():
    """Executor name resolution for dispatch."""
    assert resolve_executor_name("search_snapshots") == "search_memory"
    assert resolve_executor_name("web_search") == "web_search"
    assert resolve_executor_name("search_memory") == "search_memory"  # alias → canonical → executor


def test_get_tool_by_name():
    """Should find tools by canonical name."""
    tool = get_tool_by_name("web_fetch")
    assert tool is not None
    assert tool["name"] == "web_fetch"


def test_get_tool_by_alias():
    """Should find tools by alias."""
    tool = get_tool_by_name("search_memory")
    assert tool is not None
    assert tool["name"] == "search_snapshots"


# =============================================================================
# Anthropic Format Tests
# =============================================================================

def test_anthropic_format():
    """Anthropic tools must have name, description, input_schema."""
    tools = get_anthropic_tools("chat")
    assert len(tools) > 0
    for t in tools:
        assert "name" in t, f"Missing 'name' in Anthropic tool"
        assert "description" in t, f"Missing 'description' in {t.get('name')}"
        assert "input_schema" in t, f"Missing 'input_schema' in {t['name']}"
        assert t["input_schema"]["type"] == "object"
        # Should NOT have 'parameters' key (that's OpenAI format)
        assert "parameters" not in t, f"Anthropic tool {t['name']} has 'parameters' — should be 'input_schema'"
        # Should NOT have 'type': 'function' (that's OpenAI format)
        assert t.get("type") != "function", f"Anthropic tool {t['name']} has type=function"


# =============================================================================
# OpenAI REST Format Tests
# =============================================================================

def test_openai_rest_format():
    """OpenAI REST: {"type": "function", "function": {"name", "parameters"}}."""
    tools = get_openai_rest_tools("chat")
    assert len(tools) > 0
    for t in tools:
        assert t["type"] == "function", f"OpenAI REST tool missing type=function"
        assert "function" in t, f"OpenAI REST tool missing 'function' wrapper"
        func = t["function"]
        assert "name" in func, "OpenAI REST function missing 'name'"
        assert "description" in func, f"OpenAI REST {func.get('name')} missing 'description'"
        assert "parameters" in func, f"OpenAI REST {func['name']} missing 'parameters'"
        # Should NOT have top-level name/description (those go inside function)
        assert "name" not in t or t.get("name") is None, "OpenAI REST should not have top-level 'name'"


# =============================================================================
# OpenAI Realtime Format Tests
# =============================================================================

def test_openai_realtime_format():
    """OpenAI Realtime: {"type": "function", "name", "parameters"} (flat)."""
    tools = get_openai_realtime_tools("realtime")
    assert len(tools) > 0
    for t in tools:
        assert t["type"] == "function"
        assert "name" in t, "Realtime tool missing top-level 'name'"
        assert "parameters" in t, f"Realtime tool {t['name']} missing 'parameters'"
        assert "description" in t, f"Realtime tool {t['name']} missing 'description'"
        # Should NOT have 'function' wrapper (that's REST format)
        assert "function" not in t, f"Realtime tool {t['name']} has 'function' wrapper — should be flat"


# =============================================================================
# Gemini REST Format Tests
# =============================================================================

def test_gemini_rest_format():
    """Gemini REST: [{"function_declarations": [...]}] (snake_case)."""
    tools = get_gemini_rest_tools("chat")
    assert len(tools) == 1, f"Gemini REST should return single-element list, got {len(tools)}"
    assert "function_declarations" in tools[0], "Missing 'function_declarations' key"
    decls = tools[0]["function_declarations"]
    assert len(decls) > 0
    for d in decls:
        assert "name" in d
        assert "description" in d
        assert "parameters" in d
        # Gemini should NOT have 'default' in properties, and enums must be strings
        for prop_name, prop in d["parameters"].get("properties", {}).items():
            assert "default" not in prop, f"Gemini REST tool {d['name']}.{prop_name} has 'default'"
            if "enum" in prop:
                for v in prop["enum"]:
                    assert isinstance(v, str), f"Gemini REST tool {d['name']}.{prop_name} enum has non-string: {v}"


# =============================================================================
# Gemini Live Format Tests
# =============================================================================

def test_gemini_live_format():
    """Gemini Live: [{"functionDeclarations": [...]}] (camelCase)."""
    tools = get_gemini_live_tools("gemini_live")
    assert len(tools) == 1, f"Gemini Live should return single-element list, got {len(tools)}"
    assert "functionDeclarations" in tools[0], "Missing 'functionDeclarations' key (camelCase)"
    # Should NOT have snake_case version
    assert "function_declarations" not in tools[0], "Gemini Live should use camelCase 'functionDeclarations'"
    decls = tools[0]["functionDeclarations"]
    assert len(decls) > 0


# =============================================================================
# Deep Copy / Isolation Tests
# =============================================================================

def test_format_conversion_does_not_mutate_canonical():
    """Format converters must not mutate the canonical definitions."""
    import json
    before = json.dumps(TOOL_DEFINITIONS, sort_keys=True, default=str)
    # Run all converters
    get_anthropic_tools("chat")
    get_openai_rest_tools("chat")
    get_openai_realtime_tools("realtime")
    get_gemini_rest_tools("chat")
    get_gemini_live_tools("gemini_live")
    after = json.dumps(TOOL_DEFINITIONS, sort_keys=True, default=str)
    assert before == after, "Format converters mutated TOOL_DEFINITIONS!"


# =============================================================================
# Specific Tool Tests
# =============================================================================

def test_search_snapshots_in_all_groups():
    """search_snapshots should be available everywhere."""
    for group in ["chat", "chat_cu", "realtime", "gemini_live", "grok_live", "phone", "mcp"]:
        names = get_group_tool_names(group)
        assert "search_snapshots" in names, f"search_snapshots missing from {group}"


def test_use_computer_not_in_chat_cu():
    """use_computer must not be in chat_cu (CU agent has native control)."""
    names = get_group_tool_names("chat_cu")
    assert "use_computer" not in names


def test_communication_tools_not_in_mcp():
    """Phone/SMS tools should not be in MCP (Claude Code doesn't make calls)."""
    mcp_names = get_group_tool_names("mcp")
    assert "send_sms" not in mcp_names
    assert "make_phone_call" not in mcp_names
    assert "make_voice_call" not in mcp_names


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
