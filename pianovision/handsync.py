"""Automatic hand-edit sync: while the headset is connected, the hands recorded with Note Waterfall's
HAND REC go into the MuseScore scores by themselves when their review is clean
(``python -m pianovision hands sync``; ``desktop/nw-handsync.service`` runs it at login).

    detection  ``adb devices -l`` every ``poll`` seconds.  Each poll is its own adb call, so an adb server
               that was killed or restarted (Unity's Android builds do that) is simply started again, and the
               headset's offline / unauthorized / several-devices states come from the same list.
    listing    on connect, then every ``interval`` seconds while connected: the md5 of every
               ``HandEdits/*.hands.json`` on the headset (one ``adb shell``; not ``applied/``)
    review     sidecars that are new or changed (name + md5), or whose score, song or build options changed
               since their last review: pulled into the inbox and reviewed (:func:`handedits.review_song`)
    apply      only a clean review: verified, and no conflict, skipped, stale or unreadable edit and no error
               for that score; :func:`handedits.apply_reviews` with ``yes`` and the deploy and archive steps
               of ``hands apply`` (backup to the attic, overwrite, build, deploy to both apps, archive).
               Never while MuseScore may have the score open, and only with ``min_free_mb`` of memory
               available (the review and the build render the score).
    once       a ledger of the (sidecar name, md5) written into the scores: a sidecar in it is not reviewed
               for applying again, only deployed and archived if it is still on the headset; and a review
               reads edits that are already in the score as "already", never as a move
    the rest   stay in the inbox and on the headset; a desktop notification (``notify-send``) says why,
               once per version of the sidecar

Everything the sync does goes to ``<sync_dir>/sync.log`` (default ``~/.cache/nw-handsync``), with
``state.json`` (the ledger, each sidecar's status), ``status.json`` (for the Piano Library window),
``sync.lock`` (one pass at a time, also against ``hands sync --once``) and ``daemon.lock`` (one daemon).
``--dry-run`` lists and reviews (pulled into a temporary folder) and writes nothing.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import handedits as H

DEFAULT_SYNC_DIR = "~/.cache/nw-handsync"
DEFAULT_INTERVAL = 45.0                 # seconds between looks at HandEdits/ while connected
DEFAULT_POLL = 10.0                     # seconds between `adb devices -l`
DEFAULT_MIN_FREE_MB = 4096              # MemAvailable needed before a review or a build
MUSESCORE_RECENT_FILES = "~/.local/share/MuseScore/MuseScore4/recent_files.json"
RECENT_WINDOW = 10                      # recent files a running MuseScore may still have open
FINISH_RETRY_S = 600.0                  # an applied sidecar that could not be archived: next try
LOG_MAX_BYTES = 5 << 20
ICON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop", "piano-library.svg")
SETTLED = ("needs-review",)             # statuses kept until the sidecar, its score, song or options change


# ==============================================================================
# settings
# ==============================================================================

@dataclass
class Settings:
    sync_dir: str
    device_dir: str = H.DEFAULT_DEVICE_DIR
    inbox: str = ""
    serial: str = ""
    interval: float = DEFAULT_INTERVAL
    poll: float = DEFAULT_POLL
    min_free_mb: int = DEFAULT_MIN_FREE_MB
    dry_run: bool = False
    once: bool = False
    notify: bool = True
    deploy: bool = True
    archive: bool = True
    all_repeats: bool = False
    waterfall_deploy: Optional[str] = None   # None: [hands] waterfall_deploy
    recent_files: str = MUSESCORE_RECENT_FILES
    proc: str = "/proc"

    def path(self, name: str) -> str:
        return os.path.join(self.sync_dir, name)


def load_settings(cfg, **overrides) -> Settings:
    """[hands] sync_dir, sync_interval, min_free_mb of pianovision.toml; ``overrides`` (not None) win."""
    hands: dict = {}
    if getattr(cfg, "path", ""):
        import tomllib
        try:
            with open(cfg.path, "rb") as f:
                hands = tomllib.load(f).get("hands", {})
        except (OSError, ValueError):
            hands = {}
    base = os.path.dirname(cfg.path) if getattr(cfg, "path", "") else os.getcwd()

    def p(v: str) -> str:
        return os.path.normpath(os.path.join(base, os.path.expanduser(v)))

    s = Settings(sync_dir=p(hands.get("sync_dir", DEFAULT_SYNC_DIR)), device_dir=cfg.hands_dir,
                 inbox=os.path.join(cfg.output, ".hands", "inbox"), serial=cfg.serial,
                 interval=float(hands.get("sync_interval", DEFAULT_INTERVAL)),
                 min_free_mb=int(hands.get("min_free_mb", DEFAULT_MIN_FREE_MB)))
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, os.path.abspath(os.path.expanduser(v)) if k in ("sync_dir", "inbox", "recent_files") else v)
    return s


# ==============================================================================
# what a review means for the sync
# ==============================================================================

def _n(n: int, one: str, many: str = "") -> str:
    """``_n(1, "edit conflicts", "edits conflict")`` -> "1 edit conflicts"."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


