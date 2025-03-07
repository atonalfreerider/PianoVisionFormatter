# PianoVisionFormatter

Converts piano MusicXML or MIDI files to PianoVision json format

BUG: MusicXML tempo interpretation is not working. The tick conversion is very wrong.

Reference:

https://github.com/musescore/MuseScore/blob/master/src/importexport/midi/internal/midiexport/exportmidi.cpp  
https://github.com/musescore/MuseScore/blob/master/src/engraving/dom/tempo.cpp  
https://github.com/musescore/MuseScore/blob/master/src/engraving/dom/gradualtempochange.cpp  

Temporary fix is to rely on midi with author/composer and piece name from xml

## Usage

Process all MusicXML files in a directory (and subdirectories):
`convert_all.sh path/to/Documents/MuseScore4/Scores`

run with `-m` to process midi instead

output jsons to ./PianoVision folder

These jsons can be copied to
`Internal shared storage/Android/data/com.ZarApps.PianoVision/files`