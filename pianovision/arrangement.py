"""A score's simplified part from another score: a piano arrangement of the same music, measure by measure.

A piano-and-orchestra score (Rhapsody in Blue with its orchestra) and a piano solo arrangement of the same piece
(the arranger folds the orchestra into the piano part, cuts passages, adds a bar here and there) are aligned
measure to measure; the arrangement's notes are then moved onto the orchestral score's measures and become the
simplified part of its Note Waterfall parts file (:mod:`pianovision.parts`), which Note Waterfall shows by default.
The orchestral score's own piano part stays the ``original`` part, its orchestra the ``orchestra``.  The song plays
on the arrangement's clock (:mod:`pianovision.timeline`): the orchestral score is rendered with the solo's tempo map
carried across the alignment, by the formatter and, for the companion audio, by MuseScore.  The same alignment read
backwards (:class:`Retimer` with ``reverse``) moves an orchestral score's orchestra onto a piano solo's measures
(``orchestra_from``).

**Alignment** (:func:`align`).  Both scores are unrolled through their repeats and jumps (:class:`Timeline`: one
:class:`UMeasure` per measure as played).  Every measure gets features (:func:`measure_features`): a
duration-weighted pitch-class profile (chroma), the pitch class of the lowest and of the highest sounding note over
time (bass line, melody) and its onsets.  The arrangement is compared with the orchestral score's piano part and
with its piano plus orchestra (a solo arrangement folds the orchestra in): the better of the two counts.  A global
alignment (Gotoh's affine gaps: Needleman-Wunsch with a cost to open a cut and a smaller one per measure) maps
measures one to one, in order, with cuts on either side: orchestral measures the arrangement leaves out
(``fill``) and arrangement measures the orchestral score does not have (``drop``).  Matching texts help (tempo
words, sections, rehearsal marks that occur at most twice in each score, words compared loosely: "Grandisoso" is
"Grandioso"), and a time signature or measure length that differs costs extra.  The map is then refined at the cuts
(:func:`refine`): a measure that sounds much more like one inside the cut moves there, a dropped measure that
continues its neighbour's orchestral measure joins it (two arrangement measures over one :class:`Unit`), a measure
that sounds little like its counterpart joins the cut.  A passage the arrangement plays in another key
(:func:`find_transposed`) is reported and filled.

**Overrides** (``measure_map`` in the score's build options, :mod:`pianovision.buildopts`): ``"auto"``, or
``{"pin": [["arr", "orch"], ...], "fill": ["orch", ...], "drop": ["arr", ...]}`` with measures written as the
score numbers them, ``"12"``, ``"12-30"``, and ``"395:2"`` for the second pass through measure 395 (a range starts
at the first pass of its first measure unless given): a pin maps the arrangement's measures to the orchestral
score's one to one (equal lengths), ``fill`` keeps orchestral measures for the orchestral piano part, ``drop``
leaves arrangement measures out.  The rest is aligned automatically around them.

**Retiming** (:func:`retime`, :class:`Retimer`).  A note of the arrangement at (its unit, offset in it) is played at
the orchestral unit's start plus that offset, on the orchestral render's clock (the arrangement's, carried over:
:mod:`pianovision.timeline`), exactly as the formatter times its own notes.  Units of another length on the two sides are warped through the
onsets both strike at the same pitch (:func:`unit_warp`).  A note held into a unit that does not follow on in the
orchestral score (a cut) ends at the cut; a note held from a dropped measure into a mapped one is struck again where
the mapped measure starts.  Orchestral measures without counterpart are filled from the orchestral score's own piano
part, so the simplified part has no holes where the piano plays; their notes end where the arrangement takes over
again.  Arrangement notes carry ``"simplified": 1`` (they are not the orchestral score's notes: Note Waterfall
counts their hand-edit identity apart from the original part's); the filled notes are the original part's own notes
and do not.

**The piano part from the arrangement** (``piano_from: "simplified_from"``, :class:`PianoFrom`).  The arrangement is
the source of truth for the song's piano part too: its notes, retimed as above, replace the orchestral score's piano
part in the measures the alignment maps, before PianoVision's JSON and the parts are built from the render
(:func:`pianovision.pvjson.build_song`), so PianoVision's hands and the ``original`` part are the arrangement's notes
there and the orchestral score's piano part in the fills, exactly what the simplified part is otherwise.  The song
then has no separate simplified part.  The arrangement's notes keep ``"simplified": 1`` in the original part (Note
Waterfall reads a note so marked as one of the simplified notes: the same hand-edit identity as before, apart from
the orchestral score's notes); their accents, velocities and lyrics are the arrangement's.  The clock is the same
either way, and so is the companion audio.

The report (:func:`report_markdown`, ``<output>/.arrangements/<song>.md`` and ``.json``) lists the mapped segments
with their confidence (mean similarity, and how much better the segment fits than shifted by a measure or more),
the cuts on both sides, the filled and dropped measures, time-signature mismatches and the text anchors.
"""

from __future__ import annotations

import bisect
import difflib
import math
import operator
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

INF = float("inf")

# alignment costs (a match costs 1 - similarity, in 0..1)
GAP_OPEN = 1.0            # a cut (on either side) starts
GAP_EXTEND = 0.35         # per measure of a cut
TIMESIG_COST = 0.25       # mapped measures with another time signature or length
TRANSPOSE_COST = 0.2      # comparing the chroma transposed
ANCHOR_BONUS = 0.3        # a matching distinctive text (times the texts' similarity)
EMPTY_SIMILARITY = 0.75   # two measures where nothing sounds
PIN_COST = -5.0           # a pinned pair (measure_map overrides)
RESTRIKE_MIN = 120        # a note held from a dropped measure into a mapped one is struck again if it lasts this long
SHIFTS = (1, 2, 3, 4)     # the segment margin compares the mapping with these shifts

WEIGHTS = {"chroma": 0.5, "bass": 0.25, "top": 0.25}


# ------------------------------------------------------------------------------
# a score's playback: unrolled measures on the render's clock
# ------------------------------------------------------------------------------

@dataclass
class UMeasure:
    """One measure as played (repeats and jumps unrolled)."""
    index: int                      # position in the playback, 0-based
    number: int                     # 1-based measure number in the score
    passno: int                     # the how-manieth time the playback reaches this measure
    utick: int                      # start, unrolled ticks without pauses
    length: int                     # actual length, ticks
    timesig: Tuple[int, int]        # nominal time signature
    tick: int = 0                   # start on the render's MIDI clock (pauses included)
    end_tick: int = 0               # end on that clock (the next measure's start)

    @property
    def end_utick(self) -> int:
        return self.utick + self.length

    def label(self, repeated: bool = False) -> str:
        return f"{self.number}" + (f":{self.passno}" if repeated or self.passno > 1 else "")


@dataclass
class SNote:
    """A sounding note of a staff, unrolled ticks."""
    track: int
    pitch: int
    on: int
    off: int


class Timeline:
    """A render's measures as played and its clocks: unrolled ticks (``utick``, the score's positions laid end to end)
    and the MIDI ticks of the render (pauses inserted: ``tick``)."""

    def __init__(self, measures: List[UMeasure], pause_keys: Sequence[int] = (0,), pause_offsets: Sequence[int] = (0,),
                 texts: Optional[List[Tuple[int, str, str]]] = None):
        self.measures = measures
        self.pause_keys = list(pause_keys) or [0]
        self.pause_offsets = list(pause_offsets) or [0]
        for m in measures:
            m.tick = self.tick(m.utick)
        for a, b in zip(measures, measures[1:]):
            a.end_tick = b.tick
        if measures:
            measures[-1].end_tick = self.tick(measures[-1].end_utick)
        self._starts = [m.tick for m in measures]
        self._ustarts = [m.utick for m in measures]
        self.texts = texts or []        # (measure index, kind, text)
        counts: Dict[int, int] = {}
        for m in measures:
            counts[m.number] = counts.get(m.number, 0) + 1
        self.repeated = {n for n, c in counts.items() if c > 1}

    @classmethod
    def from_render(cls, renderer, midi=None) -> "Timeline":
        """The timeline of a :class:`pianovision.render.Renderer` after ``render()`` (``midi``: its MidiFile, for the
        texts)."""
        from .score import ticks
        measures: List[UMeasure] = []
        passes: Dict[int, int] = {}
        for rs in renderer.repeat_list:
            off = rs.utick - rs.tick
            for m in rs.measures:
                passes[m.index] = passes.get(m.index, 0) + 1
                measures.append(UMeasure(len(measures), m.index + 1, passes[m.index], ticks(m.tick) + off,
                                         ticks(m.len), tuple(m.timesig_nd)))
        pm = renderer.pause_map
        keys = list(pm.keys) or [0]
        tl = cls(measures, keys, [pm.values[k] for k in keys] if pm.keys else [0])
        if midi is not None:
            tl.texts = score_texts(tl, getattr(midi, "annotations", None) or [])
        return tl

    # -- clocks ------------------------------------------------------------------
    def tick(self, utick: int) -> int:
        """Unrolled tick -> MIDI tick (PauseMap.tick_with_pauses)."""
        i = bisect.bisect_right(self.pause_keys, utick) - 1
        return utick + self.pause_offsets[max(i, 0)]

    def utick(self, tick: int) -> int:
        """MIDI tick -> unrolled tick; a tick inside an inserted pause is the pause's place."""
        lo, hi = 0, len(self.pause_keys)
        while lo < hi:                                  # last pause key whose MIDI position is <= tick
            mid = (lo + hi) // 2
            if self.pause_keys[mid] + self.pause_offsets[mid] <= tick:
                lo = mid + 1
            else:
                hi = mid
        i = max(lo - 1, 0)
        u = tick - self.pause_offsets[i]
        if i + 1 < len(self.pause_keys) and u > self.pause_keys[i + 1]:
            u = self.pause_keys[i + 1]
        return u

    def measure_at(self, tick: int) -> int:
        """Index of the measure that plays at MIDI tick ``tick`` (clamped to the first / last)."""
        i = bisect.bisect_right(self._starts, tick) - 1
        return min(max(i, 0), len(self.measures) - 1)

    def measure_at_utick(self, utick: int) -> int:
        i = bisect.bisect_right(self._ustarts, utick) - 1
        return min(max(i, 0), len(self.measures) - 1)

    def label(self, i: int) -> str:
        m = self.measures[i]
        return m.label(m.number in self.repeated)

    def resolve(self, spec: str) -> List[int]:
        """Measure indices of ``"12"``, ``"12:2"``, ``"12-30"``, ``"395:2-400"`` (score numbers, ``:pass``): a single
        measure without a pass is every pass of it; a range runs from its first measure (that pass, else the first)
        to the next pass through its last."""
        spec = str(spec).strip()
        m = re.fullmatch(r"(\d+)(?::(\d+))?(?:\s*-\s*(\d+)(?::(\d+))?)?", spec)
        if not m:
            raise ValueError(f"measure_map: cannot read {spec!r} (12, 12:2, 12-30)")
        a, ap, b, bp = m.group(1), m.group(2), m.group(3), m.group(4)
        a, ap = int(a), int(ap) if ap else None
        starts = [x.index for x in self.measures if x.number == a and (ap is None or x.passno == ap)]
        if not starts:
            raise ValueError(f"measure_map: no measure {spec.split('-')[0]} in the playback")
        if b is None:
            return starts
        b, bp = int(b), int(bp) if bp else None
        s = starts[0]
        for k in range(s, len(self.measures)):
            x = self.measures[k]
            if x.number == b and (bp is None or x.passno == bp):
                return list(range(s, k + 1))
        raise ValueError(f"measure_map: {spec!r}: measure {b} does not follow {a} in the playback")


