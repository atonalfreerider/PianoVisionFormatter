"""The Piano Library window's logic (pianovision.guimodel), without GTK: row statuses, the headset
columns, device status parsing, the push plans and the exact lists the confirmation dialogs show,
the hand-edit approval gate, and the job plumbing (per-thread logs, streamed subprocesses, Cancel)."""

import io
import os
import shutil
import sys
import threading
import time
import types
import unittest

from pianovision import guimodel as M
from pianovision.device import md5_file
from pianovision.library import Config

from tests.scoregen import make_score
from tests.test_library import Base, FakeAdb


class SnapshotTest(Base):
    def md5(self, name):
        return md5_file(os.path.join(self.out, name))

    def test_every_status(self):
        self.score("a.mscz", title="Alpha")
        self.score("b.mscz", title="Bravo")
        self.score("c.mscz", title="Charlie")
        self.score("d.mscz", title="Delta")
        self.score("e.mscz", title="Echo")
        self.score("p.mscz", title="Pinned")
        lib = self.lib()
        lib.build()
        lib.manifest.entries["p.mscz"]["reproducible"] = False
        lib.manifest.save()
        make_score(os.path.join(self.scores, "a.mscz"), title="Alpha", pitches=(60, 60, 60, 60))    # edited
        shutil.move(os.path.join(self.scores, "b.mscz"), os.path.join(self.scores, "b2.mscz"))      # moved
        os.rename(os.path.join(self.scores, "c.mscz"), os.path.join(self.scores, "c.zip"))          # excluded
        os.remove(os.path.join(self.scores, "e.mscz"))                                              # deleted
        with open(os.path.join(self.out, "test_Delta.json"), "a") as f:                             # changed outside
            f.write(" ")
        new = self.score("n.mscz", title="New One")
        self.score("f.mscz", title="Fails")
        with open(os.path.join(self.scores, "bad.mscz"), "wb") as f:                                # not a score
            f.write(b"MThd\x00\x00\x00\x06")
        with open(os.path.join(self.scores, "stray.mid"), "wb") as f:
            f.write(b"MThd")
        with open(os.path.join(self.out, "zzzz_Orphan.json"), "w") as f:
            f.write("{}")
        st_f = os.stat(os.path.join(self.scores, "f.mscz"))
        report = {"time": "2026-09-29T10:00:00",
                  "failed": [{"rel": "f.mscz", "error": "ValueError: boom", "mtime": st_f.st_mtime,
                              "size": st_f.st_size},
                             # n.mscz failed once, but it was saved since: no longer "failed"
                             {"rel": "n.mscz", "error": "old", "mtime": 1.0, "size": os.path.getsize(new)}]}
        snap = M.snapshot(self.lib(), report=report)
        by = {r.rel: r for r in snap.rows}
        self.assertEqual({rel: r.status for rel, r in by.items()}, {
            "a.mscz": "edited", "b2.mscz": "moved", "c.zip": "excluded", "e.mscz": "retire",
            "d.mscz": "conflict", "n.mscz": "new", "f.mscz": "failed", "bad.mscz": "invalid", "p.mscz": "pinned"})
        self.assertEqual(by["f.mscz"].detail, "ValueError: boom")
        self.assertEqual((by["n.mscz"].title, by["n.mscz"].composer, by["n.mscz"].output), ("New One", "Tester Jane", ""))
        self.assertEqual(by["b2.mscz"].output, "test_Bravo.json")
        self.assertEqual(by["b2.mscz"].detail, "from b.mscz")
        self.assertIn("test_Echo.json", by["e.mscz"].detail)
        # built scores know their parts; new ones do not yet
        self.assertEqual((by["p.mscz"].simplified, by["p.mscz"].orchestra), (False, False))
        self.assertEqual((by["n.mscz"].simplified, by["n.mscz"].orchestra), (None, None))
        self.assertEqual(snap.orphans, ["zzzz_Orphan.json"])
        self.assertEqual(snap.stray_midi, ["stray.mid"])
        self.assertEqual(snap.to_build, 5)          # edited, moved, retire, new, failed
        self.assertEqual(snap.build_time, "2026-09-29T10:00:00")
        s1, s2 = M.summary_lines(snap)
        self.assertIn("1 pinned", s2)
        self.assertIn("1 orphan songs, 1 stray .mid files", s2)
        # search: every word somewhere in composer / title / file / status
        self.assertTrue(by["n.mscz"].matches("tester new"))
        self.assertTrue(by["f.mscz"].matches("failed"))
        self.assertFalse(by["n.mscz"].matches("tester bravo"))

    def test_headset_columns(self):
        self.score("a.mscz", title="Alpha")
        self.score("b.mscz", title="Bravo")
        self.score("c.mscz", title="Charlie")
        lib = self.lib()
        lib.build()
        a, b = "test_Alpha.json", "test_Bravo.json"
        pa, pb = a[:-5] + ".parts.json", b[:-5] + ".parts.json"
        pv = {a: self.md5(a), b: "0" * 32}
        nw = {a: self.md5(a), pa: md5_file(os.path.join(self.out, "NoteWaterfall", pa)),
              b: self.md5(b), pb: "0" * 32}                   # Bravo's parts are stale
        snap = M.snapshot(lib, device=M.DeviceFiles(pianovision=pv, waterfall=nw), report={})
        by = {r.rel: r for r in snap.rows}
        self.assertEqual([(by[r].pianovision, by[r].waterfall) for r in ("a.mscz", "b.mscz", "c.mscz")],
                         [("current", "current"), ("old", "old"), ("missing", "missing")])
        self.assertEqual(snap.on_device(M.PIANOVISION_APP), (1, 3))
        # no headset: the columns are unknown, not "missing"
        snap = M.snapshot(lib, device=None, report={})
        self.assertEqual({(r.pianovision, r.waterfall) for r in snap.rows}, {("", "")})
        # a listing that failed for one app
        snap = M.snapshot(lib, device=M.DeviceFiles(pianovision=pv, waterfall=None), report={})
        self.assertEqual({r.waterfall for r in snap.rows}, {""})

    def test_app_state(self):
        self.assertEqual(M.app_state([("s.json", "1")], None), "")
        self.assertEqual(M.app_state([("s.json", "1")], {}), "missing")
        self.assertEqual(M.app_state([("s.json", "1"), ("s.parts.json", None)], {"s.json": "1"}), "current")
        self.assertEqual(M.app_state([("s.json", "1"), ("s.parts.json", "2")], {"s.json": "1"}), "old")

    def test_title_change_is_offered_for_rename(self):
        p = self.score("m.mscz", title="Une nuit sur le Mont Chauve Bruits souterrains de voix surnaturelles")
        lib = self.lib()
        lib.build()
        old = lib.manifest.entries["m.mscz"]["output"]
        make_score(p, title="Une nuit sur le Mont Chauve")
        lib = self.lib()
        lib.build()
        snap = M.snapshot(lib, report={})
        self.assertEqual(snap.rows[0].new_name, "test_Une_nuit_sur_le_Mont_Chauve.json")
        self.assertEqual(M.rename_candidates(lib), [("m.mscz", old, "test_Une_nuit_sur_le_Mont_Chauve.json", True)])
        self.assertEqual(lib.rename(restrict=set()), [])                  # nothing ticked: nothing renamed
        self.assertTrue(os.path.exists(os.path.join(self.out, old)))
        self.assertEqual(lib.rename(restrict={"m.mscz"}), [("m.mscz", old, "test_Une_nuit_sur_le_Mont_Chauve.json")])
        self.assertTrue(os.path.exists(os.path.join(self.out, "test_Une_nuit_sur_le_Mont_Chauve.json")))


