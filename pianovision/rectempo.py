"""The recording leads: a sung song's score timed to the recording you own (``python -m pianovision recordings
tempo``), so Note Waterfall plays the ORIGINAL recording, never stretched, as the song's companion audio.

For pop and film songs the recording is the source of truth for the timing: instead of time-stretching the
recording onto the score's timeline (``recordings retime``), the score's tempo map is rewritten so that every
measure as played lasts what the recording's measure lasts, and Note Waterfall starts the recording at an offset.

1. **The measures in the recording** (``tools/recording_align.py bars``).  The alignment of the song's notes with
   the recording (``align``: chroma DTW, pieces and gaps) gives a coarse map; the recording's beats are detected
   (madmom's recurrent networks and DBN when ``[recordings] beats_python`` has madmom, ``tools/beat_activations.py``;
   librosa's beat tracker driven by the tempo the alignment implies, always) and each detected beat is put on the
   score's lattice of beats and half beats (thirds in compound time): a Viterbi path weighs the coarse map's place
   against a steady tempo, so detections on the off-beats (a transcription an eighth off the recording's pulse) and
   runs the coarse map has a little early or late are placed right.  Every position of every measure as played gets
   its time from those anchors (the coarse map's shape between them), with a confidence: ``detected`` (an anchor on
   it; both detectors agreeing scores highest), ``between`` (anchors at most two beats apart around it), ``coarse``
   (the alignment only) and ``outside`` (before the recording starts or after it ends: the recording's tempo
   carried on).  A measure whose length is not its time signature's (a 9/8 bar in 4/4) counts the beats the
   recording plays in it (usually the time signature's) and simply gets the recording's duration, its positions
   spread as the coarse map has them.
2. **The tempo map** (:func:`plan`): per written measure, the times of its positions from its start (the passes'
   mean when it is played more than once) shaped into tempo marks by :func:`pianovision.timeline._shape`: a mark
   where the measure starts, more inside it only where one tempo would miss a position by more than the tolerance
   (20 ms); each measure lands on its length.  They are written as :mod:`pianovision.timeline` writes a partner's
   clock (invisible tempo marks, "recording tempo q = ..."; the score's own tempo marks, fermatas and gradual tempo
   changes ``<play>0</play>``, breaths and section breaks pause 0), into the formatter's model and the file alike.
3. **Repeats** (:func:`shared_tempo_errors`): a written measure played on several passes can carry only one tempo
   in MuseScore.  The passes' mean is simulated (a measure played once absorbs the error that came before it): if
   the timing error stays within :data:`REPEAT_TOLERANCE` (40 ms) at every played measure the repeats stay;
   otherwise the score is **unrolled** (:func:`unroll_mscx`): the measures written out in the order they are
   played, repeat signs, voltas, segno / coda / fine markers and jumps removed, each copy with its own tempo; a
   copy of the k-th pass carries the syllables that pass sings (verse k, as :func:`pianovision.lyrics.collect_lyrics`
   reads it) as verse 1; slurs and ties that would now cross into another measure are dropped; time and key
   signatures and clefs are restated where the new order needs them; element ids are dropped from the copies
   (MuseScore gives them new ones).
4. **The offset** (:func:`offset_of`): Note Waterfall plays the audio file at ``song time + offset``
   (``audioFileTime = songTime + GlobalAudioLead + AudioOffset``), so the offset is the recording time at which
   the song's first measure starts.  Positive: the recording has more lead-in than the score (it starts that far
   into the file, its intro heard in the app's lead-in); negative: the score starts before the recording does (its
   first measures the recording does not have play at the recording's tempo, and the audio starts that much
   later).  The recording's beat is carried back over the score's lead measures either way.

Times are seconds of the recording as ffmpeg decodes it (gapless: the mp3's encoder delay that its LAME header
declares is skipped).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

REVISION = 1
TOLERANCE = 0.020           # s: a position inside a measure may land this far from the recording's time
REPEAT_TOLERANCE = 0.040    # s: a shared tempo for repeated measures is kept when no played measure is further off
MARK_LABEL = "recording tempo"
STATE_NAME = "tempo.json"


class RecTempoError(RuntimeError):
    pass


# ------------------------------------------------------------------------------
# the song's measures as played, with the positions a tempo mark can go to
# ------------------------------------------------------------------------------

@dataclass
class Rendered:
    score: Any
    renderer: Any
    midi: Any
    timeline: Any
    seconds: Any


def render(path: str, compat=None, options: Optional[dict] = None, plan: Optional["Plan"] = None) -> Rendered:
    """The score rendered as the formatter renders it (``plan``: with the recording's tempo map applied)."""
    from .arrangement import Timeline
    from .handsplit import apply_piano_options
    from .mscx import read_score
    from .pvjson import TempoIndex, extract_tempo_events
    from .render import Compat, Renderer
    from .timeline import apply_to_score
    c = compat if isinstance(compat, Compat) else Compat.from_dict(compat)
    score = read_score(path)
    if plan is not None:
        for m in score.measures:                            # an earlier run's marks go (write_copy drops them too)
            for seg in m.segments:
                seg.annotations = [e for e in seg.annotations if not (
                    e.tag == "Tempo" and str(e.props.get("text", "")).startswith(MARK_LABEL + " q ="))]
        apply_to_score(score, plan.timeline_plan(), label=MARK_LABEL)
    r = Renderer(score, c)
    midi = r.render()
    apply_piano_options(midi, options)
    tl = Timeline.from_render(r, midi)
    return Rendered(score, r, midi, tl, TempoIndex(extract_tempo_events(midi), midi.ticks_per_beat))


def beat_ticks(timesig: Tuple[int, int], tpb: int = 480) -> int:
    """Ticks of a beat: the time signature's unit, a dotted one in compound time (6/8, 9/8, 12/8)."""
    num, den = timesig
    beat = max(1, int(round(tpb * 4 / max(den, 1))))
    if den == 8 and num % 3 == 0 and num > 3:
        beat *= 3
    return beat


def reference(rd: Rendered) -> dict:
    """``tools/recording_align.py``'s ref.json of a render (:func:`pianovision.scorefix.score_reference`'s format)
    with, per measure as played, its length (``ticks``), its beat (``beat_ticks``), whether its length differs from
    its time signature's (``odd``) and the positions where a tempo mark can go with their times (``positions``)."""
    from .arrangement import sounding_notes
    from .timeline import write_positions
    score, tl, sec, midi = rd.score, rd.timeline, rd.seconds, rd.midi
    tpb = midi.ticks_per_beat
    wp = write_positions(score)
    measures = []
    for m in tl.measures:
        sm = score.measures[m.number - 1]
        bt = beat_ticks(m.timesig, tpb)
        pos = sorted(t for t in wp[m.number - 1] if 0 <= t < m.length)
        if not pos or pos[0] != 0:
            pos = [0] + pos
        beats = [round(sec(tl.tick(m.utick + k)), 5) for k in range(0, max(m.length, 1), bt)]
        actual = max(1, int(round(m.length / (tpb * 4 / max(m.timesig[1], 1)))))
        measures.append({"index": m.index, "number": m.number, "pass": m.passno, "start": round(sec(m.tick), 5),
                         "end": round(sec(m.end_tick), 5), "timesig": [actual, m.timesig[1]],
                         "nominal": list(m.timesig), "beats": beats, "ticks": m.length, "beat_ticks": bt,
                         "odd": sm.len != sm.timesig,
                         "positions": [[t, round(sec(tl.tick(m.utick + t)), 5)] for t in pos]})
    voice = {i for i, st in enumerate(score.staves) if (st.part.instrument().instrument_id or "").startswith("voice.")}
    notes = []
    for n in sounding_notes(midi, range(min(len(midi.tracks), len(score.staves))), tl):
        t0, t1 = sec(tl.tick(n.on)), sec(tl.tick(n.off))
        notes.append([round(t0, 5), round(max(t1 - t0, 0.0), 5), int(n.pitch), 0.7, "v" if n.track in voice else "p"])
    notes.sort()
    end = max([measures[-1]["end"] if measures else 0.0] + [n[0] + n[1] for n in notes])
    return {"format": 1, "sig": "", "song": os.path.basename(score.path or ""), "score": score.path or "",
            "song_end": round(end, 4), "measures": measures, "notes": notes}


# ------------------------------------------------------------------------------
# the recording's measures (bars.json) -> per played measure: length and the times of its positions
# ------------------------------------------------------------------------------

@dataclass
class Target:
    """Where the recording plays one measure as played: ``start`` / ``end`` (recording seconds) and the times of
    its positions (ticks from its start -> seconds from its start)."""
    index: int
    number: int                     # score measure, 1-based (file order)
    passno: int
    start: float
    end: float
    rel: Dict[int, float]
    conf: float = 0.0
    source: str = ""
    beats_score: int = 0
    beats_recording: int = 0
    odd: bool = False
    beats: List[float] = field(default_factory=list)
    beat_conf: List[float] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.end - self.start


def targets_from_bars(bars: dict) -> List[Target]:
    out = []
    for b in bars["bars"]:
        rel = {int(rt): float(t) - float(b["start"]) for rt, t in b["positions"]}
        out.append(Target(int(b["index"]), int(b["number"]), int(b.get("pass", 1)), float(b["start"]),
                          float(b["end"]), rel, float(b.get("conf", 0.0)), b.get("source", ""),
                          int(b.get("beats_score", 0)), int(b.get("beats_recording", 0)), bool(b.get("odd")),
                          [float(x) for x in b.get("beats", [])], [float(x) for x in b.get("beat_conf", [])]))
    return out


# ------------------------------------------------------------------------------
# repeats: one tempo per written measure, the error it makes
# ------------------------------------------------------------------------------

@dataclass
class SharedErrors:
    """:func:`shared_tempo_errors`: per measure as played, the error of its start (s, + = the score is late) and the
    largest error of its positions."""
    start: List[float]
    inside: List[float]
    repeated: List[bool]

    @property
    def max_abs(self) -> float:
        return max([abs(x) for x in self.start + self.inside] or [0.0])

    def p95(self) -> float:
        xs = sorted(abs(x) for x in self.start + self.inside)
        return xs[min(len(xs) - 1, int(math.ceil(0.95 * len(xs))) - 1)] if xs else 0.0


def shared_tempo_errors(written: Sequence[int], lengths: Sequence[float],
                        rel: Optional[Sequence[Dict[int, float]]] = None, absorb: bool = True) -> SharedErrors:
    """The timing error of one tempo per written measure: ``written[j]`` the score measure the j-th played measure
    is, ``lengths[j]`` how long the recording plays it, ``rel[j]`` the times of its positions from its start.  A
    measure played more than once gets the mean of its passes (length and positions); with ``absorb`` a measure
    played once takes up the error that came before it (it ends where the recording's does, as long as it keeps at
    least half its length).  Returns the error at every played measure's start and inside it."""
    passes: Dict[int, List[int]] = {}
    for j, w in enumerate(written):
        passes.setdefault(w, []).append(j)
    mean_len = {w: statistics.fmean(lengths[j] for j in js) for w, js in passes.items()}
    mean_rel: Dict[int, Dict[int, float]] = {}
    if rel is not None:
        for w, js in passes.items():
            keys = set.intersection(*(set(rel[j]) for j in js))
            mean_rel[w] = {k: statistics.fmean(rel[j][k] for j in js) for k in keys}
    err = 0.0
    starts, inside, rep = [], [], []
    for j, w in enumerate(written):
        starts.append(err)
        once = len(passes[w]) == 1
        rep.append(not once)
        if once:
            length = max(lengths[j] - err, 0.5 * lengths[j]) if absorb else lengths[j]
            scale = length / lengths[j] if lengths[j] > 0 else 1.0
            ins = [err + rel[j][k] * scale - rel[j][k] for k in rel[j]] if rel is not None else []
        else:
            length = mean_len[w]
            ins = [err + mean_rel[w][k] - rel[j][k] for k in mean_rel[w]] if rel is not None else []
        inside.append(max(ins, key=abs) if ins else err)
        err += length - lengths[j]
    return SharedErrors(starts, inside, rep)


# ------------------------------------------------------------------------------
# the tempo map
# ------------------------------------------------------------------------------

@dataclass
class Plan:
    marks: List[Any]                            # pianovision.timeline.Mark
    lengths: List[float]                        # per measure as played: seconds on the recording's clock
    offset: float = 0.0
    stats: Dict[str, Any] = field(default_factory=dict)

    def timeline_plan(self):
        from .timeline import SONG_ORCHESTRA, Plan as TPlan
        return TPlan(SONG_ORCHESTRA, self.marks, self.lengths)

    def digest(self) -> str:
        doc = {"revision": REVISION, "marks": [[m.measure, m.rtick, m.text] for m in self.marks]}
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]


