from Orchestrator import config

def resolve_stt_provider(provided=None, *, openai_ok=None, google_ok=None):
    """Return 'openai' | 'google' | None.

    Explicit choice wins if its credential is available; otherwise the single
    available provider; otherwise None. When both are available and no explicit
    choice is given, prefers 'openai' (documented tie-break; the onboarding
    wizard always sets an explicit choice). The openai_ok/google_ok kwargs
    override config (used by tests)."""
    provided = (provided if provided is not None else config.STT_PROVIDER or "").strip().lower()
    openai_ok = config.STT_OPENAI_AVAILABLE if openai_ok is None else openai_ok
    google_ok = config.STT_GOOGLE_AVAILABLE if google_ok is None else google_ok
    avail = {"openai": openai_ok, "google": google_ok}
    if provided in avail and avail[provided]:
        return provided
    live = [p for p, ok in avail.items() if ok]
    return live[0] if live else None
