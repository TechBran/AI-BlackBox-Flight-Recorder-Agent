"""ToolVault v2 execution context + result contract.

ToolContext carries the minimal per-call state (operator, base_url), mirroring
BlackBoxToolExecutor.__init__. ToolResult is re-exported (NOT copied) from
blackbox_tools so the canonical class object is shared — no circular import
because blackbox_tools' only toolvault import is lazy (function-local).
"""

from dataclasses import dataclass

# Re-export: same class object as Orchestrator.tools.blackbox_tools.ToolResult.
from Orchestrator.tools.blackbox_tools import ToolResult  # noqa: F401


@dataclass
class ToolContext:
    """Per-call execution context passed to module executors."""
    operator: str = "system"
    base_url: str = "http://localhost:9091"