def plan(score, played: Sequence[Tuple[int, Sequence[int]]], targets: Sequence[Target],
         tolerance: float = TOLERANCE) -> Plan:
    """Tempo marks that make every measure as played last what the recording's lasts.  ``played``: per measure as
    played, (its score measure, 0-based; its positions in ticks with the measure's length last), in playback order;
    ``targets``: the recording's measures, the same order.  A written measure played more than once gets its
    passes' mean."""
    from fractions import Fraction as F
    from .timeline import Mark, _shape, write_positions
    if len(played) != len(targets):
        raise RecTempoError(f"{len(played)} measures as played, {len(targets)} measures in the recording's analysis")
    positions = write_positions(score)
    passes: Dict[int, List[int]] = {}
    for j, (k, _pts) in enumerate(played):
        passes.setdefault(k, []).append(j)
    marks: List[Any] = []
    length_of: Dict[int, float] = {}
    worst, clamped, too_short = 0.0, [], []
    for k in sorted(passes):
        js = passes[k]
        pts = list(played[js[0]][1])
        times = []
        for i, p in enumerate(pts):
            if i == len(pts) - 1:
                times.append(statistics.fmean(targets[j].length for j in js))
            else:
                times.append(statistics.fmean(_rel_at(targets[j], p, pts) for j in js))
        if times[-1] <= 1e-3:
            too_short.append(k + 1)
        times = _monotone(pts, times)
        ms, total, dev, cl = _shape(pts, times, tolerance)
        for j in js:
            worst = max(worst, abs(total - targets[j].length) if len(js) == 1 else dev)
        worst = max(worst, dev)
        if cl:
            clamped.append(k + 1)
        length_of[k] = total
        for rt, bps in ms:
            path, track, pos = positions[k].get(rt, (None, 0, F(0)))
            if path is None:
                continue
            marks.append(Mark(k, rt, bps, path, track, pos))
    marks.sort(key=lambda x: (x.measure, x.rtick))
    kept: List[Any] = []
    for mk in marks:
        m = score.measures[mk.measure]
        if mk.rtick == 0 and kept and kept[-1].bps == mk.bps and not m.timesig - m.len > 0:
            continue
        kept.append(mk)
    lengths = [length_of[k] for k, _ in played]
    p = Plan(kept, lengths)
    p.stats = {"marks": len(kept), "measures": len(played), "written": len(passes),
               "repeated": sum(1 for js in passes.values() if len(js) > 1),
               "max_inside_ms": round(worst * 1000, 1), "clamped": clamped, "too_short": too_short,
               "tolerance_ms": round(tolerance * 1000, 1), "seconds": round(sum(lengths), 3)}
    return p


def _monotone(pts: Sequence[int], times: List[float], floor: float = 0.25) -> List[float]:
    """Times of a measure's positions (from 0, its length last) made increasing and inside the measure, so a stray
    analysis point cannot stop the clock (a tempo of 5 bpm) or run it past MuseScore's fastest: an isotonic fit of
    the inner times, clamped to the measure, every step at least ``floor`` of what a steady tempo would give it."""
    from .recmap import isotonic
    n = len(times)
    end = max(times[-1], 1e-3)
    if n <= 2:
        return [0.0, end]
    inner = [min(max(t, 0.0), end) for t in isotonic(times[1:-1])]
    out = [0.0] + inner + [end]
    total = max(pts[-1] - pts[0], 1)
    step = [floor * end * (pts[i] - pts[i - 1]) / total for i in range(1, n)]
    for i in range(1, n - 1):                               # forwards: a minimum step after each position
        out[i] = max(out[i], out[i - 1] + step[i - 1])
    for i in range(n - 2, 0, -1):                           # backwards: room for the steps up to the end
        out[i] = min(out[i], out[i + 1] - step[i])
    return out


