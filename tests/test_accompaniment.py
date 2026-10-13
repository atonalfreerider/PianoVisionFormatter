"""Note Waterfall's companion audio (pianovision.accompaniment): the pure parts, without MuseScore."""

import json
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from pianovision import accompaniment as A

from tests.scoregen import chord, make_piano_score, rest


def staff(idx, part, kind, notes=0, pitches=None, percussion=False, spans=None):
    pitches = list(pitches if pitches is not None else [60] * notes)
    return A.StaffInfo(idx=idx, part=part, kind=kind, notes=notes or len(pitches), pitches=pitches,
                       percussion=percussion, spans=spans or [])


def analysis(*staves, instruments=None):
    parts = {}
    for st in staves:
        p = parts.setdefault(st.part, A.PartInfo(id=st.part, name=f"P{st.part}",
                                                 instruments=(instruments or {}).get(st.part, [("x" + st.part, "x")])))
        p.staves.append(st.idx)
    return A.Analysis(staves=list(staves), parts=parts, end_time=10.0)


class PlanMutesTest(unittest.TestCase):
    def test_piano_and_simplified_staves_are_muted_orchestra_plays(self):
        a = analysis(staff(0, "1", A.KIND_PIANO, 5), staff(1, "1", A.KIND_PIANO, 5),
                     staff(2, "2", A.KIND_SIMPLIFIED, 3), staff(3, "3", A.KIND_ORCHESTRA, 7),
                     staff(4, "4", A.KIND_OTHER, 2))
        plan = A.plan_mutes(a, A.ScoreOptions())
        self.assertEqual(set(plan.muted), {"1", "2"})
        self.assertIn("piano part (original)", plan.muted["1"])
        self.assertIn("piano part (simplified)", plan.muted["2"])
        self.assertEqual(plan.audible_staves, [3, 4])
        self.assertEqual(plan.conflicts, [])

    def test_staves_the_formatter_takes_for_the_piano_whatever_their_names(self):
        # Didn't I Do Well: no staff is called piano, so the formatter takes the first two staves
        a = analysis(staff(0, "2", A.KIND_PIANO, 4), staff(1, "4", A.KIND_PIANO, 4),
                     staff(2, "3", A.KIND_ORCHESTRA, 4),
                     instruments={"2": [("violins", "strings.group")], "4": [("violins", "strings.group")],
                                  "3": [("violoncellos", "strings.group")]})
        plan = A.plan_mutes(a, A.ScoreOptions())
        self.assertEqual(sorted(plan.muted), ["2", "4"])
        self.assertEqual(plan.audible_staves, [2])

    def test_piano_orchestral_muted_with_an_orchestra_played_without(self):
        with_orch = analysis(staff(0, "1", A.KIND_PIANO, 5), staff(1, "2", A.KIND_ORCHESTRAL, 5),
                             staff(2, "3", A.KIND_OTHER, 5))
        alone = analysis(staff(0, "1", A.KIND_PIANO, 5), staff(1, "2", A.KIND_ORCHESTRAL, 5),
                         staff(2, "3", A.KIND_OTHER, 0))
        self.assertIn("2", A.plan_mutes(with_orch, A.ScoreOptions()).muted)
        self.assertNotIn("2", A.plan_mutes(alone, A.ScoreOptions()).muted)
        self.assertIn("2", A.plan_mutes(alone, A.ScoreOptions(piano_orchestral="mute")).muted)
        self.assertNotIn("2", A.plan_mutes(with_orch, A.ScoreOptions(piano_orchestral="play")).muted)

    def test_vocals_mute_parts_and_play_parts(self):
        a = analysis(staff(0, "1", A.KIND_PIANO, 5), staff(1, "2", A.KIND_OTHER, 5),
                     staff(2, "3", A.KIND_OTHER, 5), staff(3, "4", A.KIND_PIANO, 5),
                     instruments={"2": [("voice", "voice.vocals")]})
        plan = A.plan_mutes(a, A.ScoreOptions())
        self.assertEqual(set(plan.muted), {"1", "2", "4"})
        plan = A.plan_mutes(a, A.ScoreOptions(mute_vocals=False, mute_parts=["3"], play_parts=["4"]))
        self.assertEqual(set(plan.muted), {"1", "3"})
        self.assertEqual(plan.kept, {"4": "piano part (original)"})
        self.assertEqual(plan.audible_staves, [1, 3])
        with self.assertRaises(A.AccompanimentError):
            A.plan_mutes(a, A.ScoreOptions(mute_parts=["99"]))

    def test_a_staff_with_notes_muted_with_its_instrument_is_a_conflict(self):
        a = analysis(staff(0, "1", A.KIND_PIANO, 5), staff(1, "1", A.KIND_OTHER, 3), staff(2, "2", A.KIND_OTHER, 4))
        plan = A.plan_mutes(a, A.ScoreOptions())
        self.assertEqual(list(plan.muted), ["1"])
        self.assertEqual(len(plan.conflicts), 1)
        self.assertIn("staff 2", plan.conflicts[0])


