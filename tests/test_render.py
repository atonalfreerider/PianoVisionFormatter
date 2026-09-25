"""The renderer against MIDI that MuseScore 4.6.0 itself exported for the same score."""

import os
import random
import unittest

from pianovision.convert import convert_midi, convert_mscz
from pianovision.cxxsort import std_sort
from pianovision.mscx import read_score
from pianovision.render import Compat, render_score
from pianovision.smf import read_midi

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def events(mf):
    out = []
    for tr in mf.tracks:
        t, evs = 0, []
        for m in tr:
            t += m.time
            evs.append((t, m.type, tuple(sorted((k, v) for k, v in vars(m).items() if k not in ("time", "type")))))
        out.append(evs)
    return out


class GoldenMidi(unittest.TestCase):
    def test_chord_symbols_score_matches_musescore_export(self):
        mscz = os.path.join(FIX, "chord_symbols.mscz")
        got = render_score(read_score(mscz), Compat())
        ref = read_midi(os.path.join(FIX, "chord_symbols.mid"))
        self.assertEqual(len(got.tracks), len(ref.tracks))
        for a, b in zip(events(got), events(ref)):
            self.assertEqual(a, b)

    def test_json_from_render_equals_json_from_musescore_midi(self):
        mscz = os.path.join(FIX, "chord_symbols.mscz")
        direct = convert_mscz(mscz)
        legacy = convert_midi(os.path.join(FIX, "chord_symbols.mid"), mscz)
        self.assertEqual(direct.data, legacy.data)
        self.assertEqual(direct.name, "fixt_chord_symbols.json")


class StdSort(unittest.TestCase):
    def test_orders_like_sorted_for_distinct_keys(self):
        rng = random.Random(7)
        for n in (0, 1, 5, 16, 17, 100, 1000):
            a = [rng.random() for _ in range(n)]
            b = list(a)
            std_sort(b, lambda x, y: x < y)
            self.assertEqual(b, sorted(a))

    def test_is_not_stable_like_libstdcxx(self):
        # 20 equal keys: introsort's partitioning reorders them (MuseScore relies on this order).
        a = [(0, i) for i in range(20)]
        std_sort(a, lambda x, y: x[0] < y[0])
        self.assertEqual(sorted(a), [(0, i) for i in range(20)])
        self.assertNotEqual(a, [(0, i) for i in range(20)])


if __name__ == "__main__":
    unittest.main()
