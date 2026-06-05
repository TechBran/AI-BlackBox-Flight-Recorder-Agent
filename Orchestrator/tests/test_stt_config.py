import Orchestrator.config as cfg

def test_stt_model_registry_defaults():
    assert cfg.STT_OPENAI_STREAM == "gpt-realtime-whisper"
    assert cfg.STT_OPENAI_FILE == "gpt-4o-transcribe"
    assert cfg.STT_GOOGLE_MODEL == "chirp_2"
    assert cfg.STT_GOOGLE_REGION == "us-central1"
    assert cfg.STT_OPENAI_DELAY in ("minimal","low","medium","high","xhigh")
    assert cfg.STT_MODEL == "whisper-1"   # legacy fallback preserved

def test_stt_availability_flags_are_bools():
    assert isinstance(cfg.STT_OPENAI_AVAILABLE, bool)
    assert isinstance(cfg.STT_GOOGLE_AVAILABLE, bool)
