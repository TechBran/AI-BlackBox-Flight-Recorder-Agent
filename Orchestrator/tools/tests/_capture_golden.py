"""One-shot golden capture for the tool_registry parity test.

Run with the CURRENT (literal TOOL_DEFINITIONS) tool_registry BEFORE the
module-registry cutover. Serializes every provider converter's output, filtered
to drop UGV tools and sorted by name, into golden_tool_schemas.json.

This is the "before" snapshot of exactly what every provider sees (minus UGV).
The parity test asserts the POST-change output equals this byte-for-byte.

Usage:
    Orchestrator/venv/bin/python -m Orchestrator.tools.tests._capture_golden
"""

import json
import sys
import types
from pathlib import Path


def _install_mcp_stub() -> None:
    """Provide a minimal ``mcp.types.Tool`` so ``get_mcp_tools()`` runs without
    the real ``mcp`` package (not installed in this venv — the MCP server runs in
    a separate environment). ``to_mcp`` only reads ``.name/.description/
    .inputSchema`` back off the object, so a tiny dataclass-like shim is faithful.
    """
    if "mcp.types" in sys.modules or "mcp" in sys.modules:
        return
    try:
        import mcp.types  # noqa: F401 — real package present, use it.
        return
    except Exception:
        pass

    class Tool:  # minimal stand-in for mcp.types.Tool
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

GOLDEN_PATH = Path(__file__).parent / "golden_tool_schemas.json"


def _is_ugv(name: str) -> bool:
    return name.startswith("ugv_")


def _filter_anthropic(tools):
    """Anthropic / realtime / mcp-list-of-dicts: name field is top-level 'name'."""
    return sorted(
        (t for t in tools if not _is_ugv(t["name"])),
        key=lambda t: t["name"],
    )


def _filter_openai_rest(tools):
    """OpenAI REST: name under function.name."""
    return sorted(
        (t for t in tools if not _is_ugv(t["function"]["name"])),
        key=lambda t: t["function"]["name"],
    )


def _filter_gemini(tools):
    """Gemini REST / Live: single wrapper dict with function_declarations /
    functionDeclarations list. Filter + sort the inner declaration list, keep
    the wrapper shape identical."""
    out = []
    for wrapper in tools:
        new_wrapper = dict(wrapper)
        for key in ("function_declarations", "functionDeclarations"):
            if key in new_wrapper:
                decls = new_wrapper[key]
                new_wrapper[key] = sorted(
                    (d for d in decls if not _is_ugv(d["name"])),
                    key=lambda d: d["name"],
                )
        out.append(new_wrapper)
    return out


def _mcp_to_dicts(tools):
    """MCP Tool() objects -> plain dicts (name, description, inputSchema)."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.inputSchema,
        }
        for t in tools
    ]


def build_golden() -> dict:
    return {
        "anthropic_chat": _filter_anthropic(tr.get_anthropic_tools("chat")),
        "openai_rest_chat": _filter_openai_rest(tr.get_openai_rest_tools("chat")),
        "gemini_rest_chat": _filter_gemini(tr.get_gemini_rest_tools("chat")),
        "openai_realtime": _filter_anthropic(
            tr.get_openai_realtime_tools("realtime")
        ),
        "gemini_live": _filter_gemini(tr.get_gemini_live_tools("gemini_live")),
        "mcp": _filter_anthropic(_mcp_to_dicts(tr.get_mcp_tools())),
    }


if __name__ == "__main__":
    golden = build_golden()
    GOLDEN_PATH.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")

    # Report counts per format for the operator.
    print(f"Wrote {GOLDEN_PATH}")
    for fmt in ("anthropic_chat", "openai_rest_chat", "openai_realtime", "mcp"):
        print(f"  {fmt}: {len(golden[fmt])} non-ugv tools")
    for fmt in ("gemini_rest_chat", "gemini_live"):
        decls = golden[fmt][0].get("function_declarations") or golden[fmt][0].get(
            "functionDeclarations"
        )
        print(f"  {fmt}: {len(decls)} non-ugv declarations")
