"""Executor for gemini_cli_task — thin wrapper over the shared CLI-agent launch.

Auth fail-fast + fully-open (YOLO) task launch live in
Orchestrator.cli_agent.tool_support.launch (mirrors use_computer's shape). See
that module for the honest per-provider auth check and the failure-payload
contract (structured JSON in BOTH .result and .data)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.cli_agent.tool_support import launch


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    return await launch("gemini", params, ctx)