def _rel_at(t: Target, p: int, pts: Sequence[int]) -> float:
    """The recording's time of position ``p`` from the measure's start (positions the analysis has, else linear)."""
    if p in t.rel:
        return t.rel[p]
    end = pts[-1]
    keys = sorted(t.rel) + [end]
    vals = [t.rel[x] for x in sorted(t.rel)] + [t.length]
    for a, b, va, vb in zip(keys, keys[1:], vals, vals[1:]):
        if a <= p <= b:
            return va + (vb - va) * (p - a) / max(b - a, 1)
    return t.length * p / max(end, 1)


def played_positions(rd: Rendered) -> List[Tuple[int, List[int]]]:
    """Per measure as played: (score measure, 0-based; the ticks a tempo mark can go to, the measure's length last)."""
    from .timeline import write_positions
    wp = write_positions(rd.score)
    out = []
    for m in rd.timeline.measures:
        pos = sorted(t for t in wp[m.number - 1] if 0 <= t < m.length)
        if not pos or pos[0] != 0:
            pos = [0] + pos
        out.append((m.number - 1, pos + [m.length]))
    return out


def offset_of(targets: Sequence[Target]) -> float:
    """Note Waterfall's ``offset`` (audioFileTime = songTime + offset): the recording time at which the song's
    first measure starts (module docstring)."""
    return float(targets[0].start) if targets else 0.0


# ------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def percentiles(xs: Sequence[float]) -> Dict[str, Optional[float]]:
    """median, p95 and max of |x| (ms)."""
    a = sorted(abs(x) * 1000.0 for x in xs)
    if not a:
        return {"n": 0, "median_ms": None, "p95_ms": None, "max_ms": None}
    return {"n": len(a), "median_ms": round(statistics.median(a), 1),
            "p95_ms": round(a[min(len(a) - 1, int(math.ceil(0.95 * len(a))) - 1)], 1), "max_ms": round(a[-1], 1)}


def target_reference(rd: Rendered, targets: Sequence[Target]) -> dict:
    """The song's notes and measures put where the recording plays them (``targets``), in recording seconds: what
    the score timed to the recording should sound like, for ``recording_align.py verify`` against the recording
    itself (no offset)."""
    from .arrangement import sounding_notes
    tl, score, midi = rd.timeline, rd.score, rd.midi
    tms = tl.measures
    if len(tms) != len(targets):
        raise RecTempoError("the analysis does not match the score's measures as played")
    pts = [sorted(t.rel) for t in targets]

    def at(utick: int) -> float:
        j = tl.measure_at_utick(utick)
        m, t = tms[j], targets[j]
        x = utick - m.utick
        if x >= m.length:
            return t.end + (x - m.length) / max(m.length, 1) * t.length
        return t.start + _rel_at(t, x, pts[j] + [m.length])

    voice = {i for i, st in enumerate(score.staves) if (st.part.instrument().instrument_id or "").startswith("voice.")}
    notes = []
    for n in sounding_notes(midi, range(min(len(midi.tracks), len(score.staves))), tl):
        a, b = at(tl.utick(n.on)), at(tl.utick(n.off))
        notes.append([round(a, 5), round(max(b - a, 0.02), 5), int(n.pitch), 0.7, "v" if n.track in voice else "p"])
    notes.sort()
    measures = []
    for m, t in zip(tms, targets):
        bt = beat_ticks(m.timesig, midi.ticks_per_beat)
        beats = [round(at(m.utick + k), 5) for k in range(0, max(m.length, 1), bt)]
        measures.append({"index": m.index, "number": m.number, "pass": m.passno, "start": round(t.start, 5),
                         "end": round(t.end, 5), "timesig": list(m.timesig), "beats": beats})
    end = max([targets[-1].end] + [n[0] + n[1] for n in notes])
    return {"format": 1, "sig": "", "song": "", "score": score.path or "", "song_end": round(end, 4),
            "measures": measures, "notes": notes}


# ------------------------------------------------------------------------------
# unrolling: the measures written out as they are played
# ------------------------------------------------------------------------------

FRAMES = ("VBox", "HBox", "TBox", "FBox")
CONTEXT = ("Clef", "KeySig", "TimeSig")
CHORDREST = ("Chord", "Rest", "MeasureRepeat")
_EID_RE = re.compile(rb"\s*<eid>[^<]*</eid>")


@dataclass
class UnrollReport:
    measures: int = 0                       # measures written out
    from_measures: int = 0                  # the score's measures
    removed: Dict[str, int] = field(default_factory=dict)       # what was taken out (repeat signs, voltas, ...)
    spanners_dropped: Dict[str, int] = field(default_factory=dict)  # slurs / ties that would cross into another measure
    context_added: Dict[str, int] = field(default_factory=dict)     # clefs, key and time signatures restated
    lyrics_kept: int = 0
    lyrics_dropped: int = 0
    passes: List[int] = field(default_factory=list)                 # per measure written out: the pass it is

    def lines(self) -> List[str]:
        def d(x):
            return ", ".join(f"{v} {k}" for k, v in sorted(x.items())) or "none"
        return [f"{self.from_measures} measures written out as the {self.measures} measures they play",
                f"removed: {d(self.removed)}", f"slurs and ties that would cross into another measure: "
                f"{d(self.spanners_dropped)}", f"restated: {d(self.context_added)}",
                f"lyrics: {self.lyrics_kept} syllables kept (each pass's verse as verse 1), {self.lyrics_dropped} "
                f"other verses' syllables dropped from the copies"]


def _bump(d: Dict[str, int], k: str, n: int = 1) -> None:
    d[k] = d.get(k, 0) + n


def _location_measures(sp) -> int:
    """The measure offset of a spanner element's other end (``<next>`` / ``<prev>`` ``<location><measures>``)."""
    for tag in ("next", "prev"):
        n = sp.find(tag)
        if n is not None:
            loc = n.find("location")
            if loc is None:
                return 0
            v = loc.value("measures")
            try:
                return int(v) if v is not None else 0
            except ValueError:
                return 0
    return 0


def _lyric_verses(data: bytes, chord) -> Dict[int, Any]:
    """{verse (MuseScore <no>): the Lyrics node} of a chord's visible, non-empty syllables (first per verse), as
    :func:`pianovision.lyrics._verses` reads them."""
    import xml.etree.ElementTree as ET
    from .lyrics import lyric_text
    out: Dict[int, Any] = {}
    for ly in chord.findall("Lyrics"):
        el = ET.fromstring(data[ly.start:ly.end])
        if (el.findtext("visible") or "1").strip() == "0" or not lyric_text(el):
            continue
        try:
            no = int((el.findtext("no") or "0").strip())
        except ValueError:
            no = 0
        out.setdefault(no, ly)
    return out


