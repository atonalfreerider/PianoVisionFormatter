"""Where a recording's singer sings, on the score's clock (pianovision.vocalspans)."""

import unittest

from pianovision import vocalspans as VS


class SpansTest(unittest.TestCase):
    def test_singing_blocks_join_and_short_ones_go(self):
        W = 0.05
        quiet, loud = -60.0, -10.0
        rv = [quiet] * 10 + [loud] * 20 + [quiet] * 4 + [loud] * 10 + [quiet] * 20 + [loud] * 3 + [quiet] * 40
        rm = [-8.0] * len(rv)
        # 1.5 s sung, a 0.2 s breath (joined), 0.5 s sung; then 0.15 s (too short)
        self.assertEqual(VS.spans_from_levels(rv, rm, W, merge_gap=0.3, min_seconds=0.3), [(0.5, 2.2)])
        self.assertEqual(VS.spans_from_levels(rv, rm, W, merge_gap=0.1, min_seconds=0.3), [(0.5, 1.5), (1.7, 2.2)])

    def test_a_stem_far_under_the_mix_is_bleed(self):
        rv = [-30.0] * 40 + [-60.0] * 40
        rm = [-5.0] * 80                                  # 25 dB under the mix: not singing
        self.assertEqual(VS.spans_from_levels(rv, rm, 0.05, below_mix_db=18.0), [])

    def test_seconds_to_score_ticks(self):
        seconds = lambda tick: tick / 960.0              # 120 bpm, 480 ticks a quarter
        utick = lambda tick: tick
        spans = VS.to_uticks([(1.25, 2.0), (2.1, 3.0)], offset=0.25, seconds=seconds, utick=utick,
                             last_tick=100 * 480, pad_before=0.25, pad_after=0.0)
        # (1.25 - 0.25 - 0.25) s = 720 ticks .. 1.75 s; the second widened back into the first: joined
        self.assertEqual(spans, [(720, 2640)])
        self.assertEqual(VS.tick_at(seconds, -1.0, 4800), 0)
        self.assertEqual(VS.tick_at(seconds, 99.0, 4800), 4800)

    def test_measure_ranges(self):
        starts = [0, 1920, 3840, 5760, 7680]
        labels = ["1", "2", "3", "4", "5"]
        self.assertEqual(VS.measure_ranges([(100, 2000), (5800, 7000)], starts, labels), "m. 1-2, 4")
        self.assertEqual(VS.measure_ranges([], starts, labels), "none")


if __name__ == "__main__":
    unittest.main()