class AnalyseTest(unittest.TestCase):
    """The formatter's classification of a real (tiny) score."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def bars(self, pitch):
        return [[[chord("whole", pitch)]], [[rest("whole")]]]

    def test_piano_simplified_orchestral_and_violin(self):
        path = make_piano_score(os.path.join(self.tmp, "s.mscz"), self.bars(72), self.bars(48), parts=[
            ("Piano-simplified", 0, [self.bars(72), self.bars(48)]),
            ("Piano-orchestral", 0, [self.bars(76), self.bars(40)]),
            ("Violin", 40, [self.bars(79)])])
        a = A.analyse(path)
        self.assertEqual([st.kind for st in a.staves], [A.KIND_PIANO, A.KIND_PIANO, A.KIND_SIMPLIFIED,
                                                         A.KIND_SIMPLIFIED, A.KIND_ORCHESTRAL, A.KIND_ORCHESTRAL,
                                                         A.KIND_ORCHESTRA])
        self.assertEqual([st.part for st in a.staves], ["1", "1", "2", "2", "3", "3", "4"])
        self.assertEqual(a.staves[6].pitches, [79])
        self.assertEqual(len(a.staves[6].spans), 1)
        on, off = a.staves[6].spans[0]
        self.assertAlmostEqual(on, 0.0)
        self.assertAlmostEqual(off, 2.0, places=2)           # a whole note at 120 bpm
        plan = A.plan_mutes(a, A.ScoreOptions())
        self.assertEqual(sorted(plan.muted), ["1", "2", "3"])
        self.assertEqual(A.onset_times(a, plan), [0.0])

    def test_no_staff_called_piano(self):
        path = make_piano_score(os.path.join(self.tmp, "s.mscz"), self.bars(72), self.bars(48),
                                parts=[("Cello", 42, [self.bars(48)])])
        with zipfile.ZipFile(path) as z:
            xml = z.read("score.mscx").decode().replace(">Piano<", ">Violins<").replace('<program value="0"/>',
                                                                                        '<program value="48"/>', 1)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="score.mscx"/>'
                                                 '</rootfiles></container>')
            z.writestr("score.mscx", xml)
        a = A.analyse(path)
        self.assertEqual([st.kind for st in a.staves], [A.KIND_PIANO, A.KIND_PIANO, A.KIND_ORCHESTRA])
        self.assertEqual(list(A.plan_mutes(a, A.ScoreOptions()).muted), ["1"])


class AudioSettingsTest(unittest.TestCase):
    DOC = {"activeSoundProfile": "MuseScore Basic",
           "aux": [{"out": {"volumeDb": 0}, "soloMuteState": {"mute": False, "solo": True}}],
           "tracks": [
               {"partId": "12", "instrumentId": "piano", "soloMuteState": {"mute": False, "solo": True},
                "out": {"volumeDb": -3, "auxSends": [{"active": True, "signalAmount": 0.3}]}},
               {"partId": "13", "instrumentId": "violins", "soloMuteState": {"mute": True, "solo": False},
                "out": {"volumeDb": -2, "auxSends": [{"active": True, "signalAmount": 0.3}]}},
               {"partId": "4", "instrumentId": "lupophone", "soloMuteState": {"mute": False, "solo": False}},
               {"partId": "999", "instrumentId": "metronome"}]}

    def edit(self, **kw):
        args = dict(tracks=[("12", "piano"), ("13", "violins"), ("4", "bassoon"), ("20", "piano")],
                    muted_parts={"12", "20"})
        args.update(kw)
        return json.loads(A.edit_audiosettings(json.dumps(self.DOC).encode(), **args))

    def test_solo_cleared_mute_exactly_the_muted_parts(self):
        d = self.edit(renames={("4", "lupophone"): "bassoon"})
        state = {(t["partId"], t["instrumentId"]): t["soloMuteState"] for t in d["tracks"]}
        self.assertEqual(state[("12", "piano")], {"mute": True, "solo": False})
        self.assertEqual(state[("13", "violins")], {"mute": False, "solo": False})     # was muted in the score
        self.assertEqual(state[("4", "bassoon")], {"mute": False, "solo": False})
        self.assertEqual(state[("999", "metronome")], {"mute": False, "solo": False})
        self.assertEqual(state[("20", "piano")], {"mute": True, "solo": False})        # added: the file lacked it
        self.assertNotIn(("4", "lupophone"), state)
        self.assertFalse(d["aux"][0]["soloMuteState"]["solo"])
        self.assertEqual(d["activeSoundProfile"], A.SOUND_PROFILE)

    def test_muted_tracks_get_an_inaudible_volume_others_keep_theirs(self):
        d = self.edit()
        out = {t["partId"]: t.get("out", {}) for t in d["tracks"]}
        self.assertEqual(out["12"]["volumeDb"], A.MUTED_VOLUME_DB)
        self.assertEqual(out["20"]["volumeDb"], A.MUTED_VOLUME_DB)
        self.assertEqual(out["13"]["volumeDb"], -2)

    def test_reverb_sends_reset_or_turned_off(self):
        d = self.edit(reset_reverb=True)
        self.assertNotIn("auxSends", next(t for t in d["tracks"] if t["partId"] == "13")["out"])
        d = self.edit()
        self.assertEqual(next(t for t in d["tracks"] if t["partId"] == "13")["out"]["auxSends"][0]["signalAmount"], 0.3)
        d = self.edit(dry=True)
        for t in d["tracks"]:
            self.assertEqual(t["out"]["auxSends"], A._DRY_SENDS)

    def test_relabel_onto_an_instrument_the_part_has_keeps_one_track(self):
        doc = {"tracks": [{"partId": "17", "instrumentId": "violoncellos", "soloMuteState": {"mute": False}},
                          {"partId": "17", "instrumentId": "strings", "soloMuteState": {"mute": False}}]}
        d = json.loads(A.edit_audiosettings(json.dumps(doc).encode(), [("17", "violoncellos")], set(),
                                            {("17", "strings"): "violoncellos"}))
        self.assertEqual([(t["partId"], t["instrumentId"]) for t in d["tracks"]], [("17", "violoncellos")])

    def test_no_audiosettings_in_the_score(self):
        d = json.loads(A.edit_audiosettings(None, [("1", "piano"), ("2", "violin")], {"1"}))
        self.assertEqual([(t["partId"], t["soloMuteState"]["mute"]) for t in d["tracks"]], [("1", True), ("2", False)])


MSCX = """<?xml version="1.0" encoding="UTF-8"?>
<museScore version="4.70">
  <Score>
    <Part id="4">
      <Staff>
        <StaffType group="pitched"><name>stdNormal</name></StaffType>
        </Staff>
      <trackName>Lupophone, Staff-3</trackName>
      <Instrument id="lupophone">
        <longName>Lupophone, Staff-3</longName>
        <instrumentId>wind.reed.heckelphone</instrumentId>
        <Channel><program value="68"/></Channel>
        </Instrument>
      </Part>
    <Part id="5">
      <Staff>
        <StaffType group="pitched"><name>stdNormal</name></StaffType>
        </Staff>
      <trackName>Clarinet in A</trackName>
      <Instrument id="a-clarinet">
        <instrumentId>wind.reed.clarinet.a</instrumentId>
        </Instrument>
      </Part>
    <Staff id="1">
      <Measure><voice><Chord><durationType>whole</durationType><Note><pitch>40</pitch><tpc>18</tpc></Note></Chord></voice></Measure>
      </Staff>
    <Staff id="2">
      <Measure><voice><Rest><durationType>measure</durationType><duration>4/4</duration></Rest></voice></Measure>
      <Measure><voice><InstrumentChange><Instrument id="a-clarinet"><instrumentId>wind.reed.clarinet.a</instrumentId></Instrument><text>To Clarinet</text></InstrumentChange><Chord><durationType>whole</durationType><Note><pitch>62</pitch><tpc>16</tpc></Note></Chord></voice></Measure>
      </Staff>
    </Score>
  </museScore>