def unroll_mscx(data: bytes, order: Sequence[int]) -> Tuple[bytes, UnrollReport]:
    """The score file's main .mscx with its measures written out in the order ``order`` (1-based measure numbers in
    file order, as the score plays them; :func:`pianovision.scorefix.play_order`).  Module docstring, step 3."""
    from .handedits import _span, parse_xml
    root = parse_xml(data)
    score = root.find("Score")
    if score is None:
        raise RecTempoError("no <Score>")
    staves = [c for c in score.children if c.tag == "Staff" and c.find("Measure") is not None]
    if not staves:
        raise RecTempoError("no staff with measures")
    rep = UnrollReport(measures=len(order))
    counts = [len(st.findall("Measure")) for st in staves]
    if len(set(counts)) != 1:
        raise RecTempoError(f"the staves have different numbers of measures ({counts})")
    nm = counts[0]
    rep.from_measures = nm
    if any(not 1 <= w <= nm for w in order):
        raise RecTempoError("the play order names measures the score does not have")
    if any(m.find("measureRepeatCount") is not None or list(m.descendants("MeasureRepeat"))
           for st in staves for m in st.findall("Measure")):
        raise RecTempoError("the score has measure repeats (%): they cannot be written out yet")
    passno: List[int] = []
    seen: Dict[int, int] = {}
    for w in order:
        seen[w] = seen.get(w, 0) + 1
        passno.append(seen[w])
    rep.passes = passno
    keep_spanner: Dict[Tuple[int, int], bool] = {}

    def consecutive(p: int, r: int) -> bool:
        """The copies p .. p + r (r may be negative) are the written measures w .. w + r in order."""
        w = order[p]
        step = 1 if r > 0 else -1
        for i in range(step, r + step, step):
            q = p + i
            if not 0 <= q < len(order) or order[q] != w + i:
                return False
        return True

    out_staves: List[bytes] = []
    for si, st in enumerate(staves):
        measures = st.findall("Measure")
        # frames go before the first copy of the measure that follows them (those after the last measure at the end)
        frames_before: Dict[int, List[Any]] = {}
        trailing: List[Any] = []
        pending: List[Any] = []
        lead_items: List[Any] = []
        mi = 0
        for c in st.children:
            if c.tag == "Measure":
                frames_before[mi + 1] = pending
                pending = []
                mi += 1
            elif c.tag in FRAMES:
                pending.append(c)
            elif mi == 0:
                lead_items.append(c)
            else:
                pending.append(c)
        trailing = pending
        # clefs, key and time signatures in effect at the end of every measure (file order), as XML without ids
        ctx_end: List[Dict[str, bytes]] = []
        ctx: Dict[str, bytes] = {}
        starts_with: List[set] = []
        for m in measures:
            first = set()
            for v in m.findall("voice"):
                head = True
                for c in v.children:
                    if c.tag in CONTEXT:
                        ctx[c.tag] = _EID_RE.sub(b"", data[c.start:c.end])
                        if head:
                            first.add(c.tag)
                    elif c.tag in CHORDREST:
                        head = False
            starts_with.append(first)
            ctx_end.append(dict(ctx))
        # verses sung in a measure, per voice (collect_lyrics: a chord without this pass's verse is silent when the
        # verse is sung elsewhere in the measure on its track)
        in_measure: List[List[set]] = []
        for m in measures:
            per_voice = []
            for v in m.findall("voice"):
                vs = set()
                for c in v.children:
                    if c.tag in ("Chord", "Rest"):
                        vs.update(_lyric_verses(data, c))
                per_voice.append(vs)
            in_measure.append(per_voice)
        parts: List[bytes] = []
        a, _b = _span(data, st.children[0]) if st.children else (st.close, st.close)
        parts.append(data[st.start:a])
        for c in lead_items:
            s0, s1 = _span(data, c)
            parts.append(data[s0:s1])
        emitted_frames = set()
        for p, w in enumerate(order):
            m = measures[w - 1]
            k = passno[p]
            if w not in emitted_frames:
                for fr in frames_before.get(w, []):
                    s0, s1 = _span(data, fr)
                    parts.append(data[s0:s1])
                emitted_frames.add(w)
            edits: List[Tuple[int, int, bytes]] = []

            def drop(node) -> None:
                s0, s1 = _span(data, node)
                edits.append((s0, s1, b""))

            for c in m.children:
                if c.tag in ("startRepeat", "endRepeat", "Marker", "Jump"):
                    drop(c)
                    if si == 0 and k == 1:
                        _bump(rep.removed, {"startRepeat": "start repeats", "endRepeat": "end repeats",
                                            "Marker": "markers (segno, coda, fine)", "Jump": "jumps"}[c.tag])
            for vi, v in enumerate(m.findall("voice")):
                for c in v.children:
                    if c.tag == "BarLine" and "repeat" in (c.value("subtype") or ""):
                        drop(c)
                for sp in v.descendants("Spanner"):
                    typ = sp.attrs.get("type", "")
                    if typ == "Volta":
                        drop(sp)
                        if si == 0 and k == 1 and sp.find("Volta") is not None:
                            _bump(rep.removed, "voltas")
                        continue
                    r = _location_measures(sp)
                    if r and not consecutive(p, r):
                        drop(sp)
                        if sp.find("next") is not None:
                            _bump(rep.spanners_dropped, typ.lower() + "s")
                for c in v.children:
                    if c.tag not in ("Chord", "Rest"):
                        continue
                    vs = _lyric_verses(data, c)
                    if not vs:
                        continue
                    if (k - 1) in vs:
                        chosen = vs[k - 1]
                    elif (k - 1) in in_measure[w - 1][vi]:
                        chosen = None
                    else:
                        chosen = vs[min(vs)]
                    for ly in c.findall("Lyrics"):
                        if ly is chosen:
                            no = ly.find("no")
                            if no is not None:
                                drop(no)
                            rep.lyrics_kept += 1
                        elif ly in vs.values():
                            drop(ly)
                            rep.lyrics_dropped += 1
            # restate the clef, key and time signature this measure expects where the new order brings another
            if p > 0:
                expected = ctx_end[w - 2] if w > 1 else {}
                actual = ctx_end[order[p - 1] - 1]
                add = [kind for kind in CONTEXT if kind in expected and expected[kind] != actual.get(kind)
                       and kind not in starts_with[w - 1]]
                v0 = m.find("voice")
                if add and v0 is not None and v0.children:
                    first = v0.children[0]
                    ls = data.rfind(b"\n", 0, first.start) + 1
                    ind = data[ls:first.start].decode()
                    text = "".join(_reindent(expected[kind], ind) for kind in add)
                    edits.append((ls, ls, text.encode()))
                    for kind in add:
                        _bump(rep.context_added, {"Clef": "clefs", "KeySig": "key signatures",
                                                  "TimeSig": "time signatures"}[kind])
            s0, s1 = _span(data, m)
            text = _apply(data, s0, s1, edits)
            if k > 1:
                text = _EID_RE.sub(b"", text)
            parts.append(text)
        for fr in trailing:
            s0, s1 = _span(data, fr)
            parts.append(data[s0:s1])
        ls = data.rfind(b"\n", 0, st.close) + 1
        parts.append(data[ls:st.end])
        out_staves.append(b"".join(parts))
    # the staves replaced in the file
    pieces: List[bytes] = []
    pos = 0
    for st, new in zip(staves, out_staves):
        ls = data.rfind(b"\n", 0, st.start) + 1
        pieces.append(data[pos:ls])
        pieces.append(data[ls:st.start])
        pieces.append(new)
        pos = st.end
    pieces.append(data[pos:])
    return b"".join(pieces), rep


def _reindent(xml: bytes, indent: str) -> str:
    """An element (as found in the file, its first line unindented) at ``indent``, on its own lines."""
    lines = xml.decode().split("\n")
    first = lines[0].strip()
    rest = lines[1:]
    base = min((len(x) - len(x.lstrip()) for x in rest if x.strip()), default=0)
    out = [indent + first] + [indent + "  " + x[base:].rstrip() if not x.strip().startswith("</") else
                              indent + "  " + x.strip() for x in rest if x.strip()]
    return "\n".join(out) + "\n"


def _apply(data: bytes, lo: int, hi: int, edits: Sequence[Tuple[int, int, bytes]]) -> bytes:
    """``data[lo:hi]`` with the edits (start, end, replacement) applied; an edit inside a removed range is skipped."""
    out = []
    pos = lo
    for s, e, t in sorted(edits, key=lambda x: (x[0], 0 if x[1] == x[0] else 1, -(x[1] - x[0]))):
        if s < pos:
            continue
        out.append(data[pos:s])
        out.append(t)
        pos = e
    out.append(data[pos:hi])
    return b"".join(out)