@dataclass
class Verdict:
    kind: str           # apply | already | review
    summary: str


def classify(rv: H.ScoreReview) -> Verdict:
    """``apply``: verified, every edit moves or is already in the score; ``already``: nothing to write (every
    edit is in the score already, or the sidecar has none); ``review``: anything else, for the user."""
    errors = [f"{sc.name}: {sc.error}" for sc in rv.sidecars if sc.error]
    if errors:
        return Verdict("review", "cannot be read (" + "; ".join(errors) + ")")
    unreadable = sum(sc.unreadable for sc in rv.sidecars)
    if rv.error == "no edits" and not unreadable:
        return Verdict("already", "no edits in it")
    if rv.error:
        return Verdict("review", rv.error)
    c = Counter(r.status for r in rv.results)
    problems = []
    if c["conflict"]:
        problems.append(_n(c["conflict"], "edit conflicts", "edits conflict"))
    if c["skip"]:
        problems.append(_n(c["skip"], "edit") + " cannot be written safely")
    if c["stale"]:
        problems.append(_n(c["stale"], "edit no longer matches the song", "edits no longer match the song"))
    if unreadable:
        problems.append(_n(unreadable, "edit") + " could not be read")
    if problems:
        return Verdict("review", ", ".join(problems))
    if rv.ready:
        return Verdict("apply", f"{_n(rv.moved_notes, 'note')} moved to the other hand ({rv.to_right} to the right, "
                                f"{rv.to_left} to the left)")
    if rv.results and c["already"] == len(rv.results):
        return Verdict("already", "every edit is in the score already")
    return Verdict("review", "the review found nothing it could verify")


# ==============================================================================
# MuseScore: is the score open?
# ==============================================================================

_MUSESCORE = re.compile(r"(?i)mscore|musescore")
_HEADLESS_ARGS = ("-o", "-j", "--job", "--export-to", "--score-", "--source-update", "--diagnostic")


@dataclass
class MuseProc:
    pid: int
    argv: List[str]
    cwd: str = ""
    started: Optional[float] = None     # epoch seconds
    interactive: bool = True            # a window, not a converter run (-o, -j, offscreen)


