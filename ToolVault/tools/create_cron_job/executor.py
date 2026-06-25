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


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Create a new cron job."""
    try:
        provider = _normalize_provider_word(params.get("provider"))

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
