#!/bin/bash

usage() {
    echo "Usage: $0 [-m|-x] <input_directory>"
    echo "  -m : Process MIDI files instead of MuseScore files"
    echo "  -x : Process MusicXML files instead of MuseScore files"
    echo "  Default: Process MuseScore (.mscz) files"
    exit 1
}

# Default to musescore
FILE_TYPE="mscz"
CONVERTER="musescore_to_json.py"

# Parse options
while getopts "mx" opt; do
    case $opt in
        m)
            FILE_TYPE="mid"
            CONVERTER="midi_to_json.py"
            ;;
        x)
            FILE_TYPE="musicxml"
            CONVERTER="musicxml_to_json.py"
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
            python3 "${script_dir}/$CONVERTER" "$file" "$output_dir"
        else
            echo "Skipping: $file (no matching MuseScore file found)"
        fi
    done
else
    # Process MuseScore or MusicXML files directly
    find "$input_dir" -type f -name "*.$FILE_TYPE" -print0 | while IFS= read -r -d '' file; do
        echo "Processing: $file"
        python3 "${script_dir}/$CONVERTER" "$file" "$output_dir"
    done
fi

echo -e "\nAll conversions completed. Output files are in: $output_dir"
