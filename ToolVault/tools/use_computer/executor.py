"""Executor for use_computer (migrated from blackbox_tools._execute_use_computer)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Launch Computer Use agent (Claude Opus CU on Linux desktop)."""
    prompt = params.get("prompt", "")
    url = params.get("url")
    device_id = params.get("device_id", "blackbox")

    if not prompt:
        return ToolResult(False, "Prompt is required for use_computer")

    try:
        from Orchestrator.tasks import create_task
        from Orchestrator.models import TaskType
        result_data = {"device_id": device_id}
        if url:
            result_data["url"] = url
        task = create_task(
            TaskType.USE_COMPUTER,
            operator=ctx.operator,
            prompt=prompt,
            result_data=result_data
        )
        return ToolResult(
            True,
            f"Computer Use task started. Task ID: {task.task_id}. Use get_task_status to check progress.",
            data={"task_id": task.task_id}
        )
    except Exception as e:
        return ToolResult(False, f"Computer use error: {str(e)}")
