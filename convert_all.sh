#!/bin/bash

usage() {
    echo "Usage: $0 [-m] [-x] [-o] [-s] <input_directory>"
    echo "  -m : Process MIDI files instead of MuseScore files"
    echo "  -x : Process MusicXML files instead of MuseScore files"
    echo "  -o : Enable orchestra mode (include secondary piano/orchestra when space allows)"
    echo "  -s : Enable simplified mode (overwrite with simplified piano parts when available)"
    echo "  Default: Process MuseScore (.mscz) files" 
    exit 1
}

# Default to musescore
FILE_TYPE="mscz"
CONVERTER="musescore_to_json.py"
ORCHESTRA_MODE=false
SIMPLIFIED_MODE=false

# Parse options
while getopts "mxos" opt; do
    case $opt in
        m)
            FILE_TYPE="mid"
            CONVERTER="midi_to_json.py"
            ;;
        x)
            FILE_TYPE="musicxml"
            CONVERTER="musicxml_to_json.py"
            ;;
        o)
            ORCHESTRA_MODE=true
            ;;
        s)
            SIMPLIFIED_MODE=true
            ;;
        *)
            usage
            ;;
    esac
done

# Shift past the parsed options
shift $((OPTIND-1))

if [ "$#" -ne 1 ]; then
    usage
fi

input_dir="$1"
script_dir="$(dirname "$(readlink -f "$0")")"
output_dir="${script_dir}/PianoVision"

# Create output directory
mkdir -p "$output_dir"

# --- Helper Function: Check if conversion is needed based on timestamps ---
# Now also considers optional companion MIDI (forces reconvert if MIDI regenerated)
should_convert() {
    local input_file="$1"
    local output_json_file="$2"
    local companion_mid="$3"
    local threshold=180
    if [ ! -f "$output_json_file" ]; then
        return 0
    fi
    local output_mtime input_mtime midi_mtime
    output_mtime=$(stat -c %Y "$output_json_file" 2>/dev/null || echo 0)
    input_mtime=$(stat -c %Y "$input_file" 2>/dev/null || echo 0)
    if [ -n "$companion_mid" ] && [ -f "$companion_mid" ]; then
        midi_mtime=$(stat -c %Y "$companion_mid" 2>/dev/null || echo 0)
        # If MIDI newer than JSON by threshold -> reconvert
        if [ $((midi_mtime - output_mtime)) -gt $threshold ]; then
            return 0
        fi
        # Treat MIDI as authoritative over MSCZ for tempo layout freshness
        if [ $midi_mtime -gt $input_mtime ]; then
            input_mtime=$midi_mtime
        fi
    fi
    if [ $((input_mtime - output_mtime)) -gt $threshold ]; then
        return 0
    fi
    return 1
}

# Ensure MIDIs are present/fresh for every MSCZ first
echo "Ensuring MIDI companions are current (headless MuseScore export)..."
python3 "${script_dir}/update_midis.py" "$input_dir"

# Process files based on file type
echo "Processing $FILE_TYPE files"
if [ "$FILE_TYPE" = "mid" ]; then
    # All .mid files must have a matching .mscz; orphan MIDIs are ignored.
    find "$input_dir" -type f -name "*.mid" -print0 | while IFS= read -r -d '' file; do
        companion_mscz="${file%.*}.mscz"
        if [ ! -f "$companion_mscz" ]; then
            echo "Skipping (no matching MSCZ): $file"
            continue
        fi
        # Predict output name (still based on MIDI + metadata extracted from companion MSCZ)
        predicted_json_basename=$(python3 "${script_dir}/pv_util.py" --get-predicted-filename "$file" --file-type "mid")
        [ -z "$predicted_json_basename" ] && { echo "Warning: Could not predict JSON filename for $file. Skipping."; continue; }
        output_json="${output_dir}/${predicted_json_basename}"
        # For staleness, treat MSCZ as primary source and MIDI as companion (so either changing forces reconvert)
        if should_convert "$companion_mscz" "$output_json" "$file"; then
            echo "Processing: $file (with $companion_mscz) -> $output_json"
            python3 "${script_dir}/$CONVERTER" "$file" "$output_dir" $ORCHESTRA_MODE $SIMPLIFIED_MODE
        else
            echo "Skipping (up-to-date): $file"
        fi
    done
else
    # MuseScore (mscz) or MusicXML
    extensions=("$FILE_TYPE")
    if [ "$FILE_TYPE" = "musicxml" ]; then
        extensions=("xml" "musicxml")
    fi
    for ext in "${extensions[@]}"; do
        find "$input_dir" -type f -name "*.$ext" -print0 | while IFS= read -r -d '' file; do
            predicted_json_basename=$(python3 "${script_dir}/pv_util.py" --get-predicted-filename "$file" --file-type "$FILE_TYPE")
            [ -z "$predicted_json_basename" ] && { echo "Warning: Could not predict JSON filename for $file. Skipping."; continue; }
            output_json="${output_dir}/${predicted_json_basename}"
            companion_mid="${file%.*}.mid"
            # For MuseScore mode require companion MIDI (should exist after update_midis)
            if [ "$FILE_TYPE" = "mscz" ] && [ ! -f "$companion_mid" ]; then
                echo "Warning: Missing companion MIDI for $file (expected $companion_mid). Skipping."
                continue
            fi
            if should_convert "$file" "$output_json" "$companion_mid"; then
                echo "Processing: $file -> $output_json"
                python3 "${script_dir}/$CONVERTER" "$file" "$output_dir" $ORCHESTRA_MODE $SIMPLIFIED_MODE
            else
                echo "Skipping (up-to-date): $file"
            fi
        done
    done
fi

echo -e "\nAll conversions completed. Output files are in: $output_dir"