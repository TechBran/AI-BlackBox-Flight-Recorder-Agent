from Orchestrator import tasks, behavioral_core, state

def test_persona_placeholder_present_in_template():
    assert "{PERSONA}" in tasks.CORE_SYSTEM_PROMPT
    assert "ON SYCOPHANCY" not in tasks.CORE_SYSTEM_PROMPT  # old sermon no longer baked in

def test_functional_body_unchanged_below_slot():
    out = tasks.build_core_system_prompt("TOOLS_HERE")
    for marker in ("IDENTITY:", "KNOWLEDGE HIERARCHY", "TOOL USAGE", "ARTIFACT"):
        assert marker in out
    assert "TOOLS_HERE" in out

def test_default_operator_uses_lean_default():
    out = tasks.build_core_system_prompt("x", operator=None)
    assert behavioral_core.DEFAULT_PERSONA_CHAT in out

def test_custom_operator_persona_injected(monkeypatch):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {"Brandon": {"persona": "ZZZ-CUSTOM"}})
    out = tasks.build_core_system_prompt("x", operator="Brandon")
    assert "ZZZ-CUSTOM" in out
    assert behavioral_core.DEFAULT_PERSONA_CHAT not in out

def test_stream_excerpt_still_builds_at_module_load():
    assert isinstance(tasks.STREAM_EXCERPT, str) and len(tasks.STREAM_EXCERPT) > 100

def test_fallback_path_also_uses_operator_persona(monkeypatch):
    # tool_instructions="" path (no user msg / toolvault off) must still honor persona
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {"Brandon": {"persona": "FALLBACK-CUSTOM"}})
    out = tasks.build_core_system_prompt("", operator="Brandon")
    assert "FALLBACK-CUSTOM" in out

def test_cu_context_excludes_persona(monkeypatch):
    from Orchestrator.routes import chat_routes
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES",
                        {"Brandon": {"tts_voice": "openai:onyx", "persona": "SECRET-CU"}})
    ctx, _ = chat_routes.build_cu_context("hello", "Brandon")
    assert "tts_voice" in ctx
    assert "SECRET-CU" not in ctx