def score_texts(tl: Timeline, annotations) -> List[Tuple[int, str, str]]:
    """(measure index, kind, text) of the render's rehearsal marks, sections and tempo words
    (:func:`pianovision.markers.classify`), any staff."""
    from .markers import classify
    out = []
    seen = set()
    for t in annotations:
        if not getattr(t, "visible", True):
            continue
        c = classify(t.tag, t.text, getattr(t, "subtype", ""))
        if c is None or c[1] not in ("rehearsal", "section", "tempo"):
            continue
        i = tl.measure_at(t.tick)
        key = (i, c[1], c[0].casefold())
        if key not in seen:
            seen.add(key)
            out.append((i, c[1], c[0]))
    return sorted(out)


def sounding_notes(midi, tracks: Iterable[int], tl: Timeline) -> List[SNote]:
    """The notes of the MIDI ``tracks`` (note-on to note-off, per channel and pitch, first in first out), unrolled."""
    out: List[SNote] = []
    for ti in tracks:
        if ti >= len(midi.tracks):
            continue
        tick = 0
        active: Dict[Tuple[int, int], List[int]] = {}
        for msg in midi.tracks[ti]:
            tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                active.setdefault((msg.channel, msg.note), []).append(tick)
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                ons = active.get((msg.channel, msg.note))
                if ons:
                    t0 = ons.pop(0)
                    out.append(SNote(ti, msg.note, tl.utick(t0), max(tl.utick(tick), tl.utick(t0) + 1)))
    out.sort(key=lambda n: (n.on, n.pitch))
    return out


# ------------------------------------------------------------------------------
# features
# ------------------------------------------------------------------------------

@dataclass
class Features:
    chroma: List[float] = field(default_factory=lambda: [0.0] * 12)   # sounding pitch classes, duration-weighted
    bass: List[float] = field(default_factory=lambda: [0.0] * 12)     # the lowest sounding note's pitch class
    top: List[float] = field(default_factory=lambda: [0.0] * 12)      # the highest sounding note's pitch class
    onsets: int = 0                                                    # distinct note-on positions
    notes: int = 0                                                     # notes struck in the measure
    sounding: int = 0                                                  # ticks with something sounding

    @property
    def empty(self) -> bool:
        return self.sounding == 0


def _unit(v: List[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n > 0 else v


def measure_features(tl: Timeline, notes: Sequence[SNote]) -> List[Features]:
    """One :class:`Features` per measure of ``tl`` from ``notes`` (unrolled ticks); profiles have unit length."""
    feats = [Features() for _ in tl.measures]
    per: List[List[Tuple[int, int, int]]] = [[] for _ in tl.measures]   # (start, end, pitch), clipped to the measure
    onsets: List[set] = [set() for _ in tl.measures]
    for n in notes:
        i = tl.measure_at_utick(n.on)
        feats[i].notes += 1
        onsets[i].add(n.on)
        while i < len(tl.measures):
            m = tl.measures[i]
            if m.utick >= n.off:
                break
            a, b = max(n.on, m.utick), min(n.off, m.end_utick)
            if b > a:
                per[i].append((a, b, n.pitch))
            i += 1
    for i, items in enumerate(per):
        f = feats[i]
        f.onsets = len(onsets[i])
        for a, b, p in items:
            f.chroma[p % 12] += b - a
        points = sorted({a for a, _b, _p in items} | {b for _a, b, _p in items})
        for x, y in zip(points, points[1:]):
            sounding = [p for a, b, p in items if a <= x and b >= y]
            if not sounding:
                continue
            f.sounding += y - x
            f.bass[min(sounding) % 12] += y - x
            f.top[max(sounding) % 12] += y - x
        f.chroma, f.bass, f.top = _unit(f.chroma), _unit(f.bass), _unit(f.top)
    return feats


def _cos(a: Sequence[float], b: Sequence[float], shift: int = 0) -> float:
    if shift:
        return sum(a[k] * b[(k + shift) % 12] for k in range(12))
    return sum(x * y for x, y in zip(a, b))


def similarity(a: Features, b: Features, shift: int = 0) -> float:
    """0..1: how alike two measures sound (``shift``: ``a`` transposed up by that many semitones)."""
    if a.empty or b.empty:
        return EMPTY_SIMILARITY if a.empty and b.empty else 0.0
    return (WEIGHTS["chroma"] * _cos(a.chroma, b.chroma, shift) + WEIGHTS["bass"] * _cos(a.bass, b.bass, shift)
            + WEIGHTS["top"] * _cos(a.top, b.top, shift))


def similarity_matrix(arr: Sequence[Features], orch: Sequence[Sequence[Features]]) -> List[List[float]]:
    """``S[i][j]``: the best similarity of arrangement measure i with orchestral measure j over the orchestral
    feature sets ``orch`` (its piano part, and its piano plus orchestra)."""
    W = (math.sqrt(WEIGHTS["chroma"]), math.sqrt(WEIGHTS["bass"]), math.sqrt(WEIGHTS["top"]))

    def vec(f: Features) -> Optional[Tuple[float, ...]]:
        if f.empty:
            return None
        return tuple(W[0] * x for x in f.chroma) + tuple(W[1] * x for x in f.bass) + tuple(W[2] * x for x in f.top)

    av = [vec(f) for f in arr]
    ovs = [[vec(f) for f in feats] for feats in orch]
    m = len(ovs[0]) if ovs else 0
    S = []
    for a in av:
        row = [0.0] * m
        for j in range(m):
            best = 0.0
            for col in ovs:
                b = col[j]
                if a is None or b is None:
                    s = EMPTY_SIMILARITY if a is None and b is None else 0.0
                else:
                    s = sum(map(operator.mul, a, b))
                if s > best:
                    best = s
            row[j] = best
        S.append(row)
    return S


def best_transposition(a: Features, orch: Sequence[Features]) -> Tuple[int, float, float]:
    """(semitones, similarity transposed, similarity as written) of the transposition (-5..6) under which ``a``
    sounds most like the best of the orchestral feature sets ``orch`` (one measure)."""
    s0 = max(similarity(a, b) for b in orch)
    best, bk = s0, 0
    for k in range(-5, 7):
        if k:
            s = max(similarity(a, b, k) for b in orch)
            if s > best + 1e-9:
                best, bk = s, k
    return bk, best, s0


# ------------------------------------------------------------------------------
# text anchors
# ------------------------------------------------------------------------------

_STOP = {"a", "e", "ed", "ma", "non", "il", "al", "la", "le", "di", "del", "con", "and", "the", "not", "too", "poco",
         "molto", "più", "piu", "meno", "tempo", "mosso", "assai", "very", "un", "una", "sempre", "quasi", "ben"}


def _text_words(text: str) -> Tuple[str, ...]:
    t = unicodedata.normalize("NFKD", text.casefold())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return tuple(w for w in re.findall(r"[a-z0-9]+", t) if w not in _STOP)


def _word_match(a: str, b: str) -> bool:
    return a == b or (len(a) >= 5 and len(b) >= 5 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)


def text_similarity(a: str, b: str) -> float:
    wa, wb = _text_words(a), _text_words(b)
    if not wa or not wb:
        return 0.0
    hits = sum(1 for x in wa if any(_word_match(x, y) for y in wb))
    return hits / max(len(wa), len(wb))


@dataclass
class Anchor:
    arr: int                # arrangement measure index
    orch: int               # orchestral measure index
    arr_text: str
    orch_text: str
    similarity: float


def text_anchors(arr: Timeline, orch: Timeline, min_similarity: float = 0.5, max_count: int = 2) -> List[Anchor]:
    """Pairs of matching texts (tempo words, sections, rehearsal marks) that are distinctive: each occurs at most
    ``max_count`` times in its score (passes of a repeat counted), words compared loosely."""
    def by_key(tl: Timeline):
        d: Dict[Tuple[str, ...], List[Tuple[int, str]]] = {}
        for i, kind, text in tl.texts:
            words = _text_words(text)
            if not words or (kind == "rehearsal" and len(text) > 3 and not words):
                continue
            d.setdefault(words, []).append((i, text))
        return {k: v for k, v in d.items() if len(v) <= max_count}

    a_keys, o_keys = by_key(arr), by_key(orch)
    out: List[Anchor] = []
    for ka, la in a_keys.items():
        for ko, lo in o_keys.items():
            s = text_similarity(" ".join(ka), " ".join(ko))
            if s < min_similarity:
                continue
            for i, ta in la:
                for j, to in lo:
                    out.append(Anchor(i, j, ta, to, s))
    return sorted(out, key=lambda x: (x.arr, x.orch))


# ------------------------------------------------------------------------------
# alignment
# ------------------------------------------------------------------------------

@dataclass
class Overrides:
    pins: List[Tuple[int, int]] = field(default_factory=list)      # (arr index, orch index)
    fill: List[int] = field(default_factory=list)                   # orch indices without counterpart
    drop: List[int] = field(default_factory=list)                   # arr indices without counterpart


def read_overrides(spec: Any, arr: Timeline, orch: Timeline) -> Overrides:
    """``measure_map`` of the build options -> :class:`Overrides` (``"auto"`` / None: none)."""
    o = Overrides()
    if spec in (None, "auto", "", {}):
        return o
    if not isinstance(spec, dict):
        raise ValueError(f"measure_map must be \"auto\" or an object, not {spec!r}")
    unknown = sorted(k for k in spec if k not in ("pin", "fill", "drop") and not k.startswith("_"))
    if unknown:
        raise ValueError(f"measure_map: unknown key(s) {', '.join(unknown)} (pin, fill, drop)")
    for pair in spec.get("pin", []):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"measure_map pin {pair!r}: [arrangement measures, orchestral measures]")
        a, b = arr.resolve(pair[0]), orch.resolve(pair[1])
        if len(a) != len(b):
            raise ValueError(f"measure_map pin {pair!r}: {len(a)} arrangement measures, {len(b)} orchestral")
        o.pins.extend(zip(a, b))
    for s in spec.get("fill", []):
        o.fill.extend(orch.resolve(s))
    for s in spec.get("drop", []):
        o.drop.extend(arr.resolve(s))
    return o


@dataclass
class Unit:
    """Arrangement measures played over orchestral measures: one to one, or two arrangement measures over one
    (a passage the arrangement writes out at half speed)."""
    arr: Tuple[int, int]            # first, last arrangement measure index
    orch: Tuple[int, int]           # first, last orchestral measure index


@dataclass
class Segment:
    """A run of units that follow on from each other on both sides."""
    arr: Tuple[int, int]            # first, last arrangement measure index
    orch: Tuple[int, int]
    similarity: float               # mean similarity of the mapped measures (0..1)
    margin: float                   # mean similarity minus the best mean of the same run shifted by SHIFTS measures
    pinned: bool = False

    @property
    def length(self) -> int:
        return self.arr[1] - self.arr[0] + 1

    @property
    def confidence(self) -> str:
        if self.pinned:
            return "pinned"
        if self.similarity >= 0.6 and (self.margin >= 0.05 or self.length <= 2):
            return "high"
        if self.similarity >= 0.45:
            return "medium"
        return "low"


@dataclass
class Transposed:
    """A passage the arrangement plays in another key: reported; its orchestral measures are filled."""
    arr: Tuple[int, int]
    orch: Tuple[int, int]
    semitones: int                  # the arrangement sounds this many semitones above the orchestral score
    similarity: float               # mean similarity once transposed back
    mapped: bool                    # the alignment had mapped it (else it sat in a cut on both sides)


@dataclass
class Alignment:
    """The measure map: ``mapping[i]`` = the orchestral measure arrangement measure i plays over (None: dropped; two
    consecutive arrangement measures over one orchestral measure form one :class:`Unit`); ``fills``: orchestral
    measures without counterpart (no arrangement measure, or a transposed passage), filled from the orchestral
    piano part."""
    arr: Timeline
    orch: Timeline
    mapping: List[Optional[int]]
    similarity: List[List[float]]
    anchors: List[Anchor] = field(default_factory=list)
    overrides: Overrides = field(default_factory=Overrides)
    cost: float = 0.0
    transposed: List[Transposed] = field(default_factory=list)
    moved: List[Tuple[int, int, int]] = field(default_factory=list)     # (arr, from orch, to orch): refined
    merged: List[Tuple[int, int]] = field(default_factory=list)         # (arr, orch): a dropped measure joined
    trimmed: List[Tuple[int, int]] = field(default_factory=list)        # (arr, orch): unmapped at a cut's edge
    units: List[Unit] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)

    @property
    def drops(self) -> List[int]:
        return [i for i, j in enumerate(self.mapping) if j is None]

    @property
    def fills(self) -> List[int]:
        used = {j for j in self.mapping if j is not None}
        return [j for j in range(len(self.orch.measures)) if j not in used]

    def orch_to_arr(self) -> Dict[int, List[int]]:
        out: Dict[int, List[int]] = {}
        for i, j in enumerate(self.mapping):
            if j is not None:
                out.setdefault(j, []).append(i)
        return out

    def finish(self) -> "Alignment":
        """Units and segments from the mapping."""
        self.units = []
        for i, j in enumerate(self.mapping):
            if j is None:
                continue
            u = self.units[-1] if self.units else None
            if u is not None and u.arr[1] == i - 1 and u.orch == (j, j) and u.arr[0] == u.arr[1]:
                u.arr = (u.arr[0], i)                    # two arrangement measures over one orchestral measure
            else:
                self.units.append(Unit((i, i), (j, j)))
        self.segments = segments_of(self)
        return self


