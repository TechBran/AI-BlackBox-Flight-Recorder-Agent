#!/usr/bin/env python3
"""
Generate ALL TTS audio files for WorkoutVibe app.
Female Voice: nova
Male Voice: onyx
Model: tts-1-hd (high quality)
"""

import os
import sys
from pathlib import Path
from openai import OpenAI

# Initialize OpenAI client
client = OpenAI()

# Output directories
BASE_DIR = Path(__file__).resolve().parent
FEMALE_DIR = BASE_DIR / "female"
MALE_DIR = BASE_DIR / "male"

FEMALE_DIR.mkdir(parents=True, exist_ok=True)
MALE_DIR.mkdir(parents=True, exist_ok=True)

# Voice configurations
VOICES = {
    "female": {"voice": "nova", "prefix": "f", "dir": FEMALE_DIR},
    "male": {"voice": "onyx", "prefix": "m", "dir": MALE_DIR}
}

# Number words 1-60
NUMBER_WORDS = {
    1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five",
    6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten",
    11: "Eleven", 12: "Twelve", 13: "Thirteen", 14: "Fourteen", 15: "Fifteen",
    16: "Sixteen", 17: "Seventeen", 18: "Eighteen", 19: "Nineteen", 20: "Twenty",
    21: "Twenty-one", 22: "Twenty-two", 23: "Twenty-three", 24: "Twenty-four", 25: "Twenty-five",
    26: "Twenty-six", 27: "Twenty-seven", 28: "Twenty-eight", 29: "Twenty-nine", 30: "Thirty",
    31: "Thirty-one", 32: "Thirty-two", 33: "Thirty-three", 34: "Thirty-four", 35: "Thirty-five",
    36: "Thirty-six", 37: "Thirty-seven", 38: "Thirty-eight", 39: "Thirty-nine", 40: "Forty",
    41: "Forty-one", 42: "Forty-two", 43: "Forty-three", 44: "Forty-four", 45: "Forty-five",
    46: "Forty-six", 47: "Forty-seven", 48: "Forty-eight", 49: "Forty-nine", 50: "Fifty",
    51: "Fifty-one", 52: "Fifty-two", 53: "Fifty-three", 54: "Fifty-four", 55: "Fifty-five",
    56: "Fifty-six", 57: "Fifty-seven", 58: "Fifty-eight", 59: "Fifty-nine", 60: "Sixty"
}

# Workout cues (action words spoken during work phase)
WORKOUT_CUES = {
    "pushup": "Push up!",
    "crunch": "Crunch!",
    "squat": "Squat!",
    "hold": "Hold!",
    "burpee": "Burpee!",
    "run": "Run!",
    "sprint": "Sprint!",
    "go": "Go!"
}

# Rest cues (spoken during rest phase)
REST_CUES = {
    "rest": "Rest.",
    "walk": "Walk.",
    "recover": "Recover."
}

# System cues
SYSTEM_CUES = {
    "countdown": "Three, two, one, go!",
    "workout_complete": "Workout complete! Great job!",
    "keep_going": "Keep going!",
    "halfway": "Halfway there!"
}

# Set completion announcements
SET_COMPLETE = {
    1: "Set one complete!",
    2: "Set two complete!",
    3: "Set three complete!",
    4: "Set four complete!",
    5: "Set five complete!",
    6: "Set six complete!",
    7: "Set seven complete!",
    8: "Set eight complete!",
    9: "Set nine complete!",
    10: "Set ten complete!"
}


