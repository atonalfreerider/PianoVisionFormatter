"""A score's repeat structure checked and fixed in a copy (pianovision.scorefix)."""

import contextlib
import difflib
import io
import json
import os
import tempfile
import unittest
import zipfile

from pianovision import scorefix as SF
from pianovision.mscx import read_score

from .scoregen import E


def volta_start(span, endings="99", text="99", closed=False):
    kids = [E("endHookType", text="1")] if closed else []
    kids += [E("beginText", text=text), E("endings", text=endings)]
    return E("Spanner", E("Volta", *kids), E("next", E("location", E("measures", text=str(span)))), type="Volta")


def volta_end(span):
    return E("Spanner", E("prev", E("location", E("measures", text=str(-span)))), type="Volta")


def make_score(path, n=8, start_repeat=(), end_repeat=None, voltas=(), tempo=120, irregular_first=False,
               voice_part=False):
    """``n`` 4/4 measures, measure k (1-based) a whole note of pitch 59 + k; ``end_repeat``: {measure: count};
    ``voltas``: (first measure, span, endings, text, closed).  Laid out like MuseScore 4 writes (one element per
    line, closing tags indented like the children).  ``voice_part``: a vocal staff above the piano."""
    end_repeat = end_repeat or {}
    starts = {v[0]: v for v in voltas}
    ends = {v[0] + v[1]: v for v in voltas}

    def staff(sid, top, base):
        kids = []
        if top:
            kids.append(E("VBox", E("height", text="10"), E("Text", E("style", text="title"), E("text", text="T"))))
        for k in range(1, n + 1):
            mk = []
            if k == 1 and irregular_first:
                mk.append(E("irregular", text="1"))
            if k in start_repeat:
                mk.append(E("startRepeat"))
            if k in end_repeat:
                mk.append(E("endRepeat", text=str(end_repeat[k])))
            v = []
            if k == 1:
                v += [E("KeySig", E("concertKey", text="0")), E("TimeSig", E("sigN", text="4"), E("sigD", text="4"))]
                if tempo and top:
                    v.append(E("Tempo", E("tempo", text=f"{tempo / 60:g}"), E("followText", text="1"),
                               E("text", text=f"<sym>metNoteQuarterUp</sym> = {tempo}")))
            if top and k in ends:
                v.append(volta_end(ends[k][1]))
            if top and k in starts:
                s = starts[k]
                v.append(volta_start(s[1], s[2], s[3], s[4]))
            v.append(E("Chord", E("durationType", text="whole"), E("Note", E("pitch", text=str(base + k)),
                                                                     E("tpc", text="14"))))
            mk.append(E("voice", *v))
            kids.append(E("Measure", *mk))
        return E("Staff", *kids, id=str(sid))

    parts, staves = [], []
    sid = 1
    if voice_part:
        parts.append(E("Part", E("Staff", E("StaffType", E("name", text="stdNormal"), group="pitched"), id="1"),
                       E("trackName", text="Voice"),
                       E("Instrument", E("longName", text="Voice"), E("trackName", text="Voice"),
                         E("instrumentId", text="voice.vocals"), E("Channel", E("program", value="53"))), id="1"))
        staves.append(staff(1, True, 71))
        sid = 2
    parts.append(E("Part", E("Staff", E("StaffType", E("name", text="stdNormal"), group="pitched"), id=str(sid)),
                   E("trackName", text="Piano"),
                   E("Instrument", E("longName", text="Piano"), E("trackName", text="Piano"),
                     E("instrumentId", text="keyboard.piano"), E("Channel", E("program", value="0"))),
                   id=str(len(parts) + 1)))
    staves.append(staff(sid, not voice_part, 59))
    score = E("Score", E("Division", text="480"), *parts, *staves)
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<museScore version="4.70">\n' + "\n".join(
        E("programVersion", text="4.7.5").lines("  ") + score.lines("  ")) + "\n  </museScore>\n"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml", '<?xml version="1.0" encoding="UTF-8"?><container><rootfiles>'
                   '<rootfile full-path="score.mscx"/></rootfiles></container>')
        z.writestr("score.mscx", xml)
    return path


