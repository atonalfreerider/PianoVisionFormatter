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
orchestra/simplified options, and `[hands]` for Note Waterfall's hand edits). This replaces
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
| `rename [SCORE…] [--dry-run]` | rename outputs after a score's title/composer was edited; named scores also get the current naming rules (e.g. MuseScore 3 scores that the old scripts named after the file) |
| `convert SCORE.mscz [-o out.json] [--midi out.mid] [--provenance notes.jsonl] [--parts out.parts.json]` | one-off conversion, no library bookkeeping; `--provenance` also writes, per note of the song, the measure, staff, voice, chord and note element it comes from; `--parts` writes Note Waterfall's parts file |
| `hands pull\|review\|apply [FILE…]` | hand edits recorded with Note Waterfall's HAND REC → the scores, after approval (see below) |

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

## Hand edits from Note Waterfall

Note Waterfall's **HAND REC** mode watches which hand plays each note and saves the
notes that belong to the other hand in a sidecar on the headset
(`…/com.atonalfreerider.notewaterfall/files/HandEdits/<song>.hands.json`). The `hands`
command writes those changes into the MuseScore scores:

```bash
python3 -m pianovision hands pull      # copy the sidecars from the Quest into PianoVision/.hands/inbox
                                       # (the inbox is replaced only once every file arrived)
python3 -m pianovision hands review    # what would change, per score (writes nothing)
python3 -m pianovision hands apply     # the same, then a y/N per score
```

`apply` asks before every score (`--yes` skips the question; without a terminal it
refuses). For each approved score it

1. copies the original `.mscz` to `PianoVision/.attic/<date>/scores/<score path>`,
2. overwrites the score (only if it has not changed since the review),
3. runs the incremental build for that score (a calibrated score keeps its settings),
   and checks that the new JSON is exactly the one the review verified,
4. deploys the song to PianoVision and to Note Waterfall (`[hands] waterfall_deploy`,
   run as `<command> --library … --only <song>…`), and
5. moves the sidecar to `HandEdits/applied/` on the headset, once every edit in it is
   in the score, the copy on the headset is still the one that was pulled (HAND REC saved
   nothing new since: those edits would be lost) and Note Waterfall's copy of the song is
   the rebuilt one (md5 over adb). Any other sidecar stays where it is, so the app keeps
   playing those notes in the recorded hand; `pull` and `apply` again.

`--no-deploy` stops after the build; `--no-archive` leaves the sidecars on the headset.
Close the scores in MuseScore before `apply`, or a later save there overwrites the edit.

**How a hand change is written.** The PianoVision hands are the piano part's staves:
the first staff is the right hand, the second the left. A note belongs to the staff whose
*voice* holds its chord; MuseScore's cross-staff `<staffMove>` only draws a chord on the
other staff (MuseScore's own MIDI export keeps it in its staff's track). So:

* a chord whose notes all change hands moves, with its grace notes, into a free voice
  (2-4) of the other staff at the same beat; its old place becomes an invisible rest;
* a chord where only some notes change hands is split: those notes go into a new chord
  (same duration and articulations) in a free voice of the other staff;
* the moved chord gets a `<staffMove>` back to where it was drawn, so the page looks as
  before, and a chord that was already drawn on the other staff loses its staffMove;
* tied notes move with their continuations, a tuplet is rebuilt in the new voice (with
  invisible rests), ties/slurs touching a moved note are re-linked;
