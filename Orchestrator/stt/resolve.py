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


def resolve_stt_provider(provided=None, *, openai_ok=None, google_ok=None):
    """Return 'openai' | 'google' | None.

    Explicit choice wins if its credential is available; otherwise the single
    available provider; otherwise None. When both are available and no explicit
    choice is given, prefers 'openai' (documented tie-break; the onboarding
    wizard always sets an explicit choice). The openai_ok/google_ok kwargs
    override availability (used by tests); when None, availability is sourced
    live from stt_availability() so just-saved credentials are honored without
    a restart."""
    provided = (provided if provided is not None else config.STT_PROVIDER or "").strip().lower()
    if openai_ok is None or google_ok is None:
        live_openai, live_google = stt_availability()
        openai_ok = live_openai if openai_ok is None else openai_ok
        google_ok = live_google if google_ok is None else google_ok
    avail = {"openai": openai_ok, "google": google_ok}
    if provided in avail and avail[provided]:
        return provided
    live = [p for p, ok in avail.items() if ok]
    return live[0] if live else None
