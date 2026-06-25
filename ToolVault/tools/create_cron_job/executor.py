"""Executor for create_cron_job (migrated from blackbox_tools._execute_create_cron_job).

Also hosts the small cron model/provider helpers shared with edit_cron_job
(imported there) so there is one source of truth for: normalizing a provider
word to its canonical stored key, and validating a chosen specific model id
against the live /models/{provider} catalog (M4.2b).
"""
import logging

from Orchestrator.toolvault.context import ToolContext, ToolResult

logger = logging.getLogger(__name__)


# Provider WORD (what the AI/schema uses) -> the canonical stored provider key
# (what the cron executor sends to /chat and what /models/{key} is keyed on).
# gemini->google, claude->anthropic, grok->xai; openai/computer-use unchanged.
# The catalog keys are also accepted verbatim (idempotent normalization).
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
}

# Bare provider words that are a DEFAULT selector (not a specific id) -- these
# are never validated against the catalog (they resolve to a provider default).
_BARE_PROVIDER_WORDS = set(_PROVIDER_WORD_TO_KEY.keys())


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


def _validate_model(model, provider_word, operator=None):
    """Defense-in-depth check: a chosen SPECIFIC model id must resolve in its
    provider's live catalog. Returns (ok, error_message).

    Passes (ok=True) for:
      * empty / whitespace model (Auto -> provider default at fire time);
      * a bare provider word ("claude"/"gemini"/...) -- a default selector;
      * a catalog fetch that raises / returns empty (just-released id or a
        transient outage -- never block on infrastructure).
    Returns (False, msg) ONLY when the catalog was fetched successfully and the
    id is genuinely absent (a typo) -- so it fails LOUDLY here, not at fire time.
    """
    m = (model or "").strip()
    if not m:
        return True, None  # Auto
    if m.lower() in _BARE_PROVIDER_WORDS:
        return True, None  # bare provider word -> provider default

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
        return True, None  # graceful allow on any fetch failure
    if not ids:
        logger.debug(
            "cron model validation: empty catalog for provider=%s -- allowing "
            "model=%r (graceful fallback)", provider_key, m,
        )
        return True, None  # empty catalog -> allow

    if m in ids:
        return True, None

    return False, (
        f"Unknown model '{m}' for provider {provider_word}. "
        f"Pick one from /models/{provider_key}, or leave model empty for the default."
    )


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Create a new cron job."""
    try:
        provider = _normalize_provider_word(params.get("provider"))

        # M4.2b: validate a chosen specific model id against the live catalog so
        # a typo fails LOUDLY here, not silently at fire time. Graceful on any
        # catalog-fetch failure (see _validate_model).
        ok, err = _validate_model(
            params.get("model", ""), params.get("provider"), ctx.operator
        )
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
            model=params.get("model", "gemini"),
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
