"""Time maps between a recording and a song's timeline, for companion audio made from recordings
(``python -m pianovision recordings``).

Pure Python: ``tools/recording_align.py`` (numpy, librosa) computes the features, the DTW path and the
refinement, and calls this module to turn the path into a map, find the structural differences and plan the
retime; ``pianovision.recordings`` reads the result.  Times are seconds; a pair is ``(song time, recording time)``.

* A **piece** is a stretch where the recording follows the song: anchors ``(song, recording)`` increasing in
  both, linear in between.
* Between pieces (and before the first / after the last) are the **gaps**: recording material the song does not
  have (``recording_extra``: cut), song material the recording does not have (``song_missing``: silence), both
  sides with material in the same place that does not match (``weak``: stretched through, when the lengths agree
  with the tempo; ``different``: cut and silence when they do not).
* The **edit plan** tiles the song's timeline with operations: a span of the recording stretched linearly onto a
  span of the song, or silence.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = 1                         # bump when a map, its gaps or the edit plan come out differently
Pair = Tuple[float, float]          # (song seconds, recording seconds)

RECORDING_EXTRA = "recording_extra"
SONG_MISSING = "song_missing"
DIFFERENT = "different"
WEAK = "weak"
GAP_KINDS = (RECORDING_EXTRA, SONG_MISSING, DIFFERENT, WEAK)


# ------------------------------------------------------------------------------
# the map
# ------------------------------------------------------------------------------

@dataclass
class Piece:
    """A stretch where the recording follows the song: ``anchors`` increase in both times."""
    anchors: List[Pair]
    quality: float = 0.0            # mean similarity of the alignment along the piece (0 = chance, 1 = identical)

    @property
    def song0(self) -> float:
        return self.anchors[0][0]

    @property
    def song1(self) -> float:
        return self.anchors[-1][0]

    @property
    def rec0(self) -> float:
        return self.anchors[0][1]

    @property
    def rec1(self) -> float:
        return self.anchors[-1][1]

    @property
    def ratio(self) -> float:
        """Recording seconds per song second over the piece."""
        ds = self.song1 - self.song0
        return (self.rec1 - self.rec0) / ds if ds > 0 else 1.0

    def to_dict(self) -> dict:
        return {"quality": round(self.quality, 4),
                "anchors": [[round(s, 4), round(r, 4)] for s, r in self.anchors]}

    @classmethod
    def from_dict(cls, d: dict) -> "Piece":
        return cls([(float(s), float(r)) for s, r in d["anchors"]], float(d.get("quality", 0.0)))


@dataclass
class Gap:
    kind: str
    song0: float
    song1: float
    rec0: float
    rec1: float
    edge: str = ""                  # "lead" (before the first piece), "tail" (after the last), "" between pieces

    @property
    def song_len(self) -> float:
        return self.song1 - self.song0

    @property
    def rec_len(self) -> float:
        return self.rec1 - self.rec0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "edge": self.edge, "song": [round(self.song0, 3), round(self.song1, 3)],
                "recording": [round(self.rec0, 3), round(self.rec1, 3)]}


@dataclass
class TimeMap:
    pieces: List[Piece] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"pieces": [p.to_dict() for p in self.pieces]}

    @classmethod
    def from_dict(cls, d: dict) -> "TimeMap":
        return cls([Piece.from_dict(p) for p in d.get("pieces", [])])

    def ratio(self) -> float:
        """Recording seconds per song second over the matched pieces (span weighted)."""
        ds = sum(p.song1 - p.song0 for p in self.pieces)
        dr = sum(p.rec1 - p.rec0 for p in self.pieces)
        return dr / ds if ds > 0 else 1.0

    def rec_at(self, t: float) -> Optional[float]:
        """Recording time of song time ``t``; None outside the pieces."""
        for p in self.pieces:
            if p.song0 <= t <= p.song1:
                return interp([a[0] for a in p.anchors], [a[1] for a in p.anchors], t)
        return None

    def song_at(self, t: float) -> Optional[float]:
        """Song time of recording time ``t``; None outside the pieces."""
        for p in self.pieces:
            if p.rec0 <= t <= p.rec1:
                return interp([a[1] for a in p.anchors], [a[0] for a in p.anchors], t)
        return None

    def gaps(self, song_end: float, rec_end: float, seam: float = 1.0) -> List[Gap]:
        """The unmatched stretches, classified (:func:`classify_gap`); the edges only when they are longer than
        ``seam`` on the song's side or (lead) hold more than ``seam`` of recording before the song's start."""
        out: List[Gap] = []
        if not self.pieces:
            return [Gap(DIFFERENT, 0.0, song_end, 0.0, rec_end)]
        first, last = self.pieces[0], self.pieces[-1]
        # lead: the recording's intro before the song's first matched moment
        ds, dr = first.song0, first.rec0
        if ds >= seam:
            out.append(Gap(classify_gap(ds, dr, first.ratio, seam), 0.0, ds, 0.0, dr, "lead"))
        elif dr - ds >= seam:
            out.append(Gap(RECORDING_EXTRA, 0.0, ds, 0.0, dr - ds, "lead"))
        for a, b in zip(self.pieces, self.pieces[1:]):
            ds, dr = b.song0 - a.song1, b.rec0 - a.rec1
            kind = classify_gap(ds, dr, 0.5 * (a.ratio + b.ratio), seam)
            if kind:
                out.append(Gap(kind, a.song1, b.song0, a.rec1, b.rec0))
        ds, dr = song_end - last.song1, rec_end - last.rec1
        if ds >= seam:
            out.append(Gap(classify_gap(ds, max(dr, 0.0), last.ratio, seam), last.song1, song_end,
                           last.rec1, max(rec_end, last.rec1), "tail"))
        return out


def interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    """Piecewise linear interpolation (xs increasing); constant beyond the ends."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x) - 1
    x0, x1 = xs[i], xs[i + 1]
    if x1 <= x0:
        return ys[i]
    return ys[i] + (ys[i + 1] - ys[i]) * (x - x0) / (x1 - x0)


def classify_gap(ds: float, dr: float, ratio: float, seam: float = 1.0) -> Optional[str]:
    """What an unmatched stretch is: ``ds`` seconds of song against ``dr`` seconds of recording, the recording
    playing ``ratio`` seconds per song second around it.  None: a seam (both short), bridged."""
    ds, dr = max(ds, 0.0), max(dr, 0.0)
    if ds < seam and dr < seam:
        return None
    if ds < seam:
        return RECORDING_EXTRA
    expected = ds * ratio
    if dr < max(seam, 0.25 * expected):
        return SONG_MISSING
    if 0.6 <= dr / expected <= 1 / 0.6:
        return WEAK
    return DIFFERENT


# ------------------------------------------------------------------------------
# DTW path -> pieces
# ------------------------------------------------------------------------------

@dataclass
class PathParams:
    run_s: float = 3.0              # a horizontal / vertical run of the path this long (seconds) is a structural gap
    smooth_s: float = 4.0           # the similarity along the path is averaged over this many seconds
    min_similarity: float = 0.12    # smoothed similarity below this: not matched
    min_piece_s: float = 4.0        # matched stretches shorter than this (either side) are dropped
    max_ratio_dev: float = 2.0      # a piece whose tempo ratio is off the median by more than this factor is dropped
    seam_s: float = 1.0             # gaps shorter than this on both sides are bridged


def _runs(path: Sequence[Tuple[int, int]], axis: int) -> Iterable[Tuple[int, int]]:
    """(start, end) index ranges (end exclusive) of the path where the coordinate ``axis`` stays the same."""
    k = 0
    n = len(path)
    while k < n:
        e = k + 1
        while e < n and path[e][axis] == path[k][axis]:
            e += 1
        yield k, e
        k = e


