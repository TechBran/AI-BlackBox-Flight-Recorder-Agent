"""Executor for edit_cron_job (migrated from blackbox_tools._execute_edit_cron_job)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult

# Shared cron model/provider helpers live in create_cron_job's executor (one
# source of truth); import them so edit validates identically.
from ToolVault.tools.create_cron_job.executor import (
    _BARE_PROVIDER_WORDS,
    _foreign_model_error,
    _normalize_provider_word,
    _obvious_owner,
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

        # Normalize a provided provider word to its canonical stored key
        # (gemini->google, claude->anthropic, grok->xai) so the stored provider
        # matches what the cron executor + /chat expect. Done FIRST so a blank
        # provider ('  ') counts as omitted in every check below.
        if params.get("provider") is not None:
            params["provider"] = _normalize_provider_word(params.get("provider"))

        # Schema: provider is "derived from the model" when omitted. A model-only
        # edit naming another vendor's model switches the job to that vendor --
        # rows with no stored provider report one derived from the OLD model, so
        # validating against it would refuse the switch that used to work.
        if params.get("provider") is None and params.get("model") is not None and (
            _foreign_model_error(params.get("model"), existing.get("provider"))
        ):
            params["provider"] = _obvious_owner(params.get("model"))

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

        # A provider switch with no model in the call resets the model to Auto
        # ('' -> the new provider's default at fire time) so the old provider's
        # model can't ride along (e.g. "gemini" under anthropic 404s every
        # fire). Restating the same provider also repairs a stored BARE provider
        # word that belongs elsewhere (the shape the old "gemini" default wrote);
        # specific ids are left alone -- the substring heuristic can misplace them.
        new_provider = params.get("provider")
        stored_model = (existing.get("model") or "").strip().lower()
        if new_provider and params.get("model") is None and (
            new_provider != existing.get("provider")
            or (stored_model in _BARE_PROVIDER_WORDS
                and _foreign_model_error(stored_model, new_provider))
        ):
            params["model"] = ""

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
