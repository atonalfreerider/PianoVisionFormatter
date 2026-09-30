"""What the Piano Library window shows and does, without GTK (tested in tests/test_gui_model.py).

* :func:`snapshot`: one row per score (composer, title, output, status, simplified part / orchestra,
  whether PianoVision and Note Waterfall on the headset have the current version), orphans, strays;
* device status from ``adb devices -l`` / ``dumpsys power`` / ``df`` (:func:`pick_device` ...);
* push plans for the two apps (:func:`pianovision_plan`, :func:`waterfall_plan`) and the exact
  lists the confirmation dialogs show (:func:`removal_lines`);
* running jobs: a stdout that sends each worker thread's prints to its own log
  (:class:`ThreadStdout`), and subprocesses streamed line by line with Cancel (:func:`run_streaming`).

The library, device and hand-edit logic is the formatter's own (library.py, device.py,
handedits.py); Note Waterfall's side is its Tools/deploy_songs.py, imported for planning and run
as a program for pushing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

GUI_DIR = ".gui"                       # <output>/.gui: the GUI's own state (last build report)
BUILD_REPORT = "last_build.json"
DEFAULT_WATERFALL_SCRIPT = os.path.expanduser("~/Desktop/Note-Waterfall/Tools/deploy_songs.py")
WATERFALL_SONGS = "/sdcard/Android/data/com.atonalfreerider.notewaterfall/files/Songs"
PIANOVISION_APP = "PianoVision"
WATERFALL_APP = "Note Waterfall"

# status -> (label, css class)
STATUS = {
    "current": ("up to date", "success"),
    "new": ("new", "accent"),
    "edited": ("edited", "accent"),
    "missing": ("output missing", "accent"),
    "parts": ("parts to write", "accent"),
    "moved": ("moved", "accent"),
    "failed": ("failed", "error"),
    "pinned": ("pinned", "dim-label"),
    "conflict": ("changed outside", "warning"),
    "excluded": ("excluded", "dim-label"),
    "retire": ("to retire", "warning"),
    "invalid": ("not a score", "error"),
}
# per-app state on the headset -> label
APP_STATE = {"current": "current", "old": "old version", "missing": "not on headset", "": "—"}
NEEDS_BUILD = ("new", "edited", "missing", "parts", "moved", "failed", "retire")


# ==============================================================================
# the library
# ==============================================================================

@dataclass
class Row:
    rel: str                          # score path below the scores folder
    composer: str = ""
    title: str = ""
    output: str = ""                  # song file name ("" until built)
    status: str = "current"           # a key of STATUS
    detail: str = ""
    simplified: Optional[bool] = None # None: not known yet (not built)
    orchestra: Optional[bool] = None
    pianovision: str = ""             # a key of APP_STATE
    waterfall: str = ""
    new_name: str = ""                # the name `rename` would give (title changed)

    @property
    def status_label(self) -> str:
        return STATUS[self.status][0]

    @property
    def is_score(self) -> bool:
        return self.status not in ("excluded", "invalid", "retire")

    def matches(self, query: str) -> bool:
        """Every word of ``query`` is in the composer, title, output, score path or status."""
        hay = " ".join((self.composer, self.title, self.output, self.rel, self.status_label, self.detail)).lower()
        return all(w in hay for w in query.lower().split())


@dataclass
class DeviceFiles:
    """md5 of the files on the headset: PianoVision's files dir and Note Waterfall's Songs dir
    (None: that listing failed)."""
    pianovision: Optional[Dict[str, str]] = None
    waterfall: Optional[Dict[str, str]] = None
    error: str = ""


@dataclass
class Snapshot:
    scores_dir: str
    output_dir: str
    rows: List[Row] = field(default_factory=list)
    orphans: List[str] = field(default_factory=list)            # outputs without a score
    stray_midi: List[str] = field(default_factory=list)         # .mid without a score
    companion_midi: int = 0
    counts: Dict[str, int] = field(default_factory=dict)        # status -> rows
    parts: dict = field(default_factory=dict)                   # Library.parts_summary()
    device: Optional[DeviceFiles] = None
    build_time: str = ""

    def count(self, *statuses: str) -> int:
        return sum(self.counts.get(s, 0) for s in statuses)

    @property
    def to_build(self) -> int:
        return self.count(*NEEDS_BUILD)

    def on_device(self, app: str) -> Tuple[int, int]:
        """(current, songs) for an app, over the rows with an output."""
        key = "pianovision" if app == PIANOVISION_APP else "waterfall"
        rows = [r for r in self.rows if r.output and r.is_score]
        return sum(getattr(r, key) == "current" for r in rows), len(rows)


def read_build_report(output_dir: str) -> dict:
    try:
        with open(os.path.join(output_dir, GUI_DIR, BUILD_REPORT), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build_report_path(output_dir: str) -> str:
    return os.path.join(output_dir, GUI_DIR, BUILD_REPORT)


_md5_cache: Dict[str, Tuple[float, int, str]] = {}
_md5_lock = threading.Lock()


def md5_of(path: str) -> Optional[str]:
    """md5 of a local file, cached by (mtime, size); None when it does not exist."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _md5_lock:
        hit = _md5_cache.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    with _md5_lock:
        _md5_cache[path] = (st.st_mtime, st.st_size, h.hexdigest())
    return h.hexdigest()


