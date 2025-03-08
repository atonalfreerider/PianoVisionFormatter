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

# Function to check if a MIDI file has a matching MuseScore file
has_matching_companion() {
    local midi_file="$1"
    local midi_base="$(basename "$midi_file" .mid)"
    local midi_dir="$(dirname "$midi_file")"
    
    # Check for .mscz extension
    if [ -f "${midi_dir}/${midi_base}.mscz" ]; then
        return 0  # Success - matching file found
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
            echo "Processing: $file (has matching MuseScore file)"
            python3 "${script_dir}/$CONVERTER" "$file" "$output_dir" $ORCHESTRA_MODE $SIMPLIFIED_MODE
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
            echo "Processing: $file"
            # Also pass orchestra and simplified mode flags to other converters
            python3 "${script_dir}/$CONVERTER" "$file" "$output_dir" $ORCHESTRA_MODE $SIMPLIFIED_MODE
        done
    done
fi

echo -e "\nAll conversions completed. Output files are in: $output_dir"
