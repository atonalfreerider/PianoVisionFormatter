"""Note Waterfall's companion audio from recordings you own: a recording in the music folder linked to its song and
put on the song's timeline (``python -m pianovision recordings match | align | retime | status | confirm | reject``).

1. **Match.**  The music folder is inventoried (ffprobe: duration and the title, artist, album and composer tags;
   no other tag is read) and its files grouped into recordings: a full mix with its LALAL.AI stems
   (``..._no_piano_split_by_lalalai``), and what was derived from it (``_retimed``, ``-retime``, ``_mixdown``) or
   copied (same name and size).  For every song of the library, code proposes candidates (IDF-weighted word overlap
   of title, artist, score file and folder names with the recordings' tags, file and folder names) and one TypeSafe
   request judges them ("select instead of generate"): a Choice among the candidates and "none" (asked twice, the
   candidates in reverse order the second time, and averaged), and per candidate a Noul "is this the same
   composition".  :func:`decide` turns the answers into a link: ``linked`` (used), ``review`` (needs your
   confirmation; not used), ``none``.  The links file (``recordings.json``, checked in next to ``scores.json``)
   records the pick, its confidence and the alternatives; your ``decision`` in it (``confirm`` / ``reject``) always
   wins and is never overwritten.
2. **Align** (``tools/recording_align.py align``, with ``[recordings] python``): the recording's full mix against a
   synthesis of the song's notes (the parts file's original piano part and orchestra, on the song's clock):
   transposition, chroma DTW, the matched pieces and the structural gaps, beat-level refinement on onsets.
3. **The recording leads** (sung songs, the default: ``recordings tempo``, :mod:`pianovision.rectempo`): the
   score's tempo map is rewritten so its measures last what the recording's do, and the original file plays at an
   offset, never stretched; ``align`` / ``retime`` propose it once the score carries that tempo map.
   **Retime** (``"timing": "score"`` in a link's decision): the delivered variant (the piano-removed stem when the
   recording has one, else the full mix; your ``decision`` may choose) cut, padded and stretched onto the song's
   timeline (rubberband), retuned to A440 when it
   is off by ``retune_cents`` or more, then loudness-normalised and encoded like the accompaniment
   (:func:`pianovision.accompaniment.encode_mp3`: lands at sample 0 in every decoder).  Verified: the retimed audio
   against the song's notes per measure (chroma agreement, onset lag).
4. **Outputs** (``out_dir``): ``<song>.mp3``, ``audio_map.proposed.json`` (entries for Note Waterfall's
   ``Tools/audio_map.json``; ``enabled`` only when the checks pass), ``accuracy/<song>.json`` (the score against
   the recording: key offset, tuning, tempo ratio and drift, structural differences, measures with low agreement)
   and ``recordings-report.md`` / ``.json``.

The music folder, the scores and the library are only read.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as _dt
import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import recmap as RM

LINKS_NAME = "recordings.json"
ROUTE_VOCALS, ROUTE_GENERATED, ROUTE_REVIEW = "vocals", "generated", "review"
SEPARATED = "(separated from the full mix)"
REF_VERSION = 2                 # bump when the reference (ref.json) is made differently
REPORT_NAME = "recordings-report"
PROPOSAL_NAME = "audio_map.proposed.json"
API_URL = "https://api.typesafe.ai/v1/systemone"
NONE = "none"
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".aif", ".aiff", ".wma"}
VIDEO_EXT = {".mp4", ".mts", ".m2ts", ".mov", ".mkv", ".webm", ".avi", ".ts"}
TAG_KEYS = ("title", "artist", "album", "album_artist", "composer")    # never lyrics
STATUS_LINKED, STATUS_REVIEW, STATUS_NONE = "linked", "review", "none"

Log = Callable[[str], None]


class RecordingsError(RuntimeError):
    pass


# ------------------------------------------------------------------------------
# settings ([recordings] in pianovision.toml)
# ------------------------------------------------------------------------------

@dataclass
class Settings:
    music_dir: str = "~/Music"                                   # read only
    exclude: List[str] = field(default_factory=lambda: ["Note Waterfall Accompaniments", "MuseScore MIDI"])
    links: str = LINKS_NAME                                      # relative to pianovision.toml
    out_dir: str = "~/.cache/nw-recordings/out"                  # mp3s, audio_map proposal, accuracy, report
    work_dir: str = "~/.cache/nw-recordings/work"                # inventory, references, alignments, WAVs
    python: str = "~/miniconda3/envs/midi-retime/bin/python"    # numpy, scipy, soundfile, librosa
    script: str = "tools/recording_align.py"
    model: str = "jev-1.13.0"                                    # pinned: the thresholds below were set with it
    candidates: int = 12                                         # at most this many candidates per song
    min_candidate_score: float = 0.08
    link_confidence: float = 0.6                                 # Choice confidence to link without review
    same_min: float = 0.8                                        # Noul "same composition" to count a candidate valid
    none_min: float = 0.8                                        # P(none) to settle on no recording
    retune_cents: float = 15.0                                   # retune to A440 when off by this much (0: never)
    tail: float = 8.0                                            # seconds of the recording after the song's end
    bitrate: int = 256
    lufs: float = -16.0
    true_peak: float = -1.0
    audio_map: str = "~/Desktop/Note-Waterfall/Tools/audio_map.json"   # read only: what Note Waterfall has now
    sung_min: float = 0.8               # Noul "sung" from this up: sung (when the score has no vocal line)
    unsung_max: float = 0.3             # ... up to this: instrumental; in between: your decision
    classical_min: float = 0.5          # P(kind = classical) from this up: classical, the companion is generated
    min_score_vocal: int = 16           # vocal-staff notes or lyric syllables in the score that make it sung
    stems_dir: str = "~/.cache/nw-recordings/stems"     # vocals stems, by the input's content
    separator_python: str = ""          # a Python with demucs ("": the first conda environment that has it)
    separator_model: str = "htdemucs_ft"
    separator_script: str = "tools/separate_vocals.py"
    min_vocal_activity: float = 0.1     # a vocals stem with singing in less of the matched recording: not used
    min_coverage: float = 0.85          # enabled only when this share of the song (of a vocals stem: of the score's
                                        # vocal line) is matched
    min_melody_agreement: float = 0.3   # ... a vocals stem's pitch agrees with the vocal line in this share of frames
    max_lag_ms: float = 30.0            # ... the median onset lag after the retime is within this
    min_within_50ms: float = 0.7        # ... and this share of the lag windows is within 50 ms
    timeout: int = 7200
    api_timeout: float = 60.0
    workers: int = 6
    # the recording leads (recordings tempo): the score's tempo map follows the recording, the original file plays
    sung_timing: str = "recording"      # sung songs: "recording" (the score follows the recording, nothing is
                                        # stretched) or "score" (the recording stretched onto the score: retime)
    tempo_dir: str = "~/.cache/nw-rectempo"             # per song: analysis, the proposed copy, its report
    beats_python: str = ""              # a Python with madmom (beat detection; "" = librosa's beats only)
    beats_script: str = "tools/beat_activations.py"
    tempo_tolerance_ms: float = 20.0    # a position inside a measure lands this close to the recording's time
    repeat_tolerance_ms: float = 40.0   # repeats stay when one tempo per written measure keeps within this
    min_free_mb: int = 6144             # heavy steps wait for this much available memory
    # a concerto's recording with the piano's solo passages muted (recordings mute-solos, pianovision.solomute)
    solo_mute_dir: str = "~/Music/Note Waterfall Recordings"   # <song>.mp3 + <song>.solo-mute.json
    solo_min_seconds: float = 2.0       # a stretch where the piano plays alone shorter than this is not muted
    # a recording's stems for Note Waterfall's mixer (recordings stems): <stems_out_dir>/<song>/<stem>.mp3
    stems_out_dir: str = "~/Music/Note Waterfall Stems"

    def path(self, name: str) -> str:
        return os.path.abspath(os.path.expanduser(getattr(self, name)))


def load_settings(config_path: Optional[str], **overrides) -> Settings:
    """``[recordings]`` of pianovision.toml; ``links`` and the scripts relative to that file."""
    import tomllib
    raw: dict = {}
    base = os.getcwd()
    if config_path and os.path.isfile(config_path):
        with open(config_path, "rb") as f:
            raw = tomllib.load(f).get("recordings", {})
        base = os.path.dirname(os.path.abspath(config_path))
    s = Settings()
    for f in dataclasses.fields(Settings):
        if f.name in raw:
            cur = getattr(s, f.name)
            setattr(s, f.name, list(raw[f.name]) if isinstance(cur, list) else type(cur)(raw[f.name]))
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, v)
    for name in ("links", "script", "separator_script", "beats_script"):
        v = os.path.expanduser(getattr(s, name))
        if v and not os.path.isabs(v):
            setattr(s, name, os.path.normpath(os.path.join(base, v)))
    return s


# ------------------------------------------------------------------------------
# the music folder: files and recordings
# ------------------------------------------------------------------------------

LALAL = re.compile(r"_(no_piano|piano|no_vocals|vocals|instrumental|drums|bass)_split_by_lalalai$", re.I)
LALAL_TITLE = re.compile(r"_(no_piano|piano|no_vocals|vocals|instrumental|drums|bass)_\(Split_by_LALAL\.AI\)$", re.I)
DERIVED = ((re.compile(r"[_-]retimed?$", re.I), "retimed"),
           (re.compile(r"[_ -]?(?:orchestra|orch)?[_-]?mixdown$", re.I), "mixdown"))


@dataclass
class AudioFile:
    rel: str                        # relative to the music folder
    size: int
    mtime: float
    duration: float
    tags: Dict[str, str] = field(default_factory=dict)

    @property
    def stem(self) -> str:
        return os.path.splitext(os.path.basename(self.rel))[0]

    @property
    def dir(self) -> str:
        return os.path.dirname(self.rel)


def classify_name(stem: str) -> Tuple[str, str, str]:
    """(base name, kind, derived) of a file name without extension: ``kind`` "full" or the LALAL.AI stem
    ("no_piano", "piano", "vocals", ...); ``derived`` "", "retimed" or "mixdown"."""
    base, derived = stem.strip(), ""
    for rx, what in DERIVED:
        m = rx.search(base)
        if m and m.start() > 0:
            derived, base = what, base[:m.start()]
            break
    kind = "full"
    m = LALAL.search(base)
    if m:
        kind, base = m.group(1).lower(), base[:m.start()]
    return base.strip(" _-"), kind, derived


def title_base(title: str) -> str:
    return LALAL_TITLE.sub("", title or "").strip(" _-")


def probe(path: str) -> Tuple[float, Dict[str, str]]:
    """Duration and the :data:`TAG_KEYS` tags (only those are requested: lyrics are never read)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration:format_tags=" + ",".join(TAG_KEYS), "-of", "json", path],
                       capture_output=True, text=True)
    try:
        fmt = json.loads(r.stdout or "{}").get("format", {})
    except ValueError:
        fmt = {}
    tags = {k.lower(): str(v).strip() for k, v in (fmt.get("tags") or {}).items()
            if k.lower() in TAG_KEYS and str(v).strip()}
    try:
        dur = float(fmt.get("duration", 0.0))
    except (TypeError, ValueError):
        dur = 0.0
    return dur, tags


def scan_music(s: Settings, log: Log = print) -> Tuple[List[AudioFile], List[str]]:
    """Audio files of the music folder (cached by path, size and mtime in ``work_dir/inventory.json``) and the video
    files (listed, not offered)."""
    root = s.path("music_dir")
    if not os.path.isdir(root):
        raise RecordingsError(f"music folder not found: {root}")
    cache_path = os.path.join(s.path("work_dir"), "inventory.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = {d["rel"]: d for d in json.load(f).get("files", [])}
    except (OSError, ValueError):
        cache = {}
    excluded = {os.path.normpath(e) for e in s.exclude}
    found: List[Tuple[str, os.stat_result]] = []
    videos: List[str] = []
    for d, dirs, files in os.walk(root):
        reld = os.path.relpath(d, root)
        dirs[:] = sorted(x for x in dirs if os.path.normpath(os.path.join(reld, x) if reld != "." else x)
                         not in excluded and not x.startswith("."))
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            rel = os.path.normpath(os.path.join(reld, name)) if reld != "." else name
            if ext in AUDIO_EXT:
                found.append((rel, os.stat(os.path.join(d, name))))
            elif ext in VIDEO_EXT:
                videos.append(rel)
    out: List[AudioFile] = []
    todo = []
    for rel, st in found:
        c = cache.get(rel)
        if c and c.get("size") == st.st_size and abs(c.get("mtime", 0) - st.st_mtime) < 1e-3:
            out.append(AudioFile(rel, st.st_size, st.st_mtime, float(c.get("duration", 0.0)), dict(c.get("tags", {}))))
        else:
            todo.append((rel, st))
    if todo:
        log(f"probing {len(todo)} audio files ...")
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda x: probe(os.path.join(root, x[0])), todo))
        for (rel, st), (dur, tags) in zip(todo, res):
            out.append(AudioFile(rel, st.st_size, st.st_mtime, dur, tags))
    out.sort(key=lambda a: a.rel)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    _write_json(cache_path, {"format": 1, "root": root, "files": [dataclasses.asdict(a) for a in out]})
    return out, videos


@dataclass
class Recording:
    """One performance: its main file (the full mix when there is one) and its other files."""
    key: str                                                    # rel path of the main file
    files: Dict[str, str] = field(default_factory=dict)         # kind ("full", "no_piano", ...) -> rel
    duration: float = 0.0
    tags: Dict[str, str] = field(default_factory=dict)
    bases: Set[str] = field(default_factory=set)                # base names of its files (lower case)
    others: List[Tuple[str, str]] = field(default_factory=list)  # (rel, what): derived files, copies, alternates
    derived: str = ""                                           # the main file itself is derived ("retimed", ...)

    @property
    def dir(self) -> str:
        return os.path.dirname(self.key)

    def variants(self) -> Dict[str, str]:
        """The files that can be delivered: ``vocals`` (a vocals stem in the folder, else separated from the full mix:
        :data:`SEPARATED`), and as explicit choices the ``full`` mix and the ``no_piano`` stem."""
        out = {}
        if "vocals" in self.files:
            out["vocals"] = self.files["vocals"]
        elif "full" in self.files:
            out["vocals"] = SEPARATED
        for k in ("full", "no_piano"):
            if k in self.files:
                out[k] = self.files[k]
        return out

    def default_variant(self, route: str = "") -> Optional[str]:
        """The vocals for a sung song; nothing for the others (their companion is generated from the score)."""
        if route == ROUTE_VOCALS:
            return "vocals" if "vocals" in self.variants() else None
        return None

    def source(self) -> str:
        """The file to align: the full mix (it has the piano the song's notes have), else the main file."""
        return self.files.get("full") or self.key

    def desc(self) -> dict:
        """What the matching judgment sees of it."""
        d: Dict[str, Any] = {"file": self.key}
        for k in ("title", "artist", "album", "composer"):
            if self.tags.get(k):
                d[k] = self.tags[k]
        return d

    def text(self) -> str:
        return " ".join([self.key.replace("/", " ")] + [self.tags.get(k, "") for k in ("title", "artist", "album",
                                                                                         "composer")])


