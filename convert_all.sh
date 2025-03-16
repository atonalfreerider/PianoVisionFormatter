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

# Function to check if a MIDI file has a matching MusicXML file
has_matching_musicxml() {
    local midi_file="$1"
    local midi_base="$(basename "$midi_file" .mid)"
    local midi_dir="$(dirname "$midi_file")"
    
    # Check for both .musicxml and .xml extensions
    if [ -f "${midi_dir}/${midi_base}.musicxml" ] || [ -f "${midi_dir}/${midi_base}.xml" ]; then
        return 0  # Success - matching file found
    else
        return 1  # No matching file found
    fi
}

# Process files based on mode
if [ "$FILE_TYPE" = "mid" ]; then
    echo "Processing MIDI files (only those with matching MusicXML files)"
    
    # Find all MIDI files and filter those with matching MusicXML
    find "$input_dir" -type f -name "*.mid" -print0 | while IFS= read -r -d '' file; do
        if has_matching_musicxml "$file"; then
            echo "Processing: $file (has matching MusicXML)"
            python3 "${script_dir}/$CONVERTER" "$file" "$output_dir"
        else
            echo "Skipping: $file (no matching MusicXML found)"
        fi
    done
else
    # Original behavior for MusicXML files
    echo "Processing $FILE_TYPE files"
    find "$input_dir" -type f -name "*.$FILE_TYPE" -print0 | while IFS= read -r -d '' file; do
        echo "Processing: $file"
        python3 "${script_dir}/$CONVERTER" "$file" "$output_dir"
    done
fi

echo -e "\nAll conversions completed. Output files are in: $output_dir"
