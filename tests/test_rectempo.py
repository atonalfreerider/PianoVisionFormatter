"""The recording leads (pianovision.rectempo): the recording's measure times as the score's tempo map, the error of one
tempo per written measure through repeats, the offset, and repeats written out with each pass's verse.  Synthetic
scores and times only.

The lattice test of tools/recording_align.py needs numpy (run it with the [recordings] python); it is skipped under
the formatter's own Python."""

import os
import shutil
import tempfile
import unittest
import zipfile

from pianovision import rectempo as RT
from pianovision.mscx import read_score
from pianovision.scorefix import play_order, structure

from tests.scoregen import E, chord, make_piano_score, note, rest

Q = 480
W = 4 * Q


def lyric(text, no=None, syllabic=None):
    kids = []
    if no:
        kids.append(E("no", text=str(no)))
    if syllabic:
        kids.append(E("syllabic", text=syllabic))
    kids.append(E("text", text=text))
    return E("Lyrics", *kids)


def target(index, start, length, rel=None, number=None, passno=1, source="detected"):
    return RT.Target(index, number or index + 1, passno, start, start + length, rel or {0: 0.0}, 1.0, source)


def steady(rd, starts):
    """Targets for every measure as played: measure j from starts[j] to starts[j + 1], its positions evenly
    spread (a steady tempo inside)."""
    out = []
    for j, (k, pts) in enumerate(RT.played_positions(rd)):
        length = starts[j + 1] - starts[j]
        out.append(target(j, starts[j], length, {p: length * p / pts[-1] for p in pts[:-1]}, k + 1))
    return out


class SharedTempoTest(unittest.TestCase):
    def test_passes_alike_make_no_error(self):
        e = RT.shared_tempo_errors([0, 1, 2, 1, 2, 3], [2.0] * 6)
        self.assertEqual(e.max_abs, 0.0)
        self.assertEqual(e.repeated, [False, True, True, True, True, False])

    def test_a_slower_second_pass_drifts_and_comes_back(self):
        # measures 1-2 twice: 2.0 s each the first time, 2.2 s the second; one tempo each: their mean, 2.1 s
        e = RT.shared_tempo_errors([0, 1, 2, 1, 2, 3], [2.0, 2.0, 2.0, 2.2, 2.2, 2.0])
        self.assertEqual([round(x, 6) for x in e.start], [0.0, 0.0, 0.1, 0.2, 0.1, 0.0])
        self.assertAlmostEqual(e.max_abs, 0.2)

    def test_a_measure_played_once_takes_up_the_error(self):
        # measure 0 twice (2.0 s, then 2.4 s: 2.2 s shared), measure 1 between the passes, measure 2 after
        written, lengths = [0, 1, 0, 2], [2.0, 2.0, 2.4, 2.0]
        self.assertEqual([round(x, 6) for x in RT.shared_tempo_errors(written, lengths).start], [0.0, 0.2, 0.0, -0.2])
        self.assertEqual([round(x, 6) for x in RT.shared_tempo_errors(written, lengths, absorb=False).start],
                         [0.0, 0.2, 0.2, 0.0])

    def test_positions_inside_a_shared_measure(self):
        # the same length, the second beat 0.5 s in on one pass and 0.7 s on the other: the mean misses both by 0.1
        e = RT.shared_tempo_errors([0, 0], [2.0, 2.0], [{0: 0.0, Q: 0.5}, {0: 0.0, Q: 0.7}])
        self.assertAlmostEqual(e.max_abs, 0.1)
        self.assertAlmostEqual(abs(e.inside[0]), 0.1)


class OffsetTest(unittest.TestCase):
    def test_the_offset_is_where_the_recording_plays_the_first_measure(self):
        self.assertAlmostEqual(RT.offset_of([target(0, 11.09, 2.2), target(1, 13.29, 2.2)]), 11.09)
        # the score starts before the recording does (a measure the recording does not have): the audio starts later
        self.assertAlmostEqual(RT.offset_of([target(0, -0.97, 1.9, source="outside"), target(1, 0.93, 1.9)]), -0.97)
        self.assertEqual(RT.offset_of([]), 0.0)


class NoteTableTest(unittest.TestCase):
    def test_notes_after_a_pause_stay_in_their_measures(self):
        # a caesura with a pause: the render gives it ticks of its own; the table's measures are the score's
        tmp = tempfile.mkdtemp()
        try:
            breath = E("Breath", E("symbol", text="caesura"), E("pause", text="0.5"))
            rh = [[[chord("half", 60), breath, chord("half", 62)]], [[chord("whole", 64)]], [[chord("whole", 65)]]]
            lh = [[[rest("whole")]] for _ in range(3)]
            path = make_piano_score(os.path.join(tmp, "p.mscz"), rh, lh, title="Pause")
            rd = RT.render(path)
            table = RT._note_table(rd)
            got = {j: [(rt, pitch) for rt, _d, pitch, _tr, _on, _off in v] for j, v in table.items()}
            self.assertEqual(got, {0: [(0, 60), (960, 62)], 1: [(0, 64)], 2: [(0, 65)]})
            # the last two fields are MIDI ticks (the pause's ticks in them)
            on = {pitch: on for v in table.values() for _rt, _d, pitch, _tr, on, _off in v}
            self.assertGreater(on[62] - on[60], 960)
            self.assertEqual(on[64] - on[62], 960)
        finally:
            shutil.rmtree(tmp)


class AnchorsTest(unittest.TestCase):
    def test_measure_times_you_give(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "timing.txt")
            with open(path, "w", newline="") as f:
                f.write("# timed by ear\r\n001|00:00.700\r\n019|00:56.149\r\n\r\n387:2|14:40.5\r\n400, 900.25\r\n")
            self.assertEqual(RT.read_anchors(path), [("1", 0.7), ("19", 56.149), ("387:2", 880.5), ("400", 900.25)])
            with open(path, "w") as f:
                f.write("19 at 0:56\n")
            with self.assertRaises(RT.RecTempoError):
                RT.read_anchors(path)
        finally:
            shutil.rmtree(tmp)

    def test_anchors_name_measures_as_played(self):
        ref = {"measures": [{"number": 1, "start": 0.0}, {"number": 2, "start": 2.0}, {"number": 2, "pass": 2,
                                                                                       "start": 4.0},
                            {"number": 3, "start": 6.0}]}
        pins = RT.resolve_anchors(ref, [("1", 0.5), ("2:2", 5.0), ("3", 7.5)])
        self.assertEqual([(q["index"], q["song"], q["recording"], q["label"]) for q in pins],
                         [(0, 0.0, 0.5, "m. 1"), (2, 4.0, 5.0, "m. 2:2"), (3, 6.0, 7.5, "m. 3")])
        for bad in (("2", 3.0), ("2:3", 3.0), ("9", 1.0)):     # a repeated measure needs its pass; no such measure
            with self.assertRaises(RT.RecTempoError):
                RT.resolve_anchors(ref, [bad])


