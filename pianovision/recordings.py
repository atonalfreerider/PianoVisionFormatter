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
3. **Retime**: the delivered variant (the piano-removed stem when the recording has one, else the full mix; your
   ``decision`` may choose) cut, padded and stretched onto the song's timeline (rubberband), retuned to A440 when it
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
    min_coverage: float = 0.85          # enabled only when this share of the song is matched
    max_lag_ms: float = 30.0            # ... the median onset lag after the retime is within this
    min_within_50ms: float = 0.7        # ... and this share of the lag windows is within 50 ms
    timeout: int = 7200
    api_timeout: float = 60.0
    workers: int = 6

    def path(self, name: str) -> str:
        return os.path.abspath(os.path.expanduser(getattr(self, name)))


def load_settings(config_path: Optional[str], **overrides) -> Settings:
    """``[recordings]`` of pianovision.toml; ``links`` and ``script`` relative to that file."""
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
    for name in ("links", "script"):
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
        return {k: v for k, v in self.files.items() if k in ("full", "no_piano")}

    def default_variant(self) -> str:
        return "no_piano" if "no_piano" in self.files else ("full" if "full" in self.files else next(iter(self.files)))

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
      among the valid recordings, one with a piano-removed stem is preferred;
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
    by_key = {c.key: c for c in cands}
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
        pick = by_key[top]
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
        if out["status"] == STATUS_LINKED and "no_piano" not in pick.files:
            stems = [c for c in valid if "no_piano" in c.files]
            if stems:
                alt = max(stems, key=lambda c: (p[c.key], same[c.key]))
                out["recording"] = alt.key
                out["why"] += f"; {alt.key} preferred: a valid recording with a piano-removed stem"
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
# the links file (recordings.json)
# ------------------------------------------------------------------------------

LINKS_COMMENT = [
    "Recordings you own linked to the library's songs, for Note Waterfall's companion audio",
    "(`python3 -m pianovision recordings`, pianovision/recordings.py). Keys of 'links' are score paths relative to",
    "the scores folder; recording paths are relative to [recordings] music_dir. Written by `recordings match`:",
    "  status        'linked' (used), 'review' (needs your decision; not used until you confirm), 'none'",
    "  recording     the pick (or the suggestion, for review); variants: the full mix and the piano-removed stem",
    "  variant       the file that is retimed and delivered: 'no_piano' when the recording has one, else 'full'",
    "  confidence    the TypeSafe Choice confidence (both candidate orders averaged); p_none; p_same: the Noul",
    "                'same composition' of the recording; consistent: both orders picked the same",
    "  alternatives  other candidates that look like the same composition, or carried probability",
    "  judged        model, time, the hash of the question (match asks again only when it changes, or --refresh),",
    "                the candidates and the raw answers (match decides again from them: thresholds may change)",
    "Yours (never overwritten; `recordings confirm|reject` writes it):",
    "  decision      {} confirms the pick; {\"recording\": \"<path>\"} links another recording; {\"recording\": null}",
    "                rejects; \"variant\": \"full\" | \"no_piano\" chooses the file",
]


