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

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Caller marker for the ON-DEVICE phone bridge (M8/WI-7a). /local/tools/execute
# stamps this when the request carries NO X-BlackBox-Caller header (the Android
# bridge sends none — fail-closed, mirroring the recursion-guard convention);
# the MCP gateway declares itself "mcp-gateway" and every other surface leaves
# caller=None. Window-bound executors (search_snapshots) key their delivery
# budget on this marker: the phone's 6,144-token engine window cannot take
# whole snapshots.
ON_DEVICE_CALLER = "local"


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


def _local_base_url() -> str:
    """Loopback base URL of the local Orchestrator: config.orchestrator_url()
    (ORCHESTRATOR_PORT) once the app has loaded config. This module also loads
    in the lean MCP venv, which never imports config; there the port comes
    from the ORCHESTRATOR_PORT env var, else the app's default."""
    from Orchestrator.utils.loopback import orchestrator_url  # stdlib-only
    return orchestrator_url()


@dataclass
class ToolContext:
    """Per-call execution context passed to module executors.

    ``origin_device_id`` (M3) is the tailnet identity of the device the request
    ORIGINATED from — a hostname, MagicDNS name, or tailnet IPv4. It is threaded
    through so origin-aware device-control routing (``mesh.resolve_device``) can
    default the control target to the originating device. It is None for
    non-device surfaces (the box/Portal and remote MCP), which then resolve to the
    operator's PRIMARY device. Back-compat default None; JSON-serializable.

    ``caller`` (M8/WI-7a) identifies the calling SURFACE, not the operator:
    ON_DEVICE_CALLER ("local") = the on-device phone bridge via
    /local/tools/execute; "mcp-gateway" = the remote MCP gateway on the same
    endpoint; None (default) = every other surface (chat loops, voice routes,
    scheduler, CU). Executors whose result size must respect a genuine window
    bound (search_snapshots) budget on it. Back-compat default None.

    ``surface`` and ``provider`` (M7) say which chat surface the tools are
    being offered to and which provider will read them, as the turn's entry
    point stamped them (Orchestrator/wellness/turn.py). The operator-scoped
    availability features (the wellness tools) cross-check them against the
    stamped turn and refuse on any mismatch. None = not known here (the
    dispatcher's own context, static arrays); back-compat default None.
    """
    operator: str = "system"
    base_url: str = field(default_factory=_local_base_url)
    origin_device_id: Optional[str] = None
    caller: Optional[str] = None
    surface: Optional[str] = None
    provider: Optional[str] = None
