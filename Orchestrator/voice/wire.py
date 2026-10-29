"""Upstream frames the live-voice providers share.

Two wire formats: Gemini Live's BidiGenerateContent messages, and the
OpenAI-style realtime events that OpenAI Realtime and xAI Grok both speak. The
routes, their adapters and the phone bridge's call legs build audio and text
turns here, so a format fix lands once (the phone bridge still sent Gemini's
deprecated realtimeInput.mediaChunks after the routes had moved on).
"""

from typing import Dict, List


def gemini_audio(audio_b64: str, rate: int) -> Dict:
    """One chunk of base64 PCM16 input audio at `rate` Hz."""
    return {
        "realtime_input": {
            "audio": {
                "mimeType": f"audio/pcm;rate={rate}",
                "data": audio_b64
            }
        }
    }


def gemini_user_text(text: str) -> List[Dict]:
    """A complete user text turn (the model answers it)."""
    return [{
        "clientContent": {
            "turns": [{"role": "user", "parts": [{"text": text}]}],
            "turnComplete": True
        }
    }]


def openai_audio(audio_b64: str) -> Dict:
    """One chunk of base64 PCM16 input audio for the server's input buffer."""
    return {"type": "input_audio_buffer.append", "audio": audio_b64}


def openai_user_text(text: str) -> List[Dict]:
    """A user text message, then the request to answer it."""
    return [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}]
            }
        },
        {"type": "response.create"},
    ]
