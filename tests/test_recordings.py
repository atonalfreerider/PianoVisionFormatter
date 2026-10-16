"""Companion audio from recordings (pianovision.recordings): inventory, candidates, the judgment (mocked), the links
file, the outputs.  No audio is analysed here (tests/test_recording_align.py does that, with numpy)."""

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

from pianovision import recordings as R


def af(rel, duration=200.0, size=1000, **tags):
    return R.AudioFile(rel, size, 0.0, duration, tags)


def answers(pick, pick_rev, same, choice=None, choice_rev=None):
    """A System One answer set: ``pick`` / ``pick_rev`` probabilities by option, ``same`` the Nouls in order."""
    out = {"pick": {"type": "choice", "probabilities": pick, "choice": choice or max(pick, key=pick.get)},
           "pick_reversed": {"type": "choice", "probabilities": pick_rev,
                             "choice": choice_rev or max(pick_rev, key=pick_rev.get)}}
    for i, p in enumerate(same):
        out[f"same_{i}"] = {"type": "noul", "noul": p}
    return out


class InventoryTest(unittest.TestCase):
    def test_classify_name(self):
        self.assertEqual(R.classify_name("Song_no_piano_split_by_lalalai"), ("Song", "no_piano", ""))
        self.assertEqual(R.classify_name("Song_piano_split_by_lalalai"), ("Song", "piano", ""))
        self.assertEqual(R.classify_name("Song_no_piano_split_by_lalalai_retimed"), ("Song", "no_piano", "retimed"))
        self.assertEqual(R.classify_name("Rach2-1-Orchestra_mixdown"), ("Rach2-1", "full", "mixdown"))
        self.assertEqual(R.classify_name("rach3-1-retime"), ("rach3-1", "full", "retimed"))
        self.assertEqual(R.classify_name("Fountain of Sorrow (Remastered)"), ("Fountain of Sorrow (Remastered)",
                                                                              "full", ""))

    def test_grouping(self):
        files = [
            af("B/Rach2/I/Concerto 2： I. Moderato.mp3", 673.2),
            af("B/Rach2/I/Concerto 2： I. Moderato_no_piano_split_by_lalalai.mp3", 673.2),
            af("B/Rach2/I/Concerto 2： I. Moderato_piano_split_by_lalalai.mp3", 673.2),
            # another stem named differently: its title tag says what it was split from
            af("B/Rach2/I/R2-I_no_piano_split_by_lalalai.mp3", 673.2,
               title="Concerto 2： I. Moderato_no_piano_(Split_by_LALAL.AI)"),
            af("B/Rach2/I/Rach2-1-Orchestra_mixdown.wav", 673.2),
            af("Concerto 2： I. Moderato_no_piano_split_by_lalalai_retimed.wav", 714.6),   # elsewhere, by name
            af("O/Stem only_no_piano_split_by_lalalai.wav", 100.0),
            af("O/Stem only_piano_split_by_lalalai.wav", 100.0),
            af("Z/Song.mp3", 200.0, 5),
            af("Z/full/Song.mp3", 200.0, 5),                                               # a copy
            af("X/mosz-retime.wav", 1000.0),
        ]
        recs = {r.key: r for r in R.group_recordings(files)}
        self.assertEqual(sorted(recs), ["B/Rach2/I/Concerto 2： I. Moderato.mp3",
                                        "O/Stem only_no_piano_split_by_lalalai.wav", "X/mosz-retime.wav", "Z/Song.mp3"])
        r = recs["B/Rach2/I/Concerto 2： I. Moderato.mp3"]
        self.assertEqual(sorted(r.files), ["full", "no_piano", "piano"])
        stem = "B/Rach2/I/Concerto 2： I. Moderato_no_piano_split_by_lalalai.mp3"
        self.assertEqual(r.variants(), {"vocals": R.SEPARATED, "full": "B/Rach2/I/Concerto 2： I. Moderato.mp3",
                                        "no_piano": stem})
        self.assertEqual(r.default_variant(R.ROUTE_VOCALS), "vocals")
        self.assertIsNone(r.default_variant(R.ROUTE_GENERATED))
        self.assertEqual(r.source(), "B/Rach2/I/Concerto 2： I. Moderato.mp3")
        whats = sorted(w for _rel, w in r.others)
        self.assertEqual(whats, ["another no_piano stem", "mixdown", "retimed no_piano stem"])
        stem = recs["O/Stem only_no_piano_split_by_lalalai.wav"]
        self.assertEqual(sorted(stem.files), ["no_piano", "piano"])
        self.assertIsNone(stem.default_variant(R.ROUTE_VOCALS))         # no full mix to separate, no vocals stem
        self.assertEqual(stem.source(), "O/Stem only_no_piano_split_by_lalalai.wav")
        self.assertEqual(recs["Z/Song.mp3"].others, [("Z/full/Song.mp3", "copy of Z/Song.mp3")])
        self.assertEqual(recs["X/mosz-retime.wav"].derived, "retimed")

    def test_probe_requests_no_lyrics(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout=json.dumps({"format": {"duration": "12.5", "tags": {
                "TITLE": "A", "artist": "B", "lyrics": "never"}}}))
            dur, tags = R.probe("/x.mp3")
        args = run.call_args[0][0]
        self.assertIn("format=duration:format_tags=title,artist,album,album_artist,composer", args)
        self.assertEqual((dur, tags), (12.5, {"title": "A", "artist": "B"}))


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        self.songs = [R.Song("70s/fountain-of-sorrow.mscz", "brow_Fountain_of_Sorrow.json", "Fountain of Sorrow",
                             "Browne Jackson"),
                      R.Song("Film/Bond/Nobody_Does_It_Better_Live.mscz", "haml_Nobody.json",
                             "Nobody Does It Better - The Spy Who Loved Me Soundtrack", "Hamlisch"),
                      R.Song("Rachmaninoff/Rach-3/I/Piano_Concerto_3_First-master.mscz", "rach_3_1.json",
                             "Piano Concerto No 3 - First Movement", "Rachmaninoff")]
        self.recs = R.group_recordings([
            af("Fountain of Sorrow (Remastered).mp3"), af("Late for the Sky (Remastered).mp3"),
            af("soundtrack/output.mp3"), af("soundtrack/22 - All In A Day's Work.mp3"),
            af("Ohlsson/Rach3-1/01 - Rachmaninoff Piano Concerto No. 3 in D minor, Op. 30 - I. Allegro.wav",
               artist="Garrick Ohlsson"),
            af("Bronfman/Rach2/I/Piano Concerto No. 2 in C Minor, Op. 18： I. Moderato.mp3"),
        ])

    def test_the_right_recording_first(self):
        c = R.propose_candidates(self.songs, self.recs)
        self.assertEqual(c["70s/fountain-of-sorrow.mscz"][0][0].key, "Fountain of Sorrow (Remastered).mp3")
        self.assertNotIn("Late for the Sky (Remastered).mp3", [r.key for r, _ in c["70s/fountain-of-sorrow.mscz"]])
        top = [r.key for r, _ in c["Rachmaninoff/Rach-3/I/Piano_Concerto_3_First-master.mscz"]]
        self.assertTrue(any(k.startswith("Ohlsson/Rach3-1/") for k in top))
        self.assertIn("Bronfman/Rach2/I/Piano Concerto No. 2 in C Minor, Op. 18： I. Moderato.mp3", top)

    def test_a_category_word_alone_proposes_nothing(self):
        # "soundtrack" names a kind of music, not a piece: output.mp3 in soundtrack/ is no candidate
        c = R.propose_candidates(self.songs, self.recs)
        self.assertEqual(c["Film/Bond/Nobody_Does_It_Better_Live.mscz"], [])

    def test_text_score_prefix_words(self):
        idf = {}
        self.assertGreater(R.text_score({"rachmaninov"}, {"rachmaninoff"}, idf), 0.5)
        self.assertGreater(R.text_score({"rach", "3"}, {"rachmaninoff", "3"}, idf), 0.5)
        self.assertEqual(R.text_score({"love"}, {"sky"}, idf), 0.0)


class JudgmentTest(unittest.TestCase):
    def setUp(self):
        self.s = R.Settings()
        self.a = R.Recording("A.mp3", {"full": "A.mp3"}, tags={"title": "A"})
        self.b = R.Recording("B/b.wav", {"full": "B/b.wav", "no_piano": "B/b_no_piano_split_by_lalalai.wav"})
        self.c = R.Recording("C.mp3", {"full": "C.mp3"})
        self.song = R.Song("x.mscz", "x.json", "X", "Y")

    def test_questions(self):
        q = R.match_questions(self.song, [self.a, self.b, self.c])
        self.assertEqual(list(q["pick"]["criteria"]), ["A.mp3", "B/b.wav", "C.mp3", R.NONE])
        self.assertEqual(list(q["pick_reversed"]["criteria"]), ["C.mp3", "B/b.wav", "A.mp3", R.NONE])
        self.assertEqual(q["pick"]["criteria"]["A.mp3"], {"title": "A"})
        self.assertEqual([k for k in q if k.startswith("same_")], ["same_0", "same_1", "same_2"])
        self.assertIn("`recordings[1]`", q["same_1"]["instructions"]["question"])
        state = R.match_state(self.song, [self.a, self.b, self.c])
        self.assertEqual(state["recordings"][1], {"file": "B/b.wav"})

    def test_confident_pick_is_linked(self):
        v = R.decide(answers({"A.mp3": 0.97, "B/b.wav": 0.01, "C.mp3": 0.01, "none": 0.01},
                             {"A.mp3": 0.95, "B/b.wav": 0.02, "C.mp3": 0.02, "none": 0.01}, [0.95, 0.1, 0.05]),
                     [self.a, self.b, self.c], self.s)
        self.assertEqual((v["status"], v["recording"]), (R.STATUS_LINKED, "A.mp3"))
        self.assertGreater(v["confidence"], 0.9)
        self.assertEqual(v["alternatives"], [])

    def test_probability_spread_over_valid_recordings_is_linked(self):
        # two recordings of the same movement: the orders disagree, both are the same composition
        v = R.decide(answers({"A.mp3": 0.55, "B/b.wav": 0.45, "C.mp3": 0.0, "none": 0.0},
                             {"A.mp3": 0.45, "B/b.wav": 0.55, "C.mp3": 0.0, "none": 0.0}, [0.97, 0.98, 0.1]),
                     [self.a, self.b, self.c], self.s)
        self.assertEqual((v["status"], v["recording"]), (R.STATUS_LINKED, "A.mp3"))
        self.assertFalse(v["consistent"])
        self.assertEqual([x["recording"] for x in v["alternatives"]], ["B/b.wav"])

    def test_pick_that_fails_its_same_composition_check_needs_review(self):
        v = R.decide(answers({"A.mp3": 0.8, "B/b.wav": 0.0, "C.mp3": 0.0, "none": 0.2},
                             {"A.mp3": 0.8, "B/b.wav": 0.0, "C.mp3": 0.0, "none": 0.2}, [0.55, 0.1, 0.1]),
                     [self.a, self.b, self.c], self.s)
        self.assertEqual((v["status"], v["recording"]), (R.STATUS_REVIEW, "A.mp3"))

    def test_none(self):
        v = R.decide(answers({"A.mp3": 0.02, "B/b.wav": 0.02, "C.mp3": 0.01, "none": 0.95},
                             {"A.mp3": 0.02, "B/b.wav": 0.02, "C.mp3": 0.01, "none": 0.95}, [0.1, 0.2, 0.1]),
                     [self.a, self.b, self.c], self.s)
        self.assertEqual((v["status"], v["recording"]), (R.STATUS_NONE, None))
        # none picked, but one candidate looks like the same composition: you decide
        v = R.decide(answers({"A.mp3": 0.3, "B/b.wav": 0.0, "C.mp3": 0.0, "none": 0.7},
                             {"A.mp3": 0.3, "B/b.wav": 0.0, "C.mp3": 0.0, "none": 0.7}, [0.85, 0.1, 0.1]),
                     [self.a, self.b, self.c], self.s)
        self.assertEqual((v["status"], v["recording"]), (R.STATUS_REVIEW, "A.mp3"))

    def test_confidence_formula(self):
        self.assertAlmostEqual(R.choice_confidence([0.6, 0.3, 0.1]), 0.4)
        self.assertAlmostEqual(R.choice_confidence([0.25, 0.25, 0.25, 0.25]), 0.0)


