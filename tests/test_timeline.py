"""Songs on a piano solo's clock (pianovision.timeline): the plan, the edit of the orchestral score (the formatter's
model and MuseScore's copy alike), orchestra_from, the library's partner options, the companion audio's pieces."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from pianovision import accompaniment as AC
from pianovision import arrangement as A
from pianovision import buildopts
from pianovision import timeline as T
from pianovision.arrangement import Timeline, UMeasure
from pianovision.convert import convert_mscz
from pianovision.library import Config, Library, scan_library
from pianovision.pvjson import TempoIndex, extract_tempo_events, staff_roles

from tests.scoregen import E, chord, make_piano_score, rest

Q = 480
W = 4 * Q
HARMONY = [(48, [60, 64, 67]), (45, [57, 60, 64]), (41, [57, 60, 65]), (43, [59, 62, 67]), (40, [56, 59, 64]),
           (38, [57, 62, 65]), (46, [58, 62, 65]), (39, [55, 58, 63]), (44, [60, 63, 68]), (49, [61, 65, 68])]


def tempo(bpm):
    return E("Tempo", E("tempo", text=f"{bpm / 60:.6f}"), E("followText", text="1"), E("text", text=f"= {bpm}"))


def fermata():
    return E("Fermata", E("subtype", text="fermataAbove"))


def quarters(tones):
    return [chord("quarter", *tones) for _ in range(4)]


def song_notes(data, hand="right"):
    """(start, ticksStart, pitch) of a PianoVision JSON's notes."""
    doc = json.loads(data)
    return sorted((round(n["start"], 4), n["ticksStart"], n["note"]) for m in doc["tracksV2"][hand]
                  for n in m["notes"])


