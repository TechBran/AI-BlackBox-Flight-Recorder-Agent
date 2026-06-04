from Orchestrator import config

def build_stt_catalog() -> list:
    """Capability catalog for the STT provider picker (onboarding + Portal + Android).
    Mirrors config.build_tts_catalog(). Swap model names via the STT_* config consts."""
    return [
        {
            "id": "openai",
            "label": "OpenAI",
            "available": config.STT_OPENAI_AVAILABLE,
            "blurb": "gpt-realtime-whisper streaming + gpt-4o-transcribe files. Uses your OpenAI API key.",
            "models": {"streaming": config.STT_OPENAI_STREAM, "file": config.STT_OPENAI_FILE},
        },
        {
            "id": "google",
            "label": "Google",
            "available": config.STT_GOOGLE_AVAILABLE,
            "blurb": "Cloud Speech-to-Text v2 chirp_2 streaming + files. Uses a Google service-account JSON.",
            "models": {"streaming": config.STT_GOOGLE_MODEL, "file": config.STT_GOOGLE_MODEL},
        },
    ]