def _gotoh(n: int, m: int, cost: Callable[[int, int], float], pinned_rows: Iterable[int] = (),
           pinned_cols: Iterable[int] = ()) -> Tuple[List[Optional[int]], float]:
    """Global one-to-one alignment of n rows with m columns, in order, with affine gaps on both sides: (row ->
    column or None, total cost; INF when impossible).  ``cost(i, j)``: of mapping row i to column j (INF: never);
    pinned rows / columns are never left out."""
    pinned_rows, pinned_cols = set(pinned_rows), set(pinned_cols)
    OPEN = GAP_OPEN + GAP_EXTEND
    # M: row i-1 mapped to column j-1 last; X: column j-1 left out last; Y: row i-1 left out last.  B*: the table
    # the best predecessor came from (0 M, 1 X, 2 Y).
    M = [[INF] * (m + 1) for _ in range(n + 1)]
    X = [[INF] * (m + 1) for _ in range(n + 1)]
    Y = [[INF] * (m + 1) for _ in range(n + 1)]
    BM = [bytearray(m + 1) for _ in range(n + 1)]
    BX = [bytearray(m + 1) for _ in range(n + 1)]
    BY = [bytearray(m + 1) for _ in range(n + 1)]
    M[0][0] = 0.0
    for j in range(1, m + 1):
        if (j - 1) in pinned_cols:
            break
        X[0][j] = GAP_OPEN + GAP_EXTEND * j
        BX[0][j] = 1 if j > 1 else 0
    for i in range(1, n + 1):
        if (i - 1) in pinned_rows:
            break
        Y[i][0] = GAP_OPEN + GAP_EXTEND * i
        BY[i][0] = 2 if i > 1 else 0
    for i in range(1, n + 1):
        Mi, Mp, Xi, Xp, Yi, Yp = M[i], M[i - 1], X[i], X[i - 1], Y[i], Y[i - 1]
        BMi, BXi, BYi = BM[i], BX[i], BY[i]
        row_free = (i - 1) not in pinned_rows
        for j in range(1, m + 1):
            c = cost(i - 1, j - 1)
            if c < INF:
                a, b, d = Mp[j - 1], Xp[j - 1], Yp[j - 1]
                if a <= b and a <= d:
                    Mi[j], BMi[j] = a + c, 0
                elif b <= d:
                    Mi[j], BMi[j] = b + c, 1
                else:
                    Mi[j], BMi[j] = d + c, 2
            if (j - 1) not in pinned_cols:
                a, b, d = Mi[j - 1] + OPEN, Xi[j - 1] + GAP_EXTEND, Yi[j - 1] + OPEN
                if b <= a and b <= d:
                    Xi[j], BXi[j] = b, 1
                elif a <= d:
                    Xi[j], BXi[j] = a, 0
                else:
                    Xi[j], BXi[j] = d, 2
            if row_free:
                a, b, d = Mp[j] + OPEN, Yp[j] + GAP_EXTEND, Xp[j] + OPEN
                if b <= a and b <= d:
                    Yi[j], BYi[j] = b, 2
                elif a <= d:
                    Yi[j], BYi[j] = a, 0
                else:
                    Yi[j], BYi[j] = d, 1
    finals = (M[n][m], X[n][m], Y[n][m])
    state = min(range(3), key=lambda k: finals[k])
    total = finals[state]
    mapping: List[Optional[int]] = [None] * n
    if total == INF:
        return mapping, INF
    i, j = n, m
    while i > 0 or j > 0:
        if i == 0:
            state = 1
        elif j == 0:
            state = 2
        if state == 0:
            mapping[i - 1] = j - 1
            state = BM[i][j]
            i, j = i - 1, j - 1
        elif state == 1:
            state = BX[i][j]
            j -= 1
        else:
            state = BY[i][j]
            i -= 1
    return mapping, total


def align(S: List[List[float]], arr: Timeline, orch: Timeline, anchors: Sequence[Anchor] = (),
          overrides: Optional[Overrides] = None, arr_feats: Optional[Sequence[Features]] = None,
          orch_feats: Optional[Sequence[Sequence[Features]]] = None,
          orch_piano: Optional[Sequence[int]] = None) -> Alignment:
    """The measure map between ``arr`` and ``orch`` given the similarity matrix ``S`` (:func:`similarity_matrix`):

    1. the best one-to-one, in-order map with cuts on either side (Gotoh's affine gaps), text anchors and the
       overrides (pins, fills, drops);
    2. refined (:func:`refine`): a measure at the edge of a cut that sounds much more like an orchestral measure
       inside the cut moves there (the cost of opening a cut keeps the alignment from splitting a cut for one
       measure); a dropped arrangement measure that continues its mapped neighbour's orchestral measure joins it
       (two arrangement measures over one: a passage written out at half speed); a measure at the edge of a cut
       that sounds little like its counterpart (an arranger's bridge over the cut) joins the cut where the
       orchestral piano plays there (``orch_piano``: ticks it sounds per orchestral measure);
    3. with the features (``arr_feats``, ``orch_feats``: the orchestral feature sets of ``S``), passages the
       arrangement plays in another key (:func:`find_transposed`): reported, their orchestral measures filled."""
    ov = overrides or Overrides()
    bonus: Dict[Tuple[int, int], float] = {}
    for a in anchors:
        bonus[(a.arr, a.orch)] = max(bonus.get((a.arr, a.orch), 0.0), ANCHOR_BONUS * a.similarity)
    pinned_rows = {i: j for i, j in ov.pins}
    pinned_cols = {j: i for i, j in ov.pins}
    no_row, no_col = set(ov.drop), set(ov.fill)
    if (no_row & set(pinned_rows)) or (no_col & set(pinned_cols)):
        raise ValueError("measure_map: measures both pinned and filled/dropped")

    def cost(i: int, j: int) -> float:
        if i in no_row or j in no_col:
            return INF
        pi, pj = pinned_rows.get(i), pinned_cols.get(j)
        if pi is not None or pj is not None:
            return PIN_COST if pi == j else INF
        return match_cost(S, arr, orch, i, j) - bonus.get((i, j), 0.0)

    mapping, total = _gotoh(len(arr.measures), len(orch.measures), cost, pinned_rows, pinned_cols)
    if total == INF:
        raise ValueError("measure_map: the overrides leave no possible alignment")
    al = Alignment(arr, orch, mapping, S, list(anchors), ov, total)
    refine(al, orch_piano)
    if arr_feats is not None and orch_feats is not None:
        al.transposed = find_transposed(al, arr_feats, orch_feats)
        for t in al.transposed:
            if t.mapped:
                for i in range(t.arr[0], t.arr[1] + 1):
                    al.mapping[i] = None
    return al.finish()