class Scores(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def path(self, name):
        return os.path.join(self.tmp, name)


class PlanTest(Scores):
    def score(self, n=4):
        rh = [[[chord("quarter", 60 + i), chord("quarter", 62 + i), chord("quarter", 64 + i),
                chord("quarter", 65 + i)]] for i in range(n)]
        lh = [[[chord("whole", 48)]] for _ in range(n)]
        return make_piano_score(self.path("plan.mscz"), rh, lh)

    def test_every_measure_lasts_what_the_recording_plays(self):
        path = self.score()
        rd = RT.render(path)
        starts = [3.0, 5.0, 6.8, 9.2, 11.2]                  # the recording's measures: 2.0, 1.8, 2.4, 2.0 s
        tg = steady(rd, starts)
        p = RT.plan(rd.score, RT.played_positions(rd), tg)
        self.assertEqual(len(p.marks), 4)                   # one per measure (each a steady tempo)
        p.offset = RT.offset_of(tg)
        self.assertEqual(p.offset, 3.0)
        with open(path, "rb") as f:
            base = f.read()
        for rdx in (RT.render(path, plan=p), RT.render(RT.write_copy(base, p, self.path("copy.mscz")))):
            got = [rdx.seconds(m.tick) for m in rdx.timeline.measures] + \
                [rdx.seconds(rdx.timeline.measures[-1].end_tick)]
            for g, s in zip(got, starts):
                self.assertAlmostEqual(g, s - p.offset, delta=0.001)

    def test_timed_again_the_score_keeps_one_set_of_marks(self):
        path = self.score(2)
        rd = RT.render(path)
        p = RT.plan(rd.score, RT.played_positions(rd), steady(rd, [0.0, 2.0, 3.6]))
        with open(path, "rb") as f:
            once = RT.write_copy(f.read(), p, self.path("once.mscz"))
        with open(once, "rb") as f:
            base = RT.strip_own_mscz(f.read())
        with open(self.path("base.mscz"), "wb") as f:
            f.write(base)
        rd1 = RT.render(self.path("base.mscz"))
        p2 = RT.plan(rd1.score, RT.played_positions(rd1), steady(rd1, [0.0, 2.2, 4.0]))
        twice = RT.write_copy(base, p2, self.path("twice.mscz"))
        with zipfile.ZipFile(twice) as z:
            self.assertEqual(z.read("score.mscx").decode().count(RT.MARK_LABEL), len(p2.marks))
        rd2 = RT.render(twice)
        self.assertAlmostEqual(rd2.seconds(rd2.timeline.measures[1].tick), 2.2, delta=0.001)
        rd3 = RT.render(once, plan=p2)                       # the model the same way
        self.assertAlmostEqual(rd3.seconds(rd3.timeline.measures[1].tick), 2.2, delta=0.001)

    def test_a_late_beat_gets_a_mark_of_its_own(self):
        path = self.score(2)
        rd = RT.render(path)
        tg = steady(rd, [0.0, 2.0, 4.0])
        tg[0].rel[2 * Q] = 1.15                             # the third beat 150 ms late, the fourth on time
        p = RT.plan(rd.score, RT.played_positions(rd), tg, tolerance=0.02)
        self.assertGreater(len([m for m in p.marks if m.measure == 0]), 1)
        rdx = RT.render(path, plan=p)
        tl = rdx.timeline
        third = rdx.seconds(tl.tick(tl.measures[0].utick + 2 * Q))
        self.assertAlmostEqual(third, 1.15, delta=0.021)
        self.assertAlmostEqual(rdx.seconds(tl.measures[1].tick), 2.0, delta=0.001)

    def test_a_repeated_measure_gets_its_passes_mean(self):
        rh = [[[chord("whole", 60 + i)]] for i in range(3)]
        lh = [[[chord("whole", 48)]] for _ in range(3)]
        path = make_piano_score(self.path("rep.mscz"), rh, lh, repeat=(1, 1))
        rd = RT.render(path)
        self.assertEqual([m.number for m in rd.timeline.measures], [1, 2, 2, 3])
        tg = steady(rd, [0.0, 2.0, 4.0, 6.4, 8.4])          # measure 2: 2.0 s, then 2.4 s
        p = RT.plan(rd.score, RT.played_positions(rd), tg)
        self.assertEqual([round(x, 3) for x in p.lengths], [2.0, 2.2, 2.2, 2.0])

    def test_a_stray_time_cannot_stop_the_clock(self):
        path = self.score(1)
        rd = RT.render(path)
        tg = steady(rd, [0.0, 2.0])
        tg[0].rel[2 * Q] = 2.5                              # past the measure's end
        p = RT.plan(rd.score, RT.played_positions(rd), tg)
        self.assertAlmostEqual(p.lengths[0], 2.0, delta=0.001)


class UnrollTest(Scores):
    def score(self):
        """Four measures, 2-3 repeated: verse 1 and 2 stacked on them, a tie from measure 3 into measure 4."""
        def sung(p, one, two):
            return chord("half", p, extra=(lyric(one), lyric(two, no=1)))
        rh = [
            [[chord("whole", 60)]],
            [[sung(62, "la", "lo"), sung(64, "li", "lu")]],
            [[sung(65, "ma", "mo"), chord("half", note(67, tie_next=(1, "1/2")))]],
            [[chord("half", note(67, tie_prev=(-1, "-1/2"))), chord("half", 60, extra=(lyric("ne"),))]],
        ]
        lh = [[[chord("whole", 48)]] for _ in range(4)]
        return make_piano_score(self.path("unroll.mscz"), rh, lh, repeat=(1, 2))

    def test_written_out_with_each_pass_singing_its_verse(self):
        path = self.score()
        sc = read_score(path)
        order = play_order(sc)
        self.assertEqual(order, [1, 2, 3, 2, 3, 4])
        with open(path, "rb") as f:
            new, rep = RT.unroll_mscz(f.read(), order)
        out = self.path("unrolled.mscz")
        with open(out, "wb") as f:
            f.write(new)
        st = structure(read_score(out))
        self.assertEqual(st["play_order"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(rep.passes, [1, 1, 1, 2, 2, 1])
        self.assertEqual(rep.removed, {"start repeats": 1, "end repeats": 1})
        self.assertEqual(rep.spanners_dropped, {"ties": 1})      # the first pass of measure 3 goes on to measure 2
        # the same syllables on the same notes and passes: every copy sings its pass's verse as verse 1
        a, b = RT.render(path), RT.render(out)
        la, lb = RT._lyric_table(a), RT._lyric_table(b)
        self.assertEqual(la, lb)
        self.assertEqual(sorted(v[0] for k, v in lb.items() if k[0] == 3), ["lo", "lu"])
        with zipfile.ZipFile(out) as z:
            mscx = z.read("score.mscx").decode()
        self.assertNotIn("<no>", mscx)
        self.assertNotIn("Repeat", mscx)
        # the notes: the same but for the tie the first pass never played into the second measure
        na, nb = RT._note_table(a), RT._note_table(b)
        self.assertEqual(sum(map(len, na.values())), sum(map(len, nb.values())))
        self.assertEqual({j: [x[:4] for x in v] for j, v in na.items() if j != 2},
                         {j: [x[:4] for x in v] for j, v in nb.items() if j != 2})
        self.assertEqual(sorted((x[0], x[2]) for x in na[2]), sorted((x[0], x[2]) for x in nb[2]))

    def test_ids_go_from_the_copies_and_signatures_are_restated(self):
        # measure 1 in 3/4, measure 2 (repeated, after measure 3 in 4/4 the second time) needs its 3/4 again
        rh = [[[chord("half", 60), chord("quarter", 62)]], [[chord("half", 64), chord("quarter", 65)]],
              [[chord("whole", 67)]]]
        lh = [[[rest("half", dots=1)]], [[rest("half", dots=1)]], [[rest("whole")]]]
        path = make_piano_score(self.path("sig.mscz"), rh, lh)
        with zipfile.ZipFile(path) as z:
            mscx = z.read("score.mscx").decode()
        mscx = mscx.replace("<sigN>4</sigN>", "<sigN>3</sigN>", 2)
        # measure 3 in 4/4 (both staves), measure 1 repeated after it: written order 1, 2, 3, then 1 again
        parts = mscx.split("<Measure>")
        for i in (3, 6):
            parts[i] = parts[i].replace("<voice>", "<voice>\n<TimeSig><sigN>4</sigN><sigD>4</sigD></TimeSig>", 1)
        mscx = "<Measure>".join(parts).replace("<Measure>", "<Measure><eid>X</eid>")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml", '<?xml version="1.0" encoding="UTF-8"?><container><rootfiles>'
                                                 '<rootfile full-path="score.mscx"/></rootfiles></container>')
            z.writestr("score.mscx", mscx)
        with open(path, "rb") as f:
            new, rep = RT.unroll_mscz(f.read(), [1, 2, 3, 2])
        self.assertEqual(rep.context_added, {"time signatures": 2})
        out = self.path("sig-unrolled.mscz")
        with open(out, "wb") as f:
            f.write(new)
        sc = read_score(out)
        self.assertEqual([m.timesig_nd for m in sc.measures], [(3, 4), (3, 4), (4, 4), (3, 4)])
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.read("score.mscx").decode().count("<eid>"), 6)     # 3 measures x 2 staves


class ApplyTest(Scores):
    def test_the_verified_copy_goes_over_the_score_after_a_backup(self):
        score, copy, work = self.path("song.mscz"), self.path("work/song.mscz"), self.path("work")
        os.makedirs(work)
        with open(score, "wb") as f:
            f.write(b"the score")
        os.chmod(score, 0o600)
        with open(copy, "wb") as f:
            f.write(b"the copy")
        st = {"score": score, "score_sha256": RT.sha256_file(score), "copy": copy, "copy_sha256": RT.sha256_file(copy),
              "ok": True, "checks": [{"name": "timing", "ok": True}]}
        RT._write_json(os.path.join(work, RT.STATE_NAME), st)
        bak = RT.apply_copy(score, work, self.path("backup"), log=lambda m: None)
        with open(score, "rb") as f:
            self.assertEqual(f.read(), b"the copy")
        with open(bak, "rb") as f:
            self.assertEqual(f.read(), b"the score")
        self.assertEqual(os.stat(score).st_mode & 0o777, 0o600)
        # again: the score is not the one the copy was made from any more (and the backup exists)
        with self.assertRaises(RT.RecTempoError):
            RT.apply_copy(score, work, self.path("backup"), log=lambda m: None)

    def test_a_copy_that_failed_is_not_applied(self):
        score, work = self.path("song.mscz"), self.path("work")
        os.makedirs(work)
        with open(score, "wb") as f:
            f.write(b"the score")
        RT._write_json(os.path.join(work, RT.STATE_NAME), {"score": score, "ok": False, "checks": [
            {"name": "notes", "ok": False}]})
        with self.assertRaises(RT.RecTempoError):
            RT.apply_copy(score, work, self.path("backup"), log=lambda m: None)
        self.assertFalse(os.path.exists(self.path("backup")))


try:
    import numpy as np                   # noqa: F401
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "recording_align", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools",
                                        "recording_align.py"))
    RA = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(RA)
    HAVE_NUMPY = True
