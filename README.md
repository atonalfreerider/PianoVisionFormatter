# PianoVisionFormatter

Converts piano MusicXML or MIDI files to PianoVision json format

## Usage

Process all MusicXML files in a directory (and subdirectories):
`convert_all.sh path/to/Documents/MuseScore4/Scores`

run with `-m` to process midi instead

output jsons to ./PianoVision folder

These jsons can be copied to
`Internal shared storage/Android/data/com.ZarApps.PianoVision/files`