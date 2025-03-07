# PianoVisionFormatter

Converts piano MusicXML or MIDI files to PianoVision json format

BUG: MusicXML tempo interpretation is not working. The tick conversion is very wrong.
BUG: The notes are also sometimes inaccurate

Reference:

https://github.com/musescore/MuseScore/blob/master/src/importexport/midi/internal/midiexport/exportmidi.cpp  
https://github.com/musescore/MuseScore/blob/master/src/engraving/dom/tempo.cpp  
https://github.com/musescore/MuseScore/blob/master/src/engraving/dom/gradualtempochange.cpp  

Tempo extraction:
- If a MIDI file with the same name exists in the same folder as the MusicXML file, tempo will be extracted from the MIDI file instead
- Otherwise, will attempt to extract tempo from MusicXML
- For best results, use MIDI files for accurate tempo information

## Usage

Process all MusicXML files in a directory (and subdirectories):
`convert_all.sh path/to/Documents/MuseScore4/Scores`

run with `-m` to process midi instead

output jsons to ./PianoVision folder

These jsons can be copied to
`Internal shared storage/Android/data/com.ZarApps.PianoVision/files`