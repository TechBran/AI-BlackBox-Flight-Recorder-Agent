"""Executor for edit_cron_job (migrated from blackbox_tools._execute_edit_cron_job)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult

# Shared cron model/provider helpers live in create_cron_job's executor (one
# source of truth); import them so edit validates identically.
from ToolVault.tools.create_cron_job.executor import (
    _normalize_provider_word,
    _validate_model,
)


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Edit an existing cron job."""
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        job_id = params.pop("job_id", None)
        if not job_id:
            return ToolResult(False, "job_id is required")

        # Operator-ownership scoping (M2.5): only the owning operator (or the
        # 'system' operator) may mutate a job. A non-owner -- or a non-existent
        # job -- gets a GENERIC "Job not found" so the tool never leaks the
        # existence of another operator's job.
        existing = manager.get_job(job_id)
        if existing is None or (
            ctx.operator != "system" and existing.get("operator") != ctx.operator
        ):
            return ToolResult(False, "Job not found")

        # M4.2b: when the edit sets a model, validate the chosen specific id
        # against the live catalog so a typo fails LOUDLY here, not at fire time.
        # Provider for the check: the (normalized) provider in THIS call if given,
        # else the job's STORED provider -- so a model-only edit consults the
        # right catalog. Graceful on any catalog-fetch failure (_validate_model).
        if params.get("model") is not None:
            provider_for_check = params.get("provider") or existing.get("provider")
            ok, err = _validate_model(
                params.get("model"), provider_for_check, ctx.operator
            )
            if not ok:
                return ToolResult(False, err)

        # Normalize a provided provider word to its canonical stored key
        # (gemini->google, claude->anthropic, grok->xai) so the stored provider
        # matches what the cron executor + /chat expect.
        if params.get("provider") is not None:
            params["provider"] = _normalize_provider_word(params.get("provider"))

        # Translate pause/resume into a status update and fall through to the
        # SINGLE update_job path (M2.4). update_job whitelists `status` and
        # re-registers with APScheduler, so one call can both resume/pause AND
        # change schedule/prompt/etc. -- no early-return that drops field edits.
        if "pause" in params:
            updates_pause = params.pop("pause")
            params["status"] = "paused" if updates_pause else "active"

        # Update fields (status, schedule, prompt, ...) in one update_job call.
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