class OrphanTest(Base):
    def test_retire_moves_only_confirmed_names_that_are_still_orphans(self):
        self.score("a.mscz")
        os.makedirs(self.out)
        for n in ("zzzz_One.json", "zzzz_Two.json", "zzzz_Three.json"):
            with open(os.path.join(self.out, n), "w") as f:
                f.write("{}")
        lib = self.lib()
        lib.build()
        confirmed = ["zzzz_One.json", "zzzz_Two.json", "not_an_orphan.json"]
        lines = M.orphan_lines(self.out, confirmed[:2])
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith(os.path.join(self.out, "zzzz_One.json")))
        os.remove(os.path.join(self.out, "zzzz_Two.json"))        # gone since the dialog: skipped
        rep = self.lib().retire_orphans(confirmed)
        self.assertEqual([r[1] for r in rep.retired], ["zzzz_One.json"])
        self.assertEqual(self.attic(), ["zzzz_One.json"])          # moved, not deleted
        self.assertEqual(self.lib().orphans(), ["zzzz_Three.json"])
        self.assertEqual(self.lib().manifest.retired[-1]["output"], "zzzz_One.json")


class DeviceStatusTest(unittest.TestCase):
    DEVICES = ("List of devices attached\n"
               "2G0YC1ZF8F063B         device usb:1-4 product:eureka model:Quest_3 device:eureka transport_id:3\n"
               "\n")

    def test_parse_and_pick(self):
        devs = M.parse_devices(self.DEVICES)
        st = M.pick_device(devs)
        self.assertEqual((st.state, st.serial, st.model), ("connected", "2G0YC1ZF8F063B", "Quest 3"))
        self.assertEqual(M.pick_device([]).state, "none")
        self.assertEqual(M.pick_device([("X", "unauthorized", "usb:1")]).state, "unauthorized")
        self.assertEqual(M.pick_device([("X", "offline", "")]).state, "offline")
        self.assertEqual(M.pick_device(devs, serial="OTHER").state, "none")
        two = devs + [("emulator-5554", "device", "model:sdk")]
        self.assertEqual(M.pick_device(two).serial, "2G0YC1ZF8F063B")          # the Quest
        self.assertEqual(M.pick_device(two + [("Q2", "device", "model:Quest_2")]).state, "several")
        self.assertEqual(M.parse_devices("* daemon started successfully\nList of devices attached\n"), [])

    def test_details(self):
        self.assertEqual(M.parse_wakefulness("Power Manager State:\n  mWakefulness=Asleep\n"), "Asleep")
        self.assertEqual(M.parse_wakefulness(""), "")
        df = ("Filesystem     1K-blocks     Used Available Use% Mounted on\n"
              "/dev/fuse      235000000 90000000 145000000  39% /storage/emulated\n")
        self.assertEqual(M.parse_df(df), 145000000 * 1024)
        self.assertIsNone(M.parse_df("df: /sdcard: No such file"))
        st = M.DeviceState("connected", serial="S", model="Quest 3", wakefulness="Asleep", free_bytes=148_480_000_000)
        self.assertEqual(st.label(), "Quest 3 connected (asleep) · 148.5 GB free")
        self.assertIn("unauthorized", M.DeviceState("unauthorized").label())
        self.assertEqual(M.human_bytes(1500), "2 KB")

    def test_probe_without_adb(self):
        self.assertEqual(M.probe_device("/nonexistent/adb").state, "no-adb")

    def test_songs_dir_next_to_hand_edits(self):
        cfg = Config(scores="", output="", hands_dir="/sdcard/Android/data/x/files/HandEdits/")
        self.assertEqual(M.waterfall_songs_dir(cfg), "/sdcard/Android/data/x/files/Songs")


