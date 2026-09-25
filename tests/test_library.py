"""Library pipeline rules, on throwaway score folders."""

import json
import os
import shutil
import tempfile
import time
import unittest

from pianovision.convert import convert_mscz
from pianovision.device import execute_deploy, plan_deploy
from pianovision.library import Config, Library

from tests.scoregen import make_score


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scores = os.path.join(self.tmp, "Scores")
        self.out = os.path.join(self.tmp, "PianoVision")
        os.makedirs(self.scores)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def lib(self) -> Library:
        return Library(Config(scores=self.scores, output=self.out, jobs=1), log=lambda *_: None)

    def score(self, rel, **kw) -> str:
        path = os.path.join(self.scores, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return make_score(path, **kw)

    def outputs(self):
        return sorted(f for f in os.listdir(self.out) if f.endswith(".json") and not f.startswith("."))

    def attic(self):
        root = os.path.join(self.out, ".attic")
        return sorted(f for _d, _s, fs in os.walk(root) for f in fs) if os.path.isdir(root) else []


class LibraryTest(Base):
    def test_new_then_unchanged(self):
        self.score("a.mscz")
        rep = self.lib().build()
        self.assertEqual([w[1] for w in rep.written], ["test_Test_Song.json"])
        rep = self.lib().build()
        self.assertEqual((rep.kept, rep.written), (1, []))

    def test_resave_without_changes_is_not_a_change(self):
        p = self.score("a.mscz")
        self.lib().build()
        time.sleep(0.01)
        make_score(p)                                   # same content, new mtime
        os.utime(p, None)
        self.assertEqual(self.lib().build().written, [])

    def test_edited_score_is_rerendered_and_old_output_kept_in_attic(self):
        p = self.score("a.mscz")
        self.lib().build()
        make_score(p, pitches=(60, 60, 60, 60))
        rep = self.lib().build()
        self.assertEqual([(w[1], w[2]) for w in rep.written], [("test_Test_Song.json", "score changed")])
        self.assertEqual(self.attic(), ["test_Test_Song.json"])

    def test_moved_score_keeps_its_output(self):
        p = self.score("a.mscz")
        self.lib().build()
        os.makedirs(os.path.join(self.scores, "Sub"))
        shutil.move(p, os.path.join(self.scores, "Sub", "renamed.mscz"))
        rep = self.lib().build()
        self.assertEqual(rep.moved, [("a.mscz", "Sub/renamed.mscz")])
        self.assertEqual(rep.written, [])
        self.assertEqual(self.outputs(), ["test_Test_Song.json"])

    def test_excluding_by_zip_rename_retires_output(self):
        p = self.score("a.mscz")
        self.lib().build()
        os.rename(p, p[:-5] + ".zip")
        rep = self.lib().build()
        self.assertEqual(rep.retired, [("a.mscz", "test_Test_Song.json", "excluded (renamed to .zip)")])
        self.assertEqual(self.outputs(), [])
        self.assertEqual(self.attic(), ["test_Test_Song.json"])

    def test_title_collision_gets_suffix(self):
        self.score("Film/Song-Composer.mscz")
        self.score("Film/Song-Composer-piano-solo.mscz", pitches=(48, 50, 52, 53))
        self.lib().build()
        self.assertEqual(self.outputs(), ["test_Test_Song.json", "test_Test_Song_piano_solo.json"])

    def test_adopts_identical_legacy_output(self):
        p = self.score("a.mscz")
        os.makedirs(self.out)
        with open(os.path.join(self.out, "test_Test_Song.json"), "wb") as f:
            f.write(convert_mscz(p).data)
        rep = self.lib().build()
        self.assertEqual([a[2] for a in rep.adopted], ["identical to render"])
        self.assertEqual(rep.written, [])

    def test_adopts_legacy_output_with_same_notes_as_is(self):
        p = self.score("a.mscz")
        os.makedirs(self.out)
        doc = json.loads(convert_mscz(p).data)
        doc["tempos"][0]["bpm"] = 119.99999                 # e.g. an older MuseScore's tempo rounding
        legacy = json.dumps(doc).encode()
        with open(os.path.join(self.out, "test_Test_Song.json"), "wb") as f:
            f.write(legacy)
        rep = self.lib().build()
        self.assertEqual([a[2] for a in rep.adopted], ["same notes as render (pinned as is)"])
        with open(os.path.join(self.out, "test_Test_Song.json"), "rb") as f:
            self.assertEqual(f.read(), legacy)

    def test_stale_legacy_output_is_replaced(self):
        p = self.score("a.mscz")
        os.makedirs(self.out)
        other = make_score(os.path.join(self.tmp, "other.mscz"), pitches=(40, 41, 42, 43))
        with open(os.path.join(self.out, "test_Test_Song.json"), "wb") as f:
            f.write(convert_mscz(other).data)
        rep = self.lib().build()
        self.assertEqual(rep.adopted, [])
        self.assertEqual(self.attic(), ["test_Test_Song.json"])
        with open(os.path.join(self.out, "test_Test_Song.json"), "rb") as f:
            self.assertEqual(f.read(), convert_mscz(p).data)

    def test_hand_edited_output_is_a_conflict_not_overwritten(self):
        self.score("a.mscz")
        self.lib().build()
        path = os.path.join(self.out, "test_Test_Song.json")
        with open(path, "ab") as f:
            f.write(b" ")
        rep = self.lib().build()
        self.assertEqual([c[0] for c in rep.conflicts], ["a.mscz"])
        self.assertTrue(open(path, "rb").read().endswith(b" "))
        rep = self.lib().build(force=True)
        self.assertEqual([w[2] for w in rep.written], ["forced"])

    def test_non_scores_are_reported_not_rendered(self):
        with open(os.path.join(self.scores, "fake.mscz"), "wb") as f:
            f.write(b"MThd\x00\x00\x00\x06")
        self.score("real.mscz")
        with open(os.path.join(self.scores, "real.mid"), "wb") as f:
            f.write(b"MThd")
        st = self.lib().status()
        self.assertEqual(st["scan"].invalid, {"fake.mscz": "a MIDI file with an .mscz extension"})
        self.assertEqual(st["scan"].companion_midi, ["real.mid"])

    def test_orphans_are_reported_and_prunable(self):
        self.score("a.mscz")
        os.makedirs(self.out)
        with open(os.path.join(self.out, "zzzz_Old.json"), "w") as f:
            f.write("{}")
        lib = self.lib()
        lib.build()
        self.assertEqual(lib.orphans(), ["zzzz_Old.json"])
        lib.build(prune_orphans=True)
        self.assertEqual(self.outputs(), ["test_Test_Song.json"])
        self.assertEqual(self.attic(), ["zzzz_Old.json"])


class FakeAdb:
    """In-memory stand-in for the device folder."""

    def __init__(self, files):
        self.files = dict(files)            # name -> md5
        self.serial = "FAKE1"
        self.removed = []

    def connect(self):
        return self.serial

    def model(self):
        return "Quest 3"

    def md5s(self, _d):
        return dict(self.files)

    def mkdir(self, _d):
        pass

    def push(self, paths, _d):
        from pianovision.device import md5_file
        for p in paths:
            self.files[os.path.basename(p)] = md5_file(p)

    def remove(self, _d, names):
        for n in names:
            self.removed.append(n)
            self.files.pop(n)


class DeployTest(Base):
    def test_app_files_are_never_removed(self):
        self.score("a.mscz")
        self.score("b.mscz", title="Other")
        lib = self.lib()
        lib.build()
        adb = FakeAdb({"finger_position_recordings.json": "0" * 32})
        plan = plan_deploy(lib, adb)
        self.assertEqual(sorted(plan.push), ["test_Other.json", "test_Test_Song.json"])
        self.assertEqual(plan.foreign, ["finger_position_recordings.json"])
        execute_deploy(lib, adb, plan, prune=True)
        self.assertEqual(adb.removed, [])
        # retire b: its file was deployed by us, so --prune may remove it; the app file stays
        os.rename(os.path.join(self.scores, "b.mscz"), os.path.join(self.scores, "b.zip"))
        lib = self.lib()
        lib.build()
        plan = plan_deploy(lib, adb)
        self.assertEqual((plan.push, plan.remove), ([], ["test_Other.json"]))
        execute_deploy(lib, adb, plan, prune=False)
        self.assertIn("test_Other.json", adb.files)
        execute_deploy(lib, adb, plan_deploy(lib, adb), prune=True)
        self.assertEqual(sorted(adb.files), ["finger_position_recordings.json", "test_Test_Song.json"])

    def test_force_push_resends_current_files(self):
        self.score("a.mscz")
        lib = self.lib()
        lib.build()
        adb = FakeAdb({})
        execute_deploy(lib, adb, plan_deploy(lib, adb))
        self.assertEqual(plan_deploy(lib, adb).push, [])
        self.assertEqual(plan_deploy(lib, adb, force=True).push, ["test_Test_Song.json"])


if __name__ == "__main__":
    unittest.main()
