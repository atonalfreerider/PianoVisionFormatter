"""In-memory model of a MuseScore score, reduced to what playback needs.

Positions are exact rationals (``fractions.Fraction``) measured in whole
notes, exactly like MuseScore's ``Fraction``; conversion to MIDI ticks uses
:func:`ticks`, which reproduces ``Fraction::ticks()`` rounding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional

DIVISION = 480
VOICES = 4


def ticks(f: Fraction) -> int:
    """MuseScore ``Fraction::ticks()``: rounds half away from zero to 1/1920
    of a whole note."""
    sgn = -1 if f.numerator < 0 else 1
    return sgn * ((sgn * f.numerator * DIVISION * 4 + f.denominator // 2) // f.denominator)


def from_ticks(t: int) -> Fraction:
    return Fraction(t, DIVISION * 4)


# duration type name -> (fraction of a whole note, number of hooks/flags)
DURATION_TYPES = {
    "long": (Fraction(4), 0), "breve": (Fraction(2), 0), "whole": (Fraction(1), 0),
    "half": (Fraction(1, 2), 0), "quarter": (Fraction(1, 4), 0), "eighth": (Fraction(1, 8), 1),
    "16th": (Fraction(1, 16), 2), "32nd": (Fraction(1, 32), 3), "64th": (Fraction(1, 64), 4),
    "128th": (Fraction(1, 128), 5), "256th": (Fraction(1, 256), 6), "512th": (Fraction(1, 512), 7),
    "1024th": (Fraction(1, 1024), 8),
}

# SegmentType bit values; within a tick, segments are ordered by these values
SEG_BREATH = 0x800
SEG_TIMETICK = 0x1000
SEG_CHORDREST = 0x2000
SEG_ENDBARLINE = 0x20000


@dataclass
class MidiArticulation:
    name: str
    velocity: int = 100
    gate_time: int = 100


@dataclass
class InstrChannel:
    name: str = ""
    program: int = 0
    bank: int = 0
    volume: int = 100
    pan: int = 64
    reverb: int = 0
    chorus: int = 0
    midi_port: int = -1
    midi_channel: int = -1
    channel: int = -1          # global index assigned by the MIDI mapping


@dataclass
class Instrument:
    id: str = ""
    instrument_id: str = ""
    long_name: str = ""
    track_name: str = ""
    channels: List[InstrChannel] = field(default_factory=list)
    articulations: List[MidiArticulation] = field(default_factory=list)
    single_note_dynamics: bool = False
    use_drumset: bool = False
    drum_lines: Dict[int, int] = field(default_factory=dict)
    transpose_chromatic: int = 0

    def gate_time_for(self, gate: int, name: str) -> int:
        for a in self.articulations:
            if a.name == name:
                return min(gate, a.gate_time)
        return gate

    def velocity_multiplier(self, name: str) -> float:
        for a in self.articulations:
            if a.name == name:
                return a.velocity / 100
        return 1


@dataclass
class Part:
    id: str
    track_name: str = ""
    staves: List["Staff"] = field(default_factory=list)
    instruments: Dict[int, Instrument] = field(default_factory=dict)   # tick -> instrument
    show: bool = True
    hide_when_empty: str = "auto"            # AutoOnOff: "auto" | "on" | "off"
    hide_staves_individually: bool = False   # hideStavesWhenIndividuallyEmpty

    def instrument(self, tick: int = -1) -> Instrument:
        """Part::instrument(tick): the entry with the greatest key <= tick (main instrument is key -1)."""
        if len(self.instruments) == 1:
            return next(iter(self.instruments.values()))
        best = None
        for t in sorted(self.instruments):
            if t <= tick:
                best = t
        return self.instruments[best if best is not None else min(self.instruments)]


@dataclass
class Staff:
    idx: int
    part: Part
    xml_id: str = ""
    group: str = "pitched"
    linked_primary: bool = True
    keys: Dict[int, int] = field(default_factory=dict)        # tick -> concert key
    pitch_offsets: Dict[int, int] = field(default_factory=dict)  # ottava: tick -> semitones
    swing: Dict[int, tuple] = field(default_factory=dict)       # tick -> (swingUnit, swingRatio)
    channel_switches: Dict[int, List[str]] = field(default_factory=dict)
    hide_when_empty: str = "auto"            # AutoOnOff: "auto" | "on" | "off"

    def pitch_offset(self, tick: int) -> int:
        best = 0
        for t in sorted(self.pitch_offsets):
            if t <= tick:
                best = self.pitch_offsets[t]
            else:
                break
        return best


@dataclass
class NoteEvent:
    pitch: int = 0
    ontime: int = 0
    len: int = 1000
    velocity_multiplier: float = 1.0
    play: bool = True
    offset: int = 0
    slide: bool = False


@dataclass(eq=False)
class Element:
    tag: str
    track: int = 0
    props: Dict[str, object] = field(default_factory=dict)


@dataclass(eq=False)
class Tuplet(Element):
    ratio: Fraction = Fraction(1)        # actualNotes / normalNotes
    parent: Optional["Tuplet"] = None


@dataclass(eq=False)
class ChordRest(Element):
    duration_type: str = "quarter"
    dots: int = 0
    duration: Fraction = Fraction(1, 4)  # m_ticks
    tuplet: Optional[Tuplet] = None
    segment: Optional["Segment"] = None
    staff_move: int = 0
    lyrics: List[str] = field(default_factory=list)

    @property
    def is_chord(self) -> bool:
        return self.tag == "Chord"

    def global_ticks(self) -> Fraction:
        f = self.duration
        t = self.tuplet
        while t is not None:
            f = f / t.ratio
            t = t.parent
        return f

    def actual_ticks(self) -> Fraction:
        return self.global_ticks()

    @property
    def tick(self) -> Fraction:
        return self.segment.tick

    @property
    def hooks(self) -> int:
        return DURATION_TYPES.get(self.duration_type, (None, 0))[1]


@dataclass(eq=False)
class Articulation(Element):
    subtype: str = ""
    play: bool = True
    ornament_style: str = "default"
    is_ornament: bool = False


@dataclass(eq=False)
class Chord(ChordRest):
    notes: List["Note"] = field(default_factory=list)
    note_type: str = "NORMAL"
    grace_before: List["Chord"] = field(default_factory=list)
    grace_after: List["Chord"] = field(default_factory=list)
    grace_all: List["Chord"] = field(default_factory=list)
    grace_index: int = -1
    parent: Optional["Chord"] = None
    articulations: List[Articulation] = field(default_factory=list)
    arpeggio: Optional[Element] = None
    tremolo: Optional[Element] = None
    tremolo_chord_type: str = ""        # "", "single", "first", "second"
    play_event_type: str = "auto"
    note_event_lists: Optional[List[List[NoteEvent]]] = None

    @property
    def is_grace(self) -> bool:
        return self.note_type != "NORMAL"

    @property
    def tick(self) -> Fraction:
        if self.parent is not None:
            return self.parent.tick
        return self.segment.tick

    def up_note(self) -> "Note":
        return self.notes[-1]


@dataclass(eq=False)
class Note(Element):
    pitch: int = 60
    tpc: int = 14
    chord: Optional[Chord] = None
    play: bool = True
    user_velocity: int = 0
    velo_type: str = "user"
    ghost: bool = False
    dead: bool = False
    tie_for: Optional["Spanner"] = None
    tie_back: Optional["Spanner"] = None
    spanner_for: List["Spanner"] = field(default_factory=list)
    spanner_back: List["Spanner"] = field(default_factory=list)
    play_events: List[NoteEvent] = field(default_factory=list)
    user_events: Optional[List[NoteEvent]] = None

    @property
    def tick(self) -> Fraction:
        return self.chord.tick

    def first_tied_note(self) -> "Note":
        n = self
        seen = set()
        while n.tie_back is not None and n.tie_back.start is not None and id(n) not in seen:
            seen.add(id(n))
            n = n.tie_back.start
        return n

    def last_tied_note(self) -> "Note":
        n = self
        seen = set()
        while n.tie_for is not None and n.tie_for.end is not None and id(n) not in seen:
            seen.add(id(n))
            n = n.tie_for.end
        return n

    def play_ticks(self) -> int:
        stick = self.first_tied_note().chord.tick
        last = self.last_tied_note()
        return ticks(last.chord.tick + last.chord.actual_ticks() - stick)


@dataclass(eq=False)
class Spanner(Element):
    tick: Fraction = Fraction(0)
    tick2: Fraction = Fraction(0)
    track2: int = 0
    start: Optional[object] = None      # start element (note / chordrest) when anchored
    end: Optional[object] = None


@dataclass(eq=False)
class Segment:
    type: int
    tick: Fraction
    measure: "Measure"
    elements: Dict[int, ChordRest] = field(default_factory=dict)
    annotations: List[Element] = field(default_factory=list)
    breaths: Dict[int, Element] = field(default_factory=dict)

    @property
    def rtick(self) -> Fraction:
        return self.tick - self.measure.tick


@dataclass(eq=False)
class MeasureBase:
    tag: str
    tick: Fraction = Fraction(0)
    section_break: Optional[Element] = None
    index: int = -1                       # index among Measures only
    breaks: set = field(default_factory=set)

    @property
    def is_measure(self) -> bool:
        return self.tag == "Measure"


@dataclass(eq=False)
class Measure(MeasureBase):
    len: Fraction = Fraction(1)           # actual length (m_len)
    timesig: Fraction = Fraction(1)       # nominal time signature
    len_nd: tuple = (4, 4)                # unreduced numerator/denominator of len
    timesig_nd: tuple = (4, 4)            # unreduced numerator/denominator of timesig
    irregular: bool = False
    repeat_start: bool = False
    repeat_end: bool = False
    repeat_count: int = 2
    jumps: List[Element] = field(default_factory=list)
    markers: List[Element] = field(default_factory=list)
    measure_repeat_count: Dict[int, int] = field(default_factory=dict)
    measure_repeats: Dict[int, Element] = field(default_factory=dict)
    segments: List[Segment] = field(default_factory=list)
    _seg_index: Dict[tuple, Segment] = field(default_factory=dict)

    @property
    def end_tick(self) -> Fraction:
        return self.tick + self.len

    def is_anacrusis(self, prev_base: Optional["MeasureBase"]) -> bool:
        """Measure::isAnacrusis(): short measure that starts the piece or follows a break/frame."""
        starts = (self.irregular or prev_base is None or bool(prev_base.breaks & {"line", "page", "section"})
                  or not prev_base.is_measure)
        return starts and self.timesig - self.len > 0

    def find_segment(self, stype: int, tick: Fraction) -> Optional[Segment]:
        return self._seg_index.get((stype, tick))

    def get_segment(self, stype: int, tick: Fraction) -> Segment:
        seg = self._seg_index.get((stype, tick))
        if seg is None:
            seg = Segment(stype, tick, self)
            self._seg_index[(stype, tick)] = seg
            self._insert(seg)
        return seg

    def _insert(self, seg: Segment) -> None:
        key = (seg.tick, seg.type)
        i = len(self.segments)
        while i > 0 and (self.segments[i - 1].tick, self.segments[i - 1].type) > key:
            i -= 1
        self.segments.insert(i, seg)

    def get_chordrest_or_timetick_segment(self, tick: Fraction) -> Segment:
        seg = self.find_segment(SEG_CHORDREST, tick) or self.find_segment(SEG_TIMETICK, tick)
        if seg is None:
            if tick - self.tick == self.len:
                seg = self.get_segment(SEG_TIMETICK, tick)
            else:
                seg = self.get_segment(SEG_CHORDREST, tick)
        return seg

    def chordrest_segments(self) -> List[Segment]:
        return [s for s in self.segments if s.type == SEG_CHORDREST]


@dataclass
class Score:
    path: str = ""
    msc_version: int = 460
    division: int = DIVISION
    parts: List[Part] = field(default_factory=list)
    staves: List[Staff] = field(default_factory=list)
    measure_bases: List[MeasureBase] = field(default_factory=list)
    measures: List[Measure] = field(default_factory=list)
    spanners: List[Spanner] = field(default_factory=list)
    style: Dict[str, str] = field(default_factory=dict)

    def ntracks(self) -> int:
        return len(self.staves) * VOICES

    def tick2measure(self, tick: Fraction) -> Optional[Measure]:
        for m in self.measures:
            if m.tick <= tick < m.end_tick:
                return m
        return self.measures[-1] if self.measures and tick >= self.measures[-1].tick else None
