"""Render a :class:`Score` to MIDI-equivalent events, exactly like MuseScore's
"Export MIDI" (CompatMidiRendererInternal + ExportMidi), without MuseScore.
"""

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from dataclasses import dataclass

from .playevents import PlayContext, PlayEventRenderer, cdiv, NOTE_LENGTH
from .repeats import build_repeat_list
from .score import (SEG_BREATH, SEG_CHORDREST, SEG_TIMETICK, VOICES, Chord, Element, Measure, Note, Score,
                    Spanner, ticks, from_ticks)
from .smf import MidiFile, Msg
from .tempo import DEFAULT_TEMPO, PauseMap, SigEvent, TempoMap, TimeSigMap
from .velocity import DECREASING, INCREASING, VelocityMap

ME_NOTEON = "on"
ME_CONTROLLER = "ctrl"
ME_PITCHBEND = "pb"

CTRL_SUSTAIN = 64
CTRL_BREATH = 2
CTRL_RESET_ALL = 121
CTRL_LRPN, CTRL_HRPN, CTRL_HDATA = 100, 101, 6
CTRL_PROGRAM = "program"

FERMATA_STRETCH = {
    "fermataVeryShort": 1.25, "fermataShort": 1.5, "fermataShortHenze": 1.5, "fermataLong": 3.0,
    "fermataLongHenze": 3.0, "fermataVeryLong": 4.0,
}

DYNAMIC_VELOCITY = {
    "pppppp": (1, 0), "ppppp": (5, 0), "pppp": (10, 0), "ppp": (16, 0), "pp": (33, 0), "p": (49, 0),
    "mp": (64, 0), "mf": (80, 0), "f": (96, 0), "ff": (112, 0), "fff": (126, 0), "ffff": (127, 0),
    "fffff": (127, 0), "ffffff": (127, 0), "fp": (96, -47), "pf": (49, 47), "sf": (112, -18),
    "sfz": (112, -18), "sff": (126, -18), "sffz": (126, -18), "sfff": (127, -18), "sfffz": (127, -18),
    "sfp": (112, -47), "sfpp": (112, -79), "rfz": (112, -18), "rf": (112, -18), "fz": (112, -18),
    "m": (96, -16), "r": (112, -18), "s": (112, -18), "z": (80, 0), "n": (49, -48),
}


class NPlayEvent:
    __slots__ = ("type", "channel", "pitch", "velo", "controller", "value", "orig_staff", "discard", "note",
                 "data_a", "data_b")

    def __init__(self, etype, channel, pitch=0, velo=0, controller=0, value=0, orig_staff=0):
        self.type = etype
        self.channel = channel
        self.pitch = pitch
        self.velo = velo
        self.controller = controller
        self.value = value
        self.orig_staff = orig_staff
        self.discard = 0
        self.note = None
        self.data_a = 0
        self.data_b = 0

    def copy(self) -> "NPlayEvent":
        e = NPlayEvent(self.type, self.channel, self.pitch, self.velo, self.controller, self.value, self.orig_staff)
        e.discard, e.note = self.discard, self.note
        return e


class EventsHolder:
    """Per-channel multimaps (tick -> event) with insertion order inside a tick."""

    def __init__(self):
        self.channels: List[List[Tuple[int, int, NPlayEvent]]] = []
        self.seq = 0
        self._sorted: List[bool] = []

    def add(self, channel: int, tick: int, ev: NPlayEvent) -> None:
        while channel >= len(self.channels):
            self.channels.append([])
            self._sorted.append(True)
        self.channels[channel].append((tick, self.seq, ev))
        self.seq += 1
        self._sorted[channel] = False

    def items(self, channel: int):
        lst = self.channels[channel]
        if not self._sorted[channel]:
            lst.sort(key=lambda x: (x[0], x[1]))
            self._sorted[channel] = True
        return lst

    def __len__(self) -> int:
        return len(self.channels)


def _play(el) -> bool:
    v = el.props.get("play") if hasattr(el, "props") else None
    return v is None or str(v).strip() not in ("0", "false")


def _float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------------------
# tempo / time signature maps  (Score::setUpTempoMap)
# ------------------------------------------------------------------------------

def fermata_stretch(f: Element) -> float:
    if "timeStretch" in f.props:
        return _float(f.props["timeStretch"], 2.0)
    sub = str(f.props.get("subtype", "fermataAbove"))
    for suffix in ("Above", "Below"):
        if sub.endswith(suffix):
            sub = sub[:-len(suffix)]
    return FERMATA_STRETCH.get(sub, 2.0)


def breath_pause(b: Element) -> float:
    return _float(b.props.get("pause"), 0.0)


def section_pause(score: Score, mb) -> float:
    if mb.section_break is None:
        return 0.0
    p = mb.section_break.props.get("pause")
    if p is not None:
        return float(p)
    return _float(score.style.get("sectionPause"), 3.0)


def tempo_value(tt: Element, from_text: bool = False) -> float:
    """TempoText::tempo(): stored value, clamped to 5..999 bpm.

    With ``from_text`` the tempo is recomputed from its text, as MuseScore does
    after the text is edited in an open session."""
    if from_text:
        v = tempo_from_text(tt)
        if v is not None:
            return v
    v = _float(tt.props.get("tempo"), DEFAULT_TEMPO)
    return min(max(v, 5.0 / 60), 999.0 / 60)


_MET_SYMS = {
    "metNoteDoubleWhole": "\uECA0", "metNoteDoubleWholeSquare": "\uECA1", "metNoteWhole": "\uECA2",
    "metNoteHalfUp": "\uECA3", "metNoteQuarterUp": "\uECA5", "metNote8thUp": "\uECA7",
    "metNote16thUp": "\uECA9", "metNote32ndUp": "\uECAB", "metNote64thUp": "\uECAD",
    "metNote128thUp": "\uECAF", "metNote256thUp": "\uECB1", "metNote512thUp": "\uECB3",
    "metNote1024thUp": "\uECB5", "metAugmentationDot": "\uECB7", "space": " ",
}
# TempoText tp[] patterns, longest first, with beats-per-second factors
_TEMPO_PATTERNS = [
    ("\uECA5\\s*\uECB7\\s*\uECB7", 1.75 / 60.0), ("\uECA5\\s*\uECB7", 1.5 / 60.0), ("\uECA5", 1.0 / 60.0),
    ("\uECA3\\s*\uECB7\\s*\uECB7", 1.75 / 30.0), ("\uECA3\\s*\uECB7", 1.5 / 30.0), ("\uECA3", 1.0 / 30.0),
    ("\uECA7\\s*\uECB7\\s*\uECB7", 1.75 / 120.0), ("\uECA7\\s*\uECB7", 1.5 / 120.0), ("\uECA7", 1.0 / 120.0),
    ("\uECA2\\s*\uECB7", 1.5 / 15.0), ("\uECA2", 1.0 / 15.0),
    ("\uECA9\\s*\uECB7", 1.5 / 240.0), ("\uECA9", 1.0 / 240.0),
    ("\uECAB\\s*\uECB7", 1.5 / 480.0), ("\uECAB", 1.0 / 480.0),
    ("\uECA1", 1.0 / 7.5), ("\uECA0", 1.0 / 7.5), ("\uECAD", 1.0 / 960.0), ("\uECAF", 1.0 / 1920.0),
    ("\uECB1", 1.0 / 3840.0), ("\uECB3", 1.0 / 7680.0), ("\uECB5", 1.0 / 15360.0),
]


def tempo_from_text(tt: Element) -> Optional[float]:
    """TempoText::updateTempo(): beats per second parsed from the displayed text."""
    import re
    el = tt.props.get("_elem")
    if el is None:
        return None
    txt = el.find("text")
    if txt is None:
        return None
    raw = ET.tostring(txt, encoding="unicode")
    raw = re.sub(r"^<text[^>]*>|</text>$", "", raw)
    raw = re.sub(r"<sym>([^<]*)</sym>", lambda m: _MET_SYMS.get(m.group(1), ""), raw)
    plain = re.sub(r"<[^>]+>", "", raw)
    plain = plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    for a, b in ((",", "."), ("\u2252", "="), ("\u2248", "="), ("~", "="), ("ca.", ""), ("c.", ""), ("approx.", "")):
        plain = plain.replace(a, b)
    for pat, f in _TEMPO_PATTERNS:
        m = re.search(pat + r"\s*=\s*(\d+[.]{0,1}\d*)\s*", plain)
        if m:
            return min(max(float(m.group(1)) * f, 5.0 / 60), 999.0 / 60)
    return None


def tempo_type(tt: Element) -> str:
    t = str(tt.props.get("type", "normal")).strip()
    return t if t in ("aTempo", "tempoPrimo") else "normal"


def follow_text(tt: Element) -> bool:
    return str(tt.props.get("followText", "0")).strip() not in ("", "0")


@dataclass
class Compat:
    """How to reproduce a particular MuseScore export.

    dynamics      "4.6" (per-voice assignment) or "4.5" (per-staff DynamicRange)
    tempo_text    recompute text-following tempos from their text (GUI-session exports)
    text_tempo_ticks  score ticks of individual tempo markings recomputed from their text
    metadata      "v2" (default) or "legacy" title/composer rules (metadata.extract_title_artist)
    tempo_model   "auto" (command-line export rules), "read" or "layout" (post-layout tempo rebuild)
    volta_tempo   None = follow tempo_model; True/False forces Volta::setTempo entries
    """
    dynamics: str = "4.6"
    tempo_text: bool = False
    tempo_model: str = "auto"
    volta_tempo: Optional[bool] = None
    text_tempo_ticks: Tuple[int, ...] = ()
    metadata: str = "v2"

    def key(self) -> str:
        k = f"dyn={self.dynamics},text={int(self.tempo_text)},model={self.tempo_model},volta={self.volta_tempo}"
        if self.text_tempo_ticks:
            k += ",ticks=" + ":".join(str(t) for t in self.text_tempo_ticks)
        if self.metadata != "v2":
            k += f",meta={self.metadata}"
        return k

    def to_dict(self) -> dict:
        d = {}
        if self.dynamics != "4.6":
            d["dynamics"] = self.dynamics
        if self.tempo_text:
            d["tempo_text"] = True
        if self.tempo_model != "auto":
            d["tempo_model"] = self.tempo_model
        if self.volta_tempo is not None:
            d["volta_tempo"] = self.volta_tempo
        if self.text_tempo_ticks:
            d["text_tempo_ticks"] = list(self.text_tempo_ticks)
        if self.metadata != "v2":
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Compat":
        d = dict(d or {})
        if "text_tempo_ticks" in d:
            d["text_tempo_ticks"] = tuple(d["text_tempo_ticks"])
        return cls(**d)

    def uses_text(self, tt: Element, tick: int) -> bool:
        return (self.tempo_text and follow_text(tt)) or tick in self.text_tempo_ticks


def needs_post_layout_rebuild(score: Score) -> bool:
    """Whether MuseScore rebuilds the tempo map after layout (which drops Volta tempo entries).

    Triggered by any gradual tempo change line (its layout segment requests a rebuild) or by
    multi-measure rests.  Courtesy time signatures created by layout are not modelled.
    """
    if any(sp.tag == "GradualTempoChange" for sp in score.spanners):
        return True
    return str(score.style.get("createMultiMeasureRests", "0")).strip() == "1"


def build_maps(score: Score, compat: Optional[Compat] = None) -> Tuple[TempoMap, TimeSigMap]:
    compat = compat or Compat()
    if compat.tempo_model == "auto":
        laid_out = needs_post_layout_rebuild(score)
    else:
        laid_out = compat.tempo_model == "layout"
    volta_entries = (not laid_out) if compat.volta_tempo is None else compat.volta_tempo
    from_text = compat.uses_text
    tempomap = TempoMap()
    sigmap = TimeSigMap()
    measures = score.measures
    if not measures:
        tempomap.set_tempo(0, DEFAULT_TEMPO)
        return tempomap, sigmap
    fm = measures[0]
    sigmap.add(0, SigEvent(fm.len, fm.timesig, 0, fm.len_nd))
    tempo_primo = [None]
    mbs = score.measure_bases
    anacrusis = []
    for mb_i, mb in enumerate(mbs):
        if not mb.is_measure:
            continue
        m: Measure = mb
        if m.is_anacrusis(mbs[mb_i - 1] if mb_i > 0 else None):
            anacrusis.append(m)
        _rebuild_measure(score, m, mb_i, tempomap, sigmap, tempo_primo, laid_out, from_text)

    for sp in score.spanners:
        if sp.tag != "GradualTempoChange" or not _play(sp):
            continue
        _apply_gradual_tempo(sp, tempomap)
    if not tempomap.keys:
        tempomap.set_tempo(0, DEFAULT_TEMPO)
    for m in anacrusis:
        if _tempo_text_in(m) is not None:
            continue
        nm = measures[m.index + 1] if m.index + 1 < len(measures) else None
        if nm is not None:
            tt, tt_tick = _tempo_text_in(nm, with_tick=True)
            if tt is not None:
                tempomap.set_tempo(ticks(m.tick), tempo_value(tt, from_text(tt, tt_tick)))
    # layout: Volta::setTempo restores the pre-volta tempo at the end of a repeated ending
    # (lost again if the tempo map is rebuilt after layout)
    from .repeats import volta_measures
    for sp in (score.spanners if volta_entries else []):
        if sp.tag != "Volta":
            continue
        sm, em = volta_measures(score, sp)
        if sm is None or em is None or not em.repeat_end:
            continue
        before = tempomap.tempo(ticks(sm.tick) - 1)
        tempomap.set_tempo(ticks(em.end_tick) - 1, before)
    return tempomap, sigmap


def _tempo_text_in(m: Measure, with_tick: bool = False):
    for s in m.segments:
        if s.type == SEG_CHORDREST:
            for e in s.annotations:
                if e.tag == "Tempo" and _play(e):
                    return (e, ticks(s.tick)) if with_tick else e
    return (None, 0) if with_tick else None


def _all_segments_before(score: Score, m: Measure):
    """Segments of the previous measure, last first (for Segment::prev1)."""
    if m.index > 0:
        return list(reversed(score.measures[m.index - 1].segments))
    return []