def onsets(midi, tracks=None):
    """(seconds, tick, pitch) of the note-ons of a render."""
    seconds = TempoIndex(extract_tempo_events(midi), midi.ticks_per_beat)
    out = []
    for i, tr in enumerate(midi.tracks):
        if tracks is not None and i not in tracks:
            continue
        t = 0
        for msg in tr:
            t += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                out.append((round(seconds(t), 4), t, msg.note))
    return sorted(out)


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scores = os.path.join(self.tmp, "Scores")
        os.makedirs(os.path.join(self.scores, "Test"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def orchestral(self, extra_rh=None, n=10):
        """Ten measures at 120 bpm: the piano in quarter notes, a violin holding the top note an octave up; measure 4
        has a fermata of its own, measure 9 a breath pause of 1 s."""
        rh, lh, vln = [], [], []
        for i in range(n):
            bass, tones = HARMONY[i]
            notes = quarters(tones)
            if i == 3:
                notes = notes[:3] + [fermata(), notes[3]]
            if i == 8:
                notes = notes[:2] + [E("Breath", E("symbol", text="caesura"), E("pause", text="1"))] + notes[2:]
            rh.append([(extra_rh or {}).get(i, []) + notes])
            lh.append([[chord("whole", bass)]])
            vln.append([[chord("whole", tones[-1] + 12)]])
        return make_piano_score(os.path.join(self.scores, "Test", "orch.mscz"), rh, lh, title="Song",
                                parts=(("Violin", 40, [vln]),))

    def solo(self, measures=(0, 1, 2, 3, 6, 7, 8, 9), bpm=60, fermata_in=None, rests_in=()):
        """The orchestral score's measures ``measures`` (5-6 cut by default) at ``bpm``; ``fermata_in``: a measure
        index (in the solo) of two half notes, a fermata on the second; ``rests_in``: solo measures whose right hand
        rests."""
        rh, lh = [], []
        for k, i in enumerate(measures):
            bass, tones = HARMONY[i]
            head = [tempo(bpm)] if k == 0 else []
            if k in rests_in:
                rh.append([head + [rest("whole")]])
            elif k == fermata_in:
                rh.append([head + [chord("half", *tones), fermata(), chord("half", *tones)]])
            else:
                rh.append([head + [chord("whole", *tones)]])
            lh.append([[chord("whole", bass)]])
        return make_piano_score(os.path.join(self.scores, "Test", "solo.mscz"), rh, lh, title="Song")

    def partner(self, kind, path, rel):
        return {"kind": kind, "path": path, "rel": rel, "compat": None, "measure_map": "auto", "options": None}


# ------------------------------------------------------------------------------
# the plan
# ------------------------------------------------------------------------------

class ShapeTest(unittest.TestCase):
    def test_one_mark_for_a_steady_measure(self):
        marks, total, worst, clamped = T._shape([0, 480, 960, 1440, 1920], [0.0, 0.5, 1.0, 1.5, 2.0], 0.005)
        self.assertEqual(marks, [(0, 2.0)])
        self.assertAlmostEqual(total, 2.0)
        self.assertFalse(clamped)

    def test_a_bend_gets_a_mark_and_the_end_lands(self):
        # the solo slows to half speed on beat 3 (a fermata)
        marks, total, worst, _c = T._shape([0, 480, 960, 1440, 1920], [0.0, 0.5, 1.0, 2.0, 3.0], 0.005)
        self.assertEqual(marks, [(0, 2.0), (960, 1.0)])
        self.assertAlmostEqual(total, 3.0)
        self.assertLess(worst, 1e-6)

    def test_tempi_are_rounded_and_clamped_and_the_rest_carries(self):
        marks, total, _w, clamped = T._shape([0, 1, 1920], [0.0, 30.0, 31.0], 0.005)
        self.assertTrue(clamped)
        self.assertEqual(marks[0][1], round(5 / 60, 6))           # 5 bpm at most this slow
        self.assertAlmostEqual(total, 31.0, places=4)            # the next stretch makes up for it
        marks, _t, _w, _c = T._shape([0, 1920], [0.0, 1.0 / 3], 0.005)
        self.assertEqual(marks, [(0, 12.0)])

    def test_regular_warps(self):
        self.assertTrue(T._regular([(0, 0), (1920, 1920)]))
        self.assertTrue(T._regular([(0, 0), (960, 1200), (1920, 2400)]))
        self.assertFalse(T._regular([(0, 0), (1680, 0), (1920, 2400)]))   # a part of the measure without time


class PlanTest(Fixtures):
    def test_the_orchestral_score_plays_on_the_solos_clock(self):
        orch = self.orchestral()
        solo = self.solo()
        plain = convert_mscz(orch, parts=True)
        c = convert_mscz(orch, parts=True, options={"partner": self.partner("simplified", solo, "Test/solo.mscz")})
        song = json.loads(c.data)
        # every measure the solo plays lasts its 4 s (60 bpm); the cut measures 5-6 are stretched like their
        # neighbours (x2); the orchestral score's own fermata (m. 4) and pause (m. 9) are gone
        starts = {t: s for s, t, _p in song_notes(c.data, "left")}
        self.assertEqual([starts[k * W] for k in range(10)], [4.0 * k for k in range(10)])
        self.assertEqual(round(song["song_length"], 3), 40.0)
        self.assertEqual([t["bpm"] for t in song["tempos"]], [60.0])
        own = {t: s for s, t, _p in song_notes(plain.data, "left")}
        self.assertGreater(own[4 * W], 8.0)                       # (its own: the fermata made m. 4 longer)
        # the simplified part (the solo's notes) on that clock, the original and the orchestra too
        simp = c.parts["parts"]["simplified"]["right"]
        self.assertEqual({round(n["start"], 3) for n in simp if n.get("simplified")}, {0.0, 4.0, 8.0, 12.0, 24.0,
                                                                                      28.0, 32.0, 36.0})
        vln = c.parts["parts"]["orchestra"]["right"]
        self.assertEqual([round(n["start"], 3) for n in vln], [4.0 * k for k in range(10)])
        tl = c.arrangement["timeline"]
        self.assertEqual((tl["mapped"], tl["filled"], tl["seconds"]), (8, 2, 40.0))
        self.assertEqual(tl["stretch"], {"5": 2.0, "6": 2.0})

    def test_a_fermata_of_the_solo_lands_inside_the_measure(self):
        orch = self.orchestral()
        solo = self.solo(bpm=120, fermata_in=1)
        c = convert_mscz(orch, parts=True, options={"partner": self.partner("simplified", solo, "Test/solo.mscz")})
        right = [(s, t) for s, t, p in song_notes(c.data) if W <= t < 2 * W and p == HARMONY[1][1][0]]
        # solo m. 2: a half note (1 s), a half note under a fermata (2 s): beats 1-2 at 120 bpm, 3-4 at 60 (the
        # fermata's tempo ends a tick before the measure does: one mark covers beats 3-4 within the tolerance)
        self.assertEqual([t for _s, t in right], [W, W + Q, W + 2 * Q, W + 3 * Q])
        for (s, _t), want in zip(right, (2.0, 2.5, 3.0, 4.0)):
            self.assertAlmostEqual(s, want, delta=T.TOLERANCE)
        starts = {t: s for s, t, _p in song_notes(c.data, "left")}
        self.assertAlmostEqual(starts[2 * W] - starts[W], 3.0, delta=0.002)   # the measure lasts 3 s
        self.assertLessEqual(c.arrangement["timeline"]["max_inside_ms"], T.TOLERANCE * 1000)

    def test_the_copy_renders_like_the_model(self):
        orch = self.orchestral()
        solo = self.solo(fermata_in=1)
        pr = T.pair(orch, None, None, solo, None, None, T.SONG_ORCHESTRA)
        copy = T.write_edited(orch, os.path.join(self.tmp, "copy.mscz"), pr.plan)
        again = T.render(copy)
        self.assertEqual(onsets(again.midi), onsets(pr.orch_render.midi))
        self.assertEqual(extract_tempo_events(again.midi), extract_tempo_events(pr.orch_render.midi))
        with zipfile.ZipFile(copy) as z:
            text = z.read("score.mscx").decode()
        with zipfile.ZipFile(orch) as z:
            before = z.read("score.mscx").decode()
        self.assertEqual(text.count("<Tempo>"), len(pr.plan.marks))
        self.assertEqual(text.count("<visible>0</visible>"), len(pr.plan.marks))
        self.assertEqual(text.count("<play>0</play>"), before.count("<Fermata>"))
        self.assertIn("<pause>0</pause>", text)
        self.assertNotIn("<pause>1</pause>", text)
        # every mark was written before a chord or a rest
        for mk in pr.plan.marks:
            at = T._child_offsets(before, [mk.path])[mk.path]
            self.assertRegex(before[at:at + 6], r"<(Chord|Rest)")

    def test_a_mark_needs_a_chord_or_rest(self):
        orch = self.orchestral()
        score = __import__("pianovision.mscx", fromlist=["read_score"]).read_score(orch)
        pos = T.write_positions(score)
        self.assertEqual(sorted(pos[0]), [0, Q, 2 * Q, 3 * Q])
        self.assertEqual(pos[0][0][0][:2], (0, 0))                # the first staff's first measure


# ------------------------------------------------------------------------------
# orchestra_from
# ------------------------------------------------------------------------------

class ReverseRetimerTest(unittest.TestCase):
    def test_there_and_back(self):
        at = Timeline([UMeasure(i, i + 1, 1, i * W, W, (4, 4)) for i in range(6)])
        ot = Timeline([UMeasure(i, i + 1, 1, i * W, W, (4, 4)) for i in range(8)])
        al = A.Alignment(at, ot, [0, 1, None, 4, 5, 6], [[1.0] * 8] * 6).finish()
        fwd, back = A.Retimer(al), A.Retimer(al, reverse=True)
        on, off, how = fwd.note(W + 100, W + 300)
        self.assertEqual(back.note(on, off), (W + 100, W + 300, "mapped"))
        self.assertEqual(fwd.note(3 * W, 3 * W + 100)[0], 4 * W)
        self.assertEqual(back.note(4 * W + 10, 4 * W + 20)[:2], (3 * W + 10, 3 * W + 20))
        # orchestral measures 3-4 (indices 2-3) have no solo counterpart: their notes are not played
        self.assertIsNone(back.note(2 * W + 10, 2 * W + 20))
        # held from one into the next mapped measure: struck again there
        self.assertEqual(back.note(3 * W + 100, 4 * W + 400), (3 * W, 3 * W + 400, "restruck"))


class OrchestraFromTest(Fixtures):
    def test_the_solo_gets_the_orchestra_on_its_clock(self):
        orch = self.orchestral()
        solo = self.solo(rests_in=(5,))                          # the solo's m. 6 (orchestral 8): the right hand rests
        plain = convert_mscz(solo, parts=True)
        c = convert_mscz(solo, parts=True, options={"partner": self.partner("orchestra", orch, "Test/orch.mscz")})
        song, before = json.loads(c.data), json.loads(plain.data)
        self.assertEqual(song["tempos"], before["tempos"])        # the solo's tempo
        self.assertEqual(song_notes(c.data, "left"), song_notes(plain.data, "left"))
        self.assertEqual(c.parts["parts"]["original"], plain.parts["parts"]["original"])
        # the violin on the solo's measures: orchestral 5-6 are left out
        vln = c.parts["parts"]["orchestra"]["right"]
        self.assertEqual([(n["ticksStart"], n["note"]) for n in vln],
                         [(k * W, HARMONY[i][1][-1] + 12) for k, i in enumerate((0, 1, 2, 3, 6, 7, 8, 9))])
        self.assertEqual([round(n["start"], 3) for n in vln], [4.0 * k for k in range(8)])
        self.assertEqual({n["program"] for n in vln}, {40})
        # PianoVision's orchestra mode fills the measure where the solo's right hand rests, as for any score
        filled = [(s, p) for s, t, p in song_notes(c.data) if 5 * W <= t < 6 * W]
        self.assertEqual(filled, [(20.0, HARMONY[7][1][-1] + 12)])
        self.assertEqual([(s, p) for s, t, p in song_notes(plain.data) if 5 * W <= t < 6 * W], [])
        rep = c.arrangement
        self.assertEqual((rep["kind"], rep["measures"]["filled"], rep["notes"]["dropped_notes"]), ("orchestra", 2, 2))
        self.assertEqual(c.parts["orchestraFrom"]["score"], "Test/orch.mscz")
        self.assertIn("Orchestra from *Test/orch.mscz*", A.report_markdown(rep, "test_Song.json"))
        # the companion audio: orchestral 1-4 from 0 s, 7-10 from 16 s (5-6 are cut)
        self.assertEqual(rep["timeline"]["pieces"], [{"measures": ["1", "4"], "at": 0.0},
                                                     {"measures": ["7", "10"], "at": 16.0}])

    def test_partner_staves_are_orchestra_whatever_their_names(self):
        orch = make_piano_score(os.path.join(self.scores, "Test", "orch.mscz"),
                                *[[[[chord("whole", *HARMONY[i][1])]] for i in range(4)]] * 2,
                                parts=(("Piano 2", 0, [[[[chord("whole", 72)]] for _ in range(4)]]),))
        solo = make_piano_score(os.path.join(self.scores, "Test", "solo.mscz"),
                                *[[[[chord("whole", *HARMONY[i][1])]] for i in range(4)]] * 2)
        # the orchestral score's own options make 'Piano 2' (staff 3) orchestra; in the song it stays orchestra
        p = self.partner("orchestra", orch, "Test/orch.mscz")
        p["options"] = {"piano_staves": [1, 2], "hands": "staves"}
        c = convert_mscz(solo, parts=True, keep_midi=True, options={"partner": p})
        roles = staff_roles(c.midi)
        self.assertEqual([t for t, _k in roles.piano], [0, 1])
        self.assertEqual(roles.orchestra, [(2 + 2, 0)])
        self.assertEqual(len(c.parts["parts"]["orchestra"]["right"]), 4)


# ------------------------------------------------------------------------------
# build options and the library
# ------------------------------------------------------------------------------

class OptionsTest(unittest.TestCase):
    def test_partners(self):
        o = buildopts.BuildOptions.from_dict("a.mscz", {"orchestra_from": "b.mscz", "measure_map": "auto"})
        b = buildopts.BuildOptions.from_dict("b.mscz", {"song": False, "piano_staves": [3, 4], "hands": "split"})
        ro = o.render_options("/s", None, b)
        self.assertEqual(ro, {"partner": {"kind": "orchestra", "path": "/s/b.mscz", "rel": "b.mscz", "compat": None,
                                          "measure_map": "auto",
                                          "options": {"piano_staves": [3, 4], "hands": "split"}}})
        self.assertEqual(buildopts.without_partner(dict(ro, title="T")), {"title": "T"})
        for bad in ({"simplified_from": "x", "orchestra_from": "y"}, {"orchestra_from": "a.mscz"},
                    {"song": "no"}, {"song": False, "orchestra_from": "b.mscz"}):
            with self.assertRaises(buildopts.OptionsError):
                buildopts.BuildOptions.from_dict("a.mscz", bad)


class LibraryTest(Fixtures):
    def lib(self, options):
        path = os.path.join(self.tmp, "scores.json")
        with open(path, "w") as f:
            json.dump({"scores": options}, f)
        return Library(Config(scores=self.scores, output=os.path.join(self.tmp, "PV"), jobs=1, options=path),
                       log=lambda *_: None)

    def test_the_orchestral_score_becomes_the_solos_orchestra_and_retires(self):
        self.orchestral()
        self.solo()
        out = os.path.join(self.tmp, "PV")
        rep = self.lib({}).build()
        owner = {r: o for r, o, _w in rep.written}
        self.assertEqual(sorted(owner), ["Test/orch.mscz", "Test/solo.mscz"])
        opts = {"Test/solo.mscz": {"orchestra_from": "Test/orch.mscz"}, "Test/orch.mscz": {"song": False}}
        lib = self.lib(opts)
        acts = {a.rel: (a.kind, a.reason) for a in lib.plan(scan_library(self.scores))}
        self.assertEqual(acts["Test/orch.mscz"][0], "retire")
        self.assertEqual(acts["Test/solo.mscz"], ("render", "build options changed (scores.json)"))
        rep = lib.build()
        self.assertEqual([(r, o) for r, o, _w in rep.retired], [("Test/orch.mscz", owner["Test/orch.mscz"])])
        self.assertFalse(os.path.exists(os.path.join(out, owner["Test/orch.mscz"])))
        self.assertTrue(any(owner["Test/orch.mscz"] in a for a in rep.attic))
        with open(os.path.join(out, "NoteWaterfall", owner["Test/solo.mscz"][:-5] + ".parts.json")) as f:
            parts = json.load(f)
        self.assertEqual(len(parts["parts"]["orchestra"]["right"]), 8)
        self.assertEqual(parts["orchestraFrom"]["score"], "Test/orch.mscz")
        self.assertTrue(os.path.exists(os.path.join(out, ".arrangements", owner["Test/solo.mscz"][:-5] + ".md")))
        # nothing changes: nothing to do; the orchestral score is a partner only
        lib = self.lib(opts)
        acts = {a.rel: a.kind for a in lib.plan(scan_library(self.scores))}
        self.assertEqual(acts, {"Test/orch.mscz": "source", "Test/solo.mscz": "keep"})
        self.assertEqual(lib.build().written, [])
        # the partner is edited: the solo's song is rendered again
        self.orchestral(extra_rh={0: []}, n=10)
        os.utime(os.path.join(self.scores, "Test", "orch.mscz"), (1, 1))
        with zipfile.ZipFile(os.path.join(self.scores, "Test", "orch.mscz"), "a") as z:
            z.writestr("extra.mscx", "<museScore/>")              # a content change the hash sees
        acts = {a.rel: a.kind for a in self.lib(opts).plan(scan_library(self.scores))}
        self.assertEqual(acts["Test/solo.mscz"], "render")


# ------------------------------------------------------------------------------
# the companion audio
# ------------------------------------------------------------------------------

class AudioTest(Fixtures):
    def test_the_cache_key_of_other_scores_stays(self):
        base = ("sha", {"mute_vocals": True}, {"muted": {}}, {"mscore": "4.7.5"}, {"bitrate": 256})
        self.assertEqual(AC.cache_key(*base), AC.cache_key(*base, None))
        self.assertNotEqual(AC.cache_key(*base), AC.cache_key(*base, {"kind": "orchestra"}))

    def test_a_song_with_orchestra_from_renders_its_partner(self):
        self.orchestral()
        self.solo()
        opts = {"Test/solo.mscz": {"orchestra_from": "Test/orch.mscz"}, "Test/orch.mscz": {"song": False}}
        path = os.path.join(self.tmp, "scores.json")
        with open(path, "w") as f:
            json.dump({"scores": opts}, f)
        lib = Library(Config(scores=self.scores, output=os.path.join(self.tmp, "PV"), jobs=1, options=path),
                      log=lambda *_: None)
        lib.build()
        with open(os.path.join(self.tmp, "PV", ".manifest.json")) as f:
            manifest = json.load(f)
        cs = AC.candidates(manifest, self.scores, (), lib)
        self.assertEqual([(c.rel, c.source_rel) for c in cs], [("Test/solo.mscz", "Test/orch.mscz")])
        c = cs[0]
        self.assertEqual(c.partner["kind"], "orchestra")
        self.assertEqual(AC.candidates(manifest, self.scores, ("Test/orch*",), lib)[0].rel, "Test/solo.mscz")
        pr = AC.song_pair(c)
        self.assertEqual([(p.first, p.last, p.at) for p in pr.plan.pieces], [(0, 3, 0.0), (6, 9, 16.0)])
        # the render's times -> the song's clock (the cut measures 5-6 are left out)
        T.place_pieces(pr.plan, pr.orch.timeline, pr.orch.seconds, 50.0)
        self.assertEqual([(p.start, p.end) for p in pr.plan.pieces], [(0.0, 16.0), (24.0, 50.0)])
        copy = T.write_edited(c.source_path, os.path.join(self.tmp, "timeline.mscz"), pr.plan)
        a = AC.analyse(copy, c.source_compat, c.source_options)
        song = AC.on_song_clock(a, pr.plan.pieces)
        vln = next(st for st in song.staves if st.kind == AC.KIND_ORCHESTRA)
        self.assertEqual([round(t, 3) for t, _p in vln.attacks], [4.0 * k for k in range(8)])
        with open(c.parts_path) as f:
            check = AC.song_clock_check(song, json.load(f))
        self.assertEqual((check["matched"], check["notes"], check["max_ms"]), (8, 8, 0.0))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_pieces_are_placed_sample_exactly(self):
        src = os.path.join(self.tmp, "in.wav")
        dst = os.path.join(self.tmp, "out.wav")
        # 3 s of a tone that changes every second
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=1", "-f", "lavfi", "-i",
                        "sine=f=660:d=1", "-f", "lavfi", "-i", "sine=f=880:d=1", "-filter_complex",
                        "[0][1][2]concat=n=3:v=0:a=1,aresample=44100,pan=stereo|c0=c0|c1=c0", "-c:a", "pcm_f32le",
                        src], check=True)
        pieces = [T.Piece(0, 0, 0.5, 0.0, 1.0), T.Piece(2, 2, 1.5, 2.0, 3.0)]
        AC.place_wav(src, dst, pieces)
        self.assertAlmostEqual(AC.duration(dst), 2.5, places=2)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", dst, "-f", "f32le", "-ac", "1", "-"],
                             capture_output=True, check=True).stdout
        import array
        x = array.array("f")
        x.frombytes(raw)
        first = next(i for i, v in enumerate(x) if abs(v) > 1e-3)
        self.assertEqual(first, 22050 + 1)                       # sine starts at 0: its first non-zero sample
        self.assertLess(max(abs(v) for v in x[66150 - 40:66150]), 0.02)     # faded out where it is cut (1.5 s)
        self.assertGreater(max(abs(v) for v in x[66150 + 2000:66150 + 3000]), 0.1)  # the third second from 1.5 s


