# PianoVisionFormatter

Converts piano MuseScore .mscz MusicXML .musicxml or MIDI .mid files to PianoVision json format  

BUG: MuseScore and MusicXML tempo interpretation and measure timing is not working. The tick conversion is very wrong.  
BUG: The notes are also sometimes inaccurate.  
BUG: midi is not distinguishing between main piano and other piano.  
BUG: MuseScore does not handle pickup measures correctly.  

Reference:  

https://github.com/musescore/MuseScore/tree/master/src/engraving/compat/midi  

Tempo extraction:  
- If a MIDI file with the same name exists in the same folder as the MuseScore file, tempo will be extracted from the MIDI file instead  
- Otherwise, will attempt to extract tempo from MuseScore  
- For best results, use MIDI files for accurate tempo information  

## Usage

Process all MuseScore files in a directory (and subdirectories):  
`convert_all.sh path/to/Documents/MuseScore4/Scores`  

run with `-m` to process midi instead (more reliable tempo interpretation)  
run with '-x' to process musicxml  

output jsons to ./PianoVision folder

These jsons can be copied to
`Internal shared storage/Android/data/com.ZarApps.PianoVision/files`

Each script can be run with a single argument to the file to be converted to json  