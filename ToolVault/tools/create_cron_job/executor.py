"""Executor for create_cron_job (migrated from blackbox_tools._execute_create_cron_job).

Also hosts the live-catalog model check shared with edit_cron_job (imported
there): a chosen specific model id must resolve in its provider's GET
/models/{provider} catalog (M4.2b). The provider/model PAIRING rules
(normalizing a provider word to its stored key, refusing a model that
obviously belongs to another provider, Auto for an omitted model) live in
Orchestrator/scheduler/pairing.py and are applied by CronJobManager itself,
so the HTTP API gets them too (audit O15-scheduler-admin-05); the catalog
check below reuses them.
"""
import logging

from Orchestrator.scheduler.pairing import (
    BARE_PROVIDER_WORDS as _BARE_PROVIDER_WORDS,
    foreign_model_error as _foreign_model_error,
    normalize_provider_word as _normalize_provider_word,
)
from Orchestrator.toolvault.context import ToolContext, ToolResult

logger = logging.getLogger(__name__)


def _fetch_catalog_models(provider_key, operator=None):
    """Return the live model list (list of {"id","name"}) for a provider key.

    Lazy import of the in-process catalog handler from admin_routes -- deferred
    to call time so importing this executor module never drags in the heavy
    admin_routes/app bootstrap at registry-load time (avoids any import cycle).
    Returns the catalog dict (with a "models" list); the caller is responsible
    for treating a raise/empty as 'unknown -> graceful allow'.
    """
    from Orchestrator.routes.admin_routes import get_available_models
    return get_available_models(provider_key, operator)


def _validate_model(model, provider_word, operator=None):
    """Defense-in-depth check: a chosen SPECIFIC model id must resolve in its
    provider's live catalog. Returns (ok, error_message).

    Passes (ok=True) for:
      * empty / whitespace model (Auto -> provider default at fire time);
      * a bare provider word ("claude"/"gemini"/...) -- a default selector;
      * a catalog fetch that raises / returns empty (just-released id or a
        transient outage -- never block on infrastructure).
    Returns (False, msg) when the catalog was fetched successfully and the id
    is genuinely absent (a typo), or when the model obviously belongs to a
    different provider than ``provider_word`` (_foreign_model_error) -- so it
    fails LOUDLY here, not at fire time.
    """
    m = (model or "").strip()
    if not m:
        return True, None  # Auto
    if m.lower() in _BARE_PROVIDER_WORDS:
        # bare provider word -> ITS provider's default, so it must match
        err = _foreign_model_error(m, provider_word)
        return err is None, err

    provider_key = _normalize_provider_word(provider_word)
    if not provider_key:
        # No explicit provider word: derive the provider FROM the model id
        # (claude-*->anthropic, gpt-*->openai, gemini-*->google, unknown->google)
        # so a bogus id with no provider is validated against the right catalog.
        # Treating the id itself as a provider word would fetch /models/<id> -> a
        # 404 -> graceful-allow, silently skipping the typo check in the common
        # "id only, no provider" path. Lazy import avoids any cycle.
        from Orchestrator.scheduler.executor import _model_to_provider
        provider_key = _model_to_provider(m)

    try:
        catalog = _fetch_catalog_models(provider_key, operator)
        ids = [entry.get("id") for entry in (catalog or {}).get("models", [])]
    except Exception as e:
        logger.debug(
            "cron model validation: catalog fetch for provider=%s failed (%s) -- "
            "allowing model=%r (graceful fallback)", provider_key, e, m,
        )
        err = _foreign_model_error(m, provider_word)
        return err is None, err  # graceful allow unless obviously foreign
    if not ids:
        logger.debug(
            "cron model validation: empty catalog for provider=%s -- allowing "
            "model=%r (graceful fallback)", provider_key, m,
        )
        err = _foreign_model_error(m, provider_word)
        return err is None, err  # empty catalog -> allow unless obviously foreign

    if m in ids:
        return True, None

    return False, _foreign_model_error(m, provider_word) or (
        f"Unknown model '{m}' for provider {provider_word}. "
        f"Pick one from /models/{provider_key}, or leave model empty for the default."
    )


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Create a new cron job."""
    try:
        provider = _normalize_provider_word(params.get("provider"))

        # An omitted model is Auto: the manager pairs it with the provider
        # (THIS provider's default at fire time, or the box's default provider
        # when neither is given -- never "gemini" whatever the provider, W18).
        model = params.get("model")

        # M4.2b: validate a chosen specific model id against the live catalog so
        # a typo fails LOUDLY here, not silently at fire time. Graceful on any
        # catalog-fetch failure (see _validate_model).
        ok, err = _validate_model(model, params.get("provider"), ctx.operator)
        if not ok:
            return ToolResult(False, err)

        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        job = manager.create_job(
            name=params.get("name", "Unnamed Task"),
            prompt=params.get("prompt", ""),
            schedule=params.get("schedule", ""),
            operator=ctx.operator,
            frequency_hint=params.get("frequency_hint"),
            model=model,
            provider=provider,
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