def app_state(files: Sequence[Tuple[str, Optional[str]]], device: Optional[Dict[str, str]]) -> str:
    """``files``: (device file name, local md5 or None when there is no local file). current: every
    local file is on the device with that md5; missing: the first (the song) is not there; old otherwise."""
    if device is None or not files:
        return ""
    song, song_md5 = files[0]
    if song not in device:
        return "missing"
    for name, md5 in files:
        if md5 is not None and device.get(name) != md5:
            return "old"
    return "current"


def _parts_name(output: str) -> str:
    return output[:-5] + ".parts.json" if output.endswith(".json") else output + ".parts.json"


def _score_status(action, entry: Optional[dict], failed: Optional[dict], src) -> Tuple[str, str]:
    kind, reason = action.kind, action.reason
    if failed is not None and kind in ("render", "parts") and src is not None \
            and failed.get("mtime") == src.mtime and failed.get("size") == src.size:
        return "failed", failed.get("error", "")
    if kind == "keep":
        if entry and entry.get("reproducible") is False:
            return "pinned", "kept as it is: the renderer does not reproduce it exactly"
        return "current", ""
    if kind == "parts":
        return "parts", reason
    if kind == "move":
        return "moved", f"from {action.old_rel}"
    if kind == "conflict":
        return "conflict", reason + " (build --force replaces it)"
    if kind == "render":
        if reason == "new score":
            return "new", ""
        if reason == "output missing":
            return "missing", reason
        return "edited", reason
    return "current", reason


