from Orchestrator.stt.streaming import map_openai_event, map_google_result

def test_openai_delta():
    assert map_openai_event(
        {"type":"conversation.item.input_audio_transcription.delta","delta":"Hel"}
    ) == {"type":"stt_delta","text":"Hel"}

def test_openai_final():
    assert map_openai_event(
        {"type":"conversation.item.input_audio_transcription.completed","transcript":"Hello"}
    ) == {"type":"stt_final","text":"Hello"}

def test_openai_unrelated_event_returns_none():
    assert map_openai_event({"type":"session.created"}) is None
    assert map_openai_event({"type":"input_audio_buffer.committed"}) is None

def test_openai_missing_fields_safe():
    # delta event with no 'delta' key -> treat as empty delta, not a crash
    assert map_openai_event({"type":"conversation.item.input_audio_transcription.delta"}) \
        == {"type":"stt_delta","text":""}

def test_google_interim():
    assert map_google_result("Hel", is_final=False) == {"type":"stt_delta","text":"Hel"}

def test_google_final():
    assert map_google_result("Hello", is_final=True) == {"type":"stt_final","text":"Hello"}