* a moved note keeps its velocity (`<velocity>`) when the dynamics on the two staves
  differ, and its play events (`<Events>`) when its length or timing would change
  (legato lines, swing, grace-note timing are per staff in MuseScore's playback).

Everything else in the score file stays byte for byte. Before anything is written, the
edited copy is rendered and compared with the original render: only the recorded notes
(and what goes along with them: tie continuations, grace notes) may change hands; times,
pitches, durations and velocities must be identical. Edits that fail that check, or that
cannot be written safely, are reported and skipped, for example:

* a note under an 8va/8vb line, in a nested tuplet, a two-chord tremolo, with lyrics;
* grace notes on their own, or an arpeggiated chord that would be split;
* no free voice (2-4) on the other staff at that beat;
* PianoVision's accent matching changes (accents are matched per chord and hand);
* a repeated passage recorded on only some of its passes: the score has one note for
  all passes (`--all-repeats` moves it anyway);
* songs whose library JSON is not what the score renders to now (a pinned output, or
  a score edited since the last build), MuseScore 3 files, scores with parts or linked
  staves.

The sidecar format is shared with Note Waterfall: a note is identified by its
original hand, `ticksStart`, MIDI number and `occurrence` (the how-manieth note of that
hand with the same tick and pitch); an edit whose note is already in the other hand is
reported as already in the score. Version 1 counts the notes of the song JSON's `tracksV2`.
Version 2 adds `"part"` to an edit: `"original"` counts the notes of the parts file's
original piano part, `"simplified"` the notes written on the simplified staves (see below);
an edit without it is read as in version 1. Notes of the simplified staves can change hands
too (between the two simplified staves); the orchestra cannot. A change is verified on the
song and on both piano parts. A note moves only where the song still shows the same staves
afterwards: moving the last simplified note of a hand's measure out of it (which would bring
the piano part back there) is refused; a note of the piano part hidden under a simplified
measure may come into view in the other hand where that hand is not simplified.

## Note Waterfall's parts files

PianoVision's JSON merges everything into one stream per hand: with `simplified` the
'piano-simplified' staves replace the piano part in the measures where they have notes,
and with `orchestra` the 'piano-orchestral' staves (or, without them, the orchestra
instruments) fill measures marked with 'merge' / 'end merge' and measures where the piano
rests. Note Waterfall shows the original or the simplified piano part and draws the
orchestra on its own, so `build` also writes, for every song, a parts file:
`PianoVision/NoteWaterfall/<song name>.parts.json`. It never changes the PianoVision JSON
(same notes, same bytes), and PianoVision's `deploy` does not push it.

```
{"format": 1, "song": "<song>.json", "songMd5": "<md5 of that file>", "scoreContent": "<sha256>",
 "resolution": 480, "simplified": true, "orchestra": "piano-orchestral",
 "parts": {"original":   {"right": [NOTE…], "left": [NOTE…]},
           "simplified": {"right": […], "left": […]},      # only when the score has simplified staves
           "orchestra":  {"right": […], "left": […]}}}
```

A NOTE has the `tracksV2` fields Note Waterfall reads, with PianoVision's values: `note`,
`ticksStart`, `durationTicks`, `start`, `duration`, `velocity`, `measureInd`, `accent`.
Simplified-part notes written on the simplified staves carry `"simplified": 1`; orchestra
notes carry `"staff"` (the score staff), `"program"` (instruments only) and `"merged": 1`
where PianoVision's JSON merges them into the hands. The orchestra is the 'piano-orchestral'
staves (first staff right hand) when the score has any, else the orchestra instruments
PianoVision's orchestra mode takes, split at middle C. `songMd5` ties the parts to one
version of the song (the app ignores stale parts); `scoreContent` is the manifest's hash of
the score. A library built before parts existed gets them on the next `build` without any
JSON being rewritten; `status` and `build` print how many scores have a simplified part and
orchestra notes, `verify` also compares the parts files, `convert --parts FILE` writes one.
Note Waterfall's `Tools/deploy_songs.py` pushes them next to the songs.

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
  parts.py            Note Waterfall's parts files (original / simplified piano part, orchestra)
  convert.py          one score -> JSON
  library.py          manifest, incremental build, adoption of existing outputs
  device.py           adb deploy
  calibrate.py        compatibility-knob search used by verify --calibrate
  handedits.py        hand edits from Note Waterfall -> the scores (hands pull/review/apply)
  cli.py              python -m pianovision
pianovision.toml    settings
PianoVision/        generated library (git-ignored): *.json, .manifest.json, .attic/, NoteWaterfall/*.parts.json
```

## Legacy scripts

`convert_all.sh`, `musescore_to_json.py`, `midi_to_json.py`,
`musicxml_to_json.py`, `update_midis.py` (headless MuseScore MIDI export) and
their helpers still work and need the packages in `requirements.txt`. They write
into the same `PianoVision/` folder; if they change a file, `status` reports it
as edited outside the pipeline. MusicXML input is only supported there.
`sync_pianovision_files.sh` now removes device files only when given `--delete`
as its second argument.