def unroll_mscz(mscz: bytes, order: Sequence[int]) -> Tuple[bytes, UnrollReport]:
    """The .mscz with its main .mscx unrolled (:func:`unroll_mscx`); every other member as it is."""
    import io
    import zipfile
    from .handedits import _main_mscx_name, replace_mscx
    with zipfile.ZipFile(io.BytesIO(mscz)) as z:
        member = _main_mscx_name(z)
        data = z.read(member)
        others = [n for n in z.namelist() if n.endswith(".mscx") and n != member]
    if others:
        raise RecTempoError(f"the score has parts or other scores inside ({', '.join(others)}): not written out")
    new, rep = unroll_mscx(data, order)
    return replace_mscx(mscz, member, new), rep


# ------------------------------------------------------------------------------
# the run: analysis, decision, the copy, its verification
# ------------------------------------------------------------------------------

@dataclass
class Options:
    work: str                                   # this song's folder: analysis, the proposed copy, the report
    python: str                                 # numpy + scipy + soundfile + librosa (recording_align.py)
    script: str                                 # tools/recording_align.py
    beats_python: str = ""                      # a Python with madmom ("" or missing: librosa's beats only)
    beats_script: str = ""                      # tools/beat_activations.py
    tolerance: float = TOLERANCE
    repeat_tolerance: float = REPEAT_TOLERANCE
    unroll: str = "auto"                        # auto | always | never
    mscore: Optional[str] = None                # None: MuseScore's check is skipped
    min_free_mb: int = 6144                     # a heavy step waits for this much available memory
    wait_s: float = 3600.0
    force: bool = False
    tail: float = 8.0
    key: int = 0
    timeout: int = 7200


Log = Any


def mem_available_mb() -> Optional[int]:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def wait_for_memory(min_mb: int, what: str, log: Log, wait_s: float = 3600.0, poll: float = 30.0) -> None:
    """Waits until ``min_mb`` of memory is available before a heavy step (another job may be running)."""
    import time
    t0 = time.monotonic()
    warned = False
    while True:
        a = mem_available_mb()
        if a is None or a >= min_mb:
            return
        if time.monotonic() - t0 > wait_s:
            raise RecTempoError(f"{what}: only {a} MB of memory available after waiting {wait_s:.0f} s (needs "
                                f"{min_mb} MB)")
        if not warned:
            log(f"  {what}: {a} MB available, waiting for {min_mb} MB ...")
            warned = True
        time.sleep(poll)


def _tool(opts: Options, py: str, script: str, args: List[str], what: str, log: Log) -> None:
    import subprocess
    py = os.path.expanduser(py)
    if not os.path.isfile(py):
        raise RecTempoError(f"{what}: python not found: {py}")
    wait_for_memory(opts.min_free_mb, what, log, opts.wait_s)
    r = subprocess.run([py, script] + args, capture_output=True, text=True, timeout=opts.timeout)
    if r.returncode != 0:
        raise RecTempoError(f"{what} failed:\n{(r.stderr or r.stdout)[-2000:]}")


def _sig(path: str) -> str:
    st = os.stat(path)
    return f"{os.path.abspath(path)}:{st.st_size}:{int(st.st_mtime)}"


def _version(script: str, name: str = "VERSION") -> str:
    with open(script, encoding="utf-8") as f:
        m = re.search(rf"^{name} = (\d+)", f.read(), re.M)
    return m.group(1) if m else "?"


