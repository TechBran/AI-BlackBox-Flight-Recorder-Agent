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

    Returns (openai_ok, google_ok).
    """
    try:
        from dotenv import dotenv_values
        from Orchestrator.onboarding.secrets_writer import ENV_FILE
        env = dotenv_values(str(ENV_FILE))
    except Exception:
        # .env read failed — degrade gracefully to the frozen consts.
        return config.STT_OPENAI_AVAILABLE, config.STT_GOOGLE_AVAILABLE

    openai_ok = bool((env.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip())
    creds = (env.get("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    google_ok = bool(creds and os.path.exists(creds))
    return openai_ok, google_ok


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


def resolve_stt_provider(provided=None, *, openai_ok=None, google_ok=None):
    """Return 'openai' | 'google' | None.

    Explicit choice wins if its credential is available; otherwise the single
    available provider; otherwise None. When both are available and no explicit
    choice is given, prefers 'openai' (documented tie-break).

    Runtime path (openai_ok/google_ok None): availability is sourced live from
    stt_availability(), AND when the caller passes no provider (the client always
    sends ""), the saved STT_PROVIDER is fresh-read from .env — so the onboarding
    wizard selection is honored immediately, no restart. The kwargs (set by tests)
    bypass both fresh reads so the pure resolution logic stays deterministic."""
    runtime = openai_ok is None or google_ok is None
    provided = (provided or "").strip().lower()
    if not provided and runtime:
        provided = _fresh_stt_provider()
    if runtime:
        live_openai, live_google = stt_availability()
        openai_ok = live_openai if openai_ok is None else openai_ok
        google_ok = live_google if google_ok is None else google_ok
    avail = {"openai": openai_ok, "google": google_ok}
    if provided in avail and avail[provided]:
        return provided
    live = [p for p, ok in avail.items() if ok]
    return live[0] if live else None
