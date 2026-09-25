"""Per-note play events: how long each note sounds and when it starts.

Port of MuseScore's ``CompatMidiRender::createPlayEvents`` / ``renderChord``
(src/engraving/compat/midi/compatmidirender.cpp).  Note event on/len values
are in 1/1000 of the chord's duration, exactly as in MuseScore.
"""

from __future__ import annotations

import bisect
import math
import struct
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from .cxxsort import std_sort
from .score import VOICES, Chord, Note, NoteEvent, Score, Spanner, ticks
from .tempo import TempoMap

NOTE_LENGTH = 1000
GHOST_VELOCITY_MULTIPLIER = 0.6
DIVISION = 480
_16TH = DIVISION // 4
_32ND = _16TH // 2

# ornament symbol -> list of (styles or None for any, duration, prefix, body, repeat, sustain, suffix)
EXCURSIONS = [
    ("ornamentTurn", None, _32ND, [], [1, 0, -1, 0], False, True, []),
    ("ornamentTurnInverted", None, _32ND, [], [-1, 0, 1, 0], False, True, []),
    ("ornamentTurnSlash", None, _32ND, [], [-1, 0, 1, 0], False, True, []),
    ("ornamentTrill", {"baroque"}, _32ND, [1, 0], [1, 0], True, True, []),
    ("ornamentTrill", {"default"}, _32ND, [0, 1], [0, 1], True, True, []),
    ("brassMuteClosed", {"baroque"}, _32ND, [0, -1], [0, -1], True, True, []),
    ("ornamentMordent", None, _32ND, [], [0, -1, 0], False, True, []),
    ("ornamentShortTrill", {"default"}, _32ND, [], [0, 1, 0], False, True, []),
    ("ornamentShortTrill", {"baroque"}, _32ND, [1, 0, 1], [0], False, True, []),
    ("ornamentTremblement", None, _32ND, [1, 0], [1, 0], False, True, []),
    ("brassMuteClosed", {"default"}, _32ND, [], [0], False, True, []),
    ("ornamentPrallMordent", None, _32ND, [], [1, 0, -1, 0], False, True, []),
    ("ornamentLinePrall", None, _32ND, [2, 2, 2], [1, 0], True, True, []),
    ("ornamentUpPrall", None, _16TH, [-1, 0], [1, 0], True, True, [1, 0]),
    ("ornamentUpMordent", None, _16TH, [-1, 0], [1, 0], True, True, [-1, 0]),
    ("ornamentPrecompMordentUpperPrefix", None, _16TH, [1, 1, 1, 0], [1, 0], True, True, []),
    ("ornamentDownMordent", None, _16TH, [1, 1, 1, 0], [1, 0], True, True, [-1, 0]),
    ("ornamentPrallUp", None, _16TH, [1, 0], [1, 0], True, True, [-1, 0]),
    ("ornamentPrallDown", None, _16TH, [1, 0], [1, 0], True, True, [-1, 0, 0, 0]),
    ("ornamentPrecompSlide", None, _32ND, [], [0], False, True, []),
]
EXCURSION_SYMS = {e[0] for e in EXCURSIONS}

TRILL_TYPE_SYM = {"trill": "ornamentTrill", "upprall": "ornamentUpPrall",
                  "downprall": "ornamentPrecompMordentUpperPrefix", "prallprall": "ornamentTrill"}

# Articulation symbol -> playback articulation name (Articulation::symId2ArticulationName)
ARTICULATION_NAMES = {}
for _n, _syms in {
    "staccatissimo": ["articStaccatissimoAbove", "articStaccatissimoBelow", "articStaccatissimoStrokeAbove",
                      "articStaccatissimoStrokeBelow", "articStaccatissimoWedgeAbove",
                      "articStaccatissimoWedgeBelow"],
    "staccato": ["articStaccatoAbove", "articStaccatoBelow", "tremoloDivisiDots2", "tremoloDivisiDots3",
                 "tremoloDivisiDots4", "tremoloDivisiDots6"],
    "sforzatoStaccato": ["articAccentStaccatoAbove", "articAccentStaccatoBelow"],
    "marcatoStaccato": ["articMarcatoStaccatoAbove", "articMarcatoStaccatoBelow"],
    "portato": ["articTenutoStaccatoAbove", "articTenutoStaccatoBelow"],
    "sforzatoTenuto": ["articTenutoAccentAbove", "articTenutoAccentBelow"],
    "marcatoTenuto": ["articMarcatoTenutoAbove", "articMarcatoTenutoBelow"],
    "tenuto": ["articTenutoAbove", "articTenutoBelow"],
    "marcato": ["articMarcatoAbove", "articMarcatoBelow"],
    "accent": ["articAccentAbove", "articAccentBelow"],
    "sforzato": ["dynamicSforzato", "dynamicSforzando"],
    "open": ["brassMuteOpen"],
    "closed": ["brassMuteClosed"],
    "harmonic": ["stringsHarmonic"],
    "mordent": ["ornamentMordent"],
}.items():
    for _s in _syms:
        ARTICULATION_NAMES[_s] = _n