def match_cost(S: List[List[float]], arr: Timeline, orch: Timeline, i: int, j: int) -> float:
    a, b = arr.measures[i], orch.measures[j]
    c = 1.0 - S[i][j]
    if a.timesig != b.timesig or a.length != b.length:
        c += TIMESIG_COST
    return c


MOVE_GAIN = 0.3           # refine: a measure moves into a cut when it sounds this much more like a measure there
MOVE_MIN = 0.7            # ... and at least this alike
JOIN_MIN = 0.7            # a dropped measure joins its neighbour's orchestral measure when at least this alike
TRIM_MAX = 0.45           # a measure at the edge of a cut joins it when less alike than this


def refine(al: Alignment, orch_piano: Optional[Sequence[int]] = None) -> None:
    """Step 2 of :func:`align`, in place."""
    S, ov = al.similarity, al.overrides
    pinned = {i for i, _j in ov.pins}
    no_col, no_row = set(ov.fill), set(ov.drop)
    n, m = len(al.mapping), len(al.orch.measures)

    def bounds(i: int) -> Tuple[int, int]:
        lo = next((al.mapping[k] for k in range(i - 1, -1, -1) if al.mapping[k] is not None), -1) + 1
        hi = next((al.mapping[k] for k in range(i + 1, n) if al.mapping[k] is not None), m) - 1
        return lo, hi

    def score(i: int, j: int) -> float:
        return S[i][j] - (TIMESIG_COST if (al.arr.measures[i].timesig != al.orch.measures[j].timesig
                                           or al.arr.measures[i].length != al.orch.measures[j].length) else 0.0)

    for i in range(n):
        j = al.mapping[i]
        if j is None or i in pinned:
            continue
        lo, hi = bounds(i)
        cands = [x for x in range(lo, hi + 1) if x not in no_col]
        if not cands:
            continue
        best = max(cands, key=lambda x: (score(i, x), -abs(x - j)))
        if best != j and S[i][best] >= MOVE_MIN and score(i, best) >= score(i, j) + MOVE_GAIN:
            al.mapping[i] = best
            al.moved.append((i, j, best))
    for i in range(n):
        if al.mapping[i] is not None or i in no_row:
            continue
        options = []
        for k in (i - 1, i + 1):
            if 0 <= k < n and al.mapping[k] is not None:
                j = al.mapping[k]
                partners = [x for x in range(n) if al.mapping[x] == j]
                if len(partners) != 1:
                    continue
                near = [S[i][x] for x in range(max(0, j - 2), min(m, j + 3))]
                if S[i][j] >= JOIN_MIN and S[i][j] >= max(near):
                    options.append((S[i][j], j))
        if options:
            j = max(options)[1]
            al.mapping[i] = j
            al.merged.append((i, j))
    if orch_piano is None:
        return
    changed = True
    while changed:
        changed = False
        used = {j for j in al.mapping if j is not None}
        for i, j in enumerate(al.mapping):
            if j is None or i in pinned or S[i][j] >= TRIM_MAX or not orch_piano[j]:
                continue
            shared = sum(1 for x in al.mapping if x == j) > 1
            edge = ((j - 1 >= 0 and (j - 1) not in used) or (j + 1 < m and (j + 1) not in used)
                    or (i - 1 >= 0 and al.mapping[i - 1] is None) or (i + 1 < n and al.mapping[i + 1] is None))
            if edge and not shared:
                al.mapping[i] = None
                al.trimmed.append((i, j))
                changed = True
                break


def _runs(indices: Sequence[int]) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for x in sorted(indices):
        if out and out[-1][1] == x - 1:
            out[-1] = (out[-1][0], x)
        else:
            out.append((x, x))
    return out


def find_transposed(al: Alignment, arr_feats: Sequence[Features], orch_feats: Sequence[Sequence[Features]],
                    min_similarity: float = 0.6) -> List[Transposed]:
    """Passages (two measures or more) the arrangement plays in another key than the orchestral score: mapped runs
    that sound clearly more alike transposed (by more than :data:`TRANSPOSE_COST`, the same transposition for
    consecutive measures), and cuts at the same place on both sides that align transposed."""
    out: List[Transposed] = []
    pinned = {i for i, _j in al.overrides.pins}

    def sim_k(i: int, j: int, k: int) -> float:
        return max(similarity(arr_feats[i], f[j], k) for f in orch_feats)

    ks: Dict[int, Tuple[int, float]] = {}
    for i, j in enumerate(al.mapping):
        if j is None or i in pinned:
            continue
        k, sk, s0 = best_transposition(arr_feats[i], [f[j] for f in orch_feats])
        if k and sk - s0 > TRANSPOSE_COST and sk >= min_similarity:
            ks[i] = (k, sk)
    n = len(al.mapping)
    i = 0
    while i < n:
        if i not in ks:
            i += 1
            continue
        k = ks[i][0]
        e = i
        while e + 1 < n and (e + 1) in ks and ks[e + 1][0] == k and al.mapping[e + 1] == al.mapping[e] + 1:
            e += 1
        if e > i:
            sims = [ks[x][1] for x in range(i, e + 1)]
            out.append(Transposed((i, e), (al.mapping[i], al.mapping[e]), -k, sum(sims) / len(sims), True))
        i = e + 1
    # cuts on both sides between the same mapped measures
    mapped = [(i, j) for i, j in enumerate(al.mapping) if j is not None]
    bounds = [(-1, -1)] + mapped + [(len(al.arr.measures), len(al.orch.measures))]
    for (i0, j0), (i1, j1) in zip(bounds, bounds[1:]):
        rows, cols = list(range(i0 + 1, i1)), list(range(j0 + 1, j1))
        if len(rows) < 2 or len(cols) < 2 or len(rows) * len(cols) > 40000:
            continue
        best = None
        for k in range(-5, 7):
            if not k:
                continue
            sub = [[sim_k(i, j, k) for j in cols] for i in rows]
            mp, _t = _gotoh(len(rows), len(cols), lambda a, b: 1.0 - sub[a][b])
            pairs = [(a, b) for a, b in enumerate(mp) if b is not None and sub[a][b] >= min_similarity]
            if len(pairs) >= 2 and (best is None or len(pairs) > len(best[1])):
                best = (k, pairs, sub)
        if best is None:
            continue
        k, pairs, sub = best
        s0 = [sim_k(rows[a], cols[b], 0) for a, b in pairs]
        sk = [sub[a][b] for a, b in pairs]
        if sum(sk) / len(sk) - sum(s0) / len(s0) <= TRANSPOSE_COST:
            continue
        out.append(Transposed((rows[pairs[0][0]], rows[pairs[-1][0]]), (cols[pairs[0][1]], cols[pairs[-1][1]]), -k,
                              sum(sk) / len(sk), False))
    return sorted(out, key=lambda t: t.arr)


def segments_of(al: Alignment) -> List[Segment]:
    """Runs of units that follow on from each other on both sides, with their confidence."""
    S = al.similarity
    pins = set(al.overrides.pins)
    runs: List[List[Unit]] = []
    for u in al.units:
        if runs and runs[-1][-1].arr[1] == u.arr[0] - 1 and runs[-1][-1].orch[1] == u.orch[0] - 1:
            runs[-1].append(u)
        else:
            runs.append([u])
    out = []
    n_orch = len(al.orch.measures)
    for run in runs:
        pairs = [(i, al.mapping[i]) for u in run for i in range(u.arr[0], u.arr[1] + 1)]
        sim = sum(S[i][j] for i, j in pairs) / len(pairs)
        shifted = []
        for d in SHIFTS:
            for sgn in (-1, 1):
                vals = [S[i][j + sgn * d] for i, j in pairs if 0 <= j + sgn * d < n_orch]
                if vals and len(vals) >= len(pairs) / 2:
                    shifted.append(sum(vals) / len(vals))
        margin = sim - max(shifted) if shifted else sim
        out.append(Segment((run[0].arr[0], run[-1].arr[1]), (run[0].orch[0], run[-1].orch[1]), sim, margin,
                           all(p in pins for p in pairs)))
    return out


# ------------------------------------------------------------------------------
# retiming the arrangement's notes onto the orchestral timeline
# ------------------------------------------------------------------------------

def unit_warp(al: Alignment, u: Unit, arr_notes: Sequence[SNote], orch_notes: Sequence[SNote]
              ) -> List[Tuple[int, int]]:
    """Time warp of a unit: (arrangement offset, orchestral offset) points from its start, both ends included,
    strictly increasing.  Units of the same length on both sides keep the arrangement's offsets; others are
    warped through the onsets the two sides share (the same pitch struck, in order: the longest such matching of
    onsets), linearly between them (and linearly from end to end without any)."""
    A, O = al.arr, al.orch
    a0, a1 = A.measures[u.arr[0]].utick, A.measures[u.arr[1]].end_utick
    o0, o1 = O.measures[u.orch[0]].utick, O.measures[u.orch[1]].end_utick
    la, lo = a1 - a0, o1 - o0
    if la == lo:
        return [(0, 0), (la, lo)]

    def onsets(notes: Sequence[SNote], s: int, e: int) -> List[Tuple[int, frozenset]]:
        d: Dict[int, set] = {}
        for n in notes:
            if s <= n.on < e:
                d.setdefault(n.on - s, set()).add(n.pitch)
        return [(t, frozenset(p)) for t, p in sorted(d.items())]

    xa = onsets(arr_notes, a0, a1)
    xo = onsets(orch_notes, o0, o1)
    # the in-order matching of onsets that share pitches with the most weight; an onset pair weighs the overlap of
    # its pitch sets (a single note of a run matches the same single note rather than a chord that contains it)
    na, no = len(xa), len(xo)
    W = [[0.0] * (no + 1) for _ in range(na + 1)]
    for p in range(na - 1, -1, -1):
        for q in range(no - 1, -1, -1):
            best = max(W[p + 1][q], W[p][q + 1])
            common = xa[p][1] & xo[q][1]
            if common:
                best = max(best, W[p + 1][q + 1] + len(common) / len(xa[p][1] | xo[q][1]))
            W[p][q] = best
    pts: List[Tuple[int, int]] = []
    p = q = 0
    while p < na and q < no:
        common = xa[p][1] & xo[q][1]
        if common and abs(W[p][q] - (W[p + 1][q + 1] + len(common) / len(xa[p][1] | xo[q][1]))) < 1e-9:
            x, y = xa[p][0], xo[q][0]
            if not pts or (x > pts[-1][0] and y > pts[-1][1]):
                pts.append((x, y))
            p, q = p + 1, q + 1
        elif W[p + 1][q] >= W[p][q + 1]:
            p += 1
        else:
            q += 1
    # from the measure's start (a first match on its downbeat may land later in the orchestral measure), to its end
    if not pts or pts[0][0] > 0:
        pts.insert(0, (0, 0))
    return pts + [(la, lo)]