def snapshot(lib, status: Optional[dict] = None, device: Optional[DeviceFiles] = None,
             report: Optional[dict] = None, title_of: Optional[Callable[[str], Tuple[str, str]]] = None) -> Snapshot:
    """The window's model of ``lib`` (a :class:`pianovision.library.Library`).  ``status``: its
    ``lib.status()`` (computed when None); ``device``: the headset's files (None: unknown);
    ``report``: the last build report (``--report``); ``title_of(path) -> (title, composer)`` for
    scores that were never built (default: read from the score)."""
    st = status if status is not None else lib.status()
    scan = st["scan"]
    entries = lib.manifest.entries
    report = report if report is not None else read_build_report(lib.out)
    failed = {f["rel"]: f for f in report.get("failed", []) if f.get("rel")}
    drift = {rel: new for rel, _old, new in st["drift"]}
    snap = Snapshot(scores_dir=lib.cfg.scores, output_dir=lib.out, orphans=list(st["orphans"]),
                    stray_midi=list(scan.stray_midi), companion_midi=len(scan.companion_midi),
                    parts=st.get("parts") or {}, device=device, build_time=report.get("time", ""))
    pv = device.pianovision if device else None
    nw = device.waterfall if device else None
    if title_of is None:
        title_of = _read_title

    for a in st["actions"]:
        e = entries.get(a.rel) if a.kind != "move" else entries.get(a.old_rel)
        src = scan.sources.get(a.rel)
        if a.kind == "retire":
            stem = a.rel[:-5]
            excluded = "excluded" in a.reason
            row = Row(rel=(stem + ".zip") if excluded else a.rel, status="excluded" if excluded else "retire",
                      detail=f"{a.reason}; the next build moves {e['output'] if e else 'its song'} to the attic",
                      output=e["output"] if e else "")
        else:
            status, detail = _score_status(a, e, failed.get(a.rel), src)
            row = Row(rel=a.rel, status=status, detail=detail)
            if e:
                row.output = e["output"]
                row.title, row.composer = e.get("title", ""), e.get("artist", "")
                p = e.get("parts")
                if p:
                    row.simplified = bool(p.get("simplified"))
                    row.orchestra = p.get("notes", {}).get("orchestra", 0) > 0
                if a.rel in drift:
                    row.new_name = drift[a.rel]
            elif src is not None:
                try:
                    row.title, row.composer = title_of(src.path)
                except Exception as ex:                     # a score that cannot be read still gets a row
                    row.detail = row.detail or f"cannot read the title: {ex}"
        if row.title == "" and row.status in ("excluded", "retire") and e:
            row.title, row.composer = e.get("title", ""), e.get("artist", "")
        if row.output and row.status not in ("excluded", "retire"):
            out = lib.out_path(row.output)
            song_md5 = md5_of(out)
            if song_md5 is not None:
                row.pianovision = app_state([(row.output, song_md5)], pv)
                parts = os.path.join(lib.out, "NoteWaterfall", _parts_name(row.output))
                row.waterfall = app_state([(row.output, song_md5), (_parts_name(row.output), md5_of(parts))], nw)
        snap.rows.append(row)

    known = {r.rel for r in snap.rows}
    for rel in scan.excluded:
        if rel not in known:
            name = os.path.basename(rel)
            snap.rows.append(Row(rel=rel, status="excluded", title=os.path.splitext(name)[0],
                                 detail="renamed to .zip: not part of the library"))
    for rel, why in sorted(scan.invalid.items()):
        snap.rows.append(Row(rel=rel, status="invalid", title=os.path.splitext(os.path.basename(rel))[0], detail=why))
    for r in snap.rows:
        snap.counts[r.status] = snap.counts.get(r.status, 0) + 1
    snap.rows.sort(key=lambda r: (not r.is_score, r.composer.lower(), r.title.lower(), r.rel.lower()))
    return snap


def _read_title(path: str) -> Tuple[str, str]:
    from .convert import score_root
    from .metadata import extract_title_artist
    return extract_title_artist(score_root(path), path)


def summary_lines(snap: Snapshot) -> List[str]:
    c = snap.counts
    scores = sum(v for k, v in c.items() if k not in ("excluded", "invalid", "retire"))
    first = f"{scores} scores: {c.get('current', 0) + c.get('pinned', 0)} up to date"
    extra = [(k, STATUS[k][0]) for k in ("new", "edited", "missing", "parts", "moved", "failed", "conflict", "retire")]
    first += "".join(f", {c[k]} {label}" for k, label in extra if c.get(k))
    second = []
    if c.get("pinned"):
        second.append(f"{c['pinned']} pinned")
    if c.get("excluded"):
        second.append(f"{c['excluded']} excluded")
    if c.get("invalid"):
        second.append(f"{c['invalid']} not scores")
    p = snap.parts
    if p:
        second.append(f"{p.get('simplified', 0)} with a simplified part, {p.get('orchestra', 0)} with orchestra")
    if snap.orphans or snap.stray_midi:
        second.append(f"{len(snap.orphans)} orphan songs, {len(snap.stray_midi)} stray .mid files")
    return [first, "; ".join(second)]


# ==============================================================================
# the headset
# ==============================================================================

@dataclass
class DeviceState:
    state: str                     # no-adb | none | unauthorized | offline | several | connected
    serial: str = ""
    model: str = ""
    wakefulness: str = ""          # Awake | Asleep | Dozing | Dreaming ("" unknown)
    free_bytes: Optional[int] = None
    message: str = ""

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def label(self) -> str:
        if self.state == "connected":
            s = f"{self.model or 'Headset'} connected"
            if self.wakefulness == "Asleep":
                s += " (asleep)"
            elif self.wakefulness in ("Awake", "Dreaming"):
                s += " (awake: someone may be wearing it)"
            if self.free_bytes is not None:
                s += f" · {human_bytes(self.free_bytes)} free"
            return s
        return {"no-adb": "adb not found", "none": "No headset connected",
                "unauthorized": "Headset unauthorized: put it on and allow USB debugging",
                "offline": "Headset offline (replug the cable)", "several": "Several devices attached",
                "disabled": "Device checks off"}.get(self.state, self.message or self.state)


def pick_device(devs: Sequence[Tuple[str, str, str]], serial: str = "") -> DeviceState:
    """From ``adb devices -l`` rows (serial, state, description): the configured serial, else the
    only device (a Quest when several), as :meth:`pianovision.device.Adb.connect` chooses."""
    def model(desc: str) -> str:
        for kv in desc.split():
            if kv.startswith("model:"):
                return kv[6:].replace("_", " ")
        return ""
    if serial:
        match = [d for d in devs if d[0] == serial]
        if not match:
            return DeviceState("none", serial=serial, message=f"device {serial} is not connected")
        d = match[0]
    else:
        ready = [d for d in devs if d[1] == "device"]
        if len(ready) > 1:
            quests = [d for d in ready if "Quest" in d[2]]
            if len(quests) != 1:
                return DeviceState("several", message=", ".join(d[0] for d in ready))
            ready = quests
        if ready:
            d = ready[0]
        elif devs:
            d = devs[0]
        else:
            return DeviceState("none")
    state = {"device": "connected", "unauthorized": "unauthorized"}.get(d[1], "offline")
    return DeviceState(state, serial=d[0], model=model(d[2]))


def parse_devices(text: str) -> List[Tuple[str, str, str]]:
    devs = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devs.append((parts[0], parts[1], " ".join(parts[2:])))
    return devs


def parse_wakefulness(text: str) -> str:
    m = re.search(r"mWakefulness=(\w+)", text)
    return m.group(1) if m else ""


def parse_df(text: str) -> Optional[int]:
    """Available bytes from ``df -k <dir>`` (the last line's 4th column, in 1K blocks)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    if len(parts) < 4:
        return None
    try:
        return int(parts[3]) * 1024
    except ValueError:
        return None


def human_bytes(n: Optional[float]) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} TB"


def probe_device(adb: str = "adb", serial: str = "", details: bool = True, timeout: float = 8) -> DeviceState:
    """Polls adb (read only): which device, and when connected whether it sleeps and free space."""
    exe = shutil.which(adb) if os.sep not in adb else (adb if os.access(adb, os.X_OK) else None)
    if not exe:
        return DeviceState("no-adb")
    try:
        out = subprocess.run([exe, "devices", "-l"], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return DeviceState("none", message=str(e))
    st = pick_device(parse_devices(out), serial)
    if st.connected and details:
        base = [exe, "-s", st.serial, "shell"]
        try:
            r = subprocess.run(base + ["dumpsys power | grep mWakefulness="], capture_output=True, text=True,
                               timeout=timeout)
            st.wakefulness = parse_wakefulness(r.stdout)
            r = subprocess.run(base + ["df -k /sdcard"], capture_output=True, text=True, timeout=timeout)
            st.free_bytes = parse_df(r.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return st


def waterfall_songs_dir(cfg) -> str:
    """Note Waterfall's Songs folder on the headset (next to [hands] device_dir)."""
    base = (getattr(cfg, "hands_dir", "") or "").rstrip("/")
    return posixpath.join(posixpath.dirname(base), "Songs") if base else WATERFALL_SONGS


