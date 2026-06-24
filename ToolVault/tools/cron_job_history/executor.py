"""Executor for cron_job_history — past runs (outcome/duration) of a cron job.

Wraps CronJobManager.get_job_history. Operator-ownership-scoped (M2.5): only the
owning operator (or 'system') may read a job's history; a non-owner — or a
non-existent job — gets a generic "Job not found" so existence never leaks.
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Return the execution history (most recent first) for a cron job."""
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()

        job_id = params.get("job_id")
        if not job_id:
            return ToolResult(False, "job_id is required")

        # Operator-ownership scoping (M2.5): generic "Job not found" for a
        # non-owner or a missing job — no existence leak.
        job = manager.get_job(job_id)
        if job is None or (
            ctx.operator != "system" and job.get("operator") != ctx.operator
        ):
            return ToolResult(False, "Job not found")

        limit = params.get("limit", 20)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20

        rows = manager.get_job_history(job_id, limit=limit)

        if not rows:
            return ToolResult(
                True,
                f"No run history yet for cron job '{job['name']}'.",
                data={"history": []},
            )

        lines = [f"Run history for '{job['name']}' ({len(rows)} run(s), most recent first):\n"]
        for r in rows:
            outcome = r.get("result") or ("error" if r.get("error") else "?")
            outcome_icon = {"success": "[OK]", "error": "[FAIL]"}.get(outcome, "[?]")
            duration = r.get("duration_ms")
            dur_str = f"{duration}ms" if duration is not None else "n/a"
            lines.append(f"{outcome_icon} {r.get('run_at')} | {outcome} | {dur_str}")
            if r.get("error"):
                lines.append(f"   Error: {r['error']}")

        return ToolResult(True, "\n".join(lines), data={"history": rows})
    except Exception as e:
        return ToolResult(False, f"Cron job history error: {str(e)}")