class PushPlanTest(Base):
    def test_pianovision_plan_and_removal_list(self):
        self.score("a.mscz")
        self.score("b.mscz", title="Other")
        lib = self.lib()
        lib.build()
        adb = FakeAdb({"finger_position_recordings.json": "0" * 32})
        p = M.pianovision_plan(lib, adb)
        self.assertEqual([n for n, _ in p.push], ["test_Other.json", "test_Test_Song.json"])
        self.assertTrue(all(size > 0 for _, size in p.push))
        self.assertEqual((p.remove, p.foreign), ([], ["finger_position_recordings.json"]))
        from pianovision.device import execute_deploy
        execute_deploy(lib, adb, p.raw)
        os.rename(os.path.join(self.scores, "b.mscz"), os.path.join(self.scores, "b.zip"))
        lib = self.lib()
        lib.build()
        p = M.pianovision_plan(lib, adb)
        self.assertEqual((p.push, p.unchanged, p.remove), ([], 1, ["test_Other.json"]))
        # the confirmation lists exactly the files that go, and only for the apps ticked for it
        self.assertEqual(M.removal_lines([p], [M.PIANOVISION_APP]),
                         [f"PianoVision: {lib.cfg.device_dir}/test_Other.json"])
        self.assertEqual(M.removal_lines([p], []), [])
        execute_deploy(lib, adb, p.raw, prune=True)
        self.assertEqual(adb.removed, ["test_Other.json"])
        self.assertIn("finger_position_recordings.json", adb.files)

    def test_no_device_is_an_error_in_the_plan(self):
        from pianovision.device import DeviceError
        self.score("a.mscz")
        lib = self.lib()
        lib.build()

        class NoAdb(FakeAdb):
            def connect(self):
                raise DeviceError("no device connected")
        p = M.pianovision_plan(lib, NoAdb({}))
        self.assertEqual(p.error, "no device connected")
        self.assertEqual(M.removal_lines([p], [M.PIANOVISION_APP]), [])
        self.assertIn("no device connected", p.summary())

    def fake_waterfall(self, on_device, deployed_before, want):
        mod = types.SimpleNamespace()
        mod.DEVICE_SONGS = "/sdcard/Android/data/nw/files/Songs"
        mod.MANIFEST_NAME = ".deploy_manifest.json"
        mod.DEFAULT_AUDIO_MAP = ""

        class Adb:
            def __init__(self, serial):
                self.serial = serial

            def connect(self):
                return self.serial
        mod.Adb = Adb
        mod.load_audio_map = lambda _p: {}
        mod.desired_files = lambda library, amap, only, no_audio, dry_run: dict(want)
        mod.device_md5s = lambda adb, create: dict(on_device)
        mod.read_device_manifest = lambda adb: set(deployed_before)
        mod.md5_file = md5_file
        mod.md5_bytes = lambda b: __import__("hashlib").md5(b).hexdigest()
        return mod

    def test_waterfall_plan_is_deploy_songs_logic(self):
        song = os.path.join(self.tmp, "s.json")
        with open(song, "w") as f:
            f.write("{}")
        want = {"s.json": ("path", song), "s.audio.json": ("bytes", b'{"offset": 1}')}
        on_device = {"s.json": md5_file(song), "gone.json": "1", "gone.mp3": "2", "theirs.json": "3",
                     ".deploy_manifest.json": "4"}
        mod = self.fake_waterfall(on_device, {"s.json", "gone.json", "gone.mp3", "never-there.json"}, want)
        p = M.waterfall_plan(mod, self.out, "SERIAL")
        self.assertEqual((p.push, p.unchanged), ([("s.audio.json", 13)], 1))
        self.assertEqual(p.remove, ["gone.json", "gone.mp3"])      # deployed earlier, still there, not wanted
        self.assertEqual(p.foreign, ["theirs.json"])
        self.assertEqual(M.removal_lines([p], [M.WATERFALL_APP]),
                         ["Note Waterfall: /sdcard/Android/data/nw/files/Songs/gone.json",
                          "Note Waterfall: /sdcard/Android/data/nw/files/Songs/gone.mp3"])

    def test_waterfall_errors_and_command(self):
        mod = self.fake_waterfall({}, set(), {})

        def fail(_s):
            raise RuntimeError("headset is unauthorized")
        mod.Adb = lambda serial: types.SimpleNamespace(connect=lambda: fail(serial))
        self.assertEqual(M.waterfall_plan(mod, self.out, "S").error, "headset is unauthorized")
        cfg = Config(scores="", output="", waterfall_deploy="python3 /x/Tools/deploy_songs.py")
        self.assertEqual(M.waterfall_script(cfg), "/x/Tools/deploy_songs.py")
        self.assertEqual(M.waterfall_argv(cfg, "/lib", "S", prune=False),
                         ["python3", "/x/Tools/deploy_songs.py", "--library", "/lib", "--serial", "S"])
        self.assertEqual(M.waterfall_argv(cfg, "/lib", "", prune=True)[-2:], ["--prune", "--yes"])
        self.assertEqual(M.waterfall_script(Config(scores="", output="")), M.DEFAULT_WATERFALL_SCRIPT)

    def test_the_real_deploy_script_loads(self):
        script = M.DEFAULT_WATERFALL_SCRIPT
        if not os.path.exists(script):
            self.skipTest("Note Waterfall is not checked out here")
        mod = M.load_waterfall(script)
        for name in ("Adb", "desired_files", "device_md5s", "read_device_manifest", "md5_file", "md5_bytes",
                     "load_audio_map", "DEFAULT_AUDIO_MAP", "DEVICE_SONGS", "MANIFEST_NAME"):
            self.assertTrue(hasattr(mod, name), name)