def pieces_from_path(path: Sequence[Tuple[int, int]], similarity: Sequence[float], song_hop: float, rec_hop: float,
                     params: Optional[PathParams] = None) -> TimeMap:
    """The matched pieces of a DTW path.

    ``path``: (song frame, recording frame) pairs, monotone (sorted here); ``similarity``: per path point, how well the
    two frames match, 1 - cost / (the cost of chance pairs), so 0 is chance and 1 identical.  A point is unmatched
    when it lies on a long horizontal or vertical run of the path (one side stands still: a structural gap) or the
    similarity averaged around it is low; matched stretches shorter than ``min_piece_s`` and pieces whose tempo is far
    off the others' are dropped; pieces separated by less than ``seam_s`` on both sides are joined."""
    p = params or PathParams()
    order = sorted(range(len(path)), key=lambda k: (path[k][0], path[k][1]))
    pts = [(int(path[k][0]), int(path[k][1])) for k in order]
    sim = [float(similarity[k]) for k in order]
    n = len(pts)
    if n < 2:
        return TimeMap([])
    good = [True] * n
    for axis, hop in ((0, rec_hop), (1, song_hop)):            # i constant: the recording runs on, and vice versa
        for a, b in _runs(pts, axis):
            if (b - a - 1) * hop >= p.run_s:
                for k in range(a, b):
                    good[k] = False
    # similarity averaged over a window of the path
    half = max(1, int(round(p.smooth_s / 2 / min(song_hop, rec_hop))))
    cum = [0.0]
    for v in sim:
        cum.append(cum[-1] + v)
    for k in range(n):
        a, b = max(0, k - half), min(n, k + half + 1)
        if (cum[b] - cum[a]) / (b - a) < p.min_similarity:
            good[k] = False
    # matched stretches
    stretches: List[Tuple[int, int]] = []
    k = 0
    while k < n:
        if not good[k]:
            k += 1
            continue
        e = k
        while e + 1 < n and good[e + 1]:
            e += 1
        stretches.append((k, e))
        k = e + 1
    pieces: List[Piece] = []
    for a, b in stretches:
        ds = (pts[b][0] - pts[a][0]) * song_hop
        dr = (pts[b][1] - pts[a][1]) * rec_hop
        if ds < p.min_piece_s or dr < p.min_piece_s:
            continue
        anchors = _frame_anchors(pts[a:b + 1], song_hop, rec_hop)
        if len(anchors) < 2:
            continue
        pieces.append(Piece(anchors, sum(sim[a:b + 1]) / (b - a + 1)))
    if not pieces:
        return TimeMap([])
    ratios = sorted((pc.ratio, pc.song1 - pc.song0) for pc in pieces)
    total = sum(w for _r, w in ratios)
    acc, median = 0.0, ratios[-1][0]
    for r, w in ratios:
        acc += w
        if acc >= total / 2:
            median = r
            break
    pieces = [pc for pc in pieces if 1 / p.max_ratio_dev <= pc.ratio / median <= p.max_ratio_dev]
    return TimeMap(join_seams(pieces, p.seam_s))


