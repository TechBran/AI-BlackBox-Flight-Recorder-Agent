"""Executor for list_recent_snapshots (migrated from blackbox_tools._execute_list_recent_snapshots)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Get the most recent snapshots for quick context catch-up."""
    count = min(params.get("count", 5), 10)  # Cap at 10

    try:
        from Orchestrator.fossils import get_recent_fossils_for_operator

        # Get recent snapshots (cap each at 10000 chars for readability). Only
        # their byte ranges are read; no whole-volume text is needed.
        snapshots = get_recent_fossils_for_operator("", ctx.operator, count, cap_chars_each=10000)

        if not snapshots:
            return ToolResult(
                success=True,
                result=f"No recent snapshots found for operator: {ctx.operator}"
            )

        # Format output
        output = f"Recent {len(snapshots)} snapshot(s) for {ctx.operator}:\n\n"
        for i, snap in enumerate(snapshots, 1):
            output += f"--- Snapshot {i} ---\n{snap}\n\n"

        return ToolResult(
            success=True,
            result=output,
            data={"count": len(snapshots)}
        )

    except Exception as e:
        return ToolResult(False, f"List recent snapshots error: {str(e)}")