class ReviewGateTest(unittest.TestCase):
    """The GUI answers handedits.apply_reviews' own question, per score, through a dialog."""

    def setUp(self):
        from tests import test_handedits as T
        self.T = T
        self.case = T.ApplyTest("test_nothing_is_written_without_a_yes")
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def test_no_means_nothing_is_written(self):
        from pianovision import handedits as H
        lib, rv, original = self.case.reviewed()
        seen = []
        gate = M.ReviewGate([rv], lambda r: seen.append(r.rel) or False)
        rep = H.apply_reviews(lib, [rv], ask=gate, log=lambda *_: None)
        self.assertEqual((rep.written, rep.declined, seen), ([], [rv.rel], [rv.rel]))
        with open(self.case.path, "rb") as f:
            self.assertEqual(f.read(), original)

    def test_yes_applies_with_the_backup(self):
        from pianovision import handedits as H
        lib, rv, original = self.case.reviewed()
        rep = H.apply_reviews(lib, [rv], ask=M.ReviewGate([rv], lambda r: True), log=lambda *_: None)
        self.assertEqual(rep.written, [rv.rel])
        with open(rep.backups[0], "rb") as f:
            self.assertEqual(f.read(), original)
        lines = M.review_lines(rv)
        self.assertEqual([(ln.pitch, ln.from_hand, ln.to_hand, ln.status) for ln in lines],
                         [("E4", "left", "right", "move")])
        self.assertTrue(lines[0].where.startswith("m1 beat"))

    def test_an_unexpected_question_is_a_no(self):
        lib, rv, _original = self.case.reviewed()
        gate = M.ReviewGate([rv], lambda r: True)
        self.assertEqual(gate("Overwrite something/else.mscz? [y/N] "), "n")
        self.assertEqual(gate("one question too many"), "n")