"""


class RelabelTest(unittest.TestCase):
    def test_only_the_instrument_changes(self):
        r = A.Relabel(part="4", old="lupophone", new="bassoon", musicxml="wind.reed.bassoon")
        out = A.relabel_mscx(MSCX.encode(), [r]).decode()
        self.assertEqual(out, MSCX.replace('<Instrument id="lupophone">', '<Instrument id="bassoon">')
                         .replace("wind.reed.heckelphone", "wind.reed.bassoon"))
        ins = ET.fromstring(out.encode()).find("Score/Part/Instrument")
        self.assertEqual((ins.get("id"), ins.findtext("instrumentId")), ("bassoon", "wind.reed.bassoon"))
        self.assertEqual(ins.findtext("longName"), "Lupophone, Staff-3")      # names stay; notes stay

    def test_instrument_changes_of_the_part_follow(self):
        r = A.Relabel(part="5", old="a-clarinet", new="bb-clarinet", musicxml="wind.reed.clarinet.bflat")
        out = A.relabel_mscx(MSCX.encode(), [r], {"4": [0], "5": [1]}).decode()
        root = ET.fromstring(out.encode())
        self.assertEqual(root.find("Score/Part[@id='5']/Instrument").get("id"), "bb-clarinet")
        ic = root.find(".//InstrumentChange/Instrument")
        self.assertEqual((ic.get("id"), ic.findtext("instrumentId")), ("bb-clarinet", "wind.reed.clarinet.bflat"))
        self.assertEqual(root.find("Score/Part[@id='4']/Instrument").get("id"), "lupophone")
        # without the staff map only the part's own instrument changes
        out = A.relabel_mscx(MSCX.encode(), [r]).decode()
        self.assertEqual(ET.fromstring(out.encode()).find(".//InstrumentChange/Instrument").get("id"), "a-clarinet")

    def test_a_relabel_that_does_not_fit_the_score_fails(self):
        with self.assertRaises(A.AccompanimentError):
            A.relabel_mscx(MSCX.encode(), [A.Relabel(part="4", old="harp", new="violoncellos", musicxml="x")])
        with self.assertRaises(A.AccompanimentError):
            A.relabel_mscx(MSCX.encode(), [A.Relabel(part="9", old="harp", new="violoncellos", musicxml="x")])

    def test_options(self):
        o = A.score_options({"mute_vocals": True}, {"mute_vocals": False, "_why": "x", "relabel": [
            {"part": 4, "from": "lupophone", "to": "bassoon", "instrumentId": "wind.reed.bassoon"}]})
        self.assertFalse(o.mute_vocals)
        self.assertEqual(o.relabel[0].part, "4")
        with self.assertRaises(A.AccompanimentError):
            A.score_options({}, {"mute_vocal": False})
        with self.assertRaises(A.AccompanimentError):
            A.score_options({}, {"piano_orchestral": "sometimes"})


class ProbeTest(unittest.TestCase):
    def test_slots_and_probe_score(self):
        a = analysis(staff(0, "4", A.KIND_ORCHESTRA, pitches=[40, 45, 45, 50]),
                     staff(1, "5", A.KIND_ORCHESTRA, pitches=[62, 64]),
                     instruments={"4": [("lupophone", "wind.reed.heckelphone")],
                                  "5": [("a-clarinet", "wind.reed.clarinet.a")]})
        plan = A.plan_mutes(a, A.ScoreOptions())
        r = A.Relabel(part="4", old="lupophone", new="bassoon", musicxml="wind.reed.bassoon")
        slots = A.probe_slots(a, plan, [r])
        self.assertEqual([(s.part, s.instrument, s.pitch, s.role) for s in slots],
                         [("4", "bassoon", 45, "median"), ("4", "bassoon", 40, "lowest"),
                          ("4", "bassoon", 50, "highest"), ("5", "a-clarinet", 62, "median"),
                          ("5", "a-clarinet", 64, "highest")])
        self.assertEqual([s.start for s in slots], [2.0, 8.0, 14.0, 20.0, 26.0])
        root = ET.fromstring(A.probe_mscx(MSCX.encode(), a, slots))
        staves = root.findall("Score/Staff")
        self.assertEqual([len(s.findall("Measure")) for s in staves], [17, 17])
        pitches = [[(m, int(c.findtext("Note/pitch"))) for m, meas in enumerate(s.findall("Measure"))
                    for c in meas.iter("Chord")] for s in staves]
        self.assertEqual(pitches, [[(1, 45), (4, 40), (7, 50)], [(10, 62), (13, 64)]])
        self.assertIsNotNone(staves[0].find("Measure/voice/Tempo"))

    def test_verdicts(self):
        w = 0.05
        n = int(30 / w)
        slots = [A.Slot(part=str(k), instrument="i", musicxml="", staff=0, pitch=60, change=False,
                        start=2.0 + 6 * k) for k in range(4)]

        def levels(per_slot):
            out = [-200.0] * n
            for s, v in zip(slots, per_slot):
                for i in range(int(s.start / w), int((s.start + 2) / w)):
                    out[i] = v
            return out
        muse = levels([-40, -30, -200, -200])
        basic = levels([-28, -30, -30, -200])
        diff = levels([-27, -200, -30, -200])
        v = [x["verdict"] for x in A.probe_verdicts(slots, muse, basic, diff, w)]
        self.assertEqual(v, ["muse", "basic", "silent-muse", "silent"])
        fb, silent = A.summarize_probe(A.probe_verdicts(slots, muse, basic, diff, w))
        self.assertEqual([x["part"] for x in fb], ["1", "3"])
        self.assertEqual([x["part"] for x in silent], ["2"])

    def test_attack(self):
        lv = [-200.0] * 1000
        for i in range(400, 600):            # 5 ms blocks: the note starts at 2.0 s, gets loud at 2.05 s
            lv[i] = -60 if i < 410 else -30
        self.assertEqual(A.attack_ms(lv, 2.0, 0.005), 50.0)
        self.assertIsNone(A.attack_ms([-200.0] * 1000, 2.0, 0.005))


class NanTest(unittest.TestCase):
    def test_bursts(self):
        self.assertEqual(A.nan_bursts([]), [])
        b = A.nan_bursts([1.2, 1.3, 17.4, 17.5, 17.6, 40.0])
        self.assertEqual([(round(x, 1), round(y, 1)) for x, y in b], [(1.2, 1.4), (17.4, 17.7), (40.0, 40.1)])


class CacheTest(unittest.TestCase):
    LIBS = {"Muse Strings": {"guid": "a", "instruments": [["Violins 1", "0.0"]]},
            "Muse Brass": {"guid": "b", "instruments": [["Tuba", "0.0"]]}}

    def test_libraries(self):
        more = dict(self.LIBS, **{"Muse Percussion": {"guid": "c", "instruments": [["Timpani", "0.0"]]}})
        self.assertTrue(A.libraries_current(self.LIBS, self.LIBS, had_fallback=True))
        # a new library matters only when some instrument fell back to the basic sounds
        self.assertTrue(A.libraries_current(self.LIBS, more, had_fallback=False))
        self.assertFalse(A.libraries_current(self.LIBS, more, had_fallback=True))
        # a library it used was updated or removed
        changed = dict(self.LIBS, **{"Muse Brass": {"guid": "b", "instruments": [["Tuba", "1.0"]]}})
        self.assertFalse(A.libraries_current(self.LIBS, changed, had_fallback=False))
        self.assertFalse(A.libraries_current(self.LIBS, {"Muse Strings": self.LIBS["Muse Strings"]}, False))
        self.assertFalse(A.libraries_current(None, self.LIBS, False))

    def test_key(self):
        base = ("sha", {"mute_vocals": True}, {"muted": {"1": ["piano"]}}, {"mscore": "4.7.5"}, {"bitrate": 256})
        k = A.cache_key(*base)
        self.assertEqual(k, A.cache_key(*base))
        for i, other in enumerate(["sha2", {"mute_vocals": False}, {"muted": {}}, {"mscore": "4.8"},
                                   {"bitrate": 192}]):
            changed = list(base)
            changed[i] = other
            self.assertNotEqual(k, A.cache_key(*changed), i)

    def test_muse_libraries_read_only(self):
        import sqlite3
        tmp = tempfile.mkdtemp()
        try:
            sounds, sampler = os.path.join(tmp, "Muse Sounds"), os.path.join(tmp, "MuseSampler")
            os.makedirs(sounds)
            os.makedirs(sampler)
            with open(os.path.join(sampler, ".config"), "w") as f:
                f.write(sounds)
            con = sqlite3.connect(os.path.join(sounds, ".instruments"))
            con.executescript("create table packages (key integer primary key, guid text, path text, name text);"
                              "create table instruments (id integer primary key, path text, state integer, "
                              "version text, package_key integer, name text, abbreviation text);"
                              "insert into packages values (1, 'g', 'Muse Strings', 'Muse Strings');"
                              "insert into instruments values (91, 'Muse Strings/Violins 1', 1, '0.0', 1, ' Violins 1', 'Vln');"
                              "insert into instruments values (92, 'Muse Strings/Violins 2', 0, '0.0', 1, 'Violins 2', 'Vln');")
            con.commit()
            con.close()
            self.assertEqual(A.muse_libraries(sampler),
                             {"Muse Strings": {"guid": "g", "instruments": [["Violins 1", "0.0"]]}})
            self.assertEqual(A.muse_libraries(os.path.join(tmp, "nowhere")), {})
        finally:
            shutil.rmtree(tmp)


class ListenChecksTest(unittest.TestCase):
    def test_gaps_and_bleed(self):
        a = analysis(staff(0, "1", A.KIND_PIANO, 1, spans=[(10.0, 16.0)]),
                     staff(1, "2", A.KIND_ORCHESTRA, 2, spans=[(0.0, 4.0), (20.0, 24.0)]))
        plan = A.plan_mutes(a, A.ScoreOptions())
        w = 0.1
        rms = [-30.0] * 40 + [-200.0] * 160 + [-30.0] * 40 + [-200.0] * 20
        rms[22] = -200.0                                     # 2.2-2.3 s: too short to count
        ck = A.listen_checks(a, plan, rms, w)
        self.assertEqual(ck["silent_gaps"], [])
        self.assertEqual(ck["piano_only_stretches"], 1)
        self.assertEqual(ck["bleed_suspects"], [])
        rms[30:40] = [-200.0] * 10                           # the orchestra is silent for its last second
        rms[120:140] = [-35.0] * 20                          # something plays while only the piano has notes
        ck = A.listen_checks(a, plan, rms, w)
        self.assertEqual(ck["silent_gaps"], [{"from": 3.0, "to": 4.3}])     # notes ring 0.3 s after their end
        self.assertEqual(len(ck["bleed_suspects"]), 1)


class EncodeTest(unittest.TestCase):
    def test_codec_delay_cut_and_no_gapless_header(self):
        args = A.encode_args("in.wav", "out.mp3", 4.5, 256, "T", "", "", end=120.0)
        af = args[args.index("-af") + 1]
        self.assertTrue(af.startswith(f"atrim=start_sample={A.MP3_CODEC_DELAY}:end=120.000,asetpts=PTS-STARTPTS,"))
        self.assertIn("volume=4.500dB", af)
        self.assertEqual(args[args.index("-write_xing") + 1], "0")
        self.assertEqual(args[args.index("-b:a") + 1], "256k")
        self.assertEqual(A.MP3_CODEC_DELAY, 1105)
        self.assertEqual(args[-1], "out.mp3")


class OptionsFileTest(unittest.TestCase):
    def test_the_checked_in_options_parse(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), A.OPTIONS_NAME)
        defaults, scores = A.load_options(path)
        self.assertTrue(scores)
        for rel, per in scores.items():
            o = A.score_options(defaults, per)
            for r in o.relabel:
                self.assertTrue(r.part and r.old and r.new and r.musicxml, rel)
        self.assertEqual(A.score_options(defaults, scores["Pop/Shape of You.mscz"]).play_parts, ["3", "9"])


class SettingsTest(unittest.TestCase):
    def test_toml_section_and_relative_paths(self):
        tmp = tempfile.mkdtemp()
        try:
            cfg = os.path.join(tmp, "pianovision.toml")
            with open(cfg, "w") as f:
                f.write('[library]\nscores = "x"\n[accompaniment]\nbitrate = 320\nout_dir = "/music"\n'
                        'options = "acc.json"\n')
            s = A.load_settings(cfg, work_dir="/scratch")
            self.assertEqual((s.bitrate, s.out_dir, s.work_dir), (320, "/music", "/scratch"))
            self.assertEqual(s.options, os.path.join(tmp, "acc.json"))
            self.assertEqual(s.timing_script, os.path.join(tmp, "tools", "accompaniment_timing.py"))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
