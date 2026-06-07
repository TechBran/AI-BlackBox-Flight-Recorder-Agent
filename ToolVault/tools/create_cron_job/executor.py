"""Executor for create_cron_job (migrated from blackbox_tools._execute_create_cron_job)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Create a new cron job."""
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        job = manager.create_job(
            name=params.get("name", "Unnamed Task"),
            prompt=params.get("prompt", ""),
            schedule=params.get("schedule", ""),
            operator=ctx.operator,
            frequency_hint=params.get("frequency_hint"),
            model=params.get("model", "gemini"),
            delivery=params.get("delivery", "snapshot"),
            delivery_target=params.get("delivery_target"),
            one_shot=params.get("one_shot", False)
        )
        hint = job.get("frequency_hint") or job["schedule"]
        return ToolResult(
            success=True,
            result=f"Cron job created: '{job['name']}' (ID: {job['id']}). Schedule: {hint}. Delivery: {job['delivery']}.",
            data={"job": job}
        )
    except ValueError as e:
        return ToolResult(False, f"Invalid cron job: {str(e)}")
    except Exception as e:
        return ToolResult(False, f"Create cron job error: {str(e)}")