class JobTest(unittest.TestCase):
    def test_thread_stdout_routes_each_threads_prints(self):
        real = io.StringIO()
        out = M.ThreadStdout(real)
        mine, theirs = [], []
        old = sys.stdout
        sys.stdout = out
        try:
            def worker():
                out.set_sink(theirs.append)
                print("from the job")
                out.set_sink(None)
            t = threading.Thread(target=worker)
            t.start()
            t.join()
            out.set_sink(mine.append)
            print("from the window")
            out.set_sink(None)
            print("to the terminal")
        finally:
            sys.stdout = old
        self.assertEqual("".join(theirs), "from the job\n")
        self.assertEqual("".join(mine), "from the window\n")
        self.assertEqual(real.getvalue(), "to the terminal\n")

    def test_run_streaming_lines_and_exit_code(self):
        lines = []
        rc = M.run_streaming([sys.executable, "-c", "import sys; print('a'); print('b', file=sys.stderr); sys.exit(3)"],
                             lines.append)
        self.assertEqual((rc, sorted(lines)), (3, ["a", "b"]))

    def test_cancel_stops_the_process_group(self):
        token = M.CancelToken()
        lines = []
        code = "import subprocess, sys, time; print('started', flush=True); " \
               "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
        threading.Timer(0.8, token.cancel).start()
        t = time.monotonic()
        rc = M.run_streaming([sys.executable, "-c", code], lines.append, cancel=token)
        self.assertLess(time.monotonic() - t, 20)
        self.assertLess(rc, 0)
        self.assertTrue(token.cancelled)
        self.assertEqual(lines, ["started"])
        self.assertRaises(M.Cancelled, token.check)

    def test_formatter_argv(self):
        a = types.SimpleNamespace(config="pianovision.toml", scores=None, output=None)
        argv = M.formatter_argv(a, "build", "--report", "/r.json")
        self.assertEqual(argv[:3], [sys.executable, "-m", "pianovision"])
        self.assertEqual(argv[3:5], ["--config", os.path.abspath("pianovision.toml")])
        self.assertEqual(argv[5:], ["build", "--report", "/r.json"])


class BuildReportTest(Base):
    def test_build_report_is_what_the_window_reads(self):
        import contextlib
        from pianovision.cli import main
        self.score("a.mscz")
        report = M.build_report_path(self.out)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(["--scores", self.scores, "--output", self.out, "build", "--report", report])
        self.assertEqual(rc, 0)
        doc = M.read_build_report(self.out)
        self.assertEqual(([w["output"] for w in doc["written"]], doc["failed"]), (["test_Test_Song.json"], []))
        # the GUI's folder is not a song
        self.assertEqual(self.outputs(), ["test_Test_Song.json"])
        self.assertEqual(self.lib().orphans(), [])


if __name__ == "__main__":
    unittest.main()
