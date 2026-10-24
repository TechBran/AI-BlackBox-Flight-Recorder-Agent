"""Executor for edit_cron_job (migrated from blackbox_tools._execute_edit_cron_job)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult

# The provider/model pairing rules are the scheduler's (one source of truth,
# also applied by CronJobManager.update_job); the live-catalog model check is
# shared with create_cron_job, so edit validates identically.
from Orchestrator.scheduler.pairing import pair_for_update
from ToolVault.tools.create_cron_job.executor import _validate_model


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

        # Provider/model pairing (O15-05): a provider word is stored as its key
        # and a blank one counts as omitted; a model-only edit naming another
        # vendor's model moves the job to that vendor; a provider switch with
        # no model resets the model to Auto (and restating the provider repairs
        # a stored bare word that belongs elsewhere); a foreign pair is refused.
        # The manager applies the same rules in update_job; they run here first
        # so the catalog check below consults the provider the job will have.
        try:
            params = pair_for_update(existing, params)
        except ValueError as e:
            return ToolResult(False, str(e))

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