def _rebuild_measure(score: Score, m: Measure, mb_i: int, tempomap: TempoMap, sigmap: TimeSigMap, tempo_primo,
                     laid_out: bool = False, from_text=lambda tt, tick: False) -> None:
    start = m.tick
    t1, t2 = ticks(start), ticks(m.end_tick)
    zero_in = start <= 0 < m.end_tick
    tempomap.clear_range(t1, t2)
    if zero_in:
        tempomap.set_tempo(0, DEFAULT_TEMPO)
    sigmap.erase_range(t1, t2)
    if zero_in:
        fm = score.measures[0]
        sigmap.add(0, SigEvent(fm.len, fm.timesig, 0, fm.len_nd))

    # section break pause from preceding measure bases ending here
    k = mb_i - 1
    mbs = score.measure_bases
    while k >= 0:
        pmb = mbs[k]
        pend = pmb.end_tick if pmb.is_measure else pmb.tick
        if pend != start:
            break
        p = section_pause(score, pmb)
        if p:
            tempomap.set_pause(t1, p)
        k -= 1

    # breath marks at the very end of the previous measure (Segment::prev1 from the first segment)
    cand = [s for s in m.segments if s.tick == start]
    if cand:
        first = m.segments[0]
        chain = [first] if first.tick == start else []
        if first.tick == start:
            chain += [s for s in _all_segments_before(score, m) if s.tick == start]
        for s in chain:
            if s.type != SEG_BREATH:
                continue
            length = max((breath_pause(b) for b in s.breaths.values()), default=0.0)
            if length != 0.0:
                tempomap.set_pause(t1, length)

    segs = m.segments
    for si, seg in enumerate(segs):
        if seg.type == SEG_BREATH:
            length = 0.0
            for tr in range(score.ntracks()):
                b = seg.breaths.get(tr)
                if b is not None:
                    length = max(length, breath_pause(b))
            if length != 0.0:
                tempomap.set_pause(ticks(seg.tick), length)
        elif seg.type in (SEG_CHORDREST, SEG_TIMETICK):
            stretch = 0.0
            for e in seg.annotations:
                if e.tag == "Fermata" and _play(e):
                    stretch = max(stretch, fermata_stretch(e))
                elif e.tag == "Tempo":
                    if not _play(e):
                        continue
                    tv = tempo_value(e, from_text(e, ticks(seg.tick)))
                    ttype = tempo_type(e)
                    if ttype == "normal" and tempo_primo[0] is None:
                        tempo_primo[0] = tv
                    st = ticks(seg.tick)
                    if ttype == "aTempo" and follow_text(e):
                        tempomap.set_tempo(st, tempomap.tempo(st))
                    elif ttype == "tempoPrimo" and follow_text(e):
                        tempomap.set_tempo(st, tempo_primo[0] if tempo_primo[0] is not None else DEFAULT_TEMPO)
                    else:
                        tempomap.set_tempo(st, tv)
            if stretch != 0.0 and stretch != 1.0:
                st = ticks(seg.tick)
                otempo = tempomap.tempo(st)
                tempomap.set_tempo(st, otempo / stretch)
                nxt = _next1_active(score, m, si)
                if nxt is not None:
                    end_tick = nxt.tick
                elif laid_out:
                    end_tick = m.end_tick          # every measure has an EndBarLine after layout
                else:
                    end_tick = seg.tick            # Segment::ticks() is still 0 at read time
                etick = ticks(end_tick - Fraction(1, 1920))
                if etick not in tempomap:
                    tempomap.set_tempo(etick, otempo)

    pm = score.measures[m.index - 1] if m.index > 0 else None
    if pm is not None and (m.len_nd != pm.len_nd or m.timesig_nd != pm.timesig_nd):
        sigmap.add(ticks(m.tick), SigEvent(m.len, m.timesig, m.index, m.len_nd))


def _next1_active(score: Score, m: Measure, si: int):
    segs = m.segments
    for s in segs[si + 1:]:
        if _segment_active(score, s):
            return s
    for nm in score.measures[m.index + 1:]:
        for s in nm.segments:
            if _segment_active(score, s):
                return s
    return None


