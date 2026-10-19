"""The lyrics importer (pianovision.lyricsimport, ``lyrics import``): a lyrics text file onto a score's vocal line.

Only invented placeholder words are used here."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

from pianovision import lyricsimport as LI
from pianovision.cli import main
from pianovision.convert import convert_mscz

from tests.scoregen import chord, make_piano_score, note, rest

W = 4 * 480                    # a 4/4 measure in ticks


def q(p):
    return chord("quarter", p)


def mscx_of(path):
    with zipfile.ZipFile(path) as z:
        return z.read("score.mscx").decode("utf-8")


def sung(parts, part="orchestra"):
    """(ticksStart, text, syllabic, extend) of the vocal notes carrying a syllable."""
    return [(n["ticksStart"], n["lyric"]["text"], n["lyric"]["syllabic"], n["lyric"]["extend"])
            for n in parts["parts"][part]["right"] if "lyric" in n]


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def score(self, voice, repeat=None, name="song.mscz"):
        """A voice (voice.vocals) above a piano, ``voice``: one list of elements per measure."""
        n = len(voice)
        path = os.path.join(self.tmp, name)
        make_piano_score(path, [[[chord("whole", 72)]]] * n, [[[chord("whole", 48)]]] * n, repeat=repeat,
                         before=[("Voice", 53, [[[m] for m in voice]], {"id": "voice.vocals"})])
        return path

    def text(self, body, name="lyrics.txt"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    # ------------------------------------------------------------------
    def test_repeats_carry_stacked_verses(self):
        # m.1-2 repeated (a rest ends each pass), then m.3-4
        voice = [[q(67), q(69), q(71), q(72)], [q(74), q(72), rest("half")],
                 [q(72), q(71), q(69), q(67)], [chord("whole", 65)]]
        path = self.score(voice, repeat=(0, 1))
        plan = LI.plan_import(path, "[Verse 1]\nTa-mo ri-ka lo-ne\n[Verse 2]\nBe-lu sa-mi to-ra\nKi-va lo-ne pa\n")
        got = [(p.slot.measure, p.slot.passno, p.verse, p.text, p.syllabic) for p in plan.placements]
        self.assertEqual(got[:7], [(1, 1, 0, "Ta", "begin"), (1, 1, 0, "mo", "end"), (1, 1, 0, "ri", "begin"),
                                   (1, 1, 0, "ka", "end"), (2, 1, 0, "lo", "begin"), (2, 1, 0, "ne", "end"),
                                   (1, 2, 1, "Be", "begin")])
        self.assertEqual([(p.slot.measure, p.verse) for p in plan.placements[6:]],
                         [(1, 1)] * 4 + [(2, 1)] * 2 + [(3, 0)] * 4 + [(4, 0)])
        new = os.path.join(self.tmp, "new.mscz")
        with open(new, "wb") as f:
            f.write(LI.build_mscz(plan))
        # verse 2 is <no>1</no>, after verse 1 on the same chord, as MuseScore 4.7.5 writes them
        x = mscx_of(new)
        self.assertIn("            <durationType>quarter</durationType>\n"
                      "            <Lyrics>\n"
                      "              <syllabic>begin</syllabic>\n"
                      "              <text>Ta</text>\n"
                      "              </Lyrics>\n"
                      "            <Lyrics>\n"
                      "              <no>1</no>\n"
                      "              <syllabic>begin</syllabic>\n"
                      "              <text>Be</text>\n"
                      "              </Lyrics>\n"
                      "            <Note>\n", x)
        ver = LI.verify(plan, new, mscore=None, work=self.tmp)
        self.assertTrue(ver.ok, ver.lines)
        self.assertIn("lyrics.collect_lyrics: 17 of 17 syllables on their notes and passes", ver.lines)
        # the formatter's parts file: verse 1 on the first pass, verse 2 on the second
        c = convert_mscz(new, parts=True)
        texts = [(t, x) for t, x, _s, _e in sung(c.parts)]
        self.assertEqual(texts[:7], [(0, "Ta"), (480, "mo"), (960, "ri"), (1440, "ka"), (W, "lo"), (W + 480, "ne"),
                                     (2 * W, "Be")])
        self.assertEqual(texts[12:], [(4 * W, "Ki"), (4 * W + 480, "va"), (4 * W + 960, "lo"),
                                      (4 * W + 1440, "ne"), (5 * W, "pa")])
        self.assertEqual(c.parts["lyrics"]["verses"], 2)
        report = "\n".join(LI.format_plan(plan))
        self.assertIn("[Verse 2] (line 3): m.1-2 (2nd time), m.3-4; verses 1, 2", report)

    def test_melismas_held_notes_and_extenders(self):
        # m.1-2: seven notes, the last three under a slur; m.3-4: "_" holds the first syllable, a tie counts once
        slur = "<Spanner type=\"Slur\"><Slur/><next><location><fractions>1/2</fractions></location></next></Spanner>"
        from tests.scoregen import E

        class Raw(E):
            def __init__(self, text):
                super().__init__("raw")
                self.raw = text

            def lines(self, indent=""):
                return [indent + self.raw]

        end = "<Spanner type=\"Slur\"><prev><location><fractions>-1/2</fractions></location></prev></Spanner>"
        voice = [[q(60), q(62), q(64), q(65)],
                 [chord("quarter", 67, extra=(Raw(slur),)), q(65), chord("quarter", 64, extra=(Raw(end),)),
                  rest("quarter")],
                 [q(60), q(62), q(64), chord("quarter", note(65, tie_next=(1, "-3/4")))],
                 [chord("half", note(65, tie_prev=(-1, "3/4"))), q(67), rest("quarter")]]
        path = self.score(voice)
        plan = LI.plan_import(path, "Ka-lo mi-ra tu\nSe_ va lo ki\n")
        got = [(p.slot.measure, p.text, p.syllabic, len(p.held), p.ticks_f) for p in plan.placements]
        from fractions import Fraction
        self.assertEqual(got, [(1, "Ka", "begin", 0, None), (1, "lo", "end", 0, None), (1, "mi", "begin", 0, None),
                               (1, "ra", "end", 0, None), (2, "tu", "single", 2, Fraction(1, 2)),
                               (3, "Se", "single", 1, Fraction(1, 4)), (3, "va", "single", 0, None),
                               (3, "lo", "single", 0, None), (4, "ki", "single", 0, None)])
        self.assertEqual(plan.lines[0].held, 2)
        self.assertFalse(plan.lines[0].mismatch)
        new = os.path.join(self.tmp, "new.mscz")
        with open(new, "wb") as f:
            f.write(LI.build_mscz(plan))
        self.assertIn("<syllabic>end</syllabic>", mscx_of(new))
        self.assertIn("              <ticks_f>1/2</ticks_f>\n              <text>tu</text>\n", mscx_of(new))
        ver = LI.verify(plan, new, mscore=None, work=self.tmp)
        self.assertTrue(ver.ok, ver.lines)
        c = convert_mscz(new, parts=True)
        held = {x: e for _t, x, _s, e in sung(c.parts)}
        self.assertTrue(held["tu"])                 # the extender: held over the slurred notes
        self.assertTrue(held["Se"])
        self.assertFalse(held["ra"])

    def test_mismatch_report(self):
        # five notes, then after a rest three; the text has seven syllables for the five notes and four for the three
        voice = [[q(60), q(62), q(64), q(65)], [q(67), rest("quarter"), q(65), q(64)], [q(62), rest("half", 1)]]
        path = self.score(voice)
        txt = self.text("[Verse 1]\nZox-quil-vap kez-luf-vyx-nip\n\n[Verse 2]\nTez-kix-loz-pex\n")
        code, out = self.run_cli("lyrics", "import", path, txt, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("line 2: 7 syllables on 5 notes, 2 merged; one phrase; m.1-2: mismatch", out)
        self.assertIn("line 5: 4 syllables on 3 notes, 1 merged; one phrase; m.2-3: mismatch", out)
        self.assertIn("syllables: 8 written (0 notes held, 3 syllables merged, 0 notes without a syllable); "
                      "2 lines with a mismatch", out)
        for word in ("Zox", "quil", "vap", "kez", "luf", "vyx", "nip", "Tez", "kix", "loz", "pex"):
            self.assertNotIn(word, out)                     # the report never shows the text
        self.assertEqual(sorted(os.listdir(self.tmp)), ["lyrics.txt", "song.mscz"])
        # the syllables of one word merge (no elision across words while a word can take them)
        plan = LI.plan_import(path, "Zox-quil-vap kez-luf-vyx-nip\nTez-kix-loz-pex\n")
        texts = [p.text for p in plan.placements[:5]]
        self.assertEqual("".join(texts), "Zoxquilvapkezlufvyxnip")
        self.assertFalse(any(LI.ELISION in t for t in texts))

    def test_more_syllables_than_a_word_can_take_elide(self):
        voice = [[q(60), q(62), rest("half")]]
        path = self.score(voice)
        plan = LI.plan_import(path, "Ba do ki\n")
        self.assertEqual([p.text for p in plan.placements], ["Ba" + LI.ELISION + "do", "ki"])
        self.assertEqual(plan.lines[0].merged, 1)
        new = os.path.join(self.tmp, "new.mscz")
        with open(new, "wb") as f:
            f.write(LI.build_mscz(plan))
        self.assertIn("<text>Ba<sym>lyricsElision</sym>do</text>", mscx_of(new))
        self.assertTrue(LI.verify(plan, new, mscore=None, work=self.tmp).ok)

    def test_junk_block_skipped(self):
        body = ("Placeholder Song Lyrics\n"
                "[Verse 1]\n"
                "La-mo ti-ka\n"
                "You might also like\n"
                "Invented Title One\n"
                "Invented Band\n"
                "[Chorus]\n"
                "Ro-ve na-li42Embed\n"
                "3 Contributors\n"
                "Not a lyric either\n")
        lt = LI.parse_text(body)
        self.assertEqual([(ln.lineno, len(ln.syls)) for ln in lt.sung], [(3, 4), (8, 4)])
        self.assertEqual(lt.warnings, ["line 1: a title line (\"... Lyrics\") before the first header, skipped",
                                       "lines 4-6: not lyrics (a junk marker on line 4), skipped up to the next header",
                                       "line 8: a trailing \"Embed\" left out",
                                       "lines 9-10: not lyrics (a junk marker on line 9), skipped up to the next "
                                       "header"])
        self.assertEqual(lt.sung[1].syls[-1].text, "li")

    def test_text_format(self):
        body = ("[Intro]\n"
                "[Verse 1: Someone]\n"
                "Hel-lo_ the (ooh) moon,  " + chr(0x2014) + " a-way__\n"
                "\n"
                "Ka-ra (x2)\n"
                "[Piano Solo]\n"
                "[Verse 1]\n"
                "[Outro @ m.12 pass 2]\n"
                "beau- ti- ful _\n")
        lt = LI.parse_text(body)
        self.assertEqual([s.name for s in lt.sections], ["Intro", "Verse 1", "Piano Solo", "Verse 1", "Outro"])
        self.assertEqual([s.instrumental for s in lt.sections], [True, False, True, False, False])
        self.assertEqual(lt.sections[3].repeat_of, 1)
        self.assertEqual(lt.sections[4].pin, (12, 2))
        first = lt.sung[0]
        self.assertEqual([(s.text, s.syllabic, s.holds) for s in first.syls],
                         [("Hel", "begin", 0), ("lo", "end", 1), ("the", "single", 0), ("moon,", "single", 0),
                          ("a", "begin", 0), ("way", "end", 2)])
        self.assertEqual(first.need, 9)
        self.assertTrue(first.after_break and first.first)
        self.assertEqual([(ln.lineno, ln.again, ln.first, ln.section) for ln in lt.sung],
                         [(3, False, True, 1), (5, False, False, 1), (5, True, False, 1),
                          (3, True, True, 3), (5, True, False, 3), (5, True, False, 3), (9, False, True, 4)])
        self.assertTrue(lt.sung[3].after_break)
        self.assertEqual([(s.text, s.syllabic, s.holds) for s in lt.sung[-1].syls],
                         [("beau", "begin", 0), ("ti", "middle", 0), ("ful", "end", 1)])
        self.assertIn("line 3: parenthesized text left out (1 word)", lt.warnings)
        kept = LI.parse_text("Hel-lo (ooh) moon\n", keep_parentheses=True)
        self.assertEqual([s.text for s in kept.sung[0].syls], ["Hel", "lo", "(ooh)", "moon"])

    def test_rule_based_syllables(self):
        cases = {"love": "love", "lovely": "love-ly", "wanted": "wan-ted", "places": "pla-ces", "little": "lit-tle",
                 "table": "ta-ble", "hundred": "hun-dred", "instrument": "in-stru-ment", "heaven": "hea-ven",
                 "going": "go-ing", "singing": "sing-ing", "running": "run-ning", "every": "ev-ery",
                 "something": "some-thing", "lion": "li-on", "piano": "pi-a-no", "nation": "na-tion",
                 "quiet": "qui-et", "fire": "fire", "eyes": "eyes", "goodbye": "good-bye", "didn't": "did-n't",
                 "don't": "don't", "you're": "you're", "Morning": "Morn-ing", "River": "Ri-ver",
                 "beautiful": "beau-ti-ful", "hmm": "hmm"}
        got = {w: "-".join(LI.syllables(w)) for w in cases}
        self.assertEqual(got, cases)
        # a hyphenation module, when installed, splits the words the user did not split
        lt = LI.parse_text("wonderful lov-er\n", split=lambda w: [w[:3], w[3:]] if len(w) > 4 else [w])
        self.assertEqual([s.text for s in lt.sung[0].syls], ["won", "derful", "lov", "er"])

    def test_pyphen_is_used_when_installed(self):
        class FakePyphen:
            LANGUAGES = {"en_US": "x"}

            class Pyphen:
                def __init__(self, lang, left, right):
                    self.args = (lang, left, right)

                def positions(self, word):
                    return [2] if len(word) > 3 else []

        sys.modules["pyphen"] = FakePyphen
        try:
            split, name = LI.syllabifier()
        finally:
            del sys.modules["pyphen"]
        self.assertEqual(name, "pyphen (en_US)")
        self.assertEqual(split("tomato"), ["to", "mato"])

    def test_lines_and_phrases(self):
        # phrases: 3 notes | rest | 3 notes | rest | 2 notes + (no rest) 2 notes
        voice = [[q(60), q(62), q(64), rest("quarter")], [q(65), q(67), q(69), rest("quarter")],
                 [q(67), q(65), q(64), q(62)], [rest("whole")], [q(60), q(62), q(64), rest("quarter")]]
        path = self.score(voice)
        # a line over two phrases, two lines in one phrase, an instrumental section across the long rest
        text = "[A]\nLa ma ko ri va do\nSe to\nPe lu\n[Break]\n[B]\nNa ki mo\n"
        plan = LI.plan_import(path, text)
        self.assertEqual([(m.kind, m.lines, m.phrases) for m in plan.matches],
                         [("sung", [0], [0, 1]), ("sung", [1, 2], [2]), ("sung", [3], [3])])
        report = "\n".join(LI.format_plan(plan))
        self.assertIn("line 2: 6 syllables on 6 notes; over 2 phrases (rests inside the line); m.1-2", report)
        self.assertIn("line 3: 2 syllables on 2 notes; one phrase with line 4 (no rest between them); m.3", report)
        self.assertIn("line 4: 2 syllables on 2 notes; in one phrase with line 3; m.3", report)
        self.assertIn("[Break] (line 5): instrumental", report)
        # a phrase no line is sung on, and a pin
        plan = LI.plan_import(path, "[A]\nLa ma ko\n[B @ m.5]\nNa ki mo\n")
        self.assertEqual([(m.kind, m.lines, m.phrases) for m in plan.matches],
                         [("sung", [0], [0]), ("phrase", [], [1]), ("phrase", [], [2]), ("sung", [1], [3])])
        self.assertIn("phrases no line is sung on: 2 (7 notes): m.2-3", "\n".join(LI.format_plan(plan)))

    def test_dry_run_writes_nothing(self):
        voice = [[q(60), q(62), q(64), q(65)], [chord("whole", 67)]]
        path = self.score(voice)
        txt = self.text("Mo-ra ti-ka lu\n")
        with open(path, "rb") as f:
            before = f.read()
        work = os.path.join(self.tmp, "work")
        code, out = self.run_cli("lyrics", "import", path, txt, "--dry-run", "--work", work)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("song.mscz: vocal line staff 1 (Voice, voice.vocals), voice 1: 5 notes to "
                                       "sing (a tied note once) in 1 phrase\n"), out)
        self.assertIn("line 1: 5 syllables on 5 notes; one phrase; m.1-2", out)
        self.assertIn("verses written: verse 1: 5", out)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(work))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["lyrics.txt", "song.mscz"])

    def test_copy_then_in_place_with_backup(self):
        voice = [[q(60), q(62), q(64), q(65)], [chord("whole", 67)]]
        path = self.score(voice)
        txt = self.text("Mo-ra ti-ka lu\n")
        work = os.path.join(self.tmp, "work")
        with open(path, "rb") as f:
            original = f.read()
        # a copy
        copy = os.path.join(self.tmp, "copy.mscz")
        code, out = self.run_cli("lyrics", "import", path, txt, "-o", copy, "--no-musescore", "--work", work)
        self.assertEqual(code, 0, out)
        self.assertIn("  lyrics.collect_lyrics: 5 of 5 syllables on their notes and passes\n", out)
        self.assertIn(f"{copy}: 5 syllables written", out)
        self.assertEqual(mscx_of(copy).count("<Lyrics>"), 5)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), original)
        # in place: needs a backup folder
        code, out = self.run_cli("lyrics", "import", path, txt, "--in-place", "--yes", "--no-musescore")
        self.assertEqual(code, 2)
        self.assertIn("--in-place needs --backup DIR", out)
        bak = os.path.join(self.tmp, "backup")
        code, out = self.run_cli("lyrics", "import", path, txt, "--in-place", "--backup", bak, "--yes",
                                 "--no-musescore", "--work", work)
        self.assertEqual(code, 0, out)
        with open(os.path.join(bak, "song.mscz"), "rb") as f:
            self.assertEqual(f.read(), original)
        self.assertEqual(mscx_of(path).count("<Lyrics>"), 5)
        self.assertEqual(os.listdir(work), [])             # the verified candidate is gone
        # again: an existing backup is never overwritten; the staff has lyrics now (refused); --replace replaces them
        code, out = self.run_cli("lyrics", "import", path, txt, "--in-place", "--backup", bak, "--yes",
                                 "--no-musescore", "--work", work, "--replace")
        self.assertEqual(code, 2)
        self.assertIn("song.mscz exists (a backup is never overwritten)", out)
        bak2 = os.path.join(self.tmp, "backup2")
        two = self.text("Ze-ma ko-lu ri\n", "two.txt")
        code, out = self.run_cli("lyrics", "import", path, two, "--in-place", "--backup", bak2, "--yes",
                                 "--no-musescore", "--work", work)
        self.assertEqual(code, 2)
        self.assertIn("error: staff 1 already has 5 syllables (m.1-2); --replace removes them first", out)
        self.assertFalse(os.path.exists(bak2))
        code, out = self.run_cli("lyrics", "import", path, two, "--in-place", "--backup", bak2, "--yes",
                                 "--no-musescore", "--work", work, "--replace")
        self.assertEqual(code, 0, out)
        self.assertIn("  --replace: the staff's 5 syllables are removed first", out)
        x = mscx_of(path)
        self.assertEqual(x.count("<Lyrics>"), 5)
        self.assertIn("<text>Ze</text>", x)
        self.assertNotIn("<text>Mo</text>", x)

    def test_no_vocal_staff(self):
        path = os.path.join(self.tmp, "piano.mscz")
        make_piano_score(path, [[[chord("whole", 72)]]], [[[chord("whole", 48)]]])
        with self.assertRaises(LI.LyricsImportError) as cm:
            LI.plan_import(path, "La la\n")
        self.assertIn("no vocal staff; name one with --staff N (staves: 1 (Piano), 2 (Piano))", str(cm.exception))
        plan = LI.plan_import(path, "La\n", staff=1)
        self.assertIn("staff 1 is not a vocal staff for the formatter; its lyrics are used as given", plan.warnings)
        self.assertEqual(len(plan.placements), 1)


if __name__ == "__main__":
    unittest.main()
