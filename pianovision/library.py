"""The score library pipeline: MuseScore scores folder -> PianoVision JSON folder.

State lives in ``<output>/.manifest.json``.  Every output file is pinned to the
content of the score it came from:

* unchanged score            -> its JSON is left alone (byte-for-byte),
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
CONFIG_NAME = "pianovision.toml"
DEFAULT_DEVICE_DIR = "/sdcard/Android/data/com.ZarApps.PianoVision/files"


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
    cfg = Config(scores=p(lib.get("scores", "")), output=p(lib.get("output", "PianoVision")),
                 device_dir=dev.get("dir", DEFAULT_DEVICE_DIR), serial=dev.get("serial", ""),
                 adb=dev.get("adb", "adb"), orchestra=conv.get("orchestra", True),
                 simplified=conv.get("simplified", True), jobs=int(conv.get("jobs", 0)), path=cfg_path or "")
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

def _job_render(path: str, compat: Optional[dict], orchestra: bool, simplified: bool) -> dict:
    from .convert import convert_mscz
    try:
        c = convert_mscz(path, Compat.from_dict(compat), orchestra, simplified)
        return {"title": c.title, "artist": c.artist, "name": c.name, "data": c.data}
    except Exception as e:                                   # a broken score must not stop the build
        return {"error": f"{type(e).__name__}: {e}"}


def _job_verify(path: str, compat: Optional[dict], target: str, orchestra: bool, simplified: bool,
                do_calibrate: bool) -> dict:
    from .calibrate import calibrate, note_signature
    from .convert import convert_mscz
    try:
        with open(target, "rb") as f:
            want = f.read()
        got = convert_mscz(path, Compat.from_dict(compat), orchestra, simplified).data
        if got == want:
            return {"status": "identical"}
        same_notes = note_signature(got) == note_signature(want)
        if do_calibrate:
            c = calibrate(path, want, orchestra, simplified)
            if c is not None:
                return {"status": "calibrated", "compat": c.to_dict()}
        return {"status": "differs", "same_notes": same_notes}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------------------
# the pipeline
# ------------------------------------------------------------------------------

@dataclass
class Action:
    kind: str                 # keep | render | move | retire | conflict
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
        shutil.move(src, dst)
        report.attic.append(os.path.relpath(dst, self.out))

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
    def plan(self, scan: ScanResult, force: bool = False, only: Optional[List[str]] = None) -> List[Action]:
        from .convert import content_hash, file_hash
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
                actions.append(Action("render", rel, "score changed"))
            elif not os.path.exists(out):
                actions.append(Action("render", rel, "output missing", compat=e.get("compat")))
            elif file_hash(out) != e["output_sha256"]:
                actions.append(Action("conflict", rel, "output was changed outside the pipeline"))
            else:
                actions.append(Action("keep", rel))
        for rel in sorted(gone):
            stem = os.path.join(self.cfg.scores, rel)[:-5]
            why = "excluded (renamed to .zip)" if os.path.exists(stem + ".zip") else "score deleted"
            actions.append(Action("retire", rel, why))
        return actions

    # -- build --------------------------------------------------------------------
    def build(self, force: bool = False, only: Optional[List[str]] = None, dry_run: bool = False,
              prune_orphans: bool = False, jobs: Optional[int] = None) -> Report:
        from .calibrate import note_signature
        from .convert import bytes_hash, file_hash
        scan = scan_library(self.cfg.scores)
        if only:
            only = [self._resolve_rel(o, scan) for o in only]
        actions = self.plan(scan, force=force, only=only)
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
                m.retired.append({"source": a.rel, "output": e["output"], "date": _now(), "reason": a.reason})
                report.retired.append((a.rel, e["output"], a.reason))
            elif a.kind == "keep":
                src, e = scan.sources[a.rel], m.entries[a.rel]
                e.update(mtime=src.mtime, size=src.size)

        todo = [a for a in actions if a.kind == "render"]
        results = self._pool_map(_job_render, [(scan.sources[a.rel].path, a.compat, self.cfg.orchestra,
                                                self.cfg.simplified) for a in todo], jobs, "rendering")
        taken = set(m.outputs())
        new_by_name: Dict[str, List[Tuple[Action, dict]]] = {}
        for a, r in zip(todo, results):
            if "error" in r:
                report.failed.append((a.rel, r["error"]))
                continue
            if a.rel in m.entries:
                self._store(a.rel, scan.sources[a.rel], m.entries[a.rel]["output"], r, "rendered", a.reason,
                            report, compat=a.compat)
            else:
                new_by_name.setdefault(r["name"], []).append((a, r))

        # New scores: adopt an existing (legacy) output of the same name when it still shows the
        # same notes; resolve name collisions between scores.
        for name, group in sorted(new_by_name.items()):
            existing = self.out_path(name)
            owner_rel = None
            if name not in taken and os.path.exists(existing):
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
                    self._adopt(a.rel, scan.sources[a.rel], name, r, have, how, reproducible=bool(exact),
                                report=report)
                    owner_rel = a.rel
                    taken.add(name)
                    group = [g for g in group if g[0].rel != a.rel]
            for a, r in sorted(group, key=lambda g: g[0].rel):
                target = name
                if target in taken:
                    rival = owner_rel or m.owner_of(target) or ""
                    target = self._disambiguate(name, a.rel, rival, taken | set(self.output_files()))
                taken.add(target)
                owner_rel = owner_rel or a.rel
                self._store(a.rel, scan.sources[a.rel], target, r, "rendered", a.reason, report)

        if prune_orphans:
            for name in self.orphans():
                self.to_attic(name, report)
                m.retired.append({"source": None, "output": name, "date": _now(), "reason": "orphan output"})
                report.retired.append(("", name, "orphan output (no score)"))
        m.save()
        return report

    def _store(self, rel: str, src: Source, name: str, r: dict, origin: str, why: str, report: Report,
               compat: Optional[dict] = None) -> None:
        from .convert import bytes_hash
        path = self.out_path(name)
        data = r["data"]
        if os.path.exists(path):
            with open(path, "rb") as f:
                if f.read() != data:
                    self.to_attic(name, report)
        if not os.path.exists(path):
            atomic_write(path, data)
        e = {"content": self._hashes.get(rel) or self.manifest.entries.get(rel, {}).get("content"),
             "mtime": src.mtime, "size": src.size, "output": name, "output_sha256": bytes_hash(data),
             "title": r["title"], "artist": r["artist"], "default_name": r["name"], "origin": origin,
             "reproducible": True, "updated": _now()}
        if compat:
            e["compat"] = compat
        self.manifest.entries[rel] = e
        report.written.append((rel, name, why))

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
        self.manifest.entries[rel] = {
            "content": self._hashes[rel], "mtime": src.mtime, "size": src.size, "output": name,
            "output_sha256": bytes_hash(have), "title": r["title"], "artist": r["artist"], "default_name": r["name"],
            "origin": "legacy", "reproducible": reproducible, "updated": _now()}
        report.adopted.append((rel, name, how))

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
    def rename(self, dry_run: bool = False) -> List[Tuple[str, str, str]]:
        """Give outputs the name their score's current title implies (after title edits)."""
        from .convert import output_name
        m = self.manifest
        taken = set(m.outputs()) | set(self.output_files())
        done = []
        for rel, e in sorted(m.entries.items()):
            path = os.path.join(self.cfg.scores, rel)
            if not os.path.exists(path):
                continue
            want = output_name(path)
            if want == e["output"] or want in taken:
                continue
            done.append((rel, e["output"], want))
            if not dry_run:
                os.replace(self.out_path(e["output"]), self.out_path(want))
                taken.discard(e["output"])
                taken.add(want)
                e["output"], e["default_name"] = want, want
        if done and not dry_run:
            m.save()
        return done

    # -- verification -----------------------------------------------------------------
    def verify(self, only: Optional[List[str]] = None, calibrate: bool = False,
               jobs: Optional[int] = None) -> List[Tuple[str, str, dict]]:
        """Re-render every managed score and compare with its pinned output."""
        m = self.manifest
        rels = sorted(m.entries)
        if only:
            scan = scan_library(self.cfg.scores)
            rels = [self._resolve_rel(o, scan) for o in only]
        args = [(os.path.join(self.cfg.scores, rel), m.entries[rel].get("compat"),
                 self.out_path(m.entries[rel]["output"]), self.cfg.orchestra, self.cfg.simplified, calibrate)
                for rel in rels]
        results = self._pool_map(_job_verify, args, jobs, "verifying")
        out = []
        changed = False
        for rel, r in zip(rels, results):
            e = m.entries[rel]
            if r["status"] == "calibrated":
                e["compat"], e["reproducible"] = r["compat"], True
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
        }
