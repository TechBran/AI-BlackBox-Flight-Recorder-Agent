from Orchestrator.stt import resolve as stt_resolve
from Orchestrator.stt.resolve import resolve_stt_provider


def test_runtime_empty_provided_honors_saved_provider(monkeypatch):
    # Client sends provider:""; the runtime path must fresh-read the saved
    # STT_PROVIDER (wizard pick) rather than falling to the openai tie-break.
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (True, True, False))
    monkeypatch.setattr(stt_resolve, "_fresh_stt_provider", lambda: "google")
    assert resolve_stt_provider("") == "google"
    monkeypatch.setattr(stt_resolve, "_fresh_stt_provider", lambda: "")
    assert resolve_stt_provider("") == "openai"  # no saved pick -> tie-break


def test_explicit_wins_when_available():
    assert resolve_stt_provider("openai", openai_ok=True, google_ok=True) == "openai"
    assert resolve_stt_provider("google", openai_ok=True, google_ok=True) == "google"

def test_single_available_auto():
    assert resolve_stt_provider("", openai_ok=True,  google_ok=False) == "openai"
    assert resolve_stt_provider("", openai_ok=False, google_ok=True)  == "google"

def test_explicit_but_unavailable_falls_back():
    assert resolve_stt_provider("google", openai_ok=True, google_ok=False) == "openai"

def test_none_available_returns_none():
    assert resolve_stt_provider("", openai_ok=False, google_ok=False, elevenlabs_ok=False) is None

def test_both_available_no_choice_prefers_openai():
    # documented tie-break; wizard always sets an explicit choice anyway
    assert resolve_stt_provider("", openai_ok=True, google_ok=True) == "openai"

def test_explicit_elevenlabs_wins_when_available():
    # explicit elevenlabs choice resolves even when openai is also available
    assert resolve_stt_provider("elevenlabs", openai_ok=True, google_ok=False, elevenlabs_ok=True) == "elevenlabs"

def test_elevenlabs_only_available_auto():
    # elevenlabs the sole available provider, no explicit choice -> elevenlabs
    assert resolve_stt_provider("", openai_ok=False, google_ok=False, elevenlabs_ok=True) == "elevenlabs"

def test_defaults_follow_stt_availability(monkeypatch):
    # With NO explicit kwargs, resolve reflects live stt_availability().
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (True, False, False))
    assert resolve_stt_provider("") == "openai"
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (False, True, False))
    assert resolve_stt_provider("") == "google"
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (False, False, True))
    assert resolve_stt_provider("") == "elevenlabs"
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (False, False, False))
    assert resolve_stt_provider("") is None