def rb(path):
    with open(path, "rb") as f:
        return f.read()


def wb(path, data):
    with open(path, "wb") as f:
        f.write(data)


def mscx_of(path):
    with zipfile.ZipFile(path) as z:
        return z.read("score.mscx")


class Ranges(unittest.TestCase):
    def test_runs(self):
        self.assertEqual(SF.ranges([1, 2, 3, 7, 8, 3]), "1-3, 7-8, 3")
        self.assertEqual(SF.ranges([5]), "5")
        self.assertEqual(SF.ranges([]), "")
        self.assertEqual(SF.ranges([4, 4, 5]), "4, 4-5")


class UnreachableVoltas(unittest.TestCase):
    def test_endings_outside_the_passes(self):
        reps = [{"start": 2, "end": 5, "count": 2}, {"start": 9, "end": 10, "count": 3}]
        voltas = [{"start": 4, "end": 5, "endings": [99]}, {"start": 6, "end": 6, "endings": [2]},
                  {"start": 10, "end": 10, "endings": [1, 2]}, {"start": 11, "end": 11, "endings": [4]}]
        bad = SF.unreachable_voltas(voltas, reps)
        self.assertEqual([b["start"] for b in bad], [4, 11])        # 99 of 2 passes, 4 of 3
        self.assertEqual(bad[0]["passes"], [1, 2])
        self.assertEqual(bad[1]["passes"], [1, 2, 3])

    def test_volta_after_the_last_repeat(self):
        bad = SF.unreachable_voltas([{"start": 7, "end": 7, "endings": [2]}], [{"start": 1, "end": 3, "count": 2}])
        self.assertEqual([b["start"] for b in bad], [7])