def generate_audio(text: str, voice: str, output_path: Path) -> bool:
    """Generate TTS audio file using OpenAI API."""
    try:
        response = client.audio.speech.create(
            model="tts-1-hd",
            voice=voice,
            input=text
        )
        response.stream_to_file(str(output_path))
        return True
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def generate_for_voice(voice_config: dict, voice_name: str):
    """Generate all audio files for a specific voice."""
    voice = voice_config["voice"]
    prefix = voice_config["prefix"]
    output_dir = voice_config["dir"]

    print(f"\n{'='*60}")
    print(f"Generating {voice_name.upper()} voice files ({voice})")
    print(f"Output: {output_dir}")
    print(f"{'='*60}")

    generated = 0
    skipped = 0
    failed = 0

    # Generate number files (1-60)
    print(f"\n--- Numbers 1-60 ---")
    for num, word in NUMBER_WORDS.items():
        filename = f"{prefix}_num_{num}.mp3"
        output_path = output_dir / filename

        if output_path.exists():
            print(f"  [SKIP] {filename} (exists)")
            skipped += 1
            continue

        print(f"  Generating {filename}: \"{word}\"...", end=" ", flush=True)
        if generate_audio(word, voice, output_path):
            print("[OK]")
            generated += 1
        else:
            failed += 1

    # Generate workout cues
    print(f"\n--- Workout Cues ---")
    for cue_name, text in WORKOUT_CUES.items():
        filename = f"{prefix}_{cue_name}.mp3"
        output_path = output_dir / filename

        if output_path.exists():
            print(f"  [SKIP] {filename} (exists)")
            skipped += 1
            continue

        print(f"  Generating {filename}: \"{text}\"...", end=" ", flush=True)
        if generate_audio(text, voice, output_path):
            print("[OK]")
            generated += 1
        else:
            failed += 1

    # Generate rest cues
    print(f"\n--- Rest Cues ---")
    for cue_name, text in REST_CUES.items():
        filename = f"{prefix}_{cue_name}.mp3"
        output_path = output_dir / filename

        if output_path.exists():
            print(f"  [SKIP] {filename} (exists)")
            skipped += 1
            continue

        print(f"  Generating {filename}: \"{text}\"...", end=" ", flush=True)
        if generate_audio(text, voice, output_path):
            print("[OK]")
            generated += 1
        else:
            failed += 1

    # Generate system cues
    print(f"\n--- System Cues ---")
    for cue_name, text in SYSTEM_CUES.items():
        filename = f"{prefix}_{cue_name}.mp3"
        output_path = output_dir / filename

        if output_path.exists():
            print(f"  [SKIP] {filename} (exists)")
            skipped += 1
            continue

        print(f"  Generating {filename}: \"{text}\"...", end=" ", flush=True)
        if generate_audio(text, voice, output_path):
            print("[OK]")
            generated += 1
        else:
            failed += 1

    # Generate set complete announcements
    print(f"\n--- Set Complete Announcements ---")
    for set_num, text in SET_COMPLETE.items():
        filename = f"{prefix}_set_complete_{set_num}.mp3"
        output_path = output_dir / filename

        if output_path.exists():
            print(f"  [SKIP] {filename} (exists)")
            skipped += 1
            continue

        print(f"  Generating {filename}: \"{text}\"...", end=" ", flush=True)
        if generate_audio(text, voice, output_path):
            print("[OK]")
            generated += 1
        else:
            failed += 1

    return generated, skipped, failed


def main():
    print("=" * 60)
    print("WorkoutVibe TTS Audio Generator")
    print("Female: nova | Male: onyx | Model: tts-1-hd")
    print("=" * 60)

    total_generated = 0
    total_skipped = 0
    total_failed = 0

    # Check which voice to generate (or both)
    if len(sys.argv) > 1:
        voice_filter = sys.argv[1].lower()
        if voice_filter in VOICES:
            generated, skipped, failed = generate_for_voice(VOICES[voice_filter], voice_filter)
            total_generated += generated
            total_skipped += skipped
            total_failed += failed
        else:
            print(f"Unknown voice: {voice_filter}")
            print("Usage: python generate_all_tts.py [female|male]")
            sys.exit(1)
    else:
        # Generate for both voices
        for voice_name, voice_config in VOICES.items():
            generated, skipped, failed = generate_for_voice(voice_config, voice_name)
            total_generated += generated
            total_skipped += skipped
            total_failed += failed

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"  Generated: {total_generated}")
    print(f"  Skipped:   {total_skipped}")
    print(f"  Failed:    {total_failed}")
    print(f"  Total:     {total_generated + total_skipped + total_failed}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
