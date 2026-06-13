import Orchestrator.config as cfg

def test_stt_model_registry_defaults():
    assert cfg.STT_OPENAI_STREAM == "gpt-realtime-whisper"
    assert cfg.STT_OPENAI_FILE == "gpt-4o-transcribe"
    assert cfg.STT_GOOGLE_MODEL == "chirp_2"
    assert cfg.STT_GOOGLE_REGION == "us-central1"
    assert cfg.STT_OPENAI_DELAY in ("minimal","low","medium","high","xhigh")
    assert cfg.STT_MODEL == "whisper-1"   # legacy fallback preserved

def test_elevenlabs_stt_model_defaults():
    assert cfg.ELEVENLABS_STT_STREAM_MODEL == "scribe_v2_realtime"
    assert cfg.ELEVENLABS_STT_FILE_MODEL == "scribe_v2"

def test_stt_availability_flags_are_bools():
    assert isinstance(cfg.STT_OPENAI_AVAILABLE, bool)
    assert isinstance(cfg.STT_GOOGLE_AVAILABLE, bool)

def test_stt_availability_returns_three_bools():
    from Orchestrator.stt.resolve import stt_availability
    result = stt_availability()
    assert len(result) == 3
    assert all(isinstance(x, bool) for x in result)
