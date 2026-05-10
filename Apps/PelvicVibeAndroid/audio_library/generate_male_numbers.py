#!/usr/bin/env python3
"""
Generate male voice number audio files using OpenAI TTS API.
Voice: onyx (male)
Model: tts-1-hd (high quality)
"""

import os
from pathlib import Path
from openai import OpenAI

# Initialize OpenAI client
client = OpenAI()

# Output directory
output_dir = Path(__file__).resolve().parent / "male"
output_dir.mkdir(parents=True, exist_ok=True)

# Number words to generate
numbers = [
    (1, "One"),
    (2, "Two"),
    (3, "Three"),
    (4, "Four"),
    (5, "Five"),
    (6, "Six"),
    (7, "Seven"),
    (8, "Eight"),
    (9, "Nine"),
    (10, "Ten"),
    (11, "Eleven"),
    (12, "Twelve"),
    (13, "Thirteen"),
    (14, "Fourteen"),
    (15, "Fifteen"),
    (16, "Sixteen"),
    (17, "Seventeen"),
    (18, "Eighteen"),
    (19, "Nineteen"),
    (20, "Twenty"),
]

print("Generating male voice number audio files...")
print(f"Voice: onyx | Model: tts-1-hd")
print(f"Output directory: {output_dir}")
print("-" * 50)

generated_files = []

for num, word in numbers:
    filename = f"m_num_{num}.mp3"
    output_path = output_dir / filename

    try:
        response = client.audio.speech.create(
            model="tts-1-hd",
            voice="onyx",
            input=word
        )

        response.stream_to_file(str(output_path))
        generated_files.append(str(output_path))
        print(f"[OK] {filename}: \"{word}\"")

    except Exception as e:
        print(f"[ERROR] {filename}: {e}")

print("-" * 50)
print(f"Generated {len(generated_files)} files:")
for f in generated_files:
    print(f"  - {f}")