def _warp(pts: Sequence[Tuple[int, int]], x: int) -> int:
    """Piecewise-linear through ``pts`` (clamped to its ends)."""
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + int(round((x - x0) * (y1 - y0) / (x1 - x0))) if x1 > x0 else y1
    return pts[-1][1]


@dataclass
class RetimeStats:
    mapped_notes: int = 0
    dropped_notes: int = 0          # struck in a dropped measure (and not held into a mapped one)
    clipped_notes: int = 0          # held into a cut: ended at the cut
    restruck_notes: int = 0         # held from a dropped measure into a mapped one: struck again there
    fill_notes: int = 0
    fill_clipped: int = 0           # filled notes held into a mapped measure: ended there
    warped_units: int = 0           # units of another length on the two sides, warped
    silent_mapped: List[int] = field(default_factory=list)   # mapped orchestral measures where the arrangement
                                                             # sounds nothing while the orchestral piano plays
    to_orchestra: int = 0           # reduction "orchestra": the arrangement's notes that are the orchestra now ...
    to_orchestra_measures: List[int] = field(default_factory=list)   # ... in these orchestral measures as played
    replaced_orchestra: int = 0     # ... and the orchestra's own notes they stand for there

    def to_dict(self) -> dict:
        d = {"mapped_notes": self.mapped_notes, "dropped_notes": self.dropped_notes,
             "clipped_notes": self.clipped_notes, "restruck_notes": self.restruck_notes,
             "fill_notes": self.fill_notes, "fill_clipped": self.fill_clipped, "warped_units": self.warped_units}
        if self.to_orchestra:
            d.update(to_orchestra=self.to_orchestra, replaced_orchestra=self.replaced_orchestra)
        return d


class Retimer:
    """Arrangement MIDI ticks -> orchestral MIDI ticks through an alignment's units (:meth:`note`); with ``reverse``
    the other way round, orchestral ticks -> arrangement ticks (the orchestra of an orchestral score moved onto a
    piano solo's measures: :mod:`pianovision.timeline`): the same units and warps, read backwards."""

    def __init__(self, al: Alignment, arr_notes: Sequence[SNote] = (), orch_notes: Sequence[SNote] = (),
                 reverse: bool = False):
        self.al = al
        self.reverse = reverse
        self.src, self.dst = (al.orch, al.arr) if reverse else (al.arr, al.orch)
        warps = [unit_warp(al, u, arr_notes, orch_notes) for u in al.units]
        # per unit: (source measures, destination measures) and the warp from source to destination offsets
        self.units: List[Tuple[Tuple[int, int], Tuple[int, int]]] = [(u.orch, u.arr) if reverse else (u.arr, u.orch)
                                                                     for u in al.units]
        self.warps = [[(b, a) for a, b in w] for w in warps] if reverse else warps
        self.unit_of: Dict[int, int] = {}
        for k, (s, _d) in enumerate(self.units):
            for i in range(s[0], s[1] + 1):
                self.unit_of.setdefault(i, k)

    def _follows(self, k: int) -> bool:
        """Unit k+1 follows on from unit k on both sides."""
        us = self.units
        return (k + 1 < len(us) and us[k + 1][0][0] == us[k][0][1] + 1 and us[k + 1][1][0] == us[k][1][1] + 1)

    def _place(self, k: int, u_src: int) -> int:
        """Unrolled source tick inside unit k -> unrolled destination tick."""
        S, D = self.src, self.dst
        s, d = self.units[k]
        x = u_src - S.measures[s[0]].utick
        return D.measures[d[0]].utick + _warp(self.warps[k], x)

    def note(self, on_tick: int, off_tick: int) -> Optional[Tuple[int, int, str]]:
        """(on, off) of a source note (its render's MIDI ticks) on the destination render's MIDI clock, and how
        (``mapped`` | ``clipped`` | ``restruck``); None when it is not played (struck in a measure without
        counterpart)."""
        S, D = self.src, self.dst
        u_on = S.utick(on_tick)
        u_off = max(S.utick(off_tick), u_on)
        i = S.measure_at(on_tick)
        how = "mapped"
        if i not in self.unit_of:
            k = i + 1
            while k < len(S.measures) and S.measures[k].utick < u_off and k not in self.unit_of:
                k += 1
            if k >= len(S.measures) or k not in self.unit_of or u_off - S.measures[k].utick < RESTRIKE_MIN:
                return None
            i, u_on, how = k, S.measures[k].utick, "restruck"
        k = self.unit_of[i]
        on_u = self._place(k, u_on)
        # the end: through the units while they follow on
        end_unit = k
        while S.measures[self.units[end_unit][0][1]].end_utick < u_off and self._follows(end_unit):
            end_unit += 1
        s, d = self.units[end_unit]
        if u_off <= S.measures[s[1]].end_utick:
            off_u = self._place(end_unit, u_off)
        else:
            off_u = D.measures[d[1]].end_utick
            how = "clipped" if how == "mapped" else how
        on_u = min(on_u, D.measures[self.units[k][1][1]].end_utick - 1)
        return D.tick(on_u), D.tick(max(off_u, on_u)), how


def retime_arrangement(arr_notes: Dict[str, List[dict]], al: Alignment, seconds: Callable[[int], float], tpb: int,
                       arr_snotes: Sequence[SNote] = (), orch_snotes: Sequence[SNote] = ()
                       ) -> Tuple[Dict[str, List[Tuple[float, int, dict]]], RetimeStats, List[int]]:
    """The arrangement's notes (``arr_notes``: its parts document's original part, NOTE dicts on its own clock) moved
    onto the orchestral clock (:class:`Retimer`; ``arr_snotes`` / ``orch_snotes``: the sounding notes the warps of
    units of unequal length are anchored on), marked ``"simplified": 1``.  Returns ({hand: [(start, seq, NOTE)]} in the
    arrangement's order, the stats, per orchestral measure the number of arrangement notes sounding in it)."""
    st = RetimeStats()
    O = al.orch
    rt = Retimer(al, arr_snotes, orch_snotes)
    st.warped_units = sum(1 for w in rt.warps if len(w) > 2 or w[-1][0] != w[-1][1])
    out: Dict[str, List[Tuple[float, int, dict]]] = {"right": [], "left": []}
    seq = 0
    arr_sounds = [0] * len(O.measures)
    for hand in ("right", "left"):
        for n in arr_notes.get(hand, []):
            on, off = int(n["ticksStart"]), int(n["ticksStart"]) + int(n["durationTicks"])
            r = rt.note(on, off)
            if r is None:
                st.dropped_notes += 1
                continue
            t_on, t_off, how = r
            st.mapped_notes += 1
            st.clipped_notes += how == "clipped"
            st.restruck_notes += how == "restruck"
            d = _note_dict(n, t_on, t_off, seconds, tpb, simplified=True)
            out[hand].append((d["start"], seq, d))
            seq += 1
            for j in range(O.measure_at(t_on), O.measure_at(max(t_off - 1, t_on)) + 1):
                arr_sounds[j] += 1
    return out, st, arr_sounds


def fill_end(O: Timeline, fills: set, j: int, off: int) -> Optional[int]:
    """A note of the orchestral piano part struck in the filled measure ``j`` (``fills``: the filled measures) and
    released at MIDI tick ``off``: where it must end instead, the start of the next mapped measure when it is held
    into one (the arrangement takes over there); None when it ends in the cut."""
    k = j
    while k + 1 < len(O.measures) and O.measures[k].end_tick < off and (k + 1) in fills:
        k += 1
    end = O.measures[k].end_tick
    return end if off > end and k + 1 < len(O.measures) else None


def _silent_mapped(al: Alignment, arr_sounds: Sequence[int], orch_piano_sounding: Sequence[int]) -> List[int]:
    mapped = {j for j in al.mapping if j is not None}
    return [j for j in sorted(mapped) if not arr_sounds[j] and orch_piano_sounding[j]]


def retime(arr_notes: Dict[str, List[dict]], orch_original: Dict[str, List[dict]], al: Alignment,
           seconds: Callable[[int], float], tpb: int, orch_piano_sounding: Optional[Sequence[int]] = None,
           arr_snotes: Sequence[SNote] = (), orch_snotes: Sequence[SNote] = ()
           ) -> Tuple[Dict[str, List[dict]], RetimeStats]:
    """The simplified part: the arrangement's notes (``arr_notes``: its parts document's original part, NOTE dicts on
    its own clock) moved onto the orchestral clock (:func:`retime_arrangement`), and the orchestral piano part's notes
    (``orch_original``) in the orchestral measures without counterpart.  ``seconds``: orchestral MIDI tick -> s (its
    tempo map, pauses included); ``orch_piano_sounding``: per orchestral measure, the ticks the orchestral piano part
    sounds (to report mapped measures where the arrangement is silent)."""
    O = al.orch
    out, st, arr_sounds = retime_arrangement(arr_notes, al, seconds, tpb, arr_snotes, orch_snotes)
    seq = sum(len(v) for v in out.values())
    fills = set(al.fills)
    for hand in ("right", "left"):
        for n in orch_original.get(hand, []):
            on = int(n["ticksStart"])
            j = O.measure_at(on)
            if j not in fills:
                continue
            end = fill_end(O, fills, j, on + int(n["durationTicks"]))   # end where the arrangement takes over again
            if end is not None:
                d = _note_dict(n, on, end, seconds, tpb, simplified=False)
                st.fill_clipped += 1
            else:
                d = dict(n)
            st.fill_notes += 1
            out[hand].append((d["start"], seq, d))
            seq += 1
    if orch_piano_sounding is not None:
        st.silent_mapped = _silent_mapped(al, arr_sounds, orch_piano_sounding)
    return {h: [d for _t, _s, d in sorted(v, key=lambda x: (x[0], x[1]))] for h, v in out.items()}, st


