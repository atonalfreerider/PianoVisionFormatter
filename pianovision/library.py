"""The score library pipeline: MuseScore scores folder -> PianoVision JSON folder.

State lives in ``<output>/.manifest.json``.  Every output file is pinned to the
content of the score it came from:

* unchanged score            -> its JSON is left alone (byte-for-byte),
* re-saved score (e.g. by a newer MuseScore) that still renders to its JSON
                             -> the JSON is left alone, and so is a pinned JSON whose notes it still renders,
* new or edited score        -> rendered directly from the .mscz,
* moved/renamed score        -> recognised by content, keeps its output name,
* deleted or excluded score  -> output retired to ``.attic/`` (never deleted),
* two scores, one name       -> the second gets a distinguishing suffix.

Only ``*.mscz`` files are sources: renaming a score to ``.zip`` excludes it.
Files that would be overwritten are first moved to ``<output>/.attic/<run>/``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .render import Compat

MANIFEST_NAME = ".manifest.json"
ATTIC_NAME = ".attic"
ARRANGEMENTS_DIR = ".arrangements"      # alignment reports of simplified parts made from arrangements
CONFIG_NAME = "pianovision.toml"
DEFAULT_DEVICE_DIR = "/sdcard/Android/data/com.ZarApps.PianoVision/files"
DEFAULT_HANDS_DIR = "/sdcard/Android/data/com.atonalfreerider.notewaterfall/files/HandEdits"


# ------------------------------------------------------------------------------
# configuration
# ------------------------------------------------------------------------------

@dataclass
class Config:
    scores: str
    output: str
    device_dir: str = DEFAULT_DEVICE_DIR
    serial: str = ""
    adb: str = "adb"
    orchestra: bool = True
    simplified: bool = True
    jobs: int = 0                      # 0 = one per CPU
    path: str = ""                     # the config file this came from
    hands_dir: str = DEFAULT_HANDS_DIR # Note Waterfall's hand-edit sidecars on the headset
    waterfall_deploy: str = ""         # command that deploys songs to Note Waterfall (given --only NAME...)
    options: str = ""                  # per-score build options (scores.json, pianovision.buildopts)


def find_config(explicit: Optional[str] = None) -> Optional[str]:
    candidates = [explicit, os.environ.get("PIANOVISION_CONFIG"), os.path.join(os.getcwd(), CONFIG_NAME),
                  os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), CONFIG_NAME),
                  os.path.expanduser("~/.config/pianovision/config.toml")]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    if explicit:
        raise FileNotFoundError(explicit)
    return None


def load_config(path: Optional[str] = None, **overrides) -> Config:
    import tomllib
    cfg_path = find_config(path)
    raw: dict = {}
    if cfg_path:
        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
    base = os.path.dirname(cfg_path) if cfg_path else os.getcwd()

    def p(v: str) -> str:
        return os.path.normpath(os.path.join(base, os.path.expanduser(v))) if v else v

    lib, dev, conv = raw.get("library", {}), raw.get("device", {}), raw.get("convert", {})
    hands = raw.get("hands", {})
    cfg = Config(scores=p(lib.get("scores", "")), output=p(lib.get("output", "PianoVision")),
                 device_dir=dev.get("dir", DEFAULT_DEVICE_DIR), serial=dev.get("serial", ""),
                 adb=dev.get("adb", "adb"), orchestra=conv.get("orchestra", True),
                 simplified=conv.get("simplified", True), jobs=int(conv.get("jobs", 0)), path=cfg_path or "",
                 hands_dir=hands.get("device_dir", DEFAULT_HANDS_DIR),
                 waterfall_deploy=hands.get("waterfall_deploy", ""), options=p(conv.get("options", "scores.json")))
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, os.path.abspath(os.path.expanduser(v)) if k in ("scores", "output") else v)
    return cfg


# ------------------------------------------------------------------------------
# scanning the scores folder
# ------------------------------------------------------------------------------

@dataclass
class Source:
    rel: str          # path below the scores folder, "/"-separated
    path: str
    mtime: float
    size: int


@dataclass
class ScanResult:
    sources: Dict[str, Source] = field(default_factory=dict)
    invalid: Dict[str, str] = field(default_factory=dict)      # rel -> what is wrong with it
    excluded: List[str] = field(default_factory=list)          # scores renamed to .zip / extensionless zips
    companion_midi: List[str] = field(default_factory=list)    # .mid next to an .mscz (no longer needed)
    stray_midi: List[str] = field(default_factory=list)        # .mid without a score


def _mscz_problem(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError as e:
        return f"unreadable ({e.strerror})"
    if magic == b"MThd":
        return "a MIDI file with an .mscz extension"
    if magic[:2] != b"PK":
        return "not a MuseScore file"
    try:
        with zipfile.ZipFile(path) as z:
            if not any(n.endswith(".mscx") for n in z.namelist()):
                return "zip archive without a score (.mscx) inside"
    except zipfile.BadZipFile:
        return "damaged zip archive"
    return None


def scan_library(root: str) -> ScanResult:
    res = ScanResult()
    if not root or not os.path.isdir(root):
        raise FileNotFoundError(f"scores folder not found: {root!r}")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            low = fn.lower()
            if low.endswith(".mscz"):
                problem = _mscz_problem(full)
                if problem:
                    res.invalid[rel] = problem
                else:
                    st = os.stat(full)
                    res.sources[rel] = Source(rel, full, st.st_mtime, st.st_size)
            elif low.endswith(".zip"):
                res.excluded.append(rel)
            elif low.endswith((".mid", ".midi")):
                stem = full.rsplit(".", 1)[0]
                (res.companion_midi if os.path.exists(stem + ".mscz") else res.stray_midi).append(rel)
            elif "." not in fn and zipfile.is_zipfile(full):
                res.excluded.append(rel)
    return res


# ------------------------------------------------------------------------------
# manifest
# ------------------------------------------------------------------------------

def _now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class Manifest:
    VERSION = 1

    def __init__(self, path: str):
        self.path = path
        self.entries: Dict[str, dict] = {}                 # source rel -> entry
        self.deployed: Dict[str, Dict[str, str]] = {}      # device serial -> {file name: md5}
        self.retired: List[dict] = []

    @classmethod
    def load(cls, path: str) -> "Manifest":
        m = cls(path)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            m.entries = d.get("entries", {})
            m.deployed = d.get("deployed", {})
            m.retired = d.get("retired", [])
        return m

    def save(self) -> None:
        d = {"version": self.VERSION, "entries": self.entries, "retired": self.retired, "deployed": self.deployed}
        atomic_write(self.path, (json.dumps(d, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode())

    def owner_of(self, name: str) -> Optional[str]:
        for rel, e in self.entries.items():
            if e["output"] == name:
                return rel
        return None

    def outputs(self) -> Dict[str, str]:
        return {e["output"]: rel for rel, e in self.entries.items()}


# ------------------------------------------------------------------------------
# worker jobs (top level so they can run in a process pool)
# ------------------------------------------------------------------------------

def _job_render(path: str, compat: Optional[dict], orchestra: bool, simplified: bool,
                options: Optional[dict] = None) -> dict:
    from .convert import convert_mscz, output_name
    try:
        c = convert_mscz(path, Compat.from_dict(compat), orchestra, simplified, parts=True, options=options)
        return {"title": c.title, "artist": c.artist, "name": c.name, "data": c.data,
                "legacy_name": output_name(path, legacy=True, options=options), "parts": c.parts,
                "arrangement": c.arrangement, "restaffed": c.staves_revised}
    except Exception as e:                                   # a broken score must not stop the build
        return {"error": f"{type(e).__name__}: {e}"}


def resave_candidates(e: dict, version: int) -> List[dict]:
    """Knob sets (``Compat.to_dict``) under which a score saved again since its output was made may still
    render to that output, most likely first; the default knobs are not among them.

    ``e`` is the manifest entry, ``version`` the score's file format now (:func:`mscx.file_version`).
    Besides the entry's own knobs: for a file saved by MuseScore 4.7, MuseScore 4.6's export rules
    (``engine``); what a re-save changes in the file: a pre-4.4 score's dynType 0 became the voice
    assignment "allInStaff" (``va_dyn_type``, with the 4.5 dynamics model), and a MuseScore 3 score's
    capitalised title styles became lower case (``metadata`` "file": the legacy rules named it after its
    file)."""
    base = Library._verify_compat(e) or {}
    engines = [base.get("engine", "auto")] + (["4.6"] if version >= 470 and "engine" not in base else [])
    vas = [bool(base.get("va_dyn_type"))]
    if base.get("dynamics") == "4.5" and version >= 440 and not base.get("va_dyn_type"):
        vas.append(True)
    metas = [base.get("metadata", "v2")] + (["file"] if base.get("metadata") == "legacy" else [])
    out: List[dict] = []
    for meta in metas:
        for engine in engines:
            for va in vas:
                c = Compat.from_dict(base)
                c.engine, c.va_dyn_type, c.metadata = engine, va, meta
                d = c.to_dict()
                if d and d not in out:
                    out.append(d)
    return out


def _job_resaved(path: str, candidates: List[dict], target: str, orchestra: bool, simplified: bool,
                 options: Optional[dict] = None) -> dict:
    """The first of ``candidates`` (knobs) under which the score renders to exactly the file ``target``
    (``{"compat": knobs, ...render}``; ``{"compat": None}`` if none does)."""
    from .convert import convert_mscz
    try:
        with open(target, "rb") as f:
            want = f.read()
        for c in candidates:
            r = convert_mscz(path, Compat.from_dict(c), orchestra, simplified, parts=True, options=options)
            if r.data == want:
                return {"compat": c, "title": r.title, "artist": r.artist, "name": r.name, "data": r.data,
                        "parts": r.parts, "arrangement": r.arrangement}
    except Exception as e:
        return {"compat": None, "error": f"{type(e).__name__}: {e}"}
    return {"compat": None}


def _job_verify(path: str, compat: Optional[dict], target: str, orchestra: bool, simplified: bool,
                do_calibrate: bool, parts_target: Optional[str] = None, content: str = "",
                entry: Optional[dict] = None, options: Optional[dict] = None) -> dict:
    """Re-render and compare with the pinned output; with ``parts_target`` also with the parts file there
    (``parts``: identical | differs | missing).  ``entry``: the manifest entry; when the score changed since
    its output was made, the knobs ``build`` tries for a re-saved score (:func:`resave_candidates`) are
    tried as well (``"resaved"``: the ones that reproduce the output)."""
    from .calibrate import calibrate, note_signature
    from .convert import content_hash, convert_mscz
    from .mscx import file_version, load_mscx_bytes
    from .parts import parts_bytes
    try:
        with open(target, "rb") as f:
            want = f.read()
        c = convert_mscz(path, Compat.from_dict(compat), orchestra, simplified, parts=parts_target is not None,
                         options=options)
        extra0 = {}
        if c.data != want and entry is not None and content_hash(path) != entry.get("content"):
            for k in resave_candidates(entry, file_version(load_mscx_bytes(path))):
                if k == (compat or {}):
                    continue                                 # rendered just above
                c2 = convert_mscz(path, Compat.from_dict(k), orchestra, simplified, parts=parts_target is not None,
                                  options=options)
                if c2.data == want:
                    c, extra0 = c2, {"resaved": k}
                    break
        got = c.data
        extra = dict(extra0)
        if parts_target is not None:
            if not os.path.exists(parts_target):
                extra["parts"] = "missing"
            else:
                with open(parts_target, "rb") as f:
                    have = f.read()
                same = parts_bytes(c.parts, os.path.basename(target), want, content) == have
                extra["parts"] = "identical" if same else "differs"
        if got == want:
            return {"status": "identical", **extra}
        same_notes = note_signature(got) == note_signature(want)
        if do_calibrate:
            cal = calibrate(path, want, orchestra, simplified, metadata=(compat or {}).get("metadata", "v2"))
            if cal is not None:
                return {"status": "calibrated", "compat": cal.to_dict(), **extra}
        return {"status": "differs", "same_notes": same_notes, **extra}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------------------
# the pipeline
# ------------------------------------------------------------------------------

@dataclass
class Action:
    # keep | render | parts | staves | move | retire | conflict | source  (parts: only the parts file; staves: rendered
    # again with the entry's knobs, the output replaced only when the score's staves have other roles now; source: a
    # score that is not a song of its own, only another score's partner: nothing to do)
    kind: str
    rel: str
    reason: str = ""
    old_rel: str = ""
    compat: Optional[dict] = None


@dataclass
class Report:
    kept: int = 0
    written: List[Tuple[str, str, str]] = field(default_factory=list)     # (rel, output, why)
    adopted: List[Tuple[str, str, str]] = field(default_factory=list)     # (rel, output, how)
    moved: List[Tuple[str, str]] = field(default_factory=list)
    retired: List[Tuple[str, str, str]] = field(default_factory=list)     # (rel, output, why)
    renamed: List[Tuple[str, str]] = field(default_factory=list)
    conflicts: List[Tuple[str, str]] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)
    attic: List[str] = field(default_factory=list)
    planned: List[Action] = field(default_factory=list)
    parts: List[str] = field(default_factory=list)       # Note Waterfall parts files written (NoteWaterfall/...)
    resaved: List[Tuple[str, str, str]] = field(default_factory=list)     # (rel, output, how): score saved again,
                                                                          # its output kept (byte for byte)
    arrangements: List[Tuple[str, str, str]] = field(default_factory=list)  # (rel, report file, summary): the
                                                                          # songs made with a partner score


def _knobs_text(knobs: dict) -> str:
    """What a set of knobs reproduces, for the build report."""
    words = []
    if knobs.get("engine") == "4.6":
        words.append("MuseScore 4.6 export rules")
    if knobs.get("va_dyn_type"):
        words.append("4.5 dynamics with allInStaff as dynType 0")
    if knobs.get("metadata") == "file":
        words.append("title from the file name")
    return ", ".join(words) or "its knobs"


def _digest(d: dict) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _stem_tokens(rel: str) -> List[str]:
    stem = os.path.splitext(os.path.basename(rel))[0]
    return re.findall(r"[a-z0-9]+", stem.lower().replace("'", ""))


class Library:
    def __init__(self, config: Config, log: Callable[[str], None] = print):
        self.cfg = config
        self.log = log
        self.out = config.output
        os.makedirs(self.out, exist_ok=True)
        self.manifest = Manifest.load(os.path.join(self.out, MANIFEST_NAME))
        self._attic_dir: Optional[str] = None
        from . import buildopts
        self.options = buildopts.load(config.options)          # per-score build options (scores.json)
        self._optsigs: Dict[str, Optional[dict]] = {}

    # -- helpers ----------------------------------------------------------------
    def out_path(self, name: str) -> str:
        return os.path.join(self.out, name)

    def output_files(self) -> List[str]:
        return sorted(f for f in os.listdir(self.out) if f.endswith(".json") and not f.startswith("."))

    def orphans(self) -> List[str]:
        managed = self.manifest.outputs()
        return [f for f in self.output_files() if f not in managed]

    def to_attic(self, name: str, report: Report) -> None:
        src = self.out_path(name)
        if not os.path.exists(src):
            return
        if self._attic_dir is None:
            stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
            self._attic_dir = os.path.join(self.out, ATTIC_NAME, stamp)
            os.makedirs(self._attic_dir, exist_ok=True)
        dst = os.path.join(self._attic_dir, name)
        n = 2
        while os.path.exists(dst):
            dst = os.path.join(self._attic_dir, f"{name[:-5]}.{n}.json")
            n += 1
        os.makedirs(os.path.dirname(dst), exist_ok=True)       # parts files: NoteWaterfall/<stem>.parts.json
        shutil.move(src, dst)
        report.attic.append(os.path.relpath(dst, self.out))

    # -- Note Waterfall's parts files (pianovision.parts) ------------------------------
    def parts_path(self, output: str) -> str:
        from .parts import parts_rel
        return self.out_path(parts_rel(output))

    def _parts_current(self, e: dict) -> bool:
        """The entry's parts file is there, of the current format, for the current score and output."""
        from .convert import file_hash
        from .parts import PARTS_FORMAT, PARTS_REVISION
        p = e.get("parts")
        path = self.parts_path(e["output"])
        return (bool(p) and p.get("format") == PARTS_FORMAT and p.get("revision", 1) == PARTS_REVISION
                and p.get("content") == e.get("content")
                and p.get("output_sha256") == e.get("output_sha256") and os.path.exists(path)
                and file_hash(path) == p.get("sha256"))

    def _write_parts(self, rel: str, doc: Optional[dict], song_data: bytes, report: Report) -> None:
        """Writes the parts file of the entry ``rel`` (after its output was stored) and records it."""
        from .convert import bytes_hash
        from .parts import PARTS_REVISION, parts_bytes, parts_rel, parts_stats
        e = self.manifest.entries[rel]
        if doc is None:
            return
        data = parts_bytes(doc, e["output"], song_data, e.get("content") or "")
        path = self.parts_path(e["output"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, "rb") as f:
                same = f.read() == data
            if same:
                path = None
            else:
                self.to_attic(parts_rel(e["output"]), report)     # nothing is overwritten unseen
        if path is not None:
            atomic_write(path, data)
            report.parts.append(parts_rel(e["output"]))
        e["parts"] = {"format": doc["format"], "revision": PARTS_REVISION, "sha256": bytes_hash(data), "content": e.get("content"),
                      "output_sha256": e.get("output_sha256"), **parts_stats(doc)}
        sig = self._optsigs[rel] if rel in self._optsigs else self.options_signature(rel)
        if sig:
            e["options"] = sig                     # the build options (and arrangement) the parts were made with
        else:
            e.pop("options", None)

    def parts_summary(self) -> dict:
        """Counts over the manifest: scores with parts, with a simplified part, with orchestra notes, with lyrics
        (written on a staff of the piano part / on a vocal staff)."""
        ps = [e["parts"] for e in self.manifest.entries.values() if e.get("parts")]
        lyr = [p["lyrics"] for p in ps if p.get("lyrics")]
        with_orch = [p for p in ps if p.get("notes", {}).get("orchestra", 0) > 0]
        return {"scores": len(self.manifest.entries), "parts": len(ps),
                "simplified": sum(1 for p in ps if p.get("simplified")),
                "orchestra": len(with_orch),
                "piano_orchestral": sum(1 for p in with_orch if p.get("orchestra") == "piano-orchestral"),
                "instruments": sum(1 for p in with_orch if p.get("orchestra") == "instruments"),
                "orchestra_notes": sum(p.get("notes", {}).get("orchestra", 0) for p in ps),
                "lyrics": len(lyr), "lyrics_piano": sum(1 for x in lyr if "piano" in x.get("sources", [])),
                "lyrics_vocal": sum(1 for x in lyr if "vocal" in x.get("sources", []))}

    # -- per-score build options (scores.json, pianovision.buildopts) ----------------------
    def is_song(self, rel: str) -> bool:
        """False for a score whose build options say it is not a song of its own (``song: false``)."""
        o = self.options.get(rel)
        return o is None or o.song

    def partner_users(self, rel: str) -> List[str]:
        """The scores whose partner (simplified_from, orchestra_from) is ``rel``."""
        return sorted(r for r, o in self.options.items() if o.partner == rel)

    def partner_compat(self, rel: str) -> Optional[dict]:
        """The knobs a partner score is rendered with: its manifest entry's when it is a song of its own (as it is
        rendered for its own song), else none (the default knobs)."""
        if self.is_song(rel) and rel in self.manifest.entries:
            return self._verify_compat(self.manifest.entries[rel])
        return None

    def render_options(self, rel: str) -> Optional[dict]:
        """The score's build options as :func:`pianovision.convert.convert_mscz` takes them (None: none); a partner
        score is rendered with its own build options and knobs (:meth:`partner_compat`)."""
        o = self.options.get(rel)
        if o is None:
            return None
        if not o.partner:
            return o.render_options(self.cfg.scores) or None
        return o.render_options(self.cfg.scores, self.partner_compat(o.partner), self.options.get(o.partner)) or None

    def options_signature(self, rel: str) -> Optional[dict]:
        """What the manifest records of the options a score was built with: a hash of the options that make its
        own render (title, composer, piano staves, hands) and, with a partner score, which score it is, what it gives,
        the hash of its content, its knobs, the hash of its options and the measure map, and the revisions of the
        alignment and the clock.  None without options."""
        from .arrangement import ARRANGEMENT_REVISION
        from .buildopts import SONG_KEYS
        from .convert import content_hash
        from .timeline import TIMELINE_REVISION
        o = self.options.get(rel)
        if o is None:
            return None
        song = {k: v for k, v in o.raw.items() if k in SONG_KEYS}
        sig: Dict[str, object] = {"song": _digest(song)}
        if o.partner:
            path = os.path.join(self.cfg.scores, o.partner)
            try:
                content = content_hash(path)
            except (OSError, zipfile.BadZipFile):
                content = "missing"
            ro = (self.render_options(rel) or {}).get("partner") or {}
            sig["partner"] = {"score": o.partner, "kind": o.partner_kind, "content": content,
                              "compat": ro.get("compat"), "options": _digest({"map": o.measure_map,
                                                                              "partner": ro.get("options")}),
                              "revision": [ARRANGEMENT_REVISION, TIMELINE_REVISION]}
        return sig

    def _options_actions(self, actions: List["Action"]) -> List["Action"]:
        """Scores whose build options (or partner score) changed since they were built are rendered again (a partner
        sets the song's clock or orchestra: its PianoVision JSON depends on it)."""
        out = []
        entries = self.manifest.entries
        for a in actions:
            if a.kind in ("render", "move", "retire", "conflict", "source"):
                if a.kind in ("render", "move"):
                    self._optsigs[a.rel] = self.options_signature(a.rel)
                out.append(a)
                continue
            e = entries.get(a.rel)
            sig = self.options_signature(a.rel)
            self._optsigs[a.rel] = sig
            have = (e or {}).get("options")
            if e is None or sig == have:
                out.append(a)
            elif (sig or {}).get("song") != (have or {}).get("song"):
                out.append(Action("render", a.rel, "build options changed (scores.json)", compat=self._verify_compat(e)))
            else:
                p = (sig or {}).get("partner") or (have or {}).get("partner") or {}
                out.append(Action("render", a.rel, f"partner score changed ({p.get('score', '?')}: its options, its "
                                                   f"score or the alignment's revision)", compat=self._verify_compat(e)))
        return out

    def write_arrangement_report(self, rel: str, output: str, rep: dict) -> str:
        """``<output>/.arrangements/<song stem>.md`` and ``.json``: the alignment report of a song made with a partner
        score (:mod:`pianovision.arrangement`, :mod:`pianovision.timeline`); returns the .md path."""
        from .arrangement import report_markdown
        folder = os.path.join(self.out, ARRANGEMENTS_DIR)
        os.makedirs(folder, exist_ok=True)
        stem = output[:-5] if output.endswith(".json") else output
        md = os.path.join(folder, stem + ".md")
        atomic_write(md, report_markdown(rep, output).encode("utf-8"))
        atomic_write(os.path.join(folder, stem + ".json"),
                     (json.dumps(rep, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
        return md

    def _jobs(self, jobs: Optional[int]) -> int:
        return max(1, jobs or self.cfg.jobs or os.cpu_count() or 1)

    def _pool_map(self, fn, arglists: List[tuple], jobs: Optional[int], label: str):
        if not arglists:
            return []
        n = self._jobs(jobs)
        if n == 1 or len(arglists) == 1:
            return [fn(*a) for a in arglists]
        out = [None] * len(arglists)
        with ProcessPoolExecutor(max_workers=min(n, len(arglists))) as ex:
            futs = {ex.submit(fn, *a): i for i, a in enumerate(arglists)}
            done = 0
            from concurrent.futures import as_completed
            for fut in as_completed(futs):
                out[futs[fut]] = fut.result()
                done += 1
                if sys.stderr.isatty():
                    print(f"\r  {label} {done}/{len(arglists)}", end="", file=sys.stderr, flush=True)
        if sys.stderr.isatty():
            print("\r" + " " * 40 + "\r", end="", file=sys.stderr, flush=True)
        return out

    def _source_unchanged(self, src: Source, e: dict) -> Tuple[bool, str]:
        """(unchanged, content hash); cheap when mtime and size are unchanged."""
        from .convert import content_hash
        if e.get("mtime") == src.mtime and e.get("size") == src.size:
            return True, e["content"]
        h = content_hash(src.path)
        return h == e["content"], h

    def _disambiguate(self, name: str, rel: str, rival_rel: str, taken) -> str:
        base = name[:-5]
        rival = set(_stem_tokens(rival_rel))
        extra = [t for t in _stem_tokens(rel) if t not in rival]
        if extra:
            cand = f"{base}_{'_'.join(extra)[:40]}.json"
            if cand not in taken:
                return cand
        n = 2
        while f"{base}_{n}.json" in taken:
            n += 1
        return f"{base}_{n}.json"

    # -- planning ---------------------------------------------------------------
    def plan(self, scan: ScanResult, force: bool = False, only: Optional[List[str]] = None,
             compat_for: Optional[Dict[str, dict]] = None) -> List[Action]:
        """``compat_for``: scores whose edit keeps their compatibility settings (hand edits:
        the edit does not touch what the settings model)."""
        from .convert import content_hash, file_hash
        from .pvjson import STAVES_REVISION
        entries = self.manifest.entries
        actions: List[Action] = []
        wanted = None if not only else set(only)
        gone = {rel for rel in entries if rel not in scan.sources}
        by_content: Dict[str, List[str]] = {}
        for rel in gone:
            by_content.setdefault(entries[rel]["content"], []).append(rel)
        self._hashes: Dict[str, str] = {}
        for rel, src in scan.sources.items():
            e = entries.get(rel)
            selected = wanted is None or rel in wanted
            if not self.is_song(rel):
                users = self.partner_users(rel)
                why = "not a song of its own (scores.json song: false)" + (
                    f"; the partner of {', '.join(users)}" if users else "")
                actions.append(Action("retire" if e is not None else "source", rel, why))
                continue
            if e is None:
                h = content_hash(src.path)
                self._hashes[rel] = h
                if by_content.get(h):
                    old = by_content[h].pop(0)
                    gone.discard(old)
                    actions.append(Action("move", rel, "score moved or renamed", old_rel=old))
                    continue
                actions.append(Action("render", rel, "new score"))
                continue
            unchanged, h = self._source_unchanged(src, e)
            self._hashes[rel] = h
            out = self.out_path(e["output"])
            if force and selected:
                actions.append(Action("render", rel, "forced"))
            elif not unchanged:
                if compat_for is not None and rel in compat_for:
                    actions.append(Action("render", rel, "hand edits", compat=compat_for[rel] or None))
                else:
                    actions.append(Action("render", rel, "score changed"))
            elif not os.path.exists(out):
                actions.append(Action("render", rel, "output missing", compat=e.get("compat")))
            elif file_hash(out) != e["output_sha256"]:
                actions.append(Action("conflict", rel, "output was changed outside the pipeline"))
            elif e.get("staves", 1) < STAVES_REVISION:
                actions.append(Action("staves", rel, "staff classification revised", compat=self._verify_compat(e)))
            elif not self._parts_current(e):
                # the output stays byte for byte; only Note Waterfall's parts file is (re)written
                actions.append(Action("parts", rel, "Note Waterfall parts file missing or out of date",
                                      compat=self._verify_compat(e)))
            else:
                actions.append(Action("keep", rel))
        for rel in sorted(gone):
            stem = os.path.join(self.cfg.scores, rel)[:-5]
            why = "excluded (renamed to .zip)" if os.path.exists(stem + ".zip") else "score deleted"
            actions.append(Action("retire", rel, why))
        return self._options_actions(actions)

    # -- build --------------------------------------------------------------------
    def build(self, force: bool = False, only: Optional[List[str]] = None, dry_run: bool = False,
              prune_orphans: bool = False, jobs: Optional[int] = None, restrict: Optional[set] = None,
              compat_for: Optional[Dict[str, dict]] = None, keep: Optional[List[str]] = None) -> Report:
        """``restrict`` limits the build to these scores (others are left for the next build);
        ``compat_for`` is passed to :meth:`plan`.  ``keep``: scores whose current output stays as it is
        (pinned) although the score changed, e.g. after MuseScore 4.7 reads it differently."""
        from .calibrate import note_signature
        from .convert import bytes_hash, file_hash
        scan = scan_library(self.cfg.scores)
        if only:
            only = [self._resolve_rel(o, scan) for o in only]
        keep_rels = {self._resolve_rel(k, scan) for k in keep or []}
        actions = self.plan(scan, force=force, only=only, compat_for=compat_for)
        if restrict is not None:
            actions = [a for a in actions if a.rel in restrict]
        report = Report(planned=actions)
        report.kept = sum(a.kind == "keep" for a in actions)
        report.conflicts = [(a.rel, a.reason) for a in actions if a.kind == "conflict"]
        if dry_run:
            return report
        m = self.manifest
        self._attic_dir = None

        for a in actions:
            if a.kind == "move":
                e = m.entries.pop(a.old_rel)
                src = scan.sources[a.rel]
                e.update(mtime=src.mtime, size=src.size)
                m.entries[a.rel] = e
                report.moved.append((a.old_rel, a.rel))
            elif a.kind == "retire":
                e = m.entries.pop(a.rel)
                self.to_attic(e["output"], report)
                from .parts import parts_rel
                self.to_attic(parts_rel(e["output"]), report)
                m.retired.append({"source": a.rel, "output": e["output"], "date": _now(), "reason": a.reason})
                report.retired.append((a.rel, e["output"], a.reason))
            elif a.kind in ("keep", "parts", "staves"):
                src, e = scan.sources[a.rel], m.entries[a.rel]
                e.update(mtime=src.mtime, size=src.size)

        todo = [a for a in actions if a.kind in ("render", "parts", "staves")]
        results = self._pool_map(_job_render, [(scan.sources[a.rel].path, a.compat, self.cfg.orchestra,
                                                self.cfg.simplified, self.render_options(a.rel)) for a in todo],
                                 jobs, "rendering")
        resaved = self._resaved_outputs(scan, todo, results, keep_rels, jobs)
        taken = set(m.outputs())
        new_by_name: Dict[str, List[Tuple[Action, dict]]] = {}
        for a, r in zip(todo, results):
            if "error" in r:
                report.failed.append((a.rel, r["error"]))
                continue
            if a.kind == "parts":
                # the song's JSON is kept as it is; its parts go with those bytes
                with open(self.out_path(m.entries[a.rel]["output"]), "rb") as f:
                    self._write_parts(a.rel, r.get("parts"), f.read(), report)
                continue
            if a.kind == "staves":
                self._restaff(a, scan.sources[a.rel], r, report)
                continue
            if a.rel in resaved:
                self._keep_resaved(a.rel, scan.sources[a.rel], resaved[a.rel], report)
                continue
            if a.rel in m.entries:
                self._store(a.rel, scan.sources[a.rel], m.entries[a.rel]["output"], r, "rendered", a.reason,
                            report, compat=a.compat)
            else:
                new_by_name.setdefault(r["name"], []).append((a, r))

        # New scores: adopt an existing (legacy) output of the same name when it still shows the
        # same notes; resolve name collisions between scores.
        for name, group in sorted(new_by_name.items()):
            # an existing output may carry the old-rules name (MuseScore 3 scores)
            found = next((n for n in [name] + sorted({g[1]["legacy_name"] for g in group})
                          if n not in taken and os.path.exists(self.out_path(n))), None)
            existing = self.out_path(found or name)
            owner_rel = None
            if found:
                with open(existing, "rb") as f:
                    have = f.read()
                sig = note_signature(have)
                exact = [g for g in group if g[1]["data"] == have]
                close = [g for g in group if note_signature(g[1]["data"]) == sig]
                legacy = [] if exact or close else [g for g in group if self._legacy_midi_matches(
                    scan.sources[g[0].rel], have)]
                if exact or close or legacy:
                    a, r = (exact or close or legacy)[0]
                    how = ("identical to render" if exact else "same notes as render (pinned as is)" if close
                           else "matches the companion .mid export (pinned as is)")
                    self._adopt(a.rel, scan.sources[a.rel], found, r, have, how, reproducible=bool(exact),
                                report=report)
                    owner_rel = a.rel
                    taken.add(found)
                    group = [g for g in group if g[0].rel != a.rel]
            # the score with the plainest file name gets the plain output name
            for a, r in sorted(group, key=lambda g: (len(_stem_tokens(g[0].rel)), g[0].rel)):
                target = name
                if target in taken:
                    rival = owner_rel or m.owner_of(target) or ""
                    target = self._disambiguate(name, a.rel, rival, taken | set(self.output_files()))
                taken.add(target)
                owner_rel = owner_rel or a.rel
                self._store(a.rel, scan.sources[a.rel], target, r, "rendered", a.reason, report)

        for a, r in zip(todo, results):
            if r.get("arrangement") and a.rel in m.entries and not r.get("error"):
                rep = r["arrangement"]
                md = self.write_arrangement_report(a.rel, m.entries[a.rel]["output"], rep)
                n = rep["measures"]
                tl = rep.get("timeline") or {}
                clock = f"; on the solo's clock: {tl['seconds']:.1f} s, {tl['marks']} tempo marks" if tl else ""
                if rep.get("kind") == "orchestra":
                    how = (f"orchestra from {rep['score']}: {n['mapped']} of {n['arrangement']} solo measures, "
                           f"{n['filled']} orchestral measures left out")
                else:
                    how = (f"{n['mapped']} of {n['arrangement']} arrangement measures mapped, {n['filled']} orchestral "
                           f"measures filled, {n['dropped']} dropped")
                report.arrangements.append((a.rel, os.path.relpath(md, self.out), how + clock))
        if prune_orphans:
            self._retire_orphans(self.orphans(), report)
        m.save()
        return report

    def _current_output(self, e: dict) -> Optional[bytes]:
        """The entry's output as the pipeline wrote it (None if missing or changed outside the pipeline)."""
        from .convert import bytes_hash
        path = self.out_path(e["output"])
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        return data if bytes_hash(data) == e.get("output_sha256") else None

    def _resaved_outputs(self, scan: "ScanResult", todo: List[Action], results: List[dict], keep: set,
                         jobs: Optional[int]) -> Dict[str, dict]:
        """Changed scores whose current output stays: ``{rel: {"how", "compat", "parts", "pinned"}}``.

        A score saved again without changing what it renders (e.g. re-saved by MuseScore 4.7) keeps its
        output byte for byte when the new file renders to exactly that output under the entry's knobs or the
        knobs a re-save calls for (:func:`resave_candidates`); a pinned output (reproducible: false) stays
        while the score renders the same notes; ``keep`` (``build --keep``) pins the current output as it is."""
        from .calibrate import note_signature
        from .mscx import file_version, load_mscx_bytes
        m = self.manifest
        found: Dict[str, dict] = {}
        checks = []
        for a, r in zip(todo, results):
            if a.kind != "render" or a.reason != "score changed" or "error" in r or a.rel not in m.entries:
                continue
            e = m.entries[a.rel]
            cur = self._current_output(e)
            if cur is None or r["data"] == cur:
                continue
            if a.rel in keep:
                found[a.rel] = {"how": "kept as it was (--keep), now pinned", "parts": r.get("parts"), "pinned": True}
            elif e.get("reproducible") is False:
                if note_signature(r["data"]) == note_signature(cur):
                    found[a.rel] = {"how": "pinned output kept: the score still has the same notes",
                                    "parts": r.get("parts"), "pinned": True}
            else:
                path = scan.sources[a.rel].path
                cands = resave_candidates(e, file_version(load_mscx_bytes(path)))
                if cands:
                    checks.append((a.rel, path, cands, self.out_path(e["output"])))
        res = self._pool_map(_job_resaved, [(path, cands, target, self.cfg.orchestra, self.cfg.simplified,
                                             self.render_options(rel))
                                            for rel, path, cands, target in checks], jobs, "checking re-saved scores")
        for (rel, _path, _cands, _target), r in zip(checks, res):
            if r.get("compat") is not None:
                found[rel] = {"how": "output kept: " + _knobs_text(r["compat"]), "compat": r["compat"],
                              "parts": r.get("parts"), "pinned": False}
        return found

    def _restaff(self, a: Action, src: Source, r: dict, report: Report) -> None:
        """An unchanged score whose output was made under an older staff classification (``staves`` action): the
        render (with the entry's knobs) replaces the output when the score's staves have other roles now
        (:func:`pianovision.pvjson.staves_revised`) and the render differs from it; otherwise the output stays as
        it is, byte for byte, and its parts file is written again if the render's differs (vocal staves)."""
        from .pvjson import STAVES_REVISION
        e = self.manifest.entries[a.rel]
        current = self._current_output(e)
        if current is None:
            report.conflicts.append((a.rel, "output was changed outside the pipeline"))
            return
        if r.get("restaffed") and r["data"] != current:
            if e.get("reproducible") is False:
                report.conflicts.append((a.rel, "its staves are classified anew, but the renderer does not reproduce "
                                                "its pinned output (build --force replaces it)"))
                return
            self._store(a.rel, src, e["output"], r, "rendered", "staves classified anew", report, compat=a.compat)
            return
        e["staves"] = STAVES_REVISION
        report.kept += 1
        if r.get("restaffed") or not self._parts_current(e):
            self._write_parts(a.rel, r.get("parts"), current, report)

    def _keep_resaved(self, rel: str, src: Source, info: dict, report: Report) -> None:
        from .pvjson import STAVES_REVISION
        e = self.manifest.entries[rel]
        e.update(content=self._hashes.get(rel) or e["content"], mtime=src.mtime, size=src.size,
                 staves=STAVES_REVISION)
        if info["pinned"]:
            e["reproducible"] = False
        else:
            knobs = dict(info["compat"])
            if e.get("origin") == "legacy" and knobs.get("metadata") == "legacy":
                knobs.pop("metadata")                    # implied by the origin (_verify_compat)
            if knobs:
                e["compat"] = knobs
            else:
                e.pop("compat", None)
            e["reproducible"] = True
        report.resaved.append((rel, e["output"], info["how"]))
        with open(self.out_path(e["output"]), "rb") as f:
            self._write_parts(rel, info.get("parts"), f.read(), report)

    def _retire_orphans(self, names: List[str], report: Report) -> None:
        for name in names:
            self.to_attic(name, report)
            self.manifest.retired.append({"source": None, "output": name, "date": _now(), "reason": "orphan output"})
            report.retired.append(("", name, "orphan output (no score)"))

    def retire_orphans(self, names: Optional[List[str]] = None) -> Report:
        """Move orphan outputs (no score) to the attic, like ``build --prune-orphans`` without the
        build.  ``names``: only these, and only those of them that are still orphans (a list that
        was confirmed earlier never retires a file that has since got a score)."""
        current = self.orphans()
        todo = current if names is None else [n for n in current if n in set(names)]
        report = Report()
        self._attic_dir = None
        self._retire_orphans(todo, report)
        if todo:
            self.manifest.save()
        return report

    def _store(self, rel: str, src: Source, name: str, r: dict, origin: str, why: str, report: Report,
               compat: Optional[dict] = None) -> None:
        from .convert import bytes_hash
        from .pvjson import STAVES_REVISION
        path = self.out_path(name)
        data = r["data"]
        same = False
        if os.path.exists(path):
            with open(path, "rb") as f:
                same = f.read() == data
            if not same:
                self.to_attic(name, report)
        if not os.path.exists(path):
            atomic_write(path, data)
        e = {"content": self._hashes.get(rel) or self.manifest.entries.get(rel, {}).get("content"),
             "mtime": src.mtime, "size": src.size, "output": name, "output_sha256": bytes_hash(data),
             "title": r["title"], "artist": r["artist"], "default_name": r["name"], "origin": origin,
             "reproducible": True, "updated": _now(), "staves": STAVES_REVISION}
        if compat:
            e["compat"] = compat
        self.manifest.entries[rel] = e
        if same and why == "score changed":
            report.resaved.append((rel, name, "output unchanged"))      # e.g. saved again by a newer MuseScore
        else:
            report.written.append((rel, name, why))
        self._write_parts(rel, r.get("parts"), data, report)

    @staticmethod
    def _verify_compat(e: dict) -> Optional[dict]:
        """Knobs that reproduce an entry; outputs adopted from the old scripts used the old title rules."""
        c = dict(e.get("compat") or {})
        if e.get("origin") == "legacy" or e.get("metadata") == "legacy":
            c.setdefault("metadata", "legacy")
        return c or None

    def _legacy_midi_matches(self, src: Source, have: bytes) -> bool:
        """The old workflow's output: JSON converted from a MuseScore-exported .mid next to the
        score.  Trusted only when that .mid is not older than the score."""
        from .convert import convert_midi
        mid = src.path[:-5] + ".mid"
        if not os.path.exists(mid) or os.path.getmtime(mid) < src.mtime:
            return False
        try:
            return convert_midi(mid, src.path, self.cfg.orchestra, self.cfg.simplified).data == have
        except Exception:
            return False

    def _adopt(self, rel: str, src: Source, name: str, r: dict, have: bytes, how: str, reproducible: bool,
               report: Report) -> None:
        from .convert import bytes_hash
        from .pvjson import STAVES_REVISION
        self.manifest.entries[rel] = {
            "content": self._hashes[rel], "mtime": src.mtime, "size": src.size, "output": name,
            "output_sha256": bytes_hash(have), "title": r["title"], "artist": r["artist"], "default_name": r["name"],
            "origin": "legacy", "reproducible": reproducible, "updated": _now(), "staves": STAVES_REVISION}
        report.adopted.append((rel, name, how))
        self._write_parts(rel, r.get("parts"), have, report)

    def _resolve_rel(self, s: str, scan: ScanResult) -> str:
        if s in scan.sources:
            return s
        full = os.path.abspath(s)
        rel = os.path.relpath(full, self.cfg.scores).replace(os.sep, "/")
        if rel in scan.sources:
            return rel
        hits = [r for r in scan.sources if s.lower() in r.lower()]
        if len(hits) == 1:
            return hits[0]
        raise KeyError(f"no single score matches {s!r}" + (f" ({len(hits)} matches)" if hits else ""))

    # -- renaming -------------------------------------------------------------------
    def rename(self, only: Optional[List[str]] = None, dry_run: bool = False,
               restrict: Optional[set] = None) -> List[Tuple[str, str, str]]:
        """Give outputs the name their score's current title implies (after title edits).

        Outputs adopted from the old scripts keep the old naming rules unless their
        scores are named explicitly in ``only``: renaming shows up on the headset as a
        new song, so it is never done wholesale because the rules improved.
        ``restrict`` (score rels) renames only those of the scores a plain ``rename`` would."""
        from .convert import output_name
        m = self.manifest
        rels = sorted(m.entries)
        if only:
            scan = scan_library(self.cfg.scores)
            rels = [self._resolve_rel(o, scan) for o in only]
        if restrict is not None:
            rels = [r for r in rels if r in restrict]
        taken = set(m.outputs()) | set(self.output_files())
        done = []
        for rel in rels:
            e = m.entries[rel]
            path = os.path.join(self.cfg.scores, rel)
            if not os.path.exists(path):
                continue
            rules = "v2" if only else (self._verify_compat(e) or {}).get("metadata", "v2")
            want = output_name(path, rules=rules, options=self.render_options(rel))
            if want == e["output"] or want in taken:
                continue
            done.append((rel, e["output"], want))
            if not dry_run:
                os.replace(self.out_path(e["output"]), self.out_path(want))
                old_parts = self.parts_path(e["output"])
                if os.path.exists(old_parts):
                    os.replace(old_parts, self.parts_path(want))
                e.pop("parts", None)          # it names the song: the next build writes it again
                taken.discard(e["output"])
                taken.add(want)
                e["output"], e["default_name"] = want, want
        if done and not dry_run:
            m.save()
        return done

    # -- verification -----------------------------------------------------------------
    def verify(self, only: Optional[List[str]] = None, calibrate: bool = False,
               jobs: Optional[int] = None) -> List[Tuple[str, str, dict]]:
        """Re-render every managed score and compare with its pinned output, and its Note Waterfall
        parts file (result ``"parts"``: identical | differs | missing) with the parts of that render."""
        m = self.manifest
        rels = sorted(m.entries)
        if only:
            scan = scan_library(self.cfg.scores)
            rels = [self._resolve_rel(o, scan) for o in only]
        args = [(os.path.join(self.cfg.scores, rel), self._verify_compat(m.entries[rel]),
                 self.out_path(m.entries[rel]["output"]), self.cfg.orchestra, self.cfg.simplified, calibrate,
                 self.parts_path(m.entries[rel]["output"]), m.entries[rel].get("content") or "",
                 m.entries[rel] if m.entries[rel].get("reproducible") is not False else None,
                 self.render_options(rel))
                for rel in rels]
        results = self._pool_map(_job_verify, args, jobs, "verifying")
        out = []
        changed = False
        for rel, r in zip(rels, results):
            e = m.entries[rel]
            if r["status"] == "calibrated":
                e["compat"], e["reproducible"] = r["compat"], True
                changed = True
            elif r["status"] == "identical" and r.get("resaved") and calibrate:
                knobs = dict(r["resaved"])               # the knobs `build` will find for the re-saved score
                if e.get("origin") == "legacy" and knobs.get("metadata") == "legacy":
                    knobs.pop("metadata")
                e["compat"], e["reproducible"] = knobs, True
                changed = True
            elif r["status"] == "identical" and not e.get("reproducible"):
                e["reproducible"] = True
                changed = True
            elif r["status"] == "differs" and e.get("reproducible") is not False and calibrate:
                e["reproducible"] = False
                changed = True
            out.append((rel, r["status"], r))
        if changed:
            m.save()
        return out

    # -- status -------------------------------------------------------------------------
    def status(self) -> dict:
        scan = scan_library(self.cfg.scores)
        actions = self.plan(scan)
        m = self.manifest
        counts: Dict[str, int] = {}
        for a in actions:
            counts[a.kind] = counts.get(a.kind, 0) + 1
        names: Dict[str, List[str]] = {}
        for rel, e in m.entries.items():
            names.setdefault(e.get("default_name", e["output"]), []).append(rel)
        return {
            "scan": scan, "actions": actions, "counts": counts,
            "orphans": self.orphans(),
            "drift": sorted((rel, e["output"], e["default_name"]) for rel, e in m.entries.items()
                            if e.get("default_name") and e["default_name"] != e["output"]
                            and len(names.get(e["default_name"], [])) == 1),
            "pinned": sorted(rel for rel, e in m.entries.items() if e.get("reproducible") is False),
            "calibrated": sorted(rel for rel, e in m.entries.items() if e.get("compat")),
            "parts": self.parts_summary(),
        }
