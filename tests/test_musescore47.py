"""Scores saved by MuseScore 4.7 (format 4.70) against MuseScore 4.7.5's own MIDI export.

``resave_46.mscz`` is a small score saved by MuseScore 4.6.3 (title and composer on two lines, a
tempo of 70 bpm with a fermata, a crescendo hairpin written backwards: its end before its start);
``resave_47.mscz`` is that file saved again by MuseScore 4.7.5.  ``resave_46.mid`` and
``resave_47.mid`` are the two versions' MIDI exports of their file.  The two exports differ where 4.7
reads or plays the score differently: the fermata's tempo is rounded to 6 decimals, and the hairpin
runs forwards (4.6 kept it with a negative length, so it did not sound).
"""

import os
import shutil
import tempfile
import unittest
import zipfile

from pianovision.convert import convert_mscz
from pianovision.mscx import file_version, load_mscx_bytes, read_score
from pianovision.render import Compat, render_score, round_tempo
from pianovision.smf import read_midi

from tests.scoregen import E, chord, make_piano_score, make_score
from tests.test_render import FIX, events


def tempos(mf):
    t, out = 0, []
    for m in mf.tracks[0]:
        t += m.time
        if m.type == "set_tempo":
            out.append((t, m.tempo))
    return out


def velocities(mf):
    return [m.velocity for tr in mf.tracks for m in tr if m.type == "note_on" and m.velocity]


class GoldenMidi47(unittest.TestCase):
    def render(self, name, **knobs):
        return render_score(read_score(os.path.join(FIX, name)), Compat(**knobs))

    def assert_same_midi(self, got, ref):
        self.assertEqual(len(got.tracks), len(ref.tracks))
        for a, b in zip(events(got), events(ref)):
            self.assertEqual(a, b)

    def test_47_file_renders_like_the_musescore_47_export(self):
        self.assert_same_midi(self.render("resave_47.mscz"), read_midi(os.path.join(FIX, "resave_47.mid")))

    def test_46_file_still_renders_like_the_musescore_46_export(self):
        self.assert_same_midi(self.render("resave_46.mscz"), read_midi(os.path.join(FIX, "resave_46.mid")))

    def test_the_two_exports_differ_in_the_fermata_tempo_and_the_hairpin(self):
        m46, m47 = (read_midi(os.path.join(FIX, f)) for f in ("resave_46.mid", "resave_47.mid"))
        self.assertEqual([t for t in tempos(m46) if t not in tempos(m47)], [(3360, 1714285)])
        self.assertEqual([t for t in tempos(m47) if t not in tempos(m46)], [(3360, 1714284)])
        self.assertNotEqual(velocities(m46), velocities(m47))

    def test_engine_46_gives_the_4_6_tempo_map_for_a_4_7_file(self):
        old = self.render("resave_47.mscz", engine="4.6")
        self.assertEqual(tempos(old), tempos(self.render("resave_46.mscz")))
        # the hairpin is saved forwards now: no knob plays it the 4.6 way again
        self.assertEqual(velocities(old), velocities(self.render("resave_47.mscz")))

    def test_line_breaks_in_titles_read_as_before(self):
        self.assertIn(b"Resave Test<br/>Second Line", zipfile.ZipFile(os.path.join(FIX, "resave_47.mscz"))
                      .read("s47.mscx"))
        new, old = (convert_mscz(os.path.join(FIX, f)) for f in ("resave_47.mscz", "resave_46.mscz"))
        self.assertEqual((new.title, new.artist, new.name), (old.title, old.artist, old.name))
        self.assertEqual(new.title, "Resave Test Second Line")


class Reading47(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def path(self, name):
        return os.path.join(self.tmp, name)

    @staticmethod
    def set_version(path, version):
        with zipfile.ZipFile(path) as z:
            files = {n: z.read(n) for n in z.namelist()}
        files["score.mscx"] = files["score.mscx"].replace(b'version="4.60"', f'version="{version}"'.encode())
        with zipfile.ZipFile(path, "w") as z:
            for n, d in files.items():
                z.writestr(n, d)
        return path

    def test_file_version(self):
        p = make_score(self.path("a.mscz"), version="4.70")
        self.assertEqual(file_version(load_mscx_bytes(p)), 470)
        self.assertEqual(read_score(p).msc_version, 470)

    def test_br_is_a_line_break_in_4_7_files(self):
        p = make_score(self.path("a.mscz"), title="First Line\nSecond Line", version="4.70")
        self.assertIn(b"First Line\nSecond Line", load_mscx_bytes(p))
        self.assertEqual(convert_mscz(p).title, "First Line Second Line")

    def test_a_backwards_spanner_is_turned_round_in_4_7_files_only(self):
        hairpin_end = E("Spanner", E("prev", E("location", E("fractions", text="1/2"))), type="HairPin")
        hairpin_start = E("Spanner", E("HairPin", E("subtype", text="0")),
                          E("next", E("location", E("fractions", text="-1/2"))), type="HairPin")
        rh = [[[hairpin_end, chord("half", 60), hairpin_start, chord("half", 62)]]]
        lh = [[[chord("whole", 48)]]]
        p = make_piano_score(self.path("a.mscz"), rh, lh)
        sp = [s for s in read_score(p).spanners if s.tag == "HairPin"][0]
        self.assertGreater(sp.tick, sp.tick2)                   # 4.6: a negative length, as written
        sp = [s for s in read_score(self.set_version(p, "4.70")).spanners if s.tag == "HairPin"][0]
        self.assertLess(sp.tick, sp.tick2)                      # 4.7: Spanner::setTick2 swaps the ends

    def test_round_tempo(self):
        self.assertEqual(round_tempo(1.166667 / 2.0), 0.583334)
        self.assertEqual(round_tempo(2.0), 2.0)

    def test_va_dyn_type_reads_all_in_staff_as_the_old_dyn_type(self):
        """A 4.3 score's dynType 0 (staff range) is saved by MuseScore 4.4+ as voiceAssignment allInStaff;
        MuseScore 4.5 honoured the first and ignored the second."""
        def score(name, version, tag, value):
            rh = [[[chord("whole", 72)]]]
            lh = [[[E("Dynamic", E("subtype", text="ff"), E("velocity", text="112"), E(tag, text=value)),
                    chord("whole", 48)]]]
            return self.set_version(make_piano_score(self.path(name), rh, lh), version)

        def rh_velocity(path, **knobs):
            mf = render_score(read_score(path), Compat(dynamics="4.5", **knobs))
            return [m.velocity for tr in mf.tracks for m in tr
                    if m.type == "note_on" and m.note == 72 and m.velocity][0]

        old = score("old.mscz", "4.30", "dynType", "0")
        new = score("new.mscz", "4.70", "voiceAssignment", "allInStaff")
        self.assertEqual(rh_velocity(old), 80)                  # the left hand's ff stays on its staff
        self.assertEqual(rh_velocity(new), 112)                 # 4.5 ignored voice assignments: whole part
        self.assertEqual(rh_velocity(new, va_dyn_type=True), 80)


if __name__ == "__main__":
    unittest.main()
