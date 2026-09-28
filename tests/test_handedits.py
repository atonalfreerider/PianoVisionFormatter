"""Hand edits recorded on the headset -> the MuseScore scores (pianovision.handedits).

Synthetic two-staff scores (tests/scoregen.make_piano_score) in throwaway folders; a real
score from the library is only ever read, and copied first."""

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from pianovision import handedits as H
from pianovision.convert import convert_midi, convert_mscz
from pianovision.library import Config, Library, load_config

from tests.scoregen import accent, chord, dynamic, end_tuplet, loc, make_piano_score, note, rest, tuplet

Q = 480                     # ticks per quarter
SCALE = [72, 74, 76, 77]


def mscx_of(mscz: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(mscz)) as z:
        return z.read("score.mscx").decode()


def notes(data: bytes, hand: str):
    return sorted((n["ticksStart"], n["note"]) for n in H._flat(json.loads(data), hand))


def edit(frm, ticks, midi, occ=0, **kw):
    d = {"from": frm, "to": "left" if frm == "right" else "right", "ticksStart": ticks, "midi": midi,
         "occurrence": occ, "votes": 2, "against": 0}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scores = os.path.join(self.tmp, "Scores")
        self.out = os.path.join(self.tmp, "PianoVision")
        os.makedirs(self.scores)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def lib(self) -> Library:
        return Library(Config(scores=self.scores, output=self.out, jobs=1), log=lambda *_: None)

    def song(self, rh, lh, rel="Test/hands.mscz", **kw):
        path = os.path.join(self.scores, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        make_piano_score(path, rh, lh, **kw)
        lib = self.lib()
        rep = lib.build()
        self.assertEqual(rep.failed, [])
        self.rel, self.path = rel, path
        self.name = lib.manifest.entries[rel]["output"]
        return lib

    def library_song(self, lib) -> dict:
        with open(lib.out_path(self.name), "rb") as f:
            return json.loads(f.read())

    def sidecar(self, lib, edits, md5=None, name=None):
        with open(lib.out_path(self.name), "rb") as f:
            data = f.read()
        doc = {"format": 2 if any("part" in e for e in edits) else 1, "song": self.name,
               "songMd5": md5 or H._md5(data), "updated": "2026-09-27T21:04:05Z", "edits": edits}
        path = os.path.join(self.tmp, name or H.sidecar_name(self.name))
        with open(path, "w") as f:
            json.dump(doc, f)
        return path

    def review(self, lib, edits, **kw) -> H.ScoreReview:
        rv = H.review_song(lib, self.name, [H.read_sidecar(self.sidecar(lib, edits))], **kw)
        return rv

    def render(self, mscz: bytes) -> bytes:
        folder = os.path.join(self.tmp, "check", os.path.basename(os.path.dirname(self.path)))
        os.makedirs(folder, exist_ok=True)
        p = os.path.join(folder, os.path.basename(self.path))
        with open(p, "wb") as f:
            f.write(mscz)
        return convert_mscz(p).data

    def assertVerified(self, rv, right, left):
        self.assertEqual(rv.error, "")
        self.assertTrue(rv.ready, H.format_review(rv))
        self.assertEqual(self.render(rv.new_mscz), rv.new_json)
        self.assertEqual(notes(rv.new_json, "right"), sorted(right))
        self.assertEqual(notes(rv.new_json, "left"), sorted(left))


# ------------------------------------------------------------------------------

class SidecarTest(unittest.TestCase):
    def test_reads_format_1(self):
        sc = H.parse_sidecar(json.dumps({
            "format": 1, "song": "a.json", "songMd5": "ABC", "updated": "t",
            "edits": [edit("left", 960, 64, start=2.0, measureInd=0), edit("right", 0, 72)]}).encode(), "a.hands.json")
        self.assertEqual((sc.error, sc.song, sc.song_md5), ("", "a.json", "abc"))
        self.assertEqual([(e.from_hand, e.to_hand, e.ticks, e.midi) for e in sc.edits],
                         [("right", "left", 0, 72), ("left", "right", 960, 64)])
        self.assertEqual((sc.edits[1].start, sc.edits[1].measure, sc.edits[1].votes), (2.0, 0, 2))

    def test_newer_format_is_not_read(self):
        sc = H.parse_sidecar(b'{"format": 3, "edits": [{"from": "left"}]}', "a.hands.json")
        self.assertIn("newer", sc.error)
        self.assertEqual(sc.edits, [])

    def test_bad_edits_are_skipped_reverts_ignored_and_the_later_duplicate_wins(self):
        sc = H.parse_sidecar(json.dumps({"format": 1, "edits": [
            {"from": "left", "to": "right", "ticksStart": "x", "midi": 60},
            {"from": "left", "to": "left", "ticksStart": 0, "midi": 60},
            dict(edit("left", 0, 62), votes=1), dict(edit("left", 0, 62), votes=5)]}).encode("utf-8-sig"),
            "song.hands.json")
        self.assertEqual(sc.song, "song.json")
        self.assertEqual([(e.midi, e.votes) for e in sc.edits], [(62, 5)])
        self.assertEqual(len(sc.warnings), 3)

    def test_fields_the_app_leaves_unknown_read_as_the_app_reads_them(self):
        # The app writes measureInd -1 for a note without one and reads a missing/null occurrence as 0.
        sc = H.parse_sidecar(json.dumps({"format": 1, "edits": [
            dict(edit("left", 0, 60), measureInd=-1, occurrence=None), {"from": "left", "to": "right", "ticksStart": 0,
                                                                        "midi": 62}]}).encode(), "a.hands.json")
        self.assertEqual(sc.warnings, [])
        self.assertEqual([(e.midi, e.occurrence, e.measure) for e in sc.edits], [(60, 0, None), (62, 0, None)])

    def test_garbage_is_an_error_not_an_exception(self):
        self.assertTrue(H.parse_sidecar(b"\xff\x00", "x.hands.json").error)
        self.assertTrue(H.parse_sidecar(b"[1, 2]", "x.hands.json").error)

    def test_names(self):
        self.assertEqual(H.sidecar_name("chop_Nocturne.json"), "chop_Nocturne.hands.json")
        self.assertEqual(H.song_of_sidecar("chop_Nocturne.hands.json"), "chop_Nocturne.json")


class ProvenanceTest(Base):
    def test_every_note_knows_its_source_and_the_json_does_not_change(self):
        self.song(rh=[[[chord("quarter", 72), chord("quarter", 74, 79), chord("half", 76)]]],
                  lh=[[[chord("half", 48, 55), chord("half", 43)], [loc("1/4"), chord("quarter", 64)]]])
        plain = convert_mscz(self.path)
        c = convert_mscz(self.path, provenance=True)
        self.assertEqual(c.data, plain.data)
        doc = json.loads(c.data)
        for hand, staff in (("right", 0), ("left", 1)):
            flat = H._flat(doc, hand)
            self.assertEqual(len(flat), len(c.provenance[hand]))
            for n, src in zip(flat, c.provenance[hand]):
                self.assertEqual((src.ticks, src.midi), (n["ticksStart"], n["note"]))
                self.assertEqual((src.staff, src.note.pitch), (staff, n["note"]))
        voice2 = [s for s in c.provenance["left"] if s.voice == 1]
        self.assertEqual([(s.midi, s.measure, s.chord, s.element) for s in voice2], [(64, 0, 1, 1)])

    def test_a_repeat_plays_one_source_note_twice(self):
        self.song(rh=[[[chord("quarter", p) for p in SCALE]], [[chord("whole", 79)]]],
                  lh=[[[chord("whole", 48)]], [[chord("whole", 43)]]], repeat=(0, 0))
        c = convert_mscz(self.path, provenance=True)
        firsts = [s for s in c.provenance["left"] if s.midi == 48]
        self.assertEqual([s.ticks for s in firsts], [0, 4 * Q])
        self.assertIs(firsts[0].note, firsts[1].note)

    def test_real_score_copy(self):
        cfg = load_config()
        src = os.path.join(cfg.scores or "", "Chopin", "Prelude_in_A_Major_Opus_28_No._7.mscz")
        if not os.path.exists(src):
            self.skipTest("the score library is not on this machine")
        path = os.path.join(self.tmp, "Chopin", os.path.basename(src))
        os.makedirs(os.path.dirname(path))
        shutil.copy(src, path)                    # never read-modify the library itself
        c = convert_mscz(path, provenance=True, keep_midi=True)
        self.assertEqual(c.data, convert_mscz(path).data)
        hands = H._hands_of(c.midi, True)
        doc = json.loads(c.data)
        for hand in H.HANDS:
            for n, s in zip(H._flat(doc, hand), c.provenance[hand]):
                self.assertIsNotNone(s.note)
                self.assertEqual(H.HANDS[hands[s.note.chord.track // 4]], hand)
                self.assertEqual(s.note.pitch, n["note"])


class MoveTest(Base):
    RH = [[[chord("quarter", p) for p in SCALE]]]
    LH = [[[chord("half", 48, 55), chord("quarter", 48, 55, 64), rest("quarter")]]]
    RIGHT = [(i * Q, p) for i, p in enumerate(SCALE)]
    LEFT = [(0, 48), (0, 55), (2 * Q, 48), (2 * Q, 55), (2 * Q, 64)]

    def test_whole_chord_moves_to_a_new_voice_drawn_where_it_was(self):
        lib = self.song(self.RH, self.LH)
        rv = self.review(lib, [edit("left", 2 * Q, p) for p in (48, 55, 64)])
        self.assertVerified(rv, self.RIGHT + [(2 * Q, 48), (2 * Q, 55), (2 * Q, 64)], [(0, 48), (0, 55)])
        self.assertEqual((rv.moved_notes, rv.to_right, rv.to_left), (3, 3, 0))
        xml = mscx_of(rv.new_mscz)
        rh = xml[xml.index('<Staff id="1">\n      <VBox>'):xml.index('<Staff id="2">\n      <Measure>')]
        self.assertEqual(rh.count("<voice>"), 2)
        self.assertRegex(rh, r"<voice>\n +<location>\n +<fractions>1/2</fractions>\n +</location>\n +<Chord>\n"
                             r" +<staffMove>1</staffMove>\n +<durationType>quarter</durationType>")
        lh = xml[xml.index('<Staff id="2">\n      <Measure>'):]
        self.assertRegex(lh, r"<Rest>\n +<visible>0</visible>\n +<durationType>quarter</durationType>\n +</Rest>\n"
                             r" +<Rest>\n +<durationType>quarter</durationType>")
        self.assertEqual(xml.count("<pitch>64</pitch>"), 1)
        self.assertEqual([r.status for r in rv.results], ["move"] * 3)

    def test_split_chord_moves_only_the_edited_note(self):
        lib = self.song(self.RH, self.LH)
        rv = self.review(lib, [edit("left", 2 * Q, 64)])
        self.assertVerified(rv, self.RIGHT + [(2 * Q, 64)], [(0, 48), (0, 55), (2 * Q, 48), (2 * Q, 55)])
        xml = mscx_of(rv.new_mscz)
        new_chord = re.search(r"<Chord>\n +<staffMove>1</staffMove>\n +<durationType>quarter</durationType>\n"
                              r" +<Note>\n +<pitch>64</pitch>", xml)
        self.assertIsNotNone(new_chord)
        self.assertIn("<pitch>48</pitch>\n              <tpc>14</tpc>\n              </Note>\n            <Note>\n"
                      "              <pitch>55</pitch>\n              <tpc>15</tpc>\n              </Note>\n"
                      "            </Chord>\n          <Rest>", xml)

    def test_rest_of_the_file_is_untouched(self):
        lib = self.song(self.RH, self.LH)
        with zipfile.ZipFile(self.path) as z:
            before = z.read("score.mscx").decode()
            others = {n: z.read(n) for n in z.namelist() if n != "score.mscx"}
        rv = self.review(lib, [edit("left", 2 * Q, 64)])
        after = mscx_of(rv.new_mscz)
        removed = [ln for ln in before.splitlines() if ln not in after.splitlines()]
        self.assertEqual(removed, [])                      # only additions around the moved note
        self.assertEqual(before.count("\n") + 13 - 4, after.count("\n"))     # a new voice; the note's 4 lines move
        with zipfile.ZipFile(io.BytesIO(rv.new_mscz)) as z:
            self.assertEqual({n: z.read(n) for n in z.namelist() if n != "score.mscx"}, others)

    def test_a_chord_drawn_on_the_other_staff_loses_its_staff_move(self):
        rh = [[[chord("quarter", 72), chord("quarter", 50, staff_move=1), chord("half", 76)]]]
        lh = [[[chord("half", 43), rest("half")]]]
        lib = self.song(rh, lh)
        rv = self.review(lib, [edit("right", Q, 50)])
        self.assertVerified(rv, [(0, 72), (2 * Q, 76)], [(0, 43), (Q, 50)])
        xml = mscx_of(rv.new_mscz)
        self.assertNotIn("staffMove", xml)

    def test_tied_note_moves_with_its_continuation(self):
        lh = [[[chord("half", 48), chord("half", note(60, tie_next=(1, "-1/2")))]],
              [[chord("half", note(60, tie_prev=(-1, "1/2"))), chord("half", 48)]]]
        rh = [[[chord("whole", 72)]], [[chord("whole", 74)]]]
        lib = self.song(rh, lh)
        before = self.library_song(lib)
        tied = [n for n in H._flat(before, "left") if n["note"] == 60][0]
        self.assertEqual(tied["durationTicks"], 4 * Q - 1)
        rv = self.review(lib, [edit("left", 2 * Q, 60)])
        self.assertVerified(rv, [(0, 72), (4 * Q, 74), (2 * Q, 60)], [(0, 48), (6 * Q, 48)])
        moved = [n for n in H._flat(json.loads(rv.new_json), "right") if n["note"] == 60][0]
        self.assertEqual(moved["durationTicks"], 4 * Q - 1)
        self.assertEqual(len(rv.carried), 1)
        self.assertIn("tied to a moved note", rv.carried[0])
        xml = mscx_of(rv.new_mscz)
        rh_part = xml[:xml.index('<Staff id="2">\n      <Measure>')]
        self.assertEqual(rh_part.count('<Spanner type="Tie">'), 2)

    def test_tuplet_member_moves_into_a_rebuilt_tuplet(self):
        rh = [[[tuplet(), chord("eighth", 72), chord("eighth", 74), chord("eighth", 76), end_tuplet(),
                chord("quarter", 77), chord("half", 79)]]]
        lh = [[[chord("whole", 48)]]]
        lib = self.song(rh, lh)
        start = [n["ticksStart"] for n in H._flat(self.library_song(lib), "right")]
        rv = self.review(lib, [edit("right", start[1], 74)])
        self.assertVerified(rv, [(0, 72), (start[2], 76), (Q, 77), (2 * Q, 79)], [(0, 48), (start[1], 74)])
        lh_xml = mscx_of(rv.new_mscz).split('<Staff id="2">\n      <Measure>')[1]
        self.assertRegex(lh_xml, r"<Tuplet>(.|\n)*<Rest>\n +<visible>0</visible>(.|\n)*<staffMove>-1</staffMove>"
                                 r"(.|\n)*<Rest>\n +<visible>0</visible>(.|\n)*<endTuplet/>")

    def test_grace_notes_move_only_with_their_chord(self):
        rh = [[[chord("eighth", 79, grace="acciaccatura"), chord("quarter", 72), chord("quarter", 74),
                chord("half", 76)]]]
        lh = [[[chord("whole", 48)]]]
        lib = self.song(rh, lh)
        grace = [n for n in H._flat(self.library_song(lib), "right") if n["note"] == 79][0]
        rv = self.review(lib, [edit("right", grace["ticksStart"], 79)])
        self.assertFalse(rv.ready)
        self.assertIn("grace notes move only with their whole chord", rv.results[0].reason)
        rv = self.review(lib, [edit("right", 0, 72)])
        self.assertTrue(rv.ready, H.format_review(rv))
        self.assertIn((grace["ticksStart"], 79), notes(rv.new_json, "left"))
        self.assertIn("grace note of a moved chord", rv.carried[0])

    def test_no_free_voice_is_reported(self):
        lh = [[[chord("whole", 36)], [chord("whole", 40)], [chord("whole", 43)], [chord("whole", 47)]]]
        lib = self.song(self.RH, lh)
        rv = self.review(lib, [edit("right", Q, 74)])
        self.assertFalse(rv.ready)
        self.assertEqual(rv.results[0].status, "skip")
        self.assertIn("no free voice in the lower staff", rv.results[0].reason)

    def test_velocity_is_kept_when_the_dynamics_belong_to_one_staff(self):
        rh = [[[dynamic("f", 96)] + [chord("quarter", p) for p in SCALE]]]
        lh = [[[dynamic("p", 49), chord("half", 48), chord("half", 43)]]]
        lib = self.song(rh, lh)
        rv = self.review(lib, [edit("left", 2 * Q, 43)])
        self.assertVerified(rv, self.RIGHT + [(2 * Q, 43)], [(0, 48)])
        self.assertTrue(rv.results[0].velocity_pinned)
        self.assertIn("<pitch>43</pitch>\n              <tpc>15</tpc>\n              <velocity>49</velocity>",
                      mscx_of(rv.new_mscz))

    def test_a_verification_failure_is_skipped_not_written(self):
        # PianoVision accents match whole chords per hand: moving G4 next to the accented right-hand
        # chord would drop its accent, so the change is refused
        rh = [[[chord("quarter", 72, 76, extra=[accent()]), chord("quarter", 74), chord("half", 76)]]]
        lh = [[[chord("quarter", 48, 67), chord("quarter", 50), chord("half", 43)]]]
        lib = self.song(rh, lh)
        rv = self.review(lib, [edit("left", 0, 67), edit("left", Q, 50)])
        self.assertEqual([r.status for r in rv.results], ["skip", "move"])
        self.assertIn("accent 1 -> 0", rv.results[0].reason)
        self.assertVerified(rv, [(0, 72), (0, 76), (Q, 74), (Q, 50), (2 * Q, 76)], [(0, 48), (0, 67), (2 * Q, 43)])


class RepeatAndIdentityTest(Base):
    RH = [[[chord("quarter", p) for p in SCALE]], [[chord("whole", 79)]]]
    LH = [[[chord("half", 48), chord("half", 55)]], [[chord("whole", 43)]]]

    def test_a_repeat_recorded_on_one_pass_is_a_conflict(self):
        lib = self.song(self.RH, self.LH, repeat=(0, 0))
        rv = self.review(lib, [edit("left", 2 * Q, 55)])
        self.assertEqual(rv.results[0].status, "conflict")
        self.assertIn("recorded on 1 of 2 passes", rv.results[0].reason)
        self.assertIsNone(rv.new_mscz)
        rv = self.review(lib, [edit("left", 2 * Q, 55), edit("left", 6 * Q, 55)])
        self.assertTrue(rv.ready, H.format_review(rv))
        self.assertEqual([r.passes for r in rv.results], [2, 2])
        self.assertIn((6 * Q, 55), notes(rv.new_json, "right"))
        rv = self.review(lib, [edit("left", 2 * Q, 55)], all_repeats=True)
        self.assertTrue(rv.ready)
        self.assertEqual(rv.moved_notes, 2)

    def test_already_applied_and_stale_edits(self):
        lib = self.song(self.RH, self.LH)
        rv = self.review(lib, [edit("right", 0, 48), edit("left", 0, 61)])
        self.assertEqual([(r.status, r.reason) for r in rv.results],
                         [("already", "already in the score"), ("stale", "the note is not in the song any more")])
        # recorded on another version of the song: the note must still be where it was
        path = self.sidecar(lib, [edit("left", 0, 48, start=9.0), edit("left", 2 * Q, 55, start=1.0)], md5="0" * 32)
        rv = H.review_song(lib, self.name, [H.read_sidecar(path)])
        self.assertEqual([r.status for r in rv.results], ["stale", "move"])
        # a note the app knew no measure for (measureInd -1) is not stale for that
        start = next(n["start"] for n in H._flat(self.library_song(lib), "left") if n["ticksStart"] == 2 * Q and n["note"] == 55)
        path = self.sidecar(lib, [edit("left", 2 * Q, 55, start=start, measureInd=-1)], md5="0" * 32)
        self.assertEqual([r.status for r in H.review_song(lib, self.name, [H.read_sidecar(path)]).results], ["move"])

    def test_song_outside_the_library(self):
        lib = self.song(self.RH, self.LH)
        rv = H.review_song(lib, "nope.json", [H.parse_sidecar(json.dumps({"format": 1, "edits": [
            edit("left", 0, 48)]}).encode(), "nope.hands.json")])
        self.assertIn("not in the library", rv.error)


class PartsEditTest(Base):
    """Format 2: edits of the original or the simplified piano part (Note Waterfall's parts file)."""
    RH = [[[chord("quarter", p) for p in SCALE]], [[chord("whole", 79)]]]
    LH = [[[chord("half", 48), chord("half", 43)]], [[chord("whole", 36)]]]
    # the simplified right hand replaces the piano's first measure; the simplified left staff is empty
    SIMPLE_RH = ("Piano-simplified", 0, [[[[chord("whole", 72)]], [[rest("whole")]]],
                                         [[[rest("whole")]], [[rest("whole")]]]])
    # both simplified staves have notes in the first measure
    SIMPLE = ("Piano-simplified", 0, [[[[chord("half", 72), chord("half", 76)]], [[rest("whole")]]],
                                      [[[chord("whole", 48)]], [[rest("whole")]]]])

    def parts(self, rv) -> dict:
        folder = os.path.join(self.tmp, "check-parts", os.path.basename(os.path.dirname(self.path)))
        os.makedirs(folder, exist_ok=True)
        p = os.path.join(folder, os.path.basename(self.path))
        with open(p, "wb") as f:
            f.write(rv.new_mscz)
        return convert_mscz(p, parts=True).parts["parts"]

    @staticmethod
    def at(part_hand):
        return sorted((n["ticksStart"], n["note"]) for n in part_hand)

    def test_format_2_reads_the_part(self):
        sc = H.parse_sidecar(json.dumps({"format": 2, "edits": [
            edit("left", 0, 48, part="original"), edit("left", 0, 48, part="simplified"), edit("left", 0, 48),
            edit("left", 0, 50, part="orchestra")]}).encode(), "a.hands.json")
        self.assertEqual(sc.error, "")
        self.assertEqual(sorted(e.part or "" for e in sc.edits), ["", "original", "simplified"])
        self.assertEqual(len(sc.warnings), 1)                   # the orchestra is not a hand part

    def test_a_hidden_note_of_the_original_part_moves_and_stays_hidden(self):
        lib = self.song(self.RH, self.LH, parts=(self.SIMPLE,))
        before = self.library_song(lib)
        rv = self.review(lib, [edit("right", Q, 74, part="original")])
        self.assertTrue(rv.ready, H.format_review(rv))
        self.assertEqual([r.status for r in rv.results], ["move"])
        self.assertEqual((rv.moved_notes, rv.to_left), (1, 1))
        after = json.loads(rv.new_json)
        for hand in H.HANDS:                     # both hands of the first measure are simplified: the song is the same
            self.assertEqual(notes(rv.new_json, hand), notes(json.dumps(before).encode(), hand))
        P = self.parts(rv)
        self.assertEqual(self.at(P["original"]["right"]), [(0, 72), (2 * Q, 76), (3 * Q, 77), (4 * Q, 79)])
        self.assertEqual(self.at(P["original"]["left"]), [(0, 48), (Q, 74), (2 * Q, 43), (4 * Q, 36)])
        self.assertIsNotNone(after)

    def test_a_hidden_note_of_the_original_part_comes_into_view_in_the_other_hand(self):
        lib = self.song(self.RH, self.LH, parts=(self.SIMPLE_RH,))
        self.assertEqual(notes(json.dumps(self.library_song(lib)).encode(), "right"), [(0, 72), (4 * Q, 79)])
        rv = self.review(lib, [edit("right", Q, 74, part="original")])
        self.assertTrue(rv.ready, H.format_review(rv))
        # the left hand's first measure is not simplified: the song now shows the note there
        self.assertEqual(notes(rv.new_json, "right"), [(0, 72), (4 * Q, 79)])
        self.assertEqual(notes(rv.new_json, "left"), [(0, 48), (Q, 74), (2 * Q, 43), (4 * Q, 36)])
        self.assertIn((Q, 74), self.at(self.parts(rv)["original"]["left"]))

    def test_a_note_of_the_simplified_staves_moves_between_them(self):
        lib = self.song(self.RH, self.LH, parts=(self.SIMPLE,))
        rv = self.review(lib, [edit("right", 2 * Q, 76, part="simplified")])
        self.assertTrue(rv.ready, H.format_review(rv))
        self.assertEqual(notes(rv.new_json, "right"), [(0, 72), (4 * Q, 79)])
        self.assertEqual(notes(rv.new_json, "left"), [(0, 48), (2 * Q, 76), (4 * Q, 36)])
        P = self.parts(rv)
        self.assertEqual([(n["ticksStart"], n["note"]) for n in P["simplified"]["left"] if n.get("simplified")],
                         [(0, 48), (2 * Q, 76)])
        self.assertIn((2 * Q, 76), self.at(P["original"]["right"]))           # the piano's own 76 stays

    def test_a_move_that_would_change_which_staves_the_song_shows_is_refused(self):
        # the only simplified note of the right hand's first measure: moving it would bring the piano part back there
        lib = self.song(self.RH, self.LH, parts=(self.SIMPLE_RH,))
        rv = self.review(lib, [edit("right", 0, 72, part="simplified")])
        self.assertFalse(rv.ready)
        self.assertIn("the re-rendered song would change", rv.results[0].reason)

    def test_a_format_1_edit_of_a_simplified_note_is_found_in_the_song(self):
        lib = self.song(self.RH, self.LH, parts=(self.SIMPLE,))
        rv = self.review(lib, [edit("right", 2 * Q, 76)])
        self.assertTrue(rv.ready, H.format_review(rv))
        self.assertEqual(notes(rv.new_json, "left"), [(0, 48), (2 * Q, 76), (4 * Q, 36)])
        self.assertIn((2 * Q, 76), self.at(self.parts(rv)["original"]["right"]))

    def test_the_same_identity_in_two_parts_is_two_notes(self):
        lib = self.song(self.RH, self.LH, parts=(self.SIMPLE,))
        rv = self.review(lib, [edit("right", 0, 72, part="original")])
        self.assertTrue(rv.ready, H.format_review(rv))
        self.assertEqual(notes(rv.new_json, "right"), [(0, 72), (2 * Q, 76), (4 * Q, 79)])   # the simplified 72 stays
        P = self.parts(rv)
        self.assertIn((0, 72), self.at(P["original"]["left"]))
        self.assertIn((0, 72), self.at(P["simplified"]["right"]))

    def test_already_in_the_score_and_stale(self):
        lh = [[[chord("half", 48), chord("half", 43)], [rest("quarter"), chord("quarter", 74), rest("half")]],
              [[chord("whole", 36)]]]
        lib = self.song([[[chord("quarter", 72), rest("quarter"), chord("half", 76)]], [[chord("whole", 79)]]], lh,
                        parts=(self.SIMPLE,))
        rv = self.review(lib, [edit("right", Q, 74, part="original"), edit("right", 3 * Q, 75, part="original")])
        self.assertEqual([r.status for r in rv.results], ["already", "stale"])


class ApplyTest(Base):
    RH = MoveTest.RH
    LH = MoveTest.LH

    def reviewed(self):
        lib = self.song(self.RH, self.LH)
        rv = self.review(lib, [edit("left", 2 * Q, 64)])
        self.assertTrue(rv.ready)
        with open(self.path, "rb") as f:
            original = f.read()
        return lib, rv, original

    def test_nothing_is_written_without_a_yes(self):
        lib, rv, original = self.reviewed()
        with open(lib.out_path(self.name), "rb") as f:
            song = f.read()
        calls = []
        for answer in ("", "n", "no", "maybe"):
            rep = H.apply_reviews(lib, [rv], ask=lambda _q: answer, log=lambda *_: None,
                                  deploy_pianovision=calls.append, deploy_waterfall=calls.append,
                                  archive=calls.append)
            self.assertEqual((rep.written, rep.declined), ([], [self.rel]))
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), original)
        with open(lib.out_path(self.name), "rb") as f:
            self.assertEqual(f.read(), song)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.out, ".attic")))

    def test_yes_backs_up_overwrites_builds_deploys_and_archives(self):
        lib, rv, original = self.reviewed()
        calls = []
        rep = H.apply_reviews(lib, [rv], ask=lambda _q: "y", log=lambda *_: None,
                              deploy_pianovision=lambda outs: calls.append(("pv", outs)),
                              deploy_waterfall=lambda outs: calls.append(("nw", outs)),
                              archive=lambda names: calls.append(("archive", names)) or names)
        self.assertEqual((rep.written, rep.failed), ([self.rel], []))
        self.assertEqual(len(rep.backups), 1)
        self.assertRegex(rep.backups[0], r"/\.attic/\d{4}-\d\d-\d\d_\d{6}/scores/Test/hands\.mscz$")
        with open(rep.backups[0], "rb") as f:
            self.assertEqual(f.read(), original)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), rv.new_mscz)
        with open(lib.out_path(self.name), "rb") as f:
            self.assertEqual(f.read(), rv.new_json)
        self.assertEqual(calls, [("pv", [self.name]), ("nw", [self.name]),
                                 ("archive", [H.sidecar_name(self.name)])])
        self.assertEqual(rep.archived, [H.sidecar_name(self.name)])
        # the library knows the new score, and the edit now reads as already applied
        self.assertEqual(self.lib().build().written, [])
        again = self.review(self.lib(), [edit("left", 2 * Q, 64)])
        self.assertEqual(again.results[0].status, "already")

    def test_a_sidecar_the_archive_step_keeps_is_reported(self):
        lib, rv, _original = self.reviewed()
        rep = H.apply_reviews(lib, [rv], yes=True, log=lambda *_: None, archive=lambda names: [])
        self.assertEqual((rep.written, rep.archived), ([self.rel], []))
        self.assertTrue(any(H.sidecar_name(self.name) in m and "stays on the headset" in m for m in rep.messages),
                        rep.messages)

    def test_a_score_changed_since_the_review_is_left_alone(self):
        lib, rv, original = self.reviewed()
        with open(self.path, "ab") as f:
            f.write(b"\0")
        rep = H.apply_reviews(lib, [rv], yes=True, log=lambda *_: None)
        self.assertEqual(rep.written, [])
        self.assertIn("changed since the review", rep.failed[0][1])
        self.assertEqual(rep.backups, [])

    def test_partially_applied_sidecars_stay_on_the_headset(self):
        lib = self.song(self.RH, [[[chord("whole", 36)], [chord("whole", 40)], [chord("whole", 43)],
                                   [chord("whole", 47)]]])
        rv = self.review(lib, [edit("right", 0, 72), edit("left", 0, 36)])
        self.assertEqual(sorted(r.status for r in rv.results), ["move", "skip"])
        archived = []
        rep = H.apply_reviews(lib, [rv], yes=True, log=lambda *_: None, archive=lambda n: archived.extend(n) or n)
        self.assertEqual(rep.written, [self.rel])
        self.assertEqual(archived, [])
        self.assertIn("stays on the headset", rep.messages[0])


