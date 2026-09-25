"""Read MuseScore ``.mscz``/``.mscx`` files into :mod:`pianovision.score`.

The reading logic follows MuseScore 4.6's ``rw/read460`` (measure/voice
reading, relative ``location`` handling and connector matching for ties
and spanners), including the older 3.x/4.0-4.4 layouts found in real
libraries.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from .score import (DURATION_TYPES, SEG_BREATH, SEG_CHORDREST, SEG_ENDBARLINE, SEG_TIMETICK, VOICES,
                    Articulation, Chord, ChordRest, Element, InstrChannel, Instrument, Measure, MeasureBase,
                    MidiArticulation, Note, NoteEvent, Part, Score, Spanner, Staff, Tuplet, from_ticks, ticks)

INT_MIN = -(2 ** 31)

GRACE_TAGS = {
    "acciaccatura": "ACCIACCATURA", "appoggiatura": "APPOGGIATURA", "grace4": "GRACE4",
    "grace16": "GRACE16", "grace32": "GRACE32", "grace8after": "GRACE8_AFTER",
    "grace16after": "GRACE16_AFTER", "grace32after": "GRACE32_AFTER",
}
GRACE_AFTER = {"GRACE8_AFTER", "GRACE16_AFTER", "GRACE32_AFTER"}

ANNOTATION_TAGS = {
    "Dynamic", "Expression", "Tempo", "StaffText", "SystemText", "Harmony", "FretDiagram", "RehearsalMark",
    "InstrumentChange", "Sticking", "PlayTechAnnotation", "Capo", "StringTunings", "StaffState",
    "FiguredBass", "HarpPedalDiagram", "Symbol", "TremoloBar", "Image", "Text",
}
TIME_ANCHOR_TAGS = {"Dynamic", "Expression", "Tempo", "StaffText", "SystemText", "RehearsalMark",
                    "InstrumentChange", "PlayTechAnnotation", "Capo", "StringTunings", "Sticking"}


def load_mscx_bytes(path: str) -> bytes:
    if path.lower().endswith(".mscx"):
        with open(path, "rb") as f:
            return f.read()
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.endswith(".mscx")]
        # the main score is listed in META-INF/container.xml; fall back to the first mscx
        main = None
        if "META-INF/container.xml" in z.namelist():
            try:
                c = ET.fromstring(z.read("META-INF/container.xml"))
                for rf in c.iter("rootfile"):
                    fp = rf.get("full-path")
                    if fp and fp.endswith(".mscx") and fp in names:
                        main = fp
                        break
            except ET.ParseError:
                pass
        return z.read(main or names[0])


def _auto_on_off(v: str) -> str:
    """TConv::fromXml(AutoOnOff): "auto" | "on" | "off" (default auto)."""
    return v if v in ("on", "off") else "auto"


def read_style(path: str) -> Dict[str, str]:
    """Style values from score_style.mss (4.x) if present."""
    style: Dict[str, str] = {}
    if path.lower().endswith(".mscz"):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.endswith(".mss"):
                    try:
                        root = ET.fromstring(z.read(n))
                    except ET.ParseError:
                        continue
                    st = root.find("Style")
                    if st is not None:
                        for child in st:
                            style[child.tag] = (child.text or "").strip()
                    break
    return style


def _fraction(text: Optional[str], default: Fraction = Fraction(0)) -> Fraction:
    if text is None:
        return default
    text = text.strip()
    if "/" in text:
        n, d = text.split("/")
        return Fraction(int(n), int(d))
    return Fraction(int(text))


def _fraction_div_nd(a: Tuple[int, int], b: Tuple[int, int]) -> Tuple[int, int]:
    """MuseScore Fraction ``a / b`` keeping the unreduced representation."""
    sign = 1 if b[0] >= 0 else -1
    n, d = a[0] * sign * b[1], a[1] * sign * b[0]
    if b[0] != sign:
        from math import gcd
        g = gcd(n, d)
        if g:
            n, d = n // g, d // g
    return n, d


_TEMPLATES = None


def _templates() -> dict:
    global _TEMPLATES
    if _TEMPLATES is None:
        import json
        import os
        path = os.path.join(os.path.dirname(__file__), "data", "instrument_templates.json")
        with open(path, encoding="utf-8") as f:
            _TEMPLATES = json.load(f)
    return _TEMPLATES


def _template_snd(key: str) -> bool:
    """singleNoteDynamics of the instrument template with this id (MuseScore default: true)."""
    return _templates()["singleNoteDynamics"].get(key, True)


def _template_program(music_xml_id: str) -> int:
    """Instrument::recognizeMidiProgram (musicXmlId lookup; name-list matching not ported)."""
    return _templates()["programByMusicXmlId"].get(music_xml_id, 0)


_ARTICULATION_NAMES_206 = {
    "fermata": "fermataAbove", "shortfermata": "fermataShortAbove", "longfermata": "fermataLongAbove",
    "verylongfermata": "fermataVeryLongAbove", "sforzato": "articAccentAbove", "staccato": "articStaccatoAbove",
    "staccatissimo": "articStaccatissimoAbove", "tenuto": "articTenutoAbove", "portato": "articTenutoStaccatoAbove",
    "marcato": "articMarcatoAbove", "ouvert": "brassMuteOpen", "plusstop": "brassMuteClosed",
    "upbow": "stringsUpBow", "downbow": "stringsDownBow", "reverseturn": "ornamentTurnInverted",
    "turn": "ornamentTurn", "trill": "ornamentTrill", "prall": "ornamentShortTrill", "mordent": "ornamentMordent",
    "prallprall": "ornamentTremblement", "prallmordent": "ornamentPrallMordent", "upprall": "ornamentUpPrall",
    "upmordent": "ornamentUpMordent", "downmordent": "ornamentDownMordent", "pralldown": "ornamentPrallDown",
    "prallup": "ornamentPrallUp", "lineprall": "ornamentLinePrall", "schleifer": "ornamentPrecompSlide",
    "downprall": "ornamentPrecompMordentUpperPrefix", "ornamentDownPrall": "ornamentPrecompMordentUpperPrefix",
}

_ARTIC_CATEGORIES = {}
for _sym in ("articStaccatoAbove", "articStaccatoBelow", "tremoloDivisiDots2", "tremoloDivisiDots3",
             "tremoloDivisiDots4", "tremoloDivisiDots6"):
    _ARTIC_CATEGORIES[_sym] = {"staccato"}
for _sym in ("articTenutoAbove", "articTenutoBelow"):
    _ARTIC_CATEGORIES[_sym] = {"tenuto"}
for _d in ("Above", "Below"):
    _ARTIC_CATEGORIES["articMarcatoStaccato" + _d] = {"double", "staccato", "marcato"}
    _ARTIC_CATEGORIES["articTenutoStaccato" + _d] = {"double", "staccato", "tenuto"}
    _ARTIC_CATEGORIES["articAccentStaccato" + _d] = {"double", "staccato", "accent"}
    _ARTIC_CATEGORIES["articMarcatoTenuto" + _d] = {"double", "tenuto", "marcato"}
    _ARTIC_CATEGORIES["articTenutoAccent" + _d] = {"double", "tenuto", "accent"}
    _ARTIC_CATEGORIES["articAccent" + _d] = {"accent"}
    _ARTIC_CATEGORIES["articMarcato" + _d] = {"marcato"}

# symbols that MuseScore converts from Articulation to Ornament in pre-4.1 files
_ORNAMENT_IDS = {
    "ornamentTurn", "ornamentTurnInverted", "ornamentTurnSlash", "ornamentTrill", "brassMuteClosed",
    "ornamentMordent", "ornamentShortTrill", "ornamentTremblement", "ornamentPrallMordent", "ornamentLinePrall",
    "ornamentUpPrall", "ornamentUpMordent", "ornamentPrecompMordentUpperPrefix", "ornamentDownMordent",
    "ornamentPrallUp", "ornamentPrallDown", "ornamentPrecompSlide", "ornamentShake3", "ornamentShakeMuffat1",
    "ornamentTremblementCouperin", "ornamentPinceCouperin",
}

# combined articulations split into components in pre-4.1 files
_SPLIT = {
    "articAccentStaccato": ("articStaccato", "articAccent"),
    "articTenutoAccent": ("articTenuto", "articAccent"),
    "articTenutoStaccato": ("articTenuto", "articStaccato"),
    "articMarcatoStaccato": ("articStaccato", "articMarcato"),
    "articMarcatoTenuto": ("articTenuto", "articMarcato"),
}


def _close_to_note(a) -> bool:
    cats = _ARTIC_CATEGORIES.get(a.subtype, set())
    return ("staccato" in cats or "tenuto" in cats) and "double" not in cats


def _add_articulation(chord, a) -> None:
    """Chord::add ordering: staccato first, other close-to-note ones next, the rest appended."""
    arts = chord.articulations
    if _close_to_note(a):
        if "staccato" in _ARTIC_CATEGORIES.get(a.subtype, set()):
            arts.insert(0, a)
        else:
            i = 0
            while i < len(arts) and _close_to_note(arts[i]):
                i += 1
            arts.insert(i, a)
    else:
        arts.append(a)


def _legacy_articulation_fixups(chord) -> None:
    """compatutils: convert ornament articulations and split combined ones (top-level chords, <4.1)."""
    for a in list(chord.articulations):
        if not a.is_ornament and a.subtype in _ORNAMENT_IDS:
            chord.articulations.remove(a)
            new = Articulation("Ornament", track=a.track, subtype=a.subtype, play=a.play,
                               ornament_style=a.ornament_style, is_ornament=True)
            new.props = a.props
            _add_articulation(chord, new)
    for a in list(chord.articulations):
        base = a.subtype
        for suffix in ("Above", "Below"):
            if base.endswith(suffix) and base[:-len(suffix)] in _SPLIT:
                chord.articulations.remove(a)
                for comp in _SPLIT[base[:-len(suffix)]]:
                    sym = comp + suffix
                    if any(x.subtype == sym for x in chord.articulations):
                        continue
                    new = Articulation("Articulation", track=a.track, subtype=sym, play=a.play,
                                       ornament_style=a.ornament_style)
                    new.props = dict(a.props)
                    _add_articulation(chord, new)
                break


def _top_tuplet(cr):
    t = cr.tuplet
    while t is not None and t.parent is not None:
        t = t.parent
    return t


def _pct_int(elem: Optional[ET.Element]) -> int:
    """MuseScore String::toInt on '<n>' or '<n>%' (invalid or absent -> 0)."""
    if elem is None or elem.text is None:
        return 0
    t = elem.text.strip().rstrip("%").strip()
    try:
        return int(t)
    except ValueError:
        return 0


def _int(elem: Optional[ET.Element], default: int = 0) -> int:
    if elem is None or elem.text is None:
        return default
    try:
        return int(elem.text.strip())
    except ValueError:
        return int(float(elem.text.strip()))


def _text(elem: ET.Element, tag: str, default: Optional[str] = None) -> Optional[str]:
    c = elem.find(tag)
    if c is None or c.text is None:
        return default
    return c.text.strip()


class Location:
    __slots__ = ("staff", "voice", "measure", "frac", "grace", "note", "rel")

    def __init__(self, staff=INT_MIN, voice=INT_MIN, measure=INT_MIN, frac=None, grace=INT_MIN, note=INT_MIN,
                 rel=False):
        self.staff, self.voice, self.measure = staff, voice, measure
        self.frac = frac
        self.grace, self.note, self.rel = grace, note, rel

    @classmethod
    def relative(cls) -> "Location":
        return cls(0, 0, 0, Fraction(0), INT_MIN, 0, True)

    @property
    def track(self) -> int:
        return VOICES * self.staff + self.voice

    def set_track(self, track: int) -> None:
        self.staff, self.voice = track // VOICES, track % VOICES

    def to_absolute(self, ref: "Location") -> None:
        if not self.rel:
            return
        self.staff += ref.staff
        self.voice += ref.voice
        self.measure += ref.measure
        self.frac = self.frac + ref.frac
        self.note += ref.note
        self.rel = False

    def key(self):
        return (self.staff, self.voice, self.measure, self.frac, self.grace, self.note)

    def copy(self) -> "Location":
        return Location(self.staff, self.voice, self.measure, self.frac, self.grace, self.note, self.rel)


def _read_location(elem: ET.Element) -> Location:
    loc = Location.relative()
    for c in elem:
        if c.tag == "staves":
            loc.staff = _int(c)
        elif c.tag == "voices":
            loc.voice = _int(c)
        elif c.tag == "measures":
            loc.measure = _int(c)
        elif c.tag == "fractions":
            loc.frac = _fraction(c.text)
        elif c.tag == "grace":
            loc.grace = _int(c)
        elif c.tag == "notes":
            loc.note = _int(c)
    return loc


class _Connector:
    __slots__ = ("type", "elem", "current", "cur_loc", "prev_loc", "next_loc", "prev", "next", "measure")

    def __init__(self, stype, elem, current, cur_loc, measure):
        self.type = stype
        self.elem = elem
        self.current = current
        self.cur_loc = cur_loc
        self.prev_loc = None
        self.next_loc = None
        self.prev = None
        self.next = None
        self.measure = measure


class MscxReader:
    def __init__(self, path: str):
        self.path = path
        self.score = Score(path=path)
        self.tick = Fraction(0)
        self.int_tick = 0
        self.track = 0
        self.cur_measure: Optional[Measure] = None
        self.measure_index = 0
        self.last_measure: Optional[Measure] = None
        self.timesig_next = Fraction(0)
        self.timesig_next_nd = (0, 1)
        self.pending: List[_Connector] = []
        self.connectors: List[_Connector] = []
        self.spanner_seq = 0
        self.staff_defs: Dict[str, Staff] = {}
        self.element_order = 0

    # ------------------------------------------------------------------
    def read(self) -> Score:
        data = load_mscx_bytes(self.path)
        root = ET.fromstring(data)
        version = root.get("version", "4.60")
        major, _, minor = version.partition(".")
        self.score.msc_version = int(major) * 100 + int((minor + "00")[:2])
        self.score.style = read_style(self.path)
        score_el = root.find("Score")
        if score_el is None:
            raise ValueError("no <Score> element")
        inline_style = score_el.find("Style")
        if inline_style is not None:
            for child in inline_style:
                self.score.style.setdefault(child.tag, (child.text or "").strip())
        self.division = _int(score_el.find("Division"), 480)
        self.program_version = (root.findtext("programVersion") or "").strip()
        for child in score_el:
            if child.tag == "Part":
                self._read_part(child)
        staff_index = 0
        for child in score_el:
            if child.tag == "Staff":
                self._read_staff_content(child, staff_index)
                staff_index += 1
        self._finish()
        return self.score

    def _file_ticks(self, t: int) -> int:
        return t * 480 // self.division if self.division != 480 else t

    # ------------------------------------------------------------------
    # parts / instruments
    # ------------------------------------------------------------------
    def _read_part(self, el: ET.Element) -> None:
        part = Part(id=el.get("id", str(len(self.score.parts) + 1)))
        instrument = None
        staff_hide_modes: Dict[int, str] = {}
        for c in el:
            if c.tag == "Staff":
                staff = Staff(idx=len(self.score.staves), part=part, xml_id=c.get("id", ""))
                st = c.find("StaffType")
                if st is not None:
                    staff.group = st.get("group", "pitched")
                if c.find("linkedTo") is not None:
                    staff.linked_primary = False
                hwe = c.find("hideWhenEmpty")
                if hwe is not None:
                    staff_hide_modes[staff.idx] = (hwe.text or "").strip()
                part.staves.append(staff)
                self.score.staves.append(staff)
            elif c.tag == "trackName":
                part.track_name = c.text or ""
            elif c.tag == "Instrument":
                instrument = self._read_instrument(c)
            elif c.tag == "show":
                part.show = _int(c, 1) != 0
            elif c.tag == "hideWhenEmpty":
                part.hide_when_empty = _auto_on_off((c.text or "").strip())
            elif c.tag == "hideStavesWhenIndividuallyEmpty":
                part.hide_staves_individually = (c.text or "").strip() not in ("0", "false")
        self._convert_hide_modes(part, staff_hide_modes)
        if instrument is None:
            instrument = Instrument(channels=[InstrChannel()])
        if not part.track_name:
            part.track_name = instrument.track_name or instrument.long_name
        part.instruments[-1] = instrument            # main instrument (MuseScore key -1)
        self.score.parts.append(part)

    def _convert_hide_modes(self, part: Part, modes: Dict[int, str]) -> None:
        """Staff hide-when-empty settings; files before 4.6 store the old
        StaffHideMode enum (AUTO, ALWAYS, NEVER, INSTRUMENT), converted as in read400/read410."""
        if self.score.msc_version >= 460:
            for staff in part.staves:
                staff.hide_when_empty = _auto_on_off(modes.get(staff.idx, ""))
            return
        old = [{"1": "always", "2": "never", "3": "instrument"}.get(modes.get(st.idx, "0"), "auto")
               for st in part.staves]
        if len(old) == 1:
            if old[0] in ("always", "never"):
                part.hide_when_empty = "on" if old[0] == "always" else "off"
            return
        if all(v == "always" for v in old):
            part.hide_when_empty = "on"
            part.hide_staves_individually = True
            return
        if all(v == "never" for v in old):
            part.hide_when_empty = "off"
            return
        if self.score.style.get("hideEmptyStaves", "0") == "1" and "auto" in old and "instrument" not in old:
            part.hide_staves_individually = True
        for st, v in zip(part.staves, old):
            if v in ("always", "never"):
                st.hide_when_empty = "on" if v == "always" else "off"

    def _read_instrument(self, el: ET.Element) -> Instrument:
        ins = Instrument(id=el.get("id", ""))
        for c in el:
            t = c.tag
            if t == "longName":
                ins.long_name = "".join(c.itertext())
            elif t == "trackName":
                ins.track_name = c.text or ""
            elif t == "instrumentId":
                ins.instrument_id = c.text or ""
            elif t == "transposeChromatic":
                ins.transpose_chromatic = _int(c, 0)
            elif t == "singleNoteDynamics":
                ins.single_note_dynamics = (c.text or "").strip() not in ("0", "false")
                ins.props_snd_read = True
            elif t == "useDrumset":
                ins.use_drumset = _int(c, 0) != 0
            elif t == "Articulation":
                ins.articulations.append(MidiArticulation(
                    name=c.get("name", ""),
                    velocity=_pct_int(c.find("velocity")),
                    gate_time=_pct_int(c.find("gateTime"))))
            elif t == "Channel":
                ins.channels.append(self._read_channel(c))
            elif t == "Drum":
                try:
                    ins.drum_lines[int(c.get("pitch", "-1"))] = _int(c.find("line"), 0)
                except ValueError:
                    pass
        if not ins.channels:
            ins.channels.append(InstrChannel())
        if ins.channels[0].program == -1:
            ins.channels[0].program = _template_program(ins.instrument_id)
        if not getattr(ins, "props_snd_read", False):
            key = ins.track_name.lower().replace(" ", "-").replace("\u266d", "b")
            ins.single_note_dynamics = _template_snd(key)
        return ins

    def _read_channel(self, el: ET.Element) -> InstrChannel:
        ch = InstrChannel(name=el.get("name", "") or "normal", program=-1)
        for c in el:
            t = c.tag
            if t == "program":
                v = c.get("value")
                ch.program = int(v) if v not in (None, "") else -1
                if ch.program == -1 and c.text and c.text.strip():
                    ch.program = int(c.text.strip())
            elif t == "controller":
                ctrl = int(c.get("ctrl", "-1"))
                value = int(c.get("value", "0"))
                if ctrl == 0:
                    ch.bank = (ch.bank & 0x7F) | (value << 7)
                elif ctrl == 32:
                    ch.bank = (ch.bank & ~0x7F) | (value & 0x7F)
                elif ctrl == 7:
                    ch.volume = value
                elif ctrl == 10:
                    ch.pan = value
                elif ctrl == 91:
                    ch.reverb = value
                elif ctrl == 93:
                    ch.chorus = value
            elif t == "midiPort":
                ch.midi_port = _int(c, -1)
            elif t == "midiChannel":
                ch.midi_channel = _int(c, -1)
        ch.stored_program = ch.program
        return ch

    # ------------------------------------------------------------------
    # staves / measures
    # ------------------------------------------------------------------
    def _read_staff_content(self, el: ET.Element, staff_idx: int) -> None:
        self.measure_index = 0
        self._set_tick(Fraction(0))
        self.track = staff_idx * VOICES
        self.last_measure = None
        score = self.score
        if staff_idx == 0:
            for c in el:
                if c.tag == "Measure":
                    m = Measure("Measure", tick=self.tick)
                    m.index = len(score.measures)
                    self.measure_index = m.index
                    prev = self.last_measure
                    if self.timesig_next != 0:
                        f, nd = self.timesig_next, self.timesig_next_nd
                    elif prev is not None:
                        f, nd = prev.timesig, prev.timesig_nd
                    else:
                        f, nd = Fraction(4, 4), (4, 4)
                    m.len, m.len_nd = f, nd
                    m.timesig, m.timesig_nd = f, nd
                    self.timesig_next = Fraction(0)
                    self.timesig_next_nd = (0, 1)
                    self._read_measure(m, c, staff_idx)
                    score.measure_bases.append(m)
                    score.measures.append(m)
                    self.last_measure = m
                    self._set_tick(m.tick + m.len)
                elif c.tag in ("HBox", "VBox", "TBox", "FBox"):
                    mb = MeasureBase(c.tag, tick=self.tick)
                    self._read_measure_base_props(mb, c)
                    score.measure_bases.append(mb)
                elif c.tag == "tick":
                    self._set_tick(from_ticks(self._file_ticks(_int(c))))
        else:
            mi = 0
            for c in el:
                if c.tag == "Measure":
                    if mi >= len(score.measures):
                        break
                    m = score.measures[mi]
                    self._set_tick(m.tick)
                    self.measure_index = mi
                    self._read_measure(m, c, staff_idx)
                    self.last_measure = m
                    mi += 1
                elif c.tag == "tick":
                    self._set_tick(from_ticks(self._file_ticks(_int(c))))

    def _read_measure_base_props(self, mb: MeasureBase, el: ET.Element) -> None:
        for c in el:
            if c.tag == "LayoutBreak":
                self._layout_break(mb, c)

    @staticmethod
    def _layout_break(mb: MeasureBase, c: ET.Element) -> None:
        sub = _text(c, "subtype", "") or ""
        mb.breaks.add(sub)
        if sub == "section":
            lb = Element("LayoutBreak", props={"subtype": sub})
            p = c.find("pause")
            if p is not None:
                lb.props["pause"] = float(p.text)
            mb.section_break = lb

    def _set_tick(self, f: Fraction) -> None:
        self.tick = f
        self.int_tick = ticks(f)

    def _inc_tick(self, f: Fraction) -> None:
        self.tick = self.tick + f
        self.int_tick += ticks(f)

    def _rtick(self) -> Fraction:
        return self.tick - self.cur_measure.tick if self.cur_measure else self.tick

    def _location(self) -> Location:
        loc = Location()
        loc.set_track(self.track)
        loc.frac = self._rtick()
        loc.measure = self.measure_index
        return loc

    def _set_location(self, loc: Location) -> None:
        if loc.rel:
            new = loc.copy()
            new.to_absolute(self._location())
            int_ticks = ticks(loc.frac)
            if self.tick == from_ticks(self.int_tick + int_ticks):
                self.int_tick += int_ticks
                self.track = new.track
                return
            loc = new
        self.track = loc.track
        self._set_tick(loc.frac)
        self._inc_tick(self.cur_measure.tick)

    def _read_measure(self, m: Measure, el: ET.Element, staff_idx: int) -> None:
        self.cur_measure = m
        next_track = staff_idx * VOICES
        self.track = next_track
        self.has_voices = False
        irregular = False
        if el.get("len"):
            m.len = _fraction(el.get("len"))
            n, _, d = el.get("len").partition("/")
            m.len_nd = (int(n), int(d or 1))
            irregular = True
        for c in el:
            t = c.tag
            if t == "voice":
                self.track = next_track
                next_track += 1
                self._set_tick(m.tick)
                self._read_voice(m, c, staff_idx, irregular)
            elif t in ("Marker", "Jump"):
                if staff_idx != 0:
                    continue  # system objects: only the top staff's copy counts
                e = Element(t, track=self.track, props=self._props(c))
                e.props["_order"] = self.element_order
                self.element_order += 1
                (m.markers if t == "Marker" else m.jumps).append(e)
            elif t == "irregular":
                m.irregular = _int(c, 1) != 0
            elif t == "startRepeat":
                m.repeat_start = True
            elif t == "endRepeat":
                m.repeat_count = _int(c, 2)
                m.repeat_end = True
            elif t == "measureRepeatCount":
                m.measure_repeat_count[staff_idx] = _int(c)
            elif t == "LayoutBreak":
                self._layout_break(m, c)
            elif t == "multiMeasureRest":
                pass
        self._check_connectors()
        self._check_measure(m, staff_idx)
        self.cur_measure = None

    # ------------------------------------------------------------------
    # Measure::checkMeasure: fill gaps in voices with (invisible) rests
    # ------------------------------------------------------------------
    def _check_measure(self, m: Measure, staff_idx: int) -> None:
        strack = staff_idx * VOICES
        dtrack = strack + (VOICES if self.has_voices else 1)
        f = m.len
        for track in range(strack, dtrack):
            expected = Fraction(0)
            items = [(s.tick - m.tick, s.elements[track]) for s in m.chordrest_segments() if track in s.elements]
            i = 0
            while i < len(items):
                current, e = items[i]
                if current < expected:
                    break
                elif current > expected:
                    self._fill_gap(m, expected, current - expected, track)
                    if current >= f:
                        break
                top = e.tuplet
                while top is not None and top.parent is not None:
                    top = top.parent
                if top is not None:
                    # skipTuplet: continue after the tuplet's last element
                    while i + 1 < len(items) and _top_tuplet(items[i + 1][1]) is top:
                        i += 1
                    expected = current + top.props["ticks"]
                else:
                    expected = current + e.duration
                i += 1
            if f > expected and expected != 0:
                self._fill_gap(m, expected, f - expected, track)

    def _fill_gap(self, m: Measure, pos: Fraction, length: Fraction, track: int) -> None:
        from .rhythm import rhythmic_duration_list
        mbs = self.score.measure_bases
        if m in mbs:
            i = mbs.index(m)
            prev = mbs[i - 1] if i > 0 else None
        else:
            prev = mbs[-1] if mbs else None
        ana = m.is_anacrusis(prev)
        durations = rhythmic_duration_list(length, True, pos, m.timesig_nd[0], m.timesig_nd[1], m.len,
                                           is_anacrusis=ana,
                                           anacrusis_offset=(m.timesig - m.len) if ana else Fraction(0))
        cur = pos
        for dtype, dots, frac in durations:
            rest = ChordRest("Rest", track=track)
            rest.duration_type = dtype
            rest.dots = dots
            rest.duration = m.len if dtype == "measure" else frac
            rest.props["gap"] = True
            seg = m.get_segment(SEG_CHORDREST, m.tick + cur)
            rest.segment = seg
            seg.elements[track] = rest
            cur += frac

    @staticmethod
    def _props(el: ET.Element) -> Dict[str, object]:
        props: Dict[str, object] = {}
        for c in el:
            if len(c) == 0:
                props[c.tag] = (c.text or "").strip() if c.text is not None else ""
                for k, v in c.attrib.items():
                    props[f"{c.tag}@{k}"] = v
            else:
                props.setdefault("_children", []).append(c)
        for k, v in el.attrib.items():
            props[f"@{k}"] = v
        return props

    def _read_voice(self, m: Measure, el: ET.Element, staff_idx: int, irregular: bool) -> None:
        segment = None
        grace_notes: List[Chord] = []
        tuplet: Optional[Tuplet] = None
        fermata: Optional[Element] = None
        for c in el:
            t = c.tag
            if t == "location":
                self._set_location(_read_location(c))
            elif t == "tick":
                self._set_tick(from_ticks(self._file_ticks(_int(c))))
            elif t == "BarLine":
                rt = self.tick - m.tick
                if rt != 0 and rt != m.len:
                    stype = 0x400
                elif rt == m.len:
                    stype = SEG_ENDBARLINE
                else:
                    stype = 0x1
                segment = m.get_segment(stype, self.tick)
                if fermata is not None:
                    segment.annotations.append(fermata)
                    fermata = None
            elif t == "Chord":
                chord = self._read_chord(c)
                segment = m.get_segment(SEG_CHORDREST, self.tick)
                if chord.note_type != "NORMAL":
                    grace_notes.append(chord)
                else:
                    chord.segment = segment
                    segment.elements[self.track] = chord
                    if self.score.msc_version < 410:
                        _legacy_articulation_fixups(chord)
                    if self.track % VOICES and any(n.props.get("visible", True) for n in chord.notes):
                        self.has_voices = True
                    for i, gc in enumerate(grace_notes):
                        gc.grace_index = i
                        gc.parent = chord
                    chord.grace_before = [g for g in grace_notes if g.note_type not in GRACE_AFTER]
                    # Chord::graceNotesAfter() walks the grace list backwards
                    chord.grace_after = [g for g in reversed(grace_notes) if g.note_type in GRACE_AFTER]
                    chord.grace_all = list(grace_notes)
                    grace_notes = []
                    if tuplet is not None:
                        chord.tuplet = tuplet
                    self._inc_tick(chord.actual_ticks())
                if fermata is not None:
                    segment.annotations.append(fermata)
                    fermata = None
            elif t == "Rest":
                segment = m.get_segment(SEG_CHORDREST, self.tick)
                rest = self._read_rest(c, m)
                rest.segment = segment
                segment.elements[self.track] = rest
                if self.track % VOICES and rest.props.get("visible", True):
                    self.has_voices = True
                if fermata is not None:
                    segment.annotations.append(fermata)
                    fermata = None
                if tuplet is not None:
                    rest.tuplet = tuplet
                self._inc_tick(rest.actual_ticks())
            elif t == "Breath":
                segment = m.get_segment(SEG_BREATH, self.tick)
                segment.breaths[self.track] = Element("Breath", track=self.track, props=self._props(c))
            elif t == "Spanner":
                self._read_spanner(c, m, None)
            elif t in ("MeasureRepeat", "RepeatMeasure"):
                segment = m.get_segment(SEG_CHORDREST, self.tick)
                e = Element("MeasureRepeat", track=self.track, props=self._props(c))
                n = int(e.props.get("subtype", "1") or 1) if t == "MeasureRepeat" else 1
                e.props["numMeasures"] = n or 1
                m.measure_repeats[staff_idx] = e
                m.measure_repeat_count.setdefault(staff_idx, 1)
                self._inc_tick(m.len)
            elif t == "TimeSig":
                sig_nd = (_int(c.find("sigN"), 4), _int(c.find("sigD"), 4))
                stretch_n = c.find("stretchN")
                stretch_nd = (1, 1)
                if stretch_n is not None:
                    stretch_nd = (_int(stretch_n, 1), _int(c.find("stretchD"), 1))
                nd = _fraction_div_nd(sig_nd, stretch_nd)
                if self.tick == m.tick:
                    m.timesig, m.timesig_nd = Fraction(*nd), nd
                    if not irregular:
                        m.len, m.len_nd = m.timesig, nd
                elif self.tick > m.tick:
                    self.timesig_next, self.timesig_next_nd = Fraction(*nd), nd
            elif t == "KeySig":
                key = _text(c, "concertKey")
                if key is None:
                    key = _text(c, "accidental")
                if key is None:
                    key = _text(c, "subtype")
                custom = c.find("custom") is not None or c.find("CustomSym") is not None
                staff = self.score.staves[staff_idx]
                staff.keys[ticks(self.tick)] = int(key) if (key not in (None, "") and not custom) else 0
            elif t in ANNOTATION_TAGS:
                if t in TIME_ANCHOR_TAGS:
                    segment = m.get_chordrest_or_timetick_segment(self.tick)
                else:
                    segment = m.get_segment(SEG_CHORDREST, self.tick)
                ann = Element(t, track=self.track, props=self._props(c))
                ann.props["_elem"] = c
                segment.annotations.append(ann)
                if t == "InstrumentChange":
                    ins_el = c.find("Instrument")
                    if ins_el is not None:
                        # Segment::add(InstrumentChange): part->setInstrument(instrument, tick)
                        part = self.score.staves[staff_idx].part
                        part.instruments[ticks(segment.tick)] = self._read_instrument(ins_el)
            elif t == "Fermata":
                fermata = Element("Fermata", track=self.track, props=self._props(c))
            elif t == "Tuplet":
                old = tuplet
                actual = _int(c.find("actualNotes"), 1)
                normal = _int(c.find("normalNotes"), 1)
                tuplet = Tuplet("Tuplet", track=self.track, ratio=Fraction(actual, normal), parent=old)
                base = DURATION_TYPES.get(_text(c, "baseNote", "eighth") or "eighth", (Fraction(1, 8), 0))[0]
                bdots = _int(c.find("baseDots"), 0)
                if bdots:
                    base = base * (2 - Fraction(1, 2 ** bdots))
                tuplet.props["ticks"] = base * normal
            elif t == "endTuplet":
                if tuplet is not None:
                    tuplet = tuplet.parent
        if fermata is not None:
            stype = SEG_ENDBARLINE if self.tick == m.end_tick else SEG_CHORDREST
            segment = m.get_segment(stype, self.tick)
            segment.annotations.append(fermata)

    # ------------------------------------------------------------------
    # chords, rests, notes
    # ------------------------------------------------------------------
    def _duration(self, cr: ChordRest, el: ET.Element) -> None:
        for c in el:
            if c.tag == "durationType":
                cr.duration_type = (c.text or "quarter").strip()
                if cr.duration_type in DURATION_TYPES:
                    cr.duration = DURATION_TYPES[cr.duration_type][0]
            elif c.tag == "dots":
                cr.dots = _int(c)
            elif c.tag == "duration":
                cr.duration = _fraction(c.text)
                cr.props["explicit_duration"] = True
        if cr.dots and cr.duration_type in DURATION_TYPES and not cr.props.get("explicit_duration"):
            base = DURATION_TYPES[cr.duration_type][0]
            cr.duration = base * (2 - Fraction(1, 2 ** cr.dots))

    def _read_rest(self, el: ET.Element, m: Measure) -> ChordRest:
        rest = ChordRest("Rest", track=self.track)
        rest.duration = m.timesig
        rest.duration_type = "measure"
        self._duration(rest, el)
        v = el.find("visible")
        if v is not None:
            rest.props["visible"] = _int(v, 1) != 0
        if rest.duration_type == "measure" and not rest.props.get("explicit_duration"):
            rest.duration = m.timesig
        for c in el:
            if c.tag == "Spanner":            # e.g. a slur ending on a rest
                self._read_spanner(c, self.cur_measure, rest)
            elif c.tag == "Lyrics":
                rest.lyrics.append(c)
        return rest

    def _read_chord(self, el: ET.Element) -> Chord:
        chord = Chord("Chord", track=self.track)
        self._duration(chord, el)
        for c in el:
            t = c.tag
            if t == "Note":
                note = self._read_note(c, chord)
                chord.notes.append(note)
            elif t in GRACE_TAGS:
                chord.note_type = GRACE_TAGS[t]
            elif t in ("Articulation", "Ornament"):
                sub = _text(c, "subtype", "") or ""
                if self.score.msc_version < 410:
                    sub = self._legacy_articulation_sym(sub)
                play = _text(c, "play")
                a = Articulation(t, track=self.track, subtype=sub,
                                 play=(play is None or play not in ("0", "false")),
                                 ornament_style=_text(c, "ornamentStyle", "default") or "default",
                                 is_ornament=(t == "Ornament"))
                a.props = self._props(c)
                _add_articulation(chord, a)
            elif t == "Arpeggio":
                chord.arpeggio = Element("Arpeggio", track=self.track, props=self._props(c))
            elif t in ("Tremolo", "TremoloSingleChord", "TremoloTwoChord"):
                chord.tremolo = Element(t, track=self.track, props=self._props(c))
            elif t == "staffMove":
                chord.staff_move = _int(c)
            elif t == "Spanner":
                self._read_spanner(c, self.cur_measure, chord)
            elif t == "Lyrics":
                chord.lyrics.append(c)
            elif t == "play":
                chord.props["play"] = c.text
        # read-time order (Chord::add): ascending pitch, file order among unisons
        chord.notes.sort(key=lambda n: n.pitch)
        return chord

    def _legacy_articulation_sym(self, sub: str) -> str:
        """read400 fallbacks for 3.x/4.0 articulation names."""
        known = sub in _ARTIC_CATEGORIES or sub.startswith(("artic", "ornament", "brass", "strings", "fermata",
                                                            "guitar", "wiggle", "plucked", "lute", "tremolo",
                                                            "dynamic", "handbells"))
        if not known:
            sub = _ARTICULATION_NAMES_206.get(sub, "")
        if sub == "" or sub == "ornamentMordentInverted":
            sub = "ornamentMordent"
        pv = self.program_version
        if pv and pv < "3.6" and sub in ("", "ornamentMordent"):
            sub = "ornamentShortTrill"
        return sub

    def _read_note(self, el: ET.Element, chord: Chord) -> Note:
        note = Note("Note", track=self.track, chord=chord)
        spanners = []
        for c in el:
            t = c.tag
            if t == "pitch":
                note.pitch = _int(c, 60)
            elif t == "tpc":
                note.tpc = _int(c, 14)
            elif t == "play":
                note.play = _int(c, 1) != 0
            elif t == "velocity":
                if self.score.msc_version >= 400:
                    note.user_velocity = _int(c)
            elif t == "veloType":
                note.velo_type = (c.text or "").strip().lower()
                note.props["veloType"] = note.velo_type
            elif t == "ghost":
                if self.score.msc_version < 400:
                    note.dead = _int(c) != 0
                else:
                    note.ghost = _int(c) != 0
            elif t == "dead":
                note.dead = _int(c) != 0
            elif t == "Spanner":
                spanners.append(c)
            elif t == "Events":
                evs = []
                for ev in c.findall("Event"):
                    evs.append(NoteEvent(pitch=_int(ev.find("pitch"), 0), ontime=_int(ev.find("ontime"), 0),
                                         len=_int(ev.find("len"), 1000)))
                note.user_events = evs
                chord.play_event_type = "user"
            elif t == "Accidental":
                note.props["accidental"] = _text(c, "subtype", "")
            elif t == "visible":
                note.props["visible"] = _int(c, 1) != 0
            elif t in ("fret", "string"):
                note.props[t] = _int(c)
        for sp in spanners:
            self._read_spanner(sp, self.cur_measure, note)
        note.play_events = list(note.user_events) if note.user_events is not None else [NoteEvent()]
        return note

    # ------------------------------------------------------------------
    # connectors (ties and spanners)
    # ------------------------------------------------------------------
    def _read_spanner(self, el: ET.Element, measure: Measure, current) -> None:
        stype = el.get("type", "")
        cur = self._location()
        conn = _Connector(stype, None, current if current is not None else measure, cur, measure)
        for c in el:
            if c.tag == "prev":
                loc = c.find("location")
                conn.prev_loc = _read_location(loc) if loc is not None else Location.relative()
            elif c.tag == "next":
                loc = c.find("location")
                conn.next_loc = _read_location(loc) if loc is not None else Location.relative()
            elif c.tag == stype:
                conn.elem = c
        self.pending.append(conn)

    def _update_connector(self, conn: _Connector) -> None:
        cur = conn.current
        if isinstance(cur, Note):
            ch = cur.chord
            conn.cur_loc.grace = ch.grace_index if ch.is_grace else INT_MIN
            notes = ch.notes
            conn.cur_loc.note = 0 if len(notes) == 1 else notes.index(cur)
        elif isinstance(cur, Chord):
            conn.cur_loc.grace = cur.grace_index if cur.is_grace else INT_MIN
            conn.cur_loc.note = INT_MIN
        # measures and rests keep the defaults
        if conn.prev_loc is not None:
            conn.prev_loc.to_absolute(conn.cur_loc)
        if conn.next_loc is not None:
            conn.next_loc.to_absolute(conn.cur_loc)

    def _check_connectors(self) -> None:
        for c1 in self.pending:
            self.connectors.append(c1)
            self._update_connector(c1)
            for c2 in self.connectors:
                if self._connect(c2, c1):
                    if self._finished(c2):
                        self._add_to_score(c2)
                        self._remove_chain(c2)
                    break
        self.pending = []

    @staticmethod
    def _connect(a: _Connector, b: _Connector) -> bool:
        if a is b or a.type != b.type:
            return False
        if a.prev_loc is not None and a.prev is None and b.next_loc is not None and b.next is None:
            if a.prev_loc.key() == b.cur_loc.key() and a.cur_loc.key() == b.next_loc.key():
                a.prev = b
                b.next = a
                return True
        if a.next_loc is not None and a.next is None and b.prev_loc is not None and b.prev is None:
            if a.next_loc.key() == b.cur_loc.key() and a.cur_loc.key() == b.prev_loc.key():
                a.next = b
                b.prev = a
                return True
        return False

    @staticmethod
    def _first(c: _Connector) -> _Connector:
        seen = set()
        while c.prev is not None and id(c) not in seen:
            seen.add(id(c))
            c = c.prev
        return c

    def _finished(self, c: _Connector) -> bool:
        first = self._first(c)
        last = c
        seen = set()
        while last.next is not None and id(last) not in seen:
            seen.add(id(last))
            last = last.next
        return first.prev_loc is None and last.next_loc is None

    def _remove_chain(self, c: _Connector) -> None:
        chain = set()
        x = self._first(c)
        while x is not None and id(x) not in chain:
            chain.add(id(x))
            x = x.next
        self.connectors = [k for k in self.connectors if id(k) not in chain]

    def _add_to_score(self, c: _Connector) -> None:
        start = self._first(c)
        end = start
        while end.next is not None:
            end = end.next
        stype = start.type
        el = start.elem
        props = self._props(el) if el is not None else {}
        if el is not None:
            props["_elem"] = el
        sp = Spanner(stype, track=start.cur_loc.track, props=props)
        sp.track2 = end.cur_loc.track
        cur_s, cur_e = start.current, end.current
        if isinstance(cur_s, Note):
            sp.tick = cur_s.chord.tick
            sp.tick2 = cur_e.chord.tick if isinstance(cur_e, Note) else sp.tick
            if stype == "Tie":
                n = cur_s
                guard = 0
                while n.tie_for is not None and n.tie_for.end is not None and guard < 10000:
                    n = n.tie_for.end
                    guard += 1
                sp.start = n
                n.tie_for = sp
                if isinstance(cur_e, Note):
                    sp.end = cur_e
                    cur_e.tie_back = sp
                return
            sp.start = cur_s
            cur_s.spanner_for.append(sp)
            if isinstance(cur_e, Note):
                sp.end = cur_e
                cur_e.spanner_back.append(sp)
        elif isinstance(cur_s, ChordRest):
            sp.tick = cur_s.tick
            sp.start = cur_s
            if isinstance(cur_e, ChordRest):
                sp.tick2 = cur_e.tick
                sp.end = cur_e
        else:  # measure anchored
            sp.tick = start.measure.tick + start.cur_loc.frac
            sp.tick2 = end.measure.tick + end.cur_loc.frac
        sp.props["_seq"] = self.spanner_seq
        self.spanner_seq += 1
        self.score.spanners.append(sp)

    # ------------------------------------------------------------------
    def _connect_tremolos(self) -> None:
        for m in self.score.measures:
            segs = m.chordrest_segments()
            for i, seg in enumerate(segs):
                for track, e in list(seg.elements.items()):
                    if not e.is_chord or e.tremolo is None or e.tremolo_chord_type == "second":
                        continue
                    sub = str(e.tremolo.props.get("subtype", "")).lower()
                    two = e.tremolo.tag == "TremoloTwoChord" or (e.tremolo.tag == "Tremolo" and sub.startswith("c"))
                    if not two:
                        e.tremolo_chord_type = "single"
                        continue
                    partner = None
                    for s2 in segs[i + 1:]:
                        n = s2.elements.get(track)
                        if n is not None and n.is_chord:      # rests are skipped
                            partner = n
                            break
                    if partner is None:
                        e.tremolo = None                      # no second chord: tremolo removed
                        continue
                    e.tremolo_chord_type = "first"
                    partner.tremolo = e.tremolo
                    partner.tremolo_chord_type = "second"

    def _sort_notes_after_layout(self) -> None:
        """Layout re-sorts chord notes by staff line (diatonic step), then pitch."""
        from .playevents import abs_step
        for m in self.score.measures:
            for seg in m.chordrest_segments():
                for e in seg.elements.values():
                    if not e.is_chord:
                        continue
                    staff = self.score.staves[e.track // VOICES]
                    ins = staff.part.instrument()
                    for c in [e] + e.grace_all:
                        if ins.use_drumset:
                            # drum staves sort by drumset line (descending), then pitch
                            c.notes.sort(key=lambda n: (-ins.drum_lines.get(n.pitch, 0), n.pitch))
                        else:
                            c.notes.sort(key=lambda n: (abs_step(n.tpc, n.pitch), n.pitch))

    def _pre_layout_compat(self) -> None:
        """EngravingCompat::doPreLayoutCompatIfNeeded for files older than 4.4."""
        score = self.score
        if score.msc_version >= 440:
            return
        # correctPedalEndPoints: 45-degree pedal ends move back to the last chord/rest start
        for sp in score.spanners:
            if sp.tag == "Pedal" and str(sp.props.get("endHookType", "0")).strip() == "2":
                cr = self._chordrest_ending_before(sp.tick2, sp.track // VOICES)
                if cr is not None:
                    sp.tick2 = cr.tick
        # migrateDynamicPosOnVocalStaves: voice 2-4 dynamics/hairpins apply to their own voice only
        for m in score.measures:
            for seg in m.chordrest_segments():
                for a in seg.annotations:
                    if a.tag in ("Dynamic", "Expression") and a.track % VOICES != 0:
                        a.props["voiceAssignment"] = "currentVoiceOnly"
        for sp in score.spanners:
            if sp.tag == "HairPin" and sp.track % VOICES != 0:
                sp.props["voiceAssignment"] = "currentVoiceOnly"

    def _chordrest_ending_before(self, tick: Fraction, staff_idx: int):
        """Score::findChordRestEndingBeforeTickInStaff."""
        ptick = tick - Fraction(1, 1920)
        m = self.score.tick2measure(ptick)
        if m is None:
            return None
        segs = m.chordrest_segments()
        if not segs:
            return None
        strack, etrack = staff_idx * VOICES, staff_idx * VOICES + VOICES
        best_seg, best_track, last = segs[0], strack, Fraction(-1)
        for ns in segs:
            if ns.tick > ptick:
                break
            for t in range(strack, etrack):
                cr = ns.elements.get(t)
                if cr is not None:
                    end = cr.tick + cr.actual_ticks()
                    if end >= last and end <= tick:
                        best_seg, best_track, last = ns, t, end
        return best_seg.elements.get(best_track)

    def _finish(self) -> None:
        self._sort_notes_after_layout()
        self._connect_tremolos()
        self._pre_layout_compat()
        # MuseScore's spanner map is ordered by start tick, then insertion order
        self.score.spanners.sort(key=lambda s: (s.tick, s.props.get("_seq", 0)))
        for i, m in enumerate(self.score.measures):
            m.index = i


def read_score(path: str) -> Score:
    return MscxReader(path).read()
