from Orchestrator.stt import resolve as stt_resolve
from Orchestrator.stt.resolve import resolve_stt_provider


def test_runtime_empty_provided_honors_saved_provider(monkeypatch):
    # Client sends provider:""; the runtime path must fresh-read the saved
    # STT_PROVIDER (wizard pick) rather than falling to the openai tie-break.
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (True, True, False))
    monkeypatch.setattr(stt_resolve, "local_stt_available", lambda: False)  # hermetic vs registry
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
    monkeypatch.setattr(stt_resolve, "local_stt_available", lambda: False)  # hermetic vs registry
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (True, False, False))
    assert resolve_stt_provider("") == "openai"
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (False, True, False))
    assert resolve_stt_provider("") == "google"
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (False, False, True))
    assert resolve_stt_provider("") == "elevenlabs"
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (False, False, False))
    assert resolve_stt_provider("") is None

def test_onbox_ranked_above_local_no_explicit_pick():
    # No explicit pick, only onbox + local available -> onbox wins (ranked above
    # the custom-server local token). Cloud absent.
    assert resolve_stt_provider(
        "", openai_ok=False, google_ok=False, elevenlabs_ok=False,
        onbox_ok=True, local_ok=True) == "onbox"

def test_explicit_onbox_wins_when_available():
    assert resolve_stt_provider(
        "onbox", openai_ok=True, google_ok=False, onbox_ok=True) == "onbox"

def test_explicit_elevenlabs_still_wins_over_onbox(monkeypatch):
    # D9: an explicit credentialed pick is NEVER overridden by the on-box default.
    assert resolve_stt_provider(
        "elevenlabs", openai_ok=False, google_ok=False,
        elevenlabs_ok=True, onbox_ok=True) == "elevenlabs"

def test_onbox_availability_keys_on_local_stack(monkeypatch):
    # onbox availability = is_healthy() AND enabled('stt'), NOT the custom-server
    # registry (which is what local_stt_available() reads).
    import Orchestrator.stt.resolve as r
    calls = {}
    class _LS:
        @staticmethod
        def is_healthy(): calls["healthy"] = True; return True
        @staticmethod
        def enabled(cap): calls["cap"] = cap; return True
    monkeypatch.setitem(__import__("sys").modules, "Orchestrator.local_stack", _LS)
    assert r.onbox_stt_available() is True
    assert calls == {"healthy": True, "cap": "stt"}


# ── D-1: a substitution is NAMED, not silent ────────────────────────────────

def test_detailed_reports_no_override_when_the_pick_is_honoured():
    assert stt_resolve.resolve_stt_provider_detailed(
        "google", openai_ok=True, google_ok=True) == ("google", None)


def test_detailed_names_the_pick_it_dropped():
    assert stt_resolve.resolve_stt_provider_detailed(
        "google", openai_ok=True, google_ok=False) == ("openai", "google")


def test_detailed_names_the_dropped_env_pin(monkeypatch):
    # THE live bug: .env STT_PROVIDER=local, the STREAMING path passes
    # local_ok=False, and the pin quietly became openai.
    monkeypatch.setattr(stt_resolve, "stt_availability", lambda: (True, False, False))
    monkeypatch.setattr(stt_resolve, "onbox_stt_available", lambda: False)
    monkeypatch.setattr(stt_resolve, "_fresh_stt_provider", lambda: "local")
    assert stt_resolve.resolve_stt_provider_detailed("", local_ok=False) == ("openai", "local")


def test_detailed_reports_no_override_when_nothing_resolves():
    # Nothing was substituted — the caller fails loudly on the None instead.
    assert stt_resolve.resolve_stt_provider_detailed(
        "local", openai_ok=False, google_ok=False, elevenlabs_ok=False,
        local_ok=False) == (None, None)


def test_detailed_reports_no_override_when_no_pick_was_made():
    assert stt_resolve.resolve_stt_provider_detailed(
        "", openai_ok=True, google_ok=True) == ("openai", None)


def test_detailed_names_an_unknown_pick_it_dropped():
    assert stt_resolve.resolve_stt_provider_detailed(
        "whisper.cpp", openai_ok=True, google_ok=False) == ("openai", "whisper.cpp")


def test_wrapper_still_returns_a_bare_string():
    out = resolve_stt_provider("google", openai_ok=True, google_ok=False)
    assert out == "openai"
    assert type(out) is str


def test_override_out_hands_the_substitution_to_the_reporter():
    out = {}
    assert resolve_stt_provider("google", openai_ok=True, google_ok=False,
                                override_out=out) == "openai"
    assert out["overridden_from"] == "google"
    honoured = {}
    assert resolve_stt_provider("google", openai_ok=True, google_ok=True,
                                override_out=honoured) == "google"
    assert honoured["overridden_from"] is None


def test_substitution_is_logged_naming_both_providers(capsys):
    resolve_stt_provider("local", openai_ok=True, google_ok=False, local_ok=False)
    log = capsys.readouterr().out
    assert "OVERRIDE" in log
    assert "local" in log and "openai" in log


def test_honoured_pick_logs_nothing(capsys):
    resolve_stt_provider("google", openai_ok=True, google_ok=True)
    assert "OVERRIDE" not in capsys.readouterr().out
