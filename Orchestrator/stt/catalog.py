from Orchestrator import config
from Orchestrator.stt.resolve import stt_availability


def build_stt_catalog() -> list:
    """Capability catalog for the STT provider picker (onboarding + Portal + Android).
    Follows the same single-source-of-truth PATTERN as config.build_tts_catalog(),
    but a different payload shape (providers/available/models, not groups/voices).
    Swap model names via the STT_* config consts. The `available` flags are read
    LIVE via stt_availability() (fresh .env + filesystem) so a just-saved key or
    credential shows as available without a service restart."""
    openai_ok, google_ok = stt_availability()
    return [
        {
            "id": "openai",
            "label": "OpenAI",
            "available": openai_ok,
            "blurb": "gpt-realtime-whisper streaming + gpt-4o-transcribe files. Uses your OpenAI API key.",
            "models": {"streaming": config.STT_OPENAI_STREAM, "file": config.STT_OPENAI_FILE},
        },
        {
            "id": "google",
            "label": "Google",
            "available": google_ok,
            "blurb": "Cloud Speech-to-Text v2 chirp_2 streaming + files. Uses a Google service-account JSON.",
            "models": {"streaming": config.STT_GOOGLE_MODEL, "file": config.STT_GOOGLE_MODEL},
        },
    ]
