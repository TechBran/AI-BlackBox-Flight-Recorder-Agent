from Orchestrator import state
from Orchestrator.routes import persona_routes
from Orchestrator.routes.persona_routes import PersonaBody
import Orchestrator.behavioral_core as bc


def test_get_unset_returns_default(monkeypatch):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {})
    monkeypatch.setattr(state, "save_operator_preferences", lambda: None)
    r = persona_routes.get_op_persona("Dana")
    assert r["is_custom"] is False
    assert r["persona"] == bc.DEFAULT_PERSONA_CHAT
    assert r["default"] == bc.DEFAULT_PERSONA_CHAT


def test_put_then_get_custom(monkeypatch):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {})
    monkeypatch.setattr(state, "save_operator_preferences", lambda: None)
    persona_routes.put_op_persona("Dana", PersonaBody(persona="Be terse."))
    assert state.OPERATOR_PREFERENCES["Dana"]["persona"] == "Be terse."
    r = persona_routes.get_op_persona("Dana")
    assert r["is_custom"] is True
    assert r["persona"] == "Be terse."


def test_delete_resets_to_default(monkeypatch):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {"Dana": {"persona": "X", "tts_voice": "v"}})
    monkeypatch.setattr(state, "save_operator_preferences", lambda: None)
    r = persona_routes.delete_op_persona("Dana")
    assert r["is_custom"] is False
    assert r["persona"] == bc.DEFAULT_PERSONA_CHAT
    assert "persona" not in state.OPERATOR_PREFERENCES["Dana"]
    assert state.OPERATOR_PREFERENCES["Dana"]["tts_voice"] == "v"  # other prefs untouched


def test_empty_put_is_not_custom(monkeypatch):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {})
    monkeypatch.setattr(state, "save_operator_preferences", lambda: None)
    r = persona_routes.put_op_persona("Dana", PersonaBody(persona="   "))
    assert r["is_custom"] is False  # whitespace-only is not a real persona


def test_operator_name_with_spaces(monkeypatch):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {})
    monkeypatch.setattr(state, "save_operator_preferences", lambda: None)
    persona_routes.put_op_persona("Anna 2", PersonaBody(persona="Hi"))
    assert state.OPERATOR_PREFERENCES["Anna 2"]["persona"] == "Hi"