except ImportError:                      # the formatter's own Python: stdlib only
    HAVE_NUMPY = False


@unittest.skipUnless(HAVE_NUMPY, "needs numpy (run with the [recordings] python)")
class LatticeTest(unittest.TestCase):
    def test_detections_on_the_off_beats_stay_there(self):
        # a transcription an eighth off the recording's pulse: every detection half a beat after a score beat
        Bs = np.arange(20) + 0.5 + 0.05 * np.sin(np.arange(20))
        spacing, phases, _clear = RA.lattice_of(Bs, compound=False)
        self.assertEqual(spacing, 1.0)
        self.assertTrue(np.allclose(phases, 0.5))

    def test_a_steady_run_settles_what_the_coarse_map_leaves_in_doubt(self):
        # beats every 0.5 s; the coarse map puts the middle ones 0.4 beat early (closer to the wrong beat)
        d = np.arange(16) * 0.5
        Bs = np.arange(16, dtype=float)
        Bs[6:10] -= 0.4
        q = RA.assign_lattice(d, Bs, 1.0, np.zeros(16), np.full(16, 0.5))
        self.assertEqual(q, [float(k) for k in range(16)])

    def test_an_extra_detection_is_left_out(self):
        d = np.array([0.0, 0.5, 1.0, 1.24, 1.5, 2.0, 2.5])
        Bs = np.array([0.0, 1.0, 2.0, 2.48, 3.0, 4.0, 5.0])
        q = RA.assign_lattice(d, Bs, 1.0, np.zeros(len(d)), np.full(len(d), 0.5))
        self.assertIsNone(q[3])
        self.assertEqual([x for x in q if x is not None], [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])


if __name__ == "__main__":
    unittest.main()