def group_recordings(files: Sequence[AudioFile]) -> List[Recording]:
    """Files -> recordings (see the module docstring)."""
    info = {f.rel: (f,) + classify_name(f.stem) for f in files}
    # copies: same file name and size; the first (by path) stands for them
    seen: Dict[Tuple[str, int], str] = {}
    copies: Dict[str, str] = {}
    for f in sorted(files, key=lambda x: x.rel):
        k = (os.path.basename(f.rel).lower(), f.size)
        if k in seen:
            copies[f.rel] = seen[k]
        else:
            seen[k] = f.rel
    recs: Dict[str, Recording] = {}
    owner: Dict[str, str] = {}

    def new(f: AudioFile, kind: str, base: str) -> Recording:
        tags = dict(f.tags)
        if tags.get("title"):
            tags["title"] = title_base(tags["title"])
        r = Recording(f.rel, {kind: f.rel}, f.duration, tags, {base.lower()})
        recs[f.rel] = r
        owner[f.rel] = f.rel
        return r

    prim = [f for f in files if f.rel not in copies]
    for f in prim:
        _f, base, kind, derived = info[f.rel]
        if kind == "full" and not derived:
            new(f, kind, base)
    # stems: to the full mix of the same folder they were split from
    for f in prim:
        _f, base, kind, derived = info[f.rel]
        if kind == "full" or derived:
            continue
        tb = title_base(f.tags.get("title", "")).lower()
        same_dir = [r for r in recs.values() if r.dir == f.dir and "full" in r.files]
        close = [r for r in same_dir if abs(r.duration - f.duration) <= 0.25]

        def named(r: Recording) -> bool:
            rb = info[r.files["full"]][1].lower()
            rt = (r.tags.get("title") or "").lower()
            b = base.lower()
            if b and (rb.startswith(b) or b.startswith(rb)):
                return True
            if tb and (rb.startswith(tb) or tb.startswith(rb)):
                return True
            return bool(tb and rt and (rt.startswith(tb) or tb.startswith(rt)))
        match = [r for r in close if named(r)]
        if not match and len([r for r in same_dir if abs(r.duration - f.duration) <= 0.05]) == 1:
            match = [r for r in same_dir if abs(r.duration - f.duration) <= 0.05]
        if not match:
            # stems without their full mix: one recording per folder and base
            hit = [r for r in recs.values() if r.dir == f.dir and "full" not in r.files and base.lower() in r.bases]
            if hit:
                match = hit
        if match:
            r = max(match, key=lambda x: len(os.path.commonprefix([x.key.lower(), f.rel.lower()])))
            if kind in r.files:
                r.others.append((f.rel, f"another {kind} stem"))
            else:
                r.files[kind] = f.rel
                r.bases.add(base.lower())
                if not r.tags and f.tags:
                    r.tags = {k: (title_base(v) if k == "title" else v) for k, v in f.tags.items()}
            owner[f.rel] = r.key
        else:
            new(f, kind, base)
    # derived files: to the only recording of their folder, else to the recording with their base name
    for f in prim:
        _f, base, kind, derived = info[f.rel]
        if not derived:
            continue
        what = f"{derived}" + (f" {kind} stem" if kind != "full" else "")
        same_dir = [r for r in recs.values() if r.dir == f.dir]
        target = None
        if len(same_dir) == 1:
            target = same_dir[0]
        else:
            byname = [r for r in recs.values() if base.lower() in r.bases]
            if byname:
                target = max(byname, key=lambda r: ("full" in r.files, -len(r.key)))
        if target is not None:
            target.others.append((f.rel, what))
            owner[f.rel] = target.key
        else:
            r = new(f, "full", base)
            r.derived = what
    for rel, first in copies.items():
        k = owner.get(first)
        if k and k in recs:
            recs[k].others.append((rel, f"copy of {first}"))
    return sorted(recs.values(), key=lambda r: r.key)


# ------------------------------------------------------------------------------
# songs and candidates
# ------------------------------------------------------------------------------

@dataclass
class Song:
    rel: str                        # the score, relative to the scores folder
    output: str                     # the PianoVision JSON name
    title: str = ""
    artist: str = ""

    @property
    def stem(self) -> str:
        return self.output[:-5] if self.output.lower().endswith(".json") else self.output

    def desc(self) -> dict:
        d = {"title": self.title, "composer_or_artist": self.artist, "score_file": self.rel}
        return {k: v for k, v in d.items() if v}

    def text(self) -> str:
        return " ".join([self.title, self.artist, os.path.splitext(self.rel)[0].replace("/", " ")])


def library_songs(manifest: dict) -> List[Song]:
    out = []
    for rel, e in sorted(manifest.get("entries", {}).items()):
        if e.get("output"):
            out.append(Song(rel, e["output"], e.get("title", ""), e.get("artist", "")))
    return out


STOP = {"the", "a", "an", "of", "and", "in", "on", "for", "from", "by", "to", "with", "at", "de", "la", "le", "les",
        "du", "des", "di", "il", "et", "remastered", "remaster", "official", "audio", "video", "lyrics", "lyric", "hd",
        "full", "mp", "wav", "mscz", "prod", "feat", "ft", "split", "lalalai", "lalal", "ai", "no", "op", "score",
        "subtitle", "untitled", "arranger", "composer", "arr", "arranged", "version", "mix", "words", "music",
        "track", "mixdown", "retime", "retimed", "www", "com", "kbps", "fiber", "mp3fiber", "midifind", "solo"}


GENERIC = {"major", "minor", "flat", "sharp", "piano", "orchestra", "movement", "first", "second", "third", "theme",
           "remix", "slowed", "original", "cover", "live", "edit", "concerto", "symphony", "sonata", "op", "opus",
           "variation", "var", "arrangement", "advanced", "easy", "part", "song", "suite",
           # categories (library and music folders): they say what kind of music, not which piece
           "soundtrack", "ost", "film", "films", "movie", "pop", "classical", "country", "rock", "jazz", "output"}


def tokens(text: str) -> List[str]:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().replace("'", "")
    t = re.sub(r"([a-z])(\d)", r"\1 \2", t)
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)
    return [w for w in re.findall(r"[a-z]+|\d+", t) if w not in STOP and (len(w) > 1 or w.isdigit())]


def idf_table(docs: Iterable[Set[str]]) -> Dict[str, float]:
    docs = list(docs)
    df: Dict[str, int] = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def text_score(a: Set[str], b: Set[str], idf: Dict[str, float]) -> float:
    """IDF-weighted cosine of two word sets; a word that begins another (4 letters or more: "rach" and
    "rachmaninoff", "rachmaninov" and "rachmaninoff" by their first 7) counts 0.7."""
    if not a or not b:
        return 0.0
    w = lambda t: idf.get(t, 1.0)
    num = 0.0
    for t in a:
        if t in b:
            num += w(t) ** 2
            continue
        best = 0.0
        if len(t) >= 4 and not t.isdigit():
            for u in b:
                if len(u) >= 4 and not u.isdigit() and (u.startswith(t) or t.startswith(u)
                                                        or (len(t) >= 7 and len(u) >= 7 and t[:7] == u[:7])):
                    best = max(best, 0.7 * min(w(t), w(u)) ** 2)
        num += best
    den = math.sqrt(sum(w(t) ** 2 for t in a)) * math.sqrt(sum(w(u) ** 2 for u in b))
    return num / den if den else 0.0


def content_overlap(a: Set[str], b: Set[str]) -> bool:
    """Whether the two share a word that names something (not a number or a generic musical word), exactly or as
    the beginning of the other (4 letters or more)."""
    for t in a:
        if t.isdigit() or t in GENERIC:
            continue
        for u in b:
            if u.isdigit() or u in GENERIC:
                continue
            if t == u or (min(len(t), len(u)) >= 4 and (u.startswith(t) or t.startswith(u))):
                return True
    return False


def propose_candidates(songs: Sequence[Song], recs: Sequence[Recording], k: int = 8, floor: float = 0.08
                       ) -> Dict[str, List[Tuple[Recording, float]]]:
    """Per song (its score path): up to ``k`` recordings by :func:`text_score`, at least ``floor`` and sharing a word
    that names something (:func:`content_overlap`)."""
    st = {s.rel: set(tokens(s.text())) for s in songs}
    rt = {r.key: set(tokens(r.text())) for r in recs}
    idf = idf_table(list(st.values()) + list(rt.values()))
    out: Dict[str, List[Tuple[Recording, float]]] = {}
    for s in songs:
        scored = [(r, text_score(st[s.rel], rt[r.key], idf)) for r in recs
                  if content_overlap(st[s.rel], rt[r.key])]
        scored = [x for x in scored if x[1] >= floor]
        scored.sort(key=lambda x: (-x[1], x[0].key))
        out[s.rel] = scored[:k]
    return out


# ------------------------------------------------------------------------------
# the judgment (TypeSafe System One, HTTP API)
# ------------------------------------------------------------------------------

