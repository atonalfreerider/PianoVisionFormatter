"""Note Waterfall's lyrics: each sung syllable on the note that sings it, in the parts file."""

import json
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

from pianovision.convert import content_hash, convert_mscz
from pianovision.lyrics import lyric_text
from pianovision.parts import parts_bytes, parts_stats

from tests.scoregen import E, chord, make_piano_score, rest

Q = 480
W = 4 * Q          # a 4/4 measure


def lyric(text, syllabic=None, no=None, ticks=None):
    kids = []
    if no:
        kids.append(E("no", text=str(no)))
    if syllabic:
        kids.append(E("syllabic", text=syllabic))
    if ticks:
        kids.append(E("ticks_f", text=ticks))
    kids.append(E("text", text=text))
    return E("Lyrics", *kids)


def sung(dur, *pitches, lyrics=()):
    """A chord with lyrics."""
    return chord(dur, *pitches, extra=tuple(lyrics))


def quarters(*pitches_and_lyrics):
    """A measure of quarter notes: each item a pitch, or (pitch, [lyric...])."""
    out = []
    for it in pitches_and_lyrics:
        p, ls = (it, ()) if isinstance(it, int) else it
        out.append(sung("quarter", p, lyrics=ls))
    return [out]


def sung_notes(hand_notes):
    """(ticksStart, note, text, syllabic, extend, sung) of the notes carrying a lyric."""
    return [(n["ticksStart"], n["note"], n["lyric"]["text"], n["lyric"]["syllabic"], n["lyric"]["extend"],
             n.get("sung", False)) for n in hand_notes if "lyric" in n]


class LyricsTest(unittest.TestCase):
    LH = [[[chord("whole", 48)]], [[chord("whole", 43)]], [[chord("whole", 36)]], [[rest("whole")]]]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def convert(self, rh, lh=None, **kw):
        path = os.path.join(self.tmp, "Test", "song.mscz")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        make_piano_score(path, rh, lh or self.LH[:len(rh)], **kw)
        return convert_mscz(path, parts=True), path

    def test_single_begin_middle_end(self):
        rh = [quarters((72, [lyric("Sing")]), (74, [lyric(" won ", "begin")]), (76, [lyric("der", "middle")]),
                       (77, [lyric("ful", "end")]))]
        c, _ = self.convert(rh)
        self.assertEqual(sung_notes(c.parts["parts"]["original"]["right"]),
                         [(0, 72, "Sing", "single", False, False), (Q, 74, "won", "begin", False, False),
                          (2 * Q, 76, "der", "middle", False, False), (3 * Q, 77, "ful", "end", False, False)])
        self.assertEqual(sung_notes(c.parts["parts"]["original"]["left"]), [])
        self.assertEqual(c.parts["lyrics"], {"syllables": 4, "verses": 1, "staves": ["Piano"], "sources": ["piano"]})

    def test_extender_and_melisma(self):
        # "love" held by an extender over the next note; "go" hyphenated over a note without a syllable
        rh = [quarters((72, [lyric("love", ticks="1/4")]), 71, (69, [lyric("go", "begin")]), 67),
              quarters((65, [lyric("ing", "end")]), (64, [lyric("home")]), (62, [lyric("a", "begin")]),
                       (60, [lyric("way", "end", ticks="1/4")]))]
        c, _ = self.convert(rh, self.LH[:2])
        got = [(t, x, e) for t, _n, x, _s, e, _v in sung_notes(c.parts["parts"]["original"]["right"])]
        self.assertEqual(got, [(0, "love", True), (2 * Q, "go", True), (W, "ing", False), (W + Q, "home", False),
                               (W + 2 * Q, "a", False), (W + 3 * Q, "way", True)])

    def test_two_verses_over_a_repeat(self):
        rh = [quarters((72, [lyric("one", no=0), lyric("two", no=1)]), (74, [lyric("extra", no=1)]), 76, 77),
              quarters((79, [lyric("same")]), 77, 76, 74),
              quarters((72, [lyric("late", no=1)]), 72, 72, 72)]
        c, _ = self.convert(rh, self.LH[:3], repeat=(0, 1))
        got = [(t, x) for t, _n, x, *_ in sung_notes(c.parts["parts"]["original"]["right"])]
        # pass 1: verse 1 (verse 2's extra syllable waits for pass 2); pass 2: verse 2, or the lowest verse where
        # the measure has no verse 2; after the repeat: the measure's only verse
        self.assertEqual(got, [(0, "one"), (W, "same"), (2 * W, "two"), (2 * W + Q, "extra"), (3 * W, "same"),
                               (4 * W, "late")])
        self.assertEqual(c.parts["lyrics"]["verses"], 2)
        self.assertEqual(c.parts["lyrics"]["syllables"], 6)
        # on the notes' clock: 120 bpm, 2 s a measure
        starts = [n["start"] for n in c.parts["parts"]["original"]["right"] if "lyric" in n]
        self.assertEqual([round(s, 3) for s in starts], [0.0, 2.0, 4.0, 4.5, 6.0, 8.0])

    def test_without_repeats_the_first_verse(self):
        rh = [quarters((72, [lyric("second", no=1), lyric("first")]), (74, [lyric("held", no=1)]), 76, 77),
              quarters((79, [lyric("only", no=2)]), 77, 76, 74)]
        c, _ = self.convert(rh, self.LH[:2])
        got = [x for _t, _n, x, *_ in sung_notes(c.parts["parts"]["original"]["right"])]
        # verse 2's "held" is not sung (verse 1 is, in its measure); "only": the lowest verse of its measure
        self.assertEqual(got, ["first", "only"])

    def test_a_lyric_on_a_chord_goes_to_its_top_note(self):
        # voice 1: the melody on top of a chord; voice 2: a lower line with its own syllable
        rh = [[[sung("quarter", 60, 64, 72, lyrics=[lyric("top")]), chord("quarter", 74), chord("half", 76)],
               [sung("half", 55, lyrics=[lyric("low")]), rest("half")]]]
        c, _ = self.convert(rh)
        got = [(t, n, x) for t, n, x, *_ in sung_notes(c.parts["parts"]["original"]["right"])]
        self.assertEqual(got, [(0, 72, "top"), (0, 55, "low")])

    def test_a_vocal_staffs_lyric_goes_onto_the_piano_note_that_doubles_it(self):
        # a soprano (a voice: in Note Waterfall's orchestra) over a piano that doubles it at pitch and an octave up
        voice = ("Soprano", 52, [[quarters((72, [lyric("Ah")]), (74, [lyric("men", "begin")]),
                                           (76, [lyric("no")]), (77, [lyric("more", ticks="1/4")]))]])
        rh = [[[sung("quarter", 64, 72), chord("quarter", 86), chord("quarter", 67), chord("quarter", 77)]]]
        c, _ = self.convert(rh, parts=(voice,))
        P = c.parts["parts"]
        self.assertEqual(sung_notes(P["orchestra"]["right"]),
                         [(0, 72, "Ah", "single", False, True), (Q, 74, "men", "begin", False, True),
                          (2 * Q, 76, "no", "single", False, True), (3 * Q, 77, "more", "single", True, True)])
        # "no": the piano does not double it (67 is neither 76 nor an octave away)
        self.assertEqual(sung_notes(P["original"]["right"]),
                         [(0, 72, "Ah", "single", False, False), (Q, 86, "men", "begin", False, False),
                          (3 * Q, 77, "more", "single", True, False)])
        self.assertEqual(c.parts["lyrics"], {"syllables": 4, "verses": 1, "staves": ["Soprano"],
                                             "sources": ["vocal"]})

    def test_a_vocal_staff_in_the_orchestra_is_sung_there_and_copied(self):
        vln = ("Violin", 40, [[quarters((72, [lyric("la")]), (79, [lyric("di")]), 76, 77)]])
        rh = [quarters(72, 67, 76, 77)]
        lh = [[[chord("quarter", 72), chord("quarter", 55), chord("half", 48)]]]
        c, _ = self.convert(rh, lh, parts=(vln,))
        P = c.parts["parts"]
        self.assertEqual(c.parts["orchestra"], "instruments")
        self.assertEqual(sung_notes(P["orchestra"]["right"]),
                         [(0, 72, "la", "single", False, True), (Q, 79, "di", "single", False, True)])
        # both hands double "la": the right hand's note; "di" an octave lower
        self.assertEqual(sung_notes(P["original"]["right"]),
                         [(0, 72, "la", "single", False, False), (Q, 67, "di", "single", False, False)])
        self.assertEqual(sung_notes(P["original"]["left"]), [])
        self.assertEqual(c.parts["lyrics"]["sources"], ["vocal"])
        self.assertEqual(c.parts["lyrics"]["syllables"], 2)

    def test_a_voice_named_like_a_piano_is_still_a_vocal_staff(self):
        # a voice whose track name says piano is not a piano staff (pvjson.staff_roles): its notes are in the
        # orchestra, sung, and the piano's right hand carries the copy
        voice = ("Voice piano", 52, [[quarters((72, [lyric("oh")]), 74, 76, 77)]])
        rh = [quarters(72, 74, 76, 77)]
        c, _ = self.convert(rh, parts=(voice,))
        P = c.parts["parts"]
        self.assertEqual(sung_notes(P["orchestra"]["right"]), [(0, 72, "oh", "single", False, True)])
        self.assertEqual(sung_notes(P["original"]["right"]), [(0, 72, "oh", "single", False, False)])
        self.assertEqual(P["original"]["left"][0]["note"], 48)        # the piano's own left hand
        self.assertEqual(c.parts["lyrics"]["sources"], ["vocal"])

    def test_the_simplified_part_carries_the_lyrics(self):
        rh = [[[sung("quarter", 60, 64, 72, lyrics=[lyric("keep")]), sung("quarter", 62, 71, lyrics=[lyric("lost")]),
                chord("half", 72)]],
              quarters((79, [lyric("as", "begin")]), (77, [lyric("is", "end")]), 76, 74)]
        simple = ("Piano-simplified", 0, [[[[chord("quarter", 72), chord("quarter", 69), chord("half", 72)]],
                                           [[rest("whole")]]],
                                          [[[rest("whole")]], [[rest("whole")]]]])
        c, _ = self.convert(rh, self.LH[:2], parts=(simple,))
        S = c.parts["parts"]["simplified"]["right"]
        got = [(n["ticksStart"], n["note"], n["lyric"]["text"], n.get("simplified")) for n in S if "lyric" in n]
        # measure 1 from the simplified staff: "keep" survives (72), "lost" does not (71 became 69);
        # measure 2 is the original part's
        self.assertEqual(got, [(0, 72, "keep", 1), (W, 79, "as", None), (W + Q, 77, "is", None)])
        self.assertEqual(c.parts["lyrics"]["syllables"], 4)

    def test_no_lyrics_is_null_and_the_key_order(self):
        c, path = self.convert([quarters(72, 74, 76, 77)])
        self.assertIsNone(c.parts["lyrics"])
        doc = json.loads(parts_bytes(c.parts, "test_Hands.json", c.data, content_hash(path)))
        self.assertEqual(list(doc), ["format", "song", "songMd5", "scoreContent", "resolution", "simplified",
                                     "orchestra", "markers", "lyrics", "parts"])
        self.assertIsNone(doc["lyrics"])
        self.assertEqual(doc["format"], 1)
        self.assertIsNone(parts_stats(c.parts)["lyrics"])

    def test_the_pianovision_json_is_unchanged(self):
        voice = ("Soprano", 52, [[quarters(72, 74, (76, [lyric("Ah")]), 77)]])
        rh = [quarters((72, [lyric("one", no=0), lyric("two", no=1)]), (74, [lyric("Più")]), 76, 77)]
        c, path = self.convert(rh, parts=(voice,))
        self.assertEqual(c.data, convert_mscz(path).data)
        self.assertEqual(c.data, convert_mscz(path, provenance=True).data)
        data = parts_bytes(c.parts, "test_Hands.json", c.data, content_hash(path))
        self.assertIn(b'"lyric":{"text":"Pi\\u00f9","syllabic":"single","extend":false}', data)
        self.assertEqual(parts_stats(c.parts)["lyrics"], {"syllables": 3, "sources": ["piano", "vocal"]})


class LyricTextTest(unittest.TestCase):
    def text(self, xml):
        return lyric_text(ET.fromstring(f"<Lyrics><text>{xml}</text></Lyrics>"))

    def test_text_as_written(self):
        for written, shown in [('<font size="10"/><font face="Arial"/>Sing', "Sing"),
                               ("  don’t ", "don’t"), ("café,", "café,"), ("PiÃ¹", "Più"),
                               ("<b>sing</b>ing", "singing"), ("a\xa0\xa0b", "a b"),
                               ("me<sym>lyricsElision</sym>and", "me‿and"), ("o<sym>metNoteQuarterUp</sym>h", "oh"),
                               ("lo-", "lo-"), ("1.\xa0Some", "Some"), ("2. ", "2."), ("1999", "1999"), ("", "")]:
            self.assertEqual(self.text(written), shown, written)


if __name__ == "__main__":
    unittest.main()
