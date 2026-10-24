"""
Orchestrator/scheduler/pairing.py - a cron job's provider/model pairing rules.

One source for how a job's ``provider`` and ``model`` fit together, used by
CronJobManager.create_job / update_job, so every surface that writes a job
(the HTTP API behind the Portal and Android, the ToolVault cron tools, any
script) gets the same rules (audit O15-scheduler-admin-05, W18):

  * a provider WORD is stored as its catalog key (gemini->google,
    claude->anthropic, grok->xai, gpt->openai);
  * an omitted model is Auto ('' = the provider's configured default at fire
    time); with no provider either, the job runs on the box's default provider;
  * a model that obviously belongs to another provider is refused, because the
    pair 404s on every fire;
  * an edit that switches provider without naming a model resets the model to
    Auto, and an edit naming only another vendor's model moves the job to that
    vendor.

The live-catalog typo check (is this id in GET /models/{provider}?) needs a
provider round-trip and stays in the ToolVault tool layer.
"""

from typing import Any, Dict, Optional, Tuple

# Provider WORD (what a model, a person or a schema uses) -> the canonical stored
# key (what the executor sends to /chat and /models/{key} is keyed on). The
# catalog keys map to themselves, so normalization is idempotent.
PROVIDER_WORD_TO_KEY = {
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

# Bare provider words select that provider's DEFAULT; they are not model ids.
BARE_PROVIDER_WORDS = frozenset(PROVIDER_WORD_TO_KEY)

# Providers whose specific ids are single-vendor, so a foreign id can be told
# apart. computer-use runs claude/gpt/gemini CU ids and a custom server hosts
# arbitrary ids, so neither is checked for specific ids.
SINGLE_VENDOR_KEYS = ("google", "anthropic", "openai", "xai")

# The providers a cron job can run on (the /chat path). The interactive and
# device-bound providers (agents, realtime, local) are out of cron scope.
CRON_PROVIDER_KEYS = frozenset(SINGLE_VENDOR_KEYS + ("custom", "computer-use"))


def normalize_provider_word(provider: Any) -> Optional[str]:
    """Map a provider word to its canonical stored key, or None when blank.

    Unknown words pass through lowercased (never silently drop a value the
    caller meant)."""
    if provider is None:
        return None
    p = str(provider).strip().lower()
    if not p:
        return None
    return PROVIDER_WORD_TO_KEY.get(p, p)


def is_auto(model: Any) -> bool:
    """An empty/whitespace model is Auto (the provider default at fire time)."""
    return model is None or not str(model).strip()


def obvious_owner(model: Any) -> Optional[str]:
    """The provider key ``model`` unmistakably belongs to, or None.

    A bare provider word maps to its key. A specific id goes through the
    scheduler's substring heuristic (_model_to_provider), with two exceptions
    that make it 'not obvious': its google catch-all (an id it can only place
    there by default), and its computer-use mapping, which is a capability, not
    a vendor: a Gemini CU id is a Google catalog id, so the vendor is read from
    the rest of the id instead."""
    m = (str(model) if model is not None else "").strip().lower()
    if not m:
        return None
    if m in BARE_PROVIDER_WORDS:
        return PROVIDER_WORD_TO_KEY[m]
    # Lazy import keeps the manager<->executor edge out of import time.
    from Orchestrator.scheduler.executor import _model_to_provider

    owner = _model_to_provider(m)
    if owner == "computer-use":
        m = m.replace("computer-use", "")
        owner = _model_to_provider(m)
    if owner == "google" and "gemini" not in m:
        return None
    return owner


def foreign_model_error(model: Any, provider: Any) -> Optional[str]:
    """An error message when ``model`` OBVIOUSLY belongs to a provider other
    than ``provider`` (a word or a key), else None.

    Such a pair 404s on every fire: a bare provider word ("gemini") would
    resolve to ITS provider's default, and a specific id is sent verbatim to
    the stored provider's API. computer-use is never checked (it runs other
    vendors' models), nor is a specific id on a custom server."""
    provider_key = normalize_provider_word(provider)
    m = (str(model) if model is not None else "").strip().lower()
    if not m or provider_key not in SINGLE_VENDOR_KEYS + ("custom",):
        return None
    if provider_key == "custom" and m not in BARE_PROVIDER_WORDS:
        return None
    owner = obvious_owner(m)
    if owner is None or owner == provider_key:
        return None
    return (
        f"Model '{str(model).strip()}' belongs to provider {owner}, not {provider_key}. "
        f"Set provider to {owner} as well, or leave model empty for the "
        f"{provider_key} default."
    )


def default_provider() -> str:
    """The box's default chat provider as a cron provider key.

    config.DEFAULT_PROVIDER (explicit setting, else the first provider this box
    holds a key for), read at call time. A value outside cron scope falls back
    to config's own last resort, google."""
    from Orchestrator import config

    key = normalize_provider_word(getattr(config, "DEFAULT_PROVIDER", "")) or "google"
    return key if key in CRON_PROVIDER_KEYS else "google"


def pair_for_create(provider: Any, model: Any) -> Tuple[Optional[str], str]:
    """The (provider, model) to store for a new job. Raises ValueError.

    provider None stays None only for a specific model id, whose provider the
    manager derives from the id on read (legacy behaviour). A bare word is
    stored with its own key, and Auto with no provider runs on the box default.
    """
    provider_key = normalize_provider_word(provider)
    stored_model = "" if model is None else str(model)
    if provider_key is None:
        if is_auto(stored_model):
            return default_provider(), stored_model
        word_key = PROVIDER_WORD_TO_KEY.get(stored_model.strip().lower())
        return word_key, stored_model
    err = foreign_model_error(stored_model, provider_key)
    if err:
        raise ValueError(err)
    return provider_key, stored_model


def pair_for_update(existing: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the pairing rules to a partial update of ``existing``.

    Returns a copy of ``updates`` with ``provider`` normalized (a blank one
    counts as omitted) and ``provider``/``model`` filled in where the rules
    move them. Raises ValueError for a foreign pair. Only the provider/model
    keys are touched; everything else passes through.
    """
    out = dict(updates)
    if "provider" in out:
        provider_key = normalize_provider_word(out["provider"])
        if provider_key is None:
            out.pop("provider")
        else:
            out["provider"] = provider_key
    if "model" in out and out["model"] is None:
        out.pop("model")

    stored_provider = existing.get("provider")
    has_provider = "provider" in out
    has_model = "model" in out

    # A model-only edit naming another vendor's model moves the job to that
    # vendor. Rows with no stored provider report one derived from the OLD
    # model, so checking against it would refuse the switch that used to work.
    if not has_provider and has_model and foreign_model_error(out["model"], stored_provider):
        out["provider"] = obvious_owner(out["model"])
        has_provider = True

    if has_provider and has_model:
        err = foreign_model_error(out["model"], out["provider"])
        if err:
            raise ValueError(err)
    elif has_provider:
        # A provider switch with no model resets the model to Auto, so the old
        # provider's model can't ride along. Restating the same provider also
        # repairs a stored BARE word that belongs elsewhere (the shape the old
        # "gemini" default wrote); a stored specific id is left alone, since the
        # substring heuristic can misplace one.
        stored_model = (existing.get("model") or "").strip().lower()
        if out["provider"] != stored_provider or (
            stored_model in BARE_PROVIDER_WORDS
            and foreign_model_error(stored_model, out["provider"])
        ):
            out["model"] = ""
    return out