def device_files(lib, serial: str) -> DeviceFiles:
    """md5s of the songs on the headset, for both apps (read only)."""
    from .device import Adb, DeviceError
    adb = Adb(lib.cfg.adb, serial)
    res = DeviceFiles()
    errors = []
    for attr, d in (("pianovision", lib.cfg.device_dir), ("waterfall", waterfall_songs_dir(lib.cfg))):
        try:
            setattr(res, attr, adb.md5s(d))
        except DeviceError as e:
            errors.append(f"{d}: {e}")
    res.error = "; ".join(errors)
    return res


# ==============================================================================
# pushing: plans and confirmation lists
# ==============================================================================

@dataclass
class AppPlan:
    app: str
    target: str                                            # folder on the headset
    push: List[Tuple[str, int]] = field(default_factory=list)   # (file name, bytes)
    remove: List[str] = field(default_factory=list)        # files this tool deployed that left the library
    unchanged: int = 0
    foreign: List[str] = field(default_factory=list)       # never touched
    error: str = ""
    raw: object = None                                     # DeployPlan (PianoVision) / wanted dict (Note Waterfall)

    @property
    def push_bytes(self) -> int:
        return sum(n for _f, n in self.push)

    def summary(self) -> str:
        if self.error:
            return f"{self.app}: {self.error}"
        s = f"{self.app}: {len(self.push)} file(s) to push ({human_bytes(self.push_bytes)}), {self.unchanged} current"
        if self.remove:
            s += f", {len(self.remove)} no longer in the library"
        return s


def pianovision_plan(lib, adb, force: bool = False) -> AppPlan:
    """PianoVision's push plan (:func:`pianovision.device.plan_deploy`)."""
    from .device import DeviceError, plan_deploy
    plan = AppPlan(PIANOVISION_APP, lib.cfg.device_dir)
    try:
        dp = plan_deploy(lib, adb, force=force)
    except DeviceError as e:
        plan.error = str(e)
        return plan
    plan.raw = dp
    plan.push = [(n, _size(lib.out_path(n))) for n in dp.push]
    plan.remove = list(dp.remove)
    plan.unchanged = len(dp.unchanged)
    plan.foreign = list(dp.foreign)
    return plan


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def waterfall_script(cfg) -> str:
    """Note Waterfall's Tools/deploy_songs.py: the .py of [hands] waterfall_deploy, else the default."""
    cmd = getattr(cfg, "waterfall_deploy", "") or ""
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = []
    for w in words:
        if w.endswith(".py"):
            return os.path.expanduser(w)
    return DEFAULT_WATERFALL_SCRIPT


def waterfall_command(cfg) -> List[str]:
    """The command that runs deploy_songs.py ([hands] waterfall_deploy without options)."""
    script = waterfall_script(cfg)
    try:
        words = shlex.split(getattr(cfg, "waterfall_deploy", "") or "")
    except ValueError:
        words = []
    if script in [os.path.expanduser(w) for w in words]:
        i = [os.path.expanduser(w) for w in words].index(script)
        return words[:i] + [script]
    return [sys.executable, script]


_nw_modules: Dict[str, object] = {}