class Structure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def broken(self):
        """|: 2 3 [4 5 :|] [6] 7 8 with both endings numbered 99 (a PDF import's leftovers)."""
        return make_score(self.path("broken.mscz"), start_repeat=(2,), end_repeat={5: 2},
                          voltas=[(4, 2, "99", "99", True), (6, 1, "99", "99", False)])

    def test_report_of_unplayable_voltas(self):
        st = SF.structure(read_score(self.broken()))
        self.assertEqual([v["start"] for v in st["unreachable_voltas"]], [4, 6])
        self.assertEqual(st["voltas"][0]["end"], 5)
        self.assertIn("4", st["never_played"])
        self.assertEqual(st["repeats"], [{"start": 2, "end": 5, "count": 2}])

    def test_fixed_voltas_play_both_endings(self):
        src = self.broken()
        new, log = SF.apply_fixes_mscz(rb(src), [
            {"op": "volta", "measure": 4, "endings": [1], "text": "1."},
            {"op": "volta", "measure": 6, "endings": [2], "text": "2."}])
        out = self.path("fixed.mscz")
        wb(out, new)
        st = SF.structure(read_score(out))
        self.assertEqual(st["play_order_text"], "1-5, 2-3, 6-8")
        self.assertEqual(st["unreachable_voltas"], [])
        self.assertEqual(len(log), 2)

    def test_volta_extended_to_the_end_repeat(self):
        src = make_score(self.path("short.mscz"), start_repeat=(2,), end_repeat={5: 2},
                         voltas=[(4, 1, "1", "1.", True), (6, 1, "2", "2.", False)])
        new, _ = SF.apply_fixes_mscz(rb(src), [
            {"op": "volta", "measure": 4, "endings": [1], "text": "1.", "measures": 2}])
        out = self.path("long.mscz")
        wb(out, new)
        sc = read_score(out)
        self.assertEqual([(v["start"], v["end"]) for v in SF.structure(sc)["voltas"]], [(4, 5), (6, 6)])
        self.assertEqual(SF.ranges(SF.play_order(sc)), "1-5, 2-3, 6-8")
        # the end element moved: one fewer in measure 5, one more in measure 6
        data = mscx_of(out).decode()
        self.assertEqual(data.count("<measures>-2</measures>"), 1)
        self.assertEqual(data.count("<measures>-1</measures>"), 1)   # the second ending's own end

    def test_repeat_count_and_third_ending(self):
        src = make_score(self.path("vamp.mscz"), start_repeat=(2,), end_repeat={3: 2},
                         voltas=[(3, 1, "99", "99", True), (4, 1, "99", "99", False)])
        new, _ = SF.apply_fixes_mscz(rb(src), [
            {"op": "repeat_count", "measure": 3, "count": 3},
            {"op": "volta", "measure": 3, "endings": [1, 2], "text": "1., 2."},
            {"op": "volta", "measure": 4, "endings": [3], "text": "3."}])
        out = self.path("vamp3.mscz")
        wb(out, new)
        self.assertEqual(SF.ranges(SF.play_order(read_score(out))), "1-3, 2-3, 2, 4-8")

    def test_ds_al_coda(self):
        src = make_score(self.path("plain.mscz"))
        new, log = SF.apply_fixes_mscz(rb(src), [
            {"op": "marker", "measure": 2, "kind": "segno"},
            {"op": "marker", "measure": 3, "kind": "tocoda"},
            {"op": "jump", "measure": 5, "kind": "ds_al_coda"},
            {"op": "marker", "measure": 7, "kind": "coda"}])
        out = self.path("ds.mscz")
        wb(out, new)
        st = SF.structure(read_score(out))
        self.assertEqual(st["play_order_text"], "1-5, 2-3, 7-8")
        self.assertEqual([(m["measure"], m["label"]) for m in st["markers"]], [(2, "segno"), (3, "coda"), (7, "codab")])
        self.assertEqual(st["jumps"], [{"measure": 5, "jumpTo": "segno", "playUntil": "coda", "continueAt": "codab"}])

    def test_ds_al_coda_takes_the_last_ending_and_keeps_a_later_repeat(self):
        """The song's shape: |: verse [1. :|] [2.] chorus, D.S. al Coda; the coda's own repeat still plays."""
        src = make_score(self.path("song.mscz"), n=12, start_repeat=(2, 10), end_repeat={4: 2, 11: 2},
                         voltas=[(4, 1, "1", "1.", True), (5, 1, "2", "2.", False)])
        new, _ = SF.apply_fixes_mscz(rb(src), [
            {"op": "marker", "measure": 2, "kind": "segno"},
            {"op": "marker", "measure": 6, "kind": "tocoda"},
            {"op": "jump", "measure": 7, "kind": "ds_al_coda"},
            {"op": "marker", "measure": 8, "kind": "coda"}])
        out = self.path("song_ds.mscz")
        wb(out, new)
        self.assertEqual(SF.ranges(SF.play_order(read_score(out))), "1-4, 2-3, 5-7, 2-3, 5-6, 8-11, 10-12")

    def test_tempo(self):
        src = make_score(self.path("t.mscz"), tempo=120)
        new, _ = SF.apply_fixes_mscz(rb(src), [{"op": "tempo", "bpm": 126}])
        out = self.path("t126.mscz")
        wb(out, new)
        data = mscx_of(out).decode()
        self.assertIn("<tempo>2.1</tempo>", data)
        self.assertIn("= 126</text>", data)
        ref = SF.score_reference(out)
        self.assertAlmostEqual(ref["measures"][1]["start"], 4 * 60 / 126, places=3)

    def test_everything_else_byte_for_byte(self):
        src = self.broken()
        before = mscx_of(src)
        new, _ = SF.apply_fixes_mscz(rb(src), [
            {"op": "volta", "measure": 4, "endings": [1], "text": "1."},
            {"op": "marker", "measure": 2, "kind": "segno"}])
        out = self.path("bytes.mscz")
        wb(out, new)
        after = mscx_of(out)
        a, b = before.decode().split("\n"), after.decode().split("\n")
        diff = list(difflib.ndiff(a, b))
        removed = [ln[2:] for ln in diff if ln.startswith("- ")]
        added = [ln[2:] for ln in diff if ln.startswith("+ ")]
        self.assertEqual([ln.strip() for ln in removed], ["<beginText>99</beginText>", "<endings>99</endings>"])
        self.assertEqual(len(added), 2 + 6)      # the two values and the six lines of the marker
        self.assertTrue(all(ln.startswith("        ") for ln in added))

    def test_unknown_measure_or_missing_volta(self):
        src = make_score(self.path("x.mscz"))
        with self.assertRaises(SF.FixError):
            SF.apply_fixes_mscz(rb(src), [{"op": "volta", "measure": 3, "endings": [1], "text": "1."}])
        with self.assertRaises(SF.FixError):
            SF.apply_fixes_mscz(rb(src), [{"op": "repeat_count", "measure": 99, "count": 3}])

    def test_displayed_numbers_skip_an_excluded_measure(self):
        sc = read_score(make_score(self.path("pickup.mscz"), irregular_first=True))
        d = SF.displayed_numbers(sc)
        self.assertEqual((d[1], d[2], d[3], d[8]), (1, 1, 2, 7))

    def test_reference_parts_and_measures(self):
        out = make_score(self.path("vp.mscz"), n=4, voice_part=True, start_repeat=(2,), end_repeat={3: 2})
        ref = SF.score_reference(out)
        self.assertEqual([m["number"] for m in ref["measures"]], [1, 2, 3, 2, 3, 4])
        self.assertEqual(sorted({n[4] for n in ref["notes"]}), ["p", "v"])
        self.assertEqual(sum(1 for n in ref["notes"] if n[4] == "v"), 6)


