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
# Simplified: only compare input file to output json (MIDI freshness ensured by update_midis.py)
should_convert() {
    local input_file="$1"
    local output_json_file="$2"
    local threshold=180
    if [ ! -f "$output_json_file" ]; then
        return 0
    fi
    local output_mtime
    output_mtime=$(stat -c %Y "$output_json_file" 2>/dev/null || echo 0)
    local input_mtime
    input_mtime=$(stat -c %Y "$input_file" 2>/dev/null || echo 0)
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
    # All .mid files are now guaranteed to match an MSCZ and be up-to-date if the MSCZ changed
    find "$input_dir" -type f -name "*.mid" -print0 | while IFS= read -r -d '' file; do
        predicted_json_basename=$(python3 "${script_dir}/pv_util.py" --get-predicted-filename "$file" --file-type "mid")
        [ -z "$predicted_json_basename" ] && { echo "Warning: Could not predict JSON filename for $file. Skipping."; continue; }
        output_json="${output_dir}/${predicted_json_basename}"
        if should_convert "$file" "$output_json"; then
            echo "Processing: $file -> $output_json"
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
            if should_convert "$file" "$output_json"; then
                echo "Processing: $file -> $output_json"
                python3 "${script_dir}/$CONVERTER" "$file" "$output_dir" $ORCHESTRA_MODE $SIMPLIFIED_MODE
            else
                echo "Skipping (up-to-date): $file"
            fi
        done
    done
fi

echo -e "\nAll conversions completed. Output files are in: $output_dir"
    else
        return 1  # No matching file found
    fi
}

# Process files based on file type
echo "Processing $FILE_TYPE files"
if [ "$FILE_TYPE" = "mid" ]; then
    # MIDI files need special processing (only those with matching MuseScore files)
    find "$input_dir" -type f -name "*.mid" -print0 | while IFS= read -r -d '' file; do
        if has_matching_companion "$file"; then
            # Get predicted JSON filename from pv_util.py
            predicted_json_basename=$(python3 "${script_dir}/pv_util.py" --get-predicted-filename "$file" --file-type "mid")
            
            if [ -z "$predicted_json_basename" ]; then
                echo "Warning: Could not predict JSON filename for $file. Skipping."
                continue
            fi
            output_json="${output_dir}/${predicted_json_basename}"
            
            # Construct the path to the companion MuseScore file (name based on original MIDI)
            input_base_for_companion="$(basename "$file" .mid)"
            midi_dir="$(dirname "$file")"
            companion_mscz_file="${midi_dir}/${input_base_for_companion}.mscz"

            if should_convert "$file" "$output_json" "$companion_mscz_file"; then
                 echo "Processing: $file (relevant input newer or JSON missing: $output_json)"
                 python3 "${script_dir}/$CONVERTER" "$file" "$output_dir" $ORCHESTRA_MODE $SIMPLIFIED_MODE
            else
                 echo "Skipping: $file (JSON $output_json exists and is up-to-date relative to .mid and .mscz)"
            fi
        else
            echo "Skipping: $file (no matching MuseScore file found)"
        fi
    done
else
    # Process MuseScore or MusicXML files directly
    extensions=("$FILE_TYPE")
    # Add .mscz extension if processing MusicXML but no specific extension given
    if [ "$FILE_TYPE" = "musicxml" ]; then 
        extensions=("xml" "musicxml")
    fi
    
    for ext in "${extensions[@]}"; do
        find "$input_dir" -type f -name "*.$ext" -print0 | while IFS= read -r -d '' file; do
            # Get predicted JSON filename from pv_util.py
            # FILE_TYPE is already "mscz" or "musicxml" here
            predicted_json_basename=$(python3 "${script_dir}/pv_util.py" --get-predicted-filename "$file" --file-type "$FILE_TYPE")

            if [ -z "$predicted_json_basename" ]; then
                echo "Warning: Could not predict JSON filename for $file. Skipping."
                continue
            fi
            output_json="${output_dir}/${predicted_json_basename}"

            if should_convert "$file" "$output_json"; then
                echo "Processing: $file (newer or JSON missing: $output_json)"
                # Also pass orchestra and simplified mode flags to other converters
                python3 "${script_dir}/$CONVERTER" "$file" "$output_dir" $ORCHESTRA_MODE $SIMPLIFIED_MODE
            else
                echo "Skipping: $file (JSON $output_json exists and is up-to-date)"
            fi
        done
    done
fi

echo -e "\nAll conversions completed. Output files are in: $output_dir"
