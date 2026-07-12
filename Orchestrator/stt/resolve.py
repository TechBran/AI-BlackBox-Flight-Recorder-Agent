import os

from Orchestrator import config


def stt_availability():
    """Live (fresh-read) STT credential availability, so the wizard/resolver
    reflect just-saved keys without a restart (mirrors onboarding current-config E8).

    Reads .env fresh via dotenv_values(ENV_FILE) — the SAME mechanism/path
    current-config uses — so a key/credential saved by the onboarding /save
    endpoint is reflected immediately, without the stale module-level config
    consts (computed once at import from os.environ). Crash-proof: if the .env
    read fails for any reason, falls back to os.environ, then to the frozen
    config consts, rather than throwing.

    Returns (openai_ok, google_ok, elevenlabs_ok).
    """
    from Orchestrator.elevenlabs.client import resolve_api_key
    try:
        from dotenv import dotenv_values
        from Orchestrator.onboarding.secrets_writer import ENV_FILE
        env = dotenv_values(str(ENV_FILE))
    except Exception:
        # .env read failed — degrade gracefully to the frozen consts.
        # resolve_api_key() is itself crash-proof (own try/except, os.environ fallback).
        return config.STT_OPENAI_AVAILABLE, config.STT_GOOGLE_AVAILABLE, bool(resolve_api_key())

    openai_ok = bool((env.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip())
    creds = (env.get("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    google_ok = bool(creds and os.path.exists(creds))
    elevenlabs_ok = bool(resolve_api_key())
    return openai_ok, google_ok, elevenlabs_ok


def local_stt_available() -> bool:
    """True iff a registered custom server hosts a speech-to-text model.
    Lazy + fail-soft so a registry hiccup never breaks STT resolution."""
    try:
        from Orchestrator.onboarding.custom_servers import has_modality_model
        return has_modality_model("stt")
    except Exception:
        return False


def _fresh_stt_provider():
    """Fresh-read the saved STT_PROVIDER (wizard selection) from .env so it takes
    effect WITHOUT a service restart. The web/Android client sends provider:""
    and lets the backend resolve, so this is what actually honors the wizard
    pick. Falls back to the frozen config const if the .env read fails."""
    try:
        from dotenv import dotenv_values
        from Orchestrator.onboarding.secrets_writer import ENV_FILE
        env = dotenv_values(str(ENV_FILE))
        return (env.get("STT_PROVIDER") or config.STT_PROVIDER or "").strip().lower()
    except Exception:
        return (config.STT_PROVIDER or "").strip().lower()


def resolve_stt_provider(provided=None, *, openai_ok=None, google_ok=None, elevenlabs_ok=None, local_ok=None):
    """Return 'openai' | 'google' | 'elevenlabs' | 'local' | None.

    Explicit choice wins if its credential is available; otherwise the single
    available provider; otherwise None. When multiple are available and no explicit
    choice is given, prefers 'openai' (documented tie-break, dict-insertion order).

    Runtime path (openai_ok/google_ok None): availability is sourced live from
    stt_availability(), AND when the caller passes no provider (the client always
    sends ""), the saved STT_PROVIDER is fresh-read from .env — so the onboarding
    wizard selection is honored immediately, no restart. The kwargs (set by tests)
    bypass both fresh reads so the pure resolution logic stays deterministic;
    elevenlabs_ok left None in that pure path defaults to False (provider absent)."""
    runtime = openai_ok is None or google_ok is None
    provided = (provided or "").strip().lower()
    if not provided and runtime:
        provided = _fresh_stt_provider()
    if runtime:
        live_openai, live_google, live_elevenlabs = stt_availability()
        openai_ok = live_openai if openai_ok is None else openai_ok
        google_ok = live_google if google_ok is None else google_ok
        elevenlabs_ok = live_elevenlabs if elevenlabs_ok is None else elevenlabs_ok
        local_ok = local_stt_available() if local_ok is None else local_ok
    # local STT is file-only + a fallback, so it sits LAST in the tie-break order.
    avail = {"openai": openai_ok, "google": google_ok,
             "elevenlabs": bool(elevenlabs_ok), "local": bool(local_ok)}
    if provided in avail and avail[provided]:
        return provided
    live = [p for p, ok in avail.items() if ok]
    return live[0] if live else None
