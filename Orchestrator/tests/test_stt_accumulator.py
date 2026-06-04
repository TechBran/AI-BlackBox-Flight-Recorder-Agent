from Orchestrator.stt.streaming import InterimAccumulator

def test_openai_deltas_accumulate_into_cumulative():
    acc = InterimAccumulator()
    assert acc.openai({"type":"conversation.item.input_audio_transcription.delta","delta":"Hel"}) == {"type":"stt_delta","text":"Hel"}
    assert acc.openai({"type":"conversation.item.input_audio_transcription.delta","delta":"lo"}) == {"type":"stt_delta","text":"Hello"}
    assert acc.openai({"type":"conversation.item.input_audio_transcription.delta","delta":" world"}) == {"type":"stt_delta","text":"Hello world"}

def test_openai_final_returns_full_and_resets():
    acc = InterimAccumulator()
    acc.openai({"type":"conversation.item.input_audio_transcription.delta","delta":"Hello"})
    assert acc.openai({"type":"conversation.item.input_audio_transcription.completed","transcript":"Hello world"}) == {"type":"stt_final","text":"Hello world"}
    # after a final, the buffer resets so the next utterance starts fresh
    assert acc.openai({"type":"conversation.item.input_audio_transcription.delta","delta":"Next"}) == {"type":"stt_delta","text":"Next"}

def test_openai_ignored_event_returns_none():
    acc = InterimAccumulator()
    assert acc.openai({"type":"session.updated"}) is None

def test_google_interim_passthrough_cumulative():
    acc = InterimAccumulator()
    # google interim results are already cumulative; pass through unchanged
    assert acc.google("Hel", is_final=False) == {"type":"stt_delta","text":"Hel"}
    assert acc.google("Hello", is_final=False) == {"type":"stt_delta","text":"Hello"}

def test_google_final_returns_full_and_resets():
    acc = InterimAccumulator()
    acc.google("Hello", is_final=False)
    assert acc.google("Hello world", is_final=True) == {"type":"stt_final","text":"Hello world"}
    assert acc.google("Next", is_final=False) == {"type":"stt_delta","text":"Next"}