def _cached(work: str, key: str, out: str, force: bool) -> bool:
    try:
        with open(os.path.join(work, "cache.json"), encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    return not force and c.get(out) == key and os.path.isfile(os.path.join(work, out))


def _remember(work: str, key: str, out: str) -> None:
    p = os.path.join(work, "cache.json")
    try:
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    c[out] = key
    _write_json(p, c)


def analyse(score_path: str, audio: str, opts: Options, compat=None, options: Optional[dict] = None,
            log: Log = print) -> Tuple[Rendered, dict, List[Target]]:
    """Step 1 (module docstring): the score's reference, the alignment, madmom's beats, the bars; cached by their
    inputs in ``opts.work``."""
    os.makedirs(opts.work, exist_ok=True)
    rd = render(score_path, compat, options)
    ref = reference(rd)
    ref["sig"] = hashlib.sha256(json.dumps([ref["measures"], ref["notes"]]).encode()).hexdigest()[:16]
    ref_path = os.path.join(opts.work, "ref.json")
    _write_json(ref_path, ref)
    tools = f"align{_version(opts.script)}-bars{_version(opts.script, 'BARS_VERSION')}"
    akey = hashlib.sha256(f"{ref['sig']}|{_sig(audio)}|{tools}|{opts.tail}".encode()).hexdigest()[:16]
    if not _cached(opts.work, akey, "align.json", opts.force):
        log(f"  aligning the score with {os.path.basename(audio)} ...")
        _tool(opts, opts.python, opts.script, ["align", "--ref", ref_path, "--audio", audio, "--out",
                                               os.path.join(opts.work, "align.json"), "--tail", str(opts.tail)],
              "alignment", log)
        _remember(opts.work, akey, "align.json")
    act = os.path.join(opts.work, "act.npz")
    have_act = False
    bp = os.path.expanduser(opts.beats_python or "")
    if bp and os.path.isfile(bp) and opts.beats_script:
        bkey = hashlib.sha256(f"{_sig(audio)}|{_version(opts.beats_script)}".encode()).hexdigest()[:16]
        if not _cached(opts.work, bkey, "act.npz", opts.force):
            log("  detecting the recording's beats (madmom) ...")
            try:
                _tool(opts, bp, opts.beats_script, ["--audio", audio, "--out", act], "beat detection (madmom)", log)
                _remember(opts.work, bkey, "act.npz")
                have_act = True
            except RecTempoError as e:
                log(f"  madmom's beats not available ({str(e).splitlines()[0]}): librosa's only")
        else:
            have_act = True
    bars_path = os.path.join(opts.work, "bars.json")
    bk = hashlib.sha256(f"{akey}|{have_act and _sig(act)}|{tools}".encode()).hexdigest()[:16]
    if not _cached(opts.work, bk, "bars.json", opts.force):
        log("  placing the song's measures in the recording ...")
        args = ["bars", "--ref", ref_path, "--align", os.path.join(opts.work, "align.json"), "--audio", audio,
                "--out", bars_path]
        if have_act:
            args += ["--activations", act]
        _tool(opts, opts.python, opts.script, args, "bars", log)
        _remember(opts.work, bk, "bars.json")
    with open(bars_path, encoding="utf-8") as f:
        bars = json.load(f)
    targets = targets_from_bars(bars)
    if len(targets) != len(rd.timeline.measures):
        raise RecTempoError("the bars do not match the score's measures as played (the score changed?)")
    return rd, bars, targets


def _mscx_of(mscz: bytes) -> Tuple[str, bytes]:
    import io
    import zipfile
    from .handedits import _main_mscx_name
    with zipfile.ZipFile(io.BytesIO(mscz)) as z:
        member = _main_mscx_name(z)
        return member, z.read(member)


_OWN_MARK = re.compile(r"[ \t]*<Tempo>\s*(?:<play>0</play>\s*)?<tempo>[^<]*</tempo>\s*<followText>0</followText>\s*"
                       r"<visible>0</visible>\s*<text>" + re.escape(MARK_LABEL) + r" q = [^<]*</text>\s*</Tempo>\n?")


def strip_own_marks(mscx: bytes) -> bytes:
    """The tempo marks an earlier ``recordings tempo`` wrote taken out (a score timed to its recording again gets
    one set, not two)."""
    return _OWN_MARK.sub("", mscx.decode("utf-8")).encode("utf-8")


def strip_own_mscz(mscz: bytes) -> bytes:
    """The .mscz with :func:`strip_own_marks` applied (the same bytes when it has none).  Done before the plan is
    made: the marks' places in the file count the elements before them."""
    from .handedits import replace_mscx
    member, data = _mscx_of(mscz)
    new = strip_own_marks(data)
    return mscz if new == data else replace_mscx(mscz, member, new)


def write_copy(base: bytes, p: Plan, dst: str) -> str:
    """The score (``base``: .mscz bytes, unrolled or not, without an earlier run's marks: :func:`strip_own_mscz`)
    with the tempo map written in, at ``dst``."""
    from .handedits import replace_mscx
    from .timeline import apply_to_mscx
    member, data = _mscx_of(base)
    new = replace_mscx(base, member, apply_to_mscx(data, p.timeline_plan(), label=MARK_LABEL))
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(new)
    os.replace(tmp, dst)
    return dst


def run_song(score_path: str, audio: str, opts: Options, compat=None, options: Optional[dict] = None,
             orchestra: bool = True, simplified: bool = True, log: Log = print, dry_run: bool = False) -> dict:
    """Steps 1-4 and the verification for one song; writes the proposed copy ``opts.work/<score name>`` and the
    report (``tempo.json``, ``report.md``, ``audio_map.entry.json``).  Never writes the score.  ``dry_run``: the
    analysis and the decisions only (no copy, no report files)."""
    with open(score_path, "rb") as f:
        original = f.read()
    rd0, bars, targets = analyse(score_path, audio, opts, compat, options, log)
    with open(os.path.join(opts.work, "align.json"), encoding="utf-8") as f:
        al = json.load(f)
    key = int(al["key"]["offset"])
    opts.key = key
    tl0 = rd0.timeline
    written = [m.number - 1 for m in tl0.measures]
    lengths = [t.length for t in targets]
    shared = shared_tempo_errors(written, lengths, [t.rel for t in targets])
    repeated = len(set(written)) < len(written)
    unroll = repeated and (opts.unroll == "always" or (opts.unroll == "auto" and
                                                         shared.max_abs > opts.repeat_tolerance))
    base = strip_own_mscz(original)                    # a score timed before: its marks are replaced
    urep = None
    rd1 = rd0
    if base != original:
        bp = os.path.join(opts.work, "base.mscz")
        with open(bp, "wb") as f:
            f.write(base)
        rd1 = render(bp, compat, options)
    if unroll:
        base, urep = unroll_mscz(base, [m.number for m in tl0.measures])
        up = os.path.join(opts.work, "unrolled.mscz")
        with open(up, "wb") as f:
            f.write(base)
        rd1 = render(up, compat, options)
        if len(rd1.timeline.measures) != len(targets) or len({m.number for m in rd1.timeline.measures}) != len(targets):
            raise RecTempoError("the score written out does not play its measures once each, in order")
    p = plan(rd1.score, played_positions(rd1), targets, opts.tolerance)
    p.offset = offset_of(targets)
    name = os.path.basename(score_path)
    copy = os.path.join(opts.work, name)
    if not dry_run:
        write_copy(base, p, copy)
        log(f"  proposed copy: {copy}")
        ver = verify_copy(score_path, copy, rd0, targets, p, opts, compat, options, orchestra, simplified, audio, log)
    else:
        ver = {"summary": None, "checks": []}
    res = {"format": 1, "revision": REVISION, "score": score_path, "score_sha256": hashlib.sha256(original).hexdigest(),
           "recording": audio, "recording_sig": _sig(audio), "copy": None if dry_run else copy,
           "copy_sha256": None if dry_run else sha256_file(copy),
           "offset": round(p.offset, 4), "key_offset": key, "tuning_cents": al.get("tuning_cents"),
           "plan": p.stats, "plan_digest": p.digest(), "bars": bars_summary(bars, targets),
           "structure": [{"kind": g["kind"], "edge": g.get("edge", ""), "measures": g.get("measures") or [],
                          "song_s": g["song"], "recording_s": g["recording"]} for g in al.get("gaps", [])],
           "repeats": {"repeated_measures": sorted({w + 1 for w in written if written.count(w) > 1}),
                       "shared_tempo_max_ms": round(shared.max_abs * 1000, 1),
                       "shared_tempo_p95_ms": round(shared.p95() * 1000, 1),
                       "shared_tempo_start_max_ms": round(max([abs(x) for x in shared.start] or [0.0]) * 1000, 1),
                       "worst": _worst(shared, tl0, 8), "tolerance_ms": round(opts.repeat_tolerance * 1000, 1),
                       "unrolled": unroll, "unroll": urep.lines() if urep else None},
           "verify": ver.get("summary"), "checks": ver.get("checks")}
    res["ok"] = bool(res["checks"]) and all(c["ok"] for c in res["checks"])
    res["audio_map"] = audio_map_entry(audio, p.offset, res)
    if dry_run:
        return res
    _write_json(os.path.join(opts.work, STATE_NAME), res)
    _write_json(os.path.join(opts.work, "audio_map.entry.json"), res["audio_map"], indent=2)
    with open(os.path.join(opts.work, "report.md"), "w", encoding="utf-8") as f:
        f.write(report_markdown(res))
    return res


def _worst(sh: SharedErrors, tl, n: int) -> List[dict]:
    idx = sorted(range(len(sh.start)), key=lambda j: -max(abs(sh.start[j]), abs(sh.inside[j])))[:n]
    return [{"measure": tl.label(j), "start_ms": round(sh.start[j] * 1000, 1), "inside_ms": round(sh.inside[j] * 1000, 1)}
            for j in sorted(idx)]


def bars_summary(bars: dict, targets: Sequence[Target]) -> dict:
    conf = [t.conf for t in targets]
    src: Dict[str, int] = {}
    for t in targets:
        src[t.source] = src.get(t.source, 0) + 1
    odd = [{"measure": f"{t.number}:{t.passno}", "beats_score": t.beats_score, "beats_recording": t.beats_recording,
            "seconds": round(t.length, 3)} for t in targets if t.odd]
    return {"measures": len(targets), "primary_detector": bars.get("primary"), "detectors": bars.get("detectors"),
            "anchors": bars.get("anchors"), "anchors_confirmed": bars.get("anchors_confirmed"),
            "anchor_residual_beats_median": bars.get("anchor_residual_beats_median"),
            "confidence": {"high": sum(c >= 0.7 for c in conf), "medium": sum(0.4 <= c < 0.7 for c in conf),
                           "low": sum(c < 0.4 for c in conf), "median": round(statistics.median(conf), 3) if conf
                           else None},
            "sources": src, "odd_measures": odd,
            "recording_seconds": [round(targets[0].start, 3), round(targets[-1].end, 3)] if targets else None}


def audio_map_entry(audio: str, offset: float, res: dict) -> dict:
    home = os.path.expanduser("~")
    path = "~" + audio[len(home):] if audio.startswith(home + os.sep) else audio
    v = res.get("verify") or {}
    bs, be, no = (v.get("bar_starts") or {}), (v.get("beats") or {}), (v.get("note_onsets") or {})
    note = (f"the original recording, not stretched: the score's tempo map follows the recording (pianovision "
            f"recordings tempo; {res['bars']['measures']} measures"
            + (", repeats written out" if res["repeats"]["unrolled"] else "") +
            f"); offset {offset:+.3f} s = the recording time of the song's start; against the recording's "
            f"measures and beats: measure starts max {bs.get('max_ms')} ms, beats p95 {be.get('p95_ms')} ms, note "
            f"onsets p95 {no.get('p95_ms')} ms")
    problems = [c["name"] for c in res.get("checks", []) if not c["ok"]]
    if res.get("key_offset"):
        problems.append(f"the recording is {res['key_offset']:+d} semitone(s) from the score (transpose the score, or "
                        f"decide to play along in another key)")
    if problems:
        note += "; NOT ENABLED: " + "; ".join(problems)
    return {"audio": path, "offset": round(offset, 3), "enabled": bool(res.get("ok")) and not res.get("key_offset"),
            "note": note}


# ------------------------------------------------------------------------------
# verification of the copy
# ------------------------------------------------------------------------------

def _note_table(rd: Rendered) -> Dict[int, List[tuple]]:
    from .arrangement import sounding_notes
    tl = rd.timeline
    out: Dict[int, List[tuple]] = {}
    for n in sounding_notes(rd.midi, range(min(len(rd.midi.tracks), len(rd.score.staves))), tl):
        u = tl.utick(n.on)
        j = tl.measure_at_utick(u)
        out.setdefault(j, []).append((u - tl.measures[j].utick, tl.utick(n.off) - u, n.pitch, n.track, n.on, n.off))
    return {j: sorted(v) for j, v in out.items()}


def _lyric_table(rd: Rendered) -> Dict[Tuple[int, int, int], Tuple[str, str, bool]]:
    tl = rd.timeline
    lt = getattr(rd.midi, "lyrics", None)
    index = {(m.number, m.passno): j for j, m in enumerate(tl.measures)}
    out = {}
    for e in (lt.events if lt else []):
        j = index.get((e.measure, e.passes))
        if j is None:
            continue
        out[(j, e.track, tl.utick(e.tick) - tl.measures[j].utick)] = (e.text, e.syllabic, e.extend)
    return out


def verify_copy(score_path: str, copy: str, rd0: Rendered, targets: Sequence[Target], p: Plan, opts: Options,
                compat, options, orchestra: bool, simplified: bool, audio: str, log: Log) -> dict:
    """Step 5: the copy rendered by the formatter against the recording's measures and beats; its notes against the
    score's (timing aside); its lyrics; its parts file; MuseScore's check; the original recording against the copy's
    notes (``recording_align.py verify`` with the offset)."""
    from .convert import convert_mscz
    from .render import Compat
    checks: List[dict] = []
    summary: Dict[str, Any] = {}
    rd2 = render(copy, compat, options)
    tl = rd2.timeline
    off = p.offset
    if len(tl.measures) != len(targets):
        return {"summary": {}, "checks": [{"name": "measures", "ok": False,
                                           "detail": f"the copy plays {len(tl.measures)} measures, not {len(targets)}"}]}
    inside = [0.0 <= t.start for t in targets]
    # measure starts and beats
    bar_err, beat_err, beat_err_det = [], [], []
    for j, (m, t) in enumerate(zip(tl.measures, targets)):
        s = rd2.seconds(m.tick)
        if t.source != "outside":
            bar_err.append(s - (t.start - off))
        if t.odd or t.source == "outside" or t.beats_recording != t.beats_score:
            continue                        # a measure the recording plays with other beats: its start and end only
        bt = beat_ticks(m.timesig, rd2.midi.ticks_per_beat)
        for k, (tb, c) in enumerate(zip(t.beats, t.beat_conf or [0.0] * len(t.beats))):
            if k * bt >= m.length:
                break
            if k * bt not in t.rel:
                continue                    # no chord or rest starts on this beat: nothing is played there
            e = rd2.seconds(tl.tick(m.utick + k * bt)) - (tb - off)
            beat_err.append(e)
            if c >= 0.7:
                beat_err_det.append(e)
    # note onsets against where the recording plays their positions
    notes2 = _note_table(rd2)
    note_err = []
    for j, ns in notes2.items():
        t = targets[j]
        if t.source == "outside":
            continue
        pts = sorted(t.rel) + [tl.measures[j].length]
        for rt, _d, _pitch, _tr, on, _off in ns:
            note_err.append(rd2.seconds(on) - (t.start + _rel_at(t, rt, pts) - off))
    summary["bar_starts"] = percentiles(bar_err)
    summary["beats"] = percentiles(beat_err)
    summary["beats_detected"] = percentiles(beat_err_det)
    summary["note_onsets"] = percentiles(note_err)
    checks.append({"name": "timing", "ok": all((summary[k]["p95_ms"] or 0.0) <= 30.0
                                               for k in ("bar_starts", "beats", "note_onsets")),
                   "detail": f"measure starts {summary['bar_starts']}, beats {summary['beats']} (detected "
                             f"{summary['beats_detected']}), note onsets {summary['note_onsets']} against the "
                             f"recording's (the song's clock + offset {off:+.3f} s)"})
    # notes unchanged but for timing
    notes0 = _note_table(rd0)
    held, rolled, hard = [], [], []
    for j in sorted(set(notes0) | set(notes2)):
        a = [x[:4] for x in notes0.get(j, [])]
        b = [x[:4] for x in notes2.get(j, [])]
        if a == b:
            continue
        label = rd0.timeline.label(j)
        if len(a) == len(b) and {x[:1] + x[2:] for x in a} == {x[:1] + x[2:] for x in b}:
            held.append(label)              # same onsets, a length differs: a tie over a jump ends at its barline
            continue
        pa = sorted(a, key=lambda x: (x[2], x[3], x[0]))
        pb = sorted(b, key=lambda x: (x[2], x[3], x[0]))
        if len(pa) == len(pb) and all(x[2:] == y[2:] and abs(x[0] - y[0]) <= 120 and abs(x[1] - y[1]) <= 120
                                      for x, y in zip(pa, pb)):
            rolled.append(label)            # an arpeggio or ornament: its notes' offsets are seconds, not ticks
            continue
        hard.append(label)
    n0, n2 = sum(map(len, notes0.values())), sum(map(len, notes2.values()))
    checks.append({"name": "notes", "ok": n0 == n2 and not hard,
                   "detail": f"{n2} notes in the copy, {n0} in the score; measures with other notes: "
                             f"{hard or 'none'}; with a note held to its barline instead of over a jump (a tie into "
                             f"a measure not played next): {held or 'none'}; with an arpeggio rolled at the new "
                             f"tempo (offsets within an eighth): {rolled or 'none'}"})
    # lyrics: the same syllables per note and pass
    l0, l2 = _lyric_table(rd0), _lyric_table(rd2)
    missing = [k for k in l0 if k not in l2]
    wrong = [k for k in l0 if k in l2 and l0[k] != l2[k]]
    extra = [k for k in l2 if k not in l0]
    checks.append({"name": "lyrics", "ok": not missing and not wrong and not extra,
                   "detail": f"{len(l0) - len(missing) - len(wrong)} of {len(l0)} syllables on their notes and passes; "
                             f"missing {len(missing)}, read differently {len(wrong)}, extra {len(extra)}"})
    summary["lyrics"] = {"syllables": len(l0), "missing": len(missing), "different": len(wrong), "extra": len(extra)}
    # the parts file: orchestra and vocal lines
    c = Compat.from_dict(compat) if not isinstance(compat, Compat) else compat
    p0 = convert_mscz(score_path, c, orchestra, simplified, parts=True, options=options).parts or {}
    p2 = convert_mscz(copy, c, orchestra, simplified, parts=True, options=options).parts or {}

    def counts(doc):
        parts = doc.get("parts") or {}
        orch = [n for h in ("right", "left") for n in (parts.get("orchestra") or {}).get(h, [])]
        orig = [n for h in ("right", "left") for n in (parts.get("original") or {}).get(h, [])]
        return {"original": len(orig), "orchestra": len(orch), "vocal": sum(1 for n in orch if n.get("vocal")),
                "sung": sum(1 for n in orch + orig if n.get("lyric")), "lyrics": doc.get("lyrics")}
    c0, c2 = counts(p0), counts(p2)
    checks.append({"name": "parts", "ok": all(c0[k] == c2[k] for k in ("original", "orchestra", "vocal", "sung")),
                   "detail": f"parts file: copy {c2}, score {c0}"})
    summary["parts"] = {"copy": c2, "score": c0}
    # MuseScore: opens it, exports it (MusicXML with its lyrics; MIDI on the same clock as the formatter's)
    if opts.mscore:
        from .lyricsimport import musescore_check
        wait_for_memory(opts.min_free_mb, "MuseScore's check", log, opts.wait_s)
        ok, msg, n = musescore_check(opts.mscore, copy, opts.work)
        detail = msg + (f" ({n} lyrics exported)" if ok else "")
        if ok:
            mt = musescore_timing(opts.mscore, copy, opts.work, rd2)
            summary["musescore_vs_formatter"] = mt
            detail += f"; its MIDI against the formatter's note onsets: {mt}"
            ok = ok and mt.get("max_ms") is not None and mt["max_ms"] <= 5.0 and not mt.get("unmatched")
        checks.append({"name": "musescore", "ok": ok is not False, "detail": detail})
    else:
        checks.append({"name": "musescore", "ok": True, "detail": "not run"})
    # the original recording against the copy's notes, with the offset
    ref = reference(rd2)
    rp = os.path.join(opts.work, "copy-ref.json")
    _write_json(rp, ref)
    vp = os.path.join(opts.work, "copy-verify.json")
    try:
        _tool(opts, opts.python, opts.script, ["verify", "--ref", rp, "--audio", audio, "--key", str(opts.key),
                                               "--offset", f"{off:.6f}", "--out", vp], "verify", log)
        with open(vp, encoding="utf-8") as f:
            v = json.load(f)
        summary["recording_vs_copy"] = {"chroma_lag": {k: v["lag"].get(k) for k in ("windows", "windows_total",
                                                                                     "median_ms", "p5_ms", "p95_ms",
                                                                                     "within_50ms")},
                                        "onset_lag": {k: v["onset_lag"].get(k) for k in ("windows", "windows_total",
                                                                                         "median_ms", "p5_ms", "p95_ms",
                                                                                         "within_50ms")},
                                        "chroma_mean": v.get("chroma_mean")}
    except RecTempoError as e:
        summary["recording_vs_copy"] = {"error": str(e).splitlines()[0]}
    return {"summary": summary, "checks": checks}


def musescore_timing(mscore: str, path: str, work: str, rd: Rendered) -> dict:
    """MuseScore's MIDI export of ``path`` against the formatter's render ``rd``: every note-on paired with the
    formatter's nearest of the same pitch (median, p95, max of the difference; notes left unpaired)."""
    import subprocess
    from .arrangement import sounding_notes
    from .smf import merged_messages_seconds, read_midi
    env = dict(os.environ)
    base = os.path.join(work, "xdg")
    for k in ("config", "cache", "data"):
        os.makedirs(os.path.join(base, k), exist_ok=True)
        env[f"XDG_{k.upper()}_HOME"] = os.path.join(base, k)
    env["QT_QPA_PLATFORM"] = "offscreen"
    out = os.path.splitext(path)[0] + ".check.mid"
    try:
        r = subprocess.run([mscore, "-o", out, path], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)}
    if r.returncode != 0 or not os.path.isfile(out):
        return {"error": f"MIDI export failed (exit {r.returncode})"}
    ms: Dict[int, List[float]] = {}
    now = 0.0
    for msg, dt in merged_messages_seconds(read_midi(out)):
        now += dt
        if msg.type == "note_on" and msg.velocity > 0:
            ms.setdefault(msg.note, []).append(now)
    os.unlink(out)
    fm: Dict[int, List[float]] = {}
    tl = rd.timeline
    for n in sounding_notes(rd.midi, range(min(len(rd.midi.tracks), len(rd.score.staves))), tl):
        fm.setdefault(n.pitch, []).append(rd.seconds(n.on))
    diffs, unmatched = [], 0
    import bisect
    for pitch, ts in fm.items():
        cand = sorted(ms.get(pitch, []))
        for t in ts:
            i = bisect.bisect_left(cand, t)
            best = min((cand[k] for k in (i - 1, i) if 0 <= k < len(cand)), key=lambda x: abs(x - t), default=None)
            if best is None or abs(best - t) > 0.25:
                unmatched += 1
            else:
                diffs.append(best - t)
    d = percentiles(diffs)
    d["unmatched"] = unmatched
    d["musescore_notes"] = sum(map(len, ms.values()))
    return d


