from Orchestrator import config
from Orchestrator.stt.resolve import (
    stt_availability,
    local_stt_available,
    local_streaming_stt_available,
    onbox_stt_available,
)


def build_stt_catalog() -> list:
    """Capability catalog for the STT provider picker (onboarding + Portal + Android).
    Follows the same single-source-of-truth PATTERN as config.build_tts_catalog(),
    but a different payload shape (providers/available/models, not groups/voices).
    Swap model names via the STT_* config consts. The `available` flags are read
    LIVE via stt_availability() (fresh .env + filesystem) so a just-saved key or
    credential shows as available without a service restart."""
    openai_ok, google_ok, elevenlabs_ok = stt_availability()
    providers = [
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
        {
            "id": "elevenlabs", "label": "ElevenLabs", "available": elevenlabs_ok,
            "blurb": "Scribe v2 realtime streaming (~150ms) + Scribe v2 files with speaker diarization. Uses your ElevenLabs API key.",
            "models": {"streaming": config.ELEVENLABS_STT_STREAM_MODEL, "file": config.ELEVENLABS_STT_FILE_MODEL},
        },
    ]
    # Local (custom-server) STT — appended only when a registered server hosts an
    # STT model. File transcription only (live streaming needs the OpenAI realtime
    # WS protocol, which local whisper.cpp servers almost never speak).
    if local_stt_available():
        streams = local_streaming_stt_available()
        providers.append({
            "id": "local", "label": "Local (free)", "available": True,
            "blurb": ("A local OpenAI-compatible speech-to-text model (whisper / faster-whisper). "
                      + ("Realtime streaming + files; free + private."
                         if streams else "File transcription only; free + private.")),
            "models": {"streaming": "realtime" if streams else None, "file": "local"},
        })
    # On-box (local) STT — the on-box model stack's Speaches (faster-whisper) member.
    # Independent of the custom-server registry (available iff the on-box stack is
    # installed+healthy AND STT is enabled in [local_models]). Appended only when live,
    # mirroring the conditional 'local' append above so the catalog stays the single
    # source of truth for the transcription-step card's availability: present+Ready when
    # the on-box stack is up, absent (card falls back to "Needs setup") otherwise.
    if onbox_stt_available():
        try:
            from Orchestrator import local_stack
            stream_model = local_stack.stt_stream_model()
            batch_model = local_stack.stt_batch_model()
        except Exception:
            stream_model, batch_model = "whisper (realtime)", "whisper"
        providers.append({
            "id": "onbox", "label": "On-box (local)", "available": True,
            "blurb": ("On-box faster-whisper via the local model stack — realtime streaming + "
                      "files, free + fully private (no cloud STT, no API key)."),
            "models": {"streaming": stream_model, "file": batch_model},
        })
    return providers
