"""A concerto's recording with the piano's solo passages muted (pianovision.solomute)."""

import os
import shutil
import subprocess
import tempfile
import unittest

from pianovision import solomute as SM


def note(start, duration, pitch=60):
    return {"note": pitch, "start": start, "duration": duration}


def doc(piano, orchestra):
    return {"parts": {"original": {"right": piano, "left": []}, "orchestra": {"right": orchestra, "left": []}}}


class SpansTest(unittest.TestCase):
    def test_the_piano_alone_between_orchestra_notes(self):
        d = doc([note(0.5, 0.5), note(3.0, 0.5), note(6.0, 1.0), note(12.5, 0.5)],
                [note(0.0, 2.0), note(1.0, 1.5), note(9.0, 1.0)])
        # the orchestra sounds 0-2.5 and 9-10; the piano alone 2.5-9 (2 notes) and 10-13 (1 note)
        spans = SM.solo_spans(d, min_seconds=2.0)
        self.assertEqual([(s.start, s.end, s.notes) for s in spans], [(2.5, 9.0, 2), (10.0, 13.0, 1)])

    def test_short_stretches_and_silence_are_not_passages(self):
        d = doc([note(2.6, 0.2)], [note(0.0, 2.5), note(3.5, 1.0), note(10.0, 1.0)])
        # 2.5-3.5: the piano alone for 1 s (a fill); 4.5-10: nobody plays
        self.assertEqual(SM.solo_spans(d, min_seconds=2.0), [])
        self.assertEqual(len(SM.solo_spans(d, min_seconds=0.5)), 1)

    def test_a_piano_opening(self):
        d = doc([note(0.0, 4.0)], [note(5.0, 1.0)])
        self.assertEqual([(s.start, s.end) for s in SM.solo_spans(d)], [(0.0, 5.0)])


class GainTest(unittest.TestCase):
    def test_fades_on_the_recordings_clock(self):
        pts = SM.gain_breakpoints([SM.Span(10.0, 20.0, 5)], offset=1.5, fade_out=0.4, fade_in=0.1, lead=0.05)
        self.assertEqual(len(pts), 1)
        a, s, c, full = pts[0]
        self.assertAlmostEqual(a, 11.5)
        self.assertAlmostEqual(s, 11.9)
        self.assertAlmostEqual(full, 21.45)
        self.assertAlmostEqual(c, 21.35)
        self.assertEqual(SM.gain_at(pts, 11.0), 1.0)
        self.assertAlmostEqual(SM.gain_at(pts, 11.7), 0.5)
        self.assertEqual(SM.gain_at(pts, 15.0), 0.0)
        self.assertAlmostEqual(SM.gain_at(pts, 21.4), 0.5)
        self.assertEqual(SM.gain_at(pts, 22.0), 1.0)

    def test_a_short_passage_shares_its_fades(self):
        (a, s, c, full), = SM.gain_breakpoints([SM.Span(0.0, 0.5, 1)], 0.0, fade_out=0.35, fade_in=0.12, lead=0.05)
        self.assertLessEqual(s, c)

    def test_the_expression_is_the_gain(self):
        pts = SM.gain_breakpoints([SM.Span(1.0, 3.0, 1), SM.Span(5.0, 9.0, 1)], 0.25)
        e = SM.gain_expr(pts)
        env = {"clip": lambda x, lo, hi: min(hi, max(lo, x)), "max": max}
        for t in (0.0, 1.3, 1.5, 2.0, 3.15, 3.2, 4.0, 5.4, 7.0, 9.2, 10.0):
            self.assertAlmostEqual(eval(e, env, {"t": t}), SM.gain_at(pts, t), places=6)
        self.assertEqual(SM.gain_expr([]), "1")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg")
class WriteTest(unittest.TestCase):
    def test_the_passage_is_silent_and_the_rest_plays(self):
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "rec.wav")
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:sample_rate=44100:duration=8", "-ac", "2", src], check=True)
            d = doc([note(2.0, 0.5), note(3.0, 0.5)], [note(0.0, 1.5), note(4.0, 3.5)])
            mp3 = os.path.join(tmp, "out.mp3")
            rep = SM.write(src, mp3, d, offset=0.5, bitrate=128, log=lambda *_a: None)
            self.assertEqual([p["recording"] for p in rep["passages"]], [[2.0, 4.5]])
            self.assertLess(rep["passages"][0]["inside_max_db"], -80)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "out.solo-mute.json")))
            from pianovision.accompaniment import window_levels, block_seconds
            w = block_seconds(0.1)
            rms, _p = window_levels([mp3], w)
            self.assertGreater(rms[int(1.0 / w)], -30)            # before: the tone (lavfi sine: -24.5 dB RMS)
            self.assertLess(rms[int(3.2 / w)], -80)               # inside: silence
            self.assertGreater(rms[int(5.5 / w)], -30)            # after: the tone again
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
