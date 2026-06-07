"""Executor for edit_cron_job (migrated from blackbox_tools._execute_edit_cron_job)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Edit an existing cron job."""
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        job_id = params.pop("job_id", None)
        if not job_id:
            return ToolResult(False, "job_id is required")

        # Handle pause/resume
        if "pause" in params:
            if params.pop("pause"):
                job = manager.pause_job(job_id)
                if job:
                    return ToolResult(True, f"Cron job '{job['name']}' paused.", data={"job": job})
            else:
                job = manager.resume_job(job_id)
                if job:
                    return ToolResult(True, f"Cron job '{job['name']}' resumed.", data={"job": job})
            return ToolResult(False, "Job not found")

        # Update other fields
        updates = {k: v for k, v in params.items() if v is not None}
        job = manager.update_job(job_id, **updates)
        if not job:
            return ToolResult(False, f"Job not found: {job_id}")
        return ToolResult(
            success=True,
            result=f"Cron job '{job['name']}' updated.",
            data={"job": job}
        )
    except Exception as e:
        return ToolResult(False, f"Edit cron job error: {str(e)}")