def _frame_anchors(pts: Sequence[Tuple[int, int]], song_hop: float, rec_hop: float) -> List[Pair]:
    """One anchor per song frame: the median recording frame the path pairs with it; then made strictly
    increasing (:func:`monotone`)."""
    by_i: Dict[int, List[int]] = {}
    for i, j in pts:
        by_i.setdefault(i, []).append(j)
    raw = []
    for i in sorted(by_i):
        js = sorted(by_i[i])
        m = len(js)
        med = js[m // 2] if m % 2 else 0.5 * (js[m // 2 - 1] + js[m // 2])
        raw.append((i * song_hop, med * rec_hop))
    return monotone(raw, min_slope=0.05 * rec_hop / max(song_hop, 1e-9))


def split_irregular(anchors: Sequence[Pair], lo: float = 0.45, hi: float = 2.2, min_span: float = 1.0,
                    min_piece: float = 2.0) -> List[List[Pair]]:
    """A piece's anchors cut where the recording runs at an implausible speed against the rest of the piece: a run of
    segments whose ratio (recording seconds per song second) is below ``lo`` or above ``hi`` times the piece's median,
    covering at least ``min_span`` seconds on either side, is left out (it becomes a gap: song measures squeezed into
    almost no recording are measures the recording does not have, and vice versa).  Parts shorter than ``min_piece``
    song seconds are dropped."""
    pts = list(anchors)
    if len(pts) < 3:
        return [pts] if len(pts) >= 2 else []
    seg = []
    for (s0, r0), (s1, r1) in zip(pts, pts[1:]):
        ds, dr = s1 - s0, r1 - r0
        seg.append((ds, dr, dr / ds if ds > 0 else float("inf")))
    total_s = sum(x[0] for x in seg)
    ws = sorted((x[2], x[0]) for x in seg if x[0] > 0)
    acc, median = 0.0, ws[-1][0] if ws else 1.0
    for r, w in ws:
        acc += w
        if acc >= total_s / 2:
            median = r
            break
    bad = [not (lo * median <= x[2] <= hi * median) for x in seg]
    # only runs that are long enough count
    k = 0
    cut = [False] * len(seg)
    while k < len(seg):
        if not bad[k]:
            k += 1
            continue
        e = k
        while e + 1 < len(seg) and bad[e + 1]:
            e += 1
        span_s = sum(x[0] for x in seg[k:e + 1])
        span_r = sum(x[1] for x in seg[k:e + 1])
        if span_s >= min_span or span_r >= min_span:
            for i in range(k, e + 1):
                cut[i] = True
        k = e + 1
    out: List[List[Pair]] = []
    cur: List[Pair] = [pts[0]]
    for i, c in enumerate(cut):
        if c:
            if len(cur) >= 2:
                out.append(cur)
            cur = [pts[i + 1]]
        else:
            cur.append(pts[i + 1])
    if len(cur) >= 2:
        out.append(cur)
    return [a for a in out if a[-1][0] - a[0][0] >= min_piece]


def join_seams(pieces: List[Piece], seam: float) -> List[Piece]:
    """Pieces separated by less than ``seam`` on both sides become one (linear across the seam)."""
    out: List[Piece] = []
    for pc in sorted(pieces, key=lambda x: x.song0):
        if out:
            prev = out[-1]
            ds, dr = pc.song0 - prev.song1, pc.rec0 - prev.rec1
            if ds < 0 or dr < 0:                       # overlap (cannot come from one DTW path): keep the better
                if pc.quality > prev.quality:
                    out[-1] = pc
                continue
            if ds < seam and dr < seam:
                w0 = prev.song1 - prev.song0
                w1 = pc.song1 - pc.song0
                q = (prev.quality * w0 + pc.quality * w1) / max(w0 + w1, 1e-9)
                anchors = list(prev.anchors) + [a for a in pc.anchors if a[0] > prev.song1 and a[1] > prev.rec1]
                out[-1] = Piece(anchors, q)
                continue
        out.append(pc)
    return out


# ------------------------------------------------------------------------------
# anchors: monotone fit, simplification
# ------------------------------------------------------------------------------

def isotonic(ys: Sequence[float], ws: Optional[Sequence[float]] = None) -> List[float]:
    """Least-squares non-decreasing fit (pool adjacent violators)."""
    ws = list(ws) if ws is not None else [1.0] * len(ys)
    blocks: List[List[float]] = []                 # [mean, weight, count]
    for y, w in zip(ys, ws):
        blocks.append([float(y), max(float(w), 1e-12), 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            m1, w1, c1 = blocks.pop()
            m0, w0, c0 = blocks.pop()
            blocks.append([(m0 * w0 + m1 * w1) / (w0 + w1), w0 + w1, c0 + c1])
    out: List[float] = []
    for m, _w, c in blocks:
        out.extend([m] * c)
    return out


def monotone(anchors: Sequence[Pair], min_slope: float = 0.05, weights: Optional[Sequence[float]] = None) -> List[Pair]:
    """``anchors`` sorted by song time, with the recording times replaced by the closest (least squares) fit whose
    slope is at least ``min_slope`` everywhere: the recording never stands still or runs backwards."""
    pts = sorted(anchors)
    if not pts:
        return []
    s = [a[0] for a in pts]
    r = [a[1] - min_slope * a[0] for a in pts]
    fit = isotonic(r, weights)
    out: List[Pair] = []
    for si, ri in zip(s, fit):
        rr = ri + min_slope * si
        if out and si <= out[-1][0]:
            continue
        out.append((si, rr))
    return out


def simplify(anchors: Sequence[Pair], tol: float) -> List[Pair]:
    """Douglas-Peucker: the fewest anchors that keep every dropped one within ``tol`` seconds (recording time) of the
    line through its neighbours."""
    pts = list(anchors)
    if len(pts) <= 2:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        (s0, r0), (s1, r1) = pts[a], pts[b]
        worst, wi = -1.0, -1
        for k in range(a + 1, b):
            s, r = pts[k]
            pred = r0 + (r1 - r0) * (s - s0) / (s1 - s0) if s1 > s0 else r0
            d = abs(r - pred)
            if d > worst:
                worst, wi = d, k
        if worst > tol:
            keep[wi] = True
            stack.append((a, wi))
            stack.append((wi, b))
    return [p for p, k in zip(pts, keep) if k]


# ------------------------------------------------------------------------------
# the edit plan: the song's timeline tiled with stretched recording and silence
# ------------------------------------------------------------------------------

@dataclass
class Op:
    """Song ``[song0, song1]`` is the recording ``[rec0, rec1]`` stretched linearly (``kind`` "audio"), or silence."""
    kind: str
    song0: float
    song1: float
    rec0: float = 0.0
    rec1: float = 0.0
    why: str = ""

    @property
    def tempo(self) -> float:
        """Recording seconds per output second (rubberband's ``tempo``: above 1 plays faster)."""
        ds = self.song1 - self.song0
        return (self.rec1 - self.rec0) / ds if self.kind == "audio" and ds > 0 else 1.0

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "song": [round(self.song0, 4), round(self.song1, 4)]}
        if self.kind == "audio":
            d["recording"] = [round(self.rec0, 4), round(self.rec1, 4)]
        if self.why:
            d["why"] = self.why
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Op":
        r = d.get("recording") or [0.0, 0.0]
        return cls(d["kind"], float(d["song"][0]), float(d["song"][1]), float(r[0]), float(r[1]), d.get("why", ""))


def edit_plan(tm: TimeMap, song_end: float, rec_end: float, tail_s: float = 8.0, seam: float = 1.0) -> List[Op]:
    """Operations that tile the song's timeline ``[0, song_end + tail]``:

    * each piece's consecutive anchors: the recording between them stretched onto the song between them;
    * a seam or a ``weak`` gap: the recording across it stretched linearly;
    * ``recording_extra``: the recording's extra material is cut; the little song time there may be (< ``seam``) is
      played from the recording just before the next piece at that piece's tempo;
    * ``song_missing`` and ``different``: silence for the song's span (the recording's span is cut);
    * lead: the song before the first piece is played 1:1 from the recording just before it (silence where the
      recording has nothing), or stretched like a ``weak`` gap;
    * tail: after the last piece the recording plays on 1:1 up to ``tail_s`` past the song's end (or stretched to the
      song's end first when the song's tail is a ``weak`` gap)."""
    ops: List[Op] = []
    if not tm.pieces:
        return [Op("silence", 0.0, song_end, why="no match")]
    first = tm.pieces[0]

    def audio(s0, s1, r0, r1, why=""):
        if s1 - s0 <= 1e-6:
            return
        if r1 - r0 > 1e-6:
            ops.append(Op("audio", s0, s1, r0, r1, why))
        else:
            ops.append(Op("silence", s0, s1, why=why or "the recording stands still"))

    def silence(s0, s1, why):
        if s1 - s0 > 1e-6:
            ops.append(Op("silence", s0, s1, why=why))

    # lead
    s0, r0 = first.song0, first.rec0
    if s0 > 0:
        kind = classify_gap(s0, r0, first.ratio, seam) if s0 >= seam else None
        if kind == WEAK:
            audio(0.0, s0, 0.0, r0, "lead (stretched)")
        else:
            take = min(s0, r0)
            silence(0.0, s0 - take, "lead (no recording before)")
            audio(s0 - take, s0, r0 - take, r0, "lead")
    for idx, pc in enumerate(tm.pieces):
        for (sa, ra), (sb, rb) in zip(pc.anchors, pc.anchors[1:]):
            audio(sa, sb, ra, rb)
        if idx + 1 < len(tm.pieces):
            nx = tm.pieces[idx + 1]
            ds, dr = nx.song0 - pc.song1, nx.rec0 - pc.rec1
            kind = classify_gap(ds, dr, 0.5 * (pc.ratio + nx.ratio), seam)
            if kind is None or kind == WEAK:
                audio(pc.song1, nx.song0, pc.rec1, nx.rec0, kind or "seam")
            elif kind == RECORDING_EXTRA:
                take = min(ds * nx.ratio, max(dr, 0.0))
                audio(pc.song1, nx.song0, nx.rec0 - take, nx.rec0, "after a cut")
                if ds > 1e-6 and take <= 1e-6:
                    silence(pc.song1, nx.song0, "cut")
            else:
                silence(pc.song1, nx.song0, kind)
    # tail
    last = tm.pieces[-1]
    s1, r1 = last.song1, last.rec1
    ds, dr = song_end - s1, rec_end - r1
    if ds >= seam and classify_gap(ds, dr, last.ratio, seam) == WEAK:
        target = song_end
        rt = r1 + ds * last.ratio if dr > ds * last.ratio else rec_end
        audio(s1, target, r1, min(rt, rec_end), "tail (stretched)")
        s1, r1 = target, min(rt, rec_end)
        ds = 0.0
    avail = max(rec_end - r1, 0.0)
    want = max(ds, 0.0) + tail_s
    take = min(avail, want)
    audio(s1, s1 + take, r1, r1 + take, "tail")
    if take < ds:
        silence(s1 + take, song_end, "tail (recording ended)")
    return ops


def output_length(ops: Sequence[Op]) -> float:
    return ops[-1].song1 if ops else 0.0


def cuts(ops: Sequence[Op], tol: float = 1e-3) -> List[int]:
    """Indices of the operations that start where the recording does not continue from the previous one (a cut or
    silence before, or silence itself): their joins need fades."""
    out = []
    for k in range(1, len(ops)):
        a, b = ops[k - 1], ops[k]
        if a.kind != "audio" or b.kind != "audio" or abs(b.rec0 - a.rec1) > tol:
            out.append(k)
    return out


def check_plan(ops: Sequence[Op], tol: float = 1e-6) -> None:
    """Raise ValueError unless the operations tile the timeline from 0 in order with positive lengths."""
    t = 0.0
    for k, op in enumerate(ops):
        if abs(op.song0 - t) > tol:
            raise ValueError(f"op {k} starts at {op.song0}, expected {t}")
        if op.song1 <= op.song0:
            raise ValueError(f"op {k} is empty")
        if op.kind == "audio" and op.rec1 <= op.rec0:
            raise ValueError(f"op {k} plays the recording backwards")
        t = op.song1


def apply_plan(ops: Sequence[Op], t: float) -> Optional[float]:
    """Recording time that plays at output (song) time ``t``; None in silence or beyond the end."""
    for op in ops:
        if op.song0 <= t <= op.song1:
            if op.kind != "audio":
                return None
            return op.rec0 + (op.rec1 - op.rec0) * (t - op.song0) / (op.song1 - op.song0)
    return None


# ------------------------------------------------------------------------------
# summaries
# ------------------------------------------------------------------------------

def measure_label(m: dict, repeated: Iterable[int] = ()) -> str:
    n = m.get("number", m.get("index", 0) + 1)
    p = m.get("pass", 1)
    return f"{n}:{p}" if p > 1 or n in set(repeated) else f"{n}"


def measure_ranges(measures: Sequence[dict], t0: float, t1: float, min_overlap: float = 0.5) -> List[str]:
    """Labels of the measures (``start``/``end`` seconds, ``number``, ``pass``) that lie at least ``min_overlap`` of
    their length inside ``[t0, t1]``, consecutive ones as ranges ("12-30", "8:2-9:2")."""
    counts: Dict[int, int] = {}
    for m in measures:
        counts[m["number"]] = counts.get(m["number"], 0) + 1
    repeated = {n for n, c in counts.items() if c > 1}
    idx = []
    for k, m in enumerate(measures):
        a, b = m["start"], m["end"]
        if b <= a:
            continue
        ov = min(b, t1) - max(a, t0)
        if ov >= min_overlap * (b - a):
            idx.append(k)
    return ranges_text([measures[k] for k in idx], idx, repeated)


def ranges_text(ms: Sequence[dict], idx: Sequence[int], repeated: Iterable[int] = ()) -> List[str]:
    """Consecutive measures (by playback index) as ranges of labels."""
    out: List[str] = []
    k = 0
    while k < len(idx):
        e = k
        while e + 1 < len(idx) and idx[e + 1] == idx[e] + 1:
            e += 1
        a, b = measure_label(ms[k], repeated), measure_label(ms[e], repeated)
        out.append(a if k == e else f"{a}-{b}")
        k = e + 1
    return out


def tempo_summary(tm: TimeMap, measures: Sequence[dict]) -> dict:
    """The recording's tempo against the song's: ``ratio`` (recording seconds per song second over the matched
    pieces: 1.25 = the recording is 25 % slower), the spread of the per-measure ratios, a linear trend (ratio change
    per minute of song: the recording speeding up or slowing down against the score), the drift: how far the
    recording's timing wanders from a constant tempo (each piece's straight line), max and RMS seconds, and the
    ``irregular`` measures, whose ratio is off the median by more than 1/0.6 (the recording plays them with other
    lengths: ``recording_beats`` estimates how many of the score's beats it takes)."""
    ratio = tm.ratio()
    per: List[Tuple[float, float]] = []                    # (measure start, ratio)
    for m in measures:
        a, b = m["start"], m["end"]
        if b - a <= 1e-3:
            continue
        ra, rb = tm.rec_at(a), tm.rec_at(b)
        if ra is None or rb is None:
            continue
        # both ends in the same piece
        if not any(p.song0 <= a and b <= p.song1 for p in tm.pieces):
            continue
        per.append((a, (rb - ra) / (b - a)))
    resid: List[float] = []
    for p in tm.pieces:
        n = len(p.anchors)
        if n < 3:
            continue
        sx = sum(a[0] for a in p.anchors) / n
        sy = sum(a[1] for a in p.anchors) / n
        vx = sum((a[0] - sx) ** 2 for a in p.anchors)
        k = sum((a[0] - sx) * (a[1] - sy) for a in p.anchors) / vx if vx > 0 else 0.0
        resid.extend(a[1] - (sy + k * (a[0] - sx)) for a in p.anchors)
    out = {"ratio": round(ratio, 4), "measures": len(per)}
    irregular = []
    if per:
        med = sorted(r for _t, r in per)[len(per) // 2]
        by_start = {m["start"]: m for m in measures}
        for t, r in per:
            if r < 0.6 * med or r > med / 0.6:
                m = by_start.get(t, {})
                beats = len(m.get("beats", [])) or None
                irregular.append({"measure": measure_label(m) if m else f"{t:.2f}s", "ratio": round(r, 3),
                                  "beats": beats,
                                  "recording_beats": round(beats * r / med, 1) if beats else None})
    out["irregular"] = irregular
    if per:
        rs = sorted(r for _t, r in per)
        q = lambda f: rs[min(len(rs) - 1, max(0, int(round(f * (len(rs) - 1)))))]
        out.update({"p10": round(q(0.1), 4), "median": round(q(0.5), 4), "p90": round(q(0.9), 4)})
        if len(per) > 2:
            mx = sum(t for t, _ in per) / len(per)
            my = sum(r for _, r in per) / len(per)
            vx = sum((t - mx) ** 2 for t, _ in per)
            out["trend_per_min"] = round(60 * sum((t - mx) * (r - my) for t, r in per) / vx, 4) if vx > 0 else 0.0
    if resid:
        out["drift_max_s"] = round(max(abs(x) for x in resid), 3)
        out["drift_rms_s"] = round(math.sqrt(sum(x * x for x in resid) / len(resid)), 3)
    return out