def system_one(state: Any, questions: Dict[str, dict], model: str, api_key: Optional[str] = None,
               timeout: float = 60.0, retries: int = 5) -> dict:
    """POST /v1/systemone; retries 429, 529 and 5xx with exponential backoff (honouring Retry-After)."""
    key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
    if not key:
        raise RecordingsError("TYPESAFE_API_KEY is not set")
    body = json.dumps({"state": state, "model": model, "questions": questions}).encode("utf-8")
    delay = 1.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(API_URL, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if (e.code in (429, 529) or 500 <= e.code < 600) and attempt < retries:
                ra = e.headers.get("retry-after") if e.headers else None
                e.close()
                try:
                    wait = float(ra) if ra else delay
                except ValueError:
                    wait = delay
                time.sleep(min(wait, 30.0))
                delay *= 2
                continue
            detail = e.read().decode("utf-8", "replace")[:500]
            e.close()
            raise RecordingsError(f"TypeSafe API: HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise RecordingsError(f"TypeSafe API: {e}") from None
    raise RecordingsError("TypeSafe API: no answer")


SAME = {"same_composition": "The same composition: for a work in several movements, the same movement. An "
                            "arrangement, a cover, a live or remastered release, the orchestral original of a piano "
                            "arrangement, or a track with the piano or the vocals removed is still the same "
                            "composition.",
        "not_the_same": "A different piece by the same composer or artist, a different movement of the same work, a "
                        "piece that only shares words with the title, or a file whose name, folder and tags do not "
                        "say which piece it is (such as 'output.mp3' or 'Track 1')."}


def match_state(song: Song, cands: Sequence[Recording]) -> dict:
    return {"song": song.desc(), "recordings": [c.desc() for c in cands]}


def match_questions(song: Song, cands: Sequence[Recording]) -> Dict[str, dict]:
    """The Choice (twice: the candidates in order and reversed, "none" last both times) and a Noul per candidate."""
    def choice(order: Sequence[Recording]) -> dict:
        crit: Dict[str, Any] = {}
        for c in order:
            d = {k: v for k, v in c.desc().items() if k != "file"}
            crit[c.key] = d or None
        crit[NONE] = ("None of these recordings is a performance of the song: each is a different piece, a different "
                      "movement, only shares words with the title, or does not say which piece it is.")
        return {"type": "choice",
                "instructions": {"question": "Which audio file is a recording of the composition described in `song`?",
                                 **SAME},
                "criteria": crit}
    q = {"pick": choice(cands), "pick_reversed": choice(list(reversed(cands)))}
    for i in range(len(cands)):
        q[f"same_{i}"] = {"type": "noul",
                          "instructions": {"question": f"Is `recordings[{i}]` a recording of the same composition as "
                                                       f"`song`?", **SAME},
                          "criteria": {"true": "It is the same composition (see `same_composition`).",
                                       "false": "It is not (see `not_the_same`)."}}
    return q


def choice_confidence(probs: Sequence[float]) -> float:
    """TypeSafe's Choice confidence: (p_max - 1/n) / (1 - 1/n)."""
    n = len(probs)
    if n < 2:
        return 1.0
    return max(0.0, (max(probs) - 1.0 / n) / (1.0 - 1.0 / n))


def decide(answers: Dict[str, dict], cands: Sequence[Recording], s: Settings) -> dict:
    """The link from the answers (policy in code; thresholds in ``s``):

    * the two Choice distributions are averaged; ``confidence`` is the Choice confidence of the average, ``p_none``
      the probability of "none", ``consistent`` whether both orders chose the same;
    * a candidate is *valid* when its Noul "same composition" is at least ``same_min``;
    * ``linked``: the pick is valid and either the choice is consistent with ``confidence >= link_confidence``, or the
      probability is spread over valid recordings only (several recordings of the same piece) with ``p_none <= 0.1``;
    * ``none``: "none" was picked, no candidate is valid, and ``p_none >= none_min`` or every candidate's Noul is
      below 0.5;
    * ``review`` otherwise (the suggestion is the pick, or the best valid candidate when "none" was picked)."""
    keys = [c.key for c in cands] + [NONE]
    pa = answers["pick"].get("probabilities", {})
    pb = answers["pick_reversed"].get("probabilities", {})
    p = {k: 0.5 * (float(pa.get(k, 0.0)) + float(pb.get(k, 0.0))) for k in keys}
    same = {c.key: float(answers[f"same_{i}"]["noul"]) for i, c in enumerate(cands)}
    top = max(keys, key=lambda k: (p[k], k == NONE))
    conf = choice_confidence([p[k] for k in keys])
    consistent = answers["pick"].get("choice") == answers["pick_reversed"].get("choice")
    valid = [c for c in cands if same[c.key] >= s.same_min]
    out: Dict[str, Any] = {"confidence": round(conf, 3), "p_none": round(p[NONE], 3), "consistent": consistent}
    if top == NONE:
        if not valid and (p[NONE] >= s.none_min or all(v < 0.5 for v in same.values())):
            out.update(status=STATUS_NONE, recording=None, why="no candidate is this composition")
        else:
            best = max(valid, key=lambda c: (same[c.key], p[c.key])) if valid else None
            out.update(status=STATUS_REVIEW, recording=best.key if best else None,
                       why="'none' was picked" + (f" but {best.key} looks like the same composition "
                                                  f"(p_same {same[best.key]:.2f})" if best else
                                                  f" with p_none {p[NONE]:.2f} only"))
    else:
        spread_ok = p[NONE] <= 0.1 and all(same[k] >= s.same_min for k in p if k != NONE and p[k] >= 0.1)
        if same[top] < s.same_min:
            out.update(status=STATUS_REVIEW, recording=top,
                       why=f"picked, but its same-composition check is {same[top]:.2f}")
        elif consistent and conf >= s.link_confidence:
            out.update(status=STATUS_LINKED, recording=top, why="picked with high confidence")
        elif spread_ok and len([c for c in valid]) > 1:
            out.update(status=STATUS_LINKED, recording=top,
                       why=f"the probability is spread over {len(valid)} recordings of this composition")
        else:
            out.update(status=STATUS_REVIEW, recording=top,
                       why=("the two candidate orders picked differently" if not consistent else
                            f"confidence {conf:.2f} below {s.link_confidence}"))
    chosen = out.get("recording")
    out["p_same"] = round(same[chosen], 3) if chosen in same else None
    out["alternatives"] = [{"recording": c.key, "p": round(p[c.key], 3), "p_same": round(same[c.key], 3)}
                           for c in sorted(cands, key=lambda c: -p[c.key])
                           if c.key != chosen and (same[c.key] >= 0.5 or p[c.key] >= 0.1)]
    return out


def question_key(model: str, state: dict, questions: dict) -> str:
    blob = json.dumps({"model": model, "state": state, "questions": questions}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------------------
# the route: what a song's companion audio is
# ------------------------------------------------------------------------------
#
# * ``generated``: the score's orchestra rendered by MuseScore with Muse Sounds, the piano muted (``pianovision
#   accompaniment``): classical and instrumental music (the user plays the piano part; a recording's piano could not
#   be removed cleanly enough).  Never a recording; a linked recording is aligned for the accuracy summary only.
# * ``vocals``: the singing of a recording you own, isolated and retimed onto the song's timeline: sung songs (pop,
#   rock, musical, film songs).
# * ``review``: undecided; nothing is made until you decide (``recordings route SONG vocals|generated``).

KINDS = {
    "classical": "Classical concert music: concertos, symphonies, ballets and suites, piano and chamber works of the "
                 "classical repertoire, operas, arias, art songs and choral works.",
    "film_score": "Music written as the score of a film, show or game (themes and cues), not a song.",
    "popular_song": "A song from popular music: pop, rock, soul, folk, jazz standards, songs from musicals, and songs "
                    "written for films such as Bond theme songs.",
    "other": "Something else (folk dances, exercises, video game chiptunes, ...).",
}


def score_facts(entry: dict) -> dict:
    """What the library knows about a score's singing and accompaniment (the manifest's parts statistics, made by
    the formatter's staff classification ``pvjson.staff_roles`` and the lyrics): vocal-staff notes, lyric
    syllables, orchestra notes besides the vocal lines (what ``accompaniment`` renders)."""
    p = entry.get("parts") or {}
    notes = p.get("notes") or {}
    vocal = int(p.get("vocal") or 0)
    return {"vocal_notes": vocal, "lyric_syllables": int((p.get("lyrics") or {}).get("syllables") or 0),
            "orchestra_notes": max(0, int(notes.get("orchestra") or 0) - vocal)}


def route_state(song: Song) -> dict:
    return {"song": song.desc()}


def route_questions(song: Song) -> Dict[str, dict]:
    """A Noul "is it sung" and a Choice of the kind of music (asked twice, the options reversed)."""
    def kind(order):
        return {"type": "choice", "instructions": "What kind of music is `song`?",
                "criteria": {k: KINDS[k] for k in order}}
    keys = list(KINDS)
    return {
        "sung": {"type": "noul",
                 "instructions": {"question": "Is `song` sung: does its original or best-known recording have a "
                                              "singer, a lead vocal or a choir?",
                                  "about": "The score may be a piano arrangement without the words; answer about the "
                                           "piece itself, as it was written and first recorded."},
                 "criteria": {"true": "Sung: a song with vocals (pop, rock, soul, a song from a musical or a film, a "
                                      "Bond theme song), an aria or art song, choral music, a wordless vocal line.",
                              "false": "Instrumental: a piano piece, a concerto, symphony or ballet, a film score cue "
                                       "without singing, an instrumental pop or jazz tune."}},
        "kind": kind(keys),
        "kind_reversed": kind(list(reversed(keys))),
    }


def decide_route(facts: dict, answers: Optional[Dict[str, dict]], s: Settings) -> dict:
    """The route from the score's facts first, then the judgment (the rule, in order):

    1. ``classical`` (P(kind = classical), both option orders averaged, at least ``classical_min``) -> ``generated``,
       sung or not (an aria's companion is the score's orchestra too);
    2. a vocal staff or lyrics in the score (``min_score_vocal`` notes / syllables or more) -> ``vocals``;
    3. Noul "sung" at least ``sung_min`` -> ``vocals``; at most ``unsung_max`` -> ``generated``;
    4. otherwise ``review``.
    Without answers (not judged yet) only rule 2 applies, else ``review``."""
    score_sung = max(facts.get("vocal_notes", 0), facts.get("lyric_syllables", 0)) >= s.min_score_vocal
    out: Dict[str, Any] = {"facts": facts, "score_sung": score_sung}
    if answers:
        pa = answers["kind"].get("probabilities", {})
        pb = answers["kind_reversed"].get("probabilities", {})
        pk = {k: round(0.5 * (float(pa.get(k, 0)) + float(pb.get(k, 0))), 3) for k in KINDS}
        p_sung = round(float(answers["sung"]["noul"]), 3)
        out.update(p_sung=p_sung, kind=max(pk, key=pk.get), p_kind=pk)
        if pk["classical"] >= s.classical_min:
            out.update(route=ROUTE_GENERATED, why=f"classical (p {pk['classical']:.2f})"
                                                  + (", sung" if score_sung or p_sung >= s.sung_min else ""))
        elif score_sung:
            out.update(route=ROUTE_VOCALS, why=_score_why(facts))
        elif p_sung >= s.sung_min:
            out.update(route=ROUTE_VOCALS, why=f"sung (p_sung {p_sung:.2f}; no vocal line in the score)")
        elif p_sung <= s.unsung_max:
            out.update(route=ROUTE_GENERATED, why=f"instrumental (p_sung {p_sung:.2f})")
        else:
            out.update(route=ROUTE_REVIEW, why=f"unclear whether it is sung (p {p_sung:.2f})")
    elif score_sung:
        out.update(route=ROUTE_VOCALS, why=_score_why(facts) + " (not judged yet)")
    else:
        out.update(route=ROUTE_REVIEW, why="not judged yet")
    return out


def _score_why(facts: dict) -> str:
    parts = []
    if facts.get("vocal_notes"):
        parts.append(f"{facts['vocal_notes']} vocal-staff notes")
    if facts.get("lyric_syllables"):
        parts.append(f"{facts['lyric_syllables']} lyric syllables")
    return "sung: the score has " + " and ".join(parts)


def effective_route(entry: Optional[dict]) -> str:
    """Your ``decision`` (``vocals`` / ``generated``), else the rule's route; ``review`` when nothing is known."""
    if not entry:
        return ROUTE_REVIEW
    d = entry.get("decision")
    if d in (ROUTE_VOCALS, ROUTE_GENERATED):
        return d
    return entry.get("route") or ROUTE_REVIEW


def route_entry(song: Song, facts: dict, verdict: dict, model: str, qkey: str, raw: Optional[dict],
                when: Optional[str] = None) -> dict:
    e = {"song": song.output, "title": song.title, "artist": song.artist, "route": verdict["route"],
         "why": verdict["why"], "facts": facts}
    for k in ("p_sung", "kind", "p_kind"):
        if k in verdict:
            e[k] = verdict[k]
    if raw is not None:
        e["judged"] = {"model": model, "time": when or _now(), "key": qkey, "answers": raw}
    return e


def raw_route_answers(answers: Dict[str, dict]) -> dict:
    return {"sung": round(float(answers["sung"]["noul"]), 4),
            "kind": {k: round(float(v), 4) for k, v in answers["kind"].get("probabilities", {}).items()},
            "kind_reversed": {k: round(float(v), 4) for k, v in answers["kind_reversed"].get("probabilities",
                                                                                              {}).items()}}


def route_answers_from_raw(raw: dict) -> Dict[str, dict]:
    return {"sung": {"noul": raw["sung"]}, "kind": {"probabilities": raw["kind"]},
            "kind_reversed": {"probabilities": raw["kind_reversed"]}}


# ------------------------------------------------------------------------------
# the links file (recordings.json)
# ------------------------------------------------------------------------------

LINKS_COMMENT = [
    "Note Waterfall's companion audio from recordings you own (`python3 -m pianovision recordings`,",
    "pianovision/recordings.py). Keys are score paths relative to the scores folder; recording paths are relative",
    "to [recordings] music_dir. Written by `recordings match`; yours is every 'decision', which is never overwritten.",
    "",
    "routes: every song of the library, what its companion is:",
    "  route         'generated' (the score's orchestra rendered by `pianovision accompaniment`, piano muted:",
    "                classical and instrumental music; never a recording), 'vocals' (the singing of a recording",
    "                you own, isolated and retimed: sung pop and film songs), 'review' (undecided: nothing is made)",
    "  why, facts    the rule that decided and the score's facts (vocal-staff notes, lyric syllables, orchestra notes)",
    "  p_sung, kind  the TypeSafe Noul 'is it sung' and the Choice of the kind of music (p_kind: both orders averaged)",
    "  judged        model, time, the question's hash and the raw answers (thresholds can change without a request)",
    "  decision      yours: 'vocals' or 'generated' (`recordings route SONG vocals|generated|auto`)",
    "",
    "links: songs with candidate recordings, the TypeSafe pick among them:",
    "  status        'linked' (used), 'review' (needs your decision; not used until you confirm), 'none'",
    "  recording     the pick (or the suggestion, for review); variants: 'vocals' (a vocals stem in the folder, or",
    "                separated from the full mix), 'full', 'no_piano'",
    "  variant       the file that is retimed and delivered: 'vocals' for a sung song; none for a generated one (its",
    "                recording is aligned for the accuracy summary only)",
    "  confidence    the TypeSafe Choice confidence (both candidate orders averaged); p_none; p_same: the Noul",
    "                'same composition' of the recording; consistent: both orders picked the same",
    "  alternatives  other candidates that look like the same composition, or carried probability",
    "  judged        model, time, the question's hash, the candidates and the raw answers",
    "  decision      yours (`recordings confirm|reject`): {} confirms the pick; {\"recording\": \"<path>\"} links",
    "                another recording; {\"recording\": null} rejects; \"variant\": \"vocals\", \"full\" or",
    "                \"no_piano\"; \"timing\": \"recording\" (the score follows the recording, which plays",
    "                unstretched: `recordings tempo`; the default for sung songs) or \"score\" (the recording is",
    "                stretched onto the score: `recordings retime`)",
]
FORMAT = 2


def load_links(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {"_comment": LINKS_COMMENT, "format": FORMAT, "routes": {}, "links": {}}
    if not isinstance(d.get("links"), dict):
        raise RecordingsError(f"{path}: no 'links' object")
    d.setdefault("routes", {})
    return d


def save_links(path: str, d: dict) -> None:
    ordered = {"_comment": LINKS_COMMENT, "format": FORMAT,
               "routes": {k: d.get("routes", {})[k] for k in sorted(d.get("routes", {}))},
               "links": {k: d["links"][k] for k in sorted(d["links"])}}
    _write_json(path, ordered, indent=2)


def effective_link(entry: dict) -> Optional[Tuple[str, str, str]]:
    """(recording, variant, how) the pipeline uses for a links entry: your decision, else an automatic link; None
    for review, none, or rejected."""
    d = entry.get("decision")
    if isinstance(d, dict):
        rec = d.get("recording", entry.get("recording"))
        if not rec:
            return None
        variant = d.get("variant")
        if not variant:
            variant = entry.get("variant") if rec == entry.get("recording") else ""
        return rec, variant or "", "confirmed"
    if entry.get("status") == STATUS_LINKED and entry.get("recording"):
        return entry["recording"], entry.get("variant") or "", "auto"
    return None


def effective_timing(entry: Optional[dict], s: "Settings") -> str:
    """Who leads for a sung song: "recording" (the score's tempo map follows the recording, which plays as it is:
    ``recordings tempo``) or "score" (the recording is stretched onto the score: ``recordings retime``); your
    ``decision`` ``"timing"`` in the links file, else ``[recordings] sung_timing``."""
    d = (entry or {}).get("decision")
    t = d.get("timing") if isinstance(d, dict) else None
    return t if t in ("recording", "score") else (s.sung_timing if s.sung_timing in ("recording", "score")
                                                    else "recording")


def raw_answers(answers: Dict[str, dict], n: int) -> dict:
    """What :func:`decide` reads, kept in the links file so a change of policy or thresholds needs no new request."""
    def r(x):
        return round(float(x), 4)
    return {"pick": {k: r(v) for k, v in answers["pick"].get("probabilities", {}).items()},
            "pick_choice": answers["pick"].get("choice"),
            "pick_reversed": {k: r(v) for k, v in answers["pick_reversed"].get("probabilities", {}).items()},
            "pick_reversed_choice": answers["pick_reversed"].get("choice"),
            "same": [r(answers[f"same_{i}"]["noul"]) for i in range(n)]}


def answers_from_raw(raw: dict) -> Dict[str, dict]:
    out = {"pick": {"probabilities": raw["pick"], "choice": raw.get("pick_choice")},
           "pick_reversed": {"probabilities": raw["pick_reversed"], "choice": raw.get("pick_reversed_choice")}}
    for i, v in enumerate(raw["same"]):
        out[f"same_{i}"] = {"noul": v}
    return out


def link_entry(song: Song, cands: Sequence[Tuple[Recording, float]], verdict: dict, model: str, qkey: str,
               recs: Dict[str, Recording], raw: Optional[dict] = None, when: Optional[str] = None) -> dict:
    rec = recs.get(verdict.get("recording") or "")
    e: Dict[str, Any] = {"song": song.output, "status": verdict["status"],
                         "recording": verdict.get("recording")}
    if rec is not None:
        e["variants"] = rec.variants()
        e["variant"] = None                      # set from the song's route (match)
    for k in ("confidence", "p_same", "p_none", "consistent", "why", "alternatives"):
        e[k] = verdict.get(k)
    e["judged"] = {"model": model, "time": when or _now(), "key": qkey,
                   "candidates": [{"recording": r.key, "score": round(sc, 3)} for r, sc in cands]}
    if raw is not None:
        e["judged"]["answers"] = raw
    return e


# ------------------------------------------------------------------------------
# match
# ------------------------------------------------------------------------------

Judge = Callable[[dict, Dict[str, dict], str], dict]


def api_judge(s: Settings) -> Judge:
    def judge(state: dict, questions: Dict[str, dict], model: str) -> dict:
        return system_one(state, questions, model, timeout=s.api_timeout)
    return judge


def match(lib, s: Settings, only: Sequence[str] = (), refresh: bool = False, dry_run: bool = False,
          judge: Optional[Judge] = None, log: Log = print) -> dict:
    """Every song's route (the score's facts and one TypeSafe request: sung? what kind of music?), then the candidate
    recordings per song and their judgment; writes the links file (keeps every ``decision``).  A question asked
    before with the same text is not asked again: its stored answers decide again."""
    files, videos = scan_music(s, log)
    recs = group_recordings(files)
    by_key = {r.key: r for r in recs}
    songs = library_songs({"entries": lib.manifest.entries})
    if only:
        songs = [x for x in songs if _matches(x, only)]
    props = propose_candidates(songs, recs, s.candidates, s.min_candidate_score)
    links = load_links(s.links)
    L, RT = links["links"], links["routes"]
    jobs: List[tuple] = []
    stats = {"songs": len(songs), "no_candidates": 0, "asked": 0, "kept": 0, "routes_asked": 0, "routes_kept": 0}
    for song in songs:
        facts = score_facts(lib.manifest.entries.get(song.rel) or {})
        state, qs = route_state(song), route_questions(song)
        qkey = question_key(s.model, state, qs)
        old = RT.get(song.rel) or {}
        judged = old.get("judged") or {}
        if not refresh and judged.get("key") == qkey and judged.get("answers"):
            verdict = decide_route(facts, route_answers_from_raw(judged["answers"]), s)
            RT[song.rel] = _keep_decision(route_entry(song, facts, verdict, judged.get("model", s.model), qkey,
                                                      judged["answers"], judged.get("time")), old)
            stats["routes_kept"] += 1
        else:
            jobs.append(("route", song, facts, state, qs, qkey))
    for song in songs:
        cands = props.get(song.rel) or []
        if not cands:
            stats["no_candidates"] += 1
            old = L.get(song.rel)
            if old is not None and not old.get("decision"):
                del L[song.rel]
            continue
        rs = [r for r, _sc in cands]
        state, qs = match_state(song, rs), match_questions(song, rs)
        qkey = question_key(s.model, state, qs)
        old = L.get(song.rel)
        judged = (old or {}).get("judged") or {}
        if old and not refresh and judged.get("key") == qkey and judged.get("answers"):
            # the same question: its answers decide again (the policy or thresholds may have changed)
            verdict = decide(answers_from_raw(judged["answers"]), rs, s)
            L[song.rel] = _keep_decision(link_entry(song, cands, verdict, judged.get("model", s.model), qkey, by_key,
                                                    judged["answers"], judged.get("time")), old)
            stats["kept"] += 1
            continue
        jobs.append(("link", song, cands, state, qs, qkey))
    log(f"{len(files)} audio files, {len(recs)} recordings ({len(videos)} video files not offered); "
        f"{len(songs)} songs: routes {stats['routes_kept']} judged before, "
        f"{sum(1 for j in jobs if j[0] == 'route')} to judge; recordings: {stats['no_candidates']} songs without "
        f"candidates, {stats['kept']} judged before, {sum(1 for j in jobs if j[0] == 'link')} to judge")
    if dry_run:
        for job in jobs:
            if job[0] == "link":
                log(f"  {job[1].rel}: " + ", ".join(f"{r.key} ({sc:.2f})" for r, sc in job[2]))
        return stats
    judge = judge or api_judge(s)

    def ask(job):
        return job, judge(job[3], job[4], s.model)

    errors = []
    with concurrent.futures.ThreadPoolExecutor(max(1, s.workers)) as ex:
        futs = [ex.submit(ask, j) for j in jobs]
        for fu in concurrent.futures.as_completed(futs):
            try:
                job, res = fu.result()
            except RecordingsError as e:
                errors.append(str(e))
                continue
            kind, song, extra, _state, _qs, qkey = job
            if kind == "route":
                verdict = decide_route(extra, res["answers"], s)
                RT[song.rel] = _keep_decision(route_entry(song, extra, verdict, res.get("model", s.model), qkey,
                                                          raw_route_answers(res["answers"])), RT.get(song.rel) or {})
                stats["routes_asked"] += 1
                continue
            verdict = decide(res["answers"], [r for r, _ in extra], s)
            L[song.rel] = _keep_decision(link_entry(song, extra, verdict, res.get("model", s.model), qkey, by_key,
                                                    raw_answers(res["answers"], len(extra))), L.get(song.rel) or {})
            stats["asked"] += 1
            log(f"  {verdict['status']:7s} {song.output}: {verdict.get('recording')} "
                f"(confidence {verdict['confidence']:.2f}, p_none {verdict['p_none']:.2f})")
    # what a link delivers depends on its song's route
    for rel, e in L.items():
        rec = by_key.get(e.get("recording") or "")
        if rec is not None:
            e["variants"] = rec.variants()
            e["variant"] = rec.default_variant(effective_route(RT.get(rel)))
    save_links(s.links, links)
    if errors:
        log(f"{len(errors)} judgments failed: {errors[0]}")
    stats["errors"] = len(errors)
    return stats


def _keep_decision(new: dict, old: dict) -> dict:
    if "decision" in old:
        new["decision"] = old["decision"]
    return new


def _matches(song: Song, globs: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(song.rel, g) or fnmatch.fnmatch(song.output, g) or fnmatch.fnmatch(song.stem, g)
               for g in globs)


# ------------------------------------------------------------------------------
# the song's reference: measures as played and the notes, on the song's clock
# ------------------------------------------------------------------------------

def song_notes(song_doc: dict, parts_doc: Optional[dict]) -> List[list]:
    """[start, duration, pitch, velocity, part] of what sounds in the song: the parts file's original piano part
    ("p") and orchestra ("o"; vocal lines "v"); without a parts file, the song's hands."""
    out: List[list] = []
    if parts_doc and isinstance(parts_doc.get("parts"), dict):
        for part, code in (("original", "p"), ("orchestra", "o")):
            for hand in ("right", "left"):
                for n in (parts_doc["parts"].get(part) or {}).get(hand, []):
                    out.append([round(float(n["start"]), 5), round(float(n["duration"]), 5), int(n["note"]),
                                round(float(n.get("velocity", 0.7)), 4), "v" if n.get("vocal") else code])
    else:
        for hand in ("right", "left"):
            for n in (song_doc.get("tracksV2") or {}).get(hand, []):
                out.append([round(float(n["start"]), 5), round(float(n["duration"]), 5), int(n["note"]),
                            round(float(n.get("velocity", 0.7)), 4), "p"])
    out.sort()
    return out


def score_measures(lib, rel: str) -> List[dict]:
    """The score's measures as played (repeats and jumps unrolled) with their numbers, passes, start / end and beat
    times in seconds, rendered as the library renders the score (the manifest entry's knobs)."""
    from .arrangement import Timeline
    from .mscx import read_score
    from .pvjson import TempoIndex, extract_tempo_events
    from .render import Compat, Renderer
    e = lib.manifest.entries.get(rel) or {}
    score = read_score(os.path.join(lib.cfg.scores, rel))
    r = Renderer(score, Compat.from_dict(lib._verify_compat(e)))
    midi = r.render()
    tl = Timeline.from_render(r, midi)
    sec = TempoIndex(extract_tempo_events(midi), midi.ticks_per_beat)
    tpb = midi.ticks_per_beat
    out = []
    for m in tl.measures:
        num, den = m.timesig
        beat = max(1, int(round(tpb * 4 / max(den, 1))))
        if den == 8 and num % 3 == 0 and num > 3:            # compound time: the dotted quarter is the beat
            beat *= 3
        beats = [round(sec(tl.tick(m.utick + k)), 5) for k in range(0, max(m.length, 1), beat)]
        actual = max(1, int(round(m.length / (tpb * 4 / max(den, 1)))))       # a measure's actual length (5/4 ...)
        out.append({"index": m.index, "number": m.number, "pass": m.passno, "start": round(sec(m.tick), 5),
                    "end": round(sec(m.end_tick), 5), "timesig": [actual, den], "beats": beats})
    return out


def write_reference(lib, song: Song, work: str) -> Tuple[str, dict]:
    """``work/ref.json`` for the song (rewritten when the song or its parts file changed)."""
    out = lib.cfg.output
    song_path = os.path.join(out, song.output)
    parts_path = os.path.join(out, "NoteWaterfall", song.stem + ".parts.json")
    h = hashlib.sha256()
    for p in (song_path, parts_path):
        if os.path.isfile(p):
            with open(p, "rb") as f:
                h.update(f.read())
    h.update(f"reference {REF_VERSION}".encode())
    sig = h.hexdigest()[:16]
    ref_path = os.path.join(work, "ref.json")
    try:
        with open(ref_path, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("sig") == sig:
            return ref_path, old
    except (OSError, ValueError):
        pass
    with open(song_path, encoding="utf-8") as f:
        song_doc = json.load(f)
    parts_doc = None
    if os.path.isfile(parts_path):
        with open(parts_path, encoding="utf-8") as f:
            parts_doc = json.load(f)
    notes = song_notes(song_doc, parts_doc)
    # the song ends where PianoVision ends it (its last note); a score's empty measures after that are not played
    end = max([float(song_doc.get("song_length", 0.0))] + [n[0] + n[1] for n in notes])
    measures = [m for m in score_measures(lib, song.rel) if m["start"] < end - 1e-6]
    ref = {"format": 1, "sig": sig, "song": song.output, "score": song.rel, "song_end": round(end, 4),
           "measures": measures, "notes": notes}
    os.makedirs(work, exist_ok=True)
    _write_json(ref_path, ref)
    return ref_path, ref


# ------------------------------------------------------------------------------
# align, retime, verify, encode
# ------------------------------------------------------------------------------

def tool_version(script: str) -> str:
    """The analysis' version: the script's ``VERSION`` and recmap's (bumped when they change results), part of the
    keys that decide whether a song is aligned or retimed again."""
    with open(script, encoding="utf-8") as f:
        m = re.search(r"^VERSION = (\d+)", f.read(), re.M)
    return f"align{m.group(1) if m else '?'}-recmap{RM.VERSION}"


def _file_sig(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}"


def _run_tool(s: Settings, args: List[str], log: Log) -> None:
    py = os.path.expanduser(s.python)
    if not os.path.isfile(py):
        raise RecordingsError(f"[recordings] python not found: {py} (needs numpy, scipy, soundfile, librosa)")
    r = subprocess.run([py, s.script] + args, capture_output=True, text=True, timeout=s.timeout)
    if r.returncode != 0:
        raise RecordingsError(f"{os.path.basename(s.script)} {args[0]} failed:\n{(r.stderr or r.stdout)[-2000:]}")


@dataclass
class Job:
    song: Song
    entry: dict
    recording: str               # rel path (the recording's key)
    variant: str                 # "vocals", "full", "no_piano"; "" for a diagnostic
    how: str                     # "auto" / "confirmed"
    route: str = ROUTE_VOCALS

    @property
    def kind(self) -> str:
        """``deliver`` (sung: the companion is this recording's vocals) or ``diagnostic`` (generated: the recording
        is aligned for the accuracy summary only, nothing is delivered)."""
        return "deliver" if self.route == ROUTE_VOCALS else "diagnostic"


def jobs_for(lib, s: Settings, links: dict, only: Sequence[str] = ()) -> Tuple[List[Job], List[str]]:
    songs = {x.rel: x for x in library_songs({"entries": lib.manifest.entries})}
    out: List[Job] = []
    skipped: List[str] = []
    for rel, e in sorted(links["links"].items()):
        song = songs.get(rel)
        if song is None:
            skipped.append(f"{rel}: not in the library any more")
            continue
        if only and not _matches(song, only):
            continue
        eff = effective_link(e)
        if eff is None:
            if e.get("status") == STATUS_REVIEW and not e.get("decision"):
                skipped.append(f"{song.output}: the recording needs your confirmation ({e.get('why')})")
            continue
        rec, variant, how = eff
        route = effective_route(links.get("routes", {}).get(rel))
        if route == ROUTE_REVIEW:
            why = (links["routes"].get(rel) or {}).get("why")
            skipped.append(f"{song.output}: its route needs your decision ({why})")
            continue
        if route == ROUTE_VOCALS and not variant:
            variant = "vocals"
        out.append(Job(song, e, rec, variant if route == ROUTE_VOCALS else "", how, route))
    return out, skipped


def recording_files(s: Settings, job: Job, recs: Dict[str, Recording]) -> Tuple[str, Optional[str], str]:
    """(source to align, file to deliver, variant) as absolute paths; the file to deliver is None when it is a vocals
    stem still to be separated from the source (or for a diagnostic)."""
    root = s.path("music_dir")
    r = recs.get(job.recording)
    if r is None:
        p = os.path.join(root, job.recording)
        if not os.path.isfile(p):
            raise RecordingsError(f"{job.recording}: not in the music folder")
        r = Recording(job.recording, {"full": job.recording})
    src = os.path.join(root, r.source())
    if job.kind == "diagnostic":
        return src, None, ""
    variants = r.variants()
    variant = job.variant or "vocals"
    if variant not in variants:
        raise RecordingsError(f"{job.recording}: no '{job.variant}' variant (it has {', '.join(variants) or 'none'})")
    target = variants[variant]
    return src, (None if target == SEPARATED else os.path.join(root, target)), variant


def _load_cache(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def process(lib, s: Settings, job: Job, recs: Dict[str, Recording], state: dict, force: bool, retime: bool,
            log: Log) -> dict:
    """Align a song's recording; with ``retime``: for a ``vocals`` route separate the vocals (cached), check them,
    retime, encode and verify; for a ``generated`` route retime the full mix in the work folder only to verify it
    (the accuracy summary), nothing is delivered.  The result is also stored in ``state``."""
    from . import separation as SEP
    song = job.song
    work = os.path.join(s.path("work_dir"), song.stem)
    os.makedirs(work, exist_ok=True)
    src, deliver, variant = recording_files(s, job, recs)
    ref_path, ref = write_reference(lib, song, work)
    tools = tool_version(s.script)
    cache_path = os.path.join(work, "cache.json")
    prev = _load_cache(cache_path) or (state.get(song.output) or {})
    res: Dict[str, Any] = {"song": song.output, "score": song.rel, "recording": job.recording, "variant": variant,
                           "how": job.how, "route": job.route, "kind": job.kind,
                           "source": os.path.relpath(src, s.path("music_dir"))}
    akey = hashlib.sha256(f"{ref['sig']}|{src}|{_file_sig(src)}|{tools}".encode()).hexdigest()[:16]
    align_path = os.path.join(work, "align.json")
    if force or prev.get("align_key") != akey or not os.path.isfile(align_path):
        log(f"  aligning {song.output} with {res['source']} ...")
        t0 = time.monotonic()
        _run_tool(s, ["align", "--ref", ref_path, "--audio", src, "--out", align_path, "--tail", str(s.tail)], log)
        res["align_seconds"] = round(time.monotonic() - t0, 1)
        prev = {}
    else:
        res["align_seconds"] = prev.get("align_seconds")
    res["align_key"] = akey
    cache = {k: prev[k] for k in prev if k not in ("align_key", "align_seconds")}
    cache.update(align_key=akey, align_seconds=res["align_seconds"])
    _write_json(cache_path, cache)
    with open(align_path, encoding="utf-8") as f:
        al = json.load(f)
    res["alignment"] = alignment_summary(al, ref)
    tm = RM.TimeMap.from_dict(al["map"])
    if not tm.pieces:
        res["checks"] = {"ok": False, "problems": ["no part of the song matched the recording"]}
        state[song.output] = res
        return res
    lat = os.path.join(s.path("work_dir"), "rubberband-latency.json")
    key = str(al["key"]["offset"])
    cents = float(al.get("tuning_cents") or 0.0)
    retune = cents if s.retune_cents and abs(cents) >= s.retune_cents else 0.0

    def verify_full(tag: str) -> str:
        """The full mix retimed in the work folder (not kept) and verified against the notes."""
        fw = os.path.join(work, f"retimed-{tag}.wav")
        out = os.path.join(work, f"verify-{tag}.json")
        _run_tool(s, ["retime", "--ref", ref_path, "--align", align_path, "--audio", src, "--out", fw,
                      "--latency-cache", lat, "--retune-cents", f"{retune:.2f}"], log)
        _run_tool(s, ["verify", "--ref", ref_path, "--audio", fw, "--key", key, "--out", out], log)
        for f in (fw, fw + ".json"):
            if os.path.exists(f):
                os.unlink(f)
        return out

    if job.kind == "diagnostic":
        res["checks"] = {"ok": False, "problems": [f"route '{job.route}': the companion is generated from the score; "
                                                   f"this recording is aligned for the accuracy summary only"]}
        vpath = prev.get("verify_full_path") if prev.get("diag_key") == akey else None
        if retime and (force or not vpath or not os.path.isfile(vpath)):
            log("  verifying the alignment (full mix retimed in the work folder) ...")
            vpath = verify_full("full")
            cache.update(diag_key=akey, verify_full_path=vpath)
            _write_json(cache_path, cache)
        if vpath and os.path.isfile(vpath):
            with open(vpath, encoding="utf-8") as f:
                vf = json.load(f)
            res["verify_full"] = verify_summary(vf)
            res["accuracy"] = accuracy_summary(al, vf, ref, res)
            _write_accuracy(s, song, res["accuracy"])
        state[song.output] = res
        return res

    if effective_timing(job.entry, s) == "recording":
        return _recording_led(lib, s, job, res, al, src, state)

    # deliver: the vocals (or a full mix / stem you chose) on the song's timeline
    separated = deliver is None
    if separated:
        cached = SEP.cached_vocals(s.stems_dir, src, s.separator_model)
        if cached:
            deliver = cached
        elif retime:
            try:
                deliver = SEP.vocals_stem(src, s.stems_dir, s.separator_model, s.separator_python,
                                          s.separator_script, log=log)
            except SEP.SeparationUnavailable as e:
                res["checks"] = {"ok": False, "problems": [f"the vocals are not separated yet: {e}"]}
                res["pending"] = "separation"
                state[song.output] = res
                return res
        else:
            res["checks"] = {"ok": False, "problems": ["the vocals are not separated yet (retime separates them)"]}
            res["pending"] = "separation"
            state[song.output] = res
            return res
    res["delivered"] = deliver if separated else os.path.relpath(deliver, s.path("music_dir"))
    if variant == "vocals":
        act = prev.get("vocal_activity") if prev.get("vocal_key") == _file_sig(deliver) else None
        if act is None:
            act = SEP.vocal_activity(deliver, src, work, start=tm.pieces[0].rec0, end=tm.pieces[-1].rec1)
            cache.update(vocal_key=_file_sig(deliver), vocal_activity=act)
            _write_json(cache_path, cache)
        res["vocal_activity"] = act
        if act["activity"] < s.min_vocal_activity:
            res["checks"] = {"ok": False, "problems": [
                f"the recording has almost no singing where it matches the song ({act['activity']:.0%} of it, "
                f"vocals {act['vocals_to_mix_db']} dB under the mix): not used"]}
            state[song.output] = res
            return res
    if not retime:
        if prev.get("retime_key"):
            for k in CACHED:
                res[k] = prev.get(k)
            _finish_deliver(s, res, al, ref, song)
        state[song.output] = res
        return res
    out_dir = s.path("out_dir")
    os.makedirs(out_dir, exist_ok=True)
    mp3 = os.path.join(out_dir, song.stem + ".mp3")
    rkey = hashlib.sha256(f"{akey}|{deliver}|{_file_sig(deliver)}|{variant}|{retune}|{s.bitrate}|{s.lufs}|"
                          f"{s.true_peak}".encode()).hexdigest()[:16]
    if force or prev.get("retime_key") != rkey or not os.path.isfile(mp3) or prev.get("mp3_sha256") != _sha_file(mp3):
        log(f"  retiming the {variant} ({os.path.basename(deliver)}) ...")
        t0 = time.monotonic()
        wav = os.path.join(work, "retimed.wav")
        args = ["retime", "--ref", ref_path, "--align", align_path, "--audio", deliver, "--out", wav,
                "--latency-cache", lat, "--retune-cents", f"{retune:.2f}"]
        if os.path.abspath(deliver) != os.path.abspath(src):
            args += ["--source", src] + (["--assume-aligned"] if separated else [])
        _run_tool(s, args, log)
        encode(s, wav, mp3, song, job)
        res["retime_seconds"] = round(time.monotonic() - t0, 1)
        with open(wav + ".json", encoding="utf-8") as f:
            res["retime"] = json.load(f)
        os.unlink(wav)
        os.unlink(wav + ".json")
        ver = os.path.join(work, "verify.json")
        _run_tool(s, ["verify", "--ref", ref_path, "--audio", mp3, "--key", key, "--out", ver], log)
        # a stem lacks the piano the notes have: the full mix retimed the same way is what is checked
        full_ver = verify_full("full") if os.path.abspath(deliver) != os.path.abspath(src) else ver
        vs = None
        if variant == "vocals":
            # the singing against the score's vocal line, inside the song spans the recording plays
            spans = os.path.join(work, "active.json")
            _write_json(spans, [[p.song0, p.song1] for p in tm.pieces])
            vs = os.path.join(work, "vocals.json")
            _run_tool(s, ["vocals", "--ref", ref_path, "--audio", mp3, "--active", spans, "--out", vs], log)
        res.update(retime_key=rkey, mp3=mp3, mp3_sha256=_sha_file(mp3), verify_path=ver, verify_full_path=full_ver,
                   vocal_sync_path=vs)
        cache.update({k: res.get(k) for k in CACHED})
        _write_json(cache_path, cache)
    else:
        for k in CACHED:
            res[k] = prev.get(k)
    _finish_deliver(s, res, al, ref, song)
    state[song.output] = res
    return res


def tempo_state(s: Settings, song: Song) -> dict:
    """What ``recordings tempo`` last did for the song (``<tempo_dir>/<song>/tempo.json``), or {}."""
    return _load_cache(os.path.join(s.path("tempo_dir"), song.stem, "tempo.json"))


def _recording_led(lib, s: Settings, job: Job, res: dict, al: dict, src: str, state: dict) -> dict:
    """A sung song whose recording leads: nothing is stretched; the companion is the original recording at the
    offset ``recordings tempo`` found, usable once the score carries the recording's tempo map (its verified copy
    applied) and the key is the same."""
    from .rectempo import sha256_file
    song = job.song
    res["timing"] = "recording"
    res["audio"] = src
    st = tempo_state(s, song)
    score = os.path.join(lib.cfg.scores, song.rel)
    problems = []
    if not st:
        problems.append("the score does not follow the recording yet: `recordings tempo SONG`, then apply its copy "
                        "(`recordings tempo SONG --in-place --backup DIR`)")
    else:
        cur = sha256_file(score) if os.path.isfile(score) else None
        applied = (st.get("applied") or {}).get("sha256")
        if cur != applied:
            problems.append("the verified copy is not applied yet (`recordings tempo SONG --in-place --backup DIR`)"
                            if cur == st.get("score_sha256") else
                            "the score changed since `recordings tempo` made its copy: run it again")
        if not st.get("ok"):
            problems.append("the copy failed its checks: " + ", ".join(c["name"] for c in st.get("checks", [])
                                                                       if not c["ok"]))
        if os.path.abspath(st.get("recording", "")) != os.path.abspath(src):
            problems.append(f"the tempo map was made with another recording ({st.get('recording')})")
        res["offset"] = st.get("offset")
        res["tempo"] = {k: st.get(k) for k in ("copy", "offset", "repeats", "verify", "plan")}
    if al["key"]["offset"]:
        problems.append(f"the recording is {al['key']['offset']:+d} semitones from the score")
    res["checks"] = {"ok": not problems, "problems": problems}
    state[song.output] = res
    return res


def _finish_deliver(s: Settings, res: dict, al: dict, ref: dict, song: Song) -> None:
    with open(res["verify_path"], encoding="utf-8") as f:
        v = json.load(f)
    with open(res["verify_full_path"], encoding="utf-8") as f:
        vf = json.load(f)
    res["verify"] = verify_summary(v)
    res["verify_full"] = verify_summary(vf) if res["verify_full_path"] != res["verify_path"] else None
    if res.get("vocal_sync_path") and os.path.isfile(res["vocal_sync_path"]):
        with open(res["vocal_sync_path"], encoding="utf-8") as f:
            res["vocal_sync"] = json.load(f)
    res["accuracy"] = accuracy_summary(al, vf, ref, res)
    res["checks"] = checks(s, res)
    _write_accuracy(s, song, res["accuracy"])


def _write_accuracy(s: Settings, song: Song, acc: dict) -> None:
    _write_json(os.path.join(s.path("out_dir"), "accuracy", song.stem + ".json"), acc)


CACHED = ("retime_seconds", "retime", "retime_key", "mp3", "mp3_sha256", "verify_path", "verify_full_path",
          "vocal_sync_path")


def encode(s: Settings, wav: str, mp3: str, song: Song, job: Job) -> None:
    from .accompaniment import encode_mp3, loudness
    lv = loudness(wav)
    if not all(math.isfinite(lv[k]) for k in ("lufs", "true_peak")):
        raise RecordingsError(f"the retimed audio is silent ({lv})")
    gain = min(s.lufs - lv["lufs"], s.true_peak - lv["true_peak"])
    tmp = mp3 + ".tmp.mp3"
    encode_mp3(wav, tmp, gain, s.bitrate, title=song.output[:-5], artist="",
               comment=f"retimed from {job.recording} by pianovision recordings")
    os.replace(tmp, mp3)


def _sha_file(path: str) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def alignment_summary(al: dict, ref: dict) -> dict:
    tm = RM.TimeMap.from_dict(al["map"])
    song_end = float(al["song_end"])
    matched = sum(p.song1 - p.song0 for p in tm.pieces)
    weak = sum(g["song"][1] - g["song"][0] for g in al["gaps"] if g["kind"] == RM.WEAK)
    return {"key_offset": al["key"]["offset"], "key_margin": al["key"].get("margin"),
            "tuning_cents": al.get("tuning_cents"), "pieces": len(tm.pieces),
            "piece_quality": [round(p.quality, 3) for p in tm.pieces],
            "coverage": round(matched / song_end, 3) if song_end else 0.0,
            "coverage_with_weak": round((matched + weak) / song_end, 3) if song_end else 0.0,
            "gaps": al["gaps"], "tempo": al["tempo"], "path_similarity": al.get("path_similarity"),
            "refine": al.get("refine"), "duration": al.get("duration"), "song_end": song_end}


def verify_summary(v: dict) -> dict:
    ms = [m for m in v["measures"] if m.get("chroma") is not None]
    low = [m for m in ms if m["chroma"] < 0.2 or (m.get("onset_corr") or 0) >= 0.3 and abs(m.get("lag_ms") or 0) > 80]
    idx = [k for k, m in enumerate(v["measures"]) if m in low]
    return {"lag": v["lag"], "onset_lag": v.get("onset_lag"), "chroma_mean": v.get("chroma_mean"),
            "measures": len(v["measures"]),
            "measures_scored": len(ms), "low": RM.ranges_text([v["measures"][k] for k in idx], idx),
            "low_count": len(low)}


def accuracy_summary(al: dict, vf: dict, ref: dict, res: dict) -> dict:
    """What the recording says about the score (for correcting it later; nothing is changed here)."""
    ms = ref["measures"]
    gaps = []
    for g in al["gaps"]:
        d = {"kind": g["kind"], "edge": g.get("edge", ""), "song_s": g["song"], "recording_s": g["recording"],
             "measures": g.get("measures") or []}
        d["meaning"] = {RM.RECORDING_EXTRA: "the recording has material the score does not",
                        RM.SONG_MISSING: "the score has measures the recording does not",
                        RM.DIFFERENT: "both have material here that does not match",
                        RM.WEAK: "both have material here; it matches poorly"}[g["kind"]]
        gaps.append(d)
    low = verify_summary(vf)
    lows = [m for m in vf["measures"] if m.get("chroma") is not None and m["chroma"] < 0.2]
    first_bpm = None
    tempo = dict(al["tempo"])
    if ref.get("measures"):
        m0 = ms[0]
        if len(m0.get("beats", [])) > 1:
            first_bpm = 60.0 / (m0["beats"][1] - m0["beats"][0])
    if first_bpm and tempo.get("ratio"):
        tempo["score_bpm_at_start"] = round(first_bpm, 1)
        tempo["recording_bpm_at_start"] = round(first_bpm / tempo["ratio"], 1)
    return {"song": res["song"], "score": res["score"], "recording": res["recording"],
            "aligned_with": res["source"],
            "key_offset_semitones": al["key"]["offset"],
            "key_note": "+n: the recording sounds n semitones higher than the score",
            "key_margin": al["key"].get("margin"),
            "tuning_cents": al.get("tuning_cents"),
            "tempo": tempo,
            "structure": gaps,
            "coverage": alignment_summary(al, ref)["coverage"],
            "low_agreement_measures": low["low"],
            "low_agreement_detail": [{"measure": m["label"], "chroma": m["chroma"], "lag_ms": m.get("lag_ms")}
                                     for m in lows[:200]],
            "onset_lag": vf["lag"]}


def checks(s: Settings, res: dict) -> dict:
    """Whether the retimed audio can be enabled: coverage (for vocals: of the score's vocal line), timing (the chroma
    lag of the full mix retimed, which has the piano the song's notes have; for vocals also the vocal onsets against
    the vocal notes, and the stem's melody against the vocal line), the stretch's fidelity to the plan, the key."""
    a = res["alignment"]
    lag = (res.get("verify_full") or res["verify"])["lag"]
    problems = []
    vs = res.get("vocal_sync")
    if vs and vs.get("vocal_notes"):
        # vocals: what counts is the vocal line; the song's instrumental passages may differ from the recording's
        cov = vs["onsets"]["notes"] / vs["vocal_notes"]
        if cov < s.min_coverage:
            problems.append(f"only {cov:.0%} of the score's vocal line is in the matched recording")
        mel = vs["melody"].get("agree_at_best")
        if mel is None or mel < s.min_melody_agreement:
            problems.append(f"the stem's pitch agrees with the score's vocal line in {mel} of the sung frames")
        on = vs["onsets"]
        if on.get("median_ms") is None or abs(on["median_ms"]) > s.max_lag_ms:
            problems.append(f"the vocal onsets lie {on.get('median_ms')} ms from the score's vocal notes (median)")
    elif a["coverage_with_weak"] < s.min_coverage:
        problems.append(f"only {a['coverage_with_weak']:.0%} of the song is matched (structure differs)")
    fid = (res.get("retime") or {}).get("fidelity_p95_ms")
    if fid is None or fid > 15:
        problems.append(f"the stretch missed its plan by {fid} ms (p95)")
    if lag.get("median_ms") is None or lag.get("windows", 0) < max(4, 0.3 * lag.get("windows_total", 0)):
        problems.append(f"the timing could not be verified ({lag.get('windows', 0)} of {lag.get('windows_total', 0)} "
                        f"windows)")
    else:
        if abs(lag["median_ms"]) > s.max_lag_ms:
            problems.append(f"median lag against the score {lag['median_ms']:+.0f} ms")
        if (lag.get("within_50ms") or 0) < s.min_within_50ms:
            problems.append(f"only {lag.get('within_50ms') or 0:.0%} of the windows within 50 ms")
    if a["key_offset"]:
        problems.append(f"the recording is {a['key_offset']:+d} semitones from the score")
    return {"ok": not problems, "problems": problems}


# ------------------------------------------------------------------------------
# outputs: state, audio_map proposal, report
# ------------------------------------------------------------------------------

def load_state(out_dir: str) -> dict:
    try:
        with open(os.path.join(out_dir, REPORT_NAME + ".json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("format") == 1 else {"format": 1, "songs": {}}
    except (OSError, ValueError):
        return {"format": 1, "songs": {}}


def current_audio_map(s: Settings) -> Dict[str, dict]:
    try:
        with open(s.path("audio_map"), encoding="utf-8") as f:
            return json.load(f).get("songs", {})
    except (OSError, ValueError):
        return {}


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def proposal(s: Settings, state: dict) -> dict:
    """Entries for Note Waterfall's Tools/audio_map.json: the sung songs' vocals (``enabled`` only when the checks
    pass).  Songs whose companion is generated from the score never get one here."""
    have = current_audio_map(s)
    songs = {}
    for name, r in sorted(state.get("songs", {}).items()):
        if r.get("kind") == "deliver" and r.get("timing") == "recording":
            ok = r["checks"]["ok"]
            t = r.get("tempo") or {}
            v = (t.get("verify") or {}).get("bar_starts") or {}
            note = (f"the original recording {r['recording']}, not stretched: the score's tempo map follows it "
                    f"(pianovision recordings tempo"
                    + (", repeats written out" if (t.get("repeats") or {}).get("unrolled") else "")
                    + f"); offset = the recording time of the song's start; measure starts within "
                    f"{v.get('p95_ms')} ms (p95) of the recording's")
            if not ok:
                note += "; NOT ENABLED: " + "; ".join(r["checks"]["problems"])
            e = {"audio": _tilde(r["audio"]), "offset": round(float(r.get("offset") or 0.0), 3), "enabled": ok,
                 "note": note}
            if name in have:
                e["replaces"] = have[name].get("audio")
            songs[name] = e
            continue
        if r.get("kind") != "deliver" or not r.get("mp3"):
            continue
        ok = r["checks"]["ok"]
        a = r["alignment"]
        lag = (r.get("verify_full") or r["verify"])["lag"]
        what = {"vocals": "the vocals isolated from", "no_piano": "the piano-removed stem of",
                "full": "the full mix of"}.get(r["variant"], r["variant"])
        vs = r.get("vocal_sync") or {}
        if vs.get("vocal_notes"):
            on = vs["onsets"]
            matched = (f"{on['notes'] / vs['vocal_notes']:.0%} of the score's vocal line matched, vocal onsets median "
                       f"{on['median_ms']} ms from the vocal notes")
        else:
            matched = f"{a['coverage_with_weak']:.0%} of the song matched"
        note = (f"{what} {r['recording']}, retimed onto the song's timeline by pianovision recordings: tempo ratio "
                f"{a['tempo']['ratio']:.3f}, {matched}, lag against the score (full mix, chords) median "
                f"{lag.get('median_ms')} ms ({lag.get('within_50ms')} of the windows within 50 ms)")
        if not ok:
            note += "; NOT ENABLED: " + "; ".join(r["checks"]["problems"])
        e = {"audio": _tilde(r["mp3"]), "offset": 0.0, "enabled": ok, "note": note}
        if name in have:
            e["replaces"] = have[name].get("audio")
        songs[name] = e
    return {"_comment": "Proposed entries for Note Waterfall's Tools/audio_map.json from `python3 -m pianovision "
                        "recordings align|retime`: sung songs. Where the recording leads (the default), the original "
                        "file you own at the offset `recordings tempo` found (enabled once its copy is applied to "
                        "the score); else the singing of the recording stretched onto the song (the mp3s are staged "
                        "under [recordings] out_dir: move them somewhere durable before deploying). Copy the ones "
                        "you want; 'replaces' is the entry there now.",
            "songs": songs}


def route_summary(links: dict, s: Settings) -> Dict[str, List[dict]]:
    """Every judged song by what its companion is: ``generated`` (with whether its score has an orchestra to render
    and what Note Waterfall has now), ``vocals`` with a usable recording, ``vocals`` without one (a recording to add
    to the music folder, or a suggestion to confirm), ``review``."""
    have = current_audio_map(s)
    L = links.get("links", {})
    out: Dict[str, List[dict]] = {"generated": [], "vocals": [], "vocals_missing": [], "review": []}
    for rel, r in sorted(links.get("routes", {}).items()):
        route = effective_route(r)
        le = L.get(rel) or {}
        eff = effective_link(le) if le else None
        item = {"score": rel, "song": r.get("song"), "title": r.get("title"), "artist": r.get("artist"),
                "why": r.get("why"), "decided": r.get("decision") is not None,
                "orchestra_notes": (r.get("facts") or {}).get("orchestra_notes", 0),
                "companion_now": (have.get(r.get("song")) or {}).get("audio"),
                "recording": eff[0] if eff else None, "variant": eff[1] if eff else None,
                "link_status": le.get("status"),
                "suggestion": le.get("recording") if not eff and le.get("decision") is None else None,
                "rejected": le.get("decision") is not None and not eff}
        if route == ROUTE_GENERATED:
            out["generated"].append(item)
        elif route == ROUTE_VOCALS:
            out["vocals" if eff else "vocals_missing"].append(item)
        else:
            out["review"].append(item)
    return out


def write_outputs(s: Settings, state: dict, links: dict, skipped: Sequence[str]) -> Tuple[str, str]:
    out_dir = s.path("out_dir")
    os.makedirs(out_dir, exist_ok=True)
    _write_json(os.path.join(out_dir, REPORT_NAME + ".json"), state)
    prop_path = os.path.join(out_dir, PROPOSAL_NAME)
    _write_json(prop_path, proposal(s, state), indent=2)
    md = report_markdown(s, state, links, skipped)
    md_path = os.path.join(out_dir, REPORT_NAME + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    return md_path, prop_path


def _fmt_gaps(gaps: Sequence[dict], limit: int = 8) -> List[str]:
    out = []
    for g in gaps[:limit]:
        ms = ", ".join(g.get("measures") or []) or "-"
        out.append(f"{g['kind']}{' (' + g['edge'] + ')' if g.get('edge') else ''}: song "
                   f"{g['song'][0]:.1f}-{g['song'][1]:.1f} s (m. {ms}), recording "
                   f"{g['recording'][0]:.1f}-{g['recording'][1]:.1f} s")
    if len(gaps) > limit:
        out.append(f"... and {len(gaps) - limit} more")
    return out


def report_markdown(s: Settings, state: dict, links: dict, skipped: Sequence[str]) -> str:
    L = links.get("links", {})
    lines = ["# Companion audio from recordings", "",
             f"Links: {s.links}; music folder {s.path('music_dir')}; outputs {s.path('out_dir')}.", ""]
    rs = route_summary(links, s)
    lines += ["## Routes", "",
              f"{len(rs['generated'])} generated (the score's orchestra rendered, piano muted), "
              f"{len(rs['vocals'])} sung with a recording (its vocals), {len(rs['vocals_missing'])} sung without a "
              f"recording, {len(rs['review'])} undecided.", "",
              "### Sung, with a recording: the companion is its vocals", ""]
    lines += [f"* {x['song']}: {x['recording']} [{x['variant']}] ({x['why']})" for x in rs["vocals"]] or ["* none"]
    lines += ["", "### Sung, without a recording (add one to the music folder, or confirm the suggestion)", ""]
    lines += [f"* {x['title']} | {x['artist']} | {x['score']}"
              + (f" | suggestion to check: {x['suggestion']}" if x["suggestion"] else "")
              + (" | the suggested recording was rejected" if x["rejected"] else "") + f" ({x['why']})"
              for x in rs["vocals_missing"]] or ["* none"]
    lines += ["", "### Generated from the score (no recording is used)", ""]
    lines += [f"* {x['song']}: " + ("the score has an orchestra to render" if x["orchestra_notes"] else
                                    "no orchestra in the score: no companion")
              + (f"; Note Waterfall has {x['companion_now']}" if x["companion_now"] else "")
              + (f"; recording {x['recording']} aligned for the accuracy summary only" if x["recording"] else "")
              + f" ({x['why']})" for x in rs["generated"]] or ["* none"]
    lines += ["", "### Undecided (`recordings route SONG vocals|generated`)", ""]
    lines += [f"* {x['song']} ({x['why']})" for x in rs["review"]] or ["* none"]
    lines += [""]
    lines.append("## Links")
    lines.append("")
    lines.append("| song | status | recording | variant | confidence | p_same | p_none |")
    lines.append("|---|---|---|---|---|---|---|")
    for rel, e in sorted(L.items(), key=lambda kv: (kv[1].get("status") != STATUS_LINKED, kv[0])):
        eff = effective_link(e)
        st = e.get("status") + (" (decided)" if e.get("decision") is not None else "")
        lines.append(f"| {e.get('song')} | {st} | {(eff or (e.get('recording'), '', ''))[0] or '-'} | "
                     f"{(eff or ('', e.get('variant') or '', ''))[1] or '-'} | {e.get('confidence')} | "
                     f"{e.get('p_same')} | {e.get('p_none')} |")
    if skipped:
        lines += ["", "Not processed:", ""] + [f"* {x}" for x in skipped]
    lines += ["", "## Aligned songs", ""]
    for name, r in sorted(state.get("songs", {}).items()):
        a = r["alignment"]
        t = a["tempo"]
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"* route {r.get('route')} ({r.get('kind')}): {r['recording']}"
                     + (f" [{r['variant']}]" if r.get("variant") else "") + f"; aligned with {r['source']}")
        if r.get("timing") == "recording":
            t = r.get("tempo") or {}
            lines.append(f"* the recording leads (nothing stretched): the original file at offset {r.get('offset')} s; "
                         f"tempo copy {t.get('copy')}"
                         + (" (repeats written out)" if (t.get("repeats") or {}).get("unrolled") else ""))
        if r.get("vocal_activity"):
            va = r["vocal_activity"]
            lines.append(f"* singing in {va['activity']:.0%} of the matched recording (vocals {va['vocals_to_mix_db']} "
                         f"dB against the mix)")
        lines.append(f"* key offset {a['key_offset']:+d} (margin {a.get('key_margin')}), tuning "
                     f"{a.get('tuning_cents')} cents; tempo ratio {t.get('ratio')} (per measure p10 {t.get('p10')}, "
                     f"median {t.get('median')}, p90 {t.get('p90')}; trend {t.get('trend_per_min')}/min; drift max "
                     f"{t.get('drift_max_s')} s)")
        lines.append(f"* matched: {a['coverage']:.0%} of the song in {a['pieces']} piece(s) (quality "
                     f"{', '.join(str(q) for q in a['piece_quality'])}); with weak gaps {a['coverage_with_weak']:.0%}")
        for g in _fmt_gaps(a["gaps"]):
            lines.append(f"  * {g}")
        if r.get("verify_full") and not r.get("verify"):
            vf = r["verify_full"]
            lines.append(f"* full mix retimed in the work folder: lag against the score median "
                         f"{vf['lag'].get('median_ms')} ms ({vf['lag'].get('within_50ms')} within 50 ms); chroma "
                         f"agreement {vf.get('chroma_mean')}; measures with low agreement {vf['low_count']} of "
                         f"{vf['measures_scored']}")
        if r.get("verify"):
            v = r["verify"]
            rt = r.get("retime") or {}
            retuned = f"; retuned {rt.get('retune_cents')} cents" if rt.get("retune_cents") else ""
            lines.append(f"* delivered mp3: {r.get('mp3')}; stretch fidelity p95 {rt.get('fidelity_p95_ms')} ms "
                         f"({len(rt.get('passes') or [])} pass(es)){retuned}")
            for label, vv in (("delivered", v), ("full mix retimed the same way", r.get("verify_full"))):
                if not vv:
                    continue
                lg, ol = vv["lag"], vv.get("onset_lag") or {}
                lines.append(f"* {label}: lag against the score (chords) median {lg.get('median_ms')} ms (p5 "
                             f"{lg.get('p5_ms')}, p95 {lg.get('p95_ms')}; {lg.get('windows')} of "
                             f"{lg.get('windows_total')} windows, {lg.get('within_50ms')} within 50 ms); onsets "
                             f"median {ol.get('median_ms')} ms ({ol.get('windows')} windows); chroma agreement "
                             f"{vv.get('chroma_mean')}")
            if r.get("vocal_sync") and r["vocal_sync"].get("melody"):
                vs = r["vocal_sync"]
                m, on, bl = vs["melody"], vs["onsets"], vs["bleed"]
                lines.append(f"* vocals against the score's vocal line ({vs['vocal_notes']} notes, {on['notes']} in "
                             f"the matched recording): onsets median {on['median_ms']} ms ({on['matched']} matched "
                             f"within 200 ms; {on['within_50ms']} within 50 ms, {on['within_100ms']} within 100 ms); "
                             f"pitch agrees in {m['agree_at_plan']} of the sung frames, {m['agree_at_best']} shifted "
                             f"{m['best_shift_ms']} ms (per 10 s window median {m['window_shift_ms']['median']} ms); "
                             f"stem level where the vocal line rests {bl['rest_minus_sung_db']} dB under where it "
                             f"sings")
            low = (r.get("verify_full") or r["verify"])
            lines.append(f"* measures with low agreement: {low['low_count']} of {low['measures_scored']}"
                         + (f": {', '.join(low['low'][:30])}" + (" ..." if len(low['low']) > 30 else "")
                            if low["low"] else ""))
        c = r.get("checks")
        if c:
            lines.append("* checks: " + ("pass" if c["ok"] else "; ".join(c["problems"])))
        lines.append("")
    return "\n".join(lines) + "\n"


def run(lib, s: Settings, only: Sequence[str] = (), force: bool = False, retime: bool = True,
        log: Log = print) -> int:
    """``align`` / ``retime`` for every usable link (or the ``only`` ones)."""
    from .accompaniment import AccompanimentError
    links = load_links(s.links)
    files, _videos = scan_music(s, log)
    recs = {r.key: r for r in group_recordings(files)}
    jobs, skipped = jobs_for(lib, s, links, only)
    failures = 0
    for job in jobs:
        log(f"{job.song.output}  <- {job.recording} [{job.how}; route {job.route}"
            + (f", {job.variant}]" if job.variant else ": accuracy summary only]"))
        try:
            r = process(lib, s, job, recs, load_state(s.path("out_dir")).get("songs", {}), force, retime, log)
        except (RecordingsError, AccompanimentError, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as e:
            log(f"  FAILED: {e}")
            failures += 1
            continue
        # the report's state is saved after every song, merged with what is on disk (another run may write too)
        state = load_state(s.path("out_dir"))
        state.setdefault("songs", {})[job.song.output] = r
        _write_json(os.path.join(s.path("out_dir"), REPORT_NAME + ".json"), state)
        a = r["alignment"]
        log(f"  key {a['key_offset']:+d}, tuning {a.get('tuning_cents')} c, tempo ratio {a['tempo'].get('ratio')}, "
            f"matched {a['coverage']:.0%} ({a['pieces']} pieces, {len(a['gaps'])} gaps)"
            + (f"; lag against the score {(r.get('verify_full') or r['verify'])['lag'].get('median_ms')} ms"
               if r.get("verify_full") or r.get("verify") else "")
            + ("" if job.kind == "diagnostic" else
               f"; checks {'pass' if r['checks']['ok'] else 'FAIL: ' + '; '.join(r['checks']['problems'])}"))
    for x in skipped:
        log(f"skipped: {x}")
    state = load_state(s.path("out_dir"))
    usable = {j.song.output for j in jobs_for(lib, s, links)[0]}
    state["songs"] = {k: v for k, v in state.get("songs", {}).items() if k in usable}   # links you rejected leave
    md, prop = write_outputs(s, state, links, skipped)
    log(f"report: {md}")
    log(f"audio_map proposal: {prop}")
    return 1 if failures else 0


# ------------------------------------------------------------------------------
# tempo: the score follows the recording
# ------------------------------------------------------------------------------

def tempo(lib, s: Settings, specs: Sequence[str] = (), dry_run: bool = False, out: Optional[str] = None,
          in_place: bool = False, backup: Optional[str] = None, yes: bool = False, unroll: str = "auto",
          mscore: Optional[str] = None, force: bool = False, anchors: Optional[str] = None, log: Log = print) -> int:
    """``recordings tempo``: for the named sung songs (default: every sung song with a usable recording whose
    timing is the recording's), the score timed to the recording (:mod:`pianovision.rectempo`): a verified copy in
    ``<tempo_dir>/<song>/`` (``-o``: also there), or with ``in_place`` that copy over the score after a backup."""
    import shutil
    from . import rectempo as RT
    links = load_links(s.links)
    files, _videos = scan_music(s, log)
    recs = {r.key: r for r in group_recordings(files)}
    jobs, skipped = jobs_for(lib, s, links, specs)
    jobs = [j for j in jobs if j.kind == "deliver" and (specs or effective_timing(j.entry, s) == "recording")]
    for x in skipped:
        log(f"skipped: {x}")
    if not jobs:
        log("no sung song with a usable recording" + (f" matches {', '.join(specs)}" if specs else ""))
        return 2
    if out and len(jobs) != 1:
        log("error: -o COPY names one song's copy; name one song")
        return 2
    if in_place and not backup:
        log("error: --in-place needs --backup DIR (the original is copied there first)")
        return 2
    if anchors and len(jobs) != 1:
        log("error: --anchors are one recording's measure times; name one song")
        return 2
    anchor_list = RT.read_anchors(anchors) if anchors else None
    failures = 0
    for job in jobs:
        song = job.song
        src, _deliver, _variant = recording_files(s, job, recs)
        score = os.path.join(lib.cfg.scores, song.rel)
        e = lib.manifest.entries.get(song.rel) or {}
        opts = RT.Options(work=os.path.join(s.path("tempo_dir"), song.stem), python=s.python, script=s.script,
                          beats_python=s.beats_python, beats_script=s.beats_script,
                          tolerance=s.tempo_tolerance_ms / 1000.0, repeat_tolerance=s.repeat_tolerance_ms / 1000.0,
                          unroll=unroll, mscore=mscore, min_free_mb=s.min_free_mb, force=force, tail=s.tail,
                          timeout=s.timeout, anchors=anchor_list)
        log(f"{song.output}  <- {job.recording} (the original file; the score follows it)")
        try:
            st = tempo_state(s, song)
            fresh = (st and not force and st.get("ok") and os.path.isfile(score)
                     and RT.sha256_file(score) == st.get("score_sha256") and st.get("recording_sig") == RT._sig(src)
                     and st.get("copy") and os.path.isfile(st["copy"]) and RT.sha256_file(st["copy"]) ==
                     st.get("copy_sha256")
                     and [(a["label"], a["recording"]) for a in st.get("anchors") or []] ==
                     [(f"m. {lb}", round(t, 3)) for lb, t in anchor_list or []])
            if in_place and fresh:
                res = st
                log(f"  the verified copy {st['copy']} is current (the score and the recording unchanged)")
            else:
                res = RT.run_song(score, src, opts, RT_compat(lib, e), lib.render_options(song.rel),
                                  lib.cfg.orchestra, lib.cfg.simplified, log, dry_run=dry_run)
        except (RT.RecTempoError, RecordingsError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as ex:
            log(f"  FAILED: {ex}")
            failures += 1
            continue
        _log_tempo(res, log)
        if dry_run:
            continue
        if out:
            shutil.copyfile(res["copy"], out)
            log(f"  copy: {out}")
        if in_place:
            if not res.get("ok"):
                log("  not applied: the copy failed its checks")
                failures += 1
                continue
            if not yes:
                import sys
                if not sys.stdin.isatty():
                    log("error: no terminal to confirm; pass --yes")
                    return 2
                if input(f"overwrite {score} (original to {backup})? [y/N] ").strip().lower() != "y":
                    continue
            try:
                RT.apply_copy(score, opts.work, backup, log)
            except RT.RecTempoError as ex:
                log(f"  not applied: {ex}")
                failures += 1
    return 1 if failures else 0


def RT_compat(lib, entry: dict):
    from .render import Compat
    return Compat.from_dict(lib._verify_compat(entry))


def _log_tempo(res: dict, log: Log) -> None:
    b, r = res["bars"], res["repeats"]
    log(f"  {b['measures']} measures as played in the recording {b['recording_seconds']} s; confidence "
        f"{b['confidence']}; detector {b['primary_detector']}, {b['anchors']} beats placed")
    log(f"  repeats {r['repeated_measures'] and 'm. ' + ','.join(map(str, r['repeated_measures'][:3])) + '...' or 'none'}:"
        f" one tempo per written measure off by up to {r['shared_tempo_max_ms']} ms -> "
        + ("written out" if r["unrolled"] else "kept"))
    log(f"  offset {res['offset']:+.3f} s; key {res['key_offset']:+d}; tempo marks {res['plan']['marks']}")
    if res.get("anchors"):
        worst = max(res["anchors"], key=lambda a: abs(a["diff_ms"]))
        log(f"  your anchors: {len(res['anchors'])} measure starts, the furthest {worst['label']} "
            f"{worst['diff_ms']:+.1f} ms")
    for c in res.get("checks") or []:
        log(f"  {c['name']}: {'ok' if c['ok'] else 'FAILED'}: {c['detail']}")
    v = (res.get("verify") or {}).get("recording_vs_copy")
    if v:
        log(f"  the recording against the copy's notes: {v}")
    if res.get("copy"):
        log(f"  report: {os.path.join(os.path.dirname(res['copy']), 'report.md')}")
        log(f"  audio_map entry: {json.dumps(res['audio_map'])}")


# ------------------------------------------------------------------------------
# status, confirm, reject
# ------------------------------------------------------------------------------

def status(lib, s: Settings, log: Log = print) -> int:
    from . import separation as SEP
    links = load_links(s.links)
    L = links["links"]
    try:
        files, videos = scan_music(s, log)
        recs = group_recordings(files)
        log(f"music folder {s.path('music_dir')}: {len(files)} audio files, {len(recs)} recordings "
            f"({sum(1 for r in recs if 'vocals' in r.files)} with a vocals stem, "
            f"{sum(1 for r in recs if 'no_piano' in r.files)} with a piano-removed stem), {len(videos)} video files "
            f"(not offered)")
    except RecordingsError as e:
        log(f"music folder: {e}")
    sep = SEP.status(s.separator_python)
    log("vocal separation: " + (f"{SEP.PACKAGE} in {sep['python']} ({s.separator_model})" if sep["available"]
                                else f"NOT AVAILABLE: {sep['missing']}"))
    rs = route_summary(links, s)
    log(f"routes ({len(links.get('routes', {}))} songs): {len(rs['generated'])} generated from the score, "
        f"{len(rs['vocals'])} sung with a recording, {len(rs['vocals_missing'])} sung without one, "
        f"{len(rs['review'])} undecided")
    for x in rs["vocals"]:
        log(f"  vocals    {x['song']}: {x['recording']} [{x['variant']}] ({x['why']})")
    for x in rs["vocals_missing"]:
        log(f"  no rec.   {x['song']}: {x['title']} | {x['artist']}"
            + (f" | suggestion to check: {x['suggestion']}" if x["suggestion"] else "")
            + (" | suggestion rejected" if x["rejected"] else "") + f" ({x['why']})")
    for x in rs["review"]:
        log(f"  undecided {x['song']}: {x['why']}  -> recordings route {x['score']!r} vocals|generated")
    for x in rs["generated"]:
        log(f"  generated {x['song']}: " + ("orchestra to render" if x["orchestra_notes"] else "no orchestra")
            + (f", recording {x['recording']} for the accuracy summary" if x["recording"] else "")
            + f" ({x['why']})")
    counts: Dict[str, int] = {}
    for e in L.values():
        k = e.get("status", "?") + ("+decision" if e.get("decision") is not None else "")
        counts[k] = counts.get(k, 0) + 1
    log(f"links: {len(L)} songs with candidates: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    for rel, e in sorted(L.items()):
        if e.get("status") == STATUS_REVIEW and e.get("decision") is None:
            log(f"  review    {e['song']}: {e.get('recording')} (confidence {e.get('confidence')}, p_same "
                f"{e.get('p_same')}): {e.get('why')}  -> recordings confirm / reject {rel!r}")
    state = load_state(s.path("out_dir"))
    for name, r in sorted(state.get("songs", {}).items()):
        a = r["alignment"]
        c = r.get("checks") or {}
        verdict = "ok" if c.get("ok") else "; ".join(c.get("problems", []))
        log(f"  aligned   {name} ({r.get('kind')}): matched {a['coverage']:.0%}, tempo ratio "
            f"{a['tempo'].get('ratio')}, key {a['key_offset']:+d}; "
            + (f"{r.get('mp3')} ({verdict})" if r.get("mp3") else verdict))
    return 0


def decide_route_for(lib, s: Settings, song_spec: str, route: str, log: Log = print) -> int:
    """Your route decision for a song: ``vocals``, ``generated``, or ``auto`` (the rule's again)."""
    links = load_links(s.links)
    song = _one_song(lib, song_spec, log)
    if song is None:
        return 2
    e = links["routes"].setdefault(song.rel, {"song": song.output, "title": song.title, "artist": song.artist,
                                              "route": ROUTE_REVIEW, "why": "not judged yet",
                                              "facts": score_facts(lib.manifest.entries.get(song.rel) or {})})
    if route == "auto":
        e.pop("decision", None)
    else:
        e["decision"] = route
    save_links(s.links, links)
    log(f"{song.output}: route {effective_route(e)}" + (" (yours)" if "decision" in e else f" ({e.get('why')})"))
    return 0


def mute_solos(lib, s: Settings, spec: str, source: Optional[str] = None, offset: Optional[float] = None,
               min_seconds: Optional[float] = None, log: Log = print) -> int:
    """``recordings mute-solos SONG``: the song's recording (its link's full mix, or ``source``: the same audio)
    with the passages where the piano plays alone muted (:mod:`pianovision.solomute`), at ``<solo_mute_dir>/<song>.mp3``
    with its report; the audio_map entry to use.  The song's score must follow the recording (``recordings tempo``):
    its offset is the one that run found (or ``offset``)."""
    from . import solomute as SM
    from .parts import parts_name
    song = _one_song(lib, spec, log)
    if song is None:
        return 2
    link = load_links(s.links)["links"].get(song.rel) or {}
    rec = (link.get("variants") or {}).get("full") or link.get("recording")
    src = os.path.abspath(os.path.expanduser(source)) if source else (os.path.join(s.path("music_dir"), rec)
                                                                       if rec else None)
    if not src or not os.path.isfile(src):
        log(f"{song.output}: no recording" + (f" ({src})" if src else " linked") + "; give --source FILE")
        return 2
    import hashlib
    st = tempo_state(s, song)
    score = os.path.join(lib.cfg.scores, song.rel)
    if offset is None and st and os.path.isfile(score):
        with open(score, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        if sha not in (st.get("score_sha256"), st.get("copy_sha256")):
            log(f"  note: {song.rel} changed since `recordings tempo` timed it; its offset may be stale")
    try:
        off = SM.offset_of(st, offset)
    except ValueError as e:
        log(f"{song.output}: {e}")
        return 2
    parts_path = os.path.join(lib.out, "NoteWaterfall", parts_name(song.output))
    song_path = os.path.join(lib.out, song.output)
    with open(parts_path, encoding="utf-8") as f:
        doc = json.load(f)
    with open(song_path, "rb") as f:
        if doc.get("songMd5") != hashlib.md5(f.read()).hexdigest():
            log(f"{song.output}: its parts file is not the song's (build first)")
            return 2
    out_dir = s.path("solo_mute_dir")
    os.makedirs(out_dir, exist_ok=True)
    mp3 = os.path.join(out_dir, song.stem + ".mp3")
    log(f"{song.output}  <- {os.path.basename(src)} with the piano's solo passages muted (offset {off:+.3f} s)")
    rep_ = SM.write(src, mp3, doc, off, bitrate=max(256, s.bitrate), min_seconds=min_seconds or s.solo_min_seconds,
                    title=song.title, artist=song.artist,
                    comment=f"Note Waterfall: {os.path.basename(src)}, the piano's solo passages muted", log=log)
    entry = {"audio": _tilde(mp3), "offset": round(off, 3), "enabled": True,
             "note": (f"the original recording ({os.path.basename(src)}), not stretched, with the {len(rep_['passages'])} "
                      f"passages where the piano plays alone muted ({rep_['muted_seconds']} s; pianovision recordings "
                      f"mute-solos); the score follows the recording (recordings tempo); offset {off:+.3f} s")}
    log(f"  {mp3}")
    log(f"  audio_map entry: {json.dumps(entry, ensure_ascii=False)}")
    with open(os.path.splitext(mp3)[0] + ".audio_map.entry.json", "w", encoding="utf-8") as f:
        json.dump({song.output: entry}, f, indent=2, ensure_ascii=False)
    return 0


def vocal_spans(lib, s: Settings, spec: str, vocals: Optional[str] = None, log: Log = print) -> int:
    """``recordings vocal-spans SONG``: where the singer of the song's recording sings, on the score's clock
    (:mod:`pianovision.vocalspans`), at ``<tempo_dir>/<song>/sung.json`` for ``lyrics import --sung``.  The score
    must follow the recording (``recordings tempo``: its offset); the vocals stem is separated once and cached
    (``vocals``: a stem you have)."""
    import hashlib
    from . import rectempo as RT
    from . import separation as SEP
    from . import vocalspans as VS
    song = _one_song(lib, spec, log)
    if song is None:
        return 2
    link = load_links(s.links)["links"].get(song.rel) or {}
    rec = (link.get("variants") or {}).get("full") or link.get("recording")
    src = os.path.join(s.path("music_dir"), rec) if rec else None
    if not src or not os.path.isfile(src):
        log(f"{song.output}: no recording linked")
        return 2
    st = tempo_state(s, song)
    score = os.path.join(lib.cfg.scores, song.rel)
    with open(score, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    if not st or "offset" not in st:
        log(f"{song.output}: the score does not follow the recording yet (`recordings tempo SONG` first)")
        return 2
    if sha not in (st.get("score_sha256"), st.get("copy_sha256")):
        log(f"  note: {song.rel} changed since `recordings tempo` timed it (its notes and measures must not have)")
    if not vocals:
        vocals = SEP.vocals_stem(src, s.path("stems_dir"), s.separator_model, s.separator_python,
                                 s.path("separator_script"), log=log)
    scratch = os.path.join(s.path("tempo_dir"), song.stem)
    os.makedirs(scratch, exist_ok=True)
    rv = SEP.rms_levels(vocals, VS.WINDOW, scratch)
    rm = SEP.rms_levels(src, VS.WINDOW, scratch)
    secs = VS.spans_from_levels(rv, rm)
    rd = RT.render(score)
    tl = rd.timeline
    last = tl.tick(tl.measures[-1].end_utick) if tl.measures else 0
    spans = VS.to_uticks(secs, float(st["offset"]), rd.seconds, tl.utick, last)
    where = VS.measure_ranges(spans, [m.utick for m in tl.measures], [tl.label(j) for j in range(len(tl.measures))])
    out = os.path.join(scratch, "sung.json")
    doc = {"format": 1, "score": song.rel, "score_sha256": sha, "recording": src, "vocals": vocals,
           "offset": st["offset"], "measures": where, "spans": [list(x) for x in spans],
           "recording_seconds": [list(x) for x in secs]}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    sung_s = sum(b - a for a, b in secs)
    log(f"{song.output}: {len(secs)} sung stretches, {sung_s:.0f} s of {len(rv) * VS.WINDOW:.0f} s; sung in {where}")
    log(f"  {out}  (lyrics import --sung)")
    return 0


_LAG_SCRIPT = """
import subprocess, sys, numpy as np
def dec(p):
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "30", "-t", "20", "-i", p, "-ac", "1", "-ar", "44100",
                        "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(r.stdout, np.float32)
a = dec(sys.argv[1]); b = sum(dec(p)[:len(a)] for p in sys.argv[2:])
n = min(len(a), len(b)); a, b = a[:n], b[:n]
c = np.fft.irfft(np.fft.rfft(a, 2 * n) * np.conj(np.fft.rfft(b, 2 * n)))
k = int(np.argmax(c)); k = k if k < n else k - 2 * n
bb = np.roll(b, k)
print(k, round(float(np.dot(a, bb) / (np.linalg.norm(a) * np.linalg.norm(bb) + 1e-12)), 4))
"""


def stems(lib, s: Settings, spec: str, log: Log = print) -> int:
    """``recordings stems SONG``: the song's recording (its link's full mix) separated into stems (Demucs, cached:
    :func:`pianovision.separation.all_stems`), each encoded as the generated companions are (constant bit rate, the
    codec delay cut, no Xing header: sample 0 is the recording's sample 0 as ffmpeg decodes it, so the stems play at
    the recording's offset), to ``<stems_out_dir>/<song>/<stem>.mp3``; checks that the stems add up to the recording,
    on time; writes the audio_map ``stems`` to add (``stems.entry.json``)."""
    from . import separation as SEP
    from .accompaniment import _ffmpeg, encode_args
    song = _one_song(lib, spec, log)
    if song is None:
        return 2
    link = load_links(s.links)["links"].get(song.rel) or {}
    rec = (link.get("variants") or {}).get("full") or link.get("recording")
    src = os.path.join(s.path("music_dir"), rec) if rec else None
    if not src or not os.path.isfile(src):
        log(f"{song.output}: no recording linked")
        return 2
    wavs = SEP.all_stems(src, s.path("stems_dir"), s.separator_model, s.separator_python, s.path("separator_script"),
                         log=log)
    py = os.path.expanduser(s.python)

    def lag_of(files: List[str]) -> Tuple[Optional[int], Optional[float]]:
        """Samples (44.1 kHz) the files' sum is later than the recording as ffmpeg decodes it, and the correlation."""
        if not os.path.isfile(py):
            return None, None
        r = subprocess.run([py, "-c", _LAG_SCRIPT, src] + files, capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not r.stdout.split():
            return None, None
        k, c = r.stdout.split()[:2]
        return -int(k), float(c)
    # Demucs decodes the input without its gapless trim: the stems start late by the source's codec delay
    skip, _c = lag_of([wavs[n] for n in SEP.STEMS])
    skip = skip or 0
    from .accompaniment import MP3_CODEC_DELAY
    out = os.path.join(s.path("stems_out_dir"), song.stem)
    os.makedirs(out, exist_ok=True)
    mp3s: Dict[str, str] = {}
    for name in SEP.STEMS:
        dst = os.path.join(out, name + ".mp3")
        args = encode_args(wavs[name], dst + ".tmp.mp3", 0.0, s.bitrate, song.title, song.artist,
                           f"Note Waterfall: the {name} of {os.path.basename(src)} (Demucs {s.separator_model})")
        af = args.index("-af") + 1
        args[af] = args[af].replace(f"atrim=start_sample={MP3_CODEC_DELAY}",
                                    f"atrim=start_sample={MP3_CODEC_DELAY + skip}" if skip >= 0 else
                                    f"adelay={-skip}S:all=1,atrim=start_sample={MP3_CODEC_DELAY}")
        _ffmpeg(args)
        os.replace(dst + ".tmp.mp3", dst)
        mp3s[name] = dst
    lag, corr = lag_of([mp3s[n] for n in SEP.STEMS])
    entry = {song.output: {name: _tilde(mp3s[name]) for name in SEP.STEMS}}
    with open(os.path.join(out, "stems.entry.json"), "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, ensure_ascii=False)
    log(f"{song.output}: {', '.join(SEP.STEMS)} -> {out}" + (f" (the separation's {skip} samples late cut)" if skip else "")
        + (f"; the stems' sum against the recording: lag {lag} samples, correlation {corr}" if lag is not None else ""))
    if lag is not None and (abs(lag) > 2 or corr < 0.9):
        log(f"  WARNING: the stems do not line up with the recording")
        return 1
    return 0


def _one_song(lib, spec: str, log: Log) -> Optional[Song]:
    songs = library_songs({"entries": lib.manifest.entries})
    hits = [x for x in songs if x.rel == spec or x.output == spec or x.stem == spec
            or fnmatch.fnmatch(x.rel, spec) or fnmatch.fnmatch(x.output, spec)]
    if len(hits) != 1:
        log(f"{spec!r}: {'no song' if not hits else str(len(hits)) + ' songs'} match")
        return None
    return hits[0]


def decide_link(lib, s: Settings, song_spec: str, recording: Optional[str] = None, variant: Optional[str] = None,
                reject: bool = False, log: Log = print) -> int:
    """Write your decision into the links file: confirm the pick (or link ``recording``), or reject."""
    links = load_links(s.links)
    L = links["links"]
    song = _one_song(lib, song_spec, log)
    if song is None:
        return 2
    e = L.setdefault(song.rel, {"song": song.output, "status": STATUS_REVIEW, "recording": None})
    if reject:
        e["decision"] = {"recording": None}
    else:
        d: Dict[str, Any] = {}
        if recording:
            if not os.path.isfile(os.path.join(s.path("music_dir"), recording)):
                log(f"{recording}: not in {s.path('music_dir')}")
                return 2
            d["recording"] = recording
        elif not e.get("recording"):
            log(f"{song.output}: nothing to confirm (no recording picked); name one")
            return 2
        if variant:
            d["variant"] = variant
        e["decision"] = d
    save_links(s.links, links)
    eff = effective_link(e)
    log(f"{song.output}: " + (f"uses {eff[0]} [{eff[1] or 'default'}]" if eff else "no recording"))
    return 0


# ------------------------------------------------------------------------------

def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _write_json(path: str, d: Any, indent: int = 1) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=indent, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
