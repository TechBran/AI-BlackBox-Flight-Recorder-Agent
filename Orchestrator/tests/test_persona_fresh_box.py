"""Portability / fresh-box guarantees for the per-operator persona feature.

A customer boots a clean box: no operator_preferences.json, operators they named
themselves, and no Brandon. The persona must degrade to a sane generic default
everywhere, never crash, and stay migration-safe for records that predate the
feature. These are the guarantees from feedback_production_quality_portable.
"""
from Orchestrator import behavioral_core as bc, state, tasks


def test_get_persona_empty_store_any_operator_name(monkeypatch):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {})
    for op in ["Dana", "Operator 1", "Anna 2", "a.b@c"]:
        assert bc.get_persona(op, "chat") == bc.DEFAULT_PERSONA_CHAT
        assert bc.get_persona(op, "voice") == bc.DEFAULT_PERSONA_VOICE


def test_build_core_prompt_on_empty_store(monkeypatch):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {})
    out = tasks.build_core_system_prompt("TOOLS", operator="Dana")
    assert bc.DEFAULT_PERSONA_CHAT in out          # lean default injected
    assert "IDENTITY:" in out and "KNOWLEDGE HIERARCHY" in out  # functional body intact
    assert "TOOLS" in out


def test_default_persona_is_generic_no_box_identity():
    d = bc.DEFAULT_PERSONA_CHAT.lower()
    for token in ["brandon", "anna", "http://", "https://", "localhost", ".ts.net"]:
        assert token not in d, f"default persona leaks box-specific token: {token!r}"


def test_new_operator_lazy_create_then_resolve(monkeypatch):
    store = {}
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", store)
    monkeypatch.setattr(state, "save_operator_preferences", lambda: None)
    # brand-new operator with no record -> default
    assert bc.get_persona("BrandNewOp", "chat") == bc.DEFAULT_PERSONA_CHAT
    # filling it in lazily creates the record and resolves to it
    state.set_operator_preference("BrandNewOp", bc.PERSONA_PREF_KEY, "Custom for new op")
    assert store["BrandNewOp"]["persona"] == "Custom for new op"
    assert bc.get_persona("BrandNewOp", "chat") == "Custom for new op"


def test_migration_safe_record_without_persona_key(monkeypatch):
    # A record that predates the feature (only tts_voice) must keep working and
    # resolve to the default persona without touching the existing pref.
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {"Legacy": {"tts_voice": "openai:onyx"}})
    assert bc.get_persona("Legacy", "chat") == bc.DEFAULT_PERSONA_CHAT
    assert state.get_operator_preference("Legacy", "tts_voice") == "openai:onyx"
