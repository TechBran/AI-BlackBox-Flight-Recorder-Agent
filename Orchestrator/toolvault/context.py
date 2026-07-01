"""ToolVault v2 execution context + result contract.

ToolContext carries the minimal per-call state (operator, base_url), mirroring
BlackBoxToolExecutor.__init__.

ToolResult is defined canonically HERE (and re-exported by blackbox_tools) so the
toolvault package has NO import-time dependency on the tools package. This breaks
the import cycle introduced once tool_registry sources its TOOL_DEFINITIONS from
the toolvault registry: blackbox_tools (import-time get_anthropic_tools) ->
tool_registry -> toolvault.registry -> resolvers -> context. If context imported
ToolResult back from blackbox_tools, that chain would deadlock mid-import.
(See Task 0.1's documented fallback.)
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ToolResult:
    """Result from executing a tool. Canonical home; re-exported by blackbox_tools."""
    success: bool
    result: str
    data: Optional[Dict[str, Any]] = None

    def rich_result(self) -> str:
        """Return result string enriched with structured data for model consumption."""
        if self.data:
            import json
            return f"{self.result}\n[tool_data]: {json.dumps(self.data, default=str)}"
        return self.result


@dataclass
class ToolContext:
    """Per-call execution context passed to module executors.

    ``origin_device_id`` (M3) is the tailnet identity of the device the request
    ORIGINATED from — a hostname, MagicDNS name, or tailnet IPv4. It is threaded
    through so origin-aware device-control routing (``mesh.resolve_device``) can
    default the control target to the originating device. It is None for
    non-device surfaces (the box/Portal and remote MCP), which then resolve to the
    operator's PRIMARY device. Back-compat default None; JSON-serializable.
    """
    operator: str = "system"
    base_url: str = "http://localhost:9091"
    origin_device_id: Optional[str] = None