def report_markdown(res: dict) -> str:
    b, r, v = res["bars"], res["repeats"], res.get("verify") or {}
    lines = [f"# Recording tempo: {os.path.basename(res['score'])}", "",
             f"* recording: {res['recording']}", f"* proposed copy: {res['copy']} (sha256 {res['copy_sha256'][:12]})",
             f"* offset: {res['offset']:+.3f} s (Note Waterfall: audioFileTime = songTime + offset)",
             f"* key: the recording is {res['key_offset']:+d} semitone(s) from the score; tuning "
             f"{res.get('tuning_cents')} cents",
             f"* measures as played: {b['measures']}; in the recording {b['recording_seconds']}; confidence "
             f"{b['confidence']}; sources {b['sources']}; anchors {b['anchors']} ({b['anchors_confirmed']} confirmed by "
             f"both detectors), primary detector {b['primary_detector']}",
             f"* repeats: measures {_short(r['repeated_measures'])}; one tempo per written measure would be off by up "
             f"to {r['shared_tempo_max_ms']} ms (p95 {r['shared_tempo_p95_ms']} ms; tolerance {r['tolerance_ms']} ms): "
             + ("written out" if r["unrolled"] else "kept"),
             f"* tempo map: {res['plan']}"]
    if r.get("unroll"):
        lines += [f"  * {x}" for x in r["unroll"]]
    if b.get("odd_measures"):
        lines.append("* measures of another length: " + ", ".join(
            f"{o['measure']} ({o['beats_score']} beats, {o['beats_recording']} in the recording, {o['seconds']} s)"
            for o in b["odd_measures"]))
    if res.get("structure"):
        lines.append("* where the recording and the score differ (the alignment's gaps; the tempo map takes them up "
                     "inside the measures): " + "; ".join(
                         f"{g['kind']}{' (' + g['edge'] + ')' if g['edge'] else ''} m. {', '.join(g['measures']) or '-'}"
                         f" (score {g['song_s'][1] - g['song_s'][0]:.1f} s, recording "
                         f"{g['recording_s'][1] - g['recording_s'][0]:.1f} s)" for g in res["structure"]))
    lines += ["", "## Checks", ""]
    for c in res["checks"]:
        lines.append(f"* {c['name']}: {'ok' if c['ok'] else 'FAILED'}: {c['detail']}")
    if v.get("recording_vs_copy"):
        lines.append(f"* the recording against the copy's notes: {v['recording_vs_copy']}")
    lines += ["", f"audio_map entry: `{json.dumps(res['audio_map'])}`", ""]
    return "\n".join(lines)


