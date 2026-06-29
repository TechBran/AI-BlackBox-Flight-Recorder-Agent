"""Pure provider-event -> uniform-client-event mappers for streaming STT.

Kept separate from the /ws/stt transport so the translation logic is unit-
testable without any network. The /ws/stt endpoint adds the 'target' field.
"""

_OPENAI_DELTA = "conversation.item.input_audio_transcription.delta"
_OPENAI_FINAL = "conversation.item.input_audio_transcription.completed"


def join_transcript_segments(prefix: str, text: str) -> str:
    """Concatenate a carried-over transcript prefix with the current cumulative
    text, with exactly one separating space. Used to keep the transcript
    continuous across an STT provider session rotation (reconnect-and-resume)."""
    if not prefix:
        return text
    if not text:
        return prefix
    sep = "" if prefix.endswith((" ", "\n")) else " "
    return f"{prefix}{sep}{text}"


def map_openai_event(event: dict):
    """Map an OpenAI realtime transcription event to a uniform STT event.
    Returns {"type":"stt_delta"|"stt_final","text":...} or None for events we ignore."""
    etype = event.get("type")
    if etype == _OPENAI_DELTA:
        return {"type": "stt_delta", "text": event.get("delta", "")}
    if etype == _OPENAI_FINAL:
        return {"type": "stt_final", "text": event.get("transcript", "")}
    return None


def map_google_result(text: str, is_final: bool):
    """Map a Google Cloud Speech streaming result to a uniform STT event."""
    return {"type": "stt_final" if is_final else "stt_delta", "text": text}


def map_elevenlabs_message(event: dict):
    """ElevenLabs Scribe realtime -> uniform client event (or None to ignore).
    partial_transcript text is CUMULATIVE (verified live), committed is the final."""
    mt = event.get("message_type")
    if mt == "partial_transcript":
        return {"type": "stt_delta", "text": event.get("text", "")}
    if mt == "committed_transcript":
        return {"type": "stt_final", "text": event.get("text", "")}
    return None  # session_started, errors, unknown -> ignore


class InterimAccumulator:
    """Normalizes per-provider interim semantics so stt_delta.text is ALWAYS
    cumulative (the full interim transcript so far). OpenAI delta events are
    incremental and get accumulated; Google interim results and ElevenLabs
    partial_transcript events are already cumulative and pass through. All
    reset the buffer on a final."""

    def __init__(self):
        self._buf = ""

    def openai(self, event: dict):
        m = map_openai_event(event)
        if m is None:
            return None
        if m["type"] == "stt_delta":
            self._buf += m["text"]
            return {"type": "stt_delta", "text": self._buf}
        # stt_final
        self._buf = ""
        return m

    def google(self, text: str, is_final: bool):
        m = map_google_result(text, is_final)
        if is_final:
            self._buf = ""
        else:
            self._buf = m["text"]
        return m

    def elevenlabs(self, event: dict):
        m = map_elevenlabs_message(event)
        if m is None:
            return None
        if m["type"] == "stt_delta":
            self._buf = m["text"]   # cumulative: replace, don't append
            return m
        # stt_final
        self._buf = ""
        return m
