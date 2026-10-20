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
python3 -m pianovision hands sync    # keep going: hand edits recorded on the Quest -> the scores (a login service)
```

Settings live in `pianovision.toml` (scores folder, output folder, device folder,
orchestra/simplified options, and `[hands]` for Note Waterfall's hand edits); options for single scores
(a title, the piano staves, a piano solo and an orchestral score made one song) in `scores.json` (see
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
| `hands sync [--once] [--dry-run] [--interval S]` | the same by itself while the headset is connected: clean reviews are applied, the others wait for you, with a desktop notification (see [Hand edits sync](#hand-edits-sync-automatic)) |
| `gui [--read-only] [--no-device]` | the Piano Library window (see below) |
| `accompaniment [--only GLOB…] [--out DIR] [--force] [--dry-run] [--list] [--no-timing]` | Note Waterfall's companion audio: each score's orchestra (everything but the piano part) rendered by MuseScore with Muse Sounds, one mp3 per song (see below) |
| `arrangement [SCORE…] [--write]` | the measure alignment of a song made with a partner score (`scores.json` `simplified_from`, `orchestra_from`), as a report; `build` writes it to `.arrangements/` (see below) |
| `recordings match\|align\|retime\|tempo\|status\|route\|confirm\|reject [--only GLOB…]` | what each song's companion audio is (generated from the score, or for sung songs a recording you own: TypeSafe judgments); for sung songs by default the recording leads: `tempo` rewrites the score's tempo map so the original recording plays unstretched (see below); `retime` stretches a recording onto the score instead |
| `lyrics import SCORE LYRICS.txt [--dry-run] [-o COPY] [--in-place --backup DIR] [--staff N] [--replace]` | a song's lyrics, from a text file you supply, onto the vocal line of its score: each line on its phrase, each syllable on its note, verse k on the k-th pass through repeated bars; a copy, or the score after a backup, verified first (see below) |
| `scorefix report\|apply\|reference SCORE [FIXES.json] [--out COPY] [--in-place --backup DIR]` | a score's repeat structure as MuseScore plays it (voltas no pass reaches, bars never played, wrong bar lengths); structural fixes (tempo, voltas, repeat counts, segno / coda / D.S.) into a copy, or into the score after a backup; the score's notes on its own clock for an alignment with a recording (see below) |

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
out and voices are vocal staves. The staves a song takes from its partner (`orchestra_from`) are its orchestra and
vocal staves as the partner's own classification says, whatever their names and programs.
Of the library's 146 scores, 9 PianoVision songs changed with these rules (Love
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

## Hand edits sync (automatic)

`hands sync` does `hands pull`, `review` and `apply --yes` by itself while the Quest is connected, for
the hand edits that need no decision; the others wait in the inbox for you:

```bash
python3 -m pianovision hands sync              # keep going (the service below runs this at login)
python3 -m pianovision hands sync --once       # one look at the headset, then exit
python3 -m pianovision hands sync --dry-run    # one look: list and review, write nothing
desktop/install-handsync.sh                    # install the login service (--uninstall removes it)
```

* **The headset:** `adb devices -l` every 10 s (`--poll`). Each check is its own adb call, so an adb
  server killed or restarted by something else (Unity's Android builds do that) is simply started again,
  and an offline or unauthorized headset waits until it is usable. Asleep is fine. On connect and then
  every 45 s (`--interval`, `[hands] sync_interval`) the md5 of each `HandEdits/*.hands.json` is read
  (one `adb shell`; `applied/` is not looked at).
* **New or changed sidecars** (a name and md5 not seen before), and sidecars whose score, song or
  `scores.json` changed since their review, are pulled into `PianoVision/.hands/inbox` and reviewed.
* **A clean review is applied**: verified, and every edit moves or is in the score already (no
  conflict, nothing skipped, stale or unreadable, no error for that score). It goes through exactly the
  steps of `hands apply --yes`: backup to the attic, overwrite, build, deploy to PianoVision and Note
  Waterfall, sidecar to `applied/` on the headset once Note Waterfall has the rebuilt song. Notification:
  *Chopin Nocturne in E Flat Major - Op. 9 No. 2: 77 notes moved to the other hand (40 to the right, 37 to
  the left), deployed*.
* **Everything else stays** in the inbox and on the headset, untouched, with one notification per version
  of the sidecar (*… needs review: 3 edits conflict*); use `hands review` / `apply` or the Piano Library's
  Hand edits page. It is reviewed again when the sidecar, its score, its song or `scores.json` changes.
  A sidecar whose edits are all in the score already is only archived.
* **Never twice:** `state.json` keeps a ledger of the sidecars (name and md5) written into the scores; a
  sidecar in it is not reviewed for applying again, only deployed and archived if it is still on the
  headset (every 10 min at most, and on reconnect). Besides, a review reads an edit that is already in the
  score as "already", and `apply` refuses a score that changed since its review.
* **Not while MuseScore has the score open:** a MuseScore process with the score on its command line (the
  Piano Library's *Open in MuseScore*, a double-click), an autosave next to it (`<score>.autosave`: unsaved
  changes), or a MuseScore window that opened files since it started with the score among its last 10
  recent files (File › Open; MuseScore 4.7 keeps no lock file). The review waits, with a notification,
  and is applied once MuseScore lets go of it.
* **Light:** one look at a time (`sync.lock`; the service also takes `daemon.lock`); a review or a build
  (one score, no process pool) waits until 4 GB of memory are available (`--min-free-mb`,
  `[hands] min_free_mb`).

Everything goes to `~/.cache/nw-handsync/sync.log` (`[hands] sync_dir`, `--state-dir`), with `state.json`
and `status.json` (the headset, each sidecar's status and the last notifications, for the Piano Library
window). `--no-notify` turns the notifications off; `--no-deploy` and `--no-archive` work as for `apply`.

**The service:** `desktop/nw-handsync.service`, a systemd user unit (runs at login, restarts on failure,
`Nice=10`, working directory this repository, `PATH` with adb). `desktop/install-handsync.sh` copies it to
`~/.config/systemd/user/` (pointing it at the repository it is run from), enables and starts it; `systemctl
--user status nw-handsync`, `journalctl --user -u nw-handsync -f`. A `hands sync --once` while it runs skips
its look if the service is in the middle of one (`sync.lock`); `--dry-run` takes no lock and writes nothing.

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
notes carry `"staff"` (the score staff; with `orchestra_from` the partner's staves count on after the song's
own), `"program"` (instruments and vocal staves), `"vocal": 1`
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
| `simplified_from` | on an orchestral score: a piano solo of the same music; its piano part becomes the simplified part, measure-aligned, and the song plays on the solo's clock (below) |
| `orchestra_from` | on a piano solo: an orchestral score of the same music; its orchestra, measure-aligned, becomes the song's orchestra, on the solo's clock (below) |
| `measure_map` | the alignment of the two: `"auto"` or overrides (below) |
| `piano_from` | `"simplified_from"` (with `simplified_from`): the solo is the song's piano part too, Note Waterfall's `original` part and PianoVision's hands, wherever the alignment maps a solo measure; the measures the solo leaves out keep the score's own piano part, and there is no separate simplified part (below) |
| `song` | `false`: the score is not a song of its own. `build` renders nothing for it and retires the output it made before to the attic (`status` lists it as "not a song of its own", the window as "partner only"); it can still be another score's partner |

The manifest records, per score, a hash of the options that make its own render and, with a partner, the partner's
content hash, knobs and options, the measure map and the revisions of the alignment and the clock: a change renders
the score again (the partner sets the song's clock or orchestra, so its PianoVision JSON depends on it).

### Partner scores: a piano solo and an orchestral score as one song

A piano solo and an orchestral score of the same piece make one song (`pianovision/timeline.py`,
`pianovision/arrangement.py`). The option goes on the score that is the song:

```json
"Gershwin/Rhapsody_in_blue__George_Gershwin.mscz": {
  "title": "Rhapsody in Blue (Piano and Orchestra)", "composer": "Gershwin",
  "simplified_from": "Gershwin/Rhapsody_in_Blue_Piano_solo.mscz", "measure_map": "auto"},
"Rachmaninoff/Rhapsody_on_a_theme_of_Paganini_Variation_18_Piano_Solo.mscz": {
  "orchestra_from": "Rachmaninoff/pagvar18.mscz", "measure_map": "auto"},
"Rachmaninoff/pagvar18.mscz": {"song": false, "piano_staves": [11, 12], "hands": "split"}
```

* `simplified_from` on the orchestral score: the song has the orchestral score's measures, piano part (the
  `original` part) and orchestra; the solo's piano part is the `simplified` part, which Note Waterfall shows by
  default. With `"piano_from": "simplified_from"` as well, the solo's piano part is the song's piano part (step 3).
  Rhapsody in Blue with its orchestra.
* `orchestra_from` on the solo: the song has the solo's measures, piano part and tempo; the orchestral score's
  orchestra (its piano staves left out, as its own `piano_staves` say) is the song's orchestra: Note Waterfall's
  `orchestra` part, PianoVision's orchestra mode (the orchestra fills the measures where a hand of the piano rests,
  as for any score) and the companion audio. Paganini's Var. XVIII.

The partner is rendered with its own options (piano staves, hands) and, when it is a song of its own, its manifest
knobs (a `song: false` score with the default knobs).

1. **Alignment.** The same alignment serves both directions: the solo is aligned with the orchestral score. Both are
   unrolled through their repeats and jumps. Every measure as played gets a duration-weighted pitch-class profile,
   the pitch class of the lowest and the highest sounding note over time (bass line, melody) and its onsets; the
   solo is compared with the orchestral score's piano part and with its piano plus orchestra, whichever is closer
   (a solo arrangement folds the orchestra in). A global alignment (Needleman-Wunsch with affine gaps) maps the
   measures one to one and in order, with cuts on either side; matching distinctive texts (tempo words, sections,
   rehearsal marks that occur at most twice in each score, words compared loosely) pull it, a different time
   signature or measure length costs. Then a measure at the edge of a cut that sounds much more like a measure inside
   the cut moves there; a solo measure that continues its neighbour's orchestral measure joins it (two solo measures
   over one: a passage written out at half speed); a measure at the edge of a cut that sounds little like its
   counterpart (the arranger's bridge over the cut) joins the cut. A passage the solo plays in another key is
   reported and filled. `measure_map` takes overrides where `auto` is wrong, measures written as the scores number
   them (`"12"`, `"12-30"`, `"395:2"` for the second pass through measure 395), always `[solo, orchestral]`
   whichever score carries the option: `{"pin": [["solo", "orchestral"], ...], "fill": ["orchestral", ...], "drop":
   ["solo", ...]}`; the rest is aligned around them.
2. **The clock: the solo's.** The song plays on the piano solo's clock, its tempo map, fermatas and pauses carried
   across the alignment onto the orchestral score's measures. The orchestral score is rendered on that clock, by the
   formatter (the song's notes, PianoVision's tempos and measures, the parts file, markers and lyrics) and by MuseScore
   for the companion audio, from the same edit of the score: its own tempo marks, fermatas and gradual tempo changes
   get `<play>0</play>`, its breaths and section breaks a pause of 0, and invisible tempo marks ("solo tempo q = …")
   are written in. Only the formatter's model and the copy MuseScore renders are edited, never the score, and the
   two render the same tempo map (a test renders the edited copy and compares). The rule, per orchestral measure as
   played:
   * a measure the solo plays: the solo's times for its positions, through the alignment's unit (a measure of
     another length is warped through the onsets both strike at the same pitch; where that warp crowds or stretches
     part of the measure by more than 3 times its mean, the measure gets the solo's length and the orchestral
     score's own timing inside);
   * a measure the solo leaves out (a fill): the orchestral score's own timing (its tempo marks, fermatas and pauses)
     scaled by the solo's stretch next to the cut: the median of the solo-over-orchestral length of the up to 4
     measures the solo plays before the cut and after it, interpolated geometrically across the cut (the side there
     is at the start or the end of the piece).

   A tempo mark goes where a measure starts and wherever inside it the solo's clock bends by more than 5 ms (a
   fermata, a ritardando), at a position where some staff has a chord or a rest; each measure lands exactly on its
   length, positions inside it within 5 ms. A measure played more than once has one set of marks (the passes
   averaged). Marks are rounded to 6 decimals of beats per second (MuseScore 4.7's tempo precision), kept within
   5-999 bpm, and a measure-start mark equal to the tempo in effect is left out. With `orchestra_from` the song is
   the solo as it is: the orchestral score, rendered on the solo's clock, gives its orchestra, moved onto the solo's
   measures through the alignment read backwards (a note in an orchestral measure the solo leaves out is not played,
   one held into a cut ends there, one held from a left-out measure into a mapped one is struck again); its staves
   come after the solo's own in the song (the parts file's `staff`), orchestra and vocal staves as that score's own
   options classify them.
3. **The simplified part** (`simplified_from`). A solo note at (its measure, offset in it) is played at the mapped
   orchestral measure's start plus that offset; measures of other lengths are warped through the onsets both sides
   strike at the same pitch. A note held into a cut ends there; a note held from a dropped measure into a mapped one
   is struck again where that measure starts. Orchestral measures without counterpart are filled from the orchestral
   score's own piano part (drum staves left out), so the default view has no holes where the piano plays; their
   notes end where the solo takes over. Solo notes carry `"simplified": 1` (Note Waterfall counts their hand-edit
   identity apart from the original part's; filled notes are the original part's own). The parts file has
   `"arrangement": {"score", "revision", "measures": {"mapped", "filled", "dropped"}}` (with `orchestra_from`:
   `"orchestraFrom"`, the same keys). Hand edits of the solo's notes are not written back into the orchestral score
   (`hands review` gives the reason); with `orchestra_from` the solo's own notes take hand edits as usual and the
   orchestra's do not.

   **The piano part from the solo** (`"piano_from": "simplified_from"`): the solo is the source of truth for the
   piano part as well. The solo's notes, retimed exactly as for the simplified part, replace the orchestral score's
   piano part in every measure the alignment maps before PianoVision's JSON and the parts file are built from the
   render, so PianoVision's hands and the `original` part are the solo's notes there (their accents, velocities and
   lyrics the solo's; PianoVision's orchestra fills a hand only where the solo rests a whole measure), and the
   measures the solo leaves out keep the orchestral score's piano part, as the simplified part fills them (the
   orchestra plays there; the solo has nothing to give). The song then has no separate simplified part: it would be
   the same notes, and Note Waterfall's original/simplified switch would show one thing twice; the app shows the
   original part and greys the switch out. The solo's notes keep `"simplified": 1` in the original part: Note
   Waterfall reads a note so marked as one of the simplified notes wherever it is, so their hand-edit identity is
   the one they had in the simplified part (edits recorded there before still apply) and stays apart from the
   orchestral score's notes (an earlier edit of an orchestral note the solo replaced is stale). `hands review` writes
   hand edits of the filled measures into the orchestral score as usual and skips the solo's notes, naming the solo's
   own song to record them on (their score is the solo). The parts file's `"arrangement"` gets `"part":
   "original"`. The clock is the same either way: the song's tempo map, measures and timings, the companion audio
   and its cache key do not change.
4. **Report:** `<output>/.arrangements/<song>.md` and `.json` (and `python3 -m pianovision arrangement`): the mapped
   segments with their confidence (mean similarity, and the margin over the same segment shifted by 1-4 measures)
   and their times on the song's clock, the cuts on both sides, dropped measures, moved/joined/trimmed measures,
   time-signature mismatches, the text anchors, and the clock: tempo marks, the song's length against the orchestral
   score's own, the largest deviation inside a measure, the fills' stretch and (with `orchestra_from`) where the
   companion audio's pieces go.

**Rhapsody in Blue** (419 measures as played against 522; `piano_from` since 2026-10-20, after errors in the
orchestral score's piano part at the ending: its staves give the wrong hands in 383-424, 475-480 and 505, where both
hands sit on one staff or the staves are swapped; 475-480 sound an octave below the solo, which writes 8va over both
staves, and in 504-507 its 8va lines start and end elsewhere; m. 401 has A-flat for the B of every other statement;
elsewhere at the ending the two are different piano writing: the solo folds the orchestra in, 425-457 and 481-503):
five segments of high confidence (similarity 0.89-1.00)
cover 418 solo measures; the orchestral measures 138-180, 200-254, 256-259 and 472-474 (105) are the orchestral piano
part's; the solo's measure 371 (a B7 bridge over the cut) is dropped; its 11/4 measure 157 is orchestral 255, its
measures 200-201 are orchestral 302, and five more measures of other lengths are warped (the solo's measure 37, the
run alone, keeps the orchestral measure's own shape inside). On the solo's clock the song lasts 1041.7 s instead of
the orchestral score's own 1058.7 s (260 tempo marks; the solo has 166), and its ending follows the solo: the last
42 measures as played (orchestral 469-510) take 77.5 s instead of 67.8 s, measures 469-480 1.07 s each (112 bpm)
instead of 0.83 s (144 bpm), the Grandioso (487-503) 1.67 s (72 bpm) with the solo's broadenings to 2.05 s instead
of 1.5 s (80 bpm), and measure 508 the solo's fermata (5.06 s instead of 3.65 s). The fills are stretched 0.84-1.29
(the tutti 138-180 from 1.06 down to 0.84, 200-254 from 0.84 up to 1.0, 472-474 by 1.29 like their neighbours).
The clarinet's opening trill and a flute trill play fewer notes at the solo's tempo (MuseScore realises a trill
by the tempo): 56 orchestra notes fewer, the piano parts unchanged.

**Paganini, Var. XVIII**: the solo (43 measures, 60 bpm, a pickup) and `pagvar18.mscz` (a MIDI import of the
orchestral recording, 42 measures with its 492 tempo changes) align one to one (solo 2-43 = orchestral 1-42,
similarity 0.85); the song is the solo with pagvar18's 408 orchestra notes on its clock (2 tempo marks: 60 bpm and
the final fermata; 129.0 s instead of the recording's 164.2 s), its PianoVision JSON unchanged (the solo's hands
never rest a whole measure); `rach_Rhapsody_on_a_Theme_of_Paganini__Var_XVIII_Piano_and_Orchestra.json` was retired.

## Note Waterfall's companion audio (accompaniment)

`python3 -m pianovision accompaniment` renders, for every score whose parts file has orchestra
notes, the score itself minus the piano part with MuseScore Studio 4.7 and the Muse Sounds
libraries, so the audio follows the song's own timeline (tempo map, fermatas, repeats). Run
`build` first: the scores are the manifest's. Settings are `[accompaniment]` in
`pianovision.toml`; per-score options are in `accompaniment.json` (checked in, keyed by the
score's path below the scores folder: the score MuseScore renders, so with `orchestra_from` the
orchestral partner's).

**Songs with a partner score** ([Partner scores](#partner-scores-a-piano-solo-and-an-orchestral-score-as-one-song))
play on the piano solo's clock, and so does their audio: MuseScore renders the orchestral score with the same
edit the formatter renders the song from (its own tempo marks, fermatas, gradual tempo changes and pauses off, the
plan's invisible tempo marks in; `<work_dir>/<song>/timeline.mscz`, then the usual copy of it). Writing the tempo
map into the copy rather than time-stretching a render keeps Muse Sounds' own playing at the new tempo (no
stretching artefacts, a fermata or a ritardando of the solo played as one, not smeared), and the formatter's
render of the same edit is MuseScore's timeline note for note. With `simplified_from` the render is the song's
audio as it is (Rhapsody in Blue). With `orchestra_from` the render is of the partner and is cut into pieces, the
runs of orchestral measures that follow on in both scores, each placed at the solo's time (silence before the
first: the solo's pickup; cuts with 10 ms fades where the solo leaves orchestral measures out, silence where the
solo has measures of its own); the mp3 is named after the solo's song. The report's **Clock** line gives the plan,
the pieces, and a check of the song itself: every orchestra note of the song's parts file matched with the
rendered score's note of that pitch on the song's clock (median, p95 and largest difference); the timing check
then compares the audio with those notes. A song that left the library (its score retired or no longer a song of its
own) is listed in the report; its mp3 is left in the folder.

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

`python3 -m pianovision recordings` decides, for every song of the library, what Note Waterfall's companion audio is,
and for sung songs makes it from a recording in the music folder (`~/Music`, only read). By default the recording
leads: the score's tempo map is rewritten to follow it and the original file plays unstretched ([recordings
tempo](#the-recording-leads-a-sung-song-timed-to-its-recording-recordings-tempo)). With `"timing": "score"` in a
link's decision the recording (its **singing**, isolated, or the full mix you chose) is put on the song's timeline
instead (tempo map, fermatas, repeats as played) so it plays in sync with the falling notes.
You play the piano, so a recording's piano is never wanted:

* **generated** (classical and instrumental music: concertos, Rhapsody in Blue, the Rite of Spring, film score cues
  without singing): the companion is the score's orchestra rendered with Muse Sounds, piano muted
  (`pianovision accompaniment`, above), never a recording, not even a piano-removed stem. A linked recording is still
  aligned, for the accuracy summary only; nothing of it goes to Note Waterfall.
* **vocals** (sung pop and film songs): the singing of your recording, separated by Demucs and retimed.

Settings are `[recordings]` in `pianovision.toml`; the routes, the links and your decisions are in `recordings.json`
(checked in, keyed by the score's path like `scores.json`).

```bash
python3 -m pianovision recordings match                        # routes + recording candidates, TypeSafe judgments
python3 -m pianovision recordings status                       # routes, what needs your decision, results
python3 -m pianovision recordings retime                       # sung songs: vocals -> <song>.mp3; others: diagnostics
python3 -m pianovision recordings route SONG vocals            # your route for a song (vocals | generated | auto)
python3 -m pianovision recordings confirm SONG                 # use the pick of a song in review
python3 -m pianovision recordings confirm SONG "Folder/Other recording.mp3"   # use another file
python3 -m pianovision recordings confirm SONG --variant full  # the full mix (or no_piano) instead of the vocals
python3 -m pianovision recordings reject SONG                  # no recording for this song
```

**The route** (`decide_route`). Code facts first: the formatter's staff classification (`pvjson.staff_roles`: vocal
staves) and lyrics, as the manifest's parts statistics record them. Then one TypeSafe request per song (state: its
title, composer or artist and score path): a **Noul** "is it sung: does its original or best-known recording have a
singer?" (about the piece, not the score, which may be a piano arrangement) and a **Choice** of the kind of music
(classical, film score, popular song, other; asked twice with the options reversed and averaged). The rule, in order:

1. P(classical) at least `classical_min` (0.5): **generated**, sung or not (an aria's companion is its orchestra too);
2. a vocal staff or lyrics in the score (`min_score_vocal`, 16 notes or syllables; a few stray syllables are not a
   vocal line): **vocals**;
3. Noul at least `sung_min` (0.8): **vocals**; at most `unsung_max` (0.3): **generated**;
4. otherwise **review**: nothing is made until you decide (`recordings route SONG vocals|generated`).

After the separation, a vocals stem that sings in less than `min_vocal_activity` (10 %) of the matched recording
(0.5 s blocks louder than -45 dBFS and within 25 dB of the mix) is not used. The retimed vocals are checked against
the score's vocal line (`recording_align.py vocals`): the vocal notes' starts against the stem's onsets, the stem's
pitch (pYIN) against the sounding vocal note by pitch class, at the plan's timing and at the shift that agrees best
(corrected by the measurement's own bias, found on the vocal line synthesised in time), overall and per 10 s window,
and the stem's level where the vocal line rests (bleed). For a vocals stem the checks count the vocal line: at least
`min_coverage` of its notes in the matched recording, the pitch agreeing in `min_melody_agreement` (30 %) of the sung
frames, the vocal onsets' median within `max_lag_ms`.

**Vocal separation** (`pianovision/separation.py`, `tools/separate_vocals.py`): Demucs `htdemucs_ft`
(`separator_model`) in another Python (`separator_python`, else the first conda environment with `demucs`), two
stems, the vocals kept at the input's length and timing, cached by the input's content in
`stems_dir/<sha256>/<model>/vocals.wav`. Nothing is installed by this tool: `status` says whether a separator is
found and what is missing; the model runs with the Hugging Face hub offline (its weights must be in the cache). On
the GPU a run out of memory is tried again with 5 s and 3 s segments, then on the CPU (`meta.json` records what
worked, how long it took and the GPU's peak memory). A LALAL.AI vocals stem next to the full mix is used as it is.
Here: `~/.cache/nw-stems/venv` (demucs 4.1.0 over beat_this' torch 2.3.1, CUDA): Fountain of Sorrow's 6:52 in 99 s
on a GTX 1060 (6 GB), the model's own 7.8 s segments, 555 MB peak.

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
   0.1. **none** when `none` is picked, no candidate passes its Noul, and `p_none` is at least `none_min` or every
   candidate's Noul is under 0.5. **review** otherwise: not used until you decide.
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
4. **Retime** (sung songs). The variant to deliver (`variant`: `vocals` by default; `full` and `no_piano` only when
   you choose them; a LALAL.AI stem's offset against the full mix is measured, a Demucs stem has its timing) is cut
   and padded per the plan and time-stretched in one
   pass by ffmpeg's `rubberband` filter (librubberband; the tempo switched per anchor). The filter misplaces content by
   tens of ms after large tempo changes, so the output is compared with the recording resampled through the plan and
   stretched again with corrected targets (up to 3 passes, until 95 % of 3 s windows are within 8 ms:
   `fidelity_p95_ms`). A recording off A440 by `retune_cents` (15) or more is retuned. Encoded like the accompaniment
   (-16 LUFS, -1 dBTP, LAME 256 kbit/s, codec delay cut, no Xing header: lands at sample 0 in every decoder).
5. **Verify** the result against the song's notes on the song's clock: per measure the chroma agreement (0 = chance,
   1 = identical) and the onset lag; over the song, the lag of the chord changes (8 s windows) and of the onsets. For
   a stem, the full mix is retimed the same way (in the work folder, not kept) and verified too: the vocals lack what
   the notes have. Songs routed **generated** get only this check of their full mix, for the accuracy summary.

Outputs in `out_dir` (staged under `~/.cache/nw-recordings/out`; a vocals mp3 you keep goes to `~/Music/Note Waterfall
Vocals/`, a full mix or no-piano stem to `~/Music/Note Waterfall Recordings/` (both excluded from the scan, like
the accompaniments) before its entry goes into Note Waterfall's audio map):
`<song>.mp3` (sung songs), `audio_map.proposed.json` (sung songs only: entries in the format of Note Waterfall's
`Tools/audio_map.json`, offset 0;
`enabled` only when at least `min_coverage` (85 %) of the song is matched, the median lag is within `max_lag_ms`
(30 ms) with `min_within_50ms` (70 %) of the windows within 50 ms, the stretch met its plan, and the key is the same;
`replaces` names what Note Waterfall has now), `recordings-report.md` / `.json` and `accuracy/<song>.json`, what the
recording says about the score, for correcting it later (nothing is changed here): the key offset (+n = the recording
is n semitones higher) and tuning, the tempo ratio (recording seconds per score second; per measure p10 / median /
p90, trend, drift from a constant tempo, measures the recording plays with other lengths), the structural
differences with their measures, and the measures with low agreement after the retime.

`recordings.json` (format 2): `routes`, every song: `route`, `why`, `facts` (vocal-staff notes, lyric syllables,
orchestra notes), `p_sung`, `kind`, `p_kind`, `judged` (model, time, the question's hash, the raw answers) and yours,
`decision` (`vocals` or `generated`); `links`, per song with candidates: `status`, `recording` (the pick; for review
the suggestion), `variants` (`vocals`: a stem in the folder or "separated from the full mix"; `full`; `no_piano`) and
`variant` (`vocals` for a sung song, none for a generated one), `confidence` (Choice confidence of the averaged
orders), `p_same` (the pick's Noul), `p_none`, `consistent`, `why`, `alternatives`, `judged` (with the candidates and
raw answers) and yours, `decision`: `{}` confirms the pick, `{"recording": "<path>"}` links another file,
`{"recording": null}` rejects, `"variant": "vocals"|"full"|"no_piano"` chooses the file. `match` never changes a
decision, and decides again from the stored answers when a question has not changed.

## The recording leads: a sung song timed to its recording (recordings tempo)

For sung pop and film songs the recording you own is the source of truth for the timing. `python3 -m pianovision
recordings tempo` rewrites the score's tempo map so that every measure as played lasts what the recording's measure
lasts, and Note Waterfall plays the **original** file, never stretched, at an offset. This is the default for sung
songs (`[recordings] sung_timing = "recording"`; a link's `decision` `"timing": "score"` stretches that song's
recording onto the score with `retime` as before). `pianovision/rectempo.py`, `tools/recording_align.py bars`,
`tools/beat_activations.py`.

```bash
python3 -m pianovision recordings tempo '70s/fountain-of-sorrow.mscz'            # analyse, write a verified copy
python3 -m pianovision recordings tempo SONG --dry-run                          # the numbers and decisions only
python3 -m pianovision recordings tempo SONG -o COPY.mscz                       # the copy also there
python3 -m pianovision recordings tempo SONG --in-place --backup DIR [--yes]    # that copy over the score
python3 -m pianovision recordings tempo                                         # every sung song with a recording
```

1. **The measures in the recording.** The alignment of the song's notes with the recording (`align`, above) is the
   coarse map: straight across its gaps, the recording's tempo carried on before its first match and after its last.
   The recording's beats are detected twice: madmom's recurrent networks and DBN (`tools/beat_activations.py`, run
   with `[recordings] beats_python`: madmom 0.16.1 in the beat_this environment here, its 2016 models; nothing
   installed or downloaded, the aliases madmom still uses on Python 3.11 / numpy 2 are put back for that process),
   and librosa's beat tracker driven, frame by frame, by the tempo the coarse map implies. Each detection is put on
   the score's lattice: its beats, with the phase the detections share snapped to the beat, the half beat (or a third
   in compound time). A transcription an eighth off the recording's pulse has all its detections on its off-beats
   (Love Needs a Heart: the chords the score writes on the "and"s are the recording's beats). A Viterbi path weighs
   the coarse map's place (0.2 beat; 0.6 in and next to measures of another length) against a steady tempo between
   consecutive detections, so a stretch the coarse map has a little early or late is placed by the run of beats around
   it, and an extra detection is left out. The detected beats are the anchors; between anchors up to 4 beats apart the
   tempo is steady, elsewhere the coarse map's shape is moved by the anchors. Every position of every measure as
   played gets a time and every measure a confidence: `detected` (an anchor on its start; both detectors agreeing
   within 50 ms, a small residual against the coarse map and a strong activation score highest), `between`,
   `coarse`, `outside` (before the recording starts: the recording's tempo carried back). madmom's beats are the
   primary ones: librosa's land 30 ms later on all three songs below, and the onsets of the recording against the
   timed notes (`verify`) agree with madmom's within 5 ms.
2. **Measures of another length** (a bar of 5, 6 or 10 beats in 4/4 that the recording plays as a plain 4-beat
   bar) count their time signature's beats in the recording and get the recording's duration; their positions are
   spread as the coarse map has them. A regular measure in an alignment gap whose length on the coarse map is a
   beat or more off its own (and no measure of another length next to it) counts the beats it has there (Love Has
   No Pride plays three 9/8 bars with two beats and three with five).
3. **The tempo map** (`rectempo.plan`): per written measure the times of its positions from its start shaped into
   tempo marks by `timeline._shape`: a mark where the measure starts, more only where one tempo would miss a
   position by more than `tempo_tolerance_ms` (20); every measure lands on its length (MuseScore's tempo precision).
   Written as `timeline` writes a partner's clock: invisible tempo marks "recording tempo q = ...", the score's own
   tempo marks, fermatas and gradual tempo changes `<play>0</play>`, breaths and section breaks pause 0; into the
   formatter's model and the file alike (MuseScore's MIDI export of the copy has every note-on where the formatter
   has it).
4. **Repeats.** A written measure played on several passes can carry one tempo only. The passes' mean is simulated
   (a measure played once takes up the error that came before it); if the error stays within `repeat_tolerance_ms`
   (40) at every measure as played the repeats stay, otherwise (`--unroll auto`; `always`, `never`) the copy has them
   **written out** (`rectempo.unroll_mscx`): the measures in the order they are played, start/end repeats, voltas,
   segno / coda / fine markers and jumps removed, each copy with its own tempo; the k-th pass's copy carries the
   syllables that pass sings (verse k, as `lyrics.collect_lyrics` reads it: a chord without this pass's verse is
   silent when the verse is sung elsewhere in the measure, else its lowest verse) as verse 1; slurs and ties that
   would now run into another measure are dropped (a note tied over a jump ends at its barline: the formatter and
   MuseScore held it into the next measure played); clefs, key and time signatures are restated where the new order
   needs them; element ids are dropped from the second and later copies (MuseScore gives them new ones). Frames go
   before the first copy of the measure they precede.
5. **The offset.** Note Waterfall plays the audio file at `audioFileTime = songTime + GlobalAudioLead + AudioOffset`
   (`PlaybackController.SongToAudioFileTime`), and `deploy_songs.py` writes the audio map's `offset` into the
   song's `.audio.json`, which becomes `AudioOffset`. So `offset` = the recording time at which the song's first
   measure starts. **Positive**: the recording has more lead-in than the score; playback from the app's lead-in
   (`-LeadIn`, 3 s) starts the file `offset - LeadIn` seconds in (`DecideAudioStart`: Immediate), or schedules it from
   0 when that is negative, so the recording's intro before the score's first measure is heard for the lead-in and
   the rest skipped. **Negative**: the score starts before the recording (Fountain of Sorrow's first measure, which
   the recording does not have): the first measures play at the recording's tempo (carried back) and the audio is
   scheduled to start `-offset` seconds into the song. Times are the recording as ffmpeg decodes it, gapless: the
   1105 samples (25 ms) of encoder and decoder delay that the original mp3's LAME header declares are skipped. A
   decoder that ignores the header, and a player fed the mp3's frames (the Sonos), plays the file 25 ms later.
6. **Verification** of the copy before anything is proposed: the formatter's render of the copy against the
   recording's measures and beats (the song's clock + offset; median, p95, max; all p95 within 30 ms), its note onsets
   against where the recording plays their positions; the notes the same as the score's but for their timing (per
   measure as played: onset, length, pitch, staff; a tie over a jump and an arpeggio rolled at the new tempo are
   listed); `lyrics.collect_lyrics` the same syllables on the same notes and passes; the parts file the same
   original, orchestra and vocal notes and sung notes; MuseScore (`[accompaniment] mscore`, `--no-musescore` skips)
   opens it, exports MusicXML with its lyrics and MIDI on the formatter's clock; and the original recording against
   the copy's notes at the offset (`recording_align.py verify --offset`: chroma lag and onset lag over windows).
   The heavy steps (alignment, madmom, MuseScore) wait until `min_free_mb` (6144) of memory is available.

Outputs in `<tempo_dir>/<song>/` (`~/.cache/nw-rectempo`): the proposed copy (the score's file name), `report.md`,
`tempo.json` (everything, also what `retime` / `align` read), `audio_map.entry.json` (the original file, the
offset; `enabled` only when the checks pass and the recording is in the score's key), and the analysis (`ref.json`,
`align.json`, `act.npz`, `bars.json`, `copy-verify.json`), cached by their inputs. `--in-place --backup DIR` applies
the verified copy only if the score and the recording have not changed since and the copy passed its checks (else
it runs again first); it copies the original to `DIR` (never over an existing backup), writes atomically with the
file's mode kept and records it in `tempo.json`. `recordings align` / `retime` then propose the original file at the
offset for Note Waterfall's audio map once the score in the library is the applied copy.

Three songs (2026-10-19):

| | Fountain of Sorrow | Love Needs a Heart | Love Has No Pride |
|---|---|---|---|
| measures as played | 210 (122 written) | 63 | 98 (61 written) |
| confidence high / medium / low | 124 / 63 / 23 | 22 / 33 / 8 | 38 / 34 / 26 |
| beats placed (both detectors agree) | 806 (761) | 245 (194) | 245 (190) |
| one tempo per written measure, worst | 307 ms: written out | no repeats | 1170 ms: written out |
| offset | -0.974 s | +0.115 s | +11.090 s |
| copy vs recording: measures, beats, note onsets (p95) | 0, 15, 12.5 ms | 0, 12.5, 16.7 ms | 0, 18.6, 16.7 ms |
| recording vs copy's notes: onset lag median (within 50 ms) | +3.4 ms (99 %) | +3.4 ms (97 %) | +1.3 ms (100 %) |

Love Has No Pride's recording is a semitone above the score: its entry stays disabled until you decide.

## Score structure fixes (scorefix)

`python3 -m pianovision scorefix report SCORE` prints what the score plays, with repeats, voltas and jumps unrolled
the way MuseScore unrolls them (`pianovision.repeats`; MuseScore's own MIDI export agrees). It also lists the voltas
no pass reaches: a volta numbered "99", as a PDF import leaves it, is skipped every time, together with an end repeat
inside it. Then the bars that are never played, and the bars whose length is not the time signature's. Bars are
numbered in file order; when a bar is excluded from the count, MuseScore shows numbers one lower.

`scorefix apply SCORE FIXES.json --out COPY.mscz` writes structural fixes into a copy. Everything else in the file
stays byte for byte. `FIXES.json` is a list:

```json
[{"op": "tempo", "bpm": 126},
 {"op": "volta", "measure": 38, "endings": [1], "text": "1.", "measures": 4, "closed": true},
 {"op": "repeat_count", "measure": 71, "count": 3},
 {"op": "marker", "measure": 10, "kind": "segno"},
 {"op": "jump", "measure": 72, "kind": "ds_al_coda"}]
```

* `marker` kinds: `segno`, `coda`, `tocoda`, `fine`. They are written as MuseScore 4 writes them: the coda sign is
  `codab`, "To Coda" is `coda`.
* `jump` kinds: `ds_al_coda`, `dc_al_coda`, `ds_al_fine`, `dc_al_fine`, `ds`, `dc`.
* `--in-place --backup DIR` overwrites the score itself. It copies the original to DIR and checks the copy first,
  never overwrites a backup, refuses if the score changed meanwhile, writes atomically, and asks unless `--yes` is
  given.

`scorefix reference SCORE --out ref.json` renders the score into `tools/recording_align.py`'s reference format. Use
it to check a fixed copy against a recording, for example with `recording_align.py align --ref ref.json --audio REC`.

## Lyrics from a text file (lyrics import)

`python3 -m pianovision lyrics import SCORE LYRICS.txt` puts a song's lyrics on the vocal line of its score, from a
text file you supply. Note Waterfall then shows the falling melody notes with their syllables: `build` reads the
score's lyrics into the parts file (`pianovision.lyrics`). The tool works on whatever text it is given and its reports
never show it: only counts, line numbers and bar numbers.

The text file is plain UTF-8; blank lines are ignored:

```
[Verse 1]                 a section header; [Chorus: who sings] is fine (only the name is used)
one sung line per text line
a lov-er's word           "-" splits a word into syllables yourself (your split wins)
hold_ me now__            "_" holds a syllable over one more note (a melisma), "__" over two
[Piano Solo]              a header without lines: an instrumental section
[Chorus]                  a header without lines, named like an earlier sung section: that section again
[Verse 3 @ m.10 pass 3]   optional pin: the section's first line starts in bar 10, its 3rd time through
[Outro x2]                the section twice; a line ending in "(x2)" is sung twice
```

* Parenthesized text, `(ooh)`, is taken as backing vocals and left out (`--keep-parentheses` keeps it).
* Junk pasted from a lyrics site is tolerated: a line that obviously is not lyrics ("You might also like" and the
  song titles after it, "12 Contributors", "See … Live", "Get tickets", "Translations", writer and licence
  footers) starts a block that is skipped up to the next header; a "Title Lyrics" first line and an "Embed" glued
  to the last line are dropped. Each skip is reported by line number.
* Words are split into syllables by pyphen's English hyphenation when pyphen is installed, else by a small
  rule-based English syllabifier (`lyricsimport.syllables`: "lit-tle", "go-ing", "love-ly", "did-n't"). Mark any
  word it gets wrong with "-" yourself.

How the text meets the score:

1. The vocal line is the score's vocal staff (the formatter's classification, `pvjson.staff_roles`; `--staff N`
   names another), its first voice, in playback order: repeats, voltas and D.S./coda jumps unrolled as MuseScore
   plays them. A tied note counts once. Rests split the line into phrases.
2. The text's lines are matched to the phrases in order, by the cheapest fit of syllables to notes (a dynamic
   programme): a line on one phrase, a line over several phrases (a rest inside the line), several lines in one
   phrase (no rest between them), a phrase no line is sung on, a line sung nowhere. The headers are soft anchors
   (a section starts after a rest, an instrumental section is a long rest); the `@ m.N` pins are hard ones. The
   dry run says, per line, how it was matched.
3. In each match the syllables go onto the notes in order. Extra notes hold the syllable before them (a melisma,
   preferably on the last syllable of a word, under a slur, on a change of pitch, at the end of a line; never across
   a rest). More syllables than notes are merged: the syllables of one word first, else two words on one note with
   MuseScore's elision. Notes left over get no syllable. Each merge and leftover note is a mismatch in the report.
4. Each syllable is written on its chord with the verse number of its pass: on the k-th pass through a bar, verse
   k, which is how `pianovision.lyrics` (and MuseScore) read them, so repeated music carries stacked verses. A word's
   last syllable held over more notes gets an extender up to the last note it holds.

Bars are numbered in file order, as `scorefix report` numbers them. The verse numbers follow the passes: apply
any repeat fix (`scorefix`) first, then import the lyrics.

`--dry-run` writes nothing and prints, per section and line, the syllables against the notes, the held notes, the
merges and the notes left over, with bar numbers and passes ("m.41, m.10-13 (2nd time)"). `-o COPY.mscz` writes a
copy; `--in-place --backup DIR` overwrites the score after copying the original to DIR (never over an existing
backup; it asks unless `--yes`). A staff that already has lyrics is refused unless `--replace` removes them first.

The `<Lyrics>` elements are written as MuseScore 4.7.5 saves them (`no`, `syllabic`, `ticks_f` for an extender,
`text`, right after the chord's duration); everything else in the file stays byte for byte. Before anything is
written, the edited score is verified in `~/.cache/nw-lyricsimport` (`--work`): the formatter reads it and renders
the same notes (the PianoVision JSON byte for byte, the parts file the same but for its lyrics),
`lyrics.collect_lyrics` finds every syllable on its note and pass, and MuseScore (`[accompaniment] mscore`,
`--mscore`) headless opens it and exports it, with its lyrics counted (skipped with `--no-musescore`, or when less
than 2 GB of memory is free). On a failure nothing is written.

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
  lyricsimport.py     lyrics from a text file onto a score's vocal line (lyrics import): syllables, matching, writing
  buildopts.py        per-score build options (scores.json): title, piano staves and hands, partner scores
  arrangement.py      a piano solo aligned with an orchestral score: measure alignment, retiming, report
  timeline.py         a partner song on the solo's clock: the tempo plan, the edit of the orchestral score, orchestra_from
  handsplit.py        the hands of a piano part written on one staff (hands: "split")
  convert.py          one score -> JSON
  library.py          manifest, incremental build, adoption of existing outputs
  device.py           adb deploy
  calibrate.py        compatibility-knob search used by verify --calibrate
  handedits.py        hand edits from Note Waterfall -> the scores (hands pull/review/apply)
  handsync.py         hand edits applied by themselves while the headset is connected (hands sync): headset
                      polling, review verdicts, MuseScore-open check, ledger, notifications, status file
  accompaniment.py    Note Waterfall's companion audio: render copies, MuseScore + Muse Sounds, probe, mp3
  recordings.py       companion audio from recordings you own: inventory, routes and candidates (TypeSafe judgments),
                      links file, references, align/retime/verify runs, accuracy summaries, audio_map proposal, report
  separation.py       vocals stems (Demucs in another Python), cached by content; finding a separator
  recmap.py           time maps between a recording and a song: DTW path -> pieces and gaps, the edit plan
  cli.py              python -m pianovision
  gui.py              the Piano Library window (GTK 4 / libadwaita)
  guimodel.py         what the window shows and does, without GTK (statuses, push plans, device status)
desktop/            piano-library.sh (launcher), install-launcher.sh, the icon; nw-handsync.service and
                    install-handsync.sh (the hands sync login service)
tools/              accompaniment_timing.py (the accompaniment's timing check; numpy, soundfile, librosa)
                    recording_align.py (recordings: align, retime, verify; numpy, scipy, soundfile, librosa, ffmpeg)
                    separate_vocals.py (recordings: Demucs vocals; run with [recordings] separator_python)
pianovision.toml    settings
scores.json         per-score options of `build` (titles, piano staves, partner scores)
accompaniment.json  per-score options of `accompaniment` (relabelled instruments, techniques, ...)
recordings.json     every song's companion route, recordings linked to the songs (`recordings match`), your decisions
PianoVision/        generated library (git-ignored): *.json, .manifest.json, .attic/, NoteWaterfall/*.parts.json,
                    .arrangements/ (alignment reports of the partner songs)
```

## Legacy scripts

`convert_all.sh`, `musescore_to_json.py`, `midi_to_json.py`,
`musicxml_to_json.py`, `update_midis.py` (headless MuseScore MIDI export) and
their helpers still work and need the packages in `requirements.txt`. They write
into the same `PianoVision/` folder; if they change a file, `status` reports it
as edited outside the pipeline. MusicXML input is only supported there.
`sync_pianovision_files.sh` now removes device files only when given `--delete`
as its second argument.