class FakeManifest:
    def __init__(self, entries):
        self.entries = entries


class FakeLib:
    def __init__(self, entries, out):
        self.manifest = FakeManifest(entries)
        self.cfg = mock.Mock(output=out, scores=out)


def route_answers(p_sung, kinds):
    return {"sung": {"type": "noul", "noul": p_sung}, "kind": {"type": "choice", "probabilities": kinds},
            "kind_reversed": {"type": "choice", "probabilities": kinds}}


CLASSICAL = {"classical": 0.9, "film_score": 0.05, "popular_song": 0.03, "other": 0.02}
POP = {"classical": 0.02, "film_score": 0.03, "popular_song": 0.93, "other": 0.02}


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nw-rec-test-")
        self.music = os.path.join(self.tmp, "music")
        os.makedirs(os.path.join(self.music, "Rhapsody"))
        open(os.path.join(self.music, "Rhapsody", "Rhapsody In Blue_ Gershwin.mp3"), "wb").close()
        self.s = R.Settings(music_dir=self.music, links=os.path.join(self.tmp, "recordings.json"),
                            work_dir=os.path.join(self.tmp, "work"), out_dir=os.path.join(self.tmp, "out"),
                            audio_map=os.path.join(self.tmp, "none.json"))
        self.lib = FakeLib({
            "Gershwin/Rhapsody_in_Blue_Piano_solo.mscz": {"output": "gers_Rhapsody_in_Blue.json",
                                                          "title": "Rhapsody in Blue", "artist": "Gershwin George"},
            "Chopin/Ballade.mscz": {"output": "chop_Ballade.json", "title": "Ballade No 1", "artist": "Chopin"},
            "70s/fountain-of-sorrow.mscz": {"output": "brow_Fountain_of_Sorrow.json", "title": "Fountain of Sorrow",
                                            "artist": "Browne Jackson",
                                            "parts": {"vocal": 177, "notes": {"orchestra": 177, "original": 900}}},
            "Pop/Boston.mscz": {"output": "augu_Boston.json", "title": "Boston", "artist": "Augustana"}}, self.tmp)
        self.files = [af("Rhapsody/Rhapsody In Blue_ Gershwin.mp3", 986.5),
                      af("Fountain of Sorrow (Remastered).mp3", 412.0)]
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def judge(self, state, questions, model):
        self.calls.append((state, questions, model))
        if "sung" in questions:                      # the route
            title = state["song"]["title"]
            if title == "Rhapsody in Blue" or title.startswith("Ballade"):
                return {"model": "jev-test", "answers": route_answers(0.03, CLASSICAL)}
            return {"model": "jev-test", "answers": route_answers(0.97, POP)}
        keys = list(questions["pick"]["criteria"])
        want = "Rhapsody/" if "Rhapsody" in state["song"]["title"] else "Fountain"
        p = {k: (0.98 if k.startswith(want) else 0.02 / (len(keys) - 1)) for k in keys}
        return {"model": "jev-test", "answers": answers(p, p, [0.95] * (len(keys) - 1))}

    def run_match(self, **kw):
        with mock.patch.object(R, "scan_music", return_value=(self.files, [])):
            return R.match(self.lib, self.s, judge=self.judge, log=lambda m: None, **kw)

    def link(self, rel):
        return R.load_links(self.s.links)["links"][rel]

    def test_routes_and_links(self):
        stats = self.run_match()
        self.assertEqual((stats["routes_asked"], stats["asked"], stats["no_candidates"]), (4, 2, 2))
        d = R.load_links(self.s.links)
        self.assertEqual(d["format"], 2)
        rt = d["routes"]
        self.assertEqual(rt["Gershwin/Rhapsody_in_Blue_Piano_solo.mscz"]["route"], R.ROUTE_GENERATED)
        self.assertEqual(rt["70s/fountain-of-sorrow.mscz"]["route"], R.ROUTE_VOCALS)
        self.assertIn("177 vocal-staff notes", rt["70s/fountain-of-sorrow.mscz"]["why"])
        self.assertEqual(rt["Pop/Boston.mscz"]["route"], R.ROUTE_VOCALS)            # the judgment: sung
        self.assertEqual(rt["Pop/Boston.mscz"]["judged"]["answers"]["sung"], 0.97)
        # an orchestral piece's recording is linked for the accuracy summary only: nothing to deliver
        e = self.link("Gershwin/Rhapsody_in_Blue_Piano_solo.mscz")
        self.assertEqual((e["status"], e["variant"]), ("linked", None))
        self.assertEqual(e["variants"], {"vocals": R.SEPARATED, "full": "Rhapsody/Rhapsody In Blue_ Gershwin.mp3"})
        # a sung song's recording delivers its vocals
        e = self.link("70s/fountain-of-sorrow.mscz")
        self.assertEqual((e["recording"], e["variant"]), ("Fountain of Sorrow (Remastered).mp3", "vocals"))
        jobs, skipped = R.jobs_for(self.lib, self.s, R.load_links(self.s.links))
        kinds = {j.song.output: (j.kind, j.variant) for j in jobs}
        self.assertEqual(kinds, {"gers_Rhapsody_in_Blue.json": ("diagnostic", ""),
                                 "brow_Fountain_of_Sorrow.json": ("deliver", "vocals")})
        summary = R.route_summary(R.load_links(self.s.links), self.s)
        self.assertEqual([x["song"] for x in summary["vocals"]], ["brow_Fountain_of_Sorrow.json"])
        self.assertEqual([(x["title"], x["artist"]) for x in summary["vocals_missing"]], [("Boston", "Augustana")])
        self.assertEqual(sorted(x["song"] for x in summary["generated"]),
                         ["chop_Ballade.json", "gers_Rhapsody_in_Blue.json"])

    def test_unchanged_questions_are_not_asked_again_and_decisions_survive(self):
        self.run_match()
        n = len(self.calls)
        self.run_match()
        self.assertEqual(len(self.calls), n)
        # a stricter policy: no new request, a new verdict
        self.s.same_min = 0.99
        self.run_match()
        self.assertEqual(len(self.calls), n)
        self.assertEqual(self.link("70s/fountain-of-sorrow.mscz")["status"], R.STATUS_REVIEW)
        self.s.same_min = 0.8
        # your decisions survive a refresh
        self.assertEqual(R.decide_link(self.lib, self.s, "gers_Rhapsody_in_Blue.json", reject=True,
                                       log=lambda m: None), 0)
        self.assertEqual(R.decide_route_for(self.lib, self.s, "augu_Boston.json", "generated", log=lambda m: None), 0)
        self.run_match(refresh=True)
        self.assertEqual(len(self.calls), 2 * n)
        d = R.load_links(self.s.links)
        self.assertEqual(d["links"]["Gershwin/Rhapsody_in_Blue_Piano_solo.mscz"]["decision"], {"recording": None})
        self.assertEqual(R.effective_route(d["routes"]["Pop/Boston.mscz"]), R.ROUTE_GENERATED)
        self.assertEqual(R.decide_route_for(self.lib, self.s, "augu_Boston.json", "auto", log=lambda m: None), 0)
        self.assertEqual(R.effective_route(R.load_links(self.s.links)["routes"]["Pop/Boston.mscz"]), R.ROUTE_VOCALS)

    def test_confirm_another_variant_or_recording(self):
        self.run_match()
        self.assertEqual(R.decide_link(self.lib, self.s, "brow_Fountain_of_Sorrow.json", "nope.mp3",
                                       log=lambda m: None), 2)
        self.assertEqual(R.decide_link(self.lib, self.s, "brow_Fountain_of_Sorrow.json", variant="full",
                                       log=lambda m: None), 0)
        e = self.link("70s/fountain-of-sorrow.mscz")
        self.assertEqual(R.effective_link(e), ("Fountain of Sorrow (Remastered).mp3", "full", "confirmed"))

    def test_dry_run_asks_nothing_and_writes_nothing(self):
        self.run_match(dry_run=True)
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(self.s.links))


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.s = R.Settings()

    def facts(self, vocal=0, lyrics=0, orchestra=0):
        return {"vocal_notes": vocal, "lyric_syllables": lyrics, "orchestra_notes": orchestra}

    def test_score_facts_from_the_manifest(self):
        e = {"parts": {"vocal": 140, "lyrics": {"sources": ["piano", "vocal"], "syllables": 263},
                       "notes": {"orchestra": 140, "original": 877}}}
        self.assertEqual(R.score_facts(e), {"vocal_notes": 140, "lyric_syllables": 263, "orchestra_notes": 0})
        self.assertEqual(R.score_facts({}), {"vocal_notes": 0, "lyric_syllables": 0, "orchestra_notes": 0})

    def test_the_rule(self):
        d = lambda facts, p, kinds: R.decide_route(facts, route_answers(p, kinds), self.s)
        # classical comes first: an aria with words is still generated
        self.assertEqual(d(self.facts(lyrics=300), 0.95, CLASSICAL)["route"], R.ROUTE_GENERATED)
        self.assertEqual(d(self.facts(lyrics=300), 0.95, CLASSICAL)["why"], "classical (p 0.90), sung")
        # the score's vocal line decides before the judgment
        self.assertEqual(d(self.facts(vocal=177), 0.1, POP)["route"], R.ROUTE_VOCALS)
        self.assertEqual(d(self.facts(), 0.9, POP)["route"], R.ROUTE_VOCALS)
        self.assertEqual(d(self.facts(), 0.1, POP)["route"], R.ROUTE_GENERATED)
        film = {"classical": 0.1, "film_score": 0.85, "popular_song": 0.03, "other": 0.02}
        self.assertEqual(d(self.facts(orchestra=400), 0.05, film)["route"], R.ROUTE_GENERATED)
        self.assertEqual(d(self.facts(), 0.5, POP)["route"], R.ROUTE_REVIEW)
        # a few stray syllables are not a vocal line
        self.assertEqual(d(self.facts(lyrics=7), 0.5, POP)["route"], R.ROUTE_REVIEW)
        # not judged yet
        self.assertEqual(R.decide_route(self.facts(vocal=40), None, self.s)["route"], R.ROUTE_VOCALS)
        self.assertEqual(R.decide_route(self.facts(), None, self.s)["route"], R.ROUTE_REVIEW)

    def test_both_option_orders_are_averaged(self):
        a = route_answers(0.9, POP)
        a["kind_reversed"]["probabilities"] = {"classical": 0.9, "film_score": 0.0, "popular_song": 0.1, "other": 0.0}
        v = R.decide_route(self.facts(), a, self.s)
        self.assertAlmostEqual(v["p_kind"]["classical"], 0.46)
        self.assertEqual(v["route"], R.ROUTE_VOCALS)

    def test_questions(self):
        q = R.route_questions(R.Song("x.mscz", "x.json", "X", "Y"))
        self.assertEqual(q["sung"]["type"], "noul")
        self.assertEqual(list(q["kind"]["criteria"]), list(R.KINDS))
        self.assertEqual(list(q["kind_reversed"]["criteria"]), list(reversed(list(R.KINDS))))

    def test_your_decision_wins(self):
        self.assertEqual(R.effective_route({"route": R.ROUTE_GENERATED, "decision": "vocals"}), R.ROUTE_VOCALS)
        self.assertEqual(R.effective_route({"route": R.ROUTE_VOCALS}), R.ROUTE_VOCALS)
        self.assertEqual(R.effective_route(None), R.ROUTE_REVIEW)