class PianoFrom:
    """``piano_from: "simplified_from"`` (:mod:`pianovision.buildopts`): the arrangement is the song's piano part.

    Called with the song's :class:`pianovision.pvjson.NoteStreams` before anything is built from them
    (:func:`pianovision.pvjson.build_song`), it replaces the piano part's notes (the primary and simplified staves)
    in the orchestral measures the alignment maps with the arrangement's notes on the orchestral clock, exactly the
    notes the simplified part has (:func:`retime_arrangement`): PianoVision's hands, Note Waterfall's original part
    and everything made from them (accents of their own, the orchestra merged where a hand rests) follow.  The
    measures without counterpart keep the orchestral score's piano part (drum staves left out, a note held into a
    mapped measure ends there), as the simplified part fills them.  An arrangement note is a
    :class:`pianovision.pvjson.Note` with ``arranged`` set (its parts-file extras: ``"simplified": 1``, a lyric) and no
    score note (``src``): its accent and velocity are the arrangement's, its hand-edit identity the simplified
    part's."""

    def __init__(self, pr, score, reduction: bool = False):
        al, orch = pr.al, pr.orch
        self.al = al
        self.seconds = orch.seconds
        self.drums = drum_staves(score)
        # reduction "orchestra": where the orchestral piano part rests and its orchestra plays, the arrangement's notes
        # are the orchestra arranged for piano (a tutti the solo plays), not the piano part
        self.reduction = Reduction(pr, score) if reduction else None
        out, self.stats, self.arr_sounds = retime_arrangement(pr.solo_notes, al, orch.seconds, orch.tpb,
                                                              pr.solo.notes, orch.notes)
        self.arranged = {h: [d for _t, _s, d in sorted(v, key=lambda x: (x[0], x[1]))] for h, v in out.items()}
        self.stats.silent_mapped = _silent_mapped(al, self.arr_sounds, orch.piano_sounding)

    def __call__(self, streams) -> None:
        from .pvjson import Note
        O = self.al.orch
        fills = set(self.al.fills)
        st = self.stats
        st.fill_notes = st.fill_clipped = 0
        # hand -> the staff of its first piano note (the arrangement notes' MIDI track, informational)
        tracks = {h: streams.primary[h][0].track for h in (0, 1) if streams.primary[h]}
        for which in ("primary", "simplified"):
            hands = getattr(streams, which)
            for h in (0, 1):
                kept = []
                for n in hands[h]:
                    j = O.measure_at(n.ticks)
                    if j not in fills or n.track in self.drums:
                        continue
                    end = fill_end(O, fills, j, n.ticks + n.duration_ticks)
                    if end is not None:
                        n.duration_ticks = end - n.ticks
                        n.duration = max(self.seconds(end) - n.time, 0)
                        st.fill_clipped += which == "primary"
                    st.fill_notes += which == "primary"
                    kept.append(n)
                hands[h] = kept
        reduced: List[List[Note]] = [[], []]
        for h, hand in enumerate(("right", "left")):
            arranged = []
            for d in self.arranged[hand]:
                t = int(d["ticksStart"])
                extra = {k: v for k, v in d.items() if k not in _NOTE_KEYS}
                arranged.append(Note(midi=int(d["note"]), time=d["start"], velocity=d["velocity"],
                                     duration=d["duration"], ticks=t, duration_ticks=int(d["durationTicks"]),
                                     staff=h + 1, group=streams.get_measure_for_tick(t) - 1,
                                     accent=int(d.get("accent", 0)),
                                     raw_velocity=int(round(d["velocity"] * 127)), track=tracks.get(h, -1),
                                     arranged=extra))
                if self.reduction is not None and self.reduction(t):
                    arranged[-1].arranged = None            # an orchestra note now (its parts-file fields: staff)
                    reduced[h].append(arranged.pop())
            # in time order, the orchestral notes first (they never share a measure with the arrangement's)
            streams.primary[h] = sorted(streams.primary[h] + arranged, key=lambda n: n.time)
        if self.reduction is not None:
            # the reduction stands for the orchestra's instruments where the piano rests in its measures
            meas = {O.measure_at(n.ticks) for hand in reduced for n in hand}
            st.to_orchestra = sum(map(len, reduced))
            st.to_orchestra_measures = sorted(meas)
            st.replaced_orchestra = 0
            for h in (0, 1):
                kept = []
                for n in streams.other_orchestra[h]:
                    if O.measure_at(n.ticks) in meas and self.reduction(n.ticks):
                        st.replaced_orchestra += 1
                        continue
                    kept.append(n)
                streams.other_orchestra[h] = sorted(kept + reduced[h], key=lambda n: n.time)


class Reduction:
    """Where a song's orchestral score has its orchestra playing and its piano part resting (:class:`PianoFrom`'s
    ``reduction``): a MIDI tick of the song (the orchestral score on the song's clock) is in such a place when no note
    of the piano part sounds in the eighth from it on (a note ending there does not) and some orchestra note does."""

    def __init__(self, pr, score):
        midi = pr.orch_render.midi
        self.tl = pr.al.orch
        self.win = pr.orch.tpb // 2
        piano, pitched = staff_tracks(score, midi)
        others = [t for t in pitched if t not in piano]
        self.piano = _merged_spans((n.on, n.off) for n in sounding_notes(midi, piano, self.tl))
        self.orchestra = _merged_spans((n.on, n.off) for n in sounding_notes(midi, others, self.tl))

    def __call__(self, tick: int) -> bool:
        u = self.tl.utick(tick)
        return not _overlaps(self.piano, u, u + self.win) and _overlaps(self.orchestra, u, u + self.win)