# ------------------------------------------------------------------------------
# hand edits of a song with a partner
# ------------------------------------------------------------------------------

class HandEditsTest(Fixtures):
    def test_the_solo_takes_hand_edits_its_orchestra_does_not(self):
        from pianovision import handedits as H
        self.orchestral()
        self.solo(rests_in=(5,))
        path = os.path.join(self.tmp, "scores.json")
        with open(path, "w") as f:
            json.dump({"scores": {"Test/solo.mscz": {"orchestra_from": "Test/orch.mscz"},
                                  "Test/orch.mscz": {"song": False}}}, f)
        lib = Library(Config(scores=self.scores, output=os.path.join(self.tmp, "PV"), jobs=1, options=path),
                      log=lambda *_: None)
        self.assertEqual(lib.build().failed, [])
        name = lib.manifest.entries["Test/solo.mscz"]["output"]
        with open(lib.out_path(name), "rb") as f:
            data = f.read()
        violin = HARMONY[7][1][-1] + 12                       # the orchestra fills the solo's resting right hand
        top = HARMONY[0][1][-1]
        edits = [{"from": "right", "to": "left", "ticksStart": 5 * W, "midi": violin, "occurrence": 0, "votes": 2,
                  "against": 0},
                 {"from": "right", "to": "left", "ticksStart": 0, "midi": top, "occurrence": 0, "votes": 2,
                  "against": 0}]
        side = os.path.join(self.tmp, H.sidecar_name(name))
        with open(side, "w") as f:
            json.dump({"format": 1, "song": name, "songMd5": H._md5(data), "updated": "2026-10-13T10:00:00Z",
                       "edits": edits}, f)
        rv = H.review_song(lib, name, [H.read_sidecar(side)])
        self.assertEqual(rv.error, "")
        res = {r.edit.midi: r for r in rv.results}
        self.assertEqual(res[violin].status, "skip")
        self.assertIn("orchestra from Test/orch.mscz", res[violin].reason)
        self.assertEqual(res[top].status, "move", H.format_review(rv))