class ProcessRoutingTest(unittest.TestCase):
    """process() with the analysis tool and the separation mocked: what each route makes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nw-rec-proc-")
        self.music = os.path.join(self.tmp, "music")
        os.makedirs(self.music)
        self.full = os.path.join(self.music, "Song.mp3")
        with open(self.full, "wb") as f:
            f.write(b"x" * 100)
        self.s = R.Settings(music_dir=self.music, work_dir=os.path.join(self.tmp, "work"),
                            out_dir=os.path.join(self.tmp, "out"), stems_dir=os.path.join(self.tmp, "stems"),
                            links=os.path.join(self.tmp, "recordings.json"))
        self.recs = {"Song.mp3": R.Recording("Song.mp3", {"full": "Song.mp3"})}
        self.song = R.Song("Pop/song.mscz", "song.json", "Song", "Artist")
        self.tools = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def fake_tool(self, s, args, log):
        self.tools.append(args[0])
        out = args[args.index("--out") + 1]
        if args[0] == "align":
            al = {"map": {"pieces": [{"quality": 0.6, "anchors": [[0.0, 5.0], [100.0, 105.0]]}]}, "gaps": [],
                  "song_end": 100.0, "duration": 120.0, "key": {"offset": 0, "margin": 0.3}, "tuning_cents": 2.0,
                  "tempo": {"ratio": 1.0}, "path_similarity": 0.5, "refine": {}}
            with open(out, "w") as f:
                json.dump(al, f)

    def run_job(self, route, variant, retime=True):
        job = R.Job(self.song, {}, "Song.mp3", variant, "auto", route)
        ref = {"sig": "s1", "measures": [], "song_end": 100.0}
        with mock.patch.object(R, "write_reference", return_value=(os.path.join(self.tmp, "ref.json"), ref)), \
                mock.patch.object(R, "_run_tool", side_effect=self.fake_tool):
            return R.process(None, self.s, job, self.recs, {}, False, retime, lambda m: None)

    def test_generated_route_is_a_diagnostic_without_delivery(self):
        with mock.patch("pianovision.separation.vocals_stem") as sep:
            r = self.run_job(R.ROUTE_GENERATED, "", retime=False)
        sep.assert_not_called()
        self.assertEqual((r["kind"], r.get("mp3")), ("diagnostic", None))
        self.assertFalse(r["checks"]["ok"])
        self.assertIn("accuracy summary only", r["checks"]["problems"][0])
        self.assertEqual(self.tools, ["align"])

    def test_vocals_wait_for_the_separation(self):
        from pianovision import separation as SEP
        with mock.patch.object(SEP, "vocals_stem", side_effect=SEP.SeparationUnavailable("no demucs")):
            r = self.run_job(R.ROUTE_VOCALS, "vocals")
        self.assertEqual(r["pending"], "separation")
        self.assertIn("no demucs", r["checks"]["problems"][0])
        self.assertEqual(self.tools, ["align"])

    def test_a_stem_without_singing_is_not_used(self):
        from pianovision import separation as SEP
        stem = os.path.join(self.tmp, "vocals.wav")
        open(stem, "wb").close()
        with mock.patch.object(SEP, "vocals_stem", return_value=stem), \
                mock.patch.object(SEP, "vocal_activity", return_value={"activity": 0.02, "vocals_to_mix_db": -38.0,
                                                                         "blocks": 200}) as act:
            r = self.run_job(R.ROUTE_VOCALS, "vocals")
        self.assertEqual(act.call_args.kwargs["start"], 5.0)            # the matched part of the recording
        self.assertEqual(act.call_args.kwargs["end"], 105.0)
        self.assertIn("almost no singing", r["checks"]["problems"][0])
        self.assertNotIn("retime", self.tools)

    def test_recording_files(self):
        r = R.Recording("A/x.mp3", {"full": "A/x.mp3", "vocals": "A/x_vocals_split_by_lalalai.mp3"})
        recs = {"A/x.mp3": r}
        job = R.Job(self.song, {}, "A/x.mp3", "vocals", "auto", R.ROUTE_VOCALS)
        src, deliver, variant = R.recording_files(self.s, job, recs)
        self.assertTrue(deliver.endswith("A/x_vocals_split_by_lalalai.mp3"))      # a stem in the folder
        job = R.Job(self.song, {}, "Song.mp3", "vocals", "auto", R.ROUTE_VOCALS)
        self.assertEqual(R.recording_files(self.s, job, self.recs)[1:], (None, "vocals"))   # to be separated
        job = R.Job(self.song, {}, "Song.mp3", "no_piano", "auto", R.ROUTE_VOCALS)
        with self.assertRaises(R.RecordingsError):
            R.recording_files(self.s, job, self.recs)


class EffectiveLinkTest(unittest.TestCase):
    def test_rules(self):
        e = {"status": "review", "recording": "a.mp3", "variant": "no_piano"}
        self.assertIsNone(R.effective_link(e))
        self.assertEqual(R.effective_link(dict(e, decision={})), ("a.mp3", "no_piano", "confirmed"))
        self.assertEqual(R.effective_link(dict(e, decision={"variant": "full"})), ("a.mp3", "full", "confirmed"))
        self.assertEqual(R.effective_link(dict(e, decision={"recording": "b.mp3"})), ("b.mp3", "", "confirmed"))
        self.assertIsNone(R.effective_link(dict(e, status="linked", decision={"recording": None})))
        self.assertEqual(R.effective_link(dict(e, status="linked")), ("a.mp3", "no_piano", "auto"))


class ApiTest(unittest.TestCase):
    def test_retries_rate_limits_then_answers(self):
        body = json.dumps({"model": "jev-1.13.0", "answers": {}}).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = body
        err = urllib.error.HTTPError("u", 429, "slow down", {"retry-after": "0"}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=[err, resp]) as op, mock.patch("time.sleep"):
            out = R.system_one({"x": 1}, {"q": {"type": "noul", "instructions": "?"}}, "jev-1.13.0", api_key="k")
        self.assertEqual(out["model"], "jev-1.13.0")
        req = op.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer k")
        self.assertEqual(json.loads(req.data)["model"], "jev-1.13.0")

    def test_client_errors_are_not_retried(self):
        err = urllib.error.HTTPError("u", 422, "bad", {}, io.BytesIO(b'{"detail": "x"}'))
        with mock.patch("urllib.request.urlopen", side_effect=[err]):
            with self.assertRaises(R.RecordingsError):
                R.system_one({}, {}, "m", api_key="k")


class OutputsTest(unittest.TestCase):
    def test_song_notes_from_the_parts_file(self):
        parts = {"parts": {"original": {"right": [{"start": 1.0, "duration": 0.5, "note": 64, "velocity": 0.8}],
                                        "left": []},
                           "orchestra": {"right": [{"start": 0.5, "duration": 1.0, "note": 67, "velocity": 0.6,
                                                    "vocal": 1}],
                                         "left": [{"start": 0.0, "duration": 2.0, "note": 40, "velocity": 0.7}]}}}
        self.assertEqual(R.song_notes({}, parts), [[0.0, 2.0, 40, 0.7, "o"], [0.5, 1.0, 67, 0.6, "v"],
                                                    [1.0, 0.5, 64, 0.8, "p"]])
        song = {"tracksV2": {"right": [{"start": 0.0, "duration": 1.0, "note": 60, "velocity": 0.5}], "left": []}}
        self.assertEqual(R.song_notes(song, None), [[0.0, 1.0, 60, 0.5, "p"]])

    def result(self, **kw):
        r = {"song": "x.json", "recording": "x.mp3", "variant": "vocals", "kind": "deliver",
             "mp3": os.path.expanduser("~/o/x.mp3"),
             "alignment": {"coverage_with_weak": 0.97, "key_offset": 0, "tempo": {"ratio": 1.1}},
             "retime": {"fidelity_p95_ms": 5.0},
             "verify": {"lag": {"median_ms": 4.0, "within_50ms": 0.9, "windows": 30, "windows_total": 40}},
             "verify_full": None}
        for k, v in kw.items():
            r[k] = v
        return r

    def test_checks(self):
        s = R.Settings()
        self.assertTrue(R.checks(s, self.result())["ok"])
        bad = R.checks(s, self.result(alignment={"coverage_with_weak": 0.5, "key_offset": 2, "tempo": {}}))
        self.assertEqual(len(bad["problems"]), 2)
        few = R.checks(s, self.result(verify={"lag": {"median_ms": 4.0, "within_50ms": 1.0, "windows": 2,
                                                      "windows_total": 40}}))
        self.assertFalse(few["ok"])

    def test_proposal(self):
        tmp = tempfile.mkdtemp()
        try:
            amap = os.path.join(tmp, "audio_map.json")
            with open(amap, "w") as f:
                json.dump({"songs": {"x.json": {"audio": "~/render.mp3", "offset": 0.0}}}, f)
            s = R.Settings(audio_map=amap)
            ok = self.result()
            ok["checks"] = R.checks(s, ok)
            no = self.result(song="y.json", alignment={"coverage_with_weak": 0.5, "key_offset": 0,
                                                       "tempo": {"ratio": 1.0}})
            no["checks"] = R.checks(s, no)
            diag = self.result(song="z.json", kind="diagnostic")
            diag["checks"] = {"ok": False, "problems": ["generated"]}
            p = R.proposal(s, {"songs": {"x.json": ok, "y.json": no, "z.json": diag}})["songs"]
            self.assertNotIn("z.json", p)
            self.assertEqual(p["x.json"]["audio"], "~/o/x.mp3")
            self.assertEqual((p["x.json"]["enabled"], p["x.json"]["replaces"]), (True, "~/render.mp3"))
            self.assertFalse(p["y.json"]["enabled"])
            self.assertIn("NOT ENABLED", p["y.json"]["note"])
            self.assertEqual(p["x.json"]["offset"], 0.0)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
