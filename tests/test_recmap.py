"""Time maps between a recording and a song (pianovision.recmap): DTW path -> pieces and gaps, the edit plan."""

import unittest

from pianovision import recmap as RM


def diagonal(i0, j0, n, ratio=1.0):
    """A DTW path n song frames long from (i0, j0), the recording advancing ``ratio`` frames per song frame (steps
    of (1,1), (1,0), (0,1) as a DTW path has)."""
    pts = [(i0, j0)]
    i, j, acc = i0, j0, 0.0
    while i < i0 + n:
        acc += ratio
        di = 1
        dj = int(acc)
        acc -= dj
        if dj == 0:
            pts.append((i + 1, j))
        else:
            pts.append((i + 1, j + 1))
            for k in range(1, dj):
                pts.append((i + 1, j + 1 + k))
        i += di
        j += dj
    return pts


def path_of(*runs):
    out = []
    for r in runs:
        for p in r:
            if not out or p != out[-1]:
                out.append(p)
    return out


class PiecesFromPathTest(unittest.TestCase):
    HOP = 0.1

    def test_one_diagonal_is_one_piece_with_its_tempo(self):
        path = diagonal(0, 50, 300, ratio=1.25)          # 30 s of song from recording 5 s, 25 % slower
        tm = RM.pieces_from_path(path, [0.6] * len(path), self.HOP, self.HOP)
        self.assertEqual(len(tm.pieces), 1)
        p = tm.pieces[0]
        self.assertAlmostEqual(p.song0, 0.0)
        self.assertAlmostEqual(p.rec0, 5.0, places=1)
        self.assertAlmostEqual(p.ratio, 1.25, places=2)
        self.assertAlmostEqual(tm.rec_at(10.0), 5.0 + 12.5, delta=0.15)
        self.assertAlmostEqual(tm.song_at(5.0 + 12.5), 10.0, delta=0.15)
        self.assertIsNone(tm.rec_at(40.0))

    def test_horizontal_run_is_recording_material_the_song_does_not_have(self):
        a = diagonal(0, 0, 100)                           # song 0-10 s = recording 0-10 s
        run = [(100, j) for j in range(100, 200)]         # the recording plays 10 s more (a repeat the score skips)
        b = diagonal(100, 199, 100)                       # song 10-20 s = recording 19.9-29.9 s
        path = path_of(a, run, b)
        tm = RM.pieces_from_path(path, [0.6] * len(path), self.HOP, self.HOP)
        self.assertEqual(len(tm.pieces), 2)
        gaps = tm.gaps(20.0, 30.0)
        self.assertEqual([g.kind for g in gaps], [RM.RECORDING_EXTRA])
        self.assertAlmostEqual(gaps[0].rec_len, 9.9, delta=0.3)

    def test_vertical_run_is_song_material_the_recording_does_not_have(self):
        a = diagonal(0, 0, 100)
        run = [(i, 100) for i in range(100, 180)]         # 8 s of the score the recording leaves out
        b = diagonal(179, 100, 100)
        path = path_of(a, run, b)
        tm = RM.pieces_from_path(path, [0.6] * len(path), self.HOP, self.HOP)
        gaps = tm.gaps(27.9, 20.0)
        self.assertIn(RM.SONG_MISSING, [g.kind for g in gaps])
        g = [g for g in gaps if g.kind == RM.SONG_MISSING][0]
        self.assertAlmostEqual(g.song_len, 7.9, delta=0.3)

    def test_low_similarity_is_not_matched(self):
        path = diagonal(0, 0, 300)
        sim = [0.6] * len(path)
        for k, (i, _j) in enumerate(path):
            if 100 <= i < 160:                           # 6 s that do not match (a solo the score does not have)
                sim[k] = -0.2
        tm = RM.pieces_from_path(path, sim, self.HOP, self.HOP)
        self.assertEqual(len(tm.pieces), 2)
        gaps = tm.gaps(30.0, 30.0)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].kind, RM.WEAK)          # both sides have about as much: stretched through

    def test_short_matches_and_off_tempo_pieces_are_dropped(self):
        a = diagonal(0, 0, 200)
        stray = diagonal(200, 300, 20)                    # 2 s: too short to trust
        path = path_of(a, [(200, j) for j in range(200, 300)], stray)
        tm = RM.pieces_from_path(path, [0.6] * len(path), self.HOP, self.HOP)
        self.assertEqual(len(tm.pieces), 1)

    def test_pieces_close_on_both_sides_are_joined(self):
        p1 = RM.Piece([(0.0, 0.0), (10.0, 10.0)], 0.5)
        p2 = RM.Piece([(10.5, 10.4), (20.0, 20.0)], 0.7)
        joined = RM.join_seams([p1, p2], 1.0)
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0].anchors[-1], (20.0, 20.0))


