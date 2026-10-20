"""Simplified parts from arrangements (pianovision.arrangement), build options (scores.json) and the hand split."""

import json
import os
import random
import shutil
import tempfile
import unittest

from pianovision import arrangement as A
from pianovision import buildopts
from pianovision.arrangement import SNote, Timeline, UMeasure
from pianovision.convert import convert_mscz
from pianovision.handsplit import apply_piano_options, assign_hands, split_hands
from pianovision.library import Config, Library, scan_library
from pianovision.pvjson import TRACK_PRIMARY, classify_tracks
from pianovision.smf import MidiFile, Msg

from tests.scoregen import chord, make_piano_score

Q = 480
W = 4 * Q


# ------------------------------------------------------------------------------
# synthetic measures
# ------------------------------------------------------------------------------

def timeline(numbers, length=W, pauses=None) -> Timeline:
    """Measures as played, numbered as the score numbers them (a repeat: the same numbers again)."""
    passes = {}
    ms = []
    for i, num in enumerate(numbers):
        passes[num] = passes.get(num, 0) + 1
        ms.append(UMeasure(i, num, passes[num], i * length, length, (4, 4)))
    if pauses:
        keys = [0] + [k for k, _v in pauses]
        return Timeline(ms, keys, [0] + [v for _k, v in pauses])
    return Timeline(ms)


QUALITIES = [(0, 4, 7), (0, 3, 7), (0, 4, 7, 10), (0, 3, 6), (0, 4, 8), (0, 3, 7, 10), (0, 5, 7), (0, 4, 7, 11)]


def chords(n, seed):
    """n distinct random harmonies: (bass, chord tones, melody notes), as MIDI pitches."""
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        root = rng.randrange(12)
        q = rng.choice(QUALITIES)
        tones = [60 + root + x for x in q]
        mel = [72 + root + rng.choice(q), 72 + root + rng.choice(q)]
        c = (36 + root + rng.choice(q), tones, mel)
        if not out or out[-1][1] != c[1]:
            out.append(c)
    return out


def transposed(cs, k):
    return [(b + k, [t + k for t in tones], [m + k for m in mel]) for b, tones, mel in cs]


