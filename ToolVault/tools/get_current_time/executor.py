"""Executor for get_current_time (migrated from blackbox_tools._execute_get_current_time)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Get the current date and time."""
    from datetime import datetime
    now = datetime.now()

    return ToolResult(
        success=True,
        result=f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}",
        data={"iso": now.isoformat(), "unix": now.timestamp()}
    )
