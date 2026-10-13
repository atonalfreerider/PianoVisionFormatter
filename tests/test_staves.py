"""Which staves are the piano part, the orchestra and vocal lines (pianovision.pvjson.staff_roles)."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from pianovision import pvjson
from pianovision.convert import convert_mscz
from pianovision.handsplit import apply_piano_options
from pianovision.library import Config, Library
from pianovision.mscx import read_score
from pianovision.pvjson import STAVES_REVISION, StaffRoles, _legacy_roles, staff_roles
from pianovision.render import Compat, render_score

from tests.scoregen import E, chord, make_piano_score

Q = 480
W = 4 * Q          # a 4/4 measure
P = pvjson.TRACK_PRIMARY

RH = [[[chord("half", 72), chord("half", 67)]], [[chord("whole", 74)]]]
LH = [[[chord("whole", 48)]], [[chord("whole", 43)]]]


def sung(dur, *pitches, text="la"):
    return chord(dur, *pitches, extra=(E("Lyrics", E("text", text=text)),))


def harmony():
    """A C major 7 chord symbol (MuseScore plays it on the part's "harmony" channel)."""
    return E("Harmony", E("harmonyInfo", E("name", text="maj7"), E("root", text="14")))


def pv_keys(doc, hand):
    return sorted((n["ticksStart"], n["note"]) for m in doc["tracksV2"][hand] for n in m["notes"])


def keys(notes, *extra):
    return [(n["ticksStart"], n["note"]) + tuple(n.get(k) for k in extra) for n in notes]


SOPRANO = [[[harmony(), sung("half", 76), sung("half", 77)]], [[sung("whole", 79)]]]


class StaffRolesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def score(self, rh=RH, lh=LH, **kw):
        return make_piano_score(os.path.join(self.tmp, "song.mscz"), rh, lh, **kw)

    def roles(self, path):
        mid = render_score(read_score(path), Compat())
        return staff_roles(mid), _legacy_roles(mid), mid

    def test_a_voice_with_chord_symbols_above_a_piano_not_called_piano(self):
        # Love Has No Pride: a soprano whose chord symbols play on a program-0 channel, above "Фортепиано"
        p = self.score(piano="Фортепиано",
                       before=[("Сопрано", 52, [SOPRANO], {"id": "voice.soprano", "channels": [("harmony", 0)]})])
        new, legacy, mid = self.roles(p)
        self.assertEqual(legacy.piano, [(0, P), (1, P)])            # the soprano, the piano's top staff; no bass
        self.assertTrue(any(m.type == "note_on" and m.velocity and m.note < 72 for m in mid.tracks[0]))  # chords
        self.assertEqual(new, StaffRoles(piano=[(1, P), (2, P)], orchestra=[], vocal=[(0, 52)], hands={1: 0, 2: 1}))
        c = convert_mscz(p, parts=True)
        pv = json.loads(c.data)
        self.assertEqual(pv_keys(pv, "right"), [(0, 72), (2 * Q, 67), (W, 74)])
        self.assertEqual(pv_keys(pv, "left"), [(0, 48), (W, 43)])
        # Note Waterfall's orchestra: the sung line, not the chord symbols written on its staff
        self.assertEqual(c.parts["orchestra"], "instruments")
        orch = c.parts["parts"]["orchestra"]
        self.assertEqual(keys(orch["right"], "staff", "program", "vocal", "merged"),
                         [(0, 76, 0, 52, 1, None), (2 * Q, 77, 0, 52, 1, None), (W, 79, 0, 52, 1, None)])
        self.assertEqual(orch["left"], [])
        self.assertEqual(keys(c.parts["parts"]["original"]["left"]), [(0, 48), (W, 43)])

    def test_both_staves_of_a_piano_not_called_piano_are_the_hands(self):
        # A Natural Woman: "Klavier"; the program change is only on the part's top staff
        p = self.score(piano="Klavier")
        new, legacy, _ = self.roles(p)
        self.assertEqual(legacy.piano, [(0, P)])
        self.assertEqual(new.piano, [(0, P), (1, P)])
        self.assertEqual(new.hands, {0: 0, 1: 1})
        pv = json.loads(convert_mscz(p).data)
        self.assertEqual(pv_keys(pv, "left"), [(0, 48), (W, 43)])

    def test_percussion_is_neither_piano_nor_orchestra(self):
        # Shape of You: a tambourine (a drumset with program 0) and a bass drum with an orchestra program
        tamb = [[[chord("quarter", 54), chord("quarter", 54), chord("half", 54)]], [[chord("whole", 54)]]]
        drum = [[[chord("whole", 35)]], [[chord("whole", 35)]]]
        p = self.score(parts=[("Tambourine", 0, [tamb], {"id": "drum.tambourine", "drums": True}),
                              ("Bass Drum", 48, [drum], {"id": "drum.bass-drum", "drums": True})])
        new, legacy, _ = self.roles(p)
        self.assertEqual((legacy.piano, legacy.orchestra), ([(0, P), (1, P), (2, P)], [(3, 48)]))
        self.assertEqual(new, StaffRoles(piano=[(0, P), (1, P)], orchestra=[], vocal=[], hands={0: 0, 1: 1}))
        c = convert_mscz(p, parts=True)
        pv = json.loads(c.data)
        self.assertEqual(pv_keys(pv, "left"), [(0, 48), (W, 43)])
        self.assertEqual(c.parts["orchestra"], "none")

    def test_a_sung_one_staff_piano_part_above_the_piano_is_a_vocal_line(self):
        # Doctor My Eyes: the solo is written for a piano instrument
        solo = [[[sung("half", 64), sung("half", 65)]], [[sung("whole", 67)]]]
        p = self.score(before=[("Piano", 0, [solo])])
        new, _, _ = self.roles(p)
        self.assertEqual(new, StaffRoles(piano=[(1, P), (2, P)], orchestra=[], vocal=[(0, 0)], hands={1: 0, 2: 1}))
        c = convert_mscz(p, parts=True)
        self.assertEqual(pv_keys(json.loads(c.data), "right"), [(0, 72), (2 * Q, 67), (W, 74)])
        self.assertEqual(keys(c.parts["parts"]["orchestra"]["right"], "vocal", "program"),
                         [(0, 64, 1, 0), (2 * Q, 65, 1, 0), (W, 67, 1, 0)])
        # without lyrics it is a piano staff, as always
        plain = [[[chord("half", 64), chord("half", 65)]], [[chord("whole", 67)]]]
        new, legacy, _ = self.roles(self.score(before=[("Piano", 0, [plain])]))
        self.assertEqual(new, legacy)

    def test_a_choir_on_piano_staves_is_reduced_part_by_part(self):
        # Doctor My Eyes' backing: soprano/alto and tenor/bass on two grand staves, all sung
        s = [[[sung("whole", 72)]], [[sung("whole", 74)]]]
        a = [[[sung("whole", 67)]], [[sung("whole", 65)]]]
        t = [[[sung("whole", 60)]], [[sung("whole", 59)]]]
        b = [[[sung("whole", 48)]], [[sung("whole", 43)]]]
        solo = [[[sung("whole", 64)]], [[sung("whole", 62)]]]
        p = self.score(rh=s, lh=a, before=[("Piano", 0, [solo])], parts=[("Piano", 0, [t, b])])
        new, legacy, _ = self.roles(p)
        self.assertEqual(legacy.hands, {0: 0, 1: 1, 2: 1, 3: 1, 4: 1})
        self.assertEqual(new.vocal, [(0, 0)])
        self.assertEqual(new.hands, {1: 0, 2: 0, 3: 1, 4: 1})
        pv = json.loads(convert_mscz(p).data)
        self.assertEqual(pv_keys(pv, "right"), [(0, 67), (0, 72), (W, 65), (W, 74)])
        self.assertEqual(pv_keys(pv, "left"), [(0, 48), (0, 60), (W, 43), (W, 59)])

    def test_two_piano_parts_without_lyrics_keep_the_legacy_hands(self):
        # Schubert's Serenade, Vocalise: the first staff the right hand, the others the left
        p = self.score(parts=[("Piano", 0, [RH, LH])])
        new, legacy, _ = self.roles(p)
        self.assertEqual(new, legacy)
        self.assertEqual(new.hands, {0: 0, 1: 1, 2: 1, 3: 1})

    def test_build_options_name_the_piano_staves(self):
        # Paganini Var. 18 (scores.json piano_staves): the named staves are the piano, right hand first; the others
        # are orchestra by their program, percussion not
        vln = [[[chord("whole", 79)]], [[chord("whole", 77)]]]
        rh2 = [[[chord("whole", 84)]], [[chord("whole", 86)]]]
        lh2 = [[[chord("whole", 36)]], [[chord("whole", 38)]]]
        drum = [[[chord("whole", 35)]], [[chord("whole", 35)]]]
        p = self.score(parts=[("Violin", 40, [vln]), ("Piano", 0, [rh2, lh2]),
                              ("Bass Drum", 48, [drum], {"id": "drum.bass-drum", "drums": True})])
        options = {"piano_staves": [4, 5]}
        mid = render_score(read_score(p), Compat())
        apply_piano_options(mid, options)
        new, legacy = staff_roles(mid), _legacy_roles(mid)
        self.assertEqual((legacy.piano, legacy.orchestra), ([(3, P), (4, P)], [(0, 0), (2, 40), (5, 48)]))
        self.assertEqual(new, StaffRoles(piano=[(3, P), (4, P)], orchestra=[(0, 0), (2, 40)], vocal=[],
                                         hands={3: 0, 4: 1}))
        pv = json.loads(convert_mscz(p, options=options).data)
        self.assertEqual([k for k in pv_keys(pv, "right") if k[1] > 80], [(0, 84), (W, 86)])
        self.assertEqual([k for k in pv_keys(pv, "left") if k[1] < 40], [(0, 36), (W, 38)])

    def test_a_mid_file_keeps_the_legacy_rules(self):
        p = self.score(piano="Фортепиано",
                       before=[("Сопрано", 52, [SOPRANO], {"id": "voice.soprano", "channels": [("harmony", 0)]})])
        mid = render_score(read_score(p), Compat())
        mid.staves = []                                  # what a .mid read from disk has
        self.assertEqual(staff_roles(mid), _legacy_roles(mid))


class StavesRevisionTest(unittest.TestCase):
    """A library built under the legacy classification: the next build renders every score again and replaces
    the outputs of the scores whose staves are classified anew; the others stay byte for byte."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scores = os.path.join(self.tmp, "Scores")
        self.out = os.path.join(self.tmp, "PianoVision")
        os.makedirs(self.scores)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def lib(self):
        return Library(Config(scores=self.scores, output=self.out, jobs=1), log=lambda *_: None)

    def files(self):
        out = {}
        for d, _s, fs in os.walk(self.out):
            if ".attic" in d:
                continue
            for f in fs:
                if not f.startswith("."):
                    with open(os.path.join(d, f), "rb") as fh:
                        out[os.path.relpath(os.path.join(d, f), self.out)] = fh.read()
        return out

    def test_a_build_replaces_only_the_outputs_of_reclassified_staves(self):
        make_piano_score(os.path.join(self.scores, "plain.mscz"), RH, LH, title="Plain")
        make_piano_score(os.path.join(self.scores, "klavier.mscz"), RH, LH, title="Klavier", piano="Klavier")
        with mock.patch.object(pvjson, "staff_roles", pvjson._legacy_roles):
            self.lib().build()
        lib = self.lib()
        for e in lib.manifest.entries.values():          # made before the revision
            e.pop("staves")
        lib.manifest.entries["klavier.mscz"]["compat"] = {"dynamics": "4.5"}
        lib.manifest.save()
        before = self.files()
        mtime = os.path.getmtime(os.path.join(self.out, "test_Plain.json"))

        lib = self.lib()
        self.assertEqual([a.kind for a in lib.build(dry_run=True).planned], ["staves", "staves"])
        rep = lib.build()
        self.assertEqual([(r, why) for r, _o, why in rep.written], [("klavier.mscz", "staves classified anew")])
        self.assertEqual(rep.kept, 1)
        after = self.files()
        changed = sorted(f for f in after if after[f] != before.get(f))
        self.assertEqual(changed, ["NoteWaterfall/test_Klavier.parts.json", "test_Klavier.json"])
        self.assertEqual(os.path.getmtime(os.path.join(self.out, "test_Plain.json")), mtime)
        self.assertEqual(pv_keys(json.loads(after["test_Klavier.json"]), "left"), [(0, 48), (W, 43)])
        entries = self.lib().manifest.entries
        self.assertEqual({e["staves"] for e in entries.values()}, {STAVES_REVISION})
        self.assertEqual(entries["klavier.mscz"]["compat"], {"dynamics": "4.5"})     # rendered with its knobs
        rep = self.lib().build()
        self.assertEqual((rep.kept, rep.written, rep.parts), (2, [], []))


if __name__ == "__main__":
    unittest.main()
