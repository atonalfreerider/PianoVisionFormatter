"""Note Waterfall's parts file: the original piano part, the simplified part and the orchestra, apart."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from pianovision.convert import content_hash, convert_mscz
from pianovision.parts import PARTS_FORMAT, parts_bytes, parts_name, parts_rel

from tests.scoregen import accent, chord, make_piano_score, rest

Q = 480
W = 4 * Q          # a 4/4 measure


def keys(hand_notes, *extra):
    return [(n["ticksStart"], n["note"]) + tuple(n.get(k) for k in extra) for n in hand_notes]


def pv_keys(doc, hand):
    return sorted((n["ticksStart"], n["note"]) for m in doc["tracksV2"][hand] for n in m["notes"])


class PartsTest(unittest.TestCase):
    RH = [[[chord("quarter", 72), chord("quarter", 74), chord("quarter", 76), chord("quarter", 77)]],
          [[chord("whole", 79)]], [[rest("whole")]]]
    LH = [[[chord("half", 48), chord("half", 43)]], [[chord("whole", 36)]], [[rest("whole")]]]
    SIMPLE = ("Piano-simplified", 0, [[[[chord("whole", 72)]], [[rest("whole")]], [[rest("whole")]]],
                                      [[[rest("whole")]], [[rest("whole")]], [[rest("whole")]]]])
    ORCH = ("Piano-orchestral", 0, [[[[rest("whole")]], [[rest("whole")]], [[chord("whole", 84)]]],
                                    [[[rest("whole")]], [[rest("whole")]], [[chord("whole", 40)]]]])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def score(self, rh=None, lh=None, parts=()):
        path = os.path.join(self.tmp, "Test", "song.mscz")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return make_piano_score(path, rh or self.RH, lh or self.LH, parts=parts)

    def test_the_pianovision_json_is_the_same_with_parts(self):
        p = self.score(parts=(self.SIMPLE, self.ORCH))
        self.assertEqual(convert_mscz(p, parts=True).data, convert_mscz(p).data)
        self.assertEqual(convert_mscz(p, parts=True, provenance=True).data, convert_mscz(p).data)

    def test_original_simplified_and_orchestra_are_kept_apart(self):
        c = convert_mscz(self.score(parts=(self.SIMPLE, self.ORCH)), parts=True)
        doc = c.parts
        self.assertEqual((doc["format"], doc["simplified"], doc["orchestra"]), (PARTS_FORMAT, True, "piano-orchestral"))
        P = doc["parts"]
        self.assertEqual(keys(P["original"]["right"]), [(0, 72), (Q, 74), (2 * Q, 76), (3 * Q, 77), (W, 79)])
        self.assertEqual(keys(P["original"]["left"]), [(0, 48), (2 * Q, 43), (W, 36)])
        # measure 1 from the simplified staff (marked), measure 2 from the piano part
        self.assertEqual(keys(P["simplified"]["right"], "simplified"), [(0, 72, 1), (W, 79, None)])
        self.assertEqual(keys(P["simplified"]["left"], "simplified"), keys(P["original"]["left"], "simplified"))
        # the orchestra, on its own staves (3 = the fifth staff: piano 0-1, simplified 2-3, orchestral 4-5)
        self.assertEqual(keys(P["orchestra"]["right"], "staff", "merged", "program"), [(2 * W, 84, 4, 1, None)])
        self.assertEqual(keys(P["orchestra"]["left"], "staff", "merged", "program"), [(2 * W, 40, 5, 1, None)])
        # PianoVision: the simplified part plus the orchestra where the piano rests
        pv = json.loads(c.data)
        self.assertEqual(pv_keys(pv, "right"), [(0, 72), (W, 79), (2 * W, 84)])
        self.assertEqual(pv_keys(pv, "left"), [(0, 48), (2 * Q, 43), (W, 36), (2 * W, 40)])

    def test_notes_carry_the_fields_pianovision_gives_them(self):
        c = convert_mscz(self.score(parts=(self.SIMPLE,)), parts=True)
        pv = {(n["ticksStart"], n["note"]): n for m in json.loads(c.data)["tracksV2"]["right"] for n in m["notes"]}
        for n in c.parts["parts"]["simplified"]["right"]:
            want = pv[(n["ticksStart"], n["note"])]
            for k in ("durationTicks", "start", "duration", "velocity", "measureInd", "accent"):
                self.assertEqual(n[k], want[k], k)

    def test_without_simplified_staves_there_is_no_simplified_part(self):
        c = convert_mscz(self.score(), parts=True)
        self.assertEqual((c.parts["simplified"], c.parts["orchestra"]), (False, "none"))
        self.assertNotIn("simplified", c.parts["parts"])
        self.assertEqual(c.parts["parts"]["orchestra"], {"right": [], "left": []})

    def test_orchestra_instruments_are_split_into_the_hands_by_pitch(self):
        vln = ("Violin", 40, [[[[chord("half", 67), chord("half", 55)]], [[chord("whole", 69)]],
                              [[chord("whole", 57)]]]])
        c = convert_mscz(self.score(parts=(vln,)), parts=True)
        P = c.parts["parts"]
        self.assertEqual(c.parts["orchestra"], "instruments")
        self.assertEqual(keys(P["orchestra"]["right"], "program", "staff", "merged"),
                         [(0, 67, 40, 2, None), (W, 69, 40, 2, None)])
        # measure 3: the piano rests, so PianoVision merges the violin into the left hand there
        self.assertEqual(keys(P["orchestra"]["left"], "program", "staff", "merged"),
                         [(2 * Q, 55, 40, 2, None), (2 * W, 57, 40, 2, 1)])

    def test_accents_are_matched_per_part_once(self):
        rh = [[[chord("quarter", 72, extra=[accent()]), chord("quarter", 74), chord("half", 76)]],
              [[chord("whole", 79)]], [[rest("whole")]]]
        c = convert_mscz(self.score(rh=rh, parts=(self.SIMPLE,)), parts=True, provenance=True)
        orig = c.parts["parts"]["original"]["right"][0]
        raw = c.parts_provenance["original"]["right"][0].velocity / 127.0     # the note_on velocity
        self.assertEqual((orig["note"], orig["accent"]), (72, 1))
        self.assertLess(raw, 0.95)
        self.assertEqual(orig["velocity"], min(1.0, raw * 1.2))               # boosted once, not per part
        self.assertEqual(c.parts["parts"]["original"]["right"][1]["accent"], 0)

    def test_parts_file_names_the_song_it_goes_with(self):
        p = self.score(parts=(self.SIMPLE,))
        c = convert_mscz(p, parts=True)
        data = parts_bytes(c.parts, "test_Hands.json", c.data, content_hash(p))
        doc = json.loads(data)
        self.assertEqual(list(doc)[:4], ["format", "song", "songMd5", "scoreContent"])
        self.assertEqual((doc["song"], doc["songMd5"], doc["scoreContent"]),
                         ("test_Hands.json", hashlib.md5(c.data).hexdigest(), content_hash(p)))
        self.assertEqual(doc["parts"], c.parts["parts"])
        self.assertEqual((parts_name("a/b.json"), parts_rel("b.json")), ("b.parts.json", "NoteWaterfall/b.parts.json"))

    def test_provenance_of_the_parts(self):
        c = convert_mscz(self.score(parts=(self.SIMPLE, self.ORCH)), parts=True, provenance=True)
        pp = c.parts_provenance
        for part in ("original", "simplified", "orchestra"):
            for hand in ("right", "left"):
                self.assertEqual([(s.ticks, s.midi) for s in pp[part][hand]], keys(c.parts["parts"][part][hand]))
        self.assertEqual([(s.ticks, s.midi, s.staff) for s in pp["simplified_staff"]["right"]], [(0, 72, 2)])
        self.assertEqual(pp["simplified_staff"]["left"], [])
        self.assertEqual({s.staff for s in pp["original"]["right"]}, {0})


if __name__ == "__main__":
    unittest.main()
