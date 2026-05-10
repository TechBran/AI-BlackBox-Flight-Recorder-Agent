#!/usr/bin/env python3
"""
Generate male voice audio files for numbers 41-60 using OpenAI TTS.
Uses the "onyx" voice (male) with tts-1-hd model for high quality.
"""

import os
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Error: openai package not installed. Run: pip install openai")
    exit(1)

# Configuration
OUTPUT_DIR = Path(__file__).resolve().parent / "male"
VOICE = "onyx"  # Male voice
MODEL = "tts-1-hd"  # High quality model

# Numbers 41-60 with their spoken forms
NUMBERS = {
    41: "Forty-one",
    42: "Forty-two",
    43: "Forty-three",
    44: "Forty-four",
    45: "Forty-five",
    46: "Forty-six",
    47: "Forty-seven",
    48: "Forty-eight",
    49: "Forty-nine",
    50: "Fifty",
    51: "Fifty-one",
    52: "Fifty-two",
    53: "Fifty-three",
    54: "Fifty-four",
    55: "Fifty-five",
    56: "Fifty-six",
    57: "Fifty-seven",
    58: "Fifty-eight",
    59: "Fifty-nine",
    60: "Sixty",
}

def generate_audio_files():
    """Generate all number audio files."""
    # Load API key from secrets file
    import os
    env_path = str(Path(__file__).resolve().parent.parent.parent.parent / "Orchestrator" / "Secrets" / ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("OPENAI_API_KEY="):
                    api_key = line.strip().split("=", 1)[1]
                    os.environ["OPENAI_API_KEY"] = api_key
                    break

    # Initialize OpenAI client
    client = OpenAI()

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    generated_files = []

    for num, text in NUMBERS.items():
        filename = f"m_num_{num}.mp3"
        filepath = OUTPUT_DIR / filename

        print(f"Generating {filename}: \"{text}\"...")

        try:
            response = client.audio.speech.create(
                model=MODEL,
                voice=VOICE,
                input=text,
                response_format="mp3"
            )

            # Save the audio file
            response.stream_to_file(str(filepath))
            generated_files.append(str(filepath))
            print(f"  ✓ Saved: {filepath}")

        except Exception as e:
            print(f"  ✗ Error generating {filename}: {e}")

    return generated_files

if __name__ == "__main__":
    print(f"Generating male voice audio files for numbers 41-60")
    print(f"Voice: {VOICE}")
    print(f"Model: {MODEL}")
    print(f"Output directory: {OUTPUT_DIR}")
    print("-" * 50)

    files = generate_audio_files()

    print("-" * 50)
    print(f"Generated {len(files)} audio files:")
    for f in files:
        print(f"  - {f}")
