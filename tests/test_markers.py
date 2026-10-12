"""Note Waterfall's markers: the score's rehearsal marks, sections and tempo words in the parts file."""

import json
import os
import shutil
import tempfile
import unittest

from pianovision.convert import content_hash, convert_mscz
from pianovision.markers import ScoreText, build_markers, classify, clean_text
from pianovision.parts import parts_bytes

from tests.scoregen import E, chord, make_piano_score, rest

Q = 480
W = 4 * Q          # a 4/4 measure


def raw(xml):
    """Literal XML lines inside a voice (mixed content such as <sym> in a text)."""
    return [xml]


def rehearsal(text):
    return E("RehearsalMark", E("text", text=text))


def tempo(text, bpm=None, visible=True):
    kids = [] if visible else ["<visible>0</visible>"]
    t = f"<tempo>{bpm / 60:.6f}</tempo>" if bpm else ""
    return raw(f"<Tempo>{''.join(kids)}{t}<followText>1</followText><text>{text}</text></Tempo>")


def staff_text(text):
    return E("StaffText", E("text", text=text))


def play_tech(text, technique="normal"):
    """MuseScore 4.7's form of a staff text that switches the playing technique."""
    return E("PlayTechAnnotation", E("playTechType", text=technique), E("text", text=text))


def system_text(text):
    return E("SystemText", E("text", text=text))


def notes(*elements, pitch=72):
    """A 4/4 measure: the elements, then four quarter notes."""
    return [list(elements) + [chord("quarter", pitch) for _ in range(4)]]


class MarkersTest(unittest.TestCase):
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

    def markers(self, rh, lh=None, **kw):
        c, _ = self.convert(rh, lh, **kw)
        return [(round(m["time"], 3), m["text"], m["kind"], m["measure"]) for m in c.parts["markers"]]

    def test_rehearsal_marks_and_tempo_words_on_the_notes_clock(self):
        rh = [notes(rehearsal("Intro"), tempo("Andante <sym>metNoteQuarterUp</sym> = 90", 90)),
              notes(rehearsal("A")),
              notes(tempo("Più mosso (<sym>metNoteQuarterUp</sym> = 120)", 120)),
              notes(rehearsal("B"))]
        c, _ = self.convert(rh)
        got = [(round(m["time"], 3), m["text"], m["kind"], m["measure"]) for m in c.parts["markers"]]
        m1 = 4 * 60 / 90                                       # a measure at 90 bpm
        self.assertEqual(got, [(0.0, "Intro", "rehearsal", 1), (0.0, "Andante", "tempo", 1),
                               (round(m1, 3), "A", "rehearsal", 2), (round(2 * m1, 3), "Più mosso", "tempo", 3),
                               (round(2 * m1 + 2.0, 3), "B", "rehearsal", 4)])
        # exactly the time of the notes struck there
        starts = {n["ticksStart"]: n["start"] for n in c.parts["parts"]["original"]["right"]}
        times = {m["text"]: m["time"] for m in c.parts["markers"]}
        for text, tick in (("A", W), ("Più mosso", 2 * W), ("B", 3 * W)):
            self.assertAlmostEqual(times[text], starts[tick], places=4)

    def test_repeats_are_unrolled(self):
        rh = [notes(rehearsal("A")), notes(rehearsal("B")), notes(system_text("Coda"))]
        got = self.markers(rh, repeat=(0, 1))
        # 120 bpm: 2 s a measure; A B | A B | Coda
        self.assertEqual(got, [(0.0, "A", "rehearsal", 1), (2.0, "B", "rehearsal", 2), (4.0, "A", "rehearsal", 1),
                               (6.0, "B", "rehearsal", 2), (8.0, "Coda", "section", 3)])

    def test_noise_is_left_out(self):
        rh = [notes(staff_text("dim."), staff_text("merge"), staff_text("E G♯"), staff_text("L.H."),
                    staff_text("dolce"), tempo("<sym>metNoteQuarterUp</sym> = 80", 80)),
              notes(staff_text("end merge"), staff_text("poco a poco cresc."), staff_text("con sord."),
                    tempo("Allegro", 80, visible=False), system_text("Cm"), staff_text("a tempo")),
              notes(staff_text("<sym>dynamicPiano</sym> espressivo"), staff_text("0:19"), staff_text("Ped. simile"))]
        got = self.markers(rh)
        m1 = 4 * 60 / 80
        self.assertEqual(got, [(0.0, "dolce", "expression", 1), (round(m1, 3), "a tempo", "tempo", 2),
                               (round(2 * m1, 3), "espressivo", "expression", 3)])

    def test_a_playing_technique_text_is_read_like_a_staff_text(self):
        vln = ("Violin", 40, [[[[play_tech("con brio", "pizzicato"), chord("whole", 67)]], [[chord("whole", 69)]]]])
        rh = [notes(play_tech("en dehors")), notes(play_tech("una corda"))]
        got = self.markers(rh, parts=(vln,))
        self.assertEqual(got, [(0.0, "en dehors", "text", 1)])

    def test_orchestra_staff_texts_are_not_the_pianos(self):
        vln = ("Violin", 40, [[[[staff_text("pizz."), staff_text("con brio"), chord("whole", 67)]],
                               [[system_text("Meno mosso"), chord("whole", 69)]]]])
        rh = [notes(), notes()]
        got = self.markers(rh, parts=(vln,))
        self.assertEqual(got, [(2.0, "Meno mosso", "tempo", 2)])

    def test_the_same_text_twice_at_one_moment_is_kept_once(self):
        lh = [[[staff_text("cantabile"), chord("whole", 48)]], [[chord("whole", 43)]]]
        rh = [notes(staff_text("cantabile"), tempo("rit.")), notes()]
        got = self.markers(rh, lh)
        # at one moment: the more important kind first
        self.assertEqual(got, [(0.0, "rit.", "gradual", 1), (0.0, "cantabile", "expression", 1)])

    def test_the_pianovision_json_is_unchanged_and_markers_precede_the_parts(self):
        rh = [notes(rehearsal("A"), tempo("Più mosso", 100)), notes()]
        c, path = self.convert(rh)
        self.assertEqual(c.data, convert_mscz(path).data)
        data = parts_bytes(c.parts, "test_Hands.json", c.data, content_hash(path))
        doc = json.loads(data)
        self.assertEqual(list(doc), ["format", "song", "songMd5", "scoreContent", "resolution", "simplified",
                                     "orchestra", "markers", "parts"])
        self.assertIn(b"Pi\\u00f9 mosso", data)               # ASCII JSON, UTF-8 text after decoding
        self.assertEqual(doc["markers"][1]["text"], "Più mosso")

    def test_a_score_without_texts_has_an_empty_list(self):
        c, _ = self.convert([notes()])
        self.assertEqual(c.parts["markers"], [])


