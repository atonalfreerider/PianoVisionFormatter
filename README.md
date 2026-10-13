# PianoVisionFormatter

Turns a MuseScore 4 score library (`.mscz`) into PianoVision song files (`.json`)
and keeps a Meta Quest 3 in sync with it.

Scores are rendered **directly from the `.mscz`**. MuseScore is not needed and
nothing goes through MIDI export files. The renderer is a Python port of MuseScore
4.6's and 4.7's own MIDI export (note lengths, ties, grace notes, ornaments, tremolos,
arpeggios, glissandi, swing, repeats and voltas, fermatas and pauses, tempo
changes, dynamics and hairpins, chord symbols, pedal): scores saved by MuseScore 4.7
are rendered as 4.7 exports them, older scores as 4.6 does (see [MuseScore 4.7](#musescore-47)).
It needs Python 3.11+ and nothing else.

## Everyday use

```bash
python3 -m pianovision sync          # render new/edited scores, copy changes to the Quest
python3 -m pianovision status        # what would change + library health report
python3 -m pianovision watch --deploy   # keep going: rebuild on every save, deploy when the Quest is plugged in
```

Settings live in `pianovision.toml` (scores folder, output folder, device folder,
orchestra/simplified options, and `[hands]` for Note Waterfall's hand edits); options for single scores
(a title, the piano staves, a simplified part from another score) in `scores.json` (see
[Per-score options](#per-score-options-scoresjson)). This replaces
`convert_all.sh -m -o -s ~/Documents/MuseScore4/Scores/` followed by
`sync_pianovision_files.sh`: orchestra and simplified modes are on by default.

| command | what it does |
|---|---|
| `status [--device]` | scores to render/retire, invalid files, orphans, title changes; with `--device`, what the Quest is missing |
| `build [--dry-run] [--force [SCORE…]] [--keep SCORE…] [--prune-orphans] [--report FILE]` | incremental render of the library into `PianoVision/`; `--keep` keeps the current output of changed scores as it is (pinned); `--report` also writes what it did (failed scores, ...) as JSON |
| `deploy [--dry-run] [--force-push] [--prune [-y]]` | adb push of changed files, verified by md5 |
| `sync` | `build` then `deploy` (accepts the deploy options) |
| `watch [--deploy] [--interval S]` | poll the scores folder and run `build` (and `deploy`) after each save |
| `verify [SCORE…] [--calibrate]` | re-render and compare with the files in `PianoVision/` |
| `rename [SCORE…] [--dry-run]` | rename outputs after a score's title/composer was edited; named scores also get the current naming rules (e.g. MuseScore 3 scores that the old scripts named after the file) |
| `convert SCORE.mscz [-o out.json] [--midi out.mid] [--provenance notes.jsonl] [--parts out.parts.json]` | one-off conversion, no library bookkeeping; `--provenance` also writes, per note of the song, the measure, staff, voice, chord and note element it comes from; `--parts` writes Note Waterfall's parts file |
| `hands pull\|review\|apply [FILE…]` | hand edits recorded with Note Waterfall's HAND REC → the scores, after approval (see below) |
| `gui [--read-only] [--no-device]` | the Piano Library window (see below) |
| `accompaniment [--only GLOB…] [--out DIR] [--force] [--dry-run] [--list] [--no-timing]` | Note Waterfall's companion audio: each score's orchestra (everything but the piano part) rendered by MuseScore with Muse Sounds, one mp3 per song (see below) |
| `arrangement [SCORE…] [--write]` | the measure alignment of a simplified part made from a piano arrangement (`scores.json`), as a report; `build` writes it to `.arrangements/` (see below) |
| `recordings match\|align\|retime\|status\|confirm\|reject [--only GLOB…]` | Note Waterfall's companion audio from recordings you own: each song linked to a recording in the music folder (a TypeSafe judgment), aligned with the song and retimed onto its timeline (see below) |

## Piano Library (the window)

Double-click **Piano Library** on the desktop (or find it in Activities) for a window that does
the everyday work without a terminal. It is `python3 -m pianovision gui`; GTK 4 with libadwaita
(plain GTK 4 when libadwaita is missing), stdlib + PyGObject only.

* **Library:** every score with its composer, title, song file and status (up to date, new,
  edited, moved, output missing, parts to write, staves to check, failed, pinned, changed outside,
  to retire, excluded, not a score), whether it has a simplified part and orchestra, and whether PianoVision
  and Note Waterfall on the headset have the current version (md5 over adb; Note Waterfall also
  needs the current parts file). Search box (every word must match composer, title, file or status)
  and a filter (needs a build, not current in either app, problems, title changed, ...). The header
  counts everything and shows the headset: connected / unauthorized / offline, asleep or awake, free
  space. adb is polled every 4 s while the window is open; the push buttons are off without a headset.
* **Build** runs `build` (render new and edited scores, write the parts files, retire removed scores
  to the attic) with its output in the log pane; Cancel stops it. Failed scores stay marked
  "failed" (from `build --report`, kept in `PianoVision/.gui/last_build.json`) until they are saved
  again.
* **Push to headset…** first reads the headset and shows, per app, what would be pushed; tick
  PianoVision, Note Waterfall or both (the ☰ menu also has each on its own). PianoVision uses
  `deploy`'s plan and push (md5-verified); Note Waterfall runs its `Tools/deploy_songs.py`
  (songs, parts files, audio, portraits). Files this tool deployed earlier that left the library
  are listed and kept; ticking "also delete" opens a second dialog listing exactly the files that
  will be deleted from the headset (Note Waterfall's list is checked again right before it runs).
* **Sync** = Build, then the same push dialog for both apps.
* **Rename…** lists the songs `rename` would rename; songs whose score title changed since they
  were named are ticked (e.g. after shortening the paragraph-long "Une nuit sur le Mont Chauve…"
  subtitle in MuseScore and building). Others (newer naming rules only) are listed unticked.
* **Orphans & strays:** songs no score makes any more (**Retire orphans…** moves exactly the listed
  files to `.attic/<date>/`, nothing is deleted), stray `.mid` files, files that need a look.
* **Hand edits:** Pull from headset, Review (per score: measure and beat, pitch, from → to hand,
  status, and the conflicts and skipped edits with their reasons), and **Apply…**, which is
  `hands apply` with the question asked in a dialog for every score (Skip / Overwrite score): the
  same backup to the attic, verified rebuild, deploy to both apps and archiving of the hand-edit
  files on the headset. Without a headset the scores are rebuilt but not pushed.
* Score rows: **Open in MuseScore** (double-click; `mscore4portable` / `mscore` from the PATH or
  `~/.local/bin`), **Show score file**, **Show song file**.

Everything slow runs in the background with a progress bar; errors come up as a dialog. Options:
`gui --read-only` (look only: every action that writes is off), `--no-device` (no adb at all),
and the usual `--config` / `--scores` / `--output` before `gui`.

The launcher is installed by `desktop/install-launcher.sh`: `~/Desktop/Piano Library.desktop` and
`~/.local/share/applications/org.pianovision.Library.desktop` (both executable and marked
trusted with `gio set … metadata::trusted true`, so a double-click starts it), the icon in
`~/.local/share/icons/hicolor/scalable/apps/`. Both run `desktop/piano-library.sh`, which logs to
`~/.cache/piano-library/launcher.log` and shows a dialog if the window cannot start.

## How the library is kept organised

`PianoVision/.manifest.json` records, for every score, the hash of its content
(the score, style and chord list inside the `.mscz`, not thumbnails or view
settings) and the output file made from it.

* **Unchanged score:** its JSON is left alone, byte for byte.
* **Score saved again without a change to its music** (e.g. by a newer MuseScore): its JSON is left
  alone too when the new file still renders to exactly that JSON, if need be with the knobs that model
  the older version (see [MuseScore 4.7](#musescore-47)); a pinned JSON stays while the score has the
  same notes. `build` lists these as "score saved again, output kept as it is".
* **New or edited score:** rendered and written.
* **Output made under an older staff classification** (see [Which staves are the piano](#which-staves-are-the-piano);
  the manifest records the revision): the next `build` renders the score again with its entry's knobs and
  replaces the output only when the score's staves are classified differently now and the render differs;
  every other output stays byte for byte (`status`: "to check against the revised staff classification").
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

## Which staves are the piano

The score's staves are the MIDI tracks of the render. The old rules, kept for `.mid` input: a staff named
'piano-simplified', 'piano-orchestral' or '…piano…', or with a program 0-7 on any of its channels, is a piano
staff (the first of its kind the right hand, the others the left; with none, the first two staves); the
others with an orchestra program (0-7, 40-51, 64-95, not 112-119) are PianoVision's orchestra. A rendered
score corrects them with what the score says (`pianovision/pvjson.py`, `staff_roles`):

* **Percussion** (a drumset) is neither piano nor orchestra (Shape of You's tambourine and hand clap, the
  drum kits of Rach-3 II/III, Imaginary and Fool Like Me were in the left hand).
* **Vocal staves** go to Note Waterfall's orchestra (`"vocal": 1`), not to PianoVision, whose orchestra
  never took voices: a voice instrument whatever its other channels play (Love Has No Pride's soprano
  became the right hand through the program-0 channel its chord symbols play on), and a one-staff piano
  part with lyrics in a score with a grand-staff piano part (Doctor My Eyes' solo). Only the sung line goes
  there, not the chord symbols written on the staff.
* **A piano part's staves stay together:** MuseScore writes the program only on a part's top staff, so the
  lower staff of a part that is piano by its program is piano too ("Klavier", "Фортепиано").
* **A choir written on piano staves** (several piano parts, all with lyrics: Doctor My Eyes' soprano/alto and
  tenor/bass) is reduced like a rehearsal piano: the first part's staves are the right hand, the others the left.

Two piano parts without lyrics (Schubert's Serenade, Vocalise) keep the old hands. Staves named by the build
options (`scores.json` `piano_staves`) are the piano part as before; of the other staves percussion is still left
out and voices are vocal staves. Of the library's 146 scores, 9 PianoVision songs changed with these rules (Love
Has No Pride, A Natural Woman, Doctor My Eyes, Shape of You, Imaginary, Rach-3 II and III, Fool Like Me, and
Rhapsody in Blue with orchestra, whose percussion was in the left hand) and Fountain of Sorrow's parts file (its
voice); the rest are byte for byte the same. The companion audio (`accompaniment`) mutes the vocal lines the
formatter finds with the voices (`mute_vocals`) and skips a score whose orchestra is only a vocal line.

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
 "markers": [{"time": 12.5, "text": "Più mosso", "kind": "tempo", "measure": 17}, …],
 "lyrics": {"syllables": 312, "verses": 2, "staves": ["Piano"], "sources": ["piano"]},   # or null
 "parts": {"original":   {"right": [NOTE…], "left": [NOTE…]},
           "simplified": {"right": […], "left": […]},      # only when the score has simplified staves
           "orchestra":  {"right": […], "left": […]}}}
```

A NOTE has the `tracksV2` fields Note Waterfall reads, with PianoVision's values: `note`,
`ticksStart`, `durationTicks`, `start`, `duration`, `velocity`, `measureInd`, `accent`.
Simplified-part notes written on the simplified staves carry `"simplified": 1`; orchestra
notes carry `"staff"` (the score staff), `"program"` (instruments and vocal staves), `"vocal": 1`
(vocal staves) and `"merged": 1` where PianoVision's JSON merges them into the hands. The orchestra
is the 'piano-orchestral' staves (first staff right hand) when the score has any, else the orchestra
instruments PianoVision's orchestra mode takes and the vocal staves' sung lines, split at middle C. `songMd5` ties the parts to one
version of the song (the app ignores stale parts); `scoreContent` is the manifest's hash of
the score. A library built before parts existed gets them on the next `build` without any
JSON being rewritten; `status` and `build` print how many scores have a simplified part and
orchestra notes, `verify` also compares the parts files, `convert --parts FILE` writes one.
Note Waterfall's `Tools/deploy_songs.py` pushes them next to the songs.

**Markers** (`pianovision/markers.py`): the score's key moments for Note Waterfall's song
scrubber (chapters) and the small text falling with its notes, in time order. `time` is on the
notes' clock (same tempo map, pauses and fermatas; repeats and jumps unrolled, so a mark in a
repeated passage appears once per pass), `measure` the score's 1-based measure number, `kind`
one of `rehearsal` (rehearsal marks), `section` ("Intro", "Cadenza", "Theme", a coda sign),
`tempo` (a new tempo in words: "Andante", "a tempo", "Più mosso"; bare metronome marks are
not kept), `gradual` ("rit.", "accel.": a gradual tempo change line or words), `expression`
("dolce", "con brio") and `text` (other system or piano-staff text). Left out: invisible
texts, dynamics (`p`, `cresc.`), the `merge` / `end merge` control texts, harp pedal and chord
names, orchestra / hand / articulation / pedal directions (`div.`, `L.H.`, `ten.`, `Ped.`),
instrument cues and the orchestra staves' texts, jumps (D.S., segno, fine); the same text at
one moment is kept once. 83 of the library's 144 songs have markers. Parts files written
before markers existed are rewritten by the next `build` (the manifest records the parts
revision; the PianoVision JSON is not touched).

**Lyrics** (`pianovision/lyrics.py`): a note that sings a syllable carries
`"lyric": {"text": "…", "syllabic": "single"|"begin"|"middle"|"end", "extend": true|false}`
(the text as written, trimmed, without an added hyphen or the verse's number; `extend`:
MuseScore's melisma, the syllable held over the following notes). Repeats are unrolled as the
notes are: on the k-th pass through a measure a chord sings its verse k, nothing when verse k is
sung elsewhere in that measure, else its lowest verse. The syllable goes on the highest struck
note of the chord it is written on (one on a rest or a tied-over chord has no note). A vocal
staff (not part of the piano part, or a voice instrument wherever it lands) gives its notes
the syllable and `"sung": true`, and the piano part's note struck at the same moment (30 ms)
at the same pitch, else an octave away (right hand first), a copy; the simplified part carries
the original part's syllables to its notes of the same onset and pitch. `lyrics` sums them
up (`null` without lyrics); `status` and `build` print how many scores have lyrics and on
which kind of staff (11 of the library's 144; two on vocal staves: Love Has No Pride's soprano and Doctor My
Eyes' solo). Parts
revision 3: the next `build` writes every parts file again.

## Per-score options (scores.json)

`scores.json` (checked in next to `pianovision.toml`; `[convert] options` names it) holds options for single
scores, keyed by the score's path below the scores folder (keys starting with `_` are comments). Only what `build`
makes of the score changes, never the score.

| option | what it does |
|---|---|
| `title`, `composer` | the song's title and composer: PianoVision's name and artist, and the output name (`<first 4 letters of the composer>_<title>.json`), instead of the score's title frame and metadata |
| `piano_staves` | the staves (1-based, right hand first) that are the piano part, whatever their names and programs; every other staff is orchestra by its program (percussion and voices excepted: [Which staves are the piano](#which-staves-are-the-piano)) |
| `hands` | `"staves"` (default), or `"split"`: the piano part's notes go to the hands by pitch and reach (`pianovision/handsplit.py`: a beam search over the chords for the split between the hands, costing a hand's span beyond an octave, crossing the other hand's held notes, movement and register), for a MIDI import that wrote both hands on one staff. Hand edits cannot be written into such a score (`hands review` says so) |
| `simplified_from`, `measure_map` | the simplified part is another score's piano part (a piano arrangement of the same music), measure-aligned (below) |

The manifest records, per score, a hash of the options that make its song and, with an arrangement, the arrangement
score's content hash and knobs: a change renders the score again; an edited arrangement (or a changed `measure_map`)
writes only its parts file again (the PianoVision JSON stays byte for byte).

**Simplified parts from arrangements** (`pianovision/arrangement.py`). Rhapsody in Blue with its orchestra gets the
piano solo arrangement as its simplified part, which Note Waterfall shows by default:

```json
"Gershwin/Rhapsody_in_blue__George_Gershwin.mscz": {
  "title": "Rhapsody in Blue (Piano and Orchestra)", "composer": "Gershwin",
  "simplified_from": "Gershwin/Rhapsody_in_Blue_Piano_solo.mscz", "measure_map": "auto"}
```

1. **Alignment.** Both scores are unrolled through their repeats and jumps. Every measure as played gets a
   duration-weighted pitch-class profile, the pitch class of the lowest and the highest sounding note over time
   (bass line, melody) and its onsets; the arrangement is compared with the orchestral score's piano part and with
   its piano plus orchestra, whichever is closer (the arrangement folds the orchestra in). A global alignment
   (Needleman-Wunsch with affine gaps) maps the measures one to one and in order, with cuts on either side; matching
   distinctive texts (tempo words, sections, rehearsal marks that occur at most twice in each score, words compared
   loosely) pull it, a different time signature or measure length costs. Then a measure at the edge of a cut that
   sounds much more like a measure inside the cut moves there; a dropped measure that continues its neighbour's
   orchestral measure joins it (two arrangement measures over one: a passage written out at half speed); a measure
   at the edge of a cut that sounds little like its counterpart (the arranger's bridge over the cut) joins the cut.
   A passage the arrangement plays in another key is reported and filled.
2. **Retiming.** An arrangement note at (its measure, offset in it) is played at the mapped orchestral measure's
   start plus that offset, on the orchestral score's clock (its tempo map, pauses and fermatas, as the formatter
   times its own notes); measures of other lengths are warped through the onsets both sides strike at the same
   pitch. A note held into a cut ends there; a note held from a dropped measure into a mapped one is struck again
   where that measure starts. Orchestral measures without counterpart are filled from the orchestral score's own
   piano part (drum staves left out), so the default view has no holes where the piano plays; their notes end where
   the arrangement takes over. The companion audio is never retimed: it is rendered from the orchestral score.
3. **Output.** The parts file's `simplified` part (arrangement notes carry `"simplified": 1`: Note Waterfall counts
   their hand-edit identity apart from the original part's; filled notes are the original part's own), `"original"`
   and `"orchestra"` as usual, and `"arrangement": {"score", "revision", "measures": {"mapped", "filled",
   "dropped"}}`. Hand edits of the arrangement's notes are not written back (`hands review` gives the reason).
4. **Report:** `<output>/.arrangements/<song>.md` and `.json` (and `python3 -m pianovision arrangement`): the mapped
   segments with their confidence (mean similarity, and the margin over the same segment shifted by 1-4 measures),
   the cuts on both sides with what fills them, dropped measures, moved/joined/trimmed measures, time-signature
   mismatches and the text anchors.

`measure_map` takes overrides where `auto` is wrong, measures written as the scores number them (`"12"`, `"12-30"`,
`"395:2"` for the second pass through measure 395): `{"pin": [["arrangement", "orchestral"], ...], "fill":
["orchestral", ...], "drop": ["arrangement", ...]}`; the rest is aligned around them.

For Rhapsody in Blue (419 measures as played against 522): five segments of high confidence (similarity 0.89-1.00)
cover 418 arrangement measures; the orchestral measures 138-180, 200-254, 256-259 and 472-474 (105) are the
orchestral piano part's; the arrangement's measure 371 (a B7 bridge over the cut) is dropped; its 11/4 measure 157 is
orchestral 255, its measures 200-201 are orchestral 302, and five more measures of other lengths are warped.

## Note Waterfall's companion audio (accompaniment)

`python3 -m pianovision accompaniment` renders, for every score whose parts file has orchestra
notes, the score itself minus the piano part with MuseScore Studio 4.7 and the Muse Sounds
libraries, so the audio follows the song's own timeline (tempo map, fermatas, repeats). Run
`build` first: the scores are the manifest's. Settings are `[accompaniment]` in
`pianovision.toml`; per-score options are in `accompaniment.json` (checked in, keyed by the
score's path below the scores folder).

```bash
python3 -m pianovision accompaniment                  # render what changed, write the report
python3 -m pianovision accompaniment --list           # Part ids, the formatter's view of each staff, what is muted/relabelled
python3 -m pianovision accompaniment --only 'Rachmaninoff/*' --force
```

For each score:

1. **What to mute** comes from the formatter, not from instrument names: the staves
   `pvjson.classify_tracks` takes for the piano part ('piano' and 'piano-simplified' staves,
   a staff whose MIDI program is 0-7 (a drum staff on channel 10 has program 0, so it counts
   too), the first two staves when none is called piano). The 'piano-orchestral' staves are
   muted too when the score also has the orchestra (`piano_orchestral`), and vocal staves
   (`mute_vocals`). MuseScore mutes whole instruments: an instrument with a piano-part staff is
   muted with all its staves (listed as a conflict when another of its staves has notes).
2. **The render copy** (in `work_dir`; the real score is only read) gets, in its
   `audiosettings.json`, solo off everywhere, mute on the muted instruments and their volume at
   -120 dB (MuseScore 4.7.5's command-line export plays muted Muse Sounds tracks), and no saved
   reverb sends (`reset_reverb`: Muse Sounds' own levels). `relabel` gives an instrument another
   instrument's sound (its id and instrumentId, the audiosettings track, the part's instrument
   changes to it) when the score's label has no Muse Sounds instrument or is wrong (a "Lupophone"
   staff that is the bassoons). Notes are never changed.
3. **Render**: `mscore -o render.wav --sound-profile MuseSounds copy.mscz`, headless, with
   MuseScore's config, cache and data folders in `work_dir` (the Muse Sounds engine is linked
   in). A run that does not load the engine fails instead of rendering the basic sounds, and so
   does a render with NaN samples (Muse Sounds 0.105.8's Bass Trombone writes them into the whole
   mix about a second after short ff notes: relabel the instrument).
4. **Probe**: a probe score with the copy's instruments, one note each in turn (the median,
   lowest and highest pitch of the part; a percussion staff's drums), rendered with and without
   the Muse Sounds engine. An instrument that sounds the same both ways has no Muse Sounds
   instrument and plays MuseScore's basic sounds; a note that is silent with the engine is
   outside the library instrument's range. Relabelled instruments are probed before and after.
5. **Checks**: duration against the score (MuseScore's export can run on in silence for
   minutes: it is cut after the last sound, never before the end of the score), silences where
   the score has notes, sound where only the muted staves have notes (bleed), peak level; the
   timing against the formatter's note-on times of the staves that sound
   (`tools/accompaniment_timing.py`, run with `timing_python`, a Python with numpy, soundfile
   and librosa: windowed cross-correlation of the onset envelope, median / p5 / p95 lag and
   drift, + = the audio is later; and where the mp3's audio lands with and without its LAME
   gapless header). Muse Sounds' samples take 10-60 ms to speak (strings and brass most), which the
   onset envelope sees as a lag; the probe measures each instrument's attack, and a second render
   of the score's piano alone with the basic sounds (quick attacks) checks MuseScore's timeline
   itself against the notes.
6. **Encode**: a linear gain to `lufs` (-16 LUFS integrated, ffmpeg's loudnorm measurement),
   capped so the true peak stays at or below `true_peak` (-1 dBTP), no compression; LAME mp3,
   `bitrate` kbit/s CBR, `<out_dir>/<song>.mp3` named after the PianoVision JSON. The first
   1105 samples (LAME's 576-sample encoder delay plus the decoder's 529) are cut and no Xing/LAME
   header is written: every decoder, gapless-aware or not, and a player fed the frames from any
   frame on (a Sonos), plays the audio on the score's timeline.

The report is `<out_dir>/accompaniment-report.md`, and `accompaniment-report.json` (which is
also the cache). A score is rendered again only when its file, its options, the muted
instruments, MuseScore, the Muse Sounds engine or the encoding settings changed, when its mp3
is missing or was changed, or when the Muse Sounds libraries changed in a way that can change
its sound: a library it had was removed or updated, or a library was installed while some of
its instruments played the basic sounds. So installing Percussion or Harp later renders again
only the scores with timpani, harp, ... (the libraries are read from the Muse Sounds folder's
`.instruments` database, read-only).

The timing check also decodes each mp3 three ways (ffmpeg's default, without any gapless
handling, and from a frame in the middle of the file) and reports where it lands against the WAV:
0 samples. (An mp3 with the usual LAME header plays 1105 samples, 25 ms, late in any decoder that
ignores the header, and always from a frame index.)

`techniques` start a part's staves with a playing technique in the render copy (a `PlayTechAnnotation`
before their first note or rest; notes are not changed): `{"part": "15", "technique": "pizzicato", "text":
"pizz."}` makes a MIDI import's "Harp, Pizz Basses", relabelled to the contrabasses, play pizzicato. A `relabel`
whose `from` is an instrument the part only changes to (a clarinet in A for some passages) relabels those changes.
The accompaniment classifies the staves with the score's build options (`scores.json` `piano_staves`).

`play_parts` keeps an instrument the formatter counts as piano playing: the drum staves on MIDI
channel 10 have program 0, which the formatter's rule takes for a piano (Shape of You, Evanescence,
Rach 3/II and III have them in `accompaniment.json`).

## Companion audio from your recordings (recordings)

`python3 -m pianovision recordings` links recordings in the music folder (`~/Music`, only read) to the library's
songs and puts each one on its song's timeline (tempo map, fermatas, repeats as played), so Note Waterfall can play
it as companion audio in sync with the falling notes. Settings are `[recordings]` in `pianovision.toml`; the links
and your decisions are in `recordings.json` (checked in, keyed by the score's path like `scores.json`).

```bash
python3 -m pianovision recordings match                        # candidates + one TypeSafe judgment per song
python3 -m pianovision recordings status                       # what is linked, what needs your decision, results
python3 -m pianovision recordings retime                       # align + retime every usable link -> <song>.mp3
python3 -m pianovision recordings confirm SONG                 # use the pick of a song in review
python3 -m pianovision recordings confirm SONG "Folder/Other recording.mp3"   # use another file
python3 -m pianovision recordings confirm SONG --variant full  # the full mix rather than the piano-removed stem
python3 -m pianovision recordings reject SONG                  # no recording for this song
```

1. **Inventory** (`work_dir/inventory.json`, cached by size and mtime): every audio file with its duration and its
   title, artist, album and composer tags (ffprobe is asked for those tags only: lyrics are never read). Files are
   grouped into recordings: a full mix with the LALAL.AI stems split from it (`…_no_piano_split_by_lalalai` in the same
   folder, by name or by the stem's title tag, same duration), what was derived from it (`_retimed`, `-retime`,
   `…_mixdown`; by folder or name) and copies (same name and size). Video files are listed, not offered.
2. **Match.** Code proposes up to 12 candidates per song: IDF-weighted word overlap between the song (title, artist,
   score path) and the recording (file and folder names, tags); a word that begins another counts (rach /
   rachmaninoff). A candidate must share a word that names something: numbers and generic words (major, concerto,
   piano, soundtrack, film, …) alone propose nothing. Songs without candidates are not asked about. Then **one
   TypeSafe request per song** (`model`, pinned to `jev-1.13.0`; the HTTP API, `TYPESAFE_API_KEY`), "select instead
   of generate": state = the song and the candidates' names and tags;
   * a **Choice** "which audio file is a recording of the composition described in `song`" over the candidates and
     `none`, asked twice in the same request with the candidates in reverse order (Jev 1.13 leans to the first
     option) and averaged;
   * a **Noul** per candidate: "is `recordings[i]` a recording of the same composition" (an arrangement, a cover, a
     remaster or a stem counts; another movement, a title that only shares words, or a file whose name does not say
     which piece it is does not).

   `decide` (code, thresholds in `[recordings]`): **linked** when the pick's Noul is at least `same_min` (0.8) and
   either both orders picked it with Choice confidence at least `link_confidence` (0.6), or the probability is spread
   over candidates that are all the same composition (two recordings of one concerto movement) with `p_none` at most
   0.1; among such equally valid recordings one with a piano-removed stem is preferred. **none** when `none` is picked
   with `p_none` at least `none_min` and no candidate passes its Noul. **review** otherwise: not used until you decide.
   A question is asked again only when it changed (new candidates, another model) or with `--refresh`; the others are
   decided again from the raw answers stored in the links file, so a change of thresholds needs no request.
3. **Align** (`tools/recording_align.py`, run with `[recordings] python`: numpy, scipy, soundfile, librosa). The
   recording's full mix (it has the piano the song's notes have) against a synthesis of the song's notes (the parts
   file's original piano part and orchestra, vocal lines included): CQT chroma, the recording's tuning estimated; the
   transposition from all 12 rotations (a quick DTW each, the two best again at full resolution); subsequence DTW with
   a penalty on non-diagonal steps (free start and end in the recording); the matched **pieces** where the path's
   similarity holds up and no side stands still; anchors at the song's beats (the path smoothed over ±0.5 s, moved by
   onset cross-correlation where that is clear: piano and orchestra recordings); stretches where the recording would
   race through the score or crawl are taken out. Between and around the pieces are the **gaps**: `recording_extra`
   (cut), `song_missing` (silence), `weak` (both sides have material that matches poorly but fits the tempo: stretched
   through) and `different` (cut and silence). An abridged score, a repeat taken in one and not the other, an intro or
   outro show up here rather than being forced.
4. **Retime.** The variant to deliver (`variant`: the piano-removed stem when the recording has one, else the full
   mix; the stem's offset against the full mix is measured) is cut and padded per the plan and time-stretched in one
   pass by ffmpeg's `rubberband` filter (librubberband; the tempo switched per anchor). The filter misplaces content by
   tens of ms after large tempo changes, so the output is compared with the recording resampled through the plan and
   stretched again with corrected targets (up to 3 passes, until 95 % of 3 s windows are within 8 ms:
   `fidelity_p95_ms`). A recording off A440 by `retune_cents` (15) or more is retuned. Encoded like the accompaniment
   (-16 LUFS, -1 dBTP, LAME 256 kbit/s, codec delay cut, no Xing header: lands at sample 0 in every decoder).
5. **Verify** the result against the song's notes on the song's clock: per measure the chroma agreement (0 = chance,
   1 = identical) and the onset lag; over the song, the lag of the chord changes (8 s windows) and of the onsets. For
   a stem, the full mix is retimed the same way and verified too (the stem lacks the piano the notes have).

Outputs in `out_dir` (staged under `~/.cache/nw-recordings/out`; move the mp3s somewhere durable before deploying):
`<song>.mp3`, `audio_map.proposed.json` (entries in the format of Note Waterfall's `Tools/audio_map.json`, offset 0;
`enabled` only when at least `min_coverage` (85 %) of the song is matched, the median lag is within `max_lag_ms`
(30 ms) with `min_within_50ms` (70 %) of the windows within 50 ms, the stretch met its plan, and the key is the same;
`replaces` names what Note Waterfall has now), `recordings-report.md` / `.json` and `accuracy/<song>.json`, what the
recording says about the score, for correcting it later (nothing is changed here): the key offset (+n = the recording
is n semitones higher) and tuning, the tempo ratio (recording seconds per score second; per measure p10 / median /
p90, trend, drift from a constant tempo, measures the recording plays with other lengths), the structural
differences with their measures, and the measures with low agreement after the retime.

`recordings.json`, per song (score path): `song`, `status`, `recording` (the pick; for review the suggestion),
`variants` and `variant`, `confidence` (Choice confidence of the averaged orders), `p_same` (the pick's Noul),
`p_none`, `consistent` (both orders picked the same), `why`, `alternatives` and `judged` (model, time, the question's
hash, the candidates with their scores). Yours: `decision`: `{}` confirms the pick, `{"recording": "<path>"}` links
another file, `{"recording": null}` rejects, `"variant": "full"|"no_piano"` chooses the file; `match` never changes it.

## Fidelity

Checked against the 140 existing outputs, which the old scripts made from
MuseScore-exported MIDI files:

* 123 are reproduced byte for byte with MuseScore 4.6 defaults.
* 13 more are reproduced with per-file compatibility settings. These model
  MuseScore 4.5's dynamics and tempo values that MuseScore recomputed in an open
  session. `verify --calibrate` finds the settings and stores them in the manifest.
* 4 depend on state that only existed in an open MuseScore session when the MIDI
  was exported. Those outputs are kept pinned as they are.

The library's 141 scores saved again by MuseScore 4.7.5 render exactly like 4.7.5's own MIDI export
of them, except 6 that differed the same way from 4.6.3's export already (a tempo event whose place
depends on layout, which the `tempo_model` knob covers, in 4; the MIDI channels of Rach-2/I's
orchestra, which the JSON does not show) or newly in Vocalise, where 4.7's layout adds courtesy
time signatures at the voltas (its `tempo_model: layout` knob matches 4.7.5).

## MuseScore 4.7

MuseScore 4.7 saves scores as format 4.70. The renderer tells the formats apart and applies what
4.7 reads or exports differently to 4.70 files only, so scores saved by 4.6 and older render
exactly as before:

* **Line breaks in texts** are saved as `<br/>`: a title or composer on two lines reads as before
  ("Doctor My Eyes Jackson Harrower …", not "Doctor My EyesJackson Harrower …"). Staff texts
  (Note Waterfall's markers) and instrument names read the same way.
* **Tempos are rounded to 6 decimals** (beats per second) whenever they go into the tempo map:
  tempo markings, fermatas, gradual tempo changes, voltas. A 4.70 file of a score with a fermata at
  70 bpm plays the fermata at 1714284 µs per quarter where 4.6 gave 1714285, so note times move by
  microseconds (at most 0.15 ms in this library).
* **A spanner written backwards** (its end before its start, e.g. a hairpin) is turned round when
  4.7 reads it, so it sounds; 4.6 kept it with a negative length, which did not. 4.7 saves it
  forwards, so a score saved by 4.7 plays the hairpin.
* A tie into a grace note adds nothing to the tied note's length.

**Saving the library again with MuseScore 4.7** (every score's content changes): `build` keeps
every output whose music did not change, byte for byte. It tries, in order, the score's own knobs,
then the knobs a re-save calls for, and records the ones that reproduce the output in the manifest:

| knob | what it models |
|---|---|
| `engine: "4.6"` | MuseScore 4.6's export rules for a 4.70 file (the output was made before the score was saved by 4.7) |
| `va_dyn_type: true` | with `dynamics: "4.5"`: a pre-4.4 score's `dynType` 0 (staff range) was saved by 4.4+ as the voice assignment "allInStaff", which MuseScore 4.5 ignored; read it back as the staff range |
| `metadata: "file"` | the title from the file name and the composer from the folder: what the old title rules made of a MuseScore 3 score, whose capitalised title styles MuseScore 4 saves in lower case |

A pinned output stays pinned while the score has the same notes. `verify` sees a re-saved score as
`build` will (it lists the knobs). What changes is what MuseScore 4.7 plays differently, as for any
edited score: the song is rendered with 4.7's rules (`build --keep SCORE` keeps the old output pinned
instead; `build --force SCORE` drops the knobs and renders a score with the current rules).

Re-saving the whole library with MuseScore 4.7.5 (141 of 144 scores; three files MuseScore reports as
corrupted): 138 outputs stay byte for byte (80 render the same anyway, 37 with `engine: "4.6"`, 13
with their existing knobs, 3 MuseScore 3 scores with `metadata: "file"`, 1 with both, 1 with
`engine` and `va_dyn_type`, and the 3 pinned ones); 3 are rendered again because 4.7 plays hairpins
that were written backwards: Stella by Starlight (m. 40-41: a crescendo, 80 to 96, where all notes
were 96), Rachmaninoff's Concerto No. 2/II (m. 150-152, and the rest of the piece now with
MuseScore 4.6+'s dynamics instead of the 4.5 model its output came from) and Night on Bald Mountain
(m. 69-70 and 73-74: diminuendos; also a tempo saved with 6 decimals, < 1 µs). Note Waterfall's
parts files are all written again (they carry the score's content hash); in the Tchaikovsky concerto
transcription, 7 staff texts that MuseScore 3 styled as expressions are expression texts now (marker
kind `expression`), and "non apregg." is no longer a marker.

## Layout

```
pianovision/        the package (stdlib only)
  mscx.py             .mscz/.mscx reader -> score model (score.py)
  render.py           MuseScore 4.6/4.7 MIDI export: tempo/time-sig maps, channels, velocities, events
  playevents.py       note play events (gate time, ornaments, tremolo, arpeggio, glissando, swing)
  repeats.py tempo.py velocity.py harmony.py rhythm.py navigate.py cxxsort.py
  pvjson.py           PianoVision JSON builder (port of midi_to_json.py, output byte-identical)
  parts.py            Note Waterfall's parts files (original / simplified piano part, orchestra)
  markers.py          the parts files' markers (rehearsal marks, sections, tempo words) from the score's texts
  lyrics.py           the parts files' sung syllables, on the notes that sing them
  buildopts.py        per-score build options (scores.json): title, piano staves and hands, simplified_from
  arrangement.py      a simplified part from a piano arrangement: measure alignment, retiming, report
  handsplit.py        the hands of a piano part written on one staff (hands: "split")
  convert.py          one score -> JSON
  library.py          manifest, incremental build, adoption of existing outputs
  device.py           adb deploy
  calibrate.py        compatibility-knob search used by verify --calibrate
  handedits.py        hand edits from Note Waterfall -> the scores (hands pull/review/apply)
  accompaniment.py    Note Waterfall's companion audio: render copies, MuseScore + Muse Sounds, probe, mp3
  recordings.py       companion audio from recordings you own: inventory, candidates, TypeSafe judgment, links file,
                      references, align/retime/verify runs, accuracy summaries, audio_map proposal, report
  recmap.py           time maps between a recording and a song: DTW path -> pieces and gaps, the edit plan
  cli.py              python -m pianovision
  gui.py              the Piano Library window (GTK 4 / libadwaita)
  guimodel.py         what the window shows and does, without GTK (statuses, push plans, device status)
desktop/            piano-library.sh (launcher), install-launcher.sh, the icon
tools/              accompaniment_timing.py (the accompaniment's timing check; numpy, soundfile, librosa)
                    recording_align.py (recordings: align, retime, verify; numpy, scipy, soundfile, librosa, ffmpeg)
pianovision.toml    settings
scores.json         per-score options of `build` (titles, piano staves, simplified parts from arrangements)
accompaniment.json  per-score options of `accompaniment` (relabelled instruments, techniques, ...)
recordings.json     recordings linked to the songs (`recordings match`) and your decisions
PianoVision/        generated library (git-ignored): *.json, .manifest.json, .attic/, NoteWaterfall/*.parts.json,
                    .arrangements/ (alignment reports)
```

## Legacy scripts

`convert_all.sh`, `musescore_to_json.py`, `midi_to_json.py`,
`musicxml_to_json.py`, `update_midis.py` (headless MuseScore MIDI export) and
their helpers still work and need the packages in `requirements.txt`. They write
into the same `PianoVision/` folder; if they change a file, `status` reports it
as edited outside the pipeline. MusicXML input is only supported there.
`sync_pianovision_files.sh` now removes device files only when given `--delete`
as its second argument.