def notes(tl, cs):
    """A held bass and chord per measure and a melody of two half notes."""
    out = []
    for m, (bass, tones, mel) in zip(tl.measures, cs):
        out.append(SNote(0, bass, m.utick, m.end_utick))
        out.extend(SNote(0, p, m.utick, m.end_utick) for p in tones)
        out.append(SNote(0, mel[0], m.utick, m.utick + m.length // 2))
        out.append(SNote(0, mel[1], m.utick + m.length // 2, m.end_utick))
    return out


def align(arr_cs, orch_cs, arr_numbers=None, orch_numbers=None, measure_map="auto"):
    at = timeline(arr_numbers or range(1, len(arr_cs) + 1))
    ot = timeline(orch_numbers or range(1, len(orch_cs) + 1))
    af, of = A.measure_features(at, notes(at, arr_cs)), A.measure_features(ot, notes(ot, orch_cs))
    S = A.similarity_matrix(af, [of])
    return A.align(S, at, ot, (), A.read_overrides(measure_map, at, ot), af, [of], [1] * len(ot.measures))


class AlignmentTest(unittest.TestCase):
    def test_cuts_on_either_side(self):
        orch = chords(40, 1)
        extra = chords(3, 99)
        arr = orch[0:10] + orch[20:25] + extra + orch[25:40]
        al = align(arr, orch)
        self.assertEqual(al.mapping, list(range(10)) + list(range(20, 25)) + [None] * 3 + list(range(25, 40)))
        self.assertEqual(al.fills, list(range(10, 20)))
        self.assertEqual(al.drops, [15, 16, 17])
        self.assertEqual([(s.arr, s.orch) for s in al.segments], [((0, 9), (0, 9)), ((10, 14), (20, 24)),
                                                                  ((18, 32), (25, 39))])
        self.assertTrue(all(s.confidence == "high" for s in al.segments))

    def test_a_transposed_passage_is_reported_and_filled(self):
        orch = chords(30, 2)
        arr = orch[:10] + transposed(orch[10:18], 2) + orch[18:]
        al = align(arr, orch)
        self.assertEqual([(t.arr, t.orch, t.semitones) for t in al.transposed], [((10, 17), (10, 17), 2)])
        self.assertEqual(al.fills, list(range(10, 18)))
        self.assertEqual(al.drops, list(range(10, 18)))
        self.assertEqual([al.mapping[i] for i in list(range(10)) + list(range(18, 30))],
                         list(range(10)) + list(range(18, 30)))

    def test_a_repeat_the_arrangement_plays_once(self):
        cs = chords(10, 3)
        # the orchestral score repeats measures 3-6; the arrangement writes them once
        orch_numbers = [1, 2, 3, 4, 5, 6, 3, 4, 5, 6, 7, 8, 9, 10]
        orch = cs[:6] + cs[2:6] + cs[6:]
        al = align(cs, orch, orch_numbers=orch_numbers)
        self.assertEqual(len(al.fills), 4)
        filled = {al.orch.label(j) for j in al.fills}
        self.assertIn(filled, ({"3:1", "4:1", "5:1", "6:1"}, {"3:2", "4:2", "5:2", "6:2"}))
        self.assertEqual(al.drops, [])
        mapped = [al.orch.measures[j].number for j in al.mapping]
        self.assertEqual(mapped, list(range(1, 11)))           # every measure on a pass of the same measure

    def test_a_repeat_on_both_sides_maps_pass_to_pass(self):
        cs = chords(8, 4)
        numbers = [1, 2, 3, 4, 3, 4, 5, 6, 7, 8]
        seq = cs[:4] + cs[2:4] + cs[4:]
        al = align(seq, seq, numbers, numbers)
        self.assertEqual(al.mapping, list(range(10)))
        self.assertEqual([al.arr.label(i) for i in (2, 4)], ["3:1", "3:2"])

    def test_overrides(self):
        orch = chords(20, 5)
        arr = orch[:]
        al = align(arr, orch, measure_map={"fill": ["8"], "_why": "comment"})
        self.assertEqual((al.mapping[7], al.fills, al.drops), (None, [7], [7]))
        al = align(arr, orch, measure_map={"pin": [["5-6", "11-12"]]})
        self.assertEqual((al.mapping[4], al.mapping[5]), (10, 11))
        with self.assertRaises(ValueError):
            align(arr, orch, measure_map={"pin": [["5-6", "11"]]})
        with self.assertRaises(ValueError):
            align(arr, orch, measure_map={"cut": ["1"]})

    def test_a_bridge_measure_moves_into_the_cut_it_belongs_to(self):
        orch = chords(30, 6)
        # the arrangement plays orchestral 0-9, a measure from inside the cut (24), then 28-29: the gap-open cost alone
        # would put that measure on 27
        arr = orch[:10] + [orch[24]] + orch[28:]
        al = align(arr, orch)
        self.assertEqual(al.mapping[10], 24)
        self.assertEqual(al.moved, [(10, 27, 24)])

    def test_resolve(self):
        tl = timeline([1, 2, 3, 2, 3, 4])
        self.assertEqual(tl.resolve("2"), [1, 3])
        self.assertEqual(tl.resolve("2:2"), [3])
        self.assertEqual(tl.resolve("2-3"), [1, 2])
        self.assertEqual(tl.resolve("2:2-4"), [3, 4, 5])
        with self.assertRaises(ValueError):
            tl.resolve("9")

    def test_text_anchors(self):
        self.assertEqual(A.text_similarity("Grandisoso", "Grandioso (not too slow)"), 1 / 2)   # "not", "too": noise
        self.assertEqual(A.text_similarity("Andantino moderato", "Andantino moderato, con espressione"), 2 / 3)
        a, o = timeline(range(1, 5)), timeline(range(1, 5))
        a.texts = [(1, "tempo", "Tempo giusto"), (2, "tempo", "a tempo"), (3, "tempo", "a tempo"),
                   (0, "tempo", "a tempo")]
        o.texts = [(2, "tempo", "Tempo giusto")]
        self.assertEqual([(x.arr, x.orch) for x in A.text_anchors(a, o)], [(1, 2)])


# ------------------------------------------------------------------------------
# retiming
# ------------------------------------------------------------------------------

def note(t, dur, pitch=60, **kw):
    return {"note": pitch, "ticksStart": t, "durationTicks": dur, "start": t / 960, "duration": dur / 960,
            "velocity": 0.5, "measureInd": int((t / Q) / 4), "accent": 0, **kw}


def seconds(t):
    return t / 960


class RetimeTest(unittest.TestCase):
    def alignment(self, mapping, n_orch, pauses=None):
        at, ot = timeline(range(1, len(mapping) + 1)), timeline(range(1, n_orch + 1), pauses=pauses)
        S = [[1.0] * n_orch for _ in mapping]
        return A.Alignment(at, ot, list(mapping), S).finish()

    def test_offsets_in_the_measure_follow_the_mapped_measure(self):
        al = self.alignment([0, 1, 2, None, 6, 7], 10)
        simp, st = A.retime({"right": [note(W + 480, 240, 64)], "left": [note(4 * W + 960, 480, 48)]},
                            {"right": [], "left": []}, al, seconds, Q)
        r, l = simp["right"][0], simp["left"][0]
        self.assertEqual((r["ticksStart"], r["durationTicks"], r["simplified"]), (W + 480, 240, 1))
        self.assertEqual((l["ticksStart"], l["durationTicks"]), (6 * W + 960, 480))
        self.assertEqual((l["start"], l["measureInd"]), ((6 * W + 960) / 960, 6))
        self.assertEqual(st.mapped_notes, 2)

    def test_the_orchestral_clock_pauses_included(self):
        al = self.alignment([0, 1], 2, pauses=[(W, W)])          # a pause before the second orchestral measure
        simp, _st = A.retime({"right": [note(W + 480, 240)], "left": []}, {}, al, seconds, Q)
        self.assertEqual(simp["right"][0]["ticksStart"], 2 * W + 480)
        # a note held over the pause: its end moves with it
        simp, _st = A.retime({"right": [note(960, W)], "left": []}, {}, al, seconds, Q)
        self.assertEqual((simp["right"][0]["ticksStart"], simp["right"][0]["durationTicks"]), (960, 2 * W))

    def test_a_note_held_into_a_cut_ends_there(self):
        al = self.alignment([0, 1, 2, None, 6, 7], 10)
        simp, st = A.retime({"right": [note(2 * W + 960, W)], "left": []}, {}, al, seconds, Q)
        self.assertEqual((simp["right"][0]["ticksStart"], simp["right"][0]["durationTicks"]), (2 * W + 960, 960))
        self.assertEqual(st.clipped_notes, 1)
        # held over a cut on the orchestral side (arrangement 2 -> 3 follow on, orchestral 2 -> 6 do not)
        al = self.alignment([0, 1, 2, 6, 7], 10)
        simp, st = A.retime({"right": [note(2 * W + 960, W)], "left": []}, {}, al, seconds, Q)
        self.assertEqual(simp["right"][0]["durationTicks"], 960)
        self.assertEqual(st.clipped_notes, 1)

    def test_a_note_held_from_a_dropped_measure_is_struck_again(self):
        al = self.alignment([0, 1, 2, None, 6, 7], 10)
        simp, st = A.retime({"right": [note(3 * W + 960, 2 * W), note(3 * W, 480, 62)], "left": []}, {}, al,
                            seconds, Q)
        self.assertEqual([(n["ticksStart"], n["durationTicks"]) for n in simp["right"]], [(6 * W, W + 960)])
        self.assertEqual((st.restruck_notes, st.dropped_notes), (1, 1))

    def test_cuts_are_filled_from_the_orchestral_piano_part(self):
        al = self.alignment([0, 1, 2, None, 6, 7], 10)
        orig = {"right": [note(1 * W, 480, 70), note(3 * W, 480, 71), note(5 * W + 960, W, 72),
                          note(3 * W + 960, W, 73)], "left": [note(4 * W, 480, 40)]}
        simp, st = A.retime({"right": [], "left": []}, orig, al, seconds, Q)
        got = {(n["note"], n["ticksStart"], n["durationTicks"], n.get("simplified")) for h in simp for n in simp[h]}
        # 70 is in a mapped measure: the arrangement's; 72 is held into a mapped measure: it ends there
        self.assertEqual(got, {(71, 3 * W, 480, None), (73, 3 * W + 960, W, None), (40, 4 * W, 480, None),
                               (72, 5 * W + 960, 960, None)})
        self.assertEqual((st.fill_notes, st.fill_clipped), (4, 1))
        self.assertEqual(al.fills, [3, 4, 5, 8, 9])

    def test_two_arrangement_measures_over_one(self):
        al = self.alignment([0, 1, 1, 2], 3)
        self.assertEqual([(u.arr, u.orch) for u in al.units], [((0, 0), (0, 0)), ((1, 2), (1, 1)), ((3, 3), (2, 2))])
        simp, _st = A.retime({"right": [note(2 * W, 480)], "left": []}, {}, al, seconds, Q)
        self.assertEqual(simp["right"][0]["ticksStart"], W + W // 2)       # linearly: half way through the measure

    def test_a_longer_measure_is_warped_through_the_shared_onsets(self):
        at = Timeline([UMeasure(0, 1, 1, 0, 5 * Q, (4, 4))])
        ot = Timeline([UMeasure(0, 1, 1, 0, 4 * Q, (4, 4))])
        al = A.Alignment(at, ot, [0], [[1.0]]).finish()
        run = [64, 66, 68, 69]
        arr = [SNote(0, p, k * 60, k * 60 + 60) for k, p in enumerate(run)]
        orch = [SNote(0, 52, 0, 480), SNote(0, 66, 480, 960)] + [SNote(1, p, 1440 + k * 30, 1470 + k * 30)
                                                                 for k, p in enumerate(run)]
        rt = A.Retimer(al, arr, orch)
        self.assertEqual([rt.note(n.on, n.off)[0] for n in arr], [1440, 1470, 1500, 1530])
        # without shared onsets: linear
        self.assertEqual(A.Retimer(al, arr, []).note(5 * Q - 100, 5 * Q)[0], round((5 * Q - 100) * 4 / 5))


# ------------------------------------------------------------------------------
# scores: the simplified part of an orchestral score from an arrangement, and build options
# ------------------------------------------------------------------------------

HARMONY = [(48, [60, 64, 67]), (45, [57, 60, 64]), (41, [57, 60, 65]), (43, [59, 62, 67]), (40, [56, 59, 64]),
           (38, [57, 62, 65]), (46, [58, 62, 65]), (39, [55, 58, 63]), (44, [60, 63, 68]), (49, [61, 65, 68])]


def piano_measures(indices, rhythm=None):
    rh, lh = [], []
    for i in indices:
        bass, tones = HARMONY[i]
        if rhythm and i in rhythm:
            rh.append([[chord("half", *tones), chord("half", *[t + 12 for t in tones])]])
        else:
            rh.append([[chord("whole", *tones)]])
        lh.append([[chord("whole", bass)]])
    return rh, lh


class ScoreFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scores = os.path.join(self.tmp, "Scores")
        os.makedirs(os.path.join(self.scores, "Test"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def orchestral(self):
        rh, lh = piano_measures(range(10))
        vln = [[[chord("whole", HARMONY[i][1][-1] + 12)]] for i in range(10)]
        return make_piano_score(os.path.join(self.scores, "Test", "orch.mscz"), rh, lh, title="Song",
                                parts=(("Violin", 40, [vln]),))

    def arrangement(self, rhythm=None):
        # orchestral measures 1-4, 7-10 (measures 5-6 cut), the arranger's rhythm in some
        rh, lh = piano_measures([0, 1, 2, 3, 6, 7, 8, 9], rhythm)
        return make_piano_score(os.path.join(self.scores, "Test", "solo.mscz"), rh, lh, title="Song")

    def options(self, **kw):
        o = {"partner": {"kind": "simplified", "path": self.arrangement(), "rel": "Test/solo.mscz", "compat": None,
                         "measure_map": "auto", "options": None}}
        o.update(kw)
        return o


class ScoresTest(ScoreFixtures):
    def test_the_simplified_part_is_the_arrangement_measure_aligned(self):
        orch = self.orchestral()
        plain = convert_mscz(orch, parts=True)
        c = convert_mscz(orch, parts=True, options=self.options())
        self.assertEqual(c.data, plain.data)                     # the same tempo: PianoVision's JSON is the same
        doc = c.parts
        self.assertTrue(doc["simplified"])
        self.assertEqual(list(doc["parts"]), ["original", "simplified", "orchestra"])
        self.assertEqual(doc["parts"]["original"], plain.parts["parts"]["original"])
        self.assertEqual(doc["parts"]["orchestra"], plain.parts["parts"]["orchestra"])
        self.assertEqual(doc["arrangement"]["measures"], {"mapped": 8, "filled": 2, "dropped": 0})
        simp = doc["parts"]["simplified"]
        mine = sorted((n["ticksStart"], n["note"]) for n in simp["right"] if n.get("simplified"))
        filled = sorted((n["ticksStart"], n["note"]) for n in simp["right"] if not n.get("simplified"))
        self.assertEqual({t // W for t, _p in mine}, {0, 1, 2, 3, 6, 7, 8, 9})
        self.assertEqual(filled, [(4 * W, p) for p in HARMONY[4][1]] + [(5 * W, p) for p in HARMONY[5][1]])
        self.assertEqual(c.arrangement["fills"][0]["orchestra"], "5-6")

    def test_the_arrangement_rhythm_lands_in_the_mapped_measure(self):
        orch = self.orchestral()
        opts = self.options()
        opts["partner"]["path"] = self.arrangement(rhythm={7})
        c = convert_mscz(orch, parts=True, options=opts)
        right = [(n["ticksStart"], n["note"]) for n in c.parts["parts"]["simplified"]["right"]
                 if 7 * W <= n["ticksStart"] < 8 * W]
        self.assertEqual(sorted(right), sorted([(7 * W, p) for p in HARMONY[7][1]]
                                               + [(7 * W + 2 * Q, p + 12) for p in HARMONY[7][1]]))

    def test_title_and_composer(self):
        orch = self.orchestral()
        c = convert_mscz(orch, options={"title": "Song (Piano and Orchestra)", "composer": "Tester"})
        song = json.loads(c.data)
        self.assertEqual((c.title, c.artist, c.name), ("Song (Piano and Orchestra)", "Tester",
                                                         "test_Song_Piano_and_Orchestra.json"))
        self.assertEqual((song["name"], song["artist"]), ("Song (Piano and Orchestra)", "Tester"))


class LibraryOptionsTest(ScoreFixtures):
    def lib(self, options):
        path = os.path.join(self.tmp, "scores.json")
        with open(path, "w") as f:
            json.dump({"scores": options}, f)
        return Library(Config(scores=self.scores, output=os.path.join(self.tmp, "PV"), jobs=1, options=path),
                       log=lambda *_: None)

    OPTS = {"Test/orch.mscz": {"title": "Song (Piano and Orchestra)", "composer": "Tester",
                               "simplified_from": "Test/solo.mscz", "measure_map": "auto"}}

    def test_build_names_the_song_writes_the_report_and_follows_the_arrangement(self):
        self.orchestral()
        self.arrangement()
        rep = self.lib(self.OPTS).build()
        self.assertIn(("Test/orch.mscz", "test_Song_Piano_and_Orchestra.json", "new score"), rep.written)
        out = os.path.join(self.tmp, "PV")
        parts = os.path.join(out, "NoteWaterfall", "test_Song_Piano_and_Orchestra.parts.json")
        with open(parts) as f:
            self.assertEqual(json.load(f)["arrangement"]["score"], "Test/solo.mscz")
        self.assertTrue(os.path.exists(os.path.join(out, ".arrangements", "test_Song_Piano_and_Orchestra.md")))
        self.assertEqual([r for r, _md, _s in rep.arrangements], ["Test/orch.mscz"])
        self.assertEqual(self.lib(self.OPTS).build().written, [])           # nothing changed
        # the arrangement is edited: the orchestral score is rendered again (it plays on the arrangement's clock); with
        # the same tempo its JSON stays, its parts are written again
        with open(os.path.join(out, "test_Song_Piano_and_Orchestra.json"), "rb") as f:
            song = f.read()
        self.arrangement(rhythm={0})
        lib = self.lib(self.OPTS)
        acts = {a.rel: (a.kind, a.reason) for a in lib.plan(scan_library(self.scores))}
        self.assertEqual(acts["Test/orch.mscz"][0], "render")
        self.assertTrue(acts["Test/orch.mscz"][1].startswith("partner score changed (Test/solo.mscz"))
        rep = lib.build()
        self.assertIn("NoteWaterfall/test_Song_Piano_and_Orchestra.parts.json", rep.parts)
        with open(os.path.join(out, "test_Song_Piano_and_Orchestra.json"), "rb") as f:
            self.assertEqual(f.read(), song)
        # the title changes: rendered again (the output keeps its name)
        opts = {k: dict(v, title="Song II") for k, v in self.OPTS.items()}
        acts = {a.rel: a for a in self.lib(opts).plan(scan_library(self.scores))}
        self.assertEqual((acts["Test/orch.mscz"].kind, acts["Test/orch.mscz"].reason),
                         ("render", "build options changed (scores.json)"))

    def test_bad_options_are_refused(self):
        for bad in ({"hands": "split"}, {"hands": "both", "piano_staves": [1]}, {"titel": "x"},
                    {"piano_staves": [0]}, {"simplified_from": "Test/orch.mscz"}):
            with self.assertRaises(buildopts.OptionsError):
                buildopts.BuildOptions.from_dict("Test/orch.mscz", bad)

    def test_the_checked_in_options_parse(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), buildopts.OPTIONS_NAME)
        opts = buildopts.load(path)
        self.assertTrue(opts)
        for rel, o in opts.items():
            ro = o.render_options("/scores")
            if o.partner:
                self.assertEqual(ro["partner"]["rel"], o.partner)
                self.assertEqual(ro["partner"]["kind"], "simplified" if o.simplified_from else "orchestra")
                self.assertTrue(o.song)
        # a score that is not a song of its own is another's partner
        for rel, o in opts.items():
            if not o.song:
                self.assertTrue(any(x.partner == rel for x in opts.values()), rel)


# ------------------------------------------------------------------------------
# piano_from: the arrangement is the song's piano part
# ------------------------------------------------------------------------------

def pv_notes(data, hand):
    """A PianoVision JSON's notes of one hand as parts-file NOTE fields, in time order."""
    doc = json.loads(data)
    return sorted(((n["start"], n["ticksStart"], n["note"], n["durationTicks"], n["duration"], n["velocity"],
                    n["accent"]) for m in doc["tracksV2"][hand] for n in m["notes"]))


def part_notes(part, hand):
    return sorted((n["start"], n["ticksStart"], n["note"], n["durationTicks"], n["duration"], n["velocity"],
                   n["accent"]) for n in part[hand])


class PianoFromTest(ScoreFixtures):
    def piano_options(self):
        o = self.options()
        o["partner"]["piano"] = True
        return o

    def test_the_arrangement_is_the_piano_part_where_it_plays(self):
        orch = self.orchestral()
        simp = convert_mscz(orch, parts=True, options=self.options())
        c = convert_mscz(orch, parts=True, provenance=True, options=self.piano_options())
        doc = c.parts
        # no separate simplified part: the original part is what the simplified part was
        self.assertFalse(doc["simplified"])
        self.assertEqual(list(doc["parts"]), ["original", "orchestra"])
        self.assertEqual(doc["parts"]["original"], simp.parts["parts"]["simplified"])
        self.assertEqual(doc["parts"]["orchestra"], simp.parts["parts"]["orchestra"])
        self.assertEqual(doc["arrangement"], dict(simp.parts["arrangement"], part="original"))
        right = doc["parts"]["original"]["right"]
        self.assertEqual({n["ticksStart"] // W for n in right if n.get("simplified")}, {0, 1, 2, 3, 6, 7, 8, 9})
        self.assertEqual(sorted((n["ticksStart"], n["note"]) for n in right if not n.get("simplified")),
                         [(4 * W, p) for p in HARMONY[4][1]] + [(5 * W, p) for p in HARMONY[5][1]])
        # PianoVision's hands are the same notes (the piano plays every measure: nothing of the orchestra merged)
        for hand in ("right", "left"):
            self.assertEqual(pv_notes(c.data, hand), part_notes(doc["parts"]["original"], hand))
        # the clock does not change: the same tempo map and measures
        a, b = json.loads(c.data), json.loads(simp.data)
        self.assertEqual((a["tempos"], a["measures"], a["timeSignatures"]),
                         (b["tempos"], b["measures"], b["timeSignatures"]))
        # the arrangement's notes have no note of this score; the fills have theirs
        prov = c.provenance["right"]
        self.assertEqual(sum(ns.arranged for ns in prov), sum(1 for n in right if n.get("simplified")))
        self.assertTrue(all(ns.note is None for ns in prov if ns.arranged))
        self.assertTrue(all(ns.note is not None for ns in prov if not ns.arranged))
        self.assertTrue(c.arrangement["piano_from"])
        self.assertEqual(c.arrangement["notes"], simp.arrangement["notes"])
        self.assertIn("The piano part", A.report_markdown(c.arrangement))
        # without parts the JSON is the same
        self.assertEqual(convert_mscz(orch, options=self.piano_options()).data, c.data)

    def test_accents_are_the_arrangements(self):
        from tests.scoregen import accent
        rh, lh = piano_measures(range(10))
        rh[1] = [[chord("whole", *HARMONY[1][1], extra=[accent()])]]     # the orchestral score's, mapped
        rh[4] = [[chord("whole", *HARMONY[4][1], extra=[accent()])]]     # ... in the cut: kept
        vln = [[[chord("whole", HARMONY[i][1][-1] + 12)]] for i in range(10)]
        orch = make_piano_score(os.path.join(self.scores, "Test", "orch.mscz"), rh, lh, title="Song",
                                parts=(("Violin", 40, [vln]),))
        opts = self.piano_options()
        srh, slh = piano_measures([0, 1, 2, 3, 6, 7, 8, 9])
        srh[2] = [[chord("whole", *HARMONY[2][1], extra=[accent()])]]    # the solo's own accent
        solo = make_piano_score(opts["partner"]["path"], srh, slh, title="Song")
        solo_own = convert_mscz(solo, parts=True)
        c = convert_mscz(orch, parts=True, options=opts)

        def accents(notes):
            out = {}
            for n in notes:
                out[n["ticksStart"] // W] = max(out.get(n["ticksStart"] // W, 0), n["accent"])
            return out

        for notes in ([n for m in json.loads(c.data)["tracksV2"]["right"] for n in m["notes"]],
                      c.parts["parts"]["original"]["right"]):
            acc = accents(notes)
            self.assertEqual((acc[1], acc[2], acc[4]), (0, 1, 1))
        # the solo's accented chord: its velocity boosted once, as in the solo's own song
        own = [n for n in solo_own.parts["parts"]["original"]["right"] if n["ticksStart"] // W == 2]
        mine = [n for n in c.parts["parts"]["original"]["right"] if n["ticksStart"] // W == 2]
        self.assertEqual(sorted((n["note"], n["velocity"], n["accent"]) for n in mine),
                         sorted((n["note"], n["velocity"], n["accent"]) for n in own))

    def test_options(self):
        o = buildopts.BuildOptions.from_dict("a.mscz", {"simplified_from": "b.mscz", "piano_from": "simplified_from"})
        self.assertEqual(o.render_options("/s")["partner"]["piano"], True)
        plain = buildopts.BuildOptions.from_dict("a.mscz", {"simplified_from": "b.mscz"})
        self.assertNotIn("piano", plain.render_options("/s")["partner"])
        for bad in ({"piano_from": "simplified_from"}, {"orchestra_from": "b.mscz", "piano_from": "simplified_from"},
                    {"simplified_from": "b.mscz", "piano_from": "b.mscz"},
                    {"simplified_from": "b.mscz", "piano_from": "orchestra_from"}):
            with self.assertRaises(buildopts.OptionsError, msg=bad):
                buildopts.BuildOptions.from_dict("a.mscz", bad)


class PianoFromLibraryTest(ScoreFixtures):
    OPTS = LibraryOptionsTest.OPTS
    PIANO = {k: dict(v, piano_from="simplified_from") for k, v in OPTS.items()}

    def lib(self, options):
        return LibraryOptionsTest.lib(self, options)

    def test_build_follows_the_option_and_only_this_song_changes(self):
        self.orchestral()
        self.arrangement()
        self.lib(self.OPTS).build()
        out = os.path.join(self.tmp, "PV")
        with open(os.path.join(out, ".manifest.json")) as f:
            before = json.load(f)["entries"]
        lib = self.lib(self.PIANO)
        acts = {a.rel: a for a in lib.plan(scan_library(self.scores))}
        self.assertEqual((acts["Test/orch.mscz"].kind, acts["Test/orch.mscz"].reason),
                         ("render", "build options changed (scores.json)"))
        self.assertNotEqual(acts["Test/solo.mscz"].kind, "render")
        rep = lib.build()
        self.assertEqual([r for r, _md, _s in rep.arrangements], ["Test/orch.mscz"])
        self.assertTrue(rep.arrangements[0][2].startswith("piano part from Test/solo.mscz"))
        with open(os.path.join(out, "NoteWaterfall", "test_Song_Piano_and_Orchestra.parts.json")) as f:
            parts = json.load(f)
        self.assertEqual((parts["simplified"], list(parts["parts"])), (False, ["original", "orchestra"]))
        with open(os.path.join(out, ".manifest.json")) as f:
            after = json.load(f)["entries"]
        self.assertEqual(after["Test/solo.mscz"], before["Test/solo.mscz"])
        self.assertEqual(after["Test/orch.mscz"]["options"]["partner"], before["Test/orch.mscz"]["options"]["partner"])
        self.assertEqual(self.lib(self.PIANO).build().written, [])           # nothing changed

    def test_hand_edits_of_the_solos_notes_go_through_the_solos_song(self):
        from pianovision import handedits as H
        self.orchestral()
        self.arrangement()
        lib = self.lib(self.PIANO)
        self.assertEqual(lib.build().failed, [])
        name = lib.manifest.entries["Test/orch.mscz"]["output"]
        solo_song = lib.manifest.entries["Test/solo.mscz"]["output"]
        with open(lib.out_path(name), "rb") as f:
            data = f.read()
        top, bass5 = HARMONY[0][1][-1], HARMONY[4][0]

        def edit(fr, to, tick, midi, part=None):
            e = {"from": fr, "to": to, "ticksStart": tick, "midi": midi, "occurrence": 0, "votes": 2, "against": 0}
            if part:
                e["part"] = part
            return e

        edits = [edit("right", "left", 0, top, "simplified"),     # the solo's note, as Note Waterfall names it
                 edit("right", "left", W, HARMONY[1][1][0]),       # the solo's note in PianoVision's song
                 edit("right", "left", 0, top, "original"),        # the orchestral score's note it replaced
                 edit("left", "right", 4 * W, bass5, "original")]  # the orchestral score's note in the cut
        side = os.path.join(self.tmp, H.sidecar_name(name))
        with open(side, "w") as f:
            json.dump({"format": 2, "song": name, "songMd5": H._md5(data), "updated": "2026-10-20T10:00:00Z",
                       "edits": edits}, f)
        rv = H.review_song(lib, name, [H.read_sidecar(side)])
        self.assertEqual(rv.error, "")
        res = [(r.edit.part, r.edit.ticks, r.edit.midi, r.status, r.reason) for r in rv.results]
        by = {(p, t, m): (s, why) for p, t, m, s, why in res}
        for key in (("simplified", 0, top), (None, W, HARMONY[1][1][0])):
            status, why = by[key]
            self.assertEqual(status, "skip", res)
            self.assertIn("piano solo Test/solo.mscz", why)
            self.assertIn(solo_song, why)
        self.assertEqual(by[("original", 0, top)][0], "stale")
        self.assertEqual(by[("original", 4 * W, bass5)][0], "move", H.format_review(rv))

    def test_the_companion_audios_clock_key_stays(self):
        from types import SimpleNamespace
        from pianovision import accompaniment as AC
        orch = self.orchestral()
        self.arrangement()
        keys = []
        for opts in (self.OPTS, self.PIANO):
            ro = self.lib(opts).render_options("Test/orch.mscz")
            keys.append(AC.timeline_key(SimpleNamespace(partner=ro["partner"], path=orch, entry={})))
        self.assertEqual(keys[0], keys[1])


# ------------------------------------------------------------------------------
# the hand split
# ------------------------------------------------------------------------------

class HandSplitTest(unittest.TestCase):
    def test_hands_far_apart(self):
        notes = [(k * Q, k * Q + Q, 36 + (k % 3)) for k in range(8)] + [(k * Q, k * Q + Q, 84 - (k % 4))
                                                                         for k in range(8)]
        self.assertEqual(assign_hands(notes), [1] * 8 + [0] * 8)

    def test_a_chord_in_each_hand(self):
        self.assertEqual(assign_hands([(0, Q, p) for p in (48, 52, 55, 72, 76, 79)]), [1, 1, 1, 0, 0, 0])

    def test_an_arpeggio_under_a_held_melody_stays_in_the_left_hand(self):
        notes = [(0, 4 * Q, 77)] + [(k * Q // 2, k * Q // 2 + Q // 2, p) for k, p in enumerate((41, 48, 53, 57))]
        self.assertEqual(assign_hands(notes), [0, 1, 1, 1, 1])

    def test_split_moves_notes_between_the_piano_tracks(self):
        def track(*msgs):
            return list(msgs)
        mf = MidiFile(ticks_per_beat=Q)
        mf.tracks = [track(Msg("track_name", name="Piano"), Msg("program_change", channel=0, program=0),
                           Msg("note_on", channel=0, note=75, velocity=80, time=Q),
                           Msg("note_off", channel=0, note=75, velocity=0, time=Q), Msg("end_of_track", time=1)),
                     track(Msg("track_name", name="Piano, Roland Piano"), Msg("program_change", channel=1, program=0),
                           Msg("note_on", channel=1, note=36, velocity=80), Msg("note_on", channel=1, note=84, velocity=80),
                           Msg("note_off", channel=1, note=36, velocity=0, time=W),
                           Msg("note_off", channel=1, note=84, velocity=0), Msg("end_of_track", time=1)),
                     track(Msg("track_name", name="Piano, Roland Piano"), Msg("end_of_track", time=W + 1))]
        piano, _o = classify_tracks(mf, True)
        self.assertEqual(piano, [(0, TRACK_PRIMARY), (1, TRACK_PRIMARY), (2, TRACK_PRIMARY)])
        done = apply_piano_options(mf, {"piano_staves": [2, 3], "hands": "split"})
        self.assertEqual(done, {"piano_staves": [2, 3], "moved": 1})
        piano, orch = classify_tracks(mf, True)
        self.assertEqual((piano, orch), ([(1, TRACK_PRIMARY), (2, TRACK_PRIMARY)], [(0, 0)]))

        def notes_of(t):
            out, tick = [], 0
            for m in mf.tracks[t]:
                tick += m.time
                if m.type.startswith("note"):
                    out.append((tick, m.type, m.note))
            return out
        self.assertEqual(notes_of(1), [(0, "note_on", 84), (W, "note_off", 84)])
        self.assertEqual(notes_of(2), [(0, "note_on", 36), (W, "note_off", 36)])
        self.assertEqual([m.type for m in mf.tracks[1]][:2], ["track_name", "program_change"])
        self.assertEqual(sum(m.time for m in mf.tracks[2]), W + 1)          # where the track ended

    def test_split_without_two_staves_does_nothing(self):
        mf = MidiFile(ticks_per_beat=Q)
        mf.tracks = [[Msg("end_of_track", time=1)]]
        self.assertEqual(split_hands(mf, [0]), 0)


if __name__ == "__main__":
    unittest.main()