class ClassifyTest(unittest.TestCase):
    def test_clean_text(self):
        for written, shown in [("Andante cantabile.  ( = 50)", "Andante cantabile."),
                               ("Swing (            )", "Swing"), ("rit.........................", "rit."),
                               ("poco -    -    -", "poco"), ("dolce", "dolce"), ("PiÃ¹ mosso", "Più mosso"),
                               ("Meno mosso (metNoteHalfUp = 48)", "Meno mosso"),
                               ("= 54", ""), ("L'istesso tempo  =", "L'istesso tempo"), ("Lento\xa0 sostenuto", "Lento sostenuto")]:
            self.assertEqual(clean_text(written), shown, written)

    def test_kinds(self):
        cases = [("RehearsalMark", "A", ("A", "rehearsal")), ("RehearsalMark", "10", ("10", "rehearsal")),
                 ("SystemText", "Verse 2", ("Verse 2", "section")), ("Tempo", "Theme", ("Theme", "section")),
                 ("Expression", "Cadenza.", ("Cadenza.", "section")), ("Tempo", "a tempo", ("a tempo", "tempo")),
                 ("Tempo", "Tempo I.", ("Tempo I.", "tempo")), ("StaffText", "poco rit.", ("poco rit.", "gradual")),
                 ("StaffText", "un poco marc. rallent.", ("un poco marc. rallent.", "gradual")),
                 ("Tempo", "Poco più mosso.", ("Poco più mosso.", "tempo")),
                 ("Expression", "con brio", ("con brio", "expression")),
                 ("StaffText", "Freely", ("Freely", "tempo")), ("StaffText", "Real bad man", ("Real bad man", "text")),
                 ("GradualTempoChange", "", ("accel.", "gradual")), ("Marker", "", ("Coda", "section"))]
        subtypes = {"GradualTempoChange": "accelerando", "Marker": "coda"}
        for tag, text, want in cases:
            self.assertEqual(classify(tag, text, subtypes.get(tag, "")), want, (tag, text))

    def test_left_out(self):
        for tag, text in [("StaffText", "dim."), ("StaffText", "merge"), ("StaffText", "end merge"),
                          ("StaffText", "C♯ F♯"), ("SystemText", "Cm"), ("StaffText", "a.2."), ("StaffText", "unis."),
                          ("StaffText", "I. Solo"), ("StaffText", "R. H."), ("StaffText", "m.d."), ("Tempo", "ten."),
                          ("StaffText", "una corda"), ("StaffText", "tr"), ("StaffText", "To Coda"),
                          ("StaffText", "D.S. al Coda"), ("Expression", "quasi glissando"), ("Expression", "[Oboe solo]"),
                          ("StaffText", "Ossia Cadenza"), ("Expression", "Right hand over left"), ("Tempo", "= 54"),
                          ("StaffText", "x" * 60), ("Marker", "To Coda"), ("RehearsalMark", "  ")]:
            self.assertIsNone(classify(tag, text, "coda" if tag == "Marker" else ""), (tag, text))
        self.assertIsNone(classify("Marker", "", "segno"))
        self.assertIsNone(classify("Marker", "", "fine"))

    def test_build_markers_skips_invisible_and_other_staves_and_sorts(self):
        texts = [ScoreText(960, "Tempo", "Allegro", 0, 2), ScoreText(0, "RehearsalMark", "A", 0, 1),
                 ScoreText(0, "Tempo", "Lento", 0, 1, visible=False), ScoreText(0, "StaffText", "dolce", 5, 1),
                 ScoreText(0, "GradualTempoChange", "rit.", 0, 1, subtype="ritardando"),
                 ScoreText(0, "StaffText", "rit", 1, 1)]
        got = build_markers(texts, lambda t: t / 480.0, piano_staves=[0, 1])
        self.assertEqual([(m["time"], m["text"], m["kind"]) for m in got],
                         [(0.0, "A", "rehearsal"), (0.0, "rit.", "gradual"), (2.0, "Allegro", "tempo")])


if __name__ == "__main__":
    unittest.main()