def _segment_active(score: Score, s) -> bool:
    """Segment::isActive(): not a TimeTick segment, and visible after layout
    (at least one element on a staff that is shown in its system)."""
    if s.type == SEG_TIMETICK:
        return False
    if s.type not in (SEG_CHORDREST, SEG_BREATH):
        return True               # barline/header segments hold an element on every staff
    shown = _shown_staves(score, s.measure)
    if shown is None:
        return True
    for e in s.elements.values():
        if (e.track // VOICES + getattr(e, "staff_move", 0)) in shown:
            return True
    for e in s.breaths.values():
        if e.track // VOICES in shown:
            return True
    for e in s.annotations:
        if e.track // VOICES in shown:
            return True
    return False


_SYSTEM_FLAG_TAGS = {"Tempo", "RehearsalMark", "SystemText", "Marker", "Jump"}


def _measure_staff_empty(score: Score, m: Measure, staff_idx: int) -> bool:
    """Measure::isEmpty(staffIdx)."""
    strack, etrack = staff_idx * VOICES, staff_idx * VOICES + VOICES
    nstaves = len(score.staves)
    for seg in m.segments:
        if seg.type != SEG_CHORDREST:
            continue
        for track in range(strack, etrack):
            e = seg.elements.get(track)
            if e is not None and e.tag != "Rest":
                return False
            if len(score.staves[staff_idx].part.staves) > 1:
                for other in (track - VOICES, track + VOICES):
                    if 0 <= other < nstaves * VOICES:
                        e = seg.elements.get(other)
                        if e is not None and e.tag != "Rest" and \
                                other // VOICES + getattr(e, "staff_move", 0) == staff_idx:
                            return False
        for a in seg.annotations:
            if a.tag in _SYSTEM_FLAG_TAGS or not a.props.get("visible", True) or a.tag == "Fermata":
                continue
            if strack <= a.track < etrack:
                return False
    return True


def _shown_staves(score: Score, m: Measure):
    """Staves shown in the system containing ``m`` (SystemLayout::hideEmptyStaves).

    System breaks are not known without a full layout, so each measure is
    treated as its own system. Returns None when nothing can be hidden."""
    cache = score.__dict__.setdefault("_shown_cache", {})
    if m.index in cache:
        return cache[m.index]
    style = score.style
    global_hide = style.get("hideEmptyStaves", "0") == "1" and not (
        m.index == 0 and style.get("dontHideStavesInFirstSystem", "1") == "1")
    staves = score.staves
    modes = []
    hideable = False
    for st in staves:
        if not st.part.show:
            mode = "hidden"
        elif st.hide_when_empty == "on":
            mode = "staff"
        elif st.hide_when_empty == "off" or st.part.hide_when_empty == "off" or \
                (st.part.hide_when_empty == "auto" and not global_hide):
            mode = "show"
        else:
            mode = "staff" if st.part.hide_staves_individually else "instrument"
        modes.append(mode)
        hideable |= mode != "show"
    if not hideable or len(staves) == 1:
        cache[m.index] = None
        return None
    empty = [_measure_staff_empty(score, m, i) for i in range(len(staves))]
    stick, etick = m.tick, m.end_tick
    shown = set()
    for i, st in enumerate(staves):
        mode = modes[i]
        if mode == "hidden":
            continue
        if mode == "show" or not empty[i]:
            shown.add(i)
            continue
        if any(sp.track // VOICES == i and sp.tag not in _SYSTEM_FLAG_TAGS
               and sp.tick < etick and sp.tick2 >= stick
               and not (sp.tick2 == stick and sp.tag != "Slur") for sp in score.spanners):
            shown.add(i)
            continue
        if mode == "instrument" and any(not empty[o.idx] for o in st.part.staves):
            shown.add(i)
    if not shown:
        first = next((i for i, mo in enumerate(modes) if mo != "hidden"), 0)
        shown.add(first)
    cache[m.index] = shown
    return shown


def _segment_ticks(score: Score, m: Measure, si: int) -> Fraction:
    segs = m.segments
    for s in segs[si + 1:]:
        if s.tick > segs[si].tick:
            return s.tick - segs[si].tick
    return m.end_tick - segs[si].tick


def _f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def _easing_factor(x: float, method: str) -> float:
    """easingFactor(): float32 in and out, double math inside."""
    if method == "ease-in":
        r = 1 - math.sqrt(1 - math.pow(x, 2))
    elif method == "ease-out":
        r = math.sqrt(1 - math.pow(_f32(x - 1), 2))
    elif method == "ease-in-out":
        if x < 0.5:
            r = (1.0 - math.sqrt(1 - math.pow(_f32(2 * x), 2))) / 2
        else:
            r = (math.sqrt(1.0 - math.pow(_f32(-2 * x + 2), 2)) + 1) / 2
    elif method == "exponential":
        if abs(x - 1.0) * 1e9 <= min(abs(x), 1.0):
            r = x
        else:
            r = 1.0 - math.pow(2, _f32(-10 * x))
    else:
        r = x
    return _f32(r)


def easing_value_curve(total_ticks: int, steps: int, amplitude: float, method: str) -> Dict[int, float]:
    """TConv::easingValueCurve / buildEasedValueCurve."""
    curve: Dict[int, float] = {}
    if steps <= 0:
        return curve
    step = _f32(_f32(float(total_ticks)) / _f32(float(steps)))
    for i in range(steps + 1):
        key = int(_f32(i * step))
        x = _f32(_f32(float(i)) / _f32(float(steps)))
        if key not in curve:
            curve[key] = _easing_factor(x, method) * amplitude
    return curve


GRADUAL_FACTORS = {
    "accelerando": 1.33, "allargando": 0.75, "calando": 0.5, "lentando": 0.75, "morendo": 0.5,
    "precipitando": 1.15, "rallentando": 0.75, "ritardando": 0.75, "smorzando": 0.5, "sostenuto": 0.95,
    "stringendo": 1.5,
}


def _apply_gradual_tempo(sp: Spanner, tempomap: TempoMap) -> None:
    t0 = ticks(sp.tick)
    cur = tempomap.tempo(t0)
    ctype = str(sp.props.get("tempoChangeType", "undefined"))
    if sp.props.get("tempoChangeFactor") not in (None, ""):
        factor = _f32(float(sp.props["tempoChangeFactor"]))     # stored as std::optional<float>
    else:
        factor = GRADUAL_FACTORS.get(ctype, 1.0)
    new = cur * factor
    total = ticks(sp.tick2 - sp.tick)
    steps = max(8, int(total / 480) if total >= 0 else -int(-total / 480))
    method = str(sp.props.get("tempoEasingMethod", "normal"))
    for off, val in easing_value_curve(total, steps, new - cur, method).items():
        t = t0 + off
        if t not in tempomap:
            tempomap.set_tempo(t, cur + val)


# ------------------------------------------------------------------------------
# key list, ottavas, channels
# ------------------------------------------------------------------------------

OTTAVA_SHIFT = {"8va": 12, "8vb": -12, "15ma": 24, "15mb": -24, "22ma": 36, "22mb": -36,
                "0": 12, "1": -12, "2": 24, "3": -24, "4": 36, "5": -36, "8va alta": 12, "8va bassa": -12}


def apply_ottavas(score: Score) -> None:
    for sp in score.spanners:
        if sp.tag != "Ottava" or not _play(sp):
            continue
        staff = score.staves[sp.track // VOICES]
        shift = OTTAVA_SHIFT.get(str(sp.props.get("subtype", "8va")), 12)
        staff.pitch_offsets[ticks(sp.tick)] = shift
        staff.pitch_offsets[ticks(sp.tick2)] = 0


def _part_has_harmony(score: Score, part) -> bool:
    tracks = set(range(part.staves[0].idx * VOICES, (part.staves[-1].idx + 1) * VOICES))
    for m in score.measures:
        for seg in m.segments:
            if seg.type != SEG_CHORDREST:
                continue
            for a in seg.annotations:
                if a.tag in ("Harmony", "FretDiagram") and a.track in tracks:
                    return True
    return False


def assign_channels(score: Score) -> Dict[int, Tuple[int, int]]:
    """MasterScore::rebuildMidiMapping: global channel index -> (port, midi channel).

    Stored <midiPort>/<midiChannel> values are honoured; other channels take the
    next free slot (skipping 9, search position shared by all channels); drum
    instruments take channel 9 on the lowest free port.  Parts with chord
    symbols get an extra "harmony" channel (Part::updateHarmonyChannels).
    """
    from .score import InstrChannel
    order = []                          # (part, tick, InstrChannel, use_drumset)
    for part in score.parts:
        for tick in sorted(part.instruments):
            ins = part.instruments[tick]
            for ch in ins.channels:
                order.append((part, tick, ch, ins.use_drumset))
    slots: Dict[int, List[int]] = {}    # id(channel) -> [port, ch]
    occupied = set()
    for _p, _t, ch, _d in order:
        if ch.midi_port != -1 or ch.midi_channel != -1:
            slots[id(ch)] = [ch.midi_port, ch.midi_channel]
            if ch.midi_port != -1 and ch.midi_channel != -1:
                occupied.add(ch.midi_port * 16 + ch.midi_channel)
    search = [0]

    def next_free(port=-1, chan=-1) -> int:
        if chan != -1 and port != -1:
            return port * 16 + chan
        if chan != -1:
            p = 0
            while (p * 16 + chan) in occupied:
                p += 1
            occupied.add(p * 16 + chan)
            return p * 16 + chan
        if port != -1:
            for c in range(16):
                if c != 9 and (port * 16 + c) not in occupied:
                    occupied.add(port * 16 + c)
                    return port * 16 + c
        while search[0] % 16 == 9 or search[0] in occupied:
            search[0] += 1
        occupied.add(search[0])
        return search[0]

    def next_free_drum() -> int:
        i = 0
        while (i * 16 + 9) in occupied:
            i += 1
        occupied.add(i * 16 + 9)
        return i

    def update(entries):
        for _p, _t, ch, drum in entries:
            slot = slots.get(id(ch))
            if slot is None:
                if drum:
                    slots[id(ch)] = [next_free_drum(), 9]
                else:
                    nm = next_free()
                    slots[id(ch)] = [nm // 16, nm % 16]
            elif slot[0] == -1:
                nm = next_free(-1, slot[1])
                slot[0] = nm // 16
            elif slot[1] == -1:
                if drum:
                    slot[0], slot[1] = next_free_drum(), 9
                else:
                    nm = next_free(slot[0])
                    slot[0], slot[1] = nm // 16, nm % 16

    rebuilt = False
    for part in score.parts:
        main = part.instrument()
        if any(c.name == "harmony" for c in main.channels) or not _part_has_harmony(score, part):
            continue
        c0 = main.channels[0]
        hc = InstrChannel(name="harmony", program=0, bank=0 if c0.bank == 128 else c0.bank, volume=c0.volume,
                          pan=c0.pan, reverb=c0.reverb, chorus=c0.chorus)
        hc.stored_program = 0
        main.channels.append(hc)
        order = [(p, t, ch, ins.use_drumset) for p in score.parts for t in sorted(p.instruments)
                 for ins in [p.instruments[t]] for ch in ins.channels]
        search[0] = 0
        update(order)
        rebuilt = True
    search[0] = 0
    update(order)
    mapping: Dict[int, Tuple[int, int]] = {}
    for idx, (_p, _t, ch, _d) in enumerate(order):
        ch.channel = idx
        mapping[idx] = (slots[id(ch)][0], slots[id(ch)][1])
    return mapping


# ------------------------------------------------------------------------------
# velocities (fillScoreVelocities)
# ------------------------------------------------------------------------------

def dynamic_velocity(d: Element, tempomap: TempoMap) -> Tuple[int, int, Fraction]:
    """(velocity, changeInVelocity, velocityChangeLength) of a Dynamic."""
    sub = str(d.props.get("subtype", ""))
    base, change = DYNAMIC_VELOCITY.get(sub, (-1, 0))
    v = d.props.get("velocity")
    if v not in (None, "") and int(v) > 0:
        base = int(v)
    if d.props.get("veloChange") not in (None, ""):
        change = int(d.props["veloChange"])
    length = Fraction(0)
    if change != 0:
        seg_tick = d.props.get("_tick")
        ratio = tempomap.tempo(ticks(seg_tick)) / DEFAULT_TEMPO if seg_tick is not None else 1.0
        speed = {"slow": 1.3, "fast": 0.5}.get(str(d.props.get("veloChangeSpeed", "normal")).lower(), 0.8)
        length = from_ticks(int(ratio * (speed * 480.0)))
    return base, change, length


def voice_assignment(score: Score, e: Element) -> str:
    """MuseScore 4.6: voiceAssignment property (4.4+ files) or legacy dynType (older files)."""
    va = str(e.props.get("voiceAssignment", "")).strip()
    if va == "allInStaff":
        return "staff"
    if va == "currentVoiceOnly":
        return "voice"
    if va == "" and score.msc_version < 440 and e.props.get("dynType", "") != "":
        return "staff" if str(e.props["dynType"]).strip() == "0" else "instrument"
    return "instrument"


def dyn_range_452(score: Score, e: Element) -> str:
    """MuseScore 4.5: DynamicRange (only the legacy dynType tag of pre-4.4 files is honoured)."""
    if score.msc_version < 440 and e.props.get("dynType", "") != "":
        return {"0": "staff", "1": "instrument", "2": "system"}.get(str(e.props["dynType"]).strip(), "instrument")
    return "instrument"


def _hairpin_args(sp: Spanner):
    velo_change = int(sp.props.get("veloChange", 0) or 0)
    method = str(sp.props.get("veloChangeMethod", "normal") or "normal")
    direction = INCREASING
    if str(sp.props.get("subtype", "0")) in ("1", "3"):
        velo_change = -velo_change
        direction = DECREASING
    return velo_change, method, direction


def _art_mult(ctx: PlayContext, seg, el, mults_for):
    mult = ctx.chord_velocity_multiplier(el)
    if mult == 1.0:
        return
    change_time = min(_segment_ticks_simple(seg), Fraction(1, 16))
    start = int(mult * 100000)
    change = int((mult - 1) * 100000)
    vm = mults_for(el.track)
    vm.add_dynamic(el.tick, start)
    vm.add_hairpin(el.tick, el.tick + change_time, change, "normal", DECREASING)


def fill_score_velocities(score: Score, ctx: PlayContext, tempomap: TempoMap, profile: str = "4.6"):
    if profile == "4.5":
        return _fill_velocities_452(score, ctx, tempomap)
    velos: Dict[int, VelocityMap] = {t: VelocityMap() for t in range(score.ntracks())}
    mults: Dict[int, VelocityMap] = {t: VelocityMap() for t in range(score.ntracks())}
    for m in score.measures:
        for seg in m.segments:
            tick = seg.tick
            for e in seg.annotations:
                if e.tag != "Dynamic" or not score.staves[e.track // VOICES].linked_primary:
                    continue
                e.props["_tick"] = tick
                v, change, length = dynamic_velocity(e, tempomap)
                if v < 1:
                    continue
                v = max(1, min(127, v))
                direction = DECREASING if change < 0 else INCREASING
                staff_idx = e.track // VOICES
                va = voice_assignment(score, e)
                if va == "staff":
                    tracks = list(range(staff_idx * VOICES, (staff_idx + 1) * VOICES))
                elif va == "voice":
                    tracks = [e.track]
                else:
                    part = score.staves[staff_idx].part
                    tracks = [t for st in part.staves if st.linked_primary
                              for t in range(st.idx * VOICES, (st.idx + 1) * VOICES)]
                    if va == "instrument" and len(part.staves) and False:
                        pass
                if va == "instrument":
                    # all staves of the part, dynamic then optional ramp per staff
                    part = score.staves[staff_idx].part
                    for st in part.staves:
                        if not st.linked_primary:
                            continue
                        strs = range(st.idx * VOICES, (st.idx + 1) * VOICES)
                        for tr in strs:
                            velos[tr].add_dynamic(tick, v)
                        if change != 0:
                            for tr in strs:
                                velos[tr].add_hairpin(tick, tick + length, change, "normal", direction)
                    continue
                for tr in tracks:
                    velos[tr].add_dynamic(tick, v)
                if change != 0:
                    for tr in tracks:
                        velos[tr].add_hairpin(tick, tick + length, change, "normal", direction)
            if seg.type == SEG_CHORDREST:
                for tr in range(score.ntracks()):
                    el = seg.elements.get(tr)
                    if el is None or not el.is_chord or not score.staves[tr // VOICES].linked_primary:
                        continue
                    _art_mult(ctx, seg, el, lambda t: mults[t])

    for sp in score.spanners:
        if sp.tag != "HairPin":
            continue
        staff = score.staves[sp.track // VOICES]
        if not staff.linked_primary:
            continue
        velo_change, method, direction = _hairpin_args(sp)
        va = voice_assignment(score, sp)
        if va == "staff":
            tracks = range(staff.idx * VOICES, (staff.idx + 1) * VOICES)
        elif va == "voice":
            tracks = [sp.track]
        else:
            tracks = [t for st in staff.part.staves if st.linked_primary
                      for t in range(st.idx * VOICES, (st.idx + 1) * VOICES)]
        for tr in tracks:
            velos[tr].add_hairpin(sp.tick, sp.tick2, velo_change, method, direction)

    for st in score.staves:
        if not st.linked_primary:
            continue
        for tr in range(st.idx * VOICES, (st.idx + 1) * VOICES):
            velos[tr].setup()
            mults[tr].setup()
    _fill_volta_velocities(score, lambda staff_idx: [velos[t] for t in range(staff_idx * VOICES, (staff_idx + 1) * VOICES)])
    return velos, mults


def _fill_volta_velocities(score: Score, maps_for_staff) -> None:
    from .repeats import volta_measures
    for sp in score.spanners:
        if sp.tag != "Volta":
            continue
        staff = score.staves[sp.track // VOICES]
        if not staff.linked_primary:
            continue
        sm, em = volta_measures(score, sp)
        if sm is None or em is None or not em.repeat_end:
            continue
        start_tick = from_ticks(ticks(sm.tick) - 1)
        end_tick = from_ticks(ticks(em.end_tick) - 1)
        for vm in maps_for_staff(staff.idx):
            vm.add_dynamic(end_tick, vm.val(start_tick))


def _fill_velocities_452(score: Score, ctx: PlayContext, tempomap: TempoMap):
    """MuseScore 4.5.x: one map per staff, dynamics/hairpins scoped by DynamicRange."""
    staff_velo = {st.idx: VelocityMap() for st in score.staves}
    staff_mult = {st.idx: VelocityMap() for st in score.staves}

    def targets(staff_idx: int, rng: str):
        if rng == "staff":
            return [staff_idx]
        if rng == "system":
            return [st.idx for st in score.staves if st.linked_primary]
        return [st.idx for st in score.staves[staff_idx].part.staves if st.linked_primary]

    for st in score.staves:
        if not st.linked_primary:
            continue
        for m in score.measures:
            for seg in m.segments:
                tick = seg.tick
                for e in seg.annotations:
                    if e.track // VOICES != st.idx or e.tag != "Dynamic":
                        continue
                    e.props["_tick"] = tick
                    v, change, length = dynamic_velocity(e, tempomap)
                    if v < 1:
                        continue
                    v = max(1, min(127, v))
                    direction = DECREASING if change < 0 else INCREASING
                    for si in targets(st.idx, dyn_range_452(score, e)):
                        staff_velo[si].add_dynamic(tick, v)
                        if change != 0:
                            staff_velo[si].add_hairpin(tick, tick + length, change, "normal", direction)
                if seg.type == SEG_CHORDREST:
                    for tr in range(st.idx * VOICES, (st.idx + 1) * VOICES):
                        el = seg.elements.get(tr)
                        if el is None or not el.is_chord:
                            continue
                        _art_mult(ctx, seg, el, lambda t: staff_mult[t // VOICES])
        for sp in score.spanners:
            if sp.tag != "HairPin" or sp.track // VOICES != st.idx:
                continue
            velo_change, method, direction = _hairpin_args(sp)
            for si in targets(st.idx, dyn_range_452(score, sp)):
                staff_velo[si].add_hairpin(sp.tick, sp.tick2, velo_change, method, direction)
    for st in score.staves:
        if st.linked_primary:
            staff_velo[st.idx].setup()
            staff_mult[st.idx].setup()
    _fill_volta_velocities(score, lambda staff_idx: [staff_velo[staff_idx]])
    velos = {t: staff_velo[t // VOICES] for t in range(score.ntracks())}
    mults = {t: staff_mult[t // VOICES] for t in range(score.ntracks())}
    return velos, mults


def _harmony_element(a: Element):
    """The <Harmony> XML element of a Harmony annotation or a FretDiagram's chord symbol."""
    el = a.props.get("_elem")
    if el is None:
        return None
    if a.tag == "Harmony":
        return el
    if a.tag == "FretDiagram":
        return el.find("Harmony")
    return None


def _staff_key(staff, tick: int) -> int:
    best = 0
    for t in sorted(staff.keys):
        if t <= tick:
            best = staff.keys[t]
        else:
            break
    return best


def _segment_ticks_simple(seg) -> Fraction:
    """Segment::ticks(): distance to the next segment with a later tick (or the measure end)."""
    m = seg.measure
    segs = m.segments
    lo, hi = 0, len(segs)
    while lo < hi:                                   # first segment with tick > seg.tick
        mid = (lo + hi) // 2
        if segs[mid].tick <= seg.tick:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(segs):
        return segs[lo].tick - seg.tick
    return m.end_tick - seg.tick


# ------------------------------------------------------------------------------
# renderer
# ------------------------------------------------------------------------------

class Renderer:
    def __init__(self, score: Score, compat: Optional["Compat"] = None):
        if isinstance(compat, str):
            compat = Compat(dynamics=compat)
        self.compat = compat or Compat()
        self.score = score
        self.profile = self.compat.dynamics
        self.tempomap, self.sigmap = build_maps(score, self.compat)
        self.repeat_list = build_repeat_list(score)
        self.ctx = PlayContext(score, self.tempomap)
        self.events = EventsHolder()
        self.pause_map = PauseMap()

    def render(self) -> MidiFile:
        score = self.score
        apply_ottavas(score)
        PlayEventRenderer(self.ctx).create_all()
        self.channel_map = assign_channels(score)
        self.velos, self.mults = fill_score_velocities(score, self.ctx, self.tempomap, self.profile)
        for staff in score.staves:
            self.render_staff(staff)
        self.fixup_midi()
        self.render_spanners()
        self.pause_map.calculate(self.sigmap, self.tempomap, self.repeat_list)
        return self.export()

    # -- staff rendering --------------------------------------------------
    def render_staff(self, staff) -> None:
        last_measure = None
        prev_chords: List[Optional[Chord]] = [None] * VOICES
        for rs in self.repeat_list:
            offset = rs.utick - rs.tick
            for m in rs.measures:
                sidx = staff.idx
                if sidx in m.measure_repeats or m.measure_repeat_count.get(sidx):
                    mr = m.measure_repeats.get(sidx)
                    play = last_measure
                    if play is None or mr is None:
                        continue
                    count = m.measure_repeat_count.get(sidx, 1)
                    n = int(mr.props.get("numMeasures", 1) or 1)
                    i = count
                    while i < n and play.index > 0:
                        play = self.score.measures[play.index - 1]
                        i += 1
                    extra = ticks(m.tick - play.tick)
                    self.collect_measure_events(play, staff, offset + extra, prev_chords)
                else:
                    last_measure = m
                    self.collect_measure_events(m, staff, offset, prev_chords)

    def collect_measure_events(self, m: Measure, staff, tick_offset: int, prev_chords) -> None:
        score = self.score
        strack = staff.idx * VOICES
        etrack = strack + VOICES
        for seg in m.segments:
            if seg.type != SEG_CHORDREST:
                continue
            for a in seg.annotations:
                if strack <= a.track < etrack and a.tag in ("Harmony", "FretDiagram"):
                    self.render_harmony(m, seg, a, tick_offset)
            for track in range(strack, etrack):
                if not staff.linked_primary:
                    break
                voice = track % VOICES
                cr = seg.elements.get(track)
                if cr is None or not cr.is_chord:
                    prev_chords[voice] = None
                    continue
                chord = cr
                velo_mult = 1.0 * self.ctx.chord_velocity_multiplier(chord)
                self.collect_grace_before(chord, prev_chords[voice], velo_mult, staff, tick_offset)
                for note in chord.notes:
                    self.collect_note(note, velo_mult, tick_offset, staff)
                if not self.ctx.grace_notes_merged(chord):
                    for gc in chord.grace_after:
                        for note in gc.notes:
                            self.collect_note(note, velo_mult, tick_offset, staff)
                prev_chords[voice] = chord

    # -- chord symbols (renderHarmony) -------------------------------------
    def _harmony_setup(self) -> None:
        """Per-score chord-symbol data: chord list, style, and every Harmony per track in score order."""
        from . import harmony as H
        score = self.score
        self._hlib = H
        self._hstyle = H.load_harmony_style(score.path)
        self._hchords = H.load_chord_list(score.path)
        self._hdata: Dict[int, object] = {}            # id(annotation) -> HarmonyData
        self._htrack: Dict[int, List[Tuple[int, object]]] = {}   # track -> [(tick, HarmonyData)]
        for m in score.measures:
            for seg in m.segments:
                if seg.type != SEG_CHORDREST:
                    continue
                for a in seg.annotations:
                    el = _harmony_element(a)
                    if el is None:
                        continue
                    staff = score.staves[a.track // VOICES]
                    key = _staff_key(staff, ticks(seg.tick))
                    hd = H.read_harmony(el, self._hchords, self._hstyle, score.msc_version, key)
                    self._hdata[id(a)] = hd
                    self._htrack.setdefault(a.track, []).append((ticks(seg.tick), hd))
        self._hreps = [(rs.tick, rs.utick, rs.len()) for rs in self.repeat_list]

    def render_harmony(self, m: Measure, seg, a: Element, tick_offset: int) -> None:
        if not hasattr(self, "_hdata"):
            self._harmony_setup()
        H = self._hlib
        hd = self._hdata.get(id(a))
        if hd is None or not hd.play or not hd.realizable:
            return
        score = self.score
        staff = score.staves[a.track // VOICES]
        hchan = next((c for c in staff.part.instrument().channels if c.name == "harmony"), None)
        if hchan is None or not staff.linked_primary:
            return
        channel = hchan.channel
        tick = ticks(seg.tick)
        velocity = self.velos[a.track].val_ticks(tick)
        offset = 0
        if self._hstyle.get("concertPitch") in (False, None, "0", 0):
            offset += staff.part.instrument(tick).transpose_chromatic
        same_track = self._htrack.get(a.track, [])
        nxt = None
        for i, (_t, other) in enumerate(same_track):
            if other is hd:
                nxt = same_track[i + 1][1] if i + 1 < len(same_track) else None
                break
        pitches = H.realize_harmony(hd, self._hchords, self._hstyle, offset, nxt, score.msc_version)
        on_time = tick + tick_offset
        duration = H.actual_duration(hd.duration, tick, on_time, ticks(_segment_ticks_simple(seg)),
                                     ticks(m.end_tick), [t for t, _h in same_track], self._hreps)
        off_time = on_time + duration
        for p in pitches:
            self.events.add(channel, on_time, NPlayEvent(ME_NOTEON, channel, p, velocity, orig_staff=staff.idx))
            self.events.add(channel, off_time, NPlayEvent(ME_NOTEON, channel, p, 0, orig_staff=staff.idx))

    def collect_grace_before(self, chord: Chord, prev: Optional[Chord], velo_mult: float, staff,
                             tick_offset: int) -> None:
        gr = chord.grace_before
        accs = [g for g in gr if g.note_type == "ACCIACCATURA"]
        grace_sum = 0
        grace_off = 0
        if accs:
            grace_sum = ticks(accs[0].duration)
            if prev is not None:
                grace_sum = min(cdiv(ticks(prev.duration), 2), grace_sum)
            grace_off = cdiv(grace_sum, len(accs))
        if self.ctx.grace_notes_merged(chord):
            return
        cur = 0
        for c in gr:
            for note in c.notes:
                if c.note_type == "ACCIACCATURA":
                    self.collect_note(note, velo_mult, tick_offset, staff,
                                      grace_on=grace_sum - grace_off * cur,
                                      grace_off=grace_sum - grace_off * (cur + 1))
                else:
                    self.collect_note(note, velo_mult, tick_offset, staff)
            cur += 1

    def tie_length(self, note: Note) -> int:
        tie_len = 0
        n = note
        guard = 0
        while n is not None:
            tf = n.tie_for
            if tf is not None and tf.end is not None and tf.end is not n:
                n = tf.end
            else:
                break
            nel = n.play_events
            if nel:
                tie_len += cdiv(nel[0].len * ticks(n.chord.actual_ticks()), NOTE_LENGTH)
            guard += 1
            if guard > 100000:
                break
        return tie_len

    def collect_note(self, note: Note, velo_mult: float, tick_offset: int, staff,
                     grace_on: int = 0, grace_off: int = 0) -> None:
        if not note.play:
            return
        chord = note.chord
        instr = staff.part.instrument(ticks(chord.tick))
        channel = instr.channels[0].channel
        tie_len = self.tie_length(note)
        if chord.is_grace:
            chord = chord.parent
        chord_ticks = ticks(chord.actual_ticks())
        tick1 = ticks(note.chord.tick if not note.chord.is_grace else note.chord.parent.tick) + tick_offset
        nel = note.play_events
        nels = len(nel)
        for i, e in enumerate(nel):
            if note.tie_back is not None and nels == 1 and not any(
                    sp.tag == "Glissando" for sp in note.spanner_for):
                break
            pitch = note.pitch + staff.pitch_offset(ticks(chord.tick)) + e.pitch
            p = max(0, min(127, pitch))
            on = tick1 + cdiv(chord_ticks * e.ontime, 1000)
            off = on + cdiv(chord_ticks * e.len, 1000) - 1
            if note.tie_for is not None and i == nels - 1:
                off += tie_len
            velo = int(self.velos[note.track].val_ticks(on - tick_offset) * velo_mult * e.velocity_multiplier)
            if e.play:
                on_time = max(0, on - grace_on)
                off_time = max(0, off - grace_off)
                offset = cdiv(chord_ticks * e.offset, 1000) if grace_on == 0 else 0
                self.play_note(note, channel, p, max(1, min(127, velo)), on_time, off_time, offset, staff.idx)
                if instr.single_note_dynamics:
                    self.render_snd(chord, channel, tick_offset)

    def play_note(self, note: Note, channel: int, pitch: int, velo: int, on: int, off: int, offset: int,
                  staff_idx: int) -> None:
        if not note.play:
            return
        if note.user_velocity != 0:
            if note.velo_type == "offset":
                velo = velo + cdiv(velo * note.user_velocity, 100)
            else:
                velo = note.user_velocity
            velo = max(1, min(127, velo))
        if off > 0 and off < on:
            return
        ev = NPlayEvent(ME_NOTEON, channel, pitch, velo, orig_staff=staff_idx)
        ev.note = note
        self.events.add(channel, max(0, on - offset), ev)
        if off != -1:
            ev2 = ev.copy()
            ev2.velo = 0
            self.events.add(channel, max(0, off - offset), ev2)

    def render_snd(self, chord: Chord, channel: int, tick_offset: int) -> None:
        stick = chord.tick
        etick = stick + chord.duration
        vm = self.velos[chord.track]
        mm = self.mults[chord.track]
        changes = _changes_in_range(vm, stick, etick)
        mchanges = _changes_in_range(mm, stick, etick)
        velocity_map: Dict[int, int] = {}
        for a, b in changes:
            last = -1
            for t in range(ticks(a), ticks(b) + 1):
                v = vm.val_ticks(t)
                if v == last:
                    continue
                last = v
                velocity_map[t] = v
        conv = 100000
        for a, b in mchanges:
            if a == b:
                continue
            last = conv
            last_velocity = 0
            keys = sorted(velocity_map)
            import bisect as _b
            k = _b.bisect_right(keys, ticks(a))
            if k < len(keys):
                last_velocity = velocity_map[keys[k]]
            elif velocity_map:
                last_velocity = velocity_map[keys[0]]
            for t in range(ticks(a), ticks(b) + 1):
                mult = mm.val_ticks(t)
                if mult == last or mult == conv:
                    continue
                last = mult
                real = mult / conv
                if t in velocity_map:
                    last_velocity = velocity_map[t]
                    velocity_map[t] = int(velocity_map[t] * real)
                else:
                    velocity_map[t] = int(last_velocity * real)
        for t in sorted(velocity_map):
            ev = NPlayEvent(ME_CONTROLLER, channel, controller=CTRL_BREATH,
                            value=max(0, min(127, velocity_map[t])), orig_staff=chord.track // VOICES)
            self.events.add(channel, t + tick_offset, ev)

    # -- fixup --------------------------------------------------------------
    def fixup_midi(self) -> None:
        for ch in range(len(self.events)):
            now_playing: Dict[Tuple[int, int], int] = {}
            first_event: Dict[Tuple[int, int], NPlayEvent] = {}
            for _t, _s, ev in self.events.items(ch):
                if ev.type != ME_NOTEON:
                    continue
                key = (ev.channel, ev.pitch)
                np = now_playing.get(key, 0)
                if ev.velo == 0:
                    if np == 0:
                        ev.discard = 1
                    else:
                        np = (np - 1) & 0xFF
                        if np > 0:
                            ev.discard = 1
                        else:
                            ev.orig_staff = first_event[key].orig_staff
                else:
                    np = (np + 1) & 0xFF
                    if np > 1:
                        ev.discard = first_event[key].orig_staff + 1
                    first_event[key] = ev
                now_playing[key] = np

    # -- spanners (sustain pedal) ------------------------------------------
    def render_spanners(self) -> None:
        last = self.repeat_list[-1]
        limit = last.utick + last.len()
        for sp in self.score.spanners:
            if sp.tag != "Pedal":
                continue
            staff = self.score.staves[sp.track // VOICES]
            if not staff.linked_primary:
                continue
            channel = staff.part.instrument(ticks(sp.tick)).channels[0].channel
            st = ticks(sp.tick)
            on = st + 2
            off = ticks(sp.tick2) + (2 - 1)
            if off > limit:
                off = limit
            self.events.add(channel, on, NPlayEvent(ME_CONTROLLER, channel, controller=CTRL_SUSTAIN, value=127,
                                                    orig_staff=staff.idx))
            self.events.add(channel, off, NPlayEvent(ME_CONTROLLER, channel, controller=CTRL_SUSTAIN, value=0,
                                                     orig_staff=staff.idx))

    # -- export -------------------------------------------------------------
    def export(self) -> MidiFile:
        score = self.score
        pm = self.pause_map
        tracks: List[List[Tuple[int, Msg]]] = [[] for _ in score.staves]

        def ins(track_idx: int, tick: int, msg: Msg) -> None:
            tracks[track_idx].append((tick, msg))

        for i, staff in enumerate(score.staves):
            ins(i, 0, Msg("track_name", name=_midi_text(staff.part.track_name)))
        if tracks:
            for rs in self.repeat_list:
                off = rs.utick - rs.tick
                i0 = self.sigmap.lower_bound(rs.tick)
                i1 = self.sigmap.lower_bound(rs.end_tick)
                for k in self.sigmap.keys[i0:i1]:
                    se = self.sigmap.values[k]
                    num, den = se.nominal
                    if den not in (1, 2, 4, 8, 16, 32):
                        den = 4
                    ins(0, pm.tick_with_pauses(k + off), Msg("time_signature", numerator=num, denominator=den))
        for i, staff in enumerate(score.staves):
            found0 = False
            keys = sorted(staff.keys)
            for rs in self.repeat_list:
                off = rs.utick - rs.tick
                end = rs.tick + rs.len()
                for k in keys:
                    if rs.tick <= k < end:
                        from .smf import _MAJOR_KEYS
                        key = staff.keys[k]
                        ins(i, pm.tick_with_pauses(k + off), Msg("key_signature", key=_MAJOR_KEYS.get(key, "C")))
                        if k + off == 0:
                            found0 = True
            if not found0:
                ins(i, 0, Msg("key_signature", key="C"))
        if tracks:
            for k in self.pause_map.tempomap_with_pauses.keys:
                ev = self.pause_map.tempomap_with_pauses.values[k]
                tempo = int(round((1.0 / ev.tempo * 1.0) * 1000000.0))
                ins(0, k, Msg("set_tempo", tempo=tempo))

        nch = len(self.events)
        for ti, staff in enumerate(score.staves):
            part = staff.part
            is_top = part.staves[0] is staff
            part_port = self.channel_map[part.instrument().channels[0].channel][0]
            for tick_i in sorted(part.instruments):
                instr = part.instruments[tick_i]
                for ich in instr.channels:
                    port, channel = self.channel_map[ich.channel]
                    if is_top:
                        ins(ti, 0, Msg("control_change", channel=channel, control=CTRL_RESET_ALL, value=0))
                        if channel != 9:
                            ins(ti, 0, Msg("control_change", channel=channel, control=CTRL_LRPN, value=0))
                            ins(ti, 0, Msg("control_change", channel=channel, control=CTRL_HRPN, value=0))
                            ins(ti, 0, Msg("control_change", channel=channel, control=CTRL_HDATA, value=12))
                            ins(ti, 0, Msg("control_change", channel=channel, control=CTRL_LRPN, value=127))
                            ins(ti, 0, Msg("control_change", channel=channel, control=CTRL_HRPN, value=127))
                        prog = ich.stored_program if (ich.midi_port != -1 or ich.midi_channel != -1) else ich.program
                        if prog != -1:
                            ins(ti, 0, Msg("program_change", channel=channel, program=prog))
                        ins(ti, 0, Msg("control_change", channel=channel, control=7, value=ich.volume))
                        ins(ti, 0, Msg("control_change", channel=channel, control=10, value=ich.pan))
                        ins(ti, 0, Msg("control_change", channel=channel, control=91, value=ich.reverb))
                        ins(ti, 0, Msg("control_change", channel=channel, control=93, value=ich.chorus))
                    if 0 <= part_port <= 127:
                        ins(ti, 0, Msg("midi_port", port=part_port))
                    for e in range(nch):
                        for tick, _s, ev in self.events.items(e):
                            if ev.discard == ti + 1 and ev.velo > 0:
                                ins(ti, pm.tick_with_pauses(tick),
                                    Msg("note_on", channel=channel, note=ev.pitch, velocity=0))
                            if ev.orig_staff != ti:
                                continue
                            if ev.discard and ev.velo == 0:
                                continue
                            eport, echan = self.channel_map.get(ev.channel, (0, 0))
                            if port != eport or channel != echan:
                                continue
                            if ev.type == ME_NOTEON:
                                ins(ti, pm.tick_with_pauses(tick),
                                    Msg("note_on", channel=channel, note=ev.pitch, velocity=ev.velo))
                            elif ev.type == ME_CONTROLLER:
                                ins(ti, pm.tick_with_pauses(tick),
                                    Msg("control_change", channel=channel, control=ev.controller, value=ev.value))
            # lyrics / rehearsal marks
            for rs in self.repeat_list:
                off = rs.utick - rs.tick
                end = rs.end_tick
                for m in rs.measures:
                    for seg in m.segments:
                        if ticks(seg.tick) >= end:
                            break
                        if seg.type == SEG_CHORDREST:
                            for tr in range(part.staves[0].idx * VOICES, (part.staves[-1].idx + 1) * VOICES):
                                cr = seg.elements.get(tr)
                                if cr is None:
                                    continue
                                for lyr in cr.lyrics:
                                    text = _lyric_text(lyr)
                                    ins(ti, pm.tick_with_pauses(ticks(cr.tick) + off), Msg("lyrics", text=_midi_text(text)))
                        if ti == 0 and seg.type in (SEG_CHORDREST, SEG_TIMETICK):
                            for a in seg.annotations:
                                if a.tag == "RehearsalMark":
                                    text = _plain_text(a)
                                    ins(ti, pm.tick_with_pauses(ticks(seg.tick) + off), Msg("marker", text=_midi_text(text)))

        mf = MidiFile(ticks_per_beat=480)
        for track in tracks:
            track.sort(key=lambda x: x[0])
            msgs = []
            now = 0
            for tick, msg in track:
                msg.time = tick - now
                now = tick
                msgs.append(msg)
            msgs.append(Msg("end_of_track", time=1))
            mf.tracks.append(msgs)
        return mf


def _changes_in_range(vm: VelocityMap, stick: Fraction, etick: Fraction):
    import bisect as _b
    out = [(stick, stick)]
    i = _b.bisect_left(vm.ticks, stick)
    for k in range(i, len(vm.ticks)):
        t = vm.ticks[k]
        if t > etick:
            break
        ev = vm.events[k]
        if ev.type == 0:
            out.append((t, t))
        elif ev.type == 1:
            e = t + ev.length
            out.append((t, etick if e > etick else e))
    if i > 0:
        ev = vm.events[i - 1]
        if ev.type == 1:
            e = vm.ticks[i - 1] + ev.length
            if e > stick:
                out.append((stick, e))
    return out


def _midi_text(text: str) -> str:
    """MuseScore writes UTF-8 bytes; MIDI readers (and the smf module) see them as latin-1."""
    return text.encode("utf-8").decode("latin1")


def _plain_text(el: Element) -> str:
    x = el.props.get("_elem")
    if x is None:
        return str(el.props.get("text", ""))
    t = x.find("text")
    return "".join(t.itertext()) if t is not None else ""


def _lyric_text(lyr) -> str:
    t = lyr.find("text")
    text = "".join(t.itertext()) if t is not None else ""
    syl = lyr.findtext("syllabic") or "single"
    if syl in ("single", "end") and (not text or text[-1] != " "):
        text += " "
    return text


def render_score(score: Score, compat=None) -> MidiFile:
    """Render a score; ``compat`` is a :class:`Compat` (or a dynamics profile string)."""
    return Renderer(score, compat).render()
