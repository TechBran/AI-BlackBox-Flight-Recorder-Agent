import importlib
from Orchestrator import behavioral_core, state

def _reset_prefs(monkeypatch, prefs):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", prefs)

def test_get_persona_default_when_no_operator():
    assert behavioral_core.get_persona(None, "chat") == behavioral_core.DEFAULT_PERSONA_CHAT
    assert behavioral_core.get_persona("", "chat") == behavioral_core.DEFAULT_PERSONA_CHAT

def test_get_persona_default_when_operator_has_none(monkeypatch):
    _reset_prefs(monkeypatch, {})
    assert behavioral_core.get_persona("Brandon", "chat") == behavioral_core.DEFAULT_PERSONA_CHAT

def test_get_persona_returns_custom(monkeypatch):
    _reset_prefs(monkeypatch, {"Brandon": {"persona": "You are terse."}})
    assert behavioral_core.get_persona("Brandon", "chat") == "You are terse."

def test_get_persona_empty_custom_falls_back_to_default(monkeypatch):
    _reset_prefs(monkeypatch, {"Brandon": {"persona": "   "}})
    assert behavioral_core.get_persona("Brandon", "chat") == behavioral_core.DEFAULT_PERSONA_CHAT

def test_get_persona_voice_modality(monkeypatch):
    _reset_prefs(monkeypatch, {"Brandon": {"persona": "Voice me."}})
    assert behavioral_core.get_persona("Brandon", "voice") == "Voice me."

def test_default_persona_is_lean_not_old_sermon():
    assert "ON SYCOPHANCY" not in behavioral_core.DEFAULT_PERSONA_CHAT
    assert len(behavioral_core.DEFAULT_PERSONA_CHAT) < 600

def test_persona_pref_key_is_persona():
    assert behavioral_core.PERSONA_PREF_KEY == "persona"

def test_get_persona_generic_default_no_hardcoded_name():
    # Portability: default must not embed an operator name or host.
    d = behavioral_core.DEFAULT_PERSONA_CHAT.lower()
    assert "brandon" not in d
    assert "http" not in d