def _read(path: str, mode: str = "r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return None


def musescore_processes(proc: str = "/proc") -> List[MuseProc]:
    """Running MuseScore processes (the AppImage's runtime and its child both count)."""
    out: List[MuseProc] = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return out
    btime = None
    for line in (_read(os.path.join(proc, "stat")) or "").splitlines():
        if line.startswith("btime "):
            btime = float(line.split()[1])
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        hz = 100
    for pid in pids:
        base = os.path.join(proc, pid)
        raw = _read(os.path.join(base, "cmdline"), "rb")
        if not raw:
            continue
        argv = [a.decode("utf-8", "surrogateescape") for a in raw.split(b"\0") if a]
        if not argv:
            continue
        names = [os.path.basename(argv[0]), (_read(os.path.join(base, "comm")) or "").strip()]
        try:
            names.append(os.path.basename(os.readlink(os.path.join(base, "exe"))))
        except OSError:
            pass
        if not any(_MUSESCORE.search(n) for n in names if n):
            continue
        try:
            cwd = os.readlink(os.path.join(base, "cwd"))
        except OSError:
            cwd = ""
        started = None
        stat = _read(os.path.join(base, "stat")) or ""
        fields = stat[stat.rfind(")") + 2:].split() if ")" in stat else []
        if btime is not None and len(fields) > 19 and fields[19].isdigit():
            started = btime + int(fields[19]) / hz
        env = (_read(os.path.join(base, "environ"), "rb") or b"").split(b"\0")
        headless = any(a == h or (h.endswith("-") or h.startswith("--")) and a.startswith(h)
                       for a in argv[1:] for h in _HEADLESS_ARGS) or b"QT_QPA_PLATFORM=offscreen" in env
        out.append(MuseProc(int(pid), argv, cwd, started, not headless))
    return out


def autosave_files(path: str) -> List[str]:
    """MuseScore's autosave (MuseScore 4: ``<score>.autosave`` next to it) or MuseScore 3's ``.<score>,`` for
    this score: it has unsaved changes in an open MuseScore (or one that crashed)."""
    d, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    try:
        entries = os.listdir(d or ".")
    except OSError:
        return []
    out = []
    for e in entries:
        bare = e.lstrip(".")
        if e == name or not (bare.startswith(name) or bare.startswith(stem + ".")):
            continue
        if "autosave" in e.lower() or e.endswith(","):
            out.append(os.path.join(d, e))
    return sorted(out)


def _recent_files(path: str) -> Tuple[List[str], Optional[float]]:
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        mtime = os.path.getmtime(path)
    except (OSError, ValueError):
        return [], None
    files = []
    for x in doc if isinstance(doc, list) else []:
        p = x if isinstance(x, str) else (x.get("path") if isinstance(x, dict) else None)
        if isinstance(p, str) and p:
            files.append(os.path.realpath(os.path.expanduser(p)))
    return files, mtime


def score_in_use(path: str, proc: str = "/proc", recent_files: str = MUSESCORE_RECENT_FILES,
                 procs: Optional[Sequence[MuseProc]] = None) -> str:
    """Why MuseScore may have ``path`` open ("" = it does not): an autosave file next to it; a MuseScore
    process with it on its command line; or a MuseScore window that opened files since it started (its recent
    files list was written since) with this score among the last ``RECENT_WINDOW`` of them (File > Open)."""
    real = os.path.realpath(path)
    files = autosave_files(path)
    if files:
        return f"MuseScore has unsaved changes to it ({os.path.basename(files[0])}): save or close it in MuseScore"
    procs = musescore_processes(proc) if procs is None else procs
    for p in procs:
        for a in p.argv[1:]:
            if a.startswith("-"):
                continue
            if a.startswith("file://"):
                a = urllib.parse.unquote(a[7:])
            cand = a if os.path.isabs(a) else os.path.join(p.cwd or "/", a)
            if os.path.realpath(cand) == real:
                return f"it is open in MuseScore (pid {p.pid}): close it there"
    windows = [p for p in procs if p.interactive]
    if windows:
        recent, mtime = _recent_files(recent_files)
        if mtime is not None and any(p.started is None or mtime >= p.started - 2 for p in windows) \
                and real in recent[:RECENT_WINDOW]:
            return (f"MuseScore is running (pid {windows[0].pid}) and opened it recently: close it there (or quit "
                    "MuseScore)")
    return ""


def mem_available_mb(meminfo: str = "/proc/meminfo") -> Optional[int]:
    for line in (_read(meminfo) or "").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return None


# ==============================================================================
# the headset
# ==============================================================================

def list_sidecars(adb, device_dir: str) -> Dict[str, str]:
    """Sidecar name -> md5 of the copies on the headset (``applied/`` excluded)."""
    return {n: m for n, m in adb.md5s(device_dir).items() if n.endswith(H.SIDECAR_EXT)}


# ==============================================================================
# log, notifications, locks, state
# ==============================================================================

class SyncLog:
    """Timestamped lines to ``path`` (none for a dry run) and to the stream current at construction (the
    journal, for the service).  ``once(key, msg)`` logs a repeating condition only when it changes."""

    def __init__(self, path: Optional[str], stream=None):
        self.path = path
        self.stream = stream or sys.stdout
        self._last: Dict[object, str] = {}
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def __call__(self, msg: str = "") -> None:
        lines = str(msg).splitlines() or [""]
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            for ln in lines:
                self.stream.write(ln + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass
        if not self.path:
            return
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX_BYTES:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8") as f:
                for ln in lines:
                    f.write(f"{stamp} {ln}\n")
        except OSError:
            pass

    def once(self, key, msg: str) -> bool:
        if self._last.get(key) == msg:
            return False
        self._last[key] = msg
        self(msg)
        return True

    def clear(self, key) -> None:
        self._last.pop(key, None)


class _LineWriter(io.TextIOBase):
    """stdout of the shared ``hands apply`` steps, line by line into the log."""

    def __init__(self, log: Callable[[str], None]):
        self.log = log
        self.buf = ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.log(line)
        return len(s)

    def flush(self) -> None:
        if self.buf:
            self.log(self.buf)
            self.buf = ""


class Notifier:
    """Desktop notifications with ``notify-send`` (every one is logged too)."""

    def __init__(self, enabled: bool, log: Callable[[str], None], run=subprocess.run, exe: Optional[str] = None):
        self.exe = (exe or shutil.which("notify-send")) if enabled else None
        self.log = log
        self.run = run
        if enabled and not self.exe:
            log("notify-send not found: no desktop notifications (the log has everything)")

    def __call__(self, summary: str, body: str = "", urgency: str = "normal") -> None:
        self.log(f"notify: {summary}: {body}" if body else f"notify: {summary}")
        if not self.exe:
            return
        argv = [self.exe, "--app-name=Piano Library", f"--icon={ICON}", f"--urgency={urgency}",
                "--hint=string:desktop-entry:org.pianovision.Library", summary, body]
        try:
            self.run(argv, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


class FileLock:
    """An exclusive ``flock`` that the holder's pid is written into."""

    def __init__(self, path: str):
        self.path = path
        self.f = None
        self.holder = ""

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        f = open(self.path, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            self.holder = f.read().strip() or "?"
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}\n")
        f.flush()
        self.f = f
        return True

    def release(self) -> None:
        if self.f is not None:
            try:
                fcntl.flock(self.f, fcntl.LOCK_UN)
            finally:
                self.f.close()
                self.f = None


def _atomic_json(path: str, doc) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class State:
    """``ledger``: "<name>|<md5>" -> what was written into the scores (or found there already);
    ``sidecars``: name -> the status of the copy on the headset."""
    VERSION = 1

    def __init__(self, path: Optional[str], readonly: bool = False):
        self.path = path
        self.readonly = readonly
        self.ledger: Dict[str, dict] = {}
        self.sidecars: Dict[str, dict] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                self.ledger = d.get("ledger", {})
                self.sidecars = d.get("sidecars", {})
            except (OSError, ValueError):
                pass

    @staticmethod
    def key(name: str, md5: str) -> str:
        return f"{name}|{md5}"

    def done(self, name: str, md5: str) -> Optional[dict]:
        return self.ledger.get(self.key(name, md5))

    def save(self) -> None:
        if self.path and not self.readonly:
            _atomic_json(self.path, {"version": self.VERSION, "ledger": self.ledger, "sidecars": self.sidecars})


# ==============================================================================
# the sync
# ==============================================================================

class HandSync:
    """The loop (:meth:`run`), one step of it (:meth:`step`: poll the headset, look at HandEdits/ when due)
    and one look (:meth:`sync_pass`).  Everything that touches the world can be replaced (tests)."""

    def __init__(self, cfg, settings: Settings, log: Optional[Callable[[str], None]] = None,
                 notify: Optional[Callable[..., None]] = None, probe=None, adb_factory=None, review=None, apply=None,
                 in_use=None, mem=None, library=None, clock: Callable[[], float] = time.monotonic):
        from .device import Adb
        from .guimodel import probe_device
        self.cfg = cfg
        self.s = settings
        self.log = log if log is not None else SyncLog(None)
        if not hasattr(self.log, "once"):
            plain = self.log
            self.log = SyncLog(None, stream=_LineWriter(plain))
        self.notify = notify or Notifier(settings.notify, self.log)
        self.probe = probe or (lambda details=False: probe_device(cfg.adb, settings.serial, details=details))
        self.adb_factory = adb_factory or (lambda serial: Adb(cfg.adb, serial))
        self.review = review or H.review_song
        self.apply_fn = apply or self._apply
        self.in_use = in_use or (lambda path: score_in_use(path, proc=settings.proc,
                                                           recent_files=settings.recent_files))
        self.mem = mem or mem_available_mb
        self.library = library or self._library
        self.clock = clock
        self.state = State(settings.path("state.json"), readonly=settings.dry_run)
        self.stop = threading.Event()
        self.device = None
        self._last_dev: Optional[tuple] = None
        self._next_pass = 0.0
        self._reviews: Dict[tuple, Tuple[tuple, H.ScoreReview, Verdict]] = {}
        self._finish_after: Dict[str, float] = {}     # applied, not archived yet: next try
        self._hold: Dict[str, float] = {}             # a write that failed: next review
        self.last_error = ""
        self.events: List[dict] = []
        self.last_pass = ""
        self.last_result: Counter = Counter()

    # -- helpers -------------------------------------------------------------------------------------------------
    def _library(self):
        from .library import Library
        self.cfg.jobs = 1                     # one score at a time: no process pool
        return Library(self.cfg, log=self.log)

    def _event(self, title: str, text: str) -> None:
        self.events.append({"time": _now(), "title": title, "text": text})
        del self.events[:-30]

    def _tell(self, title: str, text: str, urgency: str = "normal") -> None:
        self._event(title, text)
        if not self.s.dry_run:
            self.notify(title, text, urgency)

    @contextlib.contextmanager
    def _captured(self):
        w = _LineWriter(lambda s: self.log("  " + s))
        with contextlib.redirect_stdout(w):
            try:
                yield
            finally:
                w.flush()

    def _run_logged(self, argv):
        """Note Waterfall's deploy command, its output into the log."""
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800)
        for ln in (r.stdout or "").splitlines():
            self.log("    " + ln)
        return r

    def _waterfall_cmd(self) -> str:
        return self.s.waterfall_deploy if self.s.waterfall_deploy is not None else (self.cfg.waterfall_deploy or "")

    @staticmethod
    def title(lib, rv: Optional[H.ScoreReview], song: str) -> str:
        """``Chopin Waltz in B Minor - Op. 69 No. 2`` (composer and title from the library), else the song."""
        rel = (rv.rel if rv is not None else "") or lib.manifest.owner_of(song)
        e = lib.manifest.entries.get(rel) if rel else None
        if e and e.get("title"):
            t, a = e["title"], e.get("artist") or ""
            return t if not a or a.lower() in t.lower() else f"{a} {t}"
        return os.path.splitext(song)[0]

    def fingerprint(self, lib, name: str, md5: str, song: str) -> str:
        """What a review depends on: the sidecar, the score, the library song and the build options."""
        rel = lib.manifest.owner_of(song)
        parts = [name, md5, rel or ""]
        for p in [lib.out_path(song), self.cfg.options] + ([os.path.join(self.cfg.scores, rel)] if rel else []):
            try:
                st = os.stat(p)
                parts.append(f"{st.st_size}:{st.st_mtime_ns}")
            except (OSError, TypeError):
                parts.append("-")
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]

    def _set(self, scs: Sequence[H.Sidecar], song: str, status: str, message: str, title: str = "",
             rel: str = "", fps: Optional[Dict[str, str]] = None) -> None:
        for sc in scs:
            old = self.state.sidecars.get(sc.name) or {}
            same = old.get("md5") == sc.md5 and old.get("status") == status
            self.state.sidecars[sc.name] = {
                "name": sc.name, "md5": sc.md5, "song": song, "rel": rel or old.get("rel", ""),
                "title": title or old.get("title", ""), "status": status, "message": message,
                "fingerprint": (fps or {}).get(sc.name, ""), "since": old.get("since", _now()) if same else _now(),
                "notified": old.get("notified", "") if old.get("md5") == sc.md5 else ""}

    def _notify_once(self, scs: Sequence[H.Sidecar], what: str, title: str, text: str, urgency: str = "normal") -> None:
        """Notify once per sidecar version and ``what`` (e.g. its review result)."""
        fresh = [sc for sc in scs if (self.state.sidecars.get(sc.name) or {}).get("notified") != what]
        if not fresh:
            return
        self._tell(title, text, urgency)
        for sc in fresh:
            if sc.name in self.state.sidecars:
                self.state.sidecars[sc.name]["notified"] = what

    def _enough_memory(self, scs, song: str, title: str, rel: str, fps) -> bool:
        avail = self.mem()
        if avail is None or avail >= self.s.min_free_mb:
            self.log.clear(("memory", song))
            return True
        msg = f"waiting for memory: {avail} MB available, {self.s.min_free_mb} MB needed"
        self.log.once(("memory", song), f"{song}: {msg}")
        if not self.s.dry_run:
            self._set(scs, song, "waiting", msg, title, rel, fps)
        return False

    # -- the loop ------------------------------------------------------------------------------------------------
    def run(self) -> int:
        """Until :attr:`stop` is set (SIGTERM / SIGINT in :func:`cli`), or one step with ``once``.
        Returns 0, or 2 for ``once`` without a usable headset."""
        mode = ("dry run (lists and reviews, writes nothing)" if self.s.dry_run else
                "one look" if self.s.once else f"every {self.s.interval:g}s while the headset is connected")
        self.log(f"hands sync: {mode}; HandEdits at {self.s.device_dir}; "
                 + (f"log and state in {self.s.sync_dir}" if not self.s.dry_run else "nothing is written"))
        try:
            while not self.stop.is_set():
                dev = self.step()
                if self.s.once:
                    return (1 if self.last_error else 0) if dev.connected else 2
                self.stop.wait(self.s.poll)
        finally:
            self.write_status(running=False)
        self.log("hands sync stopped")
        return 0

    def step(self):
        """Poll the headset; on a change log it; when connected and due, look at HandEdits/."""
        dev = self.probe(False)
        key = (dev.state, dev.serial)
        if key != self._last_dev:
            if dev.connected:
                try:
                    detail = self.probe(True)
                    if detail.connected and detail.serial == dev.serial:
                        dev = detail
                except Exception:
                    pass
                self.log(f"headset: {dev.label()} [{dev.serial}]")
                self._next_pass = 0.0
                self._finish_after.clear()
                self._hold.clear()
            else:
                extra = f" ({dev.message})" if dev.message and dev.state != "none" else ""
                self.log(f"headset: {dev.label()}{extra}")
            self._last_dev = key
            self.device = dev
            self.write_status()
        if dev.connected and self.clock() >= self._next_pass:
            self._locked_pass(dev)
            self._next_pass = self.clock() + self.s.interval
        return dev

    def _locked_pass(self, dev) -> None:
        from .device import DeviceError
        lock = None
        if not self.s.dry_run:
            lock = FileLock(self.s.path("sync.lock"))
            if not lock.acquire():
                self.log.once("lock", f"another hands sync is running (pid {lock.holder}); not looking this time")
                return
            self.log.clear("lock")
        try:
            self.last_result = self.sync_pass(dev)
            self.last_pass = _now()
            self.last_error = ""
            self.log.clear("error")
        except DeviceError as e:
            self.last_error = str(e)
            self.log.once("error", f"headset: {e} (trying again in {self.s.interval:g}s)")
        except Exception as e:                       # the service keeps running; the log has the traceback
            self.last_error = msg = f"hands sync failed: {type(e).__name__}: {e}"
            if self.log.once("error", msg):
                self.log(traceback.format_exc().rstrip())
                self._tell("Hand edits sync", msg, "critical")
        finally:
            if lock is not None:
                lock.release()
            self.state.save()
            self.write_status()

    # -- one look ------------------------------------------------------------------------------------------------
    def sync_pass(self, dev) -> Counter:
        """List HandEdits/ on the headset; review what is new or changed; apply the clean reviews; finish
        (deploy, archive) applied sidecars still on the headset."""
        adb = self.adb_factory(dev.serial)
        listing = list_sidecars(adb, self.s.device_dir)
        self.log.once("listing", "on the headset: " + (", ".join(f"{n} ({m[:8]})" for n, m in sorted(listing.items()))
                                                       or "no hand-edit files"))
        result: Counter = Counter()
        for name in list(self.state.sidecars):
            if name not in listing and not self.s.dry_run:
                st = self.state.sidecars.pop(name)
                self.log(f"{name}: no longer on the headset (was {st.get('status')})")
        if not listing:
            return result
        lib = self.library()
        todo, finish = [], []
        for name, md5 in sorted(listing.items()):
            done = self.state.done(name, md5)
            if done:
                finish.append(name)
                result["applied before"] += 1
                if self.s.dry_run:
                    self.log(f"{name}: written into {done.get('rel')} on {done.get('time')}; still on the headset "
                             "(would be deployed and archived)")
                continue
            if self._hold.get(name, 0.0) > self.clock():
                result["waiting"] += 1
                continue
            st = self.state.sidecars.get(name)
            if not self.s.dry_run and st and st.get("md5") == md5 and st.get("status") in SETTLED \
                    and st.get("fingerprint") == self.fingerprint(lib, name, md5, st.get("song") or ""):
                result[st["status"]] += 1
                continue
            todo.append(name)
        if todo:
            self._review_and_apply(lib, adb, todo, listing, result)
        if finish and not self.s.dry_run:
            self._finish(lib, adb, finish, listing)
        return result

    def _review_and_apply(self, lib, adb, names: List[str], listing: Dict[str, str], result: Counter) -> None:
        cached = {n for n in names if any((n, listing[n]) in k for k in self._reviews)}
        need = [n for n in names if n not in cached]
        tmp = None
        inbox = self.s.inbox
        if self.s.dry_run:
            tmp = tempfile.mkdtemp(prefix="nw-handsync-dry-")
            inbox = tmp
        try:
            scs: List[H.Sidecar] = []
            if need:
                pulled = H.pull(adb, self.s.device_dir, inbox)
                self.log.once("pulled", f"pulled {len(pulled)} hand-edit file(s) into {inbox}")
                scs += [H.read_sidecar(os.path.join(inbox, n)) for n in need if n in pulled]
            for n in sorted(cached):
                for k, (_fp, rv, _v) in self._reviews.items():
                    if (n, listing[n]) in k:
                        scs += [sc for sc in rv.sidecars if sc.name == n]
                        break
            by_song: Dict[str, List[H.Sidecar]] = {}
            for sc in scs:
                by_song.setdefault(sc.song or H.song_of_sidecar(sc.name), []).append(sc)
            for song, group in sorted(by_song.items()):
                if self.stop.is_set():
                    break
                self._handle_song(lib, adb, song, group, result)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    def _handle_song(self, lib, adb, song: str, group: List[H.Sidecar], result: Counter) -> None:
        group = [sc for sc in group if not self.state.done(sc.name, sc.md5)]
        if not group:
            return
        key = tuple(sorted((sc.name, sc.md5) for sc in group))
        fps = {sc.name: self.fingerprint(lib, sc.name, sc.md5, song) for sc in group}
        rel0 = lib.manifest.owner_of(song) or ""
        title = self.title(lib, None, song)
        hit = self._reviews.get(key)
        if hit and hit[0] == tuple(sorted(fps.items())):
            rv, verdict = hit[1], hit[2]
        else:
            if not self._enough_memory(group, song, title, rel0, fps):
                result["waiting"] += 1
                return
            self.log(f"reviewing {song} ({', '.join(sc.name for sc in group)}) ...")
            try:
                rv = self.review(lib, song, group, all_repeats=self.s.all_repeats, log=lambda s: None)
                verdict = classify(rv)
            except Exception as e:                   # a review that crashes is the user's to look at, once
                self.log(traceback.format_exc().rstrip())
                rv = H.ScoreReview(song=song, sidecars=list(group), rel=rel0,
                                   error=f"the review failed: {type(e).__name__}: {e}")
                verdict = Verdict("review", rv.error)
            for line in H.format_review(rv):
                self.log("  " + line)
            self._reviews[key] = (tuple(sorted(fps.items())), rv, verdict)
        title = self.title(lib, rv, song)
        names = ", ".join(sc.name for sc in group)

        if verdict.kind == "review":
            self._reviews.pop(key, None)
            self.log(f"{song}: needs review ({verdict.summary}); {names} stays in the inbox and on the headset")
            result["needs review"] += 1
            if self.s.dry_run:
                return
            self._set(group, song, "needs-review", verdict.summary, title, rv.rel, fps)
            self._notify_once(group, f"review:{verdict.summary}", title,
                              f"needs review: {verdict.summary}. See the Piano Library's Hand edits page, or "
                              "python3 -m pianovision hands review")
            return

        if verdict.kind == "already":
            self._reviews.pop(key, None)
            result["already"] += 1
            if self.s.dry_run:
                self.log(f"{song}: {verdict.summary}; would only be archived")
                return
            self.log(f"{song}: {verdict.summary}: nothing to write; {names} will be archived")
            for sc in group:
                self.state.ledger[State.key(sc.name, sc.md5)] = {
                    "name": sc.name, "md5": sc.md5, "song": song, "rel": rv.rel, "title": title, "time": _now(),
                    "status": "already", "moved": 0, "archived": ""}
            self._set(group, song, "applied", verdict.summary, title, rv.rel, fps)
            self._finish(lib, adb, [sc.name for sc in group], {sc.name: sc.md5 for sc in group})
            return

        # a clean review: apply it, unless MuseScore may have the score open or memory is short
        why = self.in_use(rv.score_path)
        if why:
            self.log.once(("open", song), f"{song}: ready ({verdict.summary}) but not applied: {why}")
            result["waiting"] += 1
            if not self.s.dry_run:
                self._set(group, song, "waiting", why, title, rv.rel, fps)
                self._notify_once(group, "open", title, f"ready ({verdict.summary}); waiting: {why}")
            return
        self.log.clear(("open", song))
        if not self._enough_memory(group, song, title, rv.rel, fps):
            result["waiting"] += 1
            return
        if self.s.dry_run:
            self.log(f"{song}: would apply: {verdict.summary}")
            result["would apply"] += 1
            return
        self.log(f"{song}: applying ({verdict.summary}) to {rv.rel}")
        rep = self.apply_fn(lib, adb, rv)
        self._reviews.pop(key, None)
        self._record(lib, group, song, rv, verdict, rep, title, fps, result)

    def _apply(self, lib, adb, rv: H.ScoreReview) -> H.ApplyReport:
        """``hands apply -y`` for one review: backup, overwrite, build, deploy to both apps, archive."""
        from .cli import _list, hands_archive_local, hands_callbacks
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        with self._captured():
            deploy_pv, deploy_nw, archive = hands_callbacks(
                lib, adb, self.s.device_dir, [rv], stamp, deploy=self.s.deploy, archive=self.s.archive,
                waterfall_deploy=self.s.waterfall_deploy, run=self._run_logged)
            rep = H.apply_reviews(lib, [rv], yes=True, log=print, deploy_pianovision=deploy_pv,
                                  deploy_waterfall=deploy_nw, archive=archive)
            _list("scores overwritten", rep.written)
            _list("backups", rep.backups)
            _list("songs rebuilt", rep.built)
            _list("hand-edit files moved to applied/ on the headset", rep.archived)
            _list("FAILED", [f"{r}: {e}" for r, e in rep.failed], 50)
            for m in rep.messages:
                print(m)
        hands_archive_local(lib, self.s.inbox, rep.archived, stamp)
        return rep

    def _record(self, lib, group, song, rv, verdict, rep: H.ApplyReport, title, fps, result: Counter) -> None:
        failed = [e for r, e in rep.failed if r == rv.rel]
        if rv.rel not in rep.written:                 # nothing was written: review again next time
            why = failed[0] if failed else "not written"
            self._set(group, song, "waiting", f"not applied: {why}", title, rv.rel, fps)
            self.log(f"{song}: not applied: {why}; reviewed again in {FINISH_RETRY_S / 60:g} min or on reconnect")
            for sc in group:
                self._hold[sc.name] = self.clock() + FINISH_RETRY_S
            result["waiting"] += 1
            return
        built = song in rep.built and not failed
        deploy_failed = [m for m in rep.messages if m.startswith("deploy to")]
        deployed = built and self.s.deploy and not deploy_failed
        for sc in group:
            archived = sc.name in rep.archived
            self.state.ledger[State.key(sc.name, sc.md5)] = {
                "name": sc.name, "md5": sc.md5, "song": song, "rel": rv.rel, "title": title, "time": _now(),
                "status": "applied" if built else "build-failed", "moved": rv.moved_notes,
                "to_right": rv.to_right, "to_left": rv.to_left, "backup": rep.backups[0] if rep.backups else "",
                "deployed": deployed, "archived": "yes" if archived else ""}
            if not archived:
                self._finish_after[sc.name] = self.clock() + FINISH_RETRY_S
        status = "applied" if built else "failed"
        self._set(group, song, status, verdict.summary, title, rv.rel, fps)
        if not built:
            result["failed"] += 1
            self._tell(title, f"the score was written (backup in the attic) but {failed[0] if failed else 'not built'}"
                              "; run python3 -m pianovision sync", "critical")
            return
        result["applied"] += 1
        if deployed:
            text = f"{verdict.summary}, deployed"
            if any(sc.name not in rep.archived for sc in group) and self.s.archive and self._waterfall_cmd():
                text += "; the hand-edit file stays on the headset for now"
        elif not self.s.deploy:
            text = f"{verdict.summary}; rebuilt, not deployed (--no-deploy)"
        else:
            text = f"{verdict.summary}; rebuilt, not deployed yet ({deploy_failed[0]}): retried while connected"
        self._tell(title, text)

    def _finish(self, lib, adb, names: List[str], listing: Dict[str, str]) -> None:
        """Sidecars written into the scores (or found there) still on the headset: deploy their songs to both
        apps and archive them, as ``hands apply`` does after the build (at most every FINISH_RETRY_S)."""
        if not (self.s.deploy and self.s.archive and self._waterfall_cmd()):
            return
        now = self.clock()
        entries = []
        for n in names:
            e = self.state.done(n, listing.get(n, ""))
            if e and e.get("status") in ("applied", "already") and self._finish_after.get(n, 0.0) <= now \
                    and os.path.exists(lib.out_path(e["song"])):
                entries.append(e)
        if not entries:
            return
        from .cli import hands_archive_local, hands_callbacks
        songs = sorted({e["song"] for e in entries})
        reviews = [H.ScoreReview(song=s, sidecars=[H.Sidecar(name=e["name"], song=s, md5=e["md5"])
                                                   for e in entries if e["song"] == s]) for s in songs]
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        archived: List[str] = []
        self.log(f"finishing {', '.join(e['name'] for e in entries)}: deploy {', '.join(songs)}, then archive")
        try:
            with self._captured():
                deploy_pv, deploy_nw, archive = hands_callbacks(lib, adb, self.s.device_dir, reviews, stamp,
                                                                waterfall_deploy=self.s.waterfall_deploy,
                                                                run=self._run_logged)
                deploy_pv(songs)
                deploy_nw(songs)
                archived = archive([e["name"] for e in entries]) if archive else []
        except Exception as ex:
            self.log(f"finishing failed: {type(ex).__name__}: {ex}; next try in {FINISH_RETRY_S / 60:g} min")
        hands_archive_local(lib, self.s.inbox, archived, stamp)
        for e in entries:
            if e["name"] in archived:
                e["archived"] = "yes"
                e["deployed"] = True
                self.state.sidecars.pop(e["name"], None)
                self.log(f"{e['name']}: moved to {H.APPLIED_DIR}/ on the headset")
            else:
                self._finish_after[e["name"]] = now + FINISH_RETRY_S

    # -- status file (for the Piano Library window) -----------------------------------------------------------
    def write_status(self, running: bool = True) -> None:
        if self.s.dry_run:
            return
        d = self.device
        doc = {"version": 1, "updated": _now(), "pid": os.getpid(), "running": running and not self.stop.is_set(),
               "log": self.s.path("sync.log"), "interval": self.s.interval,
               "device": ({"state": d.state, "serial": d.serial, "model": d.model, "label": d.label(),
                           "wakefulness": d.wakefulness} if d is not None else None),
               "last_pass": self.last_pass, "last_result": dict(self.last_result),
               "sidecars": [self.state.sidecars[n] for n in sorted(self.state.sidecars)],
               "events": self.events[-30:]}
        try:
            _atomic_json(self.s.path("status.json"), doc)
        except OSError:
            pass


# ==============================================================================
# python -m pianovision hands sync
# ==============================================================================

def cli(args) -> int:
    from .library import load_config
    cfg = load_config(args.config, scores=args.scores, output=args.output)
    if not cfg.scores:
        sys.exit("no scores folder: set [library] scores in pianovision.toml or pass --scores")
    if args.serial:
        cfg.serial = args.serial
    dry = bool(args.dry_run)
    s = load_settings(cfg, sync_dir=args.state_dir, interval=args.interval, poll=args.poll,
                      min_free_mb=args.min_free_mb, dry_run=dry, once=bool(args.once or dry),
                      notify=not (args.no_notify or dry), deploy=not args.no_deploy, archive=not args.no_archive,
                      all_repeats=bool(args.all_repeats), waterfall_deploy=args.waterfall_deploy,
                      device_dir=args.device_dir, inbox=args.inbox, serial=cfg.serial or None)
    log = SyncLog(None if dry else s.path("sync.log"))
    daemon = None
    if not s.once:
        daemon = FileLock(s.path("daemon.lock"))
        if not daemon.acquire():
            log(f"hands sync is already running (pid {daemon.holder}); `hands sync --once` looks once meanwhile")
            return 75
    sync = HandSync(cfg, s, log=log)

    def stop(_sig, _frm):
        sync.log("stopping (after the current step)")
        sync.stop.set()
    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        rc = sync.run()
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
        if daemon is not None:
            daemon.release()
    if s.once:
        r = sync.last_result
        if rc == 2:
            log("no usable headset: nothing looked at")
        else:
            log(("dry run: " if dry else "") + (", ".join(f"{v} {k}" for k, v in sorted(r.items()))
                                                or "nothing to do") + ("; nothing was written" if dry else ""))
    return rc