class CliTest(Base):
    def run_cli(self, *argv):
        import contextlib
        from pianovision.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--scores", self.scores, "--output", self.out] + list(argv))
        return rc, buf.getvalue()

    def test_review_reads_the_inbox_and_apply_needs_a_terminal_or_yes(self):
        lib = self.song(MoveTest.RH, MoveTest.LH)
        inbox = os.path.join(self.out, ".hands", "inbox")
        os.makedirs(inbox)
        shutil.move(self.sidecar(lib, [edit("left", 2 * Q, 64)]), inbox)
        with open(self.path, "rb") as f:
            original = f.read()
        rc, out = self.run_cli("hands", "review")
        self.assertEqual(rc, 0)
        self.assertIn("1 score(s) can be updated, 1 note(s) change hands", out)
        self.assertIn("E4", out)
        rc, out = self.run_cli("hands", "apply", "--no-deploy")      # stdin is not a terminal here
        self.assertEqual(rc, 1)
        self.assertIn("refusing to overwrite", out)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), original)
        rc, out = self.run_cli("hands", "apply", "--no-deploy", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("scores overwritten (1)", out)
        with open(lib.out_path(self.name), "rb") as f:
            self.assertIn((2 * Q, 64), notes(f.read(), "right"))


class FakeAdb:
    def __init__(self, files):
        self.files = dict(files)             # device path -> bytes
        self.serial = "FAKE1"
        self.commands = []

    def connect(self):
        return self.serial

    def md5s(self, directory):
        return {os.path.basename(p): H._md5(b) for p, b in self.files.items()
                if os.path.dirname(p) == directory.rstrip("/") and p.endswith(".json")}

    def run(self, *args, timeout=300):
        self.commands.append(args)
        if args[0] == "pull":
            if args[1] not in self.files:
                from pianovision.device import DeviceError
                raise DeviceError(f"adb: {args[1]}: No such file or directory")
            with open(args[2], "wb") as f:
                f.write(self.files[args[1]])
            return ""
        cmd = args[1]
        if cmd.startswith("cd "):
            d = cmd.split()[1].strip("'")
            return "".join(os.path.basename(p) + "\n" for p in sorted(self.files)
                           if os.path.dirname(p) == d and p.endswith(H.SIDECAR_EXT))
        if cmd.startswith("mv -f "):
            src, dst = cmd[6:].split(" ")
            self.files[dst.strip("'")] = self.files.pop(src.strip("'"))
        return ""


class DeviceTest(unittest.TestCase):
    DIR = H.DEFAULT_DEVICE_DIR

    def test_pull_copies_the_sidecars_but_not_applied_ones(self):
        adb = FakeAdb({f"{self.DIR}/a.hands.json": b"{}", f"{self.DIR}/applied/b.2026.hands.json": b"{}",
                       f"{self.DIR}/notes.txt": b""})
        inbox = tempfile.mkdtemp()
        try:
            with open(os.path.join(inbox, "old.hands.json"), "w") as f:
                f.write("{}")
            self.assertEqual(H.pull(adb, self.DIR, inbox), ["a.hands.json"])
            self.assertEqual(sorted(os.listdir(inbox)), ["a.hands.json"])
        finally:
            shutil.rmtree(inbox)

    def test_a_failed_pull_leaves_the_inbox_as_it_was(self):
        class Vanishing(FakeAdb):          # the app replaces the file while it is pulled
            def run(self, *args, timeout=300):
                if args[0] == "pull" and args[1].endswith("/b.hands.json"):
                    self.files.pop(args[1])
                return super().run(*args, timeout=timeout)

        adb = Vanishing({f"{self.DIR}/a.hands.json": b"{}", f"{self.DIR}/b.hands.json": b"{}"})
        inbox = tempfile.mkdtemp()
        try:
            with open(os.path.join(inbox, "old.hands.json"), "w") as f:
                f.write("{}")
            with self.assertRaises(Exception):
                H.pull(adb, self.DIR, inbox)
            self.assertEqual(sorted(os.listdir(inbox)), ["old.hands.json"])
        finally:
            shutil.rmtree(inbox)

    def test_only_a_sidecar_unchanged_since_the_pull_whose_song_arrived_is_archived(self):
        songs = os.path.dirname(self.DIR) + "/Songs"
        reviewed = b'{"format": 1, "edits": []}'
        adb = FakeAdb({f"{self.DIR}/a.hands.json": reviewed,         # as pulled
                       f"{self.DIR}/b.hands.json": b'{"format": 1, "edits": [{}]}',   # HAND REC saved more since
                       f"{self.DIR}/c.hands.json": reviewed,
                       f"{songs}/a.json": b"A new", f"{songs}/b.json": b"B new", f"{songs}/c.json": b"C old"})
        scs = [H.Sidecar(name=f"{x}.hands.json", song=f"{x}.json", md5=H._md5(reviewed)) for x in "abcd"]
        built = {"a.json": H._md5(b"A new"), "b.json": H._md5(b"B new"), "c.json": H._md5(b"C new"),
                 "d.json": H._md5(b"D new")}
        logs = []
        self.assertEqual(H.archivable(adb, self.DIR, songs, scs, built, log=logs.append), ["a.hands.json"])
        self.assertIn("b.hands.json stays on the headset: it changed there since it was pulled", logs[0])
        self.assertIn("c.hands.json stays on the headset: Note Waterfall does not have the rebuilt c.json", logs[1])
        self.assertIn("d.hands.json stays on the headset: it changed there", logs[2])     # gone from the headset

    def test_glob_literal(self):
        import fnmatch
        for name in ("chop_Nocturne.json", "a[1].json", "what?.json", "x*.json"):
            self.assertTrue(fnmatch.fnmatchcase(name, H.glob_literal(name)))
        self.assertFalse(fnmatch.fnmatchcase("a1.json", H.glob_literal("a[1].json")))
        self.assertFalse(fnmatch.fnmatchcase("xyz.json", H.glob_literal("x*.json")))

    def test_archive_moves_into_applied(self):
        adb = FakeAdb({f"{self.DIR}/a.hands.json": b"{}"})
        self.assertEqual(H.archive_on_device(adb, self.DIR, ["a.hands.json"], "2026-09-27_120000"), ["a.hands.json"])
        self.assertEqual(list(adb.files), [f"{self.DIR}/applied/a.2026-09-27_120000.hands.json"])
        self.assertIn(("shell", f"mkdir -p {self.DIR}/applied"), adb.commands)


@unittest.skipUnless(os.environ.get("PIANOVISION_MUSESCORE"), "set PIANOVISION_MUSESCORE=<MuseScore 4 binary>")
class MuseScoreCrossCheck(Base):
    """MuseScore itself reads the edited score and exports the same song (slow; opt-in)."""

    def test_musescore_exports_the_verified_song(self):
        lib = self.song(MoveTest.RH, MoveTest.LH)
        rv = self.review(lib, [edit("left", 2 * Q, 64), edit("right", Q, 74)])
        self.assertTrue(rv.ready)
        p = os.path.join(self.tmp, "check", "Test", "hands.mscz")
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(rv.new_mscz)
        mid = os.path.join(self.tmp, "out.mid")
        subprocess.run([os.environ["PIANOVISION_MUSESCORE"], "-o", mid, p], capture_output=True, timeout=300,
                       env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
        self.assertEqual(convert_midi(mid, p).data, rv.new_json)


if __name__ == "__main__":
    unittest.main()
