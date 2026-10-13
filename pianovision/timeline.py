"""The song's clock from the piano solo: a piano solo and an orchestral score of one piece, played on the solo's clock.

Two scores of the same music are paired by the build options of the one that is the song
(:mod:`pianovision.buildopts`):

* ``simplified_from`` on an orchestral score: the song is the orchestral score (its piano part, its orchestra, its
  measures), the solo arrangement's piano part becomes the simplified part (:mod:`pianovision.arrangement`);
* ``orchestra_from`` on a piano solo: the song is the solo (its piano part, its measures), the orchestral score's
  orchestra becomes the song's orchestra.

Either way the two are aligned measure by measure, the solo against the orchestral score
(:func:`pianovision.arrangement.arrange`: the same alignment read in both directions), and **the song plays on
the solo's clock**: its tempo map, fermatas and pauses carried across the alignment onto the orchestral score's
measures.  The orchestral score is always rendered on that clock, by the formatter (the song's notes, tempos,
measures, markers, lyrics) and by MuseScore (the companion audio, :mod:`pianovision.accompaniment`), from the same
edit of the score: its own tempo marks, fermatas, gradual tempo changes, breath and section pauses are switched off
(``<play>0</play>``, pauses of 0) and invisible tempo marks written in (:class:`Plan`).  Only the copy MuseScore
renders and the formatter's model of the score are edited, never the score.

**The plan** (:func:`plan`).  Every orchestral measure as played gets a length on the solo's clock and a shape
inside it:

* a measure the solo plays (a *mapped* measure): the times the solo plays the measure's positions at, through
  the alignment's unit and its time warp (:func:`pianovision.arrangement.unit_warp`, read backwards);
* a measure the solo leaves out (*fill*): the orchestral score's own timing (its tempo marks, fermatas, pauses)
  scaled by the stretch of the solo against the orchestral score next to it: the median of the up to
  :data:`STRETCH_NEIGHBOURS` mapped measures before the cut and after it (solo length over orchestral length),
  interpolated geometrically across the cut (at the start or the end of the piece, the side there is).

A tempo mark is written where a measure starts and wherever inside it the solo's clock bends by more than
:data:`TOLERANCE` (a fermata, a ritardando, an arrangement's longer measure): at a position where some staff has a
chord or a rest (MuseScore anchors tempo marks there).  A measure's marks reproduce its length exactly; the
positions inside it come within the tolerance.  A measure played more than once (repeats) has one set of tempo
marks: the average of its passes.  Marks are rounded to 6 decimals of beats per second (MuseScore 4.7's tempo
precision), kept within MuseScore's 5-999 bpm, and a measure-start mark equal to the tempo already in effect is
left out.

**The song**: with ``simplified_from`` the song is the orchestral score rendered on the solo's clock (the
arrangement's notes are retimed onto it as before); with ``orchestra_from`` the song is the solo as it is, and the
orchestral score's orchestra, rendered on the solo's clock, is moved onto the solo's measures through the alignment
(:class:`pianovision.arrangement.Retimer` backwards; :func:`inject_orchestra`): its staves become the song's
orchestra staves after the solo's own.  Orchestral measures the solo leaves out are left out of the song (their
notes are not played), and solo measures without counterpart have no orchestra.

**The audio** (``orchestra_from``): the render of the edited orchestral score is cut into :class:`Piece` s, the
runs of measures that follow on in both scores, each placed where the solo plays it (silence before the first:
the solo's pickup; cuts where the solo leaves orchestral measures out, silence where the solo has measures of its
own).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

TIMELINE_REVISION = 1
"""Bumped when the plan changes what a paired score renders to (the library renders those songs again)."""

TOLERANCE = 0.005             # s: positions inside a measure may land this far from the solo's clock
STRETCH_NEIGHBOURS = 4        # a fill's stretch: the median of this many mapped measures on either side
MIN_BPS, MAX_BPS = 5.0 / 60, 999.0 / 60      # MuseScore's tempo range (TempoText::tempo)
PRECISION = 6                 # decimals of beats per second (MuseScore 4.7's roundTempo)

SONG_ORCHESTRA = "orchestra"  # the song is the orchestral score (simplified_from)
SONG_SOLO = "solo"            # the song is the piano solo (orchestra_from)


@dataclass
class Mark:
    """A tempo mark written into the orchestral score: at ``rtick`` ticks into score measure ``measure`` (0-based),
    before the chord or rest at ``path`` (:attr:`pianovision.score.ChordRest.xml_path`)."""
    measure: int
    rtick: int
    bps: float                      # quarter notes per second, rounded to PRECISION decimals
    path: Tuple[int, int, int, int]
    track: int = 0
    pos: Fraction = Fraction(0)     # the exact position from the measure start (whole notes)

    @property
    def text(self) -> str:
        return f"{self.bps:.{PRECISION}f}"


@dataclass
class Piece:
    """``orchestra_from``: orchestral measures ``first``..``last`` (as played, 0-based) follow on in both scores; the
    song plays them from ``at`` seconds on.  ``start``/``end``: where the render of the edited score has them
    (seconds; set by :func:`place_pieces`)."""
    first: int
    last: int
    at: float
    start: float = 0.0
    end: float = 0.0

    def to_dict(self) -> dict:
        return {"measures": [self.first, self.last], "at": round(self.at, 4), "render": [round(self.start, 4),
                                                                                          round(self.end, 4)]}


@dataclass
class Plan:
    song: str                                   # SONG_ORCHESTRA | SONG_SOLO
    marks: List[Mark]
    lengths: List[float]                        # per orchestral measure as played: seconds on the solo's clock
    pieces: List[Piece] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def digest(self) -> str:
        """What the plan writes into the score and how the audio is cut (the companion audio's cache)."""
        doc = {"revision": TIMELINE_REVISION, "song": self.song,
               "marks": [[m.measure, m.rtick, m.text] for m in self.marks],
               "pieces": [[p.first, p.last, round(p.at, 6)] for p in self.pieces]}
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------
# where tempo marks can go
# ------------------------------------------------------------------------------

def write_positions(score) -> List[Dict[int, Tuple[Tuple[int, int, int, int], int, Fraction]]]:
    """Per score measure: ticks from its start -> (the .mscx path of the chord or rest a tempo mark is written before,
    its track, its exact position from the measure start); the first staff and voice that has one there."""
    from .score import SEG_CHORDREST, ticks
    out = []
    for m in score.measures:
        d: Dict[int, Tuple[Tuple[int, int, int, int], int, Fraction]] = {}
        t0 = ticks(m.tick)
        for seg in m.segments:
            if seg.type != SEG_CHORDREST:
                continue
            for track in sorted(seg.elements):
                p = getattr(seg.elements[track], "xml_path", None)
                if p is not None:
                    d.setdefault(ticks(seg.tick) - t0, (tuple(p), track, seg.tick - m.tick))
                    break
        out.append(d)
    return out


# ------------------------------------------------------------------------------
# the plan
# ------------------------------------------------------------------------------

def _round_bps(bps: float) -> float:
    bps = min(max(bps, MIN_BPS), MAX_BPS)
    scale = 10 ** PRECISION
    return math.floor(bps * scale + 0.5) / scale


def _inverse(points: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    return [(b, a) for a, b in points]


WARP_SPREAD = 3.0             # a unit's warp shapes the clock when no part of it is this much steeper or flatter


def _regular(points: Sequence[Tuple[int, int]]) -> bool:
    """A warp (orchestral offset, solo offset) whose every stretch keeps within :data:`WARP_SPREAD` of its mean
    slope (an identity warp always)."""
    (x0, y0), (x1, y1) = points[0], points[-1]
    if x1 <= x0 or y1 <= y0:
        return False
    mean = (y1 - y0) / (x1 - x0)
    for (a, b), (c, d) in zip(points, points[1:]):
        if c <= a:
            return False
        slope = (d - b) / (c - a)
        if not mean / WARP_SPREAD <= slope <= mean * WARP_SPREAD:
            return False
    return True


def _shape(points: Sequence[int], times: Sequence[float], tolerance: float) -> Tuple[List[Tuple[int, float]], float,
                                                                                         float, bool]:
    """Tempo marks for one measure: ``points`` (ticks from its start, the last one its end) and the ``times`` (s from
    its start) they should be played at -> ([(tick, bps)], the measure's length with those marks, the largest
    deviation inside it, whether a tempo had to be clamped).  Greedy: from each mark on, the longest stretch one
    tempo plays within ``tolerance``; the stretch's end lands exactly (rounding aside)."""
    marks: List[Tuple[int, float]] = []
    p, t = 0, 0.0
    n = len(points) - 1
    worst = 0.0
    clamped = False
    while p < n:
        best = None
        for q in range(p + 1, n + 1):
            span = times[q] - t
            if span <= 0:
                continue
            bps = (points[q] - points[p]) / 480.0 / span
            ok = all(abs(t + (points[r] - points[p]) / 480.0 / bps - times[r]) <= tolerance for r in range(p + 1, q))
            if not ok and best is not None:
                break
            if ok:
                best = (q, bps)
        if best is None:                                  # nothing reachable (the clock does not advance): slowest
            q, bps = p + 1, MIN_BPS
        else:
            q, bps = best
        rb = _round_bps(bps)
        clamped = clamped or rb in (_round_bps(MIN_BPS), _round_bps(MAX_BPS)) and abs(rb - bps) > 1e-6
        for r in range(p + 1, q):
            worst = max(worst, abs(t + (points[r] - points[p]) / 480.0 / rb - times[r]))
        marks.append((points[p], rb))
        t += (points[q] - points[p]) / 480.0 / rb
        p = q
    worst = max(worst, abs(t - times[n]))
    return marks, t, worst, clamped


def plan(al, solo, orch, score, song: str, tolerance: float = TOLERANCE) -> Plan:
    """The tempo marks that put the orchestral score on the solo's clock (module docstring).  ``al``: the alignment
    (:func:`pianovision.arrangement.arrange`; ``al.arr`` the solo, ``al.orch`` the orchestral score as rendered with
    its own timing), ``solo`` / ``orch``: their :class:`pianovision.arrangement.Side` s, ``score``: the orchestral
    score's model (where marks can go), ``song``: :data:`SONG_ORCHESTRA` or :data:`SONG_SOLO`."""
    from .arrangement import unit_warp, _warp
    A, O = al.arr, al.orch
    positions = write_positions(score)
    unit_of: Dict[int, int] = {}
    for k, u in enumerate(al.units):
        for j in range(u.orch[0], u.orch[1] + 1):
            unit_of.setdefault(j, k)
    warps = [_inverse(unit_warp(al, u, solo.notes, orch.notes)) for u in al.units]
    regular = [_regular(w) for w in warps]

    def solo_time(k: int, x_o: int) -> float:
        """Seconds on the solo's clock of the orchestral offset ``x_o`` (unrolled ticks from unit k's start)."""
        u = al.units[k]
        x_a = _warp(warps[k], x_o)
        return float(solo.seconds(A.tick(A.measures[u.arr[0]].utick + x_a)))

    def own_time(utick: int) -> float:
        return float(orch.seconds(O.tick(utick)))

    n = len(O.measures)
    points: List[List[int]] = []
    rel: List[List[float]] = []                        # per measure as played: times of its points from its start
    mapped = [j in unit_of for j in range(n)]
    for j, m in enumerate(O.measures):
        pos = sorted(t for t in positions[m.number - 1] if 0 <= t < m.length) if m.number - 1 < len(positions) else []
        if not pos or pos[0] != 0:
            pos = [0] + pos
        pts = pos + [m.length]
        if mapped[j] and regular[unit_of[j]]:
            k = unit_of[j]
            base = m.utick - O.measures[al.units[k].orch[0]].utick
            ts = [solo_time(k, base + c) for c in pts]
        elif mapped[j]:
            # a warp that crowds or stretches part of the measure (the solo has only some of its music): the solo's
            # length, the orchestral score's own timing inside
            k = unit_of[j]
            u = al.units[k]
            o0, o1 = O.measures[u.orch[0]].utick, O.measures[u.orch[1]].end_utick
            scale = (solo_time(k, o1 - o0) - solo_time(k, 0)) / max(own_time(o1) - own_time(o0), 1e-9)
            t0 = solo_time(k, 0)
            ts = [t0 + (own_time(m.utick + c) - own_time(o0)) * scale for c in pts]
        else:
            ts = [own_time(m.utick + c) for c in pts]
        points.append(pts)
        rel.append([x - ts[0] for x in ts])
    # the fills' stretch: solo length over own length of the mapped measures next to the cut
    own_len = [own_time(m.end_utick) - own_time(m.utick) for m in O.measures]
    ratio = [rel[j][-1] / own_len[j] if mapped[j] and own_len[j] > 0 else None for j in range(n)]
    stretch_of: Dict[int, float] = {}
    j = 0
    while j < n:
        if mapped[j]:
            j += 1
            continue
        e = j
        while e + 1 < n and not mapped[e + 1]:
            e += 1
        before = [r for r in (ratio[x] for x in range(j - 1, -1, -1)) if r is not None][:STRETCH_NEIGHBOURS]
        after = [r for r in (ratio[x] for x in range(e + 1, n)) if r is not None][:STRETCH_NEIGHBOURS]
        sl = statistics.median(before) if before else None
        sr = statistics.median(after) if after else None
        sl, sr = sl or sr or 1.0, sr or sl or 1.0
        for x in range(j, e + 1):
            w = (x - j + 0.5) / (e - j + 1)
            stretch_of[x] = math.exp((1 - w) * math.log(sl) + w * math.log(sr))
            rel[x] = [t * stretch_of[x] for t in rel[x]]
        j = e + 1
    # one set of marks per score measure: its passes averaged
    passes: Dict[int, List[int]] = {}
    for j, m in enumerate(O.measures):
        passes.setdefault(m.number - 1, []).append(j)
    marks: List[Mark] = []
    length_of: Dict[int, float] = {}
    worst, clamped, spread = 0.0, [], 0.0
    for k in sorted(passes):
        js = passes[k]
        pts = points[js[0]]
        times = [statistics.fmean(rel[j][i] for j in js) for i in range(len(pts))]
        if len(js) > 1:
            spread = max(spread, max(abs(rel[j][-1] - times[-1]) for j in js))
        ms, total, dev, cl = _shape(pts, times, tolerance)
        for j in js:
            worst = max(worst, max(abs(total - rel[j][-1]), dev))
        if cl:
            clamped.append(O.label(js[0]))
        length_of[k] = total
        for rt, bps in ms:
            path, track, pos = positions[k].get(rt, (None, 0, Fraction(0)))
            if path is None:
                continue                                  # no chord or rest there (a measure repeat): the tempo carries
            marks.append(Mark(k, rt, bps, path, track, pos))
    marks.sort(key=lambda x: (x.measure, x.rtick))
    # a measure-start mark equal to the tempo in effect goes (the tempo map is by score position: the last mark before
    # it in the score is in effect, whatever the playback order); a short measure keeps its own (MuseScore gives a
    # pickup without a tempo mark the next measure's)
    kept: List[Mark] = []
    for mk in marks:
        m = score.measures[mk.measure]
        if mk.rtick == 0 and kept and kept[-1].bps == mk.bps and not m.timesig - m.len > 0:
            continue
        kept.append(mk)
    lengths = [length_of[m.number - 1] for m in O.measures]
    p = Plan(song, kept, lengths)
    own_shape = [O.label(j) for j in range(n) if mapped[j] and not regular[unit_of[j]]]
    p.stats = {"marks": len(kept), "measures": n, "mapped": sum(mapped), "filled": n - sum(mapped),
               "own_shape": own_shape,
               "max_inside_ms": round(worst * 1000, 1), "repeats_spread_ms": round(spread * 1000, 1),
               "clamped": clamped, "tolerance_ms": round(tolerance * 1000, 1),
               "stretch": {O.label(j): round(s, 3) for j, s in sorted(stretch_of.items())},
               "seconds": round(sum(lengths), 2)}
    if song == SONG_SOLO:
        p.pieces = pieces(al, solo)
    return p


def pieces(al, solo) -> List[Piece]:
    """``orchestra_from``: the runs of orchestral measures whose units follow on in both scores, each at the solo's
    time of its first unit (measures the solo leaves out are in none)."""
    out: List[Piece] = []
    A = al.arr
    for k, u in enumerate(al.units):
        at = float(solo.seconds(A.measures[u.arr[0]].tick))
        prev = al.units[k - 1] if k else None
        if (out and prev is not None and u.arr[0] == prev.arr[1] + 1 and u.orch[0] == prev.orch[1] + 1):
            out[-1].last = u.orch[1]
        else:
            out.append(Piece(u.orch[0], u.orch[1], at))
    return out


def place_pieces(p: Plan, timeline, seconds: Callable[[int], float], end: float) -> List[Piece]:
    """The pieces' place in the render of the edited orchestral score (``timeline``: its
    :class:`pianovision.arrangement.Timeline`, ``seconds``: its MIDI tick -> s); the last runs to ``end`` (the
    render's end: the last notes' tails)."""
    for i, pc in enumerate(p.pieces):
        pc.start = float(seconds(timeline.measures[pc.first].tick))
        pc.end = end if i == len(p.pieces) - 1 else float(seconds(timeline.measures[pc.last].end_tick))
    return p.pieces


def song_time(pcs: Sequence[Piece], t: float) -> Optional[float]:
    """A time of the edited orchestral render -> the song's clock (None: in a cut)."""
    for pc in pcs:
        if pc.start - 1e-9 <= t < pc.end or (pc is pcs[-1] and t >= pc.start):
            return pc.at + (t - pc.start)
    return None


# ------------------------------------------------------------------------------
# the edit: the formatter's model and MuseScore's copy
# ------------------------------------------------------------------------------

def apply_to_score(score, p: Plan) -> None:
    """The plan in a score model (:func:`pianovision.mscx.read_score`), as :func:`apply_to_mscx` writes it into the
    file: tempo marks, fermatas and gradual tempo changes do not play, breaths and section breaks pause 0 s, and the
    plan's marks (invisible) are added.  The notes and their places in the file stay."""
    from .score import SEG_BREATH, SEG_CHORDREST, Element
    for m in score.measures:
        for seg in m.segments:
            for e in seg.annotations:
                if e.tag in ("Tempo", "Fermata"):
                    e.props["play"] = "0"
            if seg.type == SEG_BREATH:
                for b in seg.breaths.values():
                    b.props["pause"] = "0"
    for mb in score.measure_bases:
        if mb.section_break is not None:
            mb.section_break.props["pause"] = 0.0
    for sp in score.spanners:
        if sp.tag == "GradualTempoChange":
            sp.props["play"] = "0"
    for mk in p.marks:
        m = score.measures[mk.measure]
        seg = m.find_segment(SEG_CHORDREST, m.tick + mk.pos)
        if seg is None:
            raise ValueError(f"timeline: no chord or rest {mk.rtick} ticks into measure {mk.measure + 1}")
        seg.annotations.append(Element("Tempo", track=mk.track, props={
            "tempo": mk.text, "followText": "0", "visible": "0", "text": _mark_text(mk)}))


def _mark_text(mk: Mark) -> str:
    return f"solo tempo q = {mk.bps * 60:.2f}"


_TAG_RE = re.compile(r'<(/?)([A-Za-z_][\w.:-]*)((?:[^>"\'/]|"[^"]*"|\'[^\']*\'|/(?!>))*)(/?)>|<!--.*?-->|<\?.*?\?>'
                     r'|<!\[CDATA\[.*?\]\]>', re.S)


def _child_offsets(text: str, want) -> Dict[Tuple[int, int, int, int], int]:
    """Offsets in the .mscx text of the wanted ``(staff, measure, voice, child)`` elements: the Nth ``<Staff>`` of
    the ``<Score>``, its Nth ``<Measure>``, the measure's Nth ``<voice>``, the voice's Nth child element (the
    numbering of :attr:`pianovision.score.ChordRest.xml_path`)."""
    want = set(want)
    out: Dict[Tuple[int, int, int, int], int] = {}
    # frames: [tag, index among its counted siblings, child counter]
    stack: List[list] = []
    staff_i = -1
    for m in _TAG_RE.finditer(text):
        close, name = m.group(1), m.group(2)
        if name is None:
            continue                                         # a comment, a declaration
        if close:
            if stack:
                stack.pop()
            continue
        depth = len(stack)
        idx = -1
        if depth >= 1:
            parent = stack[-1]
            if parent[0] == "voice" and depth >= 5:
                idx = parent[2]
                parent[2] += 1
                key = (stack[-3][1], stack[-2][1], parent[1], idx)
                if key in want:
                    out[key] = m.start()
            elif name == "Staff" and parent[0] == "Score":
                staff_i += 1
                idx = staff_i
            elif name == "Measure" and parent[0] == "Staff" and depth >= 2 and stack[-2][0] == "Score":
                idx = parent[2]
                parent[2] += 1
            elif name == "voice" and parent[0] == "Measure" and depth >= 3 and stack[-2][0] == "Staff":
                idx = parent[2]
                parent[2] += 1
        if not m.group(4):
            stack.append([name, idx, 0])
    return out


def _neutralise(text: str) -> str:
    def no_play(mm: "re.Match") -> str:
        tag, body = mm.group(1), mm.group(2)
        body = re.sub(r"\s*<play>[^<]*</play>", "", body)
        return f"<{tag}>\n<play>0</play>{body}</{tag}>"

    text = re.sub(r"<(Tempo|Fermata|GradualTempoChange)>(.*?)</\1>", no_play, text, flags=re.S)
    text = re.sub(r"<Fermata/>", "<Fermata><play>0</play></Fermata>", text)

    def no_pause(mm: "re.Match") -> str:
        tag, body = mm.group(1), mm.group(2)
        if tag == "LayoutBreak" and not re.search(r"<subtype>\s*section\s*</subtype>", body):
            return mm.group(0)
        body = re.sub(r"\s*<pause>[^<]*</pause>", "", body)
        return f"<{tag}>\n<pause>0</pause>{body}</{tag}>"

    return re.sub(r"<(Breath|LayoutBreak)>(.*?)</\1>", no_pause, text, flags=re.S)


def apply_to_mscx(mscx: bytes, p: Plan) -> bytes:
    """The plan written into the score file (the main .mscx, as bytes): ``<play>0</play>`` in every tempo mark,
    fermata and gradual tempo change, a 0 s pause in every breath and section break, and an invisible tempo mark per
    plan mark before its chord or rest.  Every other byte stays."""
    text = _neutralise(mscx.decode("utf-8"))
    offsets = _child_offsets(text, [mk.path for mk in p.marks])
    missing = [mk for mk in p.marks if mk.path not in offsets]
    if missing:
        raise ValueError(f"timeline: {len(missing)} tempo mark position(s) not found in the score, e.g. "
                         f"measure {missing[0].measure + 1} at {missing[0].rtick} ticks")
    for mk in sorted(p.marks, key=lambda x: offsets[x.path], reverse=True):
        at = offsets[mk.path]
        el = (f"<Tempo>\n<tempo>{mk.text}</tempo>\n<followText>0</followText>\n<visible>0</visible>\n"
              f"<text>{_mark_text(mk)}</text>\n</Tempo>\n")
        text = text[:at] + el + text[at:]
    return text.encode("utf-8")


def write_edited(src: str, dst: str, p: Plan) -> str:
    """A copy of the score file ``src`` at ``dst`` with the plan written in (every other member as it is)."""
    import os
    import zipfile
    with zipfile.ZipFile(src) as zin:
        main = _main_mscx(zin)
        tmp = dst + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == main:
                    data = apply_to_mscx(data, p)
                zout.writestr(item, data)
    os.replace(tmp, dst)
    return dst


def _main_mscx(z) -> str:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(z.read("META-INF/container.xml"))
        for rf in root.iter("rootfile"):
            fp = rf.get("full-path", "")
            if fp.endswith(".mscx"):
                return fp
    except KeyError:
        pass
    names = [n for n in z.namelist() if n.endswith(".mscx") and "/" not in n]
    if not names:
        raise ValueError("no .mscx in the score")
    return names[0]


# ------------------------------------------------------------------------------
# rendering a pair
# ------------------------------------------------------------------------------

@dataclass
class Rendered:
    """A score rendered by the formatter: the model, the renderer (its timeline) and the MIDI."""
    score: Any
    renderer: Any
    midi: Any


def render(path: str, compat=None, options: Optional[dict] = None, p: Optional[Plan] = None,
           provenance: bool = False) -> Rendered:
    """``path`` rendered with its knobs and build options (piano staves, hands); with ``p``, on the solo's clock."""
    from .handsplit import apply_piano_options
    from .mscx import read_score
    from .render import Compat, Renderer
    c = compat if isinstance(compat, Compat) else Compat.from_dict(compat)
    score = read_score(path)
    if p is not None:
        apply_to_score(score, p)
    r = Renderer(score, c, provenance=provenance)
    midi = r.render()
    apply_piano_options(midi, options)
    return Rendered(score, r, midi)


@dataclass
class Pair:
    """Two scores aligned, the orchestral score rendered on the solo's clock."""
    song: str                         # SONG_ORCHESTRA | SONG_SOLO
    al: Any                           # the alignment; al.orch: the orchestral timeline on the solo's clock
    plan: Plan
    solo: Any                         # the solo's Side
    orch: Any                         # the orchestral score's Side on the solo's clock (no features)
    orch_render: Rendered             # ... and its render
    solo_notes: Optional[dict] = None   # SONG_ORCHESTRA: the solo's parts original part (the simplified part)
    orch_own_seconds: float = 0.0     # the orchestral score's length with its own timing


def pair(orch_path: str, orch_compat, orch_options: Optional[dict], solo_path: str, solo_compat,
         solo_options: Optional[dict], song: str, measure_map: Any = "auto", solo_render: Optional[Rendered] = None,
         provenance: bool = False) -> Pair:
    """Align the solo with the orchestral score and render the orchestral score on the solo's clock (``provenance``:
    the notes of that render carry their score notes; ``solo_render``: the solo as already rendered, the song)."""
    from .arrangement import arrange, render_arrangement, side_of
    first = render(orch_path, orch_compat, orch_options)
    orch_own = side_of(first.score, first.renderer, first.midi, False)
    solo_notes = None
    if solo_render is not None:
        solo = side_of(solo_render.score, solo_render.renderer, solo_render.midi, True)
    else:
        solo, solo_notes = render_arrangement(solo_path, solo_compat, solo_options)
    al = arrange(orch_own, solo, measure_map)
    p = plan(al, solo, orch_own, first.score, song)
    again = render(orch_path, orch_compat, orch_options, p, provenance=provenance)
    orch = side_of(again.score, again.renderer, again.midi, False, features=False)
    if len(orch.timeline.measures) != len(al.orch.measures):
        raise ValueError("timeline: the orchestral score plays other measures on the solo's clock")
    own_end = float(orch_own.seconds(al.orch.measures[-1].end_tick)) if al.orch.measures else 0.0
    al.orch = orch.timeline
    return Pair(song, al, p, solo, orch, again, solo_notes, own_end)


# ------------------------------------------------------------------------------
# orchestra_from: the orchestral score's orchestra onto the solo's measures
# ------------------------------------------------------------------------------

@dataclass
class PartnerTracks:
    """The tracks of a song's MIDI that come from another score (``orchestra_from``): from ``first`` on; their roles
    (:func:`pianovision.pvjson.staff_roles`) are given, not read from their names and programs."""
    first: int
    orchestra: List[Tuple[int, int]]          # (track, program)
    vocal: List[Tuple[int, int]]
    score: str = ""                           # the score they come from (scores.json path)


def inject_orchestra(song_midi, pr: Pair, rel: str = ""):
    """Append the orchestral score's orchestra and vocal staves (rendered on the solo's clock) to the song's MIDI,
    moved onto the solo's measures (:class:`pianovision.arrangement.Retimer` backwards).  Returns the retiming's
    :class:`pianovision.arrangement.RetimeStats` (mapped / dropped / clipped / restruck notes)."""
    from .arrangement import RetimeStats, Retimer
    from .pvjson import StaffFacts, staff_roles
    from .smf import Msg
    om = pr.orch_render.midi
    roles = staff_roles(om)
    keep = {t: ("orchestra", p) for t, p in roles.orchestra}
    keep.update({t: ("vocal", p) for t, p in roles.vocal})
    rt = Retimer(pr.al, pr.solo.notes, pr.orch.notes, reverse=True)
    st = RetimeStats()
    first = len(song_midi.tracks)
    facts = list(getattr(song_midi, "staves", None) or [])
    part0 = (max((f.part for f in facts), default=-1) + 1) if facts else 0
    orch_facts = list(getattr(om, "staves", None) or [])
    for ti, track in enumerate(om.tracks):
        events: List[Tuple[int, int, Msg]] = []
        tick = 0
        active: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
        for msg in track:
            tick += msg.time
            if msg.type in ("track_name", "program_change"):
                events.append((0, 0, Msg(msg.type, channel=msg.channel, program=msg.program, name=msg.name)))
            elif ti in keep and msg.type == "note_on" and msg.velocity > 0:
                active.setdefault((msg.channel, msg.note), []).append((tick, msg.velocity))
            elif ti in keep and (msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)):
                ons = active.get((msg.channel, msg.note))
                if not ons:
                    continue
                on, velo = ons.pop(0)
                r = rt.note(on, tick)
                if r is None:
                    st.dropped_notes += 1
                    continue
                a_on, a_off, how = r
                st.mapped_notes += 1
                st.clipped_notes += how == "clipped"
                st.restruck_notes += how == "restruck"
                events.append((a_on, 2, Msg("note_on", channel=msg.channel, note=msg.note, velocity=velo)))
                events.append((max(a_off, a_on + 1), 1, Msg("note_off", channel=msg.channel, note=msg.note)))
        events.sort(key=lambda e: (e[0], e[1]))
        msgs, now = [], 0
        for t, _o, msg in events:
            msg.time = t - now
            now = t
            msgs.append(msg)
        msgs.append(Msg("end_of_track", time=1))
        song_midi.tracks.append(msgs)
        if facts and ti < len(orch_facts):
            f = orch_facts[ti]
            facts.append(StaffFacts(part0 + f.part, f.index, f.staves, f.instrument, f.program, f.drums, f.sung))
    if facts:
        song_midi.staves = facts
    song_midi.partner = PartnerTracks(first, [(first + t, p) for t, (k, p) in sorted(keep.items()) if k == "orchestra"],
                                      [(first + t, p) for t, (k, p) in sorted(keep.items()) if k == "vocal"], rel)
    return st


def plan_report(p: Plan, pr: Optional[Pair] = None) -> dict:
    """The plan for the reports (JSON-ready)."""
    d = dict(p.stats)
    if pr is not None:
        d["own_seconds"] = round(pr.orch_own_seconds, 2)
    if p.pieces:
        d["pieces"] = [{"measures": [pr.al.orch.label(pc.first) if pr else pc.first,
                                     pr.al.orch.label(pc.last) if pr else pc.last], "at": round(pc.at, 3)}
                       for pc in p.pieces]
    return d