def load_waterfall(script: str):
    """Imports deploy_songs.py (its planning functions: desired_files, device_md5s, ...)."""
    script = os.path.abspath(script)
    if script in _nw_modules:
        return _nw_modules[script]
    if not os.path.isfile(script):
        raise FileNotFoundError(f"Note Waterfall's deploy script not found: {script}")
    spec = importlib.util.spec_from_file_location("notewaterfall_deploy_songs", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _nw_modules[script] = mod
    return mod


def waterfall_plan(mod, library: str, serial: str) -> AppPlan:
    """Note Waterfall's push plan, computed the way deploy_songs.py computes it (a full deploy:
    songs, parts files, audio, catalog overrides; ``remove`` = what ``--prune`` would delete)."""
    plan = AppPlan(WATERFALL_APP, getattr(mod, "DEVICE_SONGS", WATERFALL_SONGS))
    try:
        adb = mod.Adb(serial)
        adb.connect()
        want = mod.desired_files(library, mod.load_audio_map(mod.DEFAULT_AUDIO_MAP), None, False, True)
        on_device = mod.device_md5s(adb, create=False)
        before = mod.read_device_manifest(adb)
    except Exception as e:                                  # DeployError, OSError, ...
        plan.error = str(e) or type(e).__name__
        return plan
    plan.raw = want
    for name, (kind, src) in sorted(want.items()):
        local = mod.md5_file(src) if kind == "path" else mod.md5_bytes(src)
        if on_device.get(name) != local:
            plan.push.append((name, _size(src) if kind == "path" else len(src)))
        else:
            plan.unchanged += 1
    plan.remove = waterfall_stale(want, on_device, before)
    plan.foreign = sorted(n for n in on_device if n not in want and n not in before and n != mod.MANIFEST_NAME)
    return plan


def waterfall_stale(want: Dict[str, object], on_device: Dict[str, str], deployed_before) -> List[str]:
    """deploy_songs.py's "stale": files it deployed earlier that are no longer wanted (--prune deletes them)."""
    return sorted((set(deployed_before) - set(want)) & set(on_device))


def waterfall_argv(cfg, library: str, serial: str, prune: bool) -> List[str]:
    argv = waterfall_command(cfg) + ["--library", library]
    if serial:
        argv += ["--serial", serial]
    if prune:
        argv += ["--prune", "--yes"]                       # the GUI asked, listing exactly these files
    return argv


def push_lines(plan: AppPlan, limit: int = 0) -> List[str]:
    items = [f"{n}  ({human_bytes(b)})" for n, b in plan.push]
    if limit and len(items) > limit:
        items = items[:limit] + [f"... and {len(items) - limit} more"]
    return items


def removal_lines(plans: Sequence[AppPlan], prune_apps: Sequence[str]) -> List[str]:
    """Exactly the headset files a push with ``prune_apps`` deletes, as "App: folder/file"."""
    out = []
    for p in plans:
        if p.app in prune_apps and not p.error:
            out += [f"{p.app}: {p.target.rstrip('/')}/{n}" for n in p.remove]
    return out


def orphan_lines(output_dir: str, names: Sequence[str]) -> List[str]:
    """What `Retire orphans` moves, and where to."""
    return [f"{os.path.join(output_dir, n)}  ->  .attic/<date>/{n}" for n in names]


# ==============================================================================
# rename, MuseScore, hand-edit reviews
# ==============================================================================

def rename_candidates(lib) -> List[Tuple[str, str, str, bool]]:
    """(score, current name, new name, title changed) for what `rename` would apply.  "title changed":
    the last build saw a title that names the song differently (the usual reason to rename); the
    others only follow newer naming rules, and the dialog leaves them unticked."""
    drift = {rel for rel, _o, _n in lib.status()["drift"]}
    return [(rel, old, new, rel in drift) for rel, old, new in lib.rename(dry_run=True)]


def find_musescore() -> Optional[str]:
    for name in ("mscore4portable", "musescore4portable", "mscore", "mscore4", "musescore", "musescore4"):
        p = shutil.which(name)
        if p:
            return p
    for p in (os.path.expanduser("~/.local/bin/mscore4portable"), os.path.expanduser("~/.local/bin/mscore")):
        if os.access(p, os.X_OK):
            return p
    return None


@dataclass
class ReviewLine:
    where: str            # "m12 beat 3"
    pitch: str            # "E4"
    from_hand: str
    to_hand: str
    status: str           # move | already | skip | conflict | stale
    reason: str = ""


def review_lines(rv) -> List[ReviewLine]:
    """A hand-edit review (handedits.ScoreReview) as rows: measure, pitch, from -> to, status."""
    from .handedits import note_name
    out = []
    order = {"move": 0, "conflict": 1, "skip": 2, "stale": 3, "already": 4}
    for r in sorted(rv.results, key=lambda r: (order.get(r.status, 9), r.edit.ticks, r.edit.midi)):
        e = r.edit
        out.append(ReviewLine(where=r.where or f"tick {e.ticks}", pitch=note_name(e.midi), from_hand=e.from_hand,
                              to_hand=e.to_hand, status=r.status, reason=r.reason))
    return out


class ReviewGate:
    """The ``ask`` of :func:`pianovision.handedits.apply_reviews` for a window: each question is
    matched to its review (the ready ones, in order) and answered by ``confirm(review) -> bool``,
    which the GUI implements as a dialog per score.  Anything unexpected is a "no"."""

    def __init__(self, reviews, confirm: Callable[[object], bool]):
        self.pending = [rv for rv in reviews if rv.ready]
        self.confirm = confirm
        self.asked: List[str] = []

    def __call__(self, question: str) -> str:
        if not self.pending:
            return "n"
        rv = self.pending.pop(0)
        self.asked.append(rv.rel)
        if rv.rel not in question:
            return "n"
        return "y" if self.confirm(rv) else "n"


# ==============================================================================
# jobs: logs and subprocesses
# ==============================================================================

class ThreadStdout(io.TextIOBase):
    """sys.stdout replacement: text printed by a thread with a sink goes to that sink (the
    job's log pane), everything else to the real stdout."""

    def __init__(self, real):
        self.real = real
        self._local = threading.local()

    def set_sink(self, sink: Optional[Callable[[str], None]]) -> None:
        self._local.sink = sink

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        sink = getattr(self._local, "sink", None)
        if sink is not None:
            sink(s)
        elif self.real is not None:
            self.real.write(s)
        return len(s)

    def flush(self) -> None:
        if getattr(self._local, "sink", None) is None and self.real is not None:
            self.real.flush()

    def isatty(self) -> bool:
        return False


class Cancelled(Exception):
    pass


class CancelToken:
    def __init__(self):
        self.event = threading.Event()
        self.procs: List[subprocess.Popen] = []
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def cancel(self) -> None:
        self.event.set()
        with self._lock:
            procs = list(self.procs)
        for p in procs:
            _terminate(p)

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled()


def _terminate(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)          # the build's render workers too
    except (ProcessLookupError, PermissionError):
        try:
            p.terminate()
        except ProcessLookupError:
            pass


def run_streaming(argv: Sequence[str], line: Callable[[str], None], cancel: Optional[CancelToken] = None,
                  cwd: Optional[str] = None, env: Optional[dict] = None) -> int:
    """Runs ``argv`` (stdout and stderr merged), passing each output line to ``line``; returns the
    exit code (negative when cancelled/killed).  Cancel terminates the whole process group."""
    e = dict(os.environ if env is None else env)
    e["PYTHONUNBUFFERED"] = "1"
    p = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         text=True, bufsize=1, cwd=cwd, env=e, start_new_session=True, errors="replace")
    if cancel is not None:
        with cancel._lock:
            cancel.procs.append(p)
        if cancel.cancelled:
            _terminate(p)
    try:
        with p.stdout:
            for ln in p.stdout:
                line(ln.rstrip("\n"))
        return p.wait()
    finally:
        if cancel is not None:
            with cancel._lock:
                if p in cancel.procs:
                    cancel.procs.remove(p)


class Result:
    """What :func:`run_streaming` returns, shaped like subprocess.CompletedProcess (for cli.hands_callbacks)."""

    def __init__(self, returncode: int):
        self.returncode = returncode


def formatter_argv(args, *cmd: str) -> List[str]:
    """``python3 -m pianovision [--config/--scores/--output as given to the GUI] <cmd...>``."""
    argv = [sys.executable, "-m", "pianovision"]
    for opt in ("config", "scores", "output"):
        v = getattr(args, opt, None)
        if v:
            argv += [f"--{opt}", os.path.abspath(os.path.expanduser(v))]
    return argv + list(cmd)


def package_root() -> str:
    """The folder that holds the pianovision package (cwd for ``python -m pianovision``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
