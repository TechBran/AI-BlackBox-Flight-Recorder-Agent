"""Executor for create_cron_job (migrated from blackbox_tools._execute_create_cron_job).

Also hosts the small cron model/provider helpers shared with edit_cron_job
(imported there) so there is one source of truth for: normalizing a provider
word to its canonical stored key, validating a chosen specific model id
against the live /models/{provider} catalog (M4.2b), and refusing a model that
obviously belongs to a different provider than the job's.
"""
import logging

from Orchestrator.toolvault.context import ToolContext, ToolResult

logger = logging.getLogger(__name__)


# Provider WORD (what the AI/schema uses) -> the canonical stored provider key
# (what the cron executor sends to /chat and what /models/{key} is keyed on).
# gemini->google, claude->anthropic, grok->xai; openai/computer-use/custom
# unchanged. The catalog keys are also accepted verbatim (idempotent
# normalization).
_PROVIDER_WORD_TO_KEY = {
    "gemini": "google",
    "google": "google",
    "claude": "anthropic",
    "anthropic": "anthropic",
    "openai": "openai",
    "gpt": "openai",
    "grok": "xai",
    "xai": "xai",
    "computer-use": "computer-use",
    "cu": "computer-use",
    "custom": "custom",
}

# Bare provider words that are a DEFAULT selector (not a specific id) -- these
# are never validated against the catalog (they resolve to a provider default).
_BARE_PROVIDER_WORDS = set(_PROVIDER_WORD_TO_KEY.keys())

# Providers whose specific ids are single-vendor, so _model_to_provider can
# tell a foreign id apart. computer-use runs claude/gpt/gemini CU ids and custom
# servers host arbitrary ids, so neither is checked for specific ids.
_SINGLE_VENDOR_KEYS = ("google", "anthropic", "openai", "xai")


def _normalize_provider_word(provider):
    """Map a provider word to its canonical stored key, or None when blank.

    Unknown words pass through lowercased (defense-in-depth: never silently
    drop a value the caller meant)."""
    if not provider:
        return None
    p = provider.strip().lower()
    if not p:
        return None
    return _PROVIDER_WORD_TO_KEY.get(p, p)


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


def _obvious_owner(model):
    """The provider key ``model`` unmistakably belongs to, or None.

    A bare provider word maps to its key; a specific id goes through the
    scheduler's _model_to_provider, except its google catch-all (an id it can
    only place there by default is not a positive match)."""
    m = (model or "").strip().lower()
    if not m:
        return None
    if m in _BARE_PROVIDER_WORDS:
        return _PROVIDER_WORD_TO_KEY[m]
    from Orchestrator.scheduler.executor import _model_to_provider
    owner = _model_to_provider(m)
    if owner == "google" and "gemini" not in m:
        return None
    return owner


def _foreign_model_error(model, provider_word):
    """Return an error message when ``model`` OBVIOUSLY belongs to a provider
    other than ``provider_word``, else None.

    Such a pairing 404s on every fire: a bare provider word ("gemini") resolves
    to ITS provider's default regardless of the stored provider
    (scheduler.executor._resolve_model_name), and a specific id is sent
    verbatim to the stored provider's API. computer-use is never checked (it
    legitimately runs other vendors' models), and an id _model_to_provider can
    only place via its google catch-all is not 'obvious'.
    """
    provider_key = _normalize_provider_word(provider_word)
    m = (model or "").strip().lower()
    if not m or provider_key not in _SINGLE_VENDOR_KEYS + ("custom",):
        return None
    if provider_key == "custom" and m not in _BARE_PROVIDER_WORDS:
        return None  # custom: any specific id may be a registry model
    owner = _obvious_owner(m)
    if owner is None or owner == provider_key:
        return None
    return (
        f"Model '{model.strip()}' belongs to provider {owner}, not {provider_key}. "
        f"Set provider to {owner} as well, or leave model empty for the "
        f"{provider_key} default."
    )


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

        # An omitted model follows the provider: '' is Auto, which the scheduler
        # resolves to THIS provider's default at fire time. A call with neither
        # keeps the legacy "gemini" default (the manager backfills provider
        # google from it).
        model = params.get("model")
        if model is None:
            model = "" if provider else "gemini"

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
