# PianoVisionFormatter

Turns a MuseScore 4 score library (`.mscz`) into PianoVision song files (`.json`)
and keeps a Meta Quest 3 in sync with it.

Scores are rendered **directly from the `.mscz`**. MuseScore is not needed and
nothing goes through MIDI export files. The renderer is a Python port of MuseScore
4.6's own MIDI export (note lengths, ties, grace notes, ornaments, tremolos,
arpeggios, glissandi, swing, repeats and voltas, fermatas and pauses, tempo
changes, dynamics and hairpins, chord symbols, pedal). It needs Python 3.11+ and
nothing else.

## Everyday use

```bash
python3 -m pianovision sync          # render new/edited scores, copy changes to the Quest
python3 -m pianovision status        # what would change + library health report
python3 -m pianovision watch --deploy   # keep going: rebuild on every save, deploy when the Quest is plugged in
```

Settings live in `pianovision.toml` (scores folder, output folder, device folder,
orchestra/simplified options). This replaces
`convert_all.sh -m -o -s ~/Documents/MuseScore4/Scores/` followed by
`sync_pianovision_files.sh`: orchestra and simplified modes are on by default.

| command | what it does |
|---|---|
| `status [--device]` | scores to render/retire, invalid files, orphans, title changes; with `--device`, what the Quest is missing |
| `build [--dry-run] [--force [SCORE…]] [--prune-orphans]` | incremental render of the library into `PianoVision/` |
| `deploy [--dry-run] [--force-push] [--prune [-y]]` | adb push of changed files, verified by md5 |
| `sync` | `build` then `deploy` (accepts the deploy options) |
| `watch [--deploy] [--interval S]` | poll the scores folder and run `build` (and `deploy`) after each save |
| `verify [SCORE…] [--calibrate]` | re-render and compare with the files in `PianoVision/` |
| `rename [--dry-run]` | rename outputs after a score's title/composer was edited |
| `convert SCORE.mscz [-o out.json] [--midi out.mid]` | one-off conversion, no library bookkeeping |

## How the library is kept organised

`PianoVision/.manifest.json` records, for every score, the hash of its content
(the score, style and chord list inside the `.mscz`, not thumbnails or view
settings) and the output file made from it.

* **Unchanged score:** its JSON is left alone, byte for byte.
* **New or edited score:** rendered and written.
* **Moved or renamed score:** recognised by content; it keeps its output name, so the Quest keeps its history for the song.
* **Excluded score** (renamed to `.zip`) **or deleted score:** its JSON is retired to `PianoVision/.attic/`.
* **Two scores with the same title:** the second one gets a suffix from its file name, for example `howa_Didnt_I_Do_Well__Red_Sparrow_piano_solo.json`. The old scripts silently overwrote one with the other.
* **Anything that would be overwritten** is first moved to `PianoVision/.attic/<date>/`. Nothing is deleted.
* **Output file names** use the same `<first 4 letters of composer>_<title>.json` scheme as before. Names stay fixed once assigned; `rename` applies title edits.

Only `*.mscz` files are sources. `.zip` files, extensionless zip files and
`.mscbackup` folders are ignored. `status` also reports `.mscz` files that
aren't really scores (for example `Film/Disney/A_Whole_New_World_from_Aladdin.mscz`
is a MIDI file) and `.mid` files that no longer have a score.

The `.mid` files next to the scores are no longer needed.

## Deploying to the Quest

`deploy` compares md5 checksums with the headset and pushes only what differs.
PianoVision keeps its own data in the same folder
(`/sdcard/Android/data/com.ZarApps.PianoVision/files`), including
`finger_position_recordings.json`. The tool therefore only removes files that it
deployed itself and that have since left the library, and only with `--prune`
(it asks first unless you pass `-y`). USB debugging must be enabled; plugging in
the headset and allowing the prompt is enough. `adb connect <ip>` works for
wireless.

## Fidelity

Checked against the 140 existing outputs, which the old scripts made from
MuseScore-exported MIDI files:

* 123 are reproduced byte for byte with MuseScore 4.6 defaults.
* 13 more are reproduced with per-file compatibility settings. These model
  MuseScore 4.5's dynamics and tempo values that MuseScore recomputed in an open
  session. `verify --calibrate` finds the settings and stores them in the manifest.
* 4 depend on state that only existed in an open MuseScore session when the MIDI
  was exported. Those outputs are kept pinned as they are.

## Layout

```
pianovision/        the package (stdlib only)
  mscx.py             .mscz/.mscx reader -> score model (score.py)
  render.py           MuseScore 4.6 MIDI export: tempo/time-sig maps, channels, velocities, events
  playevents.py       note play events (gate time, ornaments, tremolo, arpeggio, glissando, swing)
  repeats.py tempo.py velocity.py harmony.py rhythm.py navigate.py cxxsort.py
  pvjson.py           PianoVision JSON builder (port of midi_to_json.py, output byte-identical)
  convert.py          one score -> JSON
  library.py          manifest, incremental build, adoption of existing outputs
  device.py           adb deploy
  calibrate.py        compatibility-knob search used by verify --calibrate
  cli.py              python -m pianovision
pianovision.toml    settings
PianoVision/        generated library (git-ignored): *.json, .manifest.json, .attic/
```

## Legacy scripts

`convert_all.sh`, `musescore_to_json.py`, `midi_to_json.py`,
`musicxml_to_json.py`, `update_midis.py` (headless MuseScore MIDI export) and
their helpers still work and need the packages in `requirements.txt`. They write
into the same `PianoVision/` folder; if they change a file, `status` reports it
as edited outside the pipeline. MusicXML input is only supported there.
`sync_pianovision_files.sh` now removes device files only when given `--delete`
as its second argument.
