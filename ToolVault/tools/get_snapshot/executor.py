"""Executor for get_snapshot (migrated from blackbox_tools._execute_get_snapshot)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Retrieve a specific snapshot by ID."""
    snap_id = params.get("snap_id", "")

    if not snap_id:
        return ToolResult(False, "Snapshot ID is required")

    try:
        from Orchestrator.fossils import get_snapshot_by_id

        result = get_snapshot_by_id(snap_id)

        if not result:
            return ToolResult(False, f"Snapshot not found: {snap_id}")

        content = result.get("content", "")
        metadata = result.get("metadata", {})

        # Format the response
        output = f"Snapshot {snap_id}:\n"
        output += f"Operator: {metadata.get('operator', 'unknown')}\n"
        output += f"Timestamp: {metadata.get('timestamp', 'unknown')}\n"
        output += f"Type: {metadata.get('type', 'normal')}\n"
        output += f"\n--- Content ---\n{content}"

        return ToolResult(
            success=True,
            result=output,
            data=result
        )

    except Exception as e:
        return ToolResult(False, f"Get snapshot error: {str(e)}")