def load_links(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {"_comment": LINKS_COMMENT, "format": 1, "links": {}}
    if not isinstance(d.get("links"), dict):
        raise RecordingsError(f"{path}: no 'links' object")
    return d


def save_links(path: str, d: dict) -> None:
    d = dict(d)
    d["_comment"] = LINKS_COMMENT
    d["format"] = 1
    d["links"] = {k: d["links"][k] for k in sorted(d["links"])}
    ordered = {"_comment": d["_comment"], "format": 1, "links": d["links"]}
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
        e["variant"] = rec.default_variant()
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
    """Candidates per song and the judgment; writes the links file (keeps every ``decision``)."""
    files, videos = scan_music(s, log)
    recs = group_recordings(files)
    by_key = {r.key: r for r in recs}
    songs = library_songs({"entries": lib.manifest.entries})
    if only:
        songs = [x for x in songs if _matches(x, only)]
    props = propose_candidates(songs, recs, s.candidates, s.min_candidate_score)
    links = load_links(s.links)
    L = links["links"]
    jobs = []
    stats = {"songs": len(songs), "no_candidates": 0, "asked": 0, "kept": 0}
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
            e = link_entry(song, cands, verdict, judged.get("model", s.model), qkey, by_key, judged["answers"],
                           judged.get("time"))
            if "decision" in old:
                e["decision"] = old["decision"]
            L[song.rel] = e
            stats["kept"] += 1
            continue
        jobs.append((song, cands, state, qs, qkey))
    log(f"{len(files)} audio files, {len(recs)} recordings ({len(videos)} video files not offered); "
        f"{len(songs)} songs: {stats['no_candidates']} without candidates, {stats['kept']} judged before, "
        f"{len(jobs)} to judge")
    if dry_run:
        for song, cands, *_ in jobs:
            log(f"  {song.rel}: " + ", ".join(f"{r.key} ({sc:.2f})" for r, sc in cands))
        return stats
    judge = judge or api_judge(s)

    def ask(job):
        song, cands, state, qs, qkey = job
        res = judge(state, qs, s.model)
        return job, res

    errors = []
    with concurrent.futures.ThreadPoolExecutor(max(1, s.workers)) as ex:
        futs = [ex.submit(ask, j) for j in jobs]
        for fu in concurrent.futures.as_completed(futs):
            try:
                (song, cands, state, qs, qkey), res = fu.result()
            except RecordingsError as e:
                errors.append(str(e))
                continue
            verdict = decide(res["answers"], [r for r, _ in cands], s)
            e = link_entry(song, cands, verdict, res.get("model", s.model), qkey, by_key,
                           raw_answers(res["answers"], len(cands)))
            old = L.get(song.rel) or {}
            if "decision" in old:
                e["decision"] = old["decision"]
            L[song.rel] = e
            stats["asked"] += 1
            log(f"  {verdict['status']:7s} {song.output}: {verdict.get('recording')} "
                f"(confidence {verdict['confidence']:.2f}, p_none {verdict['p_none']:.2f})")
    save_links(s.links, links)
    if errors:
        log(f"{len(errors)} judgments failed: {errors[0]}")
    stats["errors"] = len(errors)
    return stats


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
    variant: str
    how: str                     # "auto" / "confirmed"


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
                skipped.append(f"{song.output}: needs your confirmation ({e.get('why')})")
            continue
        rec, variant, how = eff
        out.append(Job(song, e, rec, variant, how))
    return out, skipped


def recording_files(s: Settings, job: Job, recs: Dict[str, Recording]) -> Tuple[str, str, str]:
    """(source to align, file to deliver, variant) as absolute paths."""
    root = s.path("music_dir")
    r = recs.get(job.recording)
    if r is None:
        p = os.path.join(root, job.recording)
        if not os.path.isfile(p):
            raise RecordingsError(f"{job.recording}: not in the music folder")
        return p, p, "full"
    variant = job.variant if job.variant in r.files else r.default_variant()
    return os.path.join(root, r.source()), os.path.join(root, r.files[variant]), variant


def process(lib, s: Settings, job: Job, recs: Dict[str, Recording], state: dict, force: bool, retime: bool,
            log: Log) -> dict:
    """Align (and with ``retime``: retime, encode, verify) one song; the result is also stored in ``state``."""
    song = job.song
    work = os.path.join(s.path("work_dir"), song.stem)
    os.makedirs(work, exist_ok=True)
    src, deliver, variant = recording_files(s, job, recs)
    ref_path, ref = write_reference(lib, song, work)
    tools = tool_version(s.script)
    # what was made before: the song's own record in its work folder (survives a lost or concurrent report)
    cache_path = os.path.join(work, "cache.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = state.get(song.output) or {}
    res: Dict[str, Any] = {"song": song.output, "score": song.rel, "recording": job.recording, "variant": variant,
                           "how": job.how, "source": os.path.relpath(src, s.path("music_dir")),
                           "delivered": os.path.relpath(deliver, s.path("music_dir"))}
    akey = hashlib.sha256(f"{ref['sig']}|{src}|{_file_sig(src)}|{tools}".encode()).hexdigest()[:16]
    align_path = os.path.join(work, "align.json")
    if force or prev.get("align_key") != akey or not os.path.isfile(align_path):
        log(f"  aligning {song.output} with {res['source']} ...")
        t0 = time.monotonic()
        _run_tool(s, ["align", "--ref", ref_path, "--audio", src, "--out", align_path, "--tail", str(s.tail)], log)
        res["align_seconds"] = round(time.monotonic() - t0, 1)
    else:
        res["align_seconds"] = prev.get("align_seconds")
    res["align_key"] = akey
    _write_json(cache_path, {k: res.get(k, prev.get(k)) for k in ("align_key", "align_seconds")} |
                ({k: prev[k] for k in CACHED if k in prev} if prev.get("align_key") == akey else {}))
    with open(align_path, encoding="utf-8") as f:
        al = json.load(f)
    res["alignment"] = alignment_summary(al, ref)
    if not al["map"]["pieces"]:
        res["checks"] = {"ok": False, "problems": ["no part of the song matched the recording"]}
        for k in ("mp3", "verify", "verify_full", "accuracy"):
            res[k] = None
        state[song.output] = res
        return res
    if retime:
        out_dir = s.path("out_dir")
        os.makedirs(out_dir, exist_ok=True)
        mp3 = os.path.join(out_dir, song.stem + ".mp3")
        cents = float(al.get("tuning_cents") or 0.0)
        retune = cents if s.retune_cents and abs(cents) >= s.retune_cents else 0.0
        rkey = hashlib.sha256(f"{akey}|{deliver}|{_file_sig(deliver)}|{retune}|{s.bitrate}|{s.lufs}|{s.true_peak}"
                              .encode()).hexdigest()[:16]
        if force or prev.get("retime_key") != rkey or not os.path.isfile(mp3) or \
                prev.get("mp3_sha256") != _sha_file(mp3):
            log(f"  retiming {res['delivered']} ...")
            t0 = time.monotonic()
            wav = os.path.join(work, "retimed.wav")
            lat = os.path.join(s.path("work_dir"), "rubberband-latency.json")
            args = ["retime", "--ref", ref_path, "--align", align_path, "--audio", deliver, "--out", wav,
                    "--latency-cache", lat, "--retune-cents", f"{retune:.2f}"]
            if os.path.abspath(deliver) != os.path.abspath(src):
                args += ["--source", src]
            _run_tool(s, args, log)
            encode(s, wav, mp3, song, job)
            res["retime_seconds"] = round(time.monotonic() - t0, 1)
            with open(wav + ".json", encoding="utf-8") as f:
                res["retime"] = json.load(f)
            # verify the delivered mp3; for a stem, also the full mix retimed the same way (the accuracy summary)
            ver = os.path.join(work, "verify.json")
            _run_tool(s, ["verify", "--ref", ref_path, "--audio", mp3, "--key", str(al["key"]["offset"]),
                          "--out", ver], log)
            full_ver = ver
            if os.path.abspath(deliver) != os.path.abspath(src):
                fw = os.path.join(work, "retimed-full.wav")
                _run_tool(s, ["retime", "--ref", ref_path, "--align", align_path, "--audio", src, "--out", fw,
                              "--latency-cache", lat, "--retune-cents", f"{retune:.2f}"], log)
                full_ver = os.path.join(work, "verify-full.json")
                _run_tool(s, ["verify", "--ref", ref_path, "--audio", fw, "--key", str(al["key"]["offset"]),
                              "--out", full_ver], log)
                os.unlink(fw)
            os.unlink(wav)
            res["retime_key"] = rkey
            res["mp3"] = mp3
            res["mp3_sha256"] = _sha_file(mp3)
            res["verify_path"], res["verify_full_path"] = ver, full_ver
            _write_json(cache_path, {k: res.get(k) for k in ("align_key", "align_seconds") + CACHED})
        else:
            for k in CACHED:
                res[k] = prev.get(k)
        with open(res["verify_path"], encoding="utf-8") as f:
            v = json.load(f)
        with open(res["verify_full_path"], encoding="utf-8") as f:
            vf = json.load(f)
        res["verify"] = verify_summary(v)
        res["verify_full"] = verify_summary(vf) if res["verify_full_path"] != res["verify_path"] else None
        res["accuracy"] = accuracy_summary(al, vf, ref, res)
        res["checks"] = checks(s, res)
        acc_dir = os.path.join(out_dir, "accuracy")
        os.makedirs(acc_dir, exist_ok=True)
        _write_json(os.path.join(acc_dir, song.stem + ".json"), res["accuracy"])
    elif prev.get("align_key") == akey and prev.get("verify_path") and os.path.isfile(prev["verify_path"]):
        # align only, nothing changed: what the last retime made still holds
        for k in CACHED:
            res[k] = prev.get(k)
        with open(res["verify_path"], encoding="utf-8") as f:
            v = json.load(f)
        with open(res["verify_full_path"], encoding="utf-8") as f:
            vf = json.load(f)
        res["verify"] = verify_summary(v)
        res["verify_full"] = verify_summary(vf) if res["verify_full_path"] != res["verify_path"] else None
        res["accuracy"] = accuracy_summary(al, vf, ref, res)
        res["checks"] = checks(s, res)
    state[song.output] = res
    return res


CACHED = ("retime_seconds", "retime", "retime_key", "mp3", "mp3_sha256", "verify_path", "verify_full_path")


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
    """Whether the retimed audio can be enabled: coverage, timing (the chroma lag of the full mix retimed, which has
    the piano the song's notes have), the stretch's fidelity to the plan, and the key."""
    a = res["alignment"]
    lag = (res.get("verify_full") or res["verify"])["lag"]
    problems = []
    if a["coverage_with_weak"] < s.min_coverage:
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
    """Entries for Note Waterfall's Tools/audio_map.json (``enabled`` only when the checks pass)."""
    have = current_audio_map(s)
    songs = {}
    for name, r in sorted(state.get("songs", {}).items()):
        if not r.get("mp3"):
            continue
        ok = r["checks"]["ok"]
        a = r["alignment"]
        lag = (r.get("verify_full") or r["verify"])["lag"]
        note = (f"Recording {r['recording']} ({'piano-removed stem' if r['variant'] == 'no_piano' else 'full mix'}) "
                f"retimed onto the song's timeline by pianovision recordings: tempo ratio {a['tempo']['ratio']:.3f}, "
                f"{a['coverage_with_weak']:.0%} of the song matched, lag against the score median "
                f"{lag.get('median_ms')} ms ({lag.get('within_50ms')} of the windows within 50 ms)")
        if not ok:
            note += "; NOT ENABLED: " + "; ".join(r["checks"]["problems"])
        e = {"audio": _tilde(r["mp3"]), "offset": 0.0, "enabled": ok, "note": note}
        if name in have:
            e["replaces"] = have[name].get("audio")
        songs[name] = e
    return {"_comment": "Proposed entries for Note Waterfall's Tools/audio_map.json from `python3 -m pianovision "
                        "recordings retime` (copy the ones you want; 'replaces' is the entry there now). The mp3s are "
                        "staged under [recordings] out_dir: move them somewhere durable before deploying.",
            "songs": songs}


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
        lines.append(f"* recording: {r['recording']} ({r['variant']}; aligned with {r['source']})")
        lines.append(f"* key offset {a['key_offset']:+d} (margin {a.get('key_margin')}), tuning "
                     f"{a.get('tuning_cents')} cents; tempo ratio {t.get('ratio')} (per measure p10 {t.get('p10')}, "
                     f"median {t.get('median')}, p90 {t.get('p90')}; trend {t.get('trend_per_min')}/min; drift max "
                     f"{t.get('drift_max_s')} s)")
        lines.append(f"* matched: {a['coverage']:.0%} of the song in {a['pieces']} piece(s) (quality "
                     f"{', '.join(str(q) for q in a['piece_quality'])}); with weak gaps {a['coverage_with_weak']:.0%}")
        for g in _fmt_gaps(a["gaps"]):
            lines.append(f"  * {g}")
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
        log(f"{job.song.output}  <- {job.recording} [{job.how}]")
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
            + (f"; lag against the score {(r.get('verify_full') or r['verify'])['lag'].get('median_ms')} ms, checks "
               f"{'pass' if r['checks']['ok'] else 'FAIL: ' + '; '.join(r['checks']['problems'])}"
               if r.get("verify") else ""))
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
# status, confirm, reject
# ------------------------------------------------------------------------------

def status(lib, s: Settings, log: Log = print) -> int:
    links = load_links(s.links)
    L = links["links"]
    try:
        files, videos = scan_music(s, log)
        recs = group_recordings(files)
        stems = sum(1 for r in recs if "no_piano" in r.files)
        log(f"music folder {s.path('music_dir')}: {len(files)} audio files, {len(recs)} recordings "
            f"({stems} with a piano-removed stem), {len(videos)} video files (not offered)")
    except RecordingsError as e:
        log(f"music folder: {e}")
    counts: Dict[str, int] = {}
    for e in L.values():
        k = e.get("status", "?") + ("+decision" if e.get("decision") is not None else "")
        counts[k] = counts.get(k, 0) + 1
    log(f"links file {s.links}: {len(L)} songs judged: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    for rel, e in sorted(L.items()):
        eff = effective_link(e)
        if eff:
            log(f"  use     {e['song']}: {eff[0]} [{eff[1] or 'default'}; {eff[2]}; confidence {e.get('confidence')}]")
    for rel, e in sorted(L.items()):
        if e.get("status") == STATUS_REVIEW and e.get("decision") is None:
            log(f"  review  {e['song']}: {e.get('recording')} (confidence {e.get('confidence')}, p_same "
                f"{e.get('p_same')}): {e.get('why')}  -> recordings confirm {rel!r} / reject")
    state = load_state(s.path("out_dir"))
    for name, r in sorted(state.get("songs", {}).items()):
        a = r["alignment"]
        c = r.get("checks") or {}
        verdict = "ok" if c.get("ok") else "not enabled: " + "; ".join(c.get("problems", []))
        log(f"  aligned {name}: matched {a['coverage']:.0%}, tempo ratio {a['tempo'].get('ratio')}, key "
            f"{a['key_offset']:+d}; " + (f"{r.get('mp3')} ({verdict})" if r.get("mp3") else verdict))
    return 0


def decide_link(lib, s: Settings, song_spec: str, recording: Optional[str] = None, variant: Optional[str] = None,
                reject: bool = False, log: Log = print) -> int:
    """Write your decision into the links file: confirm the pick (or link ``recording``), or reject."""
    links = load_links(s.links)
    L = links["links"]
    songs = library_songs({"entries": lib.manifest.entries})
    hits = [x for x in songs if x.rel == song_spec or x.output == song_spec or x.stem == song_spec
            or fnmatch.fnmatch(x.rel, song_spec) or fnmatch.fnmatch(x.output, song_spec)]
    if len(hits) != 1:
        log(f"{song_spec!r}: {'no song' if not hits else str(len(hits)) + ' songs'} match")
        return 2
    song = hits[0]
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
