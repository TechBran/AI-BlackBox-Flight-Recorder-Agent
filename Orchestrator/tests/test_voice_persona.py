from Orchestrator import behavioral_core
def test_voice_persona_combines_persona_and_delivery_note(monkeypatch):
    from Orchestrator import state
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {"Op": {"persona": "VP-CUSTOM"}})
    combined = behavioral_core.get_persona("Op", "voice") + "\n\n" + behavioral_core.VOICE_DELIVERY_NOTE
    assert "VP-CUSTOM" in combined
    assert "Don't read URLs" in combined
