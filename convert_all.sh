#!/bin/bash

usage() {
    echo "Usage: $0 [-m] <input_directory>"
    echo "  -m : Process MIDI files instead of MusicXML files"
    exit 1
}

# Default to musicxml
FILE_TYPE="musicxml"
CONVERTER="musicxml_to_json.py"

# Parse options
while getopts "m" opt; do
    case $opt in
        m)
            FILE_TYPE="mid"
            CONVERTER="midi_to_json.py"
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

# Find all files of specified type and process them
find "$input_dir" -type f -name "*.$FILE_TYPE" -print0 | while IFS= read -r -d '' file; do
    echo "Processing: $file"
    python3 "${script_dir}/$CONVERTER" "$file" "$output_dir"
done

echo -e "\nAll conversions completed. Output files are in: $output_dir"