def _merged_spans(spans) -> List[Tuple[int, int]]:
    out: List[List[int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _overlaps(spans: Sequence[Tuple[int, int]], a: int, b: int) -> bool:
    """Some span of ``spans`` (merged, in order) meets ``[a, b)``."""
    k = bisect.bisect_left(spans, (b, b)) - 1
    return k >= 0 and spans[k][1] > a


# the fields of a parts-file NOTE that the note itself gives (pianovision.parts.build_parts); the rest are extras
_NOTE_KEYS = ("note", "ticksStart", "durationTicks", "start", "duration", "velocity", "measureInd", "accent")


def _note_dict(n: dict, on: int, off: int, seconds: Callable[[int], float], tpb: int, simplified: bool) -> dict:
    """A parts-file NOTE (keys in :func:`pianovision.parts.build_parts`' order) at MIDI ticks ``on``..``off``."""
    start = seconds(on)
    d = {"note": n["note"], "ticksStart": on, "durationTicks": max(off - on, 0), "start": start,
         "duration": max(seconds(off) - start, 0), "velocity": n["velocity"], "measureInd": int((on / tpb) / 4),
         "accent": n.get("accent", 0)}
    if simplified:
        d["simplified"] = 1
    for k, v in n.items():                   # lyric, sung, ...
        if k not in d and k != "simplified":
            d[k] = v
    return d


# ------------------------------------------------------------------------------
# from scores to the parts file
# ------------------------------------------------------------------------------

ARRANGEMENT_REVISION = 2
"""Bumped when the alignment or the retiming changes what an arrangement gives (the library rewrites those parts).
2: the song plays on the arrangement's clock (:mod:`pianovision.timeline`)."""


def drum_staves(score) -> set:
    """Staves of drumsets (unpitched percussion): their MIDI notes are drum sounds, not pitches."""
    return {st.idx for st in score.staves if st.group == "percussion" or st.part.instrument().use_drumset}


def staff_tracks(score, midi) -> Tuple[List[int], List[int]]:
    """(the piano part's tracks as PianoVision classifies them, every pitched track): MIDI tracks = staves; drum
    staves are in neither."""
    from .pvjson import TRACK_PRIMARY, TRACK_SIMPLIFIED, classify_tracks
    drums = drum_staves(score)
    piano, _orch = classify_tracks(midi, True)
    piano_tracks = [t for t, kind in piano if kind in (TRACK_PRIMARY, TRACK_SIMPLIFIED) and t not in drums]
    pitched = [i for i in range(len(midi.tracks)) if i < len(score.staves) and i not in drums]
    return piano_tracks, pitched


@dataclass
class Side:
    """One score of the pair, rendered: its timeline, features, sounding notes and clock."""
    timeline: Timeline
    feats: List[List[Features]]      # feature sets: [piano] for the arrangement, [piano, piano + orchestra] else
    notes: List[SNote]               # sounding notes the warps anchor on: the piano (arrangement), everything pitched
    piano_sounding: List[int]        # per measure: ticks the piano part sounds
    seconds: Callable[[int], float]
    tpb: int


def side_of(score, renderer, midi, arrangement: bool, features: bool = True) -> Side:
    """``features=False``: the timeline, notes and clock only (no feature sets; the piano's sounding ticks still)."""
    from .pvjson import TempoIndex, extract_tempo_events
    tl = Timeline.from_render(renderer, midi)
    piano, pitched = staff_tracks(score, midi)
    pn = sounding_notes(midi, piano, tl)
    if arrangement and not pn:                      # no piano staff found: everything pitched
        pn = sounding_notes(midi, pitched, tl)
    pf = measure_features(tl, pn)
    if arrangement:
        feats, notes = [pf], pn
    else:
        notes = sounding_notes(midi, pitched, tl)
        feats = [pf, measure_features(tl, notes)] if features else [pf]
    return Side(tl, feats, notes, [f.sounding for f in pf],
                TempoIndex(extract_tempo_events(midi), midi.ticks_per_beat), midi.ticks_per_beat)


def render_arrangement(path: str, compat=None, options: Optional[dict] = None
                       ) -> Tuple[Side, Dict[str, List[dict]]]:
    """The arrangement rendered as the library renders it (``compat``: its knobs, ``options``: its build options'
    piano staves and hands): its side and its parts document's piano part (the notes of the song the arrangement is
    on its own, original part)."""
    from .convert import score_root
    from .handsplit import apply_piano_options
    from .mscx import read_score
    from .pvjson import build_song
    from .render import Compat, Renderer
    c = compat if isinstance(compat, Compat) else Compat.from_dict(compat)
    score = read_score(path)
    r = Renderer(score, c, provenance=True)
    midi = r.render()
    apply_piano_options(midi, options)
    parts: Dict[str, Any] = {}
    build_song(midi, score_root(path), path, legacy_metadata=c.metadata, parts=parts)
    return side_of(score, r, midi, True), parts["doc"]["parts"]["original"]


def arrange(orch: Side, arr: Side, measure_map: Any = "auto") -> Alignment:
    """Align the arrangement's measures with the orchestral score's (:func:`align`)."""
    ov = read_overrides(measure_map, arr.timeline, orch.timeline)
    S = similarity_matrix(arr.feats[0], orch.feats)
    anchors = text_anchors(arr.timeline, orch.timeline)
    return align(S, arr.timeline, orch.timeline, anchors, ov, arr.feats[0], orch.feats, orch.piano_sounding)


def apply_arrangement(spec: dict, pr, score, parts_out: dict, piano: Optional[PianoFrom] = None) -> dict:
    """Make the arrangement the simplified part of the orchestral score's parts (``parts_out``:
    :func:`pianovision.pvjson.build_song`'s ``parts`` of the orchestral score rendered on the arrangement's clock,
    its ``doc`` changed in place).  ``pr``: the pair (:func:`pianovision.timeline.pair`: the alignment, the
    arrangement's notes, the orchestral side on that clock); ``spec``: the build options' partner (``rel``,
    ``measure_map``); ``score``: the orchestral score's model.  ``piano``: the arrangement is already the song's
    piano part (``piano_from``, :class:`PianoFrom` gave it to :func:`pianovision.pvjson.build_song`): the parts stay
    as built, the original part being the arrangement with the fills, and there is no separate simplified part (it
    would be the same notes).  Returns the report (:func:`report_dict`)."""
    from .timeline import plan_report
    al, orch = pr.al, pr.orch
    doc = parts_out["doc"]
    if piano is not None:
        stats = piano.stats
    else:
        # the cuts are filled with the original part's notes, drum sounds left out (a drum staff counted as piano)
        original = doc["parts"]["original"]
        objs = (parts_out.get("notes") or {}).get("original")
        drums = drum_staves(score)
        if objs and drums:
            original = {hand: [d for d, n in zip(original[hand], objs[h]) if getattr(n, "track", -1) not in drums]
                        for h, hand in enumerate(("right", "left"))}
        simplified, stats = retime(pr.solo_notes, original, al, orch.seconds, orch.tpb, orch.piano_sounding,
                                   pr.solo.notes, orch.notes)
        doc["simplified"] = True
    rep = report_dict(al, stats, spec, orch, kind="simplified")
    if piano is not None:
        rep["piano_from"] = True
    rep["timeline"] = plan_report(pr.plan, pr)
    rest = {k: v for k, v in doc.items() if k != "parts"}
    old = doc["parts"]
    doc.clear()
    doc.update(rest)
    doc["arrangement"] = {"score": spec.get("rel") or os.path.basename(spec["path"]), "revision": ARRANGEMENT_REVISION,
                          "measures": {k: rep["measures"][k] for k in ("mapped", "filled", "dropped")}}
    if piano is not None:
        # the arrangement is the original part (and PianoVision's hands); its notes carry "simplified": 1
        doc["arrangement"]["part"] = "original"
        doc["parts"] = old
        return rep
    doc["parts"] = {"original": old["original"], "simplified": simplified,
                    **{k: v for k, v in old.items() if k not in ("original", "simplified")}}
    # the simplified part's notes are no notes of this score's simplified staves (hand edits: pianovision.handedits)
    notes = parts_out.get("notes")
    if isinstance(notes, dict):
        notes.pop("simplified", None)
    parts_out["simplified_staff"] = set()
    return rep


def orchestra_report(spec: dict, pr, stats: RetimeStats, parts_out: Optional[dict] = None) -> dict:
    """The report of an orchestra from another score (``orchestra_from``): the alignment (the solo as the
    arrangement), what became of the orchestral notes, the plan; records the partner in the parts document."""
    from .timeline import plan_report
    rep = report_dict(pr.al, stats, spec, pr.solo, kind="orchestra", orch=pr.orch)
    rep["timeline"] = plan_report(pr.plan, pr)
    if parts_out is not None:
        doc = parts_out["doc"]
        rest = {k: v for k, v in doc.items() if k != "parts"}
        old = doc["parts"]
        doc.clear()
        doc.update(rest)
        doc["orchestraFrom"] = {"score": spec.get("rel") or os.path.basename(spec["path"]),
                                "revision": ARRANGEMENT_REVISION,
                                "measures": {k: rep["measures"][k] for k in ("mapped", "filled", "dropped")}}
        doc["parts"] = old
    return rep


# ------------------------------------------------------------------------------
# the report
# ------------------------------------------------------------------------------

def _range(tl: Timeline, a: int, b: int) -> str:
    return tl.label(a) if a == b else f"{tl.label(a)}-{tl.label(b)}"


def _ranges(tl: Timeline, indices: Sequence[int]) -> List[str]:
    return [_range(tl, a, b) for a, b in _runs(indices)]


def report_dict(al: Alignment, stats: RetimeStats, spec: dict, clock: Side, kind: str = "simplified",
                orch: Optional[Side] = None) -> dict:
    """What the alignment and the retiming did, JSON-ready: measures in score numbers (``n:pass`` for a measure the
    playback reaches more than once), times in seconds on the song's clock (``clock``: the side whose measures the
    song has: the orchestral score's for ``kind`` "simplified" (``simplified_from``), the solo's for "orchestra"
    (``orchestra_from``; ``orch``: the orchestral side, for where its piano plays)."""
    A, O = al.arr, al.orch
    song_tl = O if kind == "simplified" else A
    orch = orch or clock

    def t0(j: int) -> float:
        return round(float(clock.seconds(song_tl.measures[j].tick)), 2)

    def t1(j: int) -> float:
        return round(float(clock.seconds(song_tl.measures[j].end_tick)), 2)

    def seg_time(s) -> List[float]:
        r = s.orch if kind == "simplified" else s.arr
        return [t0(r[0]), t1(r[1])]

    segs = [{"arrangement": _range(A, *s.arr), "orchestra": _range(O, *s.orch), "measures": s.length,
             "time": seg_time(s), "similarity": round(s.similarity, 3),
             "margin": round(s.margin, 3), "confidence": s.confidence} for s in al.segments]
    weak = [{"arrangement": A.label(i), "orchestra": O.label(j), "similarity": round(al.similarity[i][j], 3)}
            for i, j in enumerate(al.mapping) if j is not None and al.similarity[i][j] < 0.45]
    mism = []
    for u in al.units:
        la = A.measures[u.arr[1]].end_utick - A.measures[u.arr[0]].utick
        lo = O.measures[u.orch[1]].end_utick - O.measures[u.orch[0]].utick
        ta, tb = A.measures[u.arr[0]].timesig, O.measures[u.orch[0]].timesig
        if la != lo or ta != tb:
            mism.append({"arrangement": _range(A, *u.arr), "orchestra": _range(O, *u.orch),
                         "arrangement_timesig": f"{ta[0]}/{ta[1]}", "orchestra_timesig": f"{tb[0]}/{tb[1]}",
                         "arrangement_quarters": la / 480, "orchestra_quarters": lo / 480})
    o2a = al.orch_to_arr()
    by_text: Dict[Tuple[int, str], List[Anchor]] = {}
    for x in al.anchors:
        by_text.setdefault((x.arr, x.arr_text), []).append(x)
    anchors = []
    for (i, text), xs in sorted(by_text.items()):
        kept = next((x for x in xs if i in o2a.get(x.orch, [])), None)
        j = al.mapping[i]
        anchors.append({"arrangement": A.label(i), "text": text,
                        "kept": {"orchestra": O.label(kept.orch), "text": kept.orch_text} if kept else None,
                        "candidates": [{"orchestra": O.label(x.orch), "text": x.orch_text} for x in xs if x is not kept],
                        "mapped_to": O.label(j) if j is not None else None})
    transposed = [{"arrangement": _range(A, *x.arr), "orchestra": _range(O, *x.orch), "semitones": x.semitones,
                   "similarity": round(x.similarity, 3), "was_mapped": x.mapped} for x in al.transposed]
    fills = [{"orchestra": _range(O, a, b), "measures": b - a + 1,
              "time": [t0(a), t1(b)] if kind == "simplified" else None,
              "piano_plays": sum(1 for j in range(a, b + 1) if orch.piano_sounding[j])} for a, b in _runs(al.fills)]
    drops = [{"arrangement": _range(A, a, b), "measures": b - a + 1,
              **({"time": [t0(a), t1(b)]} if kind == "orchestra" else {})} for a, b in _runs(al.drops)]
    moved = [{"arrangement": A.label(i), "from": O.label(j0), "to": O.label(j1),
              "similarity": [round(al.similarity[i][j0], 3), round(al.similarity[i][j1], 3)]}
             for i, j0, j1 in al.moved]
    merged = [{"arrangement": A.label(i), "orchestra": O.label(j)} for i, j in al.merged]
    trimmed = [{"arrangement": A.label(i), "orchestra": O.label(j), "similarity": round(al.similarity[i][j], 3)}
               for i, j in al.trimmed]
    return {"score": spec.get("rel") or spec.get("path"), "kind": kind, "revision": ARRANGEMENT_REVISION,
            "measure_map": spec.get("measure_map", "auto"),
            "measures": {"arrangement": len(A.measures), "orchestra": len(O.measures),
                         "mapped": sum(1 for j in al.mapping if j is not None), "filled": len(al.fills),
                         "dropped": len(al.drops)},
            "segments": segs, "fills": fills, "drops": drops, "transposed": transposed, "moved": moved,
            "merged": merged, "trimmed": trimmed, "weak": weak, "timesig_mismatches": mism, "anchors": anchors,
            "silent_mapped": _ranges(O, stats.silent_mapped), "notes": stats.to_dict(), "cost": round(al.cost, 3),
            **({"to_orchestra": _ranges(O, stats.to_orchestra_measures)} if stats.to_orchestra else {})}


def report_markdown(rep: dict, song: str = "") -> str:
    orchestra_from = rep.get("kind") == "orchestra"
    L = [f"# {'Orchestra' if orchestra_from else 'Arrangement'}: {song or rep.get('score')}", ""]
    m = rep["measures"]
    if orchestra_from:
        L.append(f"Orchestra from *{rep.get('score')}*, on this piano solo's clock (measure_map: "
                 f"{json_text(rep.get('measure_map'))}). Measures as played (repeats unrolled; `n:2` = the second "
                 f"pass through measure n): solo (the arrangement) {m['arrangement']}, orchestral score "
                 f"{m['orchestra']}; {m['mapped']} solo measures with the orchestra, {m['filled']} orchestral measures "
                 f"left out (the solo does not have them), {m['dropped']} solo measures without orchestra.")
    else:
        what = ("The piano part (Note Waterfall's original part and PianoVision's hands; piano_from) and its clock"
                if rep.get("piano_from") else "Simplified part")
        L.append(f"{what} from *{rep.get('score')}* (measure_map: {json_text(rep.get('measure_map'))}); the "
                 f"song plays on its clock. Measures as played (repeats unrolled; `n:2` = the second pass through "
                 f"measure n): arrangement {m['arrangement']}, orchestral score {m['orchestra']}; {m['mapped']} "
                 f"arrangement measures mapped, {m['filled']} orchestral measures filled from the orchestral piano "
                 f"part, {m['dropped']} arrangement measures dropped.")
    L.append("")
    L.append("## Mapped segments")
    L.append("")
    L.append("| arrangement | orchestral score | measures | time (s) | similarity | margin | confidence |")
    L.append("|---|---|---|---|---|---|---|")
    for s in rep["segments"]:
        L.append(f"| {s['arrangement']} | {s['orchestra']} | {s['measures']} | {s['time'][0]:.1f}-{s['time'][1]:.1f} | "
                 f"{s['similarity']:.2f} | {s['margin']:+.2f} | {s['confidence']} |")
    L.append("")
    L.append("Similarity: 0..1, the pitch classes sounding, in the bass and on top, of the arrangement against the "
             "orchestral piano part or the piano with the orchestra, whichever is closer. Margin: the segment's mean "
             "similarity minus its best mean shifted by 1-4 measures (how distinctive the fit is). Times on the "
             "song's clock.")
    if rep.get("weak"):
        L.append("")
        L.append("Mapped measures that sound little alike (< 0.45; rewritten by the arranger, or worth a look): "
                 + ", ".join(f"{w['arrangement']}→{w['orchestra']} ({w['similarity']:.2f})" for w in rep["weak"]))
    if rep.get("moved") or rep.get("merged") or rep.get("trimmed"):
        L.append("")
        for x in rep.get("moved", []):
            L.append(f"- arrangement {x['arrangement']} moved from orchestral {x['from']} to {x['to']} (similarity "
                     f"{x['similarity'][0]:.2f} → {x['similarity'][1]:.2f})")
        for x in rep.get("merged", []):
            L.append(f"- arrangement {x['arrangement']} joins orchestral {x['orchestra']} with its neighbour (two "
                     f"arrangement measures over one)")
        for x in rep.get("trimmed", []):
            L.append(f"- arrangement {x['arrangement']} (at the edge of a cut, similarity {x['similarity']:.2f} with "
                     f"orchestral {x['orchestra']}) joins the cut: dropped, the orchestral measure filled")
    L.append("")
    L.append("## Cuts")
    L.append("")
    if rep["fills"]:
        if orchestra_from:
            L.append("Orchestral measures without counterpart in the solo, left out (their notes are not played, the "
                     "companion audio is cut there):")
            L.append("")
            for f in rep["fills"]:
                L.append(f"- {f['orchestra']} ({f['measures']} measure{'s' if f['measures'] != 1 else ''})")
        else:
            L.append("Orchestral measures without counterpart in the arrangement, filled from the orchestral piano "
                     "part:")
            L.append("")
            for f in rep["fills"]:
                L.append(f"- {f['orchestra']} ({f['measures']} measure{'s' if f['measures'] != 1 else ''}, "
                         f"{f['time'][0]:.1f}-{f['time'][1]:.1f} s; the piano plays in {f['piano_plays']})")
    else:
        L.append("Every orchestral measure has a counterpart.")
    L.append("")
    if rep["drops"]:
        if orchestra_from:
            L.append("Solo measures without counterpart in the orchestral score (no orchestra there):")
            L.append("")
            for d in rep["drops"]:
                L.append(f"- {d['arrangement']} ({d['measures']} measure{'s' if d['measures'] != 1 else ''}"
                         + (f", {d['time'][0]:.1f}-{d['time'][1]:.1f} s" if d.get("time") else "") + ")")
        else:
            L.append("Arrangement measures without counterpart in the orchestral score (dropped):")
            L.append("")
            for d in rep["drops"]:
                L.append(f"- {d['arrangement']} ({d['measures']} measure{'s' if d['measures'] != 1 else ''})")
    else:
        L.append("No arrangement measure dropped." if not orchestra_from else "Every solo measure has orchestra.")
    L.append("")
    if rep["transposed"]:
        L.append("## Transposed passages (filled from the orchestral piano part)")
        L.append("")
        for x in rep["transposed"]:
            L.append(f"- arrangement {x['arrangement']} ≈ orchestral {x['orchestra']}, {x['semitones']:+d} semitones "
                     f"(similarity {x['similarity']:.2f} transposed back)")
        L.append("")
    L.append("## Time signatures and lengths")
    L.append("")
    if rep["timesig_mismatches"]:
        L.append("Mapped measures whose time signature or length differ (the notes are warped from one measure onto "
                 "the other through the onsets both strike at the same pitch):")
        L.append("")
        for x in rep["timesig_mismatches"]:
            L.append(f"- arrangement {x['arrangement']} ({x['arrangement_timesig']}, {x['arrangement_quarters']:g} "
                     f"quarters) → orchestral {x['orchestra']} ({x['orchestra_timesig']}, "
                     f"{x['orchestra_quarters']:g} quarters)")
    else:
        L.append("Every mapped measure has the same time signature and length on both sides.")
    L.append("")
    if rep["anchors"]:
        L.append("## Text anchors")
        L.append("")
        L.append("Distinctive texts both scores have (each at most twice), and where the alignment put them:")
        L.append("")
        for x in rep["anchors"]:
            if x["kept"]:
                L.append(f"- arrangement {x['arrangement']} \"{x['text']}\" = orchestral {x['kept']['orchestra']} "
                         f"\"{x['kept']['text']}\"")
            else:
                cands = ", ".join(f"{c['orchestra']} \"{c['text']}\"" for c in x["candidates"])
                L.append(f"- arrangement {x['arrangement']} \"{x['text']}\": not at orchestral {cands} (mapped to "
                         f"{x['mapped_to'] or 'nothing'})")
        L.append("")
    tl = rep.get("timeline")
    if tl:
        L.append("## The clock")
        L.append("")
        L.append(timeline_markdown(tl, orchestra_from))
        L.append("")
    n = rep["notes"]
    L.append("## Notes")
    L.append("")
    if orchestra_from:
        L.append(f"{n['mapped_notes']} orchestral notes placed on the solo's measures ({n['clipped_notes']} held into a "
                 f"cut, ended there; {n['restruck_notes']} held from a left-out measure, struck again where the next "
                 f"one starts), {n['dropped_notes']} left out with their measures.")
    else:
        L.append(f"{n['mapped_notes']} arrangement notes placed ({n['clipped_notes']} held into a cut, ended there; "
                 f"{n['restruck_notes']} held from a dropped measure, struck again where the mapped measure starts), "
                 f"{n['dropped_notes']} dropped with their measures; {n['fill_notes']} notes of the orchestral piano "
                 f"part fill the cuts ({n['fill_clipped']} ended where the arrangement takes over).")
    if rep["silent_mapped"]:
        L.append("")
        L.append("Mapped measures where the arrangement is silent while the orchestral piano plays: "
                 + ", ".join(rep["silent_mapped"]))
    if rep.get("to_orchestra"):
        L.append("")
        L.append(f"The orchestra arranged for piano (reduction \"orchestra\"): {n['to_orchestra']} of the arrangement's "
                 f"notes, where the orchestral piano part rests and its orchestra plays, are the song's orchestra "
                 f"(standing for {n['replaced_orchestra']} notes of its instruments there), in "
                 + ", ".join(rep["to_orchestra"]))
    return "\n".join(L) + "\n"


def timeline_markdown(tl: dict, orchestra_from: bool) -> str:
    """The plan's paragraph (:func:`pianovision.timeline.plan_report`)."""
    s = (f"The orchestral score plays on the solo's clock: {tl['marks']} tempo marks for its {tl['measures']} measures "
         f"as played ({tl['mapped']} with the solo's timing, {tl['filled']} with its own scaled to the solo's next "
         f"to them); {tl['seconds']:.1f} s")
    if tl.get("own_seconds"):
        s += f" (its own timing: {tl['own_seconds']:.1f} s)"
    s += (f". Positions inside a measure within {tl['tolerance_ms']:g} ms of the solo's clock (largest deviation "
          f"{tl['max_inside_ms']:g} ms)")
    if tl.get("repeats_spread_ms"):
        s += f"; repeated measures averaged over their passes (up to {tl['repeats_spread_ms']:g} ms apart)"
    s += "."
    if tl.get("clamped"):
        s += f" Tempo clamped to MuseScore's 5-999 bpm in {', '.join(tl['clamped'])}."
    if tl.get("stretch"):
        vals = list(tl["stretch"].values())
        s += (f" Cut measures stretched by {min(vals):.3f}-{max(vals):.3f} (solo time over the orchestral score's "
              f"own next to the cut).")
    if orchestra_from and tl.get("pieces"):
        s += " Companion audio: " + "; ".join(f"orchestral {p['measures'][0]}-{p['measures'][1]} from {p['at']:.2f} s"
                                              for p in tl["pieces"]) + "."
    return s


def json_text(v: Any) -> str:
    import json
    return json.dumps(v, ensure_ascii=False)