BUILTIN_ARTICULATIONS = {
    "staccatissimo": (1.0, 30), "staccato": (1.0, 50), "portato": (1.0, 67), "tenuto": (1.0, 100),
    "accent": (1.2, 100), "marcato": (1.44, 100), "sforzato": (1.69, 100),
}


def f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def cdiv(a: int, b: int) -> int:
    """C++ integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def articulation_name(sym: str) -> str:
    return ARTICULATION_NAMES.get(sym, "---")


# ------------------------------------------------------------------------------
# pitch helpers for ornaments
# ------------------------------------------------------------------------------

_NATURAL = [0, 2, 4, 5, 7, 9, 11]


def tpc2step(tpc: int) -> int:
    return ((tpc - 14) * 4) % 7


def tpc2alter(tpc: int) -> int:
    return (tpc + 1) // 7 - 2


def tpc2pitch(tpc: int) -> int:
    return _NATURAL[tpc2step(tpc)] + tpc2alter(tpc)


def abs_step(tpc: int, pitch: int) -> int:
    line = tpc2step(tpc) + (pitch // 12) * 7
    tp = tpc2pitch(tpc)
    if tp < 0:
        line += 7
    else:
        line -= (tp // 12) * 7
    return line


def step_natural_pitch(step: int) -> int:
    return (step // 7) * 12 + _NATURAL[step % 7]


class PlayContext:
    """Score-wide lookups needed while rendering play events."""

    def __init__(self, score: Score, tempomap: TempoMap):
        self.score = score
        self.tempomap = tempomap
        self.missing_builtins: Dict[str, set] = {}
        for part in score.parts:
            for ins in part.instruments.values():
                names = {a.name for a in ins.articulations}
                for n in BUILTIN_ARTICULATIONS:
                    if n not in names:
                        # an entry exists only for instruments missing at least one built-in
                        self.missing_builtins.setdefault(ins.id, set()).add(n)
        self.slurs = [sp for sp in score.spanners if sp.tag == "Slur"]
        self.trills = [sp for sp in score.spanners if sp.tag == "Trill"]
        # per staff: slur starts sorted, with running maximum of their ends (for "is tick inside a slur")
        self._slur_index: Dict[int, Tuple[List[Fraction], List[Fraction]]] = {}
        by_staff: Dict[int, List[Tuple[Fraction, Fraction]]] = {}
        for sp in self.slurs:
            by_staff.setdefault(sp.track // VOICES, []).append((sp.tick, sp.tick2))
        for staff_idx, ivs in by_staff.items():
            ivs.sort(key=lambda x: x[0])
            starts, maxend = [], []
            cur = None
            for a, b in ivs:
                cur = b if cur is None or b > cur else cur
                starts.append(a)
                maxend.append(cur)
            self._slur_index[staff_idx] = (starts, maxend)
        self._mb_pos = {id(mb): i for i, mb in enumerate(score.measure_bases)}
        from .navigate import Navigator
        self.nav = Navigator(score)
        self.style_swing = (_swing_unit(score.style.get("swingUnit", "")), int(score.style.get("swingRatio", 60) or 60))
        self.swing_lists = build_swing_lists(score)

    # -- instrument articulation values (useDefaultArticulations == false) --
    def update_gate_time(self, instr, gate: int, name: str) -> int:
        missing = self.missing_builtins.get(instr.id)
        if missing is None:
            return gate
        if name in missing:
            return min(gate, BUILTIN_ARTICULATIONS[name][1])
        return instr.gate_time_for(gate, name)

    def velocity_multiplier(self, instr, name: str) -> float:
        missing = self.missing_builtins.get(instr.id)
        if missing is not None and name in missing:
            return BUILTIN_ARTICULATIONS[name][0]
        return instr.velocity_multiplier(name)

    def chord_velocity_multiplier(self, chord: Chord) -> float:
        instr = self.staff_of(chord).part.instrument()
        mult = 1.0
        for a in chord.articulations:
            if a.play:
                mult *= self.velocity_multiplier(instr, articulation_name(a.subtype))
        return mult

    def staff_of(self, chord: Chord):
        return self.score.staves[chord.track // VOICES]

    def tempo_at(self, tick: Fraction) -> float:
        return self.tempomap.tempo(ticks(tick)) * self.tempomap.multiplier

    def find_first_trill(self, chord: Chord) -> Optional[Spanner]:
        t = ticks(chord.tick)
        lo, hi = 1 + t, t + ticks(chord.actual_ticks()) - 1
        for sp in self.trills:
            if ticks(sp.tick) <= hi and ticks(sp.tick2) >= lo and sp.track == chord.track and _play(sp):
                return sp
        return None

    def grace_notes_merged(self, chord: Chord) -> bool:
        if self.find_first_trill(chord) is not None:
            return True
        return any(a.subtype in EXCURSION_SYMS for a in chord.articulations)

    def is_pitched(self, chord: Chord) -> bool:
        return self.staff_of(chord).group == "pitched"

    def staff_of_note(self, note: Note):
        return self.score.staves[note.chord.track // VOICES]

    def in_slur(self, staff_idx: int, tick: Fraction) -> bool:
        idx = self._slur_index.get(staff_idx)
        if not idx:
            return False
        starts, maxend = idx
        i = bisect.bisect_right(starts, tick) - 1
        return i >= 0 and maxend[i] > tick

    def anacrusis_offset(self, m) -> Fraction:
        mbs = self.score.measure_bases
        i = self._mb_pos[id(m)]
        prev = mbs[i - 1] if i > 0 else None
        return m.timesig - m.len if m.is_anacrusis(prev) else Fraction(0)

    def swing_for(self, chord: Chord):
        staff = self.staff_of(chord)
        lst = self.swing_lists.get(staff.idx)
        t = ticks(chord.tick)
        best = None
        if lst:
            for k in sorted(lst):
                if k <= t:
                    best = lst[k]
                else:
                    break
        if best is None:
            return self.style_swing
        return best

    # -- chromaticPitchSteps(noteL, noteR, steps) --
    def chromatic_pitch_steps(self, note: Note, nsteps: int, note_r: Optional[Note] = None) -> int:
        if nsteps == 0:
            return 0
        chord = note.chord
        main = chord.parent if chord.is_grace else chord
        staff = self.staff_of(main)
        if staff.group != "pitched":
            return nsteps
        step_l = abs_step(note.tpc, note.pitch)
        target = step_l + nsteps
        right = note_r if note_r is not None else note
        main_r = _main(right.chord)
        staff = self.score.staves[main_r.track // VOICES + main_r.staff_move]
        seg = main_r.segment
        measure = seg.measure
        key = key_at(staff, ticks(measure.tick))
        state = key_state(key)
        part = staff.part
        tracks = range(part.staves[0].idx * VOICES, (part.staves[-1].idx + 1) * VOICES)
        for s in measure.chordrest_segments():
            if s is seg:
                break
            for tr in tracks:
                e = s.elements.get(tr)
                if e is None or not e.is_chord:
                    continue
                for gc in e.grace_all:
                    if (gc.track // VOICES) + gc.staff_move != staff.idx:
                        continue
                    _apply_accidentals(state, gc)
                if (e.track // VOICES) + e.staff_move != staff.idx:
                    continue
                _apply_accidentals(state, e)
        alter = state.get(target, 0) if 0 <= target < 75 else 0
        nat = step_natural_pitch(max(target, 0))
        return max(0, min(127, nat)) + alter - note.pitch


def _swing_unit(name: str) -> int:
    return {"eighth": DIVISION // 2, "16th": DIVISION // 4}.get(str(name).strip(), 0)


def build_swing_lists(score: Score) -> Dict[int, Dict[int, Tuple[int, int]]]:
    """Score::updateSwing: swing settings from staff/system texts (first text at a tick wins)."""
    lists: Dict[int, Dict[int, Tuple[int, int]]] = {}
    for m in score.measures:
        for seg in m.chordrest_segments():
            for a in seg.annotations:
                el = a.props.get("_elem")
                if el is None or a.tag not in ("StaffText", "SystemText", "PlayTechAnnotation", "Text"):
                    continue
                sw = el.find("swing")
                if sw is None:
                    continue
                txt = el.find("text")
                if txt is None or not "".join(txt.itertext()) and len(txt) == 0:
                    continue
                params = (_swing_unit(sw.get("unit", "")), int(sw.get("ratio", "60") or 60))
                t = ticks(seg.tick)
                staves = range(len(score.staves)) if a.tag == "SystemText" else [a.track // VOICES]
                for si in staves:
                    lists.setdefault(si, {}).setdefault(t, params)
    return lists


def _apply_accidentals(state: Dict[int, int], chord: Chord) -> None:
    for n in chord.notes:
        if n.tie_back is not None and not n.props.get("accidental"):
            continue
        state[abs_step(n.tpc, n.pitch)] = tpc2alter(n.tpc)


def key_at(staff, tick: int) -> int:
    best = 0
    for t in sorted(staff.keys):
        if t <= tick:
            best = staff.keys[t]
    return best


def key_state(key: int) -> Dict[int, int]:
    state: Dict[int, int] = {}
    if key > 0:
        for i in range(key):
            idx = tpc2step(20 + i)
            for octave in range(0, 77, 7):
                if idx + octave < 75:
                    state[idx + octave] = 1
    elif key < 0:
        for i in range(0, key, -1):
            idx = tpc2step(12 + i)
            for octave in range(0, 77, 7):
                if idx + octave < 75:
                    state[idx + octave] = -1
    return state


def _play(sp: Spanner) -> bool:
    v = sp.props.get("play")
    return v is None or str(v).strip() not in ("0", "false")


# ------------------------------------------------------------------------------
# note helpers
# ------------------------------------------------------------------------------

def tie_for_end(n: Note) -> Optional[Note]:
    return n.tie_for.end if n.tie_for is not None else None


def total_tied_note_ticks(note: Note) -> int:
    total = note.chord.actual_ticks()
    n = note
    guard = 0
    while n.tie_for is not None and n.tie_for.end is not None and n.chord.tick < n.tie_for.end.chord.tick:
        n = n.tie_for.end
        total += n.chord.actual_ticks()
        guard += 1
        if guard > 100000:
            break
    return ticks(total)


def note_is_glissando_start(note: Note) -> bool:
    return any(sp.tag == "Glissando" and isinstance(sp.end, Note) for sp in note.spanner_for)


def back_glissando(note: Note) -> Optional[Spanner]:
    for sp in note.spanner_back:
        if sp.tag == "Glissando" and isinstance(sp.start, Note):
            return sp
    return None


# ------------------------------------------------------------------------------
# createPlayEvents
# ------------------------------------------------------------------------------

class PlayEventRenderer:
    def __init__(self, ctx: PlayContext):
        self.ctx = ctx
        self.score = ctx.score

    def create_all(self) -> None:
        score = self.score
        measures = score.measures
        for track in range(score.ntracks()):
            staff = score.staves[track // VOICES]
            if not staff.linked_primary:
                continue
            prev_chord = None
            for mi, m in enumerate(measures):
                segs = m.chordrest_segments()
                for si, seg in enumerate(segs):
                    item = seg.elements.get(track)
                    if item is None:
                        continue
                    if not item.is_chord:
                        prev_chord = None
                        continue
                    next_chord = self._next_chord(track, mi, si, item)
                    self.create_play_events(item, prev_chord, next_chord)
                    prev_chord = item

    def _next_chord(self, track: int, mi: int, si: int, chord=None) -> Optional[Chord]:
        measures = self.score.measures
        # nextChordRest(skipGrace): next chord/rest on this track anywhere later in the score
        nxt = self.ctx.nav._main_neighbour(chord, 1) if chord is not None else None
        if nxt is not None and nxt.is_chord:
            return nxt
        if mi + 1 < len(measures):
            segs = measures[mi + 1].chordrest_segments()
            if segs:
                e = segs[0].elements.get(track)
                if e is not None and e.is_chord:
                    return e
        return None

    def create_play_events(self, chord: Chord, prev_chord: Optional[Chord], next_chord: Optional[Chord]) -> None:
        ctx = self.ctx
        gate = 100
        tick = chord.tick
        staff_idx = chord.track // VOICES
        instr = ctx.staff_of(chord).part.instrument(ticks(tick))
        if not ctx.in_slur(staff_idx, tick):
            gate = instr.gate_time_for(gate, "")
        ontime, trailtime = self.create_grace_notes_play_events(chord)
        trailtime = self.adjust_trailtime(trailtime, chord, next_chord)
        unit, ratio = ctx.swing_for(chord)
        if unit and chord.tuplet is None:
            ontime, gate = self.swing_adjust(chord, unit, ratio, ontime, gate)
        el = self.render_chord(chord, gate, ontime, trailtime)
        if chord.play_event_type == "auto":
            for i, n in enumerate(chord.notes):
                n.play_events = el[i] if i < len(el) else []

    # ------------------------------------------------------------------
    def _is_subdivided(self, cr, unit: int) -> bool:
        if cr is None:
            return False
        prev = self.ctx.nav.prev_chord_rest(cr)
        return ticks(cr.actual_ticks()) < unit or (prev is not None and ticks(prev.actual_ticks()) < unit)

    def swing_adjust(self, chord: Chord, unit: int, ratio: int, ontime: int, gate: int):
        """Swing::swingAdjustParams."""
        m = chord.segment.measure
        rtick = chord.tick - m.tick + self.ctx.anacrusis_offset(m)
        swing_beat = unit * 2
        dur = float(ticks(chord.actual_ticks()))
        tick_adjust = float(swing_beat) * (float(ratio - 50) / 100.0)
        actual_adjust = (tick_adjust / dur) * 1000.0
        start = ticks(rtick)
        if start % swing_beat == unit:
            if not self._is_subdivided(chord, unit):
                ontime = int(ontime + actual_adjust)
                gate = int(200 - (gate + (actual_adjust / 10)))
        end = int(start + dur)
        if end % swing_beat == unit:
            ncr = self.ctx.nav.next_chord_rest(chord)
            if not self._is_subdivided(ncr, unit):
                gate = int(gate + actual_adjust / 10)
        return ontime, gate

    # ------------------------------------------------------------------
    def create_grace_notes_play_events(self, chord: Chord) -> Tuple[int, int]:
        ontime = 0
        trailtime = 0
        gnb = [g for g in chord.grace_before if _grace_playable(g)]
        gna = [g for g in chord.grace_after if _grace_playable(g)]   # already in playing order
        nb, na = len(gnb), len(gna)
        if nb + na == 0:
            return ontime, trailtime
        if self.ctx.grace_notes_merged(chord):
            return ontime, trailtime
        weighta = f32(f32(float(na)) / (nb + na))
        grace_duration = 0
        tps = self.ctx.tempo_at(chord.tick) * DIVISION
        chord_ms = (ticks(chord.actual_ticks()) / tps) * 1000
        drumset = self.ctx.staff_of(chord).group == "percussion" and \
            self.ctx.staff_of(chord).part.instrument().use_drumset
        if drumset:
            grace_duration = int(15 / chord_ms * 1000)
            ontime = grace_duration * nb
        elif nb:
            gc = gnb[0]
            if gc.note_type == "ACCIACCATURA":
                ontime = 0
                grace_duration = 0
                weighta = 1.0
            else:
                grace_ms = (ticks(gc.actual_ticks()) / tps) * 1000
                ontime = min(500, int((grace_ms / chord_ms) * 1000))
                grace_duration = cdiv(ontime, nb)
                weighta = 1.0
                trailtime += ontime
        on = 0
        for gc in gnb:
            if gc.play_event_type == "auto":
                for n in gc.notes:
                    n.play_events = [NoteEvent(0, on, grace_duration)]
            on += grace_duration
        if na:
            if chord.dots == 1:
                trailtime = int(math.floor(f32(667 * weighta)))
            elif chord.dots == 2:
                trailtime = int(math.floor(f32(571 * weighta)))
            else:
                trailtime = int(math.floor(f32(500 * weighta)))
            gd1 = cdiv(trailtime, na)
            on = 1000 - trailtime
            for gc in gna:
                if gc.play_event_type == "auto":
                    for n in gc.notes:
                        n.play_events = [NoteEvent(0, on, gd1)]
                on += gd1
        return ontime, trailtime

    def adjust_trailtime(self, trailtime: int, chord: Chord, next_chord: Optional[Chord]) -> int:
        if next_chord is None:
            return trailtime
        accs = [g for g in next_chord.grace_before if g.note_type == "ACCIACCATURA"]
        current = ticks(chord.duration)
        if current <= 0:
            return trailtime
        reduced = 0
        if accs:
            reduced = min(ticks(accs[0].duration), cdiv(current, 2))
        return int(trailtime + reduced / current * NOTE_LENGTH)

    # ------------------------------------------------------------------
    def render_chord(self, chord: Chord, gate: int, ontime: int, trailtime: int) -> List[List[NoteEvent]]:
        notes = chord.notes
        if not notes:
            return []
        ell: List[List[NoteEvent]] = [[] for _ in notes]
        base_len = NOTE_LENGTH - ontime - trailtime
        tremolo_len = base_len / NOTE_LENGTH
        if any(note_is_glissando_start(n) for n in notes):
            tremolo_len = 0.5 * base_len / NOTE_LENGTH
        arpeggio = False
        tremolo = False
        arp = chord.arpeggio
        if arp is not None and _arpeggio_plays(arp):
            self.render_arpeggio(chord, ell, ontime)
            arpeggio = True
        else:
            if chord.tremolo is not None:
                ontime = self.render_tremolo(chord, ell, ontime, tremolo_len)
                tremolo = True
            gate = self.render_chord_articulation(chord, ell, gate, ontime / NOTE_LENGTH, tremolo)
        for i in get_notes_indexes_to_render(self.ctx, chord):
            note = notes[i]
            el = ell[i]
            if arpeggio:
                continue
            if not el and chord.tremolo_chord_type != "second":
                el.append(NoteEvent(0, ontime, 1000 - trailtime,
                                    GHOST_VELOCITY_MULTIPLIER if note.ghost else 1.0))
                gl = back_glissando(note)
                if gl is not None and _glissando_valid(gl) and not _glissando_shift(gl):
                    el[-1].slide = True
            for e in el:
                e.len = cdiv(e.len * gate, 100)
            std_sort(el, _event_less)
        return ell

    def render_arpeggio(self, chord: Chord, ell, ontime: int) -> None:
        n = len(chord.notes)
        play_ticks = chord.up_note().play_ticks()
        l = 64
        while l and l * n > play_ticks:
            l = cdiv(2 * l, 3)
        atype = str(chord.arpeggio.props.get("subtype", "0"))
        up = atype not in ("2", "5")
        order = range(n) if up else range(n - 1, -1, -1)
        stretch = float(chord.arpeggio.props.get("timeStretch", chord.arpeggio.props.get("stretch", 1.0)) or 1.0)
        tempo_ratio = self.ctx.tempo_at(chord.tick) / 2.0
        for j, i in enumerate(order):
            events = ell[i]
            events.clear()
            ot = int(cdiv(l * j * 1000, play_ticks) * tempo_ratio * stretch)
            ot = min(max(ot + ontime, ot), 1000)
            events.append(NoteEvent(0, ot, 1000 - ot))

    def render_tremolo(self, chord: Chord, ell, ontime: int, part_of_chord: float) -> int:
        if part_of_chord == 0:
            return ontime
        trem = chord.tremolo
        sub = str(trem.props.get("subtype", ""))
        lines = tremolo_lines(sub)
        if sub == "buzzroll":
            return ontime
        ctype = chord.tremolo_chord_type
        notes = chord.notes
        if ctype == "first":
            t = cdiv(DIVISION, 1 << (lines + chord.hooks))
            if t == 0:
                t = 1
            seg = chord.segment
            m = seg.measure
            segs = m.chordrest_segments()
            idx = segs.index(seg)
            c2 = None
            for s2 in segs[idx + 1:]:
                e = s2.elements.get(chord.track)
                if e is not None:
                    c2 = e
                    break
            if c2 is None or not c2.is_chord:
                return ontime
            n2 = len(c2.notes)
            tnotes = max(len(notes), n2)
            tticks = ticks(chord.duration) * 2
            n = cdiv(tticks, t)
            n = cdiv(n, 2)
            l = cdiv(2000 * t, tticks)
            for k in range(tnotes):
                if k < len(notes):
                    events = ell[k]
                    events.clear()
                else:
                    events = ell[0]
                if k < len(notes) and k < n2:
                    dp = c2.notes[k].pitch - notes[k].pitch
                    for i in range(n):
                        events.append(NoteEvent(0, l * i * 2, l))
                        events.append(NoteEvent(dp, l * i * 2 + l, l))
                elif k < len(notes):
                    for i in range(n):
                        events.append(NoteEvent(0, l * i * 2, l))
                else:
                    dp = c2.notes[k].pitch - notes[0].pitch
                    for i in range(n):
                        events.append(NoteEvent(dp, l * i * 2 + l, l))
        elif ctype == "second":
            for k in range(len(notes)):
                ell[k].clear()
        elif ctype == "single":
            t = cdiv(DIVISION, 1 << (lines + chord.hooks))
            if t == 0:
                t = 1
            size = int(cdiv(ticks(chord.duration), t) * part_of_chord)
            trem_time = int(1000 * part_of_chord)
            step = cdiv(trem_time, size) if size else 0
            ontime += trem_time
            for k in range(len(notes)):
                events = ell[k]
                events.clear()
                for i in range(size):
                    events.append(NoteEvent(0, step * i, step))
        return ontime

    def render_chord_articulation(self, chord: Chord, ell, gate: int, grace_on_beat: float, tremolo_before: bool) -> int:
        ctx = self.ctx
        instr = ctx.staff_of(chord).part.instrument(ticks(chord.tick))
        for k, note in enumerate(chord.notes):
            events = ell[k]
            trill = None
            if note_is_glissando_start(note):
                self.render_glissando(events, note, grace_on_beat, tremolo_before)
            elif ctx.is_pitched(chord) and (trill := ctx.find_first_trill(chord)) is not None:
                sym = TRILL_TYPE_SYM.get(str(trill.props.get("subtype", "trill")))
                if sym is not None:
                    self.render_ornament(events, note, sym, str(trill.props.get("ornamentStyle", "default")))
            else:
                for a in chord.articulations:
                    if not a.play:
                        continue
                    if not self.render_ornament(events, note, a.subtype, a.ornament_style):
                        gate = ctx.update_gate_time(instr, gate, articulation_name(a.subtype))
        return gate

    def render_ornament(self, events, note: Note, sym: str, style: str) -> bool:
        if not self.ctx.is_pitched(note.chord if not note.chord.is_grace else note.chord.parent):
            return False
        style = "baroque" if style in ("baroque", "1") else "default"
        for (asym, styles, duration, prefix, body, repeatp, sustainp, suffix) in EXCURSIONS:
            if asym == sym and (styles is None or style in styles):
                return self.render_note_articulation(events, note, False, duration, prefix, body, repeatp,
                                                     sustainp, suffix)
        return False

    def render_glissando(self, events, note: Note, grace_on_beat: float, tremolo_before: bool) -> None:
        for sp in note.spanner_for:
            if sp.tag != "Glissando" or not _play(sp):
                continue
            body = self.glissando_pitch_steps(sp)
            if body:
                self.render_note_articulation(events, note, True, DIVISION, [], body, False, True, [], 16, 0,
                                              grace_on_beat, tremolo_before)

    def glissando_pitch_steps(self, sp: Spanner) -> List[int]:
        start, end = sp.start, sp.end
        if not isinstance(end, Note):
            return []
        style = str(sp.props.get("glissandoStyle", "chromatic")).lower()
        if style == "portamento":
            return []
        p0 = start.pitch + self.ctx.staff_of_note(start).pitch_offset(ticks(_main(start.chord).tick))
        p1 = end.pitch + self.ctx.staff_of_note(end).pitch_offset(ticks(_main(end.chord).tick))
        if p0 == p1:
            return []
        direction = 1 if p1 > p0 else -1
        out: List[int] = []
        if style == "diatonic":
            k = 0
            while True:
                half = self.ctx.chromatic_pitch_steps(start, k * direction, end)
                pitch = p0 + half
                if (direction == 1 and pitch < p1) or (direction == -1 and pitch > p1):
                    out.append(half)
                else:
                    break
                k += 1
            return out
        if style == "chromatic":
            return [p - p0 for p in range(p0, p1, direction)]
        white = [True, False, True, False, True, True, False, True, False, True, False, True]
        pick = style == "whitekeys"
        return [p - p0 for p in range(p0, p1, direction) if white[((p - 60) + 1200) % 12] == pick]

    def render_note_articulation(self, events, note: Note, chromatic: bool, requested: int, prefix, body,
                                 repeatp: bool, sustainp: bool, suffix, fastest: int = 64, slowest: int = 8,
                                 grace_on_beat: float = 0.0, tremolo_before: bool = False) -> bool:
        if not tremolo_before:
            events.clear()
        chord = note.chord
        maxticks = total_tied_note_ticks(note)
        space = 1000 * maxticks
        numrepeat = 1
        sustain = 0
        ontime = 0
        gnb = 0 if note_is_glissando_start(note) else len(chord.grace_before)
        p, b, s = len(prefix), len(body), len(suffix)
        gna = len(chord.grace_after)
        if gnb + p + b + s + gna <= 0:
            return False
        tempo = self.ctx.tempo_at(chord.tick)
        tps = int(tempo * DIVISION)
        min_tpn = cdiv(tps, fastest)
        max_tpn = 0 if slowest == 0 else cdiv(tps, slowest)
        tpn = max(requested, min_tpn)
        if slowest <= 0:
            pass
        elif tpn <= max_tpn:
            pass
        else:
            tpn = requested
            while tpn > max_tpn:
                tpn = cdiv(tpn, 2)
            if tpn < min_tpn:
                tpn = min_tpn
        total = gnb + p + b + s + gna
        if tpn * total <= maxticks:
            pass
        elif tpn == min_tpn:
            return False
        else:
            tpn = cdiv(maxticks, total)
            if slowest > 0 and tpn < min_tpn:
                return False
        mpn = cdiv(space * tpn, maxticks)
        chord_ticks = ticks(chord.actual_ticks())
        ghost = note.ghost

        def make_event(pitch: int, on: int, duration: int, play: bool = True) -> int:
            vm = 1.0 * (GHOST_VELOCITY_MULTIPLIER if ghost else 1.0)
            steps = pitch if chromatic else self.ctx.chromatic_pitch_steps(note, pitch)
            events.append(NoteEvent(steps, cdiv(on, chord_ticks), cdiv(duration, chord_ticks), vm, play))
            return on + duration

        def tie_forward(j: int, vec) -> Tuple[int, int]:
            duration = mpn
            while j < len(vec) - 1 and vec[j] == vec[j + 1]:
                duration += mpn
                j += 1
            return duration, j

        if repeatp:
            numrepeat = cdiv(space - mpn * (gnb + p + s + gna), mpn * b)
        if sustainp:
            sustain = space - mpn * (gnb + p + numrepeat * b + s + gna)
        j = 0
        while j < p:
            d, j = tie_forward(j, prefix)
            ontime = make_event(prefix[j], ontime, d)
            j += 1
        if b > 0:
            state = "none"
            on_times: List[int] = []
            for sp in note.spanner_for:
                if sp.tag != "Glissando":
                    continue
                if not _glissando_valid(sp):
                    state = "invalid"
                    break
                ease_in = float(sp.props.get("easeInSpin", 0) or 0) / 100.0
                ease_out = float(sp.props.get("easeOutSpin", 0) or 0) / 100.0
                total = mpn * b
                if tremolo_before:
                    gdur = int(total * 0.33)
                else:
                    gdur = int(total * (1 - grace_on_beat) * 0.33)
                on_times = ease_time_list(ease_in, ease_out, b - 1, gdur)
                if on_times:
                    on_times[0] = int(on_times[0] + grace_on_beat * total)
                if len(on_times) > 1:
                    off = on_times[1]
                    for i in range(1, len(on_times)):
                        on_times[i] += total - gdur - off
                state = "valid"
                break
            if state == "valid":
                if len(on_times) > 1:
                    make_event(body[0], on_times[0], on_times[1] - on_times[0], play=note.tie_back is None)
                    gl = back_glissando(note)
                    if gl is not None and _glissando_valid(gl) and not _glissando_shift(gl):
                        events[-1].slide = True
                for jj in range(1, b - 1):
                    make_event(body[jj], on_times[jj], on_times[jj + 1] - on_times[jj])
                    events[-1].slide = True
                if b > 1:
                    make_event(body[b - 1], on_times[b - 1], (mpn * b - on_times[b - 1]) + sustain)
                    events[-1].slide = True
            elif state != "invalid":
                for _r in range(numrepeat - 1):
                    for jj in range(b):
                        ontime = make_event(body[jj], ontime, mpn)
                for jj in range(b - 1):
                    ontime = make_event(body[jj], ontime, mpn)
                ontime = make_event(body[b - 1], ontime, mpn + sustain)
        j = 0
        while j < s:
            d, j = tie_forward(j, suffix)
            ontime = make_event(suffix[j], ontime, d)
            j += 1
        return True


def ease_time_list(ease_in: float, ease_out: float, nb_notes: int, duration: int) -> List[int]:
    """EaseInOut::timeList (pushes nb_notes + 1 values)."""
    def lround(x: float) -> int:
        return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))

    def eval_x(t: float) -> float:
        tc = 1.0 - t
        return (3.0 * ease_in * tc * tc + (3.0 - 3.0 * ease_out * tc - 2.0 * t) * t) * t

    def t_from_y(y: float) -> float:
        return 0.5 + math.cos((4.0 * math.pi + math.acos(1.0 - 2.0 * y)) / 3.0)

    n_notes = float(nb_notes)
    space = float(duration)
    out = []
    for n in range(nb_notes + 1):
        y = n / n_notes if n_notes else 0.0
        if abs(ease_in) <= 1e-9 and abs(ease_out) <= 1e-9:
            out.append(lround(y * space))
        else:
            out.append(lround(eval_x(t_from_y(y)) * space))
    return out


def _main(chord: Chord) -> Chord:
    return chord.parent if chord.is_grace else chord


def tremolo_lines(sub: str) -> int:
    sub = sub.lower()
    for key, lines in (("8", 1), ("16", 2), ("32", 3), ("64", 4)):
        if sub.endswith(key):
            return lines
    return {"0": 1, "1": 2, "2": 3, "3": 4, "4": 1, "5": 2, "6": 3, "7": 4}.get(sub, 0)


def _grace_playable(gc: Chord) -> bool:
    """Chord::isChordPlayable: decided by the first (lowest) note's play flag."""
    if gc.notes:
        return gc.notes[0].play
    return False


