"""Generate assets/speech_24k.pcm (24kHz s16le mono) for transcription probes.

Uses OpenAI POST /v1/audio/speech with response_format=pcm. Best-effort:
tries model ids in order; exits 1 with a clear message if all fail (the xai
suite then marks transcription_default UNRESOLVED instead of crashing).
"""
import json
import sys
import urllib.request
from pathlib import Path

from diagnostics.voice_probes.env import get_key

ASSET = Path(__file__).resolve().parent / "assets" / "speech_24k.pcm"
MODELS = ["gpt-4o-mini-tts", "tts-1"]
TEXT = ("Testing, one, two, three. This is a transcription probe "
        "for the voice pipeline. The quick brown fox jumps over the lazy dog.")


def main() -> int:
    key = get_key("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY not set in service env")
        return 1
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps({"model": model, "voice": "alloy",
                             "input": TEXT, "response_format": "pcm"}).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
        )
        try:
            audio = urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:
            print(f"{model}: {type(e).__name__}: {e}")
            continue
        if len(audio) > 24000:  # > 0.5s of 24kHz s16le
            ASSET.write_bytes(audio)
            print(f"wrote {ASSET} ({len(audio)} bytes, model={model})")
            return 0
    print("all TTS model ids failed — transcription probe will run UNRESOLVED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
