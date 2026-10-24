from Orchestrator import config
from Orchestrator.stt.resolve import (
    stt_availability,
    local_stt_available,
    local_streaming_stt_available,
    onbox_stt_available,
    onbox_stt_degraded,
    resolve_stt_provider_detailed,
)


def build_stt_resolution(*, local_ok=None, streaming_local_ok=None, onbox_ok=None) -> dict:
    """Which provider each STT path ACTUALLY resolves to, reported separately.

    File transcription and the realtime mic path do not share an answer, and a
    single "resolved" hides that: local STT is file-only unless the server
    advertises /v1/realtime, so a box pinned to 'local' transcribes files locally
    while the mic streams to the cloud. Both resolutions are reported here, each
    with the pick it dropped, so the split is visible instead of inferred. The two
    calls mirror their real callers exactly — file transcription resolves with
    local_stt_available(), /ws/stt with local_streaming_stt_available()."""
    if local_ok is None:
        local_ok = local_stt_available()
    if streaming_local_ok is None:
        streaming_local_ok = local_streaming_stt_available()
    if onbox_ok is None:
        onbox_ok = onbox_stt_available()
    file_provider, file_override = resolve_stt_provider_detailed(
        "", local_ok=local_ok, onbox_ok=onbox_ok)
    stream_provider, stream_override = resolve_stt_provider_detailed(
        "", local_ok=streaming_local_ok, onbox_ok=onbox_ok)
    return {
        "resolved_file": file_provider,
        "resolved_streaming": stream_provider,
        "overridden_from_file": file_override,
        "overridden_from_streaming": stream_override,
    }


def build_stt_catalog() -> list:
    """Capability catalog for the STT provider picker (onboarding + Portal + Android).
    Follows the same single-source-of-truth PATTERN as config.build_tts_catalog(),
    but a different payload shape (providers/available/models, not groups/voices).
    Swap model names via the STT_* config consts. The `available` flags are read
    LIVE via stt_availability() (fresh .env + filesystem) so a just-saved key or
    credential shows as available without a service restart."""
    openai_ok, google_ok, elevenlabs_ok = stt_availability()
    file_local_ok = local_stt_available()
    streaming_local_ok = local_streaming_stt_available()
    onbox_ok = onbox_stt_available()
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
    if file_local_ok:
        streams = streaming_local_ok
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
    if onbox_ok:
        try:
            from Orchestrator import local_stack
            stream_model = local_stack.stt_stream_model()
            batch_model = local_stack.stt_batch_model()
        except Exception:
            stream_model, batch_model = "whisper (realtime)", "whisper"
        # Additive (plan Task 10): a dead GPU driver leaves on-box STT working
        # on CPU, slowly — kept available, but marked, never shown as healthy.
        degraded = onbox_stt_degraded()
        providers.append({
            "id": "onbox", "label": "On-box (local)", "available": True,
            "blurb": ("On-box faster-whisper via the local model stack — realtime streaming + "
                      "files, free + fully private (no cloud STT, no API key)."
                      + (f" Degraded: {degraded}." if degraded else "")),
            "models": {"streaming": stream_model, "file": batch_model},
            "degraded": bool(degraded),
            "degraded_reason": degraded,
        })
    # Additive (D-3): which path each provider actually serves right now. A
    # provider can be `available` and still serve neither — pinning 'local' on a
    # server with no /v1/realtime leaves resolves_file true and resolves_streaming
    # false, which is exactly the split a single "resolved" used to hide.
    resolution = build_stt_resolution(local_ok=file_local_ok,
                                      streaming_local_ok=streaming_local_ok,
                                      onbox_ok=onbox_ok)
    for p in providers:
        p["resolves_file"] = p["id"] == resolution["resolved_file"]
        p["resolves_streaming"] = p["id"] == resolution["resolved_streaming"]
    return providers
