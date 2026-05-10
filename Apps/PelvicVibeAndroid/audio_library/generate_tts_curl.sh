#!/bin/bash
# Generate TTS audio files using the Orchestrator's /tts endpoint
# This script uses curl to call the running Orchestrator service

AUDIO_LIB="$(dirname "$(realpath "$0")")"
UPLOADS_DIR="${BLACKBOX_ROOT:?}/Portal/uploads"
TTS_URL="http://localhost:9091/tts"

# Function to generate a single TTS file
generate_tts() {
    local text="$1"
    local voice="$2"
    local filename="$3"
    local output_dir="$4"

    local output_path="${output_dir}/${filename}"

    # Skip if file already exists
    if [[ -f "$output_path" ]]; then
        echo "[SKIP] $filename (exists)"
        return 0
    fi

    echo -n "Generating $filename: \"$text\"... "

    # Call TTS endpoint with return_json=true
    response=$(curl -s -X POST "$TTS_URL" \
        -H "Content-Type: application/json" \
        -d "{\"text\": \"$text\", \"voice\": \"$voice\", \"model\": \"tts-1-hd\", \"format\": \"mp3\", \"return_json\": true}")

    # Extract audio_url from response
    audio_url=$(echo "$response" | grep -oP '"audio_url"\s*:\s*"\K[^"]+')

    if [[ -n "$audio_url" ]]; then
        # Extract filename from URL and copy to destination
        uploaded_file="${UPLOADS_DIR}/$(basename $audio_url)"
        if [[ -f "$uploaded_file" ]]; then
            cp "$uploaded_file" "$output_path"
            echo "[OK]"
            return 0
        fi
    fi

    echo "[FAILED]"
    echo "Response: $response"
    return 1
}

# Number words
declare -A NUMBERS=(
    [1]="One" [2]="Two" [3]="Three" [4]="Four" [5]="Five"
    [6]="Six" [7]="Seven" [8]="Eight" [9]="Nine" [10]="Ten"
    [11]="Eleven" [12]="Twelve" [13]="Thirteen" [14]="Fourteen" [15]="Fifteen"
    [16]="Sixteen" [17]="Seventeen" [18]="Eighteen" [19]="Nineteen" [20]="Twenty"
    [21]="Twenty-one" [22]="Twenty-two" [23]="Twenty-three" [24]="Twenty-four" [25]="Twenty-five"
    [26]="Twenty-six" [27]="Twenty-seven" [28]="Twenty-eight" [29]="Twenty-nine" [30]="Thirty"
    [31]="Thirty-one" [32]="Thirty-two" [33]="Thirty-three" [34]="Thirty-four" [35]="Thirty-five"
    [36]="Thirty-six" [37]="Thirty-seven" [38]="Thirty-eight" [39]="Thirty-nine" [40]="Forty"
    [41]="Forty-one" [42]="Forty-two" [43]="Forty-three" [44]="Forty-four" [45]="Forty-five"
    [46]="Forty-six" [47]="Forty-seven" [48]="Forty-eight" [49]="Forty-nine" [50]="Fifty"
    [51]="Fifty-one" [52]="Fifty-two" [53]="Fifty-three" [54]="Fifty-four" [55]="Fifty-five"
    [56]="Fifty-six" [57]="Fifty-seven" [58]="Fifty-eight" [59]="Fifty-nine" [60]="Sixty"
)

# Workout cues
declare -A WORKOUT_CUES=(
    ["pushup"]="Push up!"
    ["crunch"]="Crunch!"
    ["squat"]="Squat!"
    ["hold"]="Hold!"
    ["burpee"]="Burpee!"
    ["run"]="Run!"
    ["sprint"]="Sprint!"
    ["go"]="Go!"
)

# Rest cues
declare -A REST_CUES=(
    ["rest"]="Rest."
    ["walk"]="Walk."
    ["recover"]="Recover."
)

# System cues
declare -A SYSTEM_CUES=(
    ["countdown"]="Three, two, one, go!"
    ["workout_complete"]="Workout complete! Great job!"
    ["keep_going"]="Keep going!"
    ["halfway"]="Halfway there!"
)

# Set completion announcements
declare -A SET_COMPLETE=(
    [1]="Set one complete!"
    [2]="Set two complete!"
    [3]="Set three complete!"
    [4]="Set four complete!"
    [5]="Set five complete!"
    [6]="Set six complete!"
    [7]="Set seven complete!"
    [8]="Set eight complete!"
    [9]="Set nine complete!"
    [10]="Set ten complete!"
)

generate_for_voice() {
    local voice="$1"
    local prefix="$2"
    local output_dir="$3"

    mkdir -p "$output_dir"

    echo "============================================================"
    echo "Generating ${prefix^^} voice files (voice: $voice)"
    echo "Output: $output_dir"
    echo "============================================================"

    # Numbers 1-60
    echo ""
    echo "--- Numbers 1-60 ---"
    for num in $(seq 1 60); do
        word="${NUMBERS[$num]}"
        generate_tts "$word" "$voice" "${prefix}_num_${num}.mp3" "$output_dir"
    done

    # Workout cues
    echo ""
    echo "--- Workout Cues ---"
    for cue in "${!WORKOUT_CUES[@]}"; do
        text="${WORKOUT_CUES[$cue]}"
        generate_tts "$text" "$voice" "${prefix}_${cue}.mp3" "$output_dir"
    done

    # Rest cues
    echo ""
    echo "--- Rest Cues ---"
    for cue in "${!REST_CUES[@]}"; do
        text="${REST_CUES[$cue]}"
        generate_tts "$text" "$voice" "${prefix}_${cue}.mp3" "$output_dir"
    done

    # System cues
    echo ""
    echo "--- System Cues ---"
    for cue in "${!SYSTEM_CUES[@]}"; do
        text="${SYSTEM_CUES[$cue]}"
        generate_tts "$text" "$voice" "${prefix}_${cue}.mp3" "$output_dir"
    done

    # Set complete announcements
    echo ""
    echo "--- Set Complete Announcements ---"
    for num in $(seq 1 10); do
        text="${SET_COMPLETE[$num]}"
        generate_tts "$text" "$voice" "${prefix}_set_complete_${num}.mp3" "$output_dir"
    done
}

# Main
echo "============================================================"
echo "WorkoutVibe TTS Audio Generator"
echo "Female: nova | Male: onyx | Model: tts-1-hd"
echo "============================================================"

# Check if voice argument is provided
if [[ -n "$1" ]]; then
    case "$1" in
        female)
            generate_for_voice "nova" "f" "${AUDIO_LIB}/female"
            ;;
        male)
            generate_for_voice "onyx" "m" "${AUDIO_LIB}/male"
            ;;
        *)
            echo "Unknown voice: $1"
            echo "Usage: $0 [female|male]"
            exit 1
            ;;
    esac
else
    # Generate for both voices
    generate_for_voice "nova" "f" "${AUDIO_LIB}/female"
    generate_for_voice "onyx" "m" "${AUDIO_LIB}/male"
fi

echo ""
echo "============================================================"
echo "Generation complete!"
echo "============================================================"
