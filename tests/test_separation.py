"""Vocals stems (pianovision.separation): finding a separator, the cache, the singing check. No model runs here."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from pianovision import separation as SEP


class StatusTest(unittest.TestCase):
    def test_missing_names_the_environments_with_torch(self):
        mods = {"/e/a/bin/python": {"demucs": False, "torch": True},
                "/e/b/bin/python": {"demucs": False, "torch": False}}
        with mock.patch.object(SEP, "candidate_pythons", return_value=list(mods)), \
                mock.patch.object(SEP, "has_modules", side_effect=lambda py, m: mods[py]):
            st = SEP.status()
        self.assertFalse(st["available"])
        self.assertEqual(st["torch"], ["/e/a/bin/python"])
        self.assertIn("pip install demucs", st["missing"])

    def test_the_first_python_with_demucs(self):
        mods = {"/x/bin/python": {"demucs": False, "torch": True}, "/y/bin/python": {"demucs": True, "torch": True}}
        with mock.patch.object(SEP, "candidate_pythons", return_value=list(mods)), \
                mock.patch.object(SEP, "has_modules", side_effect=lambda py, m: mods[py]):
            st = SEP.status()
        self.assertEqual((st["available"], st["python"]), (True, "/y/bin/python"))

    def test_configured_python_comes_first(self):
        with mock.patch("glob.glob", return_value=["/c/envs/a/bin/python"]), \
                mock.patch("os.path.isfile", return_value=True):
            self.assertEqual(SEP.candidate_pythons("/my/python")[:2], ["/my/python", "/c/envs/a/bin/python"])


class StemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nw-sep-test-")
        self.src = os.path.join(self.tmp, "song.mp3")
        with open(self.src, "wb") as f:
            f.write(b"audio" * 100)
        self.stems = os.path.join(self.tmp, "stems")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_unavailable(self):
        with mock.patch.object(SEP, "status", return_value={"available": False, "missing": "no demucs anywhere"}):
            with self.assertRaises(SEP.SeparationUnavailable) as cm:
                SEP.vocals_stem(self.src, self.stems, log=lambda m: None)
        self.assertIn("no demucs anywhere", str(cm.exception))

    def test_separated_once_then_cached_by_content(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            out = argv[argv.index("--out-dir") + 1]
            open(os.path.join(out, "vocals.wav"), "wb").close()
            with open(os.path.join(out, "tool.json"), "w") as f:
                json.dump({"demucs": "4.0.1", "device": "cuda"}, f)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(SEP, "status", return_value={"available": True, "python": "/y/bin/python"}), \
                mock.patch("subprocess.run", side_effect=fake_run):
            a = SEP.vocals_stem(self.src, self.stems, script="sep.py", log=lambda m: None)
            b = SEP.vocals_stem(self.src, self.stems, script="sep.py", log=lambda m: None)
        self.assertEqual(a, b)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["/y/bin/python", "sep.py"])
        self.assertIn("htdemucs_ft", calls[0])
        self.assertTrue(a.startswith(os.path.join(self.stems, SEP.content_hash(self.src), "htdemucs_ft")))
        with open(os.path.join(os.path.dirname(a), "meta.json")) as f:
            meta = json.load(f)
        self.assertEqual((meta["demucs"], meta["device"], meta["input"]), ("4.0.1", "cuda", self.src))
        self.assertIn("wall_seconds", meta)
        # another file's content: another stem
        with open(self.src, "ab") as f:
            f.write(b"more")
        self.assertIsNone(SEP.cached_vocals(self.stems, self.src, "htdemucs_ft"))


class ActivityTest(unittest.TestCase):
    def test_share_of_blocks_with_singing(self):
        vocals = [-80.0] * 10 + [-20.0] * 10          # sings in the second half
        mix = [-12.0] * 20
        a = SEP.activity_from_levels(vocals, mix, window=0.5)
        self.assertEqual((a["activity"], a["blocks"]), (0.5, 20))
        self.assertLess(a["vocals_to_mix_db"], -10)
        self.assertEqual(SEP.activity_from_levels(vocals, mix, start=5.0, window=0.5)["activity"], 1.0)
        self.assertEqual(SEP.activity_from_levels(vocals, mix, end=4.0, window=0.5)["activity"], 0.0)
        # a stem far under its mix is bleed, not singing
        self.assertEqual(SEP.activity_from_levels([-40.0] * 4, [-10.0] * 4)["activity"], 0.0)


if __name__ == "__main__":
    unittest.main()