class AnchorsTest(unittest.TestCase):
    def test_isotonic_pools_violators(self):
        self.assertEqual(RM.isotonic([1, 3, 2, 4]), [1, 2.5, 2.5, 4])

    def test_monotone_keeps_a_minimum_slope(self):
        pts = [(0, 0.0), (1, 1.0), (2, 1.0), (3, 0.9), (4, 4.0)]
        out = RM.monotone(pts, min_slope=0.2)
        for (s0, r0), (s1, r1) in zip(out, out[1:]):
            self.assertGreaterEqual((r1 - r0) / (s1 - s0), 0.2 - 1e-9)

    def test_simplify_drops_collinear_anchors_within_tolerance(self):
        pts = [(t, 2.0 * t) for t in range(11)] + [(11, 22.5), (12, 24.0)]
        out = RM.simplify(pts, 0.01)
        self.assertEqual(out[0], (0, 0.0))
        self.assertIn((11, 22.5), out)
        self.assertLess(len(out), 6)
        loose = RM.simplify(pts, 1.0)
        self.assertEqual(loose, [(0, 0.0), (12, 24.0)])

    def test_split_irregular_cuts_where_the_recording_races(self):
        pts = [(float(t), float(t)) for t in range(0, 11)]                     # 1:1 for 10 s
        pts += [(11.0, 10.1), (12.0, 10.2), (13.0, 10.3)]                       # 3 s of song in 0.3 s of recording
        pts += [(float(t), t - 2.7) for t in range(14, 25)]                     # 1:1 again
        parts = RM.split_irregular(pts)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0][-1], (10.0, 10.0))
        self.assertEqual(parts[1][0], (13.0, 10.3))


class GapTest(unittest.TestCase):
    def test_classify(self):
        self.assertIsNone(RM.classify_gap(0.5, 0.5, 1.0))
        self.assertEqual(RM.classify_gap(0.2, 12.0, 1.0), RM.RECORDING_EXTRA)
        self.assertEqual(RM.classify_gap(8.0, 0.3, 1.0), RM.SONG_MISSING)
        self.assertEqual(RM.classify_gap(8.0, 10.0, 1.2), RM.WEAK)
        self.assertEqual(RM.classify_gap(8.0, 40.0, 1.0), RM.DIFFERENT)

    def test_edges(self):
        tm = RM.TimeMap([RM.Piece([(4.0, 30.0), (60.0, 86.0)])])
        gaps = tm.gaps(70.0, 200.0)
        self.assertEqual([(g.edge, g.kind) for g in gaps], [("lead", RM.DIFFERENT), ("tail", RM.DIFFERENT)])
        tm = RM.TimeMap([RM.Piece([(0.2, 12.0), (60.0, 71.8)])])          # the recording's 12 s intro
        gaps = tm.gaps(60.0, 72.0)
        self.assertEqual([(g.edge, g.kind) for g in gaps], [("lead", RM.RECORDING_EXTRA)])


