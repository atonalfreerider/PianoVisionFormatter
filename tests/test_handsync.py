"""The automatic hand-edit sync (pianovision.handsync, `hands sync`).

A fake adb (a script answering `devices -l` from a JSON file and running `shell` commands on a folder that
stands in for the headset's /sdcard), mocked reviews and applies, a fake /proc for MuseScore, and one run
through the real review and apply of a synthetic score (tests/scoregen).  No headset, no real scores."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

from pianovision import handedits as H
from pianovision import handsync as S
from pianovision.library import Config, Library

from tests.scoregen import chord, make_piano_score, rest

SERIAL = "2G0YC1ZF8F063B"
QUEST = "usb:1-11 product:eureka model:Quest_3 device:eureka transport_id:1"
NW = "/sdcard/Android/data/com.atonalfreerider.notewaterfall/files"
HANDS = NW + "/HandEdits"
PV = "/sdcard/Android/data/com.ZarApps.PianoVision/files"
Q = 480

FAKE_ADB = r'''#!{python}
import json, os, shutil, subprocess, sys
with open({state!r}) as f:
    st = json.load(f)
args = sys.argv[1:]
with open(st["log"], "a") as f:
    f.write(json.dumps(args) + "\n")
serial = None
if args[:1] == ["-s"]:
    serial, args = args[1], args[2:]
if args[:1] == ["devices"]:
    if st.get("server_restart"):            # what a client prints when it starts a killed adb server again
        print("* daemon not running; starting now at tcp:5037")
        print("* daemon started successfully")
    print("List of devices attached")
    for s, state, desc in st["devices"]:
        print(f"{{s}}\t{{state}} {{desc}}".rstrip())
    print()
    sys.exit(0)
devs = [d for d in st["devices"] if serial is None or d[0] == serial]
if not devs:
    print("adb: no devices/emulators found", file=sys.stderr)
    sys.exit(1)
if devs[0][1] != "device":
    print(f"adb: device {{devs[0][1]}}", file=sys.stderr)
    sys.exit(1)
root = st["root"]
def local(p):
    return os.path.join(root, p.lstrip("/"))
if args[0] == "shell":
    cmd = " ".join(args[1:])
    if "dumpsys power" in cmd:
        print("  mWakefulness=Asleep")
        sys.exit(0)
    sys.exit(subprocess.run(["/bin/sh", "-c", cmd.replace("/sdcard", root + "/sdcard")]).returncode)
if args[0] == "pull":
    try:
        shutil.copyfile(local(args[1]), args[2])
    except OSError as e:
        print(f"adb: error: {{e}}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if args[0] == "push":
    *srcs, dst = args[1:]
    for s in srcs:
        shutil.copy(s, os.path.join(local(dst), os.path.basename(s)) if dst.endswith("/") else local(dst))
    sys.exit(0)
print("fake adb: unsupported: " + " ".join(args), file=sys.stderr)
sys.exit(1)
'''

# Note Waterfall's Tools/deploy_songs.py, as far as `hands apply` uses it: --library L --serial S --only GLOB...
FAKE_NW_DEPLOY = r'''#!{python}
import argparse, fnmatch, os, shutil
ap = argparse.ArgumentParser()
ap.add_argument("--library")
ap.add_argument("--serial")
ap.add_argument("--only", action="append", default=[])
a = ap.parse_args()
songs = {songs!r}
os.makedirs(songs, exist_ok=True)
for f in sorted(os.listdir(a.library)):
    if f.endswith(".json") and any(fnmatch.fnmatchcase(f, g) for g in a.only):
        shutil.copy(os.path.join(a.library, f), os.path.join(songs, f))
        print("pushed", f)
'''


class FakeHeadset:
    """The fake adb's world: which devices `adb devices` lists, and the headset's files under ``root``."""

    def __init__(self, tmp: str):
        self.root = os.path.join(tmp, "device")
        self.state_path = os.path.join(tmp, "adb.json")
        self.log_path = os.path.join(tmp, "adb.log")
        self.adb = os.path.join(tmp, "fake-adb")
        os.makedirs(self.local(HANDS))
        with open(self.adb, "w") as f:
            f.write(FAKE_ADB.format(python=sys.executable, state=self.state_path))
        os.chmod(self.adb, 0o755)
        open(self.log_path, "w").close()
        self.set([])

    def set(self, devices, server_restart=False) -> None:
        with open(self.state_path, "w") as f:
            json.dump({"root": self.root, "log": self.log_path, "devices": devices,
                       "server_restart": server_restart}, f)

    def connect(self, state="device", **kw) -> None:
        self.set([[SERIAL, state, QUEST if state == "device" else "usb:1-11"]], **kw)

    def local(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    def put(self, name: str, data: bytes, folder: str = HANDS) -> None:
        with open(os.path.join(self.local(folder), name), "wb") as f:
            f.write(data)

    def files(self, folder: str = HANDS):
        try:
            return sorted(os.listdir(self.local(folder)))
        except OSError:
            return []

    def calls(self):
        with open(self.log_path) as f:
            return [json.loads(ln) for ln in f if ln.strip()]


def sidecar_bytes(song: str, edits, md5: str = "") -> bytes:
    return json.dumps({"format": 1, "song": song, "songMd5": md5, "updated": "2026-10-20T10:50:00Z",
                       "edits": edits}).encode()


def edit(frm, ticks, midi, occ=0):
    return {"from": frm, "to": "left" if frm == "right" else "right", "ticksStart": ticks, "midi": midi,
            "occurrence": occ, "votes": 2, "against": 0}


class Base(unittest.TestCase):
    SONG = "chop_Waltz.json"
    REL = "Chopin/waltz.mscz"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scores = os.path.join(self.tmp, "Scores")
        self.out = os.path.join(self.tmp, "PianoVision")
        self.sync_dir = os.path.join(self.tmp, "sync")
        os.makedirs(self.scores)
        os.makedirs(self.out)
        self.headset = FakeHeadset(self.tmp)
        self.now = [1000.0]
        self.logs, self.notes = [], []
        self.cfg = Config(scores=self.scores, output=self.out, adb=self.headset.adb, hands_dir=HANDS, device_dir=PV,
                          jobs=1, options=os.path.join(self.tmp, "scores.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def settings(self, **kw) -> S.Settings:
        d = dict(sync_dir=self.sync_dir, device_dir=HANDS, inbox=os.path.join(self.out, ".hands", "inbox"),
                 interval=45.0, poll=0.0, waterfall_deploy="", recent_files=os.path.join(self.tmp, "recent.json"),
                 proc=os.path.join(self.tmp, "proc"))
        d.update(kw)
        return S.Settings(**d)

    def make_sync(self, settings=None, **kw) -> S.HandSync:
        kw.setdefault("in_use", lambda path: "")
        kw.setdefault("mem", lambda: 16000)
        return S.HandSync(self.cfg, settings or self.settings(), log=self.logs.append,
                          notify=lambda title, text="", urgency="normal": self.notes.append((title, text)),
                          clock=lambda: self.now[0], **kw)

    def later(self, s: float = 45.0) -> None:
        self.now[0] += s

    def log_text(self) -> str:
        return "\n".join(self.logs)


class MockedBase(Base):
    """A library with one song (no rendering): reviews and applies are mocked."""

    def setUp(self):
        super().setUp()
        self.score = os.path.join(self.scores, self.REL)
        os.makedirs(os.path.dirname(self.score))
        with open(self.score, "wb") as f:
            f.write(b"PK score")
        with open(os.path.join(self.out, self.SONG), "w") as f:
            f.write("{}")
        with open(os.path.join(self.out, ".manifest.json"), "w") as f:
            json.dump({"version": 1, "entries": {self.REL: {"output": self.SONG, "title": "Waltz in B Minor",
                                                            "artist": "Chopin"}}}, f)
        self.kind = "clean"
        self.reviewed, self.applied = [], []
        self.archive_ok = True

    def review(self, lib, song, scs, all_repeats=False, log=None):
        self.reviewed.append((song, [(sc.name, sc.md5) for sc in scs]))
        rv = H.ScoreReview(song=song, sidecars=list(scs), rel=self.REL, score_path=self.score)
        if self.kind == "crash":
            raise ValueError("boom")
        status = {"clean": "move", "conflict": "conflict", "skip": "skip", "already": "already"}[self.kind]
        for sc in scs:
            for e in sc.edits:
                rv.results.append(H.EditResult(sc.name, e, status, reason="" if status == "move" else "why"))
        if self.kind == "clean":
            rv.new_mscz, rv.new_json = b"PK new", b"{}"
            rv.moved_notes = rv.to_right = len(rv.results)
        return rv

    def apply(self, lib, adb, rv):
        self.applied.append(rv.song)
        rep = H.ApplyReport(written=[rv.rel], built=[rv.song], backups=["/attic/x"])
        if self.archive_ok:                       # what the real archive step does on the headset
            for sc in rv.sidecars:
                os.replace(os.path.join(self.headset.local(HANDS), sc.name),
                           os.path.join(self.headset.local(HANDS), "applied", sc.name))
            rep.archived = [sc.name for sc in rv.sidecars]
        return rep

    def make_sync(self, settings=None, **kw):
        kw.setdefault("review", self.review)
        kw.setdefault("apply", self.apply)
        return super().make_sync(settings, **kw)

    def put_sidecar(self, edits=None, name="chop_Waltz.hands.json") -> bytes:
        data = sidecar_bytes(self.SONG, edits if edits is not None else [edit("left", 0, 64), edit("left", 0, 67)])
        os.makedirs(os.path.join(self.headset.local(HANDS), "applied"), exist_ok=True)
        self.headset.put(name, data)
        return data


# ------------------------------------------------------------------------------

class DetectionTest(MockedBase):
    def listings(self):
        return sum(1 for c in self.headset.calls() if c[:3] == ["-s", SERIAL, "shell"] and "md5sum" in c[3])

    def test_the_headset_comes_goes_and_is_only_looked_at_when_usable(self):
        sync = self.make_sync()
        sync.step()                                            # nothing attached
        self.assertIn("headset: No headset connected", self.log_text())
        self.headset.connect("unauthorized")
        sync.step()
        self.assertIn("unauthorized", self.logs[-1])
        self.headset.connect("offline")
        sync.step()
        self.assertIn("offline", self.logs[-1])
        self.assertEqual(self.listings(), 0)
        self.assertTrue(all(c[:1] == ["devices"] for c in self.headset.calls()))

        self.headset.connect(server_restart=True)            # Unity's build restarted the adb server meanwhile
        sync.step()
        self.assertIn("Quest 3 connected (asleep)", self.log_text())
        self.assertEqual(self.listings(), 1)
        sync.step()                                            # not due yet
        self.assertEqual(self.listings(), 1)
        self.later(44)
        sync.step()
        self.assertEqual(self.listings(), 1)
        self.later(1)
        sync.step()
        self.assertEqual(self.listings(), 2)

        self.headset.set([])                                  # unplugged
        sync.step()
        self.assertIn("headset: No headset connected", self.logs[-1])
        self.headset.connect()                                # plugged in again: looked at at once
        sync.step()
        self.assertEqual(self.listings(), 3)
        self.assertEqual(self.reviewed, [])

    def test_a_headset_that_goes_away_during_a_look_is_logged_once_and_tried_again(self):
        sync = self.make_sync()
        self.put_sidecar()
        self.headset.connect()

        def gone(serial):
            from pianovision.device import Adb
            self.headset.set([[SERIAL, "offline", ""]])       # unplugged right after the poll
            return Adb(self.headset.adb, serial)
        sync.adb_factory = gone
        sync.step()
        self.assertIn("headset: adb: device offline", self.log_text())
        self.assertEqual(self.reviewed, [])
        self.assertEqual(self.headset.files(), ["applied", "chop_Waltz.hands.json"])

    def test_the_status_file_says_what_the_window_needs(self):
        self.put_sidecar()
        self.headset.connect()
        self.make_sync().step()
        with open(os.path.join(self.sync_dir, "status.json")) as f:
            st = json.load(f)
        self.assertEqual(st["device"]["state"], "connected")
        self.assertEqual(st["device"]["wakefulness"], "Asleep")
        self.assertEqual(st["last_result"], {"applied": 1})
        self.assertEqual(st["events"][-1]["title"], "Chopin Waltz in B Minor")


class ApplyOnceTest(MockedBase):
    def test_a_clean_review_is_applied_once_and_the_rest_left_alone(self):
        data = self.put_sidecar()
        self.headset.connect()
        sync = self.make_sync()
        sync.step()
        self.assertEqual(self.applied, [self.SONG])
        self.assertEqual(self.notes, [("Chopin Waltz in B Minor",
                                       "2 notes moved to the other hand (2 to the right, 0 to the left), deployed")])
        key = S.State.key("chop_Waltz.hands.json", H._md5(data))
        with open(os.path.join(self.sync_dir, "state.json")) as f:
            ledger = json.load(f)["ledger"]
        self.assertEqual(ledger[key]["status"], "applied")
        self.assertEqual(ledger[key]["archived"], "yes")
        self.later()
        sync.step()                                            # the headset has nothing left
        self.assertEqual((len(self.reviewed), len(self.applied)), (1, 1))

        # the same file back on the headset (an archive that did not happen), and a restarted service
        self.headset.put("chop_Waltz.hands.json", data)
        again = self.make_sync()
        again.step()
        self.assertEqual((len(self.reviewed), len(self.applied)), (1, 1))
        self.assertEqual(len(self.notes), 1)

        # HAND REC saved more: a new version is reviewed (the earlier edits read as "already" there)
        self.headset.put("chop_Waltz.hands.json", sidecar_bytes(self.SONG, [edit("right", 960, 72)]))
        self.later()
        again.step()
        self.assertEqual((len(self.reviewed), len(self.applied)), (2, 2))

    def test_a_conflicting_review_stays_in_the_inbox_and_on_the_headset_and_is_told_once(self):
        self.kind = "conflict"
        data = self.put_sidecar()
        self.headset.connect()
        sync = self.make_sync()
        sync.step()
        self.assertEqual(self.applied, [])
        self.assertEqual(len(self.notes), 1)
        self.assertIn("needs review: 2 edits conflict", self.notes[0][1])
        self.assertEqual(self.headset.files(), ["applied", "chop_Waltz.hands.json"])
        with open(os.path.join(self.out, ".hands", "inbox", "chop_Waltz.hands.json"), "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(sync.state.sidecars["chop_Waltz.hands.json"]["status"], "needs-review")
        for _ in range(3):                                     # unchanged: not reviewed or told again
            self.later()
            sync.step()
        self.assertEqual((len(self.reviewed), len(self.notes)), (1, 1))
        self.make_sync().step()                               # nor after a restart
        self.assertEqual((len(self.reviewed), len(self.notes)), (1, 1))

        os.utime(self.score, (2e9, 2e9))                      # the score was edited: reviewed again
        self.later()
        sync.step()
        self.assertEqual(len(self.reviewed), 2)
        self.assertEqual(len(self.notes), 1)                  # same result: still told once

        self.kind = "clean"                                    # recorded again, now clean
        self.put_sidecar([edit("left", 0, 64)])
        self.later()
        sync.step()
        self.assertEqual(self.applied, [self.SONG])
        self.assertIn("1 note moved", self.notes[-1][1])

    def test_skipped_and_unreadable_edits_need_review_too(self):
        self.kind = "skip"
        self.put_sidecar()
        self.headset.connect()
        self.make_sync().step()
        self.assertEqual(self.applied, [])
        self.assertIn("needs review: 2 edits cannot be written safely", self.notes[0][1])

    def test_edits_already_in_the_score_are_not_written(self):
        self.kind = "already"
        data = self.put_sidecar()
        self.headset.connect()
        sync = self.make_sync()
        sync.step()
        self.assertEqual(self.applied, [])
        self.assertEqual(sync.state.done("chop_Waltz.hands.json", H._md5(data))["status"], "already")
        self.later()
        sync.step()
        self.assertEqual(len(self.reviewed), 1)

    def test_a_review_that_crashes_is_reported_once_not_retried_in_a_loop(self):
        self.kind = "crash"
        self.put_sidecar()
        self.headset.connect()
        sync = self.make_sync()
        for _ in range(3):
            sync.step()
            self.later()
        self.assertEqual(len(self.reviewed), 1)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("the review failed: ValueError: boom", self.notes[0][1])

    def test_a_write_that_fails_is_reviewed_again_later_not_every_look(self):
        self.put_sidecar()
        self.headset.connect()
        sync = self.make_sync(apply=lambda lib, adb, rv: self.applied.append(rv.song) or H.ApplyReport(
            failed=[(rv.rel, "the score changed since the review; run review again")]))
        sync.step()
        self.assertEqual(sync.state.sidecars["chop_Waltz.hands.json"]["status"], "waiting")
        self.later()
        sync.step()
        self.assertEqual((len(self.reviewed), len(self.applied)), (1, 1))
        self.later(S.FINISH_RETRY_S)
        sync.step()
        self.assertEqual((len(self.reviewed), len(self.applied)), (2, 2))
        self.assertEqual(self.notes, [])

    def test_low_memory_waits_before_reviewing(self):
        self.put_sidecar()
        self.headset.connect()
        free = [1000]
        sync = self.make_sync(mem=lambda: free[0])
        sync.step()
        self.assertEqual(self.reviewed, [])
        self.assertIn("waiting for memory: 1000 MB available, 4096 MB needed", self.log_text())
        self.later()
        sync.step()
        self.assertEqual(self.log_text().count("waiting for memory"), 1)
        free[0] = 6000
        self.later()
        sync.step()
        self.assertEqual(self.applied, [self.SONG])

    def test_one_look_at_a_time(self):
        self.put_sidecar()
        self.headset.connect()
        other = S.FileLock(os.path.join(self.sync_dir, "sync.lock"))
        self.assertTrue(other.acquire())
        try:
            sync = self.make_sync()
            sync.step()
            self.assertIn(f"another hands sync is running (pid {os.getpid()})", self.log_text())
            self.assertEqual(self.reviewed, [])
        finally:
            other.release()
        self.later()
        sync.step()
        self.assertEqual(self.applied, [self.SONG])

    def test_dry_run_reviews_and_writes_nothing(self):
        self.put_sidecar()
        self.headset.connect()
        sync = self.make_sync(self.settings(dry_run=True, once=True))
        self.assertEqual(sync.run(), 0)
        self.assertEqual(len(self.reviewed), 1)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.notes, [])
        self.assertIn("would apply: 2 notes moved to the other hand", self.log_text())
        self.assertFalse(os.path.exists(self.sync_dir))
        self.assertFalse(os.path.exists(os.path.join(self.out, ".hands")))
        self.assertEqual(self.headset.files(), ["applied", "chop_Waltz.hands.json"])
        self.assertFalse([c for c in self.headset.calls() if c[2:3] == ["push"]
                          or (c[2:3] == ["shell"] and c[3].startswith(("mkdir", "mv", "rm")))])

    def test_once_without_a_headset(self):
        sync = self.make_sync(self.settings(once=True))
        self.assertEqual(sync.run(), 2)


class ClassifyTest(unittest.TestCase):
    def rv(self, *statuses, **kw):
        sc = H.Sidecar(name="a.hands.json", song="a.json", edits=[H.HandEdit("left", "right", 0, 60)])
        rv = H.ScoreReview(song="a.json", sidecars=[sc], **kw)
        rv.results = [H.EditResult(sc.name, sc.edits[0], s) for s in statuses]
        return rv

    def test_kinds(self):
        clean = self.rv("move", "already", new_mscz=b"x", moved_notes=1, to_right=1)
        self.assertEqual(S.classify(clean).kind, "apply")
        self.assertEqual(S.classify(clean).summary, "1 note moved to the other hand (1 to the right, 0 to the left)")
        self.assertEqual(S.classify(self.rv("already", "already")).kind, "already")
        self.assertEqual(S.classify(self.rv("move", "conflict", new_mscz=b"x", moved_notes=1)).summary,
                         "1 edit conflicts")
        self.assertEqual(S.classify(self.rv("move", "stale", "stale", new_mscz=b"x", moved_notes=1)).summary,
                         "2 edits no longer match the song")
        self.assertEqual(S.classify(self.rv("move", "skip", new_mscz=b"x", moved_notes=1)).kind, "review")
        self.assertEqual(S.classify(self.rv(error="score not found: x")).summary, "score not found: x")
        self.assertEqual(S.classify(self.rv("move")).kind, "review")          # not verified
        unread = self.rv("move", new_mscz=b"x", moved_notes=1)
        unread.sidecars[0].unreadable = 1
        self.assertEqual(S.classify(unread).summary, "1 edit could not be read")
        empty = H.ScoreReview(song="a.json", sidecars=[H.Sidecar(name="a.hands.json", song="a.json")],
                              error="no edits")
        self.assertEqual(S.classify(empty).kind, "already")
        broken = H.ScoreReview(song="a.json", sidecars=[H.Sidecar(name="a.hands.json", song="a.json",
                                                                  error="format 3 is newer than 2")])
        self.assertIn("format 3 is newer", S.classify(broken).summary)


class MuseScoreTest(Base):
    BTIME = 1_700_000_000

    def setUp(self):
        super().setUp()
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        with open(os.path.join(self.proc, "stat"), "w") as f:
            f.write(f"cpu 1 2 3\nbtime {self.BTIME}\n")
        self.score = os.path.join(self.scores, "Chopin", "waltz.mscz")
        os.makedirs(os.path.dirname(self.score))
        with open(self.score, "wb") as f:
            f.write(b"PK")
        self.recent = os.path.join(self.tmp, "recent.json")

    def process(self, pid, argv, cwd=None, started=None, env=b""):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(os.path.basename(argv[0])[:15] + "\n")
        os.symlink(cwd or self.tmp, os.path.join(d, "cwd"))
        ticks = int(((started or self.BTIME + 100) - self.BTIME) * os.sysconf("SC_CLK_TCK"))
        with open(os.path.join(d, "stat"), "w") as f:
            f.write(f"{pid} (x y) S 1 " + "0 " * 17 + f"{ticks} 0 0\n")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(env)

    def in_use(self):
        return S.score_in_use(self.score, proc=self.proc, recent_files=self.recent)

    def test_nothing_running(self):
        self.process(10, ["/usr/bin/python3", "-m", "pianovision", "hands", "sync", self.score])
        self.assertEqual(self.in_use(), "")

    def test_a_musescore_with_the_score_on_its_command_line(self):
        self.process(4242, ["/tmp/.mount_mscore/bin/mscore4portable", "waltz.mscz"],
                     cwd=os.path.dirname(self.score))
        self.assertIn("open in MuseScore (pid 4242)", self.in_use())

    def test_a_converter_reading_another_score_does_not_count(self):
        self.process(4243, ["/home/x/.local/bin/mscore", "-o", "out.mid", "/elsewhere/other.mscz"],
                     env=b"QT_QPA_PLATFORM=offscreen\0")
        with open(self.recent, "w") as f:
            json.dump([self.score], f)
        self.assertEqual(self.in_use(), "")

    def test_a_window_that_opened_files_since_it_started_may_have_a_recent_score_open(self):
        started = self.BTIME + 5000
        self.process(4244, ["/home/x/.local/bin/MuseScore-Studio-4.7.5.AppImage"], started=started)
        with open(self.recent, "w") as f:
            json.dump(["/x/other.mscz", self.score], f)
        os.utime(self.recent, (started + 60, started + 60))
        self.assertIn("MuseScore is running (pid 4244) and opened it recently", self.in_use())
        os.utime(self.recent, (started - 600, started - 600))     # nothing opened since it started
        self.assertEqual(self.in_use(), "")
        with open(self.recent, "w") as f:
            json.dump(["/x/o%d.mscz" % i for i in range(S.RECENT_WINDOW)] + [self.score], f)
        os.utime(self.recent, (started + 60, started + 60))
        self.assertEqual(self.in_use(), "")                        # opened long ago

    def test_an_autosave_next_to_the_score(self):
        open(self.score + ".autosave", "w").close()
        self.assertIn("unsaved changes to it (waltz.mscz.autosave)", self.in_use())


class MuseScoreWaitTest(MockedBase):
    def test_a_ready_score_open_in_musescore_waits_then_is_applied_without_a_second_review(self):
        self.put_sidecar()
        self.headset.connect()
        why = ["it is open in MuseScore (pid 4242): close it there"]
        sync = self.make_sync(in_use=lambda path: why[0] if path == self.score else "?")
        sync.step()
        self.assertEqual(self.applied, [])
        self.assertEqual(sync.state.sidecars["chop_Waltz.hands.json"]["status"], "waiting")
        self.assertIn("waiting: it is open in MuseScore", self.notes[0][1])
        self.later()
        sync.step()
        self.assertEqual(len(self.notes), 1)
        why[0] = ""                                             # closed
        self.later()
        sync.step()
        self.assertEqual(self.applied, [self.SONG])
        self.assertEqual(len(self.reviewed), 1)
        self.assertIn("deployed", self.notes[-1][1])


# ------------------------------------------------------------------------------

class EndToEndTest(Base):
    """A synthetic score, the real review and `hands apply -y` steps, the fake headset and a fake
    Note Waterfall deploy: applied, built, pushed to both apps, archived; then never again."""
    RH = [[[chord("quarter", p) for p in (72, 74, 76, 77)]]]
    LH = [[[chord("half", 48, 55), chord("quarter", 48, 55, 64), rest("quarter")]]]

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.scores, "Test", "hands.mscz")
        os.makedirs(os.path.dirname(self.path))
        make_piano_score(self.path, self.RH, self.LH)
        lib = Library(self.cfg, log=lambda *_: None)
        self.assertEqual(lib.build().failed, [])
        e = lib.manifest.entries["Test/hands.mscz"]
        self.song, self.title = e["output"], f"{e['artist']} {e['title']}"
        with open(lib.out_path(self.song), "rb") as f:
            self.song_md5 = H._md5(f.read())
        nw = os.path.join(self.tmp, "fake-deploy-songs.py")
        with open(nw, "w") as f:
            f.write(FAKE_NW_DEPLOY.format(python=sys.executable, songs=self.headset.local(NW + "/Songs")))
        self.cfg.waterfall_deploy = f"{sys.executable} {nw}"
        self.name = H.sidecar_name(self.song)
        self.data = sidecar_bytes(self.song, [edit("left", 2 * Q, 64)], self.song_md5)
        self.headset.put(self.name, self.data)
        self.headset.connect()
        with open(self.path, "rb") as f:
            self.original = f.read()

    def right_hand(self):
        with open(os.path.join(self.out, self.song), "rb") as f:
            return sorted((n["ticksStart"], n["note"]) for n in H._flat(json.loads(f.read()), "right"))

    def test_applied_built_deployed_archived_and_never_again(self):
        sync = self.make_sync(self.settings(waterfall_deploy=None))
        with contextlib.redirect_stdout(io.StringIO()):
            sync.step()
        self.assertEqual(self.notes, [(self.title,
                                       "1 note moved to the other hand (1 to the right, 0 to the left), deployed")],
                         self.log_text())
        with open(self.path, "rb") as f:
            written = f.read()
        self.assertNotEqual(written, self.original)
        self.assertIn((2 * Q, 64), self.right_hand())
        backups = [os.path.join(dp, f) for dp, _dn, fn in os.walk(os.path.join(self.out, ".attic")) for f in fn
                   if f.endswith(".mscz")]
        self.assertEqual(len(backups), 1)
        with open(backups[0], "rb") as f:
            self.assertEqual(f.read(), self.original)
        with open(os.path.join(self.out, self.song), "rb") as f:
            built = f.read()
        for folder in (PV, NW + "/Songs"):
            with open(os.path.join(self.headset.local(folder), self.song), "rb") as f:
                self.assertEqual(f.read(), built, folder)
        self.assertEqual(self.headset.files(), ["applied"])
        self.assertRegex(self.headset.files(HANDS + "/applied")[0], r"\.\d{4}-\d\d-\d\d_\d{6}\.hands\.json$")
        self.assertEqual(os.listdir(os.path.join(self.out, ".hands", "inbox")), [])
        self.assertEqual(sync.state.done(self.name, H._md5(self.data))["archived"], "yes")

        # the same file again on the headset: not written again, only archived again
        self.headset.put(self.name, self.data)
        self.later()
        with contextlib.redirect_stdout(io.StringIO()):
            sync.step()
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), written)
        self.assertEqual(self.headset.files(), ["applied"])
        self.assertEqual(len(self.notes), 1)

    def run_cli(self, *argv):
        from pianovision.cli import main
        toml = os.path.join(self.tmp, "pianovision.toml")
        with open(toml, "w") as f:
            f.write(f'[library]\nscores = "{self.scores}"\noutput = "{self.out}"\n'
                    f'[device]\nadb = "{self.headset.adb}"\ndir = "{PV}"\n'
                    f'[hands]\ndevice_dir = "{HANDS}"\nwaterfall_deploy = "{self.cfg.waterfall_deploy}"\n'
                    f'sync_dir = "{self.sync_dir}"\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--config", toml, "hands", "sync", "--min-free-mb", "0", "--no-notify"] + list(argv))
        return rc, buf.getvalue()

    def test_cli_dry_run_then_once(self):
        rc, out = self.run_cli("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("would apply: 1 note moved to the other hand", out)
        self.assertIn("dry run: 1 would apply; nothing was written", out)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), self.original)
        self.assertFalse(os.path.exists(self.sync_dir))
        self.assertFalse(os.path.exists(os.path.join(self.out, ".hands")))
        self.assertEqual(self.headset.files(), [self.name])

        rc, out = self.run_cli("--once")
        self.assertEqual(rc, 0, out)
        self.assertIn("1 applied", out)
        self.assertIn((2 * Q, 64), self.right_hand())
        self.assertEqual(self.headset.files(), ["applied"])
        with open(os.path.join(self.sync_dir, "sync.log")) as f:
            self.assertIn("applying (1 note moved", f.read())


if __name__ == "__main__":
    unittest.main()
