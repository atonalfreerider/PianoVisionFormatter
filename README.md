# PianoVisionFormatter

Converts MuseScore (.mscz), MusicXML (.musicxml/.xml) or MIDI (.mid) files to PianoVision json format.

Default mode (no -m flag):
- update_midis.py is run first to (re)generate companion .mid files for every .mscz (missing or outdated).
- musescore_to_json.py then converts each .mscz and uses its companion .mid for tempo and measure timing.
- Only if the companion .mid is absent (unexpected) will it fall back to (less accurate) direct MuseScore timing.

-m flag (MIDI mode):
- Skips .mscz parsing and converts the .mid directly with midi_to_json.py.

-x flag:
- Processes MusicXML files (no automatic MIDI generation).

Current known issues:
- MuseScore / MusicXML direct tempo & measure derivation (fallback path) is still inaccurate.
- Some note parsing edge cases remain.
- Pickup measures need better handling when no MIDI is available.

Tempo extraction priority:
1. Companion MIDI (always present in default flow due to pre-generation).
2. Fallback MuseScore tempo parsing (only if MIDI missing).

Usage examples:
convert_all.sh /path/to/MuseScore4/Scores
convert_all.sh -m /path/to/MuseScore4/Scores
convert_all.sh -x /path/to/xml/library
convert_all.sh -m -o -s /home/john/Documents/MuseScore4/Scores

After generation, copy JSONs to:
Internal shared storage/Android/data/com.ZarApps.PianoVision/files

Manual MuseScore part renaming inside MSCX (if needed):
1. Rename .mscz -> .zip
2. Unzip
3. Edit .mscx
4. Re-zip contents (ensure directory structure intact)
5. Rename back to .mscz

Sync to device:
sync_pianovision_files.sh /home/john/Desktop/Piano/PianoVisionFormatter/PianoVision