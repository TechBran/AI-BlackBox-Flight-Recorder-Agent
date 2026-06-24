"""Executor for search_cron_jobs (migrated from blackbox_tools._execute_search_cron_jobs)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Search/list cron jobs."""
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        status_filter = params.get("status", "all")
        query = params.get("query", "")

        jobs = manager.list_jobs(
            operator=ctx.operator,
            status=None if status_filter == "all" else status_filter
        )

        # Filter by query if provided
        if query:
            query_lower = query.lower()
            jobs = [j for j in jobs if query_lower in j.get("name", "").lower()
                    or query_lower in j.get("prompt", "").lower()]

        if not jobs:
            return ToolResult(True, "No cron jobs found.", data={"jobs": []})

        # Format results
        lines = [f"Found {len(jobs)} cron job(s):\n"]
        for j in jobs:
            status_icon = {"active": "[ACTIVE]", "paused": "[PAUSED]"}.get(j["status"], "[?]")
            hint = j.get("frequency_hint") or j["schedule"]
            lines.append(f"{status_icon} {j['name']} (ID: {j['id']})")
            lines.append(f"   Schedule: {hint} | Delivery: {j['delivery']}")
            lines.append(f"   Prompt: {j['prompt'][:100]}{'...' if len(j.get('prompt','')) > 100 else ''}")
            if j.get("last_run_at"):
                # Surface the run outcome so the AI can answer "did the 7am job
                # fail?" without a separate history lookup (M2.7).
                outcome = j.get("last_run_result")
                outcome_str = f" ({outcome})" if outcome else ""
                duration = j.get("last_run_duration_ms")
                dur_str = f", {duration}ms" if duration is not None else ""
                lines.append(f"   Last run: {j['last_run_at']}{outcome_str}{dur_str}")
            errors = j.get("error_count")
            if errors:
                lines.append(f"   Errors: {errors}")
            lines.append("")

        return ToolResult(True, "\n".join(lines), data={"jobs": jobs})
    except Exception as e:
        return ToolResult(False, f"Search cron jobs error: {str(e)}")