def _short(nums: Sequence[int]) -> str:
    from .scorefix import ranges
    return ranges(sorted(nums)) or "none"


def _write_json(path: str, d: Any, indent: int = 1) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=indent, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------------------
# applying a verified copy to the score
# ------------------------------------------------------------------------------

def apply_copy(score_path: str, work: str, backup: str, log: Log = print) -> str:
    """The verified copy of ``work`` (``tempo.json``) over the score: refused when the score changed since the copy
    was made, when the copy changed or failed a check, or when a backup of the same name exists; the original is
    copied to ``backup`` first and checked; the score is written atomically with its file mode kept."""
    import shutil
    import tempfile
    with open(os.path.join(work, STATE_NAME), encoding="utf-8") as f:
        st = json.load(f)
    if os.path.abspath(st["score"]) != os.path.abspath(score_path):
        raise RecTempoError(f"the copy in {work} was made from {st['score']}, not {score_path}")
    if not st.get("ok"):
        raise RecTempoError("the copy did not pass its checks: " + "; ".join(
            c["name"] for c in st.get("checks", []) if not c["ok"]))
    with open(score_path, "rb") as f:
        cur = f.read()
    if hashlib.sha256(cur).hexdigest() != st["score_sha256"]:
        raise RecTempoError("the score changed since the copy was made: run `recordings tempo` again")
    if sha256_file(st["copy"]) != st["copy_sha256"]:
        raise RecTempoError(f"{st['copy']} changed since it was verified")
    os.makedirs(backup, exist_ok=True)
    bak = os.path.join(backup, os.path.basename(score_path))
    if os.path.exists(bak):
        raise RecTempoError(f"{bak} exists (a backup is never overwritten)")
    shutil.copy2(score_path, bak)
    if sha256_file(bak) != st["score_sha256"]:
        raise RecTempoError("the backup differs from the score; nothing written")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(score_path)), prefix=".rectempo-", suffix=".tmp")
    with os.fdopen(fd, "wb") as f, open(st["copy"], "rb") as g:
        f.write(g.read())
    shutil.copymode(score_path, tmp)
    os.replace(tmp, score_path)
    st["applied"] = {"score": score_path, "sha256": st["copy_sha256"], "backup": bak}
    _write_json(os.path.join(work, STATE_NAME), st)
    log(f"{score_path}: the recording's tempo map written (original: {bak})")
    return bak
