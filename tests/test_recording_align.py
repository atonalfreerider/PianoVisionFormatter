"""tools/recording_align.py end to end on synthetic audio with a known answer: a "recording" of a song made from its
own notes in another timbre, transposed, slower in its first half and faster in its second, with an intro the song
does not have and four of the song's measures left out.

Needs numpy, scipy, soundfile and librosa (and ffmpeg with rubberband): run with the [recordings] python, e.g.

    ~/miniconda3/envs/midi-retime/bin/python -m unittest tests.test_recording_align

Skipped under a Python without them.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

try:
    import numpy as np
    import soundfile as sf
    import librosa  # noqa: F401
    HAVE = True
except ImportError:                      # the formatter's own Python: stdlib only
    HAVE = False

from pianovision import recmap as RM

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

BPM, BEATS, MEASURES = 120.0, 4, 28
KEY = 2                                   # the recording is two semitones higher
MISSING = (14, 18)                        # the song's measures 15-18 (0-based 14..17) are not in the recording
INTRO = 3.0


def song_ref():
    """28 measures of 4/4 at 120 bpm: a chord per measure (no two measures alike) and a melody in eighths."""
    rng = np.random.RandomState(11)
    roots = rng.randint(0, 12, MEASURES)
    notes, measures = [], []
    mlen = BEATS * 60.0 / BPM
    for m in range(MEASURES):
        t0 = m * mlen
        root = 48 + int(roots[m])
        third = 4 if m % 3 else 3
        for p in (root, root + third + 12, root + 7 + 12):
            notes.append([round(t0, 5), round(mlen * 0.95, 5), p, 0.7, "p"])
        for k in range(8):
            notes.append([round(t0 + k * mlen / 8, 5), round(mlen / 8 * 0.9, 5), 72 + int(rng.randint(0, 12)), 0.8,
                          "p"])
        measures.append({"index": m, "number": m + 1, "pass": 1, "start": t0, "end": t0 + mlen,
                         "timesig": [4, 4], "beats": [t0 + b * 60.0 / BPM for b in range(BEATS)]})
    end = MEASURES * mlen
    return {"format": 1, "song": "test.json", "song_end": end, "measures": measures, "notes": sorted(notes)}


def warp(t):
    """Song time -> recording time (None for the measures the recording leaves out)."""
    mlen = BEATS * 60.0 / BPM
    a, b = MISSING[0] * mlen, MISSING[1] * mlen
    if t < a:
        return INTRO + 1.15 * t
    if t < b:
        return None
    return INTRO + 1.15 * a + 0.9 * (t - b)


@unittest.skipUnless(HAVE, "needs numpy, soundfile and librosa (run with the [recordings] python)")
class AlignRetimeVerifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, TOOLS)
        import recording_align as A
        cls.A = A
        cls.tmp = tempfile.mkdtemp(prefix="nw-align-test-")
        cls.ref = song_ref()
        cls.ref_path = os.path.join(cls.tmp, "ref.json")
        with open(cls.ref_path, "w") as f:
            json.dump(cls.ref, f)
        rec_notes = []
        for st, du, p, v, _part in cls.ref["notes"]:
            a, b = warp(st), warp(st + du * 0.999)
            if a is None or b is None:
                continue
            rec_notes.append([a, b - a, p + KEY, v, "o"])            # held notes, another timbre
        rec = {"notes": rec_notes, "song_end": max(n[0] + n[1] for n in rec_notes) + 4.0}
        y = A.synthesize(rec)
        rng = np.random.RandomState(5)
        y[:int(INTRO * A.SR)] += (0.05 * rng.randn(int(INTRO * A.SR))).astype(np.float32)   # an intro: noise
        y += (0.01 * rng.randn(len(y))).astype(np.float32)
        cls.rec_path = os.path.join(cls.tmp, "rec.wav")
        sf.write(cls.rec_path, np.stack([y, y], axis=1), A.SR)
        cls.align_path = os.path.join(cls.tmp, "align.json")
        A.main(["align", "--ref", cls.ref_path, "--audio", cls.rec_path, "--out", cls.align_path])
        with open(cls.align_path) as f:
            cls.al = json.load(f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_key_and_tempo(self):
        self.assertEqual(self.al["key"]["offset"], KEY)
        self.assertAlmostEqual(self.al["tempo"]["ratio"], (1.15 * 14 + 0.9 * 10) / 24, delta=0.06)

    def test_map_follows_the_known_warp(self):
        tm = RM.TimeMap.from_dict(self.al["map"])
        errs = []
        for m in self.ref["measures"]:
            for b in m["beats"]:
                want, got = warp(b), tm.rec_at(b)
                if want is not None and got is not None:
                    errs.append(abs(got - want))
        self.assertGreater(len(errs), 0.7 * (MEASURES - 4) * BEATS)
        self.assertLess(float(np.median(errs)), 0.04)
        self.assertLess(float(np.percentile(errs, 90)), 0.1)

    def test_the_left_out_measures_are_a_gap(self):
        gaps = [g for g in self.al["gaps"] if g["kind"] in (RM.SONG_MISSING, RM.DIFFERENT, RM.WEAK)
                and not g["edge"]]
        self.assertTrue(gaps, self.al["gaps"])
        mlen = BEATS * 60.0 / BPM
        g = max(gaps, key=lambda g: g["song"][1] - g["song"][0])
        self.assertLess(abs(g["song"][0] - MISSING[0] * mlen), 1.5)
        self.assertLess(abs(g["song"][1] - MISSING[1] * mlen), 1.5)
        self.assertEqual(g["kind"], RM.SONG_MISSING)
        lead = [g for g in self.al["gaps"] if g["edge"] == "lead"]
        self.assertTrue(lead and lead[0]["kind"] == RM.RECORDING_EXTRA and abs(lead[0]["recording"][1] - INTRO) < 0.3,
                        lead)

    def test_retime_lands_on_the_song(self):
        out = os.path.join(self.tmp, "retimed.wav")
        self.A.main(["retime", "--ref", self.ref_path, "--align", self.align_path, "--audio", self.rec_path,
                     "--out", out, "--latency-cache", os.path.join(self.tmp, "latency.json")])
        with open(out + ".json") as f:
            info = json.load(f)
        self.assertLessEqual(info["fidelity_p95_ms"], 15.0, info["passes"])
        z, sr = sf.read(out)
        self.assertAlmostEqual(len(z) / sr, RM.output_length([RM.Op.from_dict(o) for o in self.al["plan"]]),
                               delta=0.01)
        ver = os.path.join(self.tmp, "verify.json")
        self.A.main(["verify", "--ref", self.ref_path, "--audio", out, "--key", str(KEY), "--out", ver])
        with open(ver) as f:
            v = json.load(f)
        self.assertLess(abs(v["lag"]["median_ms"]), 40.0, v["lag"])
        self.assertGreater(v["lag"]["within_50ms"], 0.8, v["lag"])
        # the measures the recording leaves out are silent and agree with nothing; the others agree
        by = {m["number"]: m for m in v["measures"]}
        self.assertTrue(all((by[n]["chroma"] or 0) < 0.2 for n in range(MISSING[0] + 2, MISSING[1])))
        self.assertGreater(float(np.median([by[n]["chroma"] for n in range(3, 13)])), 0.5)


@unittest.skipUnless(HAVE, "needs numpy, soundfile and librosa (run with the [recordings] python)")
class VocalsCheckTest(unittest.TestCase):
    """The vocals check on a sung line 40 ms later than the score's vocal notes, with a little bleed where it rests."""

    def test_melody_onsets_and_bleed(self):
        sys.path.insert(0, TOOLS)
        import recording_align as A
        tmp = tempfile.mkdtemp(prefix="nw-vocals-test-")
        try:
            rng = np.random.RandomState(3)
            notes, t = [], 0.0
            while t < 40.0:
                if int(t) % 10 < 7:                                  # sings 7 s of every 10
                    notes.append([round(t, 3), 0.45, int(rng.randint(57, 72)), 0.8, "v"])
                t += 0.5
            ref = {"song_end": 42.0, "measures": [], "notes": notes}
            sung = A.synthesize({"song_end": 42.0, "notes": [[n[0] + 0.04, n[1], n[2], n[3], "o"] for n in notes]})
            sung += (0.003 * rng.randn(len(sung))).astype(np.float32)
            with open(os.path.join(tmp, "ref.json"), "w") as f:
                json.dump(ref, f)
            sf.write(os.path.join(tmp, "v.wav"), sung, A.SR)
            out = os.path.join(tmp, "v.json")
            A.main(["vocals", "--ref", os.path.join(tmp, "ref.json"), "--audio", os.path.join(tmp, "v.wav"),
                    "--out", out])
            with open(out) as f:
                v = json.load(f)
            self.assertGreater(v["melody"]["agree_at_best"], 0.8, v["melody"])
            self.assertLess(abs(v["melody"]["best_shift_ms"] - 40), 30, v["melody"])
            self.assertLess(abs(v["onsets"]["median_ms"] - 40), 20, v["onsets"])
            self.assertLess(v["bleed"]["rest_minus_sung_db"], -20, v["bleed"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