def _arpeggio_plays(arp) -> bool:
    v = arp.props.get("play")
    return v is None or str(v).strip() not in ("0", "false")


def _glissando_valid(gl: Spanner) -> bool:
    s, e = gl.start, gl.end
    if not isinstance(s, Note) or not isinstance(e, Note):
        return False
    return s.props.get("string", -1) == e.props.get("string", -1)


def _glissando_shift(gl: Spanner) -> bool:
    return str(gl.props.get("glissandoShift", "0")) in ("1", "true")


def _event_less(l: NoteEvent, r: NoteEvent) -> bool:
    return (l.ontime, -l.offset) < (r.ontime, -r.offset)


def get_notes_indexes_to_render(ctx: PlayContext, chord: Chord) -> List[int]:
    longest: Dict[int, int] = {}
    pts = [n.play_ticks() for n in chord.notes]
    for n, pt in zip(chord.notes, pts):
        longest[n.pitch] = max(longest.get(n.pitch, 0), pt)

    def should_render(n: Note) -> bool:
        guard = 0
        while n.tie_back is not None and n.tie_back.start is not None and n is not n.tie_back.start:
            n = n.tie_back.start
            if ctx.find_first_trill(n.chord) is not None:
                return False
            for a in n.chord.articulations:
                if a.is_ornament:
                    return False
            guard += 1
            if guard > 100000:
                break
        return True

    return [i for i, (n, pt) in enumerate(zip(chord.notes, pts)) if should_render(n) and longest[n.pitch] == pt]