class EditPlanTest(unittest.TestCase):
    def tm(self):
        return RM.TimeMap([RM.Piece([(0.0, 5.0), (10.0, 17.0), (20.0, 29.0)], 0.6),       # tempo 1.2
                           RM.Piece([(30.0, 60.0), (40.0, 72.0)], 0.6),                     # after a 21 s cut
                           RM.Piece([(55.0, 72.5), (70.0, 90.5)], 0.6)])                    # 15 s of song missing

    def test_plan_tiles_the_song_and_follows_the_map(self):
        tm = self.tm()
        ops = RM.edit_plan(tm, song_end=70.0, rec_end=200.0, tail_s=8.0)
        RM.check_plan(ops)
        self.assertAlmostEqual(RM.output_length(ops), 78.0)
        for t in (0.0, 3.0, 10.0, 19.9, 30.5, 35.0, 56.0, 69.0):
            self.assertAlmostEqual(RM.apply_plan(ops, t), tm.rec_at(t), places=6)
        # the 10 s between 20 and 30: the recording runs on 31 s there (more than the tempo explains): cut + silence
        self.assertIsNone(RM.apply_plan(ops, 25.0))
        self.assertIsNone(RM.apply_plan(ops, 50.0))            # song 40-55 s is not in the recording
        self.assertAlmostEqual(RM.apply_plan(ops, 74.0), 94.5)  # the tail plays on 1:1
        kinds = [o.why for o in ops if o.kind == "silence"]
        self.assertEqual(kinds, [RM.DIFFERENT, RM.SONG_MISSING])
        self.assertEqual(len(RM.cuts(ops)), 4)                  # into and out of both silences

    def test_lead_comes_from_the_recording_before_the_first_piece(self):
        tm = RM.TimeMap([RM.Piece([(3.0, 10.0), (20.0, 27.0)])])
        ops = RM.edit_plan(tm, 20.0, 27.0, tail_s=0.0)
        self.assertEqual((ops[0].song0, ops[0].song1, ops[0].rec0, ops[0].rec1), (0.0, 3.0, 7.0, 10.0))
        tm = RM.TimeMap([RM.Piece([(3.0, 1.0), (20.0, 18.0)])])        # only 1 s of recording before
        ops = RM.edit_plan(tm, 20.0, 18.0, tail_s=0.0)
        self.assertEqual(ops[0].kind, "silence")
        self.assertAlmostEqual(ops[0].song1, 2.0)
        self.assertEqual((ops[1].rec0, ops[1].rec1), (0.0, 1.0))

    def test_recording_extra_between_pieces_is_cut(self):
        tm = RM.TimeMap([RM.Piece([(0.0, 0.0), (10.0, 10.0)]), RM.Piece([(10.4, 30.0), (20.0, 39.6)])])
        ops = RM.edit_plan(tm, 20.0, 40.0, tail_s=0.0)
        RM.check_plan(ops)
        self.assertTrue(all(o.kind == "audio" for o in ops))
        self.assertAlmostEqual(RM.apply_plan(ops, 10.2), 29.8, places=6)  # played from just before the next piece

    def test_check_plan_rejects_holes(self):
        with self.assertRaises(ValueError):
            RM.check_plan([RM.Op("audio", 0.0, 1.0, 0.0, 1.0), RM.Op("audio", 1.5, 2.0, 1.0, 2.0)])

    def test_round_trip(self):
        tm = self.tm()
        self.assertEqual(RM.TimeMap.from_dict(tm.to_dict()).to_dict(), tm.to_dict())
        ops = RM.edit_plan(tm, 70.0, 200.0)
        self.assertEqual([RM.Op.from_dict(o.to_dict()).to_dict() for o in ops], [o.to_dict() for o in ops])


class SummaryTest(unittest.TestCase):
    def measures(self, n, length=2.0):
        return [{"index": k, "number": k + 1, "pass": 1, "start": k * length, "end": (k + 1) * length,
                 "beats": [k * length + b * length / 4 for b in range(4)]} for k in range(n)]

    def test_tempo_summary(self):
        anchors = [(0.0, 10.0)]
        for k in range(1, 21):
            anchors.append((k * 2.0, anchors[-1][1] + (2.0 * 1.2 if k != 10 else 2.0 * 0.4)))
        tm = RM.TimeMap([RM.Piece(anchors)])
        t = RM.tempo_summary(tm, self.measures(20))
        self.assertAlmostEqual(t["median"], 1.2, places=3)
        self.assertEqual([x["measure"] for x in t["irregular"]], ["10"])
        self.assertAlmostEqual(t["irregular"][0]["recording_beats"], 4 * 0.4 / 1.2, places=1)
        self.assertGreater(t["drift_max_s"], 0.3)

    def test_measure_ranges_and_labels(self):
        ms = self.measures(10)
        ms[6] = dict(ms[6], number=3, **{"pass": 2})            # a repeat: m. 3 again
        self.assertEqual(RM.measure_ranges(ms, 3.9, 10.1), ["3:1-5"])
        self.assertEqual(RM.measure_ranges(ms, 12.0, 14.0), ["3:2"])
        self.assertEqual(RM.measure_ranges(ms, 0.0, 0.5), [])


if __name__ == "__main__":
    unittest.main()