class Command(unittest.TestCase):
    """``python3 -m pianovision scorefix``: a copy, or the score itself only with a backup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.score = make_score(os.path.join(self.tmp.name, "s.mscz"), start_repeat=(2,), end_repeat={5: 2},
                                voltas=[(4, 2, "99", "99", True), (6, 1, "99", "99", False)])
        self.fixes = os.path.join(self.tmp.name, "fixes.json")
        with open(self.fixes, "w") as f:
            json.dump([{"op": "volta", "measure": 4, "endings": [1], "text": "1."},
                       {"op": "volta", "measure": 6, "endings": [2], "text": "2."}], f)
        self.original = rb(self.score)

    def run_cli(self, *argv):
        from pianovision.cli import main
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["scorefix", *argv])
        return code, out.getvalue()

    def test_copy_leaves_the_score(self):
        out = os.path.join(self.tmp.name, "copy.mscz")
        code, text = self.run_cli("apply", self.score, self.fixes, "--out", out)
        self.assertEqual(code, 0)
        self.assertEqual(rb(self.score), self.original)
        self.assertIn("1-5, 2-3, 6-8", text)

    def test_refuses_to_overwrite_without_a_backup(self):
        self.assertEqual(self.run_cli("apply", self.score, self.fixes, "--out", self.score)[0], 2)
        self.assertEqual(self.run_cli("apply", self.score, self.fixes, "--in-place", "--yes")[0], 2)
        self.assertEqual(rb(self.score), self.original)

    def test_in_place_with_a_backup(self):
        bak = os.path.join(self.tmp.name, "backup")
        code, _ = self.run_cli("apply", self.score, self.fixes, "--in-place", "--backup", bak, "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(rb(os.path.join(bak, "s.mscz")), self.original)
        self.assertEqual(SF.ranges(SF.play_order(read_score(self.score))), "1-5, 2-3, 6-8")
        # a second run does not overwrite the backup
        self.assertEqual(self.run_cli("apply", self.score, self.fixes, "--in-place", "--backup", bak, "--yes")[0], 2)

    def test_report(self):
        js = os.path.join(self.tmp.name, "r.json")
        code, text = self.run_cli("report", self.score, "--json", js)
        self.assertEqual(code, 0)
        self.assertIn("no pass plays it", text)
        with open(js) as f:
            self.assertEqual(len(json.load(f)["unreachable_voltas"]), 2)


if __name__ == "__main__":
    unittest.main()
