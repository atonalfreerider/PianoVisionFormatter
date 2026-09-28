"""Hand edits: write the hands recorded on the headset back into the MuseScore scores.

Note Waterfall's HAND REC mode records which hand plays each note and saves the notes
that should change hands in a sidecar, ``HandEdits/<song>.hands.json`` on the headset
(format 1, see :func:`parse_sidecar`).  This module takes those sidecars back to the
scores (``python -m pianovision hands pull|review|apply``):

    pull     adb pull of the sidecars into ``<output>/.hands/inbox`` (not ``applied/``)
    review   map every edit to its source note (a render with provenance), plan the
             score edit, write it to a temporary copy, re-render and compare
    apply    the same, then per score a y/N (or ``--yes``): back up the .mscz to
             ``<output>/.attic/<date>/scores/``, overwrite it, build, deploy to both
             apps and move the applied sidecars to ``HandEdits/applied/`` on the headset

How a hand change is written.  The renderer (like MuseScore's own MIDI export) puts a
note into the MIDI track of the staff whose voice holds its chord, and the first piano
staff is the right hand, the second the left.  ``<staffMove>`` only draws a chord on the
other staff; it does not change the track.  A note therefore changes hands by moving into
a voice of the other staff of the piano part:

* a chord whose notes all change hands moves as a whole (with its grace notes) into a free
  voice (2-4) of the other staff, at the same tick; its old place becomes an invisible rest;
* a chord where only some notes change hands is split: the moving notes go into a new
  chord with the same duration and articulations in a free voice of the other staff;
* the moved chord gets a ``<staffMove>`` back to the staff it was drawn on, so the page
  looks as before (a chord that was drawn on the other staff already loses its staffMove);
* tied continuations move with their note; a tuplet is rebuilt in the target voice with
  invisible rests for the members that stay; ties, slurs and other spanners touching a
  moved note get their relative locations rewritten;
* a moved note whose velocity would change (dynamics that apply to one staff only) gets
  its velocity written into the score.

The rest of the .mscx stays byte for byte.  Whatever cannot be written safely (a grace
note without its chord, an ottava line, an arpeggio on a split chord, no free voice, a
repeat recorded on only some passes, ...) is reported and skipped.  Before anything is
written the edited score is rendered and compared with the render of the original: only
the edited notes may change hands; times, pitches, durations and velocities stay the same.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import xml.parsers.expat as expat
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .score import VOICES, Chord, ChordRest, Note, NoteEvent, Score, ticks

SIDECAR_FORMAT = 1
SIDECAR_EXT = ".hands.json"
APPLIED_DIR = "applied"
DEFAULT_DEVICE_DIR = "/sdcard/Android/data/com.atonalfreerider.notewaterfall/files/HandEdits"
HANDS = ("right", "left")
INT_MIN = -(2 ** 31)
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


class PlanError(Exception):
    """A hand change that cannot be written safely (reported, then skipped)."""


# ==============================================================================
# the sidecar (shared with Note Waterfall, format 1)
# ==============================================================================

@dataclass
class HandEdit:
    """One note that changes hands: identified by its ORIGINAL hand, ticksStart, MIDI number and
    occurrence (how many notes of that hand with the same ticksStart and note come before it)."""
    from_hand: str
    to_hand: str
    ticks: int
    midi: int
    occurrence: int = 0
    start: Optional[float] = None        # seconds (informational)
    measure: Optional[int] = None        # measureInd (informational)
    votes: int = 0
    against: int = 0

    def key(self) -> Tuple[str, int, int, int]:
        return (self.from_hand, self.ticks, self.midi, self.occurrence)


@dataclass
class Sidecar:
    name: str                             # file name, <song stem>.hands.json
    song: str                             # song file name, <song stem>.json
    song_md5: str = ""
    updated: str = ""
    format: int = SIDECAR_FORMAT
    edits: List[HandEdit] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: str = ""                       # set when nothing could be read
    path: str = ""
    md5: str = ""                         # of the file's bytes (the copy on the headset must still match to archive it)


def sidecar_name(song: str) -> str:
    """``chop_Nocturne.json`` -> ``chop_Nocturne.hands.json``."""
    base = os.path.basename(song)
    return (base[:-5] if base.lower().endswith(".json") else base) + SIDECAR_EXT


def song_of_sidecar(name: str) -> str:
    base = os.path.basename(name)
    return (base[:-len(SIDECAR_EXT)] if base.endswith(SIDECAR_EXT) else os.path.splitext(base)[0]) + ".json"


def _hand(v) -> Optional[str]:
    if not isinstance(v, str):
        return None
    t = v.strip().lower()
    return {"left": "left", "l": "left", "right": "right", "r": "right"}.get(t)


def _int(v) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v) if float(v).is_integer() else None


def parse_sidecar(data: bytes, name: str = "") -> Sidecar:
    """Read a sidecar; never raises.  A newer format, or a file that is not a sidecar, gives no
    edits and ``error``; edits that cannot be read are skipped with a warning; for a note listed
    twice the later edit wins (as in the app); a revert (from = to) is ignored."""
    sc = Sidecar(name=os.path.basename(name), song=song_of_sidecar(name) if name else "")
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        sc.error = f"not readable JSON ({e})"
        return sc
    if not isinstance(doc, dict):
        sc.error = "not a JSON object"
        return sc
    fmt = doc.get("format")
    if fmt is None:
        sc.warnings.append('no "format": read as format 1')
    elif _int(fmt) is None:
        sc.error = f"format {fmt!r} is not a number"
        return sc
    elif _int(fmt) > SIDECAR_FORMAT:
        sc.format = _int(fmt)
        sc.error = f"format {sc.format} is newer than {SIDECAR_FORMAT}: update the formatter"
        return sc
    if isinstance(doc.get("song"), str) and doc["song"]:
        sc.song = os.path.basename(doc["song"])
    sc.song_md5 = str(doc.get("songMd5") or "").lower()
    sc.updated = str(doc.get("updated") or "")
    edits = doc.get("edits") or []
    if not isinstance(edits, list):
        sc.error = '"edits" is not an array'
        return sc
    latest: Dict[tuple, HandEdit] = {}
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            sc.warnings.append(f"edit {i}: not an object")
            continue
        fr, to = _hand(e.get("from")), _hand(e.get("to"))
        # as the app reads it: a missing (or null) occurrence is the first, a negative measureInd is unknown
        occ = 0 if e.get("occurrence") is None else _int(e.get("occurrence"))
        tk, midi = _int(e.get("ticksStart")), _int(e.get("midi"))
        if fr is None or to is None or tk is None or midi is None or occ is None or not 0 <= midi <= 127 \
                or occ < 0:
            sc.warnings.append(f"edit {i}: unreadable ({json.dumps(e)[:80]})")
            continue
        start = e.get("start")
        measure = _int(e.get("measureInd"))
        he = HandEdit(fr, to, tk, midi, occ,
                      start=float(start) if isinstance(start, (int, float)) and not isinstance(start, bool) else None,
                      measure=measure if measure is not None and measure >= 0 else None, votes=_int(e.get("votes")) or 0,
                      against=_int(e.get("against")) or 0)
        if he.key() in latest:
            sc.warnings.append(f"edit {i}: the note is listed twice; the later edit wins")
        latest[he.key()] = he
    for he in latest.values():
        if he.from_hand == he.to_hand:
            sc.warnings.append(f"edit {he.midi}@{he.ticks}: from = to (a revert), ignored")
            continue
        sc.edits.append(he)
    sc.edits.sort(key=lambda h: (h.ticks, h.midi, h.from_hand, h.occurrence))
    return sc


def read_sidecar(path: str) -> Sidecar:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return Sidecar(name=os.path.basename(path), song=song_of_sidecar(path), error=str(e), path=path)
    sc = parse_sidecar(data, os.path.basename(path))
    sc.path = path
    sc.md5 = _md5(data)
    return sc


# ==============================================================================
# the .mscx with byte offsets, and patching it
# ==============================================================================

class XNode:
    """An element of the .mscx: ``start`` is its '<', ``end`` the offset after its last '>',
    ``close`` the offset of its end tag (None when self-closing)."""
    __slots__ = ("tag", "attrs", "start", "end", "close", "children", "parent", "_text")

    def __init__(self, tag: str, attrs: dict, start: int, parent: Optional["XNode"]):
        self.tag, self.attrs, self.start, self.parent = tag, attrs, start, parent
        self.end = -1
        self.close: Optional[int] = None
        self.children: List[XNode] = []
        self._text: List[str] = []

    @property
    def text(self) -> str:
        return "".join(self._text).strip()

    def find(self, tag: str) -> Optional["XNode"]:
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def findall(self, tag: str) -> List["XNode"]:
        return [c for c in self.children if c.tag == tag]

    def value(self, tag: str, default: Optional[str] = None) -> Optional[str]:
        c = self.find(tag)
        return c.text if c is not None else default

    def descendants(self, tag: str) -> Iterable["XNode"]:
        for c in self.children:
            if c.tag == tag:
                yield c
            yield from c.descendants(tag)


def _start_tag_end(data: bytes, i: int) -> int:
    """Offset after the '>' that ends the tag starting at ``i`` (quotes respected)."""
    q = 0
    n = len(data)
    while i < n:
        c = data[i]
        if q:
            if c == q:
                q = 0
        elif c in (0x22, 0x27):
            q = c
        elif c == 0x3E:
            return i + 1
        i += 1
    raise ValueError("unterminated tag")


def parse_xml(data: bytes) -> XNode:
    """Element tree of ``data`` with byte offsets (expat)."""
    parser = expat.ParserCreate()
    parser.buffer_text = True
    stack: List[XNode] = []
    top: List[XNode] = []

    def start(tag, attrs):
        n = XNode(tag, attrs, parser.CurrentByteIndex, stack[-1] if stack else None)
        te = _start_tag_end(data, n.start)
        if data[te - 2:te] == b"/>":
            n.end = te
        if stack:
            stack[-1].children.append(n)
        else:
            top.append(n)
        stack.append(n)

    def end(_tag):
        n = stack.pop()
        if n.end < 0:
            n.close = parser.CurrentByteIndex
            n.end = data.index(b">", n.close) + 1

    def chars(t):
        if stack:
            stack[-1]._text.append(t)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.Parse(data, True)
    return top[0]


def _line_start(data: bytes, pos: int) -> int:
    return data.rfind(b"\n", 0, pos) + 1


def _indent(data: bytes, node: XNode) -> str:
    ls = _line_start(data, node.start)
    ind = data[ls:node.start]
    if ind.strip(b" \t"):
        raise PlanError(f"<{node.tag}> does not start its own line")
    return ind.decode()


def _span(data: bytes, node: XNode) -> Tuple[int, int]:
    """The lines of an element: from its line start to after the newline that follows it."""
    _indent(data, node)
    end = node.end
    if data[end:end + 1] == b"\n":
        end += 1
    elif data[end:end + 2] == b"\r\n":
        end += 2
    return _line_start(data, node.start), end


def _child_indent(data: bytes, node: XNode) -> str:
    if node.children:
        return _indent(data, node.children[0])
    return _indent(data, node) + "  "


class Patches:
    """Replacements and insertions on the original bytes.  A patch inside a range that another
    patch replaces is not applied there (it still applies when that range is extracted with
    :meth:`text`, which is how moved elements carry their own edits along)."""

    def __init__(self, data: bytes):
        self.data = data
        self.items: List[Tuple[int, int, bytes, int]] = []

    def add(self, start: int, end: int, text) -> None:
        if isinstance(text, str):
            text = text.encode("utf-8")
        self.items.append((start, end, text, len(self.items)))

    def remove(self, node: XNode) -> None:
        s, e = _span(self.data, node)
        self.add(s, e, b"")

    def _apply(self, lo: int, hi: int, items) -> bytes:
        items = sorted(items, key=lambda p: (p[0], 0 if p[0] == p[1] else 1, -p[1], p[3]))
        out = []
        pos = lo
        cursor = lo
        for s, e, text, _seq in items:
            if s < cursor:
                if e <= cursor:
                    continue                      # inside a range that is replaced as a whole
                raise PlanError("overlapping edits (internal)")
            out.append(self.data[pos:s])
            out.append(text)
            pos = max(pos, e)
            cursor = max(cursor, e)
        out.append(self.data[pos:hi])
        return b"".join(out)

    def text(self, lo: int, hi: int, extra: Sequence[Tuple[int, int, bytes, int]] = ()) -> bytes:
        """``data[lo:hi]`` with the patches strictly inside it (and ``extra``) applied."""
        inside = [p for p in list(self.items) + list(extra)
                  if lo <= p[0] and p[1] <= hi and (p[0], p[1]) != (lo, hi)
                  and (p[0] != p[1] or lo < p[0] < hi)]
        return self._apply(lo, hi, inside)

    def result(self) -> bytes:
        return self._apply(0, len(self.data), self.items)


def _without_eids(data: bytes, node: XNode) -> List[Tuple[int, int, bytes, int]]:
    """Local patches that drop every <eid> below ``node`` (copied elements get new ids in MuseScore)."""
    out = []
    for e in node.descendants("eid"):
        s, t = _span(data, e)
        out.append((s, t, b"", -1))
    return out


# ==============================================================================
# locations (MuseScore's Location, as mscx.MscxReader resolves connectors)
# ==============================================================================

@dataclass(frozen=True)
class Loc:
    staff: int
    voice: int
    measure: int
    frac: Fraction
    grace: int = INT_MIN
    note: int = INT_MIN

    def moved(self, **kw) -> "Loc":
        d = dict(staff=self.staff, voice=self.voice, measure=self.measure, frac=self.frac, grace=self.grace,
                 note=self.note)
        d.update(kw)
        return Loc(**d)


def _parse_frac(text: str) -> Fraction:
    text = (text or "0").strip()
    if "/" in text:
        n, d = text.split("/")
        return Fraction(int(n), int(d))
    return Fraction(int(text))


def _rel_of(link: Optional[XNode]) -> Tuple[int, int, int, Fraction, int, int]:
    """Relative location inside a <prev>/<next> element (MuseScore's relative defaults)."""
    rel = [0, 0, 0, Fraction(0), INT_MIN, 0]
    loc = link.find("location") if link is not None else None
    if loc is None:
        return tuple(rel)
    for c in loc.children:
        v = c.text
        if c.tag == "staves":
            rel[0] = int(v)
        elif c.tag == "voices":
            rel[1] = int(v)
        elif c.tag == "measures":
            rel[2] = int(v)
        elif c.tag == "fractions":
            rel[3] = _parse_frac(v)
        elif c.tag == "grace":
            rel[4] = int(v)
        elif c.tag == "notes":
            rel[5] = int(v)
    return tuple(rel)


def _abs(rel, cur: Loc) -> Loc:
    return Loc(cur.staff + rel[0], cur.voice + rel[1], cur.measure + rel[2], cur.frac + rel[3], rel[4],
               cur.note + rel[5])


def _rel(cur: Loc, target: Loc) -> Tuple[int, int, int, Fraction, int, int]:
    return (target.staff - cur.staff, target.voice - cur.voice, target.measure - cur.measure,
            target.frac - cur.frac, target.grace, target.note - cur.note)


def _fmt_frac(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


def _link_xml(tag: str, rel, indent: str) -> str:
    """A <prev>/<next> element with its <location>, in MuseScore's layout."""
    i2, i3 = indent + "  ", indent + "    "
    lines = [f"{indent}<{tag}>", f"{i2}<location>"]
    for name, v, default in (("staves", rel[0], 0), ("voices", rel[1], 0), ("measures", rel[2], 0)):
        if v != default:
            lines.append(f"{i3}<{name}>{v}</{name}>")
    if rel[3] != 0:
        lines.append(f"{i3}<fractions>{_fmt_frac(rel[3])}</fractions>")
    if rel[4] != INT_MIN:
        lines.append(f"{i3}<grace>{rel[4]}</grace>")
    if rel[5] != 0:
        lines.append(f"{i3}<notes>{rel[5]}</notes>")
    lines.append(f"{i3}</location>")
    lines.append(f"{i2}</{tag}>")
    return "\n".join(lines) + "\n"


def _location_xml(frac: Fraction, indent: str) -> str:
    return f"{indent}<location>\n{indent}  <fractions>{_fmt_frac(frac)}</fractions>\n{indent}  </location>\n"


def _rest_xml(indent: str, duration_type: str, dots: int) -> str:
    i2 = indent + "  "
    lines = [f"{indent}<Rest>", f"{i2}<visible>0</visible>"]
    if dots:
        lines.append(f"{i2}<dots>{dots}</dots>")
    lines.append(f"{i2}<durationType>{duration_type}</durationType>")
    lines.append(f"{i2}</Rest>")
    return "\n".join(lines) + "\n"


# ==============================================================================
# the score: model (mscx.MscxReader) + element offsets
# ==============================================================================

def _main_mscx_name(z: zipfile.ZipFile) -> str:
    names = [n for n in z.namelist() if n.endswith(".mscx")]
    if not names:
        raise PlanError("no .mscx in the archive")
    if "META-INF/container.xml" in z.namelist():
        try:
            import xml.etree.ElementTree as ET
            c = ET.fromstring(z.read("META-INF/container.xml"))
            for rf in c.iter("rootfile"):
                fp = rf.get("full-path")
                if fp and fp.endswith(".mscx") and fp in names:
                    return fp
        except Exception:
            pass
    return names[0]


def replace_mscx(mscz: bytes, member: str, mscx: bytes) -> bytes:
    """The .mscz with its main .mscx replaced; every other member copied as it is."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(mscz)) as zin, zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            data = mscx if info.filename == member else zin.read(info.filename)
            ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            ni.compress_type = info.compress_type
            ni.external_attr = info.external_attr
            ni.create_system = info.create_system
            zout.writestr(ni, data)
    return out.getvalue()


class ScoreDoc:
    """The main .mscx of a score: its bytes and element offsets, tied to the reader's model
    through the xml paths the reader records (ChordRest.xml_path, Note.xml_index)."""

    def __init__(self, mscz: bytes, score: Score):
        with zipfile.ZipFile(io.BytesIO(mscz)) as z:
            self.member = _main_mscx_name(z)
            self.data = z.read(self.member)
            self.members = z.namelist()
        self.mscz = mscz
        self.score = score
        self.root = parse_xml(self.data)
        score_el = self.root.find("Score")
        if score_el is None:
            raise PlanError("no <Score> element")
        self.staff_nodes = [c for c in score_el.children if c.tag == "Staff"]
        self._measures: Dict[int, List[XNode]] = {}
        self.cr_by_path: Dict[tuple, ChordRest] = {}
        for m in score.measures:
            for seg in m.segments:
                for cr in seg.elements.values():
                    if cr.xml_path is not None:
                        self.cr_by_path[cr.xml_path] = cr
                    if cr.is_chord:
                        for g in cr.grace_all:
                            if g.xml_path is not None:
                                self.cr_by_path[g.xml_path] = g
        self.version = self.root.attrs.get("version", "")

    def measures(self, staff: int) -> List[XNode]:
        if staff not in self._measures:
            self._measures[staff] = self.staff_nodes[staff].findall("Measure")
        return self._measures[staff]

    def voices(self, staff: int, mi: int) -> List[XNode]:
        ms = self.measures(staff)
        return ms[mi].findall("voice") if mi < len(ms) else []

    def node(self, path: tuple) -> XNode:
        staff, mi, vi, ci = path
        return self.voices(staff, mi)[vi].children[ci]

    def note_node(self, note: Note) -> XNode:
        return self.node(note.chord.xml_path).children[note.xml_index]


def _main(chord: Chord) -> Chord:
    return chord.parent if chord.is_grace else chord


def _measure_of(chord: ChordRest):
    return (chord.parent if getattr(chord, "is_grace", False) and chord.is_chord else chord).segment.measure


def _read_index(note: Note) -> int:
    """The note's index in its chord as the reader resolves locations (pitch order, file order among
    unisons, at read time)."""
    notes = sorted(note.chord.notes, key=lambda n: n.xml_index)
    notes.sort(key=lambda n: n.pitch)
    return notes.index(note)


def _loc_of_chord(chord: ChordRest) -> Loc:
    m = _measure_of(chord)
    grace = chord.grace_index if (chord.is_chord and chord.is_grace) else INT_MIN
    return Loc(chord.track // VOICES, chord.track % VOICES, m.index, chord.tick - m.tick, grace, INT_MIN)


@dataclass
class _End:
    """One end of a connector written inside a Chord, Note or Rest."""
    node: XNode              # <Spanner type=...>
    type: str
    owner: object            # Chord / Rest / Note of the model
    cur: Loc
    prev: Optional[Loc]
    next: Optional[Loc]
    prev_node: Optional[XNode]
    next_node: Optional[XNode]
    partner_prev: Optional["_End"] = None
    partner_next: Optional["_End"] = None


def _connector_ends(doc: ScoreDoc) -> List[_End]:
    ends: List[_End] = []

    def add(sp: XNode, owner, cur: Loc):
        pn, nn = sp.find("prev"), sp.find("next")
        ends.append(_End(sp, sp.attrs.get("type", ""), owner, cur,
                         _abs(_rel_of(pn), cur) if pn is not None else None,
                         _abs(_rel_of(nn), cur) if nn is not None else None, pn, nn))

    for path, cr in doc.cr_by_path.items():
        node = doc.node(path)
        cur = _loc_of_chord(cr)
        for c in node.children:
            if c.tag == "Spanner":
                add(c, cr, cur)
        if cr.is_chord:
            for note in cr.notes:
                nn = node.children[note.xml_index]
                ncur = cur.moved(note=_read_index(note))
                for c in nn.children:
                    if c.tag == "Spanner":
                        add(c, note, ncur)
    by_cur: Dict[tuple, List[_End]] = {}
    for e in ends:
        by_cur.setdefault((e.type, e.cur), []).append(e)
    for e in ends:
        if e.next is not None:
            for b in by_cur.get((e.type, e.next), []):
                if b.prev is not None and b.prev == e.cur and b.partner_prev is None:
                    e.partner_next, b.partner_prev = b, e
                    break
    return ends


# ==============================================================================
# planning a score edit
# ==============================================================================

@dataclass
class _Unit:
    """A main chord with notes that change hands (its grace chords go along on a whole move)."""
    chord: Chord
    moving: List[Note]
    whole: bool
    target: int = -1               # staff
    voice: int = -1
    item: Optional["_Item"] = None


@dataclass
class _Item:
    """What goes into the target voice at one place: one unit, or the units of one tuplet."""
    staff: int
    measure: int                   # measure index
    a: Fraction                    # measure-relative range
    b: Fraction
    units: List[_Unit]
    tuplet: object = None          # the source Tuplet (model) when rebuilt in the target
    tuplet_node: Optional[XNode] = None
    members: List[ChordRest] = field(default_factory=list)
    voice: int = -1


@dataclass
class Cluster:
    """Units linked by ties: they move together into one voice, or not at all."""
    id: int
    items: List[_Item]
    requested: List[Note]
    notes: List[Note]                       # every note that changes hands (requested + carried)
    carried: Dict[int, str] = field(default_factory=dict)   # id(note) -> why it goes along
    voice: int = -1
    error: str = ""


@dataclass
class Pins:
    """What moved notes keep written into the score so that they sound as before: their velocity
    (dynamics that apply to one staff only) and their play events (legato and trill lines, swing and
    grace-note timing that depend on the staff or on the neighbouring chords)."""
    velocity: Dict[int, int] = field(default_factory=dict)          # id(note) -> MIDI velocity
    events: Dict[int, list] = field(default_factory=dict)           # id(note) -> [NoteEvent]

    def copy(self) -> "Pins":
        return Pins(dict(self.velocity), dict(self.events))

    def __len__(self) -> int:
        return len(set(self.velocity) | set(self.events))


@dataclass
class PlanResult:
    data: Optional[bytes]                   # the edited .mscx (None when nothing moves)
    clusters: List[Cluster]
    skipped: Dict[int, str]                 # id(requested note) -> reason
    cluster_of: Dict[int, int]              # id(note) -> cluster id, for every note that moves


class Planner:
    """Plans the .mscx edit that moves notes to the other hand's staff (see the module docs)."""

    def __init__(self, doc: ScoreDoc, hands: Dict[int, int]):
        self.doc = doc
        self.score = doc.score
        self.hands = hands                  # content staff -> 0 (right) / 1 (left), primary piano staves
        self._ends: Optional[List[_End]] = None
        self._voice_cache: Dict[tuple, "_VoiceInfo"] = {}

    # -- which staff a note moves to ---------------------------------------------
    def target_staff(self, staff: int, to_hand: int) -> int:
        if staff not in self.hands:
            raise PlanError("the note is not on a piano hand staff")
        if self.hands[staff] == to_hand:
            raise PlanError("the note is already on that hand's staff")
        part = self.score.staves[staff].part
        staves = [s.idx for s in part.staves]
        if len(staves) != 2 or any(s not in self.hands for s in staves):
            raise PlanError("the hands are not the two staves of one piano part")
        other = staves[1] if staves[0] == staff else staves[0]
        if self.hands[other] != to_hand:
            raise PlanError("the other staff of the part is not the other hand")
        return other

    def ends(self) -> List[_End]:
        if self._ends is None:
            self._ends = _connector_ends(self.doc)
        return self._ends

    # -- the plan ---------------------------------------------------------------------
    def plan(self, requests: Sequence[Tuple[Note, int]], pins: Optional[Pins] = None) -> PlanResult:
        """``requests``: (score note, hand it moves to: 0 right, 1 left); ``pins``: what moved notes
        keep written into the score.  Returns the edited .mscx and what was skipped."""
        pins = pins or Pins()
        skipped: Dict[int, str] = {}
        target_of: Dict[int, int] = {}          # id(note) -> target staff
        source_of: Dict[int, int] = {}
        req_notes: Dict[int, Note] = {}
        for note, hand in requests:
            try:
                if note.chord is None or note.chord.xml_path is None:
                    raise PlanError("the note has no place in the score file")
                staff = note.chord.track // VOICES
                target_of[id(note)] = self.target_staff(staff, hand)
                source_of[id(note)] = staff
                req_notes[id(note)] = note
            except PlanError as e:
                skipped[id(note)] = str(e)

        # closure: tie continuations and grace notes go along
        moving: Dict[int, Note] = dict(req_notes)
        carried: Dict[int, str] = {}
        parent: Dict[int, int] = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            parent[find(a)] = find(b)

        errors: Dict[int, str] = {}           # union root -> error (filled after the closure)
        early: Dict[int, str] = {}            # id(note) -> error found while closing
        changed = True
        while changed:
            changed = False
            for nid, n in list(moving.items()):
                tf = n.tie_for
                if tf is not None and tf.end is not None:
                    e = tf.end
                    union(nid, id(e))
                    if id(e) not in moving:
                        moving[id(e)] = e
                        carried[id(e)] = "tied to a moved note"
                        target_of[id(e)] = target_of[nid]
                        source_of[id(e)] = source_of[nid]
                        changed = True
                tb = n.tie_back
                if tb is not None and tb.start is not None and id(tb.start) not in moving:
                    early[nid] = "tied from a note that stays"
            by_main: Dict[int, List[Note]] = {}
            for nid, n in moving.items():
                by_main.setdefault(id(_main(n.chord)), []).append(n)
            for mid_, notes in by_main.items():
                main = _main(notes[0].chord)
                for n in notes:
                    union(id(n), id(main))
                ids = {id(n) for n in notes}
                if all(id(n) in ids for n in main.notes):
                    for g in main.grace_all:
                        for gn in g.notes:
                            if id(gn) not in moving:
                                moving[id(gn)] = gn
                                carried[id(gn)] = "grace note of a moved chord"
                                target_of[id(gn)] = target_of[id(notes[0])]
                                source_of[id(gn)] = source_of[id(notes[0])]
                                changed = True

        # units
        units: Dict[int, _Unit] = {}
        for nid, n in moving.items():
            main = _main(n.chord)
            u = units.get(id(main))
            if u is None:
                u = units[id(main)] = _Unit(main, [], False, target=target_of[nid])
            u.moving.append(n)
            if target_of[nid] != u.target:
                early[nid] = "the chord's notes would go to different staves"
        for u in units.values():
            main_ids = {id(n) for n in u.moving if not n.chord.is_grace}
            u.whole = all(id(n) in main_ids for n in u.chord.notes)
            err = self._unit_problem(u, source_of[id(u.moving[0])])
            if err:
                errors[find(id(u.chord))] = err
        for nid, err in early.items():
            errors.setdefault(find(nid), err)

        # items (tuplets are rebuilt as a whole in the target voice)
        items: Dict[tuple, _Item] = {}
        for u in units.values():
            if find(id(u.chord)) in errors:
                continue
            try:
                self._add_to_item(u, items)
            except PlanError as e:
                errors[find(id(u.chord))] = str(e)
        # clusters: everything linked by ties / chords / tuplets
        comp: Dict[int, List[_Item]] = {}
        for it in items.values():
            roots = {find(id(u.chord)) for u in it.units}
            for r in list(roots)[1:]:
                union(r, list(roots)[0])
        for it in items.values():
            comp.setdefault(find(id(it.units[0].chord)), []).append(it)
        for root in list(errors):
            r = find(root)
            if r != root:
                errors.setdefault(r, errors[root])

        clusters: List[Cluster] = []
        cluster_of: Dict[int, int] = {}
        for root in sorted({find(nid) for nid in moving},
                           key=lambda r: min((_order_key(n) for nid, n in moving.items() if find(nid) == r))):
            notes = sorted((n for nid, n in moving.items() if find(nid) == root), key=_order_key)
            cl = Cluster(len(clusters), sorted(comp.get(root, []), key=lambda it: (it.measure, it.a)),
                         [n for n in notes if id(n) in req_notes], notes,
                         {id(n): carried[id(n)] for n in notes if id(n) in carried})
            cl.error = errors.get(root, "")
            if not cl.error and not cl.items:
                cl.error = "nothing to move"
            clusters.append(cl)

        # every connector of a moving note or chord must connect (it is rewritten from both ends)
        for cl in clusters:
            if cl.error:
                continue
            owners = {id(n) for n in cl.notes}
            for it in cl.items:
                for u in it.units:
                    if u.whole:
                        owners.add(id(u.chord))
                        owners.update(id(g) for g in u.chord.grace_all)
                    else:
                        owners.update(id(n) for n in u.chord.notes)
            for end in self.ends():
                if id(end.owner) not in owners:
                    continue
                if (end.next_node is not None and end.partner_next is None) or \
                        (end.prev_node is not None and end.partner_prev is None):
                    cl.error = f"a {end.type or 'spanner'} at {where(self.score, _owner_chord(end.owner))} does not connect"
                    break
                if end.type == "Tie" and isinstance(end.owner, Note) and end.next_node is not None:
                    tf = end.owner.tie_for
                    if tf is None or tf.end is not end.partner_next.owner:
                        cl.error = f"a tie at {where(self.score, end.owner.chord)} could not be followed"
                        break

        # voices
        taken: Dict[tuple, List[Tuple[Fraction, Fraction]]] = {}
        for cl in clusters:
            if cl.error:
                continue
            for v in (1, 2, 3):
                if all(self._free(it, v, taken) for it in cl.items):
                    cl.voice = v
                    for it in cl.items:
                        it.voice = v
                        taken.setdefault((it.staff, it.measure, v), []).append((it.a, it.b))
                    break
            else:
                st = cl.items[0].staff
                cl.error = (f"no free voice in the {'upper' if self.hands.get(st) == 0 else 'lower'} staff "
                            f"at {where(self.score, cl.items[0].units[0].chord)}")

        ok = [cl for cl in clusters if not cl.error]
        for cl in clusters:
            for n in cl.requested:
                if cl.error:
                    skipped[id(n)] = cl.error
            if not cl.error:
                for n in cl.notes:
                    cluster_of[id(n)] = cl.id
        if not ok:
            return PlanResult(None, clusters, skipped, cluster_of)
        data = self._write(ok, pins)
        return PlanResult(data, clusters, skipped, cluster_of)

    # -- checks ---------------------------------------------------------------------------
    def _unit_problem(self, u: _Unit, source: int) -> str:
        c = u.chord
        if c.track // VOICES != source:
            return "a tied note continues on another staff"
        if c.xml_path is None:
            return "the chord has no place in the score file"
        if c.tuplet is not None and c.tuplet.parent is not None:
            return f"nested tuplet at {where(self.score, c)}"
        if c.tremolo_chord_type in ("first", "second"):
            return f"two-chord tremolo at {where(self.score, c)}"
        if c.lyrics:
            return f"the chord has lyrics ({where(self.score, c)})"
        if not u.whole:
            if any(n.chord.is_grace for n in u.moving):
                return f"grace notes move only with their whole chord ({where(self.score, c)})"
            if c.arpeggio is not None:
                return f"arpeggio on a chord that would be split ({where(self.score, c)})"
        m = c.segment.measure
        tk = ticks(c.tick)
        for st in (source, u.target):
            if st in m.measure_repeats or m.measure_repeat_count.get(st):
                return f"measure repeat at {where(self.score, c)}"
            if self.score.staves[st].pitch_offset(tk) != 0:
                return f"under an ottava line ({where(self.score, c)})"
        drawn = source + c.staff_move
        if drawn not in (source, u.target):
            return "the chord is drawn on a third staff"
        for g in c.grace_all:
            if u.whole and g.xml_path is None:
                return "a grace note has no place in the score file"
        return ""

    def _tuplet_node(self, chord: Chord) -> Tuple[XNode, List[ChordRest]]:
        """The <Tuplet> element of ``chord``'s (top level) tuplet and its members in stream order."""
        staff, mi, vi, ci = chord.xml_path
        voice = self.doc.voices(staff, mi)[vi]
        open_: List[XNode] = []
        members: Dict[int, List[ChordRest]] = {}
        found = None
        for i, c in enumerate(voice.children):
            if c.tag == "Tuplet":
                open_.append(c)
                members[id(c)] = []
            elif c.tag == "endTuplet":
                if open_:
                    open_.pop()
            elif c.tag in ("Chord", "Rest") and open_:
                cr = self.doc.cr_by_path.get((staff, mi, vi, i))
                if cr is not None and not (cr.is_chord and cr.is_grace):
                    members[id(open_[0])].append(cr)
                if i == ci:
                    if len(open_) != 1:
                        raise PlanError(f"nested tuplet at {where(self.score, chord)}")
                    found = open_[0]
        if found is None:
            raise PlanError(f"tuplet not found at {where(self.score, chord)}")
        return found, members[id(found)]

    def _add_to_item(self, u: _Unit, items: Dict[tuple, _Item]) -> None:
        c = u.chord
        m = c.segment.measure
        if c.tuplet is None:
            a = c.tick - m.tick
            it = _Item(u.target, m.index, a, a + c.actual_ticks(), [u])
            items[("c", id(c))] = it
            u.item = it
            return
        key = ("t", id(c.tuplet), u.target)
        it = items.get(key)
        if it is None:
            node, members = self._tuplet_node(c)
            if not members:
                raise PlanError("empty tuplet")
            a = members[0].tick - m.tick
            b = members[-1].tick + members[-1].actual_ticks() - m.tick
            it = items[key] = _Item(u.target, m.index, a, b, [], tuplet=c.tuplet, tuplet_node=node, members=members)
        it.units.append(u)
        u.item = it

    # -- free voices ---------------------------------------------------------------------
    def _voice(self, staff: int, mi: int, v: int) -> "_VoiceInfo":
        key = (staff, mi, v)
        if key not in self._voice_cache:
            self._voice_cache[key] = _VoiceInfo(self.doc, staff, mi, v)
        return self._voice_cache[key]

    def _free(self, it: _Item, v: int, taken) -> bool:
        for a, b in taken.get((it.staff, it.measure, v), []):
            if a < it.b and it.a < b:
                return False
        vi = self._voice(it.staff, it.measure, v)
        return vi.free(it.a, it.b)

    # -- writing ----------------------------------------------------------------------------
    def _write(self, clusters: List[Cluster], pins: Pins) -> bytes:
        doc = self.doc
        data = doc.data
        P = Patches(data)
        new_loc: Dict[int, Loc] = {}          # id(model element) -> location after the edit
        units = [u for cl in clusters for it in cl.items for u in it.units]
        unit_text: Dict[int, bytes] = {}

        for u in units:
            c = u.chord
            m = c.segment.measure
            frac = c.tick - m.tick
            T, v = u.target, u.item.voice
            cnode = doc.node(c.xml_path)
            source = c.track // VOICES
            if u.whole:
                chords = sorted(c.grace_all, key=lambda g: g.xml_path[3]) + [c]
                parts = []
                for ch in chords:
                    node = doc.node(ch.xml_path)
                    drawn = source + ch.staff_move
                    self._staff_move_patch(P, node, drawn - T)
                    base = Loc(T, v, m.index, frac, ch.grace_index if ch.is_grace else INT_MIN, INT_MIN)
                    new_loc[id(ch)] = base
                    for n in ch.notes:
                        new_loc[id(n)] = base.moved(note=_read_index(n))
                        self._pin_patches(P, doc.note_node(n), n, pins)
                    s, e = _span(data, node)
                    parts.append((s, e))
                # source: the chord becomes an invisible rest, its grace chords go
                s, e = _span(data, cnode)
                P.add(s, e, _rest_xml(_indent(data, cnode), c.duration_type, c.dots))
                for g in c.grace_all:
                    P.remove(doc.node(g.xml_path))
                u._parts = parts
            else:
                stay = [n for n in sorted(c.notes, key=lambda n: n.xml_index) if id(n) not in {id(x) for x in u.moving}]
                move = [n for n in sorted(c.notes, key=lambda n: n.xml_index) if id(n) in {id(x) for x in u.moving}]
                base_src = _loc_of_chord(c)
                for n in stay:
                    new_loc[id(n)] = base_src.moved(note=_stable_index(stay, n))
                base = Loc(T, v, m.index, frac, INT_MIN, INT_MIN)
                for n in move:
                    new_loc[id(n)] = base.moved(note=_stable_index(move, n))
                    self._pin_patches(P, doc.note_node(n), n, pins)
                    P.remove(doc.note_node(n))
                u._move_notes = move

        # connectors touching anything that moved or was renumbered
        self._rewrite_connectors(P, new_loc)

        # the new chords' text (after the inner patches above are known)
        for u in units:
            c = u.chord
            cnode = doc.node(c.xml_path)
            if u.whole:
                unit_text[id(u)] = b"".join(P.text(s, e) for s, e in u._parts)
            else:
                unit_text[id(u)] = self._split_chord_xml(P, cnode, u._move_notes, c.track // VOICES + c.staff_move - u.target)

        # insert into the target voices
        by_voice: Dict[tuple, List[_Item]] = {}
        for cl in clusters:
            for it in cl.items:
                by_voice.setdefault((it.staff, it.measure, it.voice), []).append(it)
        for (staff, mi, v), its in sorted(by_voice.items()):
            vi = self._voice(staff, mi, v)
            blocks = []
            for it in sorted(its, key=lambda x: x.a):
                blocks.append((it.a, it.b, self._item_xml(P, it, unit_text, vi.item_indent())))
            vi.insert(P, blocks)
        return P.result()

    def _item_xml(self, P: Patches, it: _Item, unit_text: Dict[int, bytes], indent: str) -> bytes:
        if it.tuplet is None:
            return unit_text[id(it.units[0])]
        data = self.doc.data
        by_chord = {id(u.chord): u for u in it.units}
        s, e = _span(data, it.tuplet_node)
        out = [P.text(s, e, _without_eids(data, it.tuplet_node))]
        for cr in it.members:
            u = by_chord.get(id(cr))
            if u is not None:
                out.append(unit_text[id(u)])
            else:
                out.append(_rest_xml(indent, cr.duration_type, cr.dots).encode())
        out.append(f"{indent}<endTuplet/>\n".encode())
        return b"".join(out)

    def _split_chord_xml(self, P: Patches, cnode: XNode, move: List[Note], staff_move: int) -> bytes:
        """A new chord for the moving notes of a split: the chord's properties and articulations
        (without ids, layout overrides, slurs or beam settings), the moving notes, a staffMove."""
        data = self.doc.data
        ind = _indent(data, cnode)
        cind = _child_indent(data, cnode)
        move_idx = {n.xml_index for n in move}
        skip = {"eid", "BeamMode", "staffMove", "Spanner", "Lyrics", "Stem", "Hook", "StemDirection", "noStem",
                "StemSlash", "showStemSlash", "offset", "linkedMain", "Beam"}
        out = [f"{ind}<Chord>\n".encode()]
        for i, ch in enumerate(cnode.children):
            if ch.tag == "Note":
                if i in move_idx:
                    s, e = _span(data, ch)
                    out.append(P.text(s, e))
                continue
            if ch.tag in skip:
                continue
            if ch.tag == "durationType" and staff_move:
                out.append(f"{cind}<staffMove>{staff_move}</staffMove>\n".encode())
            s, e = _span(data, ch)
            out.append(P.text(s, e, _without_eids(data, ch)))
        out.append(f"{cind}</Chord>\n".encode())
        return b"".join(out)

    def _staff_move_patch(self, P: Patches, node: XNode, value: int) -> None:
        data = self.doc.data
        sm = node.find("staffMove")
        if sm is not None:
            s, e = _span(data, sm)
            if value == 0:
                P.add(s, e, b"")
            elif int(sm.text or 0) != value:
                P.add(s, e, f"{_indent(data, sm)}<staffMove>{value}</staffMove>\n")
            return
        if value == 0:
            return
        dt = node.find("durationType")
        if dt is None:
            raise PlanError("chord without a durationType")
        s = _line_start(data, dt.start)
        P.add(s, s, f"{_indent(data, dt)}<staffMove>{value}</staffMove>\n")

    def _pin_patches(self, P: Patches, nnode: XNode, note: Note, pins: Pins) -> None:
        data = self.doc.data
        velocity = pins.velocity.get(id(note))
        if velocity is not None and nnode.find("velocity") is None:
            after = nnode.find("tpc2") or nnode.find("tpc") or nnode.find("pitch")
            if after is None:
                raise PlanError("note without a pitch")
            _s, e = _span(data, after)
            P.add(e, e, f"{_indent(data, after)}<velocity>{velocity}</velocity>\n")
        events = pins.events.get(id(note))
        if events is not None and nnode.find("Events") is None:
            first = next((c for c in nnode.children if c.tag not in ("eid", "linkedMain")), None)
            pos = _line_start(data, first.start if first is not None else nnode.close)
            ind = _child_indent(data, nnode)
            lines = [f"{ind}<Events>"]
            for ev in events:
                lines.append(f"{ind}  <Event>")
                if ev.pitch:
                    lines.append(f"{ind}    <pitch>{ev.pitch}</pitch>")
                if ev.ontime:
                    lines.append(f"{ind}    <ontime>{ev.ontime}</ontime>")
                if ev.len != 1000:
                    lines.append(f"{ind}    <len>{ev.len}</len>")
                lines.append(f"{ind}    </Event>")
            lines.append(f"{ind}  </Events>")
            P.add(pos, pos, "\n".join(lines) + "\n")

    def _rewrite_connectors(self, P: Patches, new_loc: Dict[int, Loc]) -> None:
        data = self.doc.data
        for end in self.ends():
            me = self._new_cur(end, new_loc)
            for tag, partner, link, old_abs in (("next", end.partner_next, end.next_node, end.next),
                                                ("prev", end.partner_prev, end.prev_node, end.prev)):
                if link is None:
                    continue
                if partner is None:
                    continue                      # checked per cluster before writing
                other = self._new_cur(partner, new_loc)
                if other == partner.cur and me == end.cur:
                    continue
                rel = _rel(me, other)
                if rel == _rel(end.cur, old_abs) and _abs(rel, me) == other:
                    continue
                s, e = _span(data, link)
                P.add(s, e, _link_xml(tag, rel, _indent(data, link)))

    @staticmethod
    def _new_cur(end: _End, new_loc: Dict[int, Loc]) -> Loc:
        loc = new_loc.get(id(end.owner))
        if loc is None:
            return end.cur
        if isinstance(end.owner, Note):
            return loc
        return loc.moved(note=INT_MIN)


def _owner_chord(owner) -> ChordRest:
    return owner.chord if isinstance(owner, Note) else owner


def _stable_index(notes: List[Note], note: Note) -> int:
    """Index of ``note`` among ``notes`` (file order) once sorted by pitch like the reader does."""
    s = sorted(notes, key=lambda n: n.xml_index)
    s.sort(key=lambda n: n.pitch)
    return s.index(note)


def _order_key(n: Note):
    c = _main(n.chord)
    return (c.tick, c.track, n.chord.grace_index if n.chord.is_grace else 99, n.pitch)


class _VoiceInfo:
    """One voice of one measure of a staff: what occupies it, and where to write into it."""

    def __init__(self, doc: ScoreDoc, staff: int, mi: int, v: int):
        self.doc = doc
        self.staff, self.mi, self.v = staff, mi, v
        voices = doc.voices(staff, mi)
        self.count = len(voices)
        self.node = voices[v] if v < len(voices) else None
        self.measure = doc.score.measures[mi]
        self.complex = False
        self.items: List[list] = []          # [kind, node, position before, position after, chord/rest]
        if self.node is None:
            self.complex = v != self.count     # only the next voice can be added
            self.end = Fraction(0)
            return
        pos = Fraction(0)
        for i, c in enumerate(self.node.children):
            if c.tag == "location":
                if any(c.find(t) is not None for t in ("staves", "voices", "measures", "grace", "notes")):
                    self.complex = True
                d = _parse_frac(c.value("fractions", "0"))
                self.items.append(["loc", c, pos, pos + d, None])
                pos += d
            elif c.tag in ("tick", "MeasureRepeat", "RepeatMeasure"):
                self.complex = True
                self.items.append(["other", c, pos, pos, None])
            elif c.tag in ("Chord", "Rest"):
                cr = doc.cr_by_path.get((staff, mi, v, i))
                if cr is None:
                    self.complex = True
                    self.items.append(["other", c, pos, pos, None])
                    continue
                if cr.is_chord and cr.is_grace:
                    self.items.append(["grace", c, pos, pos, cr])
                    continue
                start = cr.tick - self.measure.tick
                if start != pos:
                    self.complex = True
                end = start + cr.actual_ticks()
                self.items.append(["cr", c, start, end, cr])
                pos = end
            else:
                self.items.append(["other", c, pos, pos, None])
        self.end = pos

    def item_indent(self) -> str:
        data = self.doc.data
        if self.node is not None and self.node.children:
            return _indent(data, self.node.children[0])
        ref = self.doc.voices(self.staff, self.mi)[0]
        return _indent(data, ref) + "  "

    def free(self, a: Fraction, b: Fraction) -> bool:
        if self.complex:
            return False
        for it in self.items:
            if it[0] != "cr":
                continue
            s, e, cr = it[2], it[3], it[4]
            if not (s < b and e > a):
                continue
            if cr.is_chord or cr.tuplet is not None:
                return False
            visible = cr.props.get("visible", True)
            if visible and not (a <= s and e <= b):
                return False
        return True

    def insert(self, P: Patches, blocks: List[Tuple[Fraction, Fraction, bytes]]) -> None:
        """Write ``blocks`` (measure-relative ranges with their XML) into this voice; rests in the
        way become gaps, every other element keeps its position."""
        data = self.doc.data
        ind = self.item_indent()
        if self.node is None:
            last = self.doc.voices(self.staff, self.mi)[-1]
            _s, e = _span(data, last)
            vind = _indent(data, last)
            body = self._emit(blocks, Fraction(0), None, ind)
            P.add(e, e, f"{vind}<voice>\n".encode() + body + f"{ind}</voice>\n".encode())
            return
        if self.node.close is None:            # <voice/>
            s, e = _span(data, self.node)
            vind = _indent(data, self.node)
            body = self._emit(blocks, Fraction(0), None, ind)
            P.add(s, e, f"{vind}<voice>\n".encode() + body + f"{ind}</voice>\n".encode())
            return
        # rests overlapping a block become gaps (the items themselves are shared by later plans)
        kinds = [it[0] for it in self.items]
        gapped: Set[int] = set()
        for k, it in enumerate(self.items):
            if it[0] == "cr" and any(it[2] < b and it[3] > a for a, b, _t in blocks):
                kinds[k] = "loc"
                gapped.add(k)
        # runs of consecutive gap items that only move forward
        runs: List[List[int]] = []
        for k, it in enumerate(self.items):
            if kinds[k] == "loc" and it[3] >= it[2]:
                if runs and runs[-1][-1] == k - 1:
                    runs[-1].append(k)
                else:
                    runs.append([k])
        placed: Dict[int, List] = {}          # run index -> blocks
        rest_blocks = []
        for blk in blocks:
            a, b, _t = blk
            for ri, run in enumerate(runs):
                if self.items[run[0]][2] <= a and b <= self.items[run[-1]][3]:
                    placed.setdefault(ri, []).append(blk)
                    break
            else:
                rest_blocks.append(blk)
        for ri, run in enumerate(runs):
            if ri not in placed and not any(k in gapped for k in run):
                continue
            first, last = self.items[run[0]], self.items[run[-1]]
            s = _span(data, first[1])[0]
            e = _span(data, last[1])[1]
            if ri in placed:
                text = self._emit(placed[ri], first[2], last[3], ind)
            else:
                text = b"".join(_location_xml(self.items[k][3] - self.items[k][2], ind).encode() if k in gapped
                                else P.text(*_span(data, self.items[k][1])) for k in run)
            P.add(s, e, text)
        if not rest_blocks:
            return
        rest_blocks.sort(key=lambda x: x[0])
        after = [it for k, it in enumerate(self.items) if kinds[k] == "cr" and it[2] >= rest_blocks[-1][1]]
        if not after:
            pos = self.close_line()           # append at the end of the voice
            P.add(pos, pos, self._emit(rest_blocks, self.end, None, ind))
            return
        nxt = after[0]                        # before the next chord or rest, returning to its position
        s = _span(data, nxt[1])[0]
        P.add(s, s, self._emit(rest_blocks, nxt[2], nxt[2], ind))

    def close_line(self) -> int:
        return _line_start(self.doc.data, self.node.close)

    @staticmethod
    def _emit(blocks, start: Fraction, back: Optional[Fraction], ind: str) -> bytes:
        out = []
        cur = start
        for a, b, text in sorted(blocks, key=lambda x: x[0]):
            if a != cur:
                out.append(_location_xml(a - cur, ind).encode())
            out.append(text)
            cur = b
        if back is not None and back != cur:
            out.append(_location_xml(back - cur, ind).encode())
        return b"".join(out)


# ==============================================================================
# describing notes
# ==============================================================================

def note_name(midi: int) -> str:
    return f"{_NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def where(score: Score, chord: ChordRest) -> str:
    """"m12 beat 3 1/2" (measure number = position in the score, beat in time-signature units)."""
    c = _main(chord) if chord.is_chord else chord
    m = c.segment.measure
    rt = c.tick - m.tick
    den = m.timesig_nd[1] or 4
    beat = 1 + rt * den
    whole = int(beat)
    rest = beat - whole
    b = f"{whole}" + (f" {rest.numerator}/{rest.denominator}" if rest else "")
    return f"m{m.index + 1} beat {b}"


# ==============================================================================
# comparing renders
# ==============================================================================

_HAND_FIELDS = ("noteMeasureInd", "id")


def _flat(doc: dict, hand: str) -> List[dict]:
    return [n for m in doc.get("tracksV2", {}).get(hand, []) for n in m.get("notes", [])]


def _nkey(n: dict) -> str:
    return json.dumps({k: v for k, v in n.items() if k not in _HAND_FIELDS}, sort_keys=True)


def _fmt_value(v) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _describe_diff(missing: Counter, extra: Counter, limit: int = 3) -> str:
    """"C5 at tick 960: accent 1 -> 0, velocity 0.567 -> 0.472; E4 at tick 0 is gone"."""
    miss = [json.loads(k) for k in missing.elements()]
    ext = [json.loads(k) for k in extra.elements()]
    out = []
    for m in miss:
        same = next((x for x in ext if x["ticksStart"] == m["ticksStart"] and x["note"] == m["note"]), None)
        label = f"{note_name(m['note'])} at tick {m['ticksStart']}"
        if same is None:
            out.append(f"{label} is gone")
            continue
        ext.remove(same)
        fields = [k for k in ("accent", "velocity", "durationTicks") if m.get(k) != same.get(k)] or \
                 sorted(k for k in m if m.get(k) != same.get(k))
        out.append(label + ": " + ", ".join(f"{k} {_fmt_value(m.get(k))} -> {_fmt_value(same.get(k))}"
                                            for k in fields))
    out += [f"{note_name(x['note'])} at tick {x['ticksStart']} is new" for x in ext]
    more = len(out) - limit
    return "; ".join(out[:limit]) + (f" (and {more} more)" if more > 0 else "")


def compare_songs(base: dict, new: dict, moves: Dict[Tuple[str, int], str]) -> Tuple[List[str], Dict[str, Counter]]:
    """Problems when ``new`` is not ``base`` with exactly the notes in ``moves`` ((hand, index in
    base tracksV2[hand]) -> new hand) in the other hand; also the per-hand notes that are missing."""
    problems: List[str] = []
    for k in sorted(set(base) | set(new)):
        if k in ("tracksV2", "supportingTracks"):
            continue
        if base.get(k) != new.get(k):
            problems.append(f"{k} changed")
    missing_all: Dict[str, Counter] = {}
    extra_all: Dict[str, Counter] = {}
    for h in HANDS:
        expected: Counter = Counter()
        for oh in HANDS:
            for i, n in enumerate(_flat(base, oh)):
                if moves.get((oh, i), oh) == h:
                    expected[_nkey(n)] += 1
        got = Counter(_nkey(n) for n in _flat(new, h))
        missing, extra = expected - got, got - expected
        missing_all[h], extra_all[h] = missing, extra
        if missing or extra:
            problems.append(f"{h} hand: " + _describe_diff(missing, extra))
    # supportingTracks follow the hands (track 0 = right) when both hands have notes
    bs, ns = base.get("supportingTracks", []), new.get("supportingTracks", [])
    if len(bs) == len(ns) == 2 and all(_flat(base, h) for h in HANDS) and all(_flat(new, h) for h in HANDS):
        for ti, h in enumerate(HANDS):
            expected = Counter()
            for oi, oh in enumerate(HANDS):
                for i, n in enumerate(bs[oi]["notes"]):
                    if moves.get((oh, i), oh) == h:
                        expected[json.dumps(n, sort_keys=True)] += 1
            got = Counter(json.dumps(n, sort_keys=True) for n in ns[ti]["notes"])
            if got != expected:
                problems.append(f"supportingTracks[{ti}] differs")
            if {k: v for k, v in bs[ti].items() if k != "notes"} != {k: v for k, v in ns[ti].items() if k != "notes"}:
                problems.append(f"supportingTracks[{ti}] settings changed")
    elif len(bs) != len(ns):
        problems.append("supportingTracks changed")
    return problems, missing_all


# ==============================================================================
# reviewing one song
# ==============================================================================

@dataclass
class EditResult:
    sidecar: str
    edit: HandEdit
    status: str                   # move | already | skip | conflict | stale
    reason: str = ""
    where: str = ""
    passes: int = 1               # how many notes of the song the source note plays (repeats)
    velocity_pinned: bool = False
    events_pinned: bool = False


@dataclass
class ScoreReview:
    song: str                                      # song JSON file name
    sidecars: List[Sidecar] = field(default_factory=list)
    rel: str = ""                                  # score, relative to the scores folder
    score_path: str = ""
    results: List[EditResult] = field(default_factory=list)
    carried: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: str = ""
    compat: Optional[dict] = None
    score_sha256: str = ""
    new_mscz: Optional[bytes] = None
    new_json: Optional[bytes] = None
    moved_notes: int = 0                           # notes of the song that change hands
    to_right: int = 0
    to_left: int = 0

    @property
    def ready(self) -> bool:
        return self.new_mscz is not None and self.moved_notes > 0

    def resolved_sidecars(self) -> List[Sidecar]:
        """Sidecars whose every edit is now in the score (applied here or already)."""
        out = []
        for sc in self.sidecars:
            rs = [r for r in self.results if r.sidecar == sc.name]
            if sc.error or any(r.status not in ("move", "already") for r in rs):
                continue
            out.append(sc)
        return out


@dataclass
class _Req:
    src: Note
    to: str
    edits: List[Tuple[Sidecar, HandEdit]]
    instances: List[Tuple[str, int]]


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hands_of(midi, orchestra: bool) -> Dict[int, int]:
    from .pvjson import TRACK_PRIMARY, classify_tracks, piano_hand
    piano, _orch = classify_tracks(midi, orchestra)
    return {t: piano_hand(piano, t, tt) for t, tt in piano if tt == TRACK_PRIMARY}


def review_song(lib, song: str, sidecars: List[Sidecar], all_repeats: bool = False,
                log: Callable[[str], None] = lambda s: None) -> ScoreReview:
    """Map the sidecars' edits of ``song`` to the score, plan, write to a temporary copy and verify.
    Nothing outside a temporary folder is written."""
    from .convert import convert_mscz
    from .render import Compat
    rv = ScoreReview(song=song, sidecars=list(sidecars))
    for sc in sidecars:
        if sc.error:
            rv.notes.append(f"{sc.name}: {sc.error}")
        for w in sc.warnings:
            rv.notes.append(f"{sc.name}: {w}")
    edits = [(sc, e) for sc in sidecars if not sc.error for e in sc.edits]
    if not edits:
        rv.error = rv.error or "no edits"
        return rv
    rel = lib.manifest.owner_of(song)
    if rel is None:
        rv.error = f"{song} is not in the library (no score renders it)"
        return rv
    entry = lib.manifest.entries[rel]
    rv.rel = rel
    rv.score_path = os.path.join(lib.cfg.scores, rel)
    out_path = lib.out_path(song)
    if not os.path.exists(rv.score_path):
        rv.error = f"score not found: {rv.score_path}"
        return rv
    if not os.path.exists(out_path):
        rv.error = f"library file not found: {out_path}"
        return rv
    with open(out_path, "rb") as f:
        lib_bytes = f.read()
    with open(rv.score_path, "rb") as f:
        mscz = f.read()
    rv.score_sha256 = _sha256(mscz)
    rv.compat = lib._verify_compat(entry)
    compat = Compat.from_dict(rv.compat)
    orchestra, simplified = lib.cfg.orchestra, lib.cfg.simplified

    with zipfile.ZipFile(io.BytesIO(mscz)) as z:
        names = z.namelist()
    if any(n.startswith("Excerpts/") for n in names):
        rv.error = "the score has parts (excerpts); edit it in MuseScore"
        return rv
    try:
        base = convert_mscz(rv.score_path, compat, orchestra, simplified, keep_midi=True, provenance=True)
    except Exception as e:                                   # a broken score must not stop the others
        rv.error = f"render failed: {type(e).__name__}: {e}"
        return rv
    score: Score = base.score
    if score.msc_version < 400:
        rv.error = "MuseScore 3 file: open and save it in MuseScore 4 first"
        return rv
    if any(not st.linked_primary for st in score.staves):
        rv.error = "the score has linked staves; edit it in MuseScore"
        return rv
    base_doc = json.loads(base.data)
    lib_doc = json.loads(lib_bytes)
    lib_md5 = _md5(lib_bytes)
    if base.data != lib_bytes:
        if entry.get("reproducible") is False:
            rv.error = ("the library keeps a pinned output for this score that the renderer does not reproduce: "
                        f"`build --force {rel}` replaces it with a render; deploy, then record again")
        else:
            rv.error = ("the library file is not what the score renders to now (the score changed since the "
                        "last build): run `sync`, then record again")
        return rv

    # -- map every edit to its source note
    lib_flat = {h: _flat(lib_doc, h) for h in HANDS}
    index: Dict[str, Dict[tuple, int]] = {}
    present: Dict[str, Set[tuple]] = {}
    for h in HANDS:
        seen: Counter = Counter()
        index[h] = {}
        present[h] = set()
        for i, n in enumerate(lib_flat[h]):
            k = (n["ticksStart"], n["note"])
            index[h][k + (seen[k],)] = i
            seen[k] += 1
            present[h].add(k)
    prov = base.provenance
    instances: Dict[int, List[Tuple[str, int]]] = {}
    for h in HANDS:
        for i, ns in enumerate(prov[h]):
            if ns.note is not None:
                instances.setdefault(id(ns.note), []).append((h, i))
    reqs: Dict[int, _Req] = {}
    results: Dict[int, EditResult] = {}
    for sc, e in edits:
        r = EditResult(sc.name, e, "skip")
        results[id(e)] = r
        pos = index[e.from_hand].get((e.ticks, e.midi, e.occurrence))
        if pos is None:
            if (e.ticks, e.midi) in present[e.to_hand]:
                r.status, r.reason = "already", "already in the score"
            else:
                r.status, r.reason = "stale", "the note is not in the song any more"
            continue
        n = lib_flat[e.from_hand][pos]
        if sc.song_md5 and sc.song_md5 != lib_md5:
            if (e.start is not None and abs(n.get("start", 0) - e.start) > 0.002) or \
                    (e.measure is not None and n.get("measureInd") != e.measure):
                r.status, r.reason = "stale", "the song changed since this was recorded"
                continue
        src = prov[e.from_hand][pos].note
        if src is None:
            r.reason = "chord-symbol playback, not a note of the score"
            continue
        r.where = f"{where(score, src.chord)}  {note_name(e.midi)}"
        q = reqs.get(id(src))
        if q is None:
            q = reqs[id(src)] = _Req(src, e.to_hand, [], instances.get(id(src), []))
        q.edits.append((sc, e))
    for q in list(reqs.values()):
        rs = [results[id(e)] for _sc, e in q.edits]
        passes = len(q.instances)
        for r in rs:
            r.passes = passes
        tos = {e.to_hand for _sc, e in q.edits}
        hands = {h for h, _i in q.instances}
        edited = {(e.from_hand, index[e.from_hand][(e.ticks, e.midi, e.occurrence)]) for _sc, e in q.edits}
        reason = ""
        if len(tos) > 1:
            reason = "the passes of this note were recorded in different hands"
        elif len(hands) > 1:
            reason = "the song plays this note in both hands on different passes"
        elif len(edited) < passes and not all_repeats:
            reason = (f"recorded on {len(edited)} of {passes} passes (repeats); the score has one note for all: "
                      "record the others too, or pass --all-repeats")
        if reason:
            for r in rs:
                r.status, r.reason = "conflict", reason
            del reqs[id(q.src)]
    rv.results = [results[id(e)] for _sc, e in edits]
    if not reqs:
        return rv

    # -- plan, write to a temporary copy, re-render, compare
    doc = ScoreDoc(mscz, score)
    planner = Planner(doc, _hands_of(base.midi, orchestra))
    req_list = sorted(reqs.values(), key=lambda q: _order_key(q.src))
    requests = [(q.src, HANDS.index(q.to)) for q in req_list]
    tmpdir = tempfile.mkdtemp(prefix="pv-hands-")
    try:
        attempt = _Attempt(planner, requests, base_doc, prov, instances, rv.score_path, doc, compat, orchestra,
                           simplified, tmpdir, log)
        final = attempt.run()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    plan, pins, new, full = final.plan, final.pins, final.new, final.full
    ok_plan = plan is not None and bool(final.accepted) and not final.problems and final.mscx is not None
    for q in req_list:
        cid = full.cluster_of.get(id(q.src)) if full is not None else None
        for _sc, e in q.edits:
            r = results[id(e)]
            if ok_plan and cid is not None and cid in final.accepted:
                r.status, r.reason = "move", ""
                r.velocity_pinned = id(q.src) in pins.velocity
                r.events_pinned = id(q.src) in pins.events
            else:
                r.reason = ((full.skipped.get(id(q.src)) if full is not None else "") or final.rejected.get(cid, "")
                            or final.reason or "not written")
    if ok_plan:
        for cl in plan.clusters:
            if cl.id not in final.accepted:
                continue
            for n in cl.notes:
                if id(n) in cl.carried:
                    rv.carried.append(f"{where(score, n.chord)}  {note_name(n.pitch)}  (goes along: "
                                      f"{cl.carried[id(n)]})")
        moved = final.moves
        rv.moved_notes = len(moved)
        rv.to_right = sum(1 for h in moved.values() if h == "right")
        rv.to_left = rv.moved_notes - rv.to_right
        rv.new_mscz = replace_mscx(mscz, doc.member, final.mscx)
        rv.new_json = new
        if pins.velocity:
            rv.notes.append(f"{len(pins.velocity)} moved note(s) keep their velocity written into the score "
                            "(dynamics there apply to one staff only)")
        if pins.events:
            rv.notes.append(f"{len(pins.events)} moved note(s) keep their length and timing written into the "
                            "score as play events (legato, swing or grace timing differs on the other staff)")
    return rv


@dataclass
class _Outcome:
    plan: Optional[PlanResult] = None
    mscx: Optional[bytes] = None
    new: Optional[bytes] = None
    pins: Pins = field(default_factory=Pins)
    moves: Dict[Tuple[str, int], str] = field(default_factory=dict)
    accepted: Set[int] = field(default_factory=set)
    rejected: Dict[int, str] = field(default_factory=dict)
    reason: str = ""
    problems: List[str] = field(default_factory=list)
    full: Optional[PlanResult] = None       # the plan of every request (its cluster ids are the reference)


class _Attempt:
    """Plan -> temporary .mscz -> render -> compare, narrowing down to the clusters that verify."""

    def __init__(self, planner: Planner, requests, base_doc, prov, instances, score_path, doc, compat, orchestra,
                 simplified, tmpdir, log):
        self.planner, self.requests, self.base_doc, self.prov = planner, requests, base_doc, prov
        self.instances, self.score_path, self.doc, self.compat = instances, score_path, doc, compat
        self.orchestra, self.simplified, self.tmpdir, self.log = orchestra, simplified, tmpdir, log
        self.runs = 0

    def _try(self, clusters: Optional[Set[int]], pins: Pins, full_plan: PlanResult) -> _Outcome:
        from .convert import convert_mscz
        o = _Outcome(pins=pins.copy())
        if clusters is None:
            reqs = self.requests
        else:
            keep = {id(n) for cl in full_plan.clusters if cl.id in clusters for n in cl.requested}
            reqs = [r for r in self.requests if id(r[0]) in keep]
        plan = self.planner.plan(reqs, pins)
        o.plan = plan
        if plan.data is None:
            o.reason = "nothing could be written"
            return o
        o.mscx = plan.data
        # same file and folder name as the score: titles and composers can fall back to them
        folder = os.path.join(self.tmpdir, f"edit{self.runs}", os.path.basename(os.path.dirname(self.score_path)))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, os.path.basename(self.score_path))
        self.runs += 1
        with open(path, "wb") as f:
            f.write(replace_mscx(self.doc.mscz, self.doc.member, plan.data))
        moves: Dict[Tuple[str, int], str] = {}
        for cl in plan.clusters:
            if cl.error:
                continue
            for n in cl.notes:
                for h, i in self.instances.get(id(n), []):
                    moves[(h, i)] = HANDS[1 - HANDS.index(h)]
        o.moves = moves
        self.log(f"    render {self.runs}: {len(moves)} note(s) changing hands")
        try:
            new = convert_mscz(path, self.compat, self.orchestra, self.simplified, provenance=True)
        except Exception as e:
            o.problems = [f"the edited score does not render: {type(e).__name__}: {e}"]
            return o
        o.new = new.data
        problems = _structure_problems(self.doc.score, new.score)
        p2, missing = compare_songs(self.base_doc, json.loads(new.data), moves)
        o.problems = problems + p2
        o._missing = missing
        o._new_doc = json.loads(new.data)
        o.accepted = {cl.id for cl in plan.clusters if not cl.error}
        return o

    def _pins_for(self, o: _Outcome, plan: PlanResult) -> Pins:
        """Moved notes that differ only in velocity or length: pin what they had (all passes alike)."""
        new_doc = o._new_doc
        pins = o.pins.copy()
        moving = {id(n): n for cl in plan.clusters if not cl.error for n in cl.notes}
        for n in list(moving.values()):
            inst = self.instances.get(id(n), [])
            if not inst:
                continue
            to = HANDS[1 - HANDS.index(inst[0][0])]
            want = [_flat(self.base_doc, h)[i] for h, i in inst]
            if not any(o._missing[to].get(_nkey(w)) for w in want):
                continue
            diffs: Set[str] = set()
            for w in want:
                got = [x for x in _flat(new_doc, to) if x["ticksStart"] == w["ticksStart"] and x["note"] == w["note"]]
                close = [x for x in got if _differs_in(x, w) <= _PINNABLE]
                if not close:
                    diffs = {"other"}
                    break
                diffs |= min((_differs_in(x, w) for x in close), key=len)
            if "other" in diffs or not diffs:
                continue
            if "velocity" in diffs:
                velos = {self.prov[h][i].velocity for h, i in inst}
                if len(velos) == 1 and n.user_velocity == 0:
                    pins.velocity[id(n)] = velos.pop()
            if diffs & _LENGTH_FIELDS:
                for m in _event_group(n, moving):
                    if m.chord.play_event_type == "auto" and all(_plain_event(ev) for ev in m.play_events):
                        pins.events.setdefault(id(m), [NoteEvent(pitch=ev.pitch, ontime=ev.ontime, len=ev.len)
                                                       for ev in m.play_events])
        return pins

    def run(self) -> _Outcome:
        full = self.planner.plan(self.requests, Pins())
        if full.data is None:
            return _Outcome(plan=full, full=full, reason="nothing could be written")
        o = self._run(full)
        o.full = full
        return o

    def _run(self, full: PlanResult) -> _Outcome:
        pins = Pins()
        o = self._with_pins(self._try(None, pins, full), None, full, pins)
        if not o.problems:
            return o
        # narrow down: halve the groups of clusters that do not verify, then all good ones together
        rejected: Dict[int, str] = {}
        ok = self._narrow([cl.id for cl in full.clusters if not cl.error], pins, full, rejected, tried=o)
        result = _Outcome(plan=full, pins=pins, rejected=rejected)
        if not ok:
            result.reason = "no change verifies"
            return result
        both = self._try(set(ok), pins, full)
        if not both.problems:
            both.rejected = rejected
            both.plan = self._align(both, full)
            return both
        # interactions: add one cluster at a time
        good: List[int] = []
        last = None
        for cid in ok:
            t = self._try(set(good + [cid]), pins, full)
            if t.problems:
                rejected[cid] = "the re-rendered song would change together with the others: " + t.problems[0]
            else:
                good.append(cid)
                last = t
        if last is None:
            result.reason = "no change verifies"
            return result
        last.rejected = rejected
        last.plan = self._align(last, full)
        return last

    def _with_pins(self, o: _Outcome, ids: Optional[Set[int]], full: PlanResult, pins: Pins) -> _Outcome:
        """A failed attempt whose moved notes only lost their velocity or length: once more with them
        pinned (``pins`` grows in place and is used by every later attempt)."""
        if o.problems and o.new is not None:
            more = self._pins_for(o, o.plan)
            if len(more) > len(pins):
                pins.velocity.update(more.velocity)
                pins.events.update(more.events)
                o = self._try(ids, pins, full)
        return o

    def _narrow(self, ids: List[int], pins: Pins, full: PlanResult, rejected: Dict[int, str],
                tried: Optional[_Outcome] = None) -> List[int]:
        """The clusters among ``ids`` that verify, found by halving the groups that do not."""
        if tried is None:
            tried = self._with_pins(self._try(set(ids), pins, full), set(ids), full, pins)
            if not tried.problems:
                return list(ids)
        if len(ids) <= 1:
            for cid in ids:
                rejected[cid] = "the re-rendered song would change: " + "; ".join(tried.problems[:2])
            return []
        half = len(ids) // 2
        return self._narrow(ids[:half], pins, full, rejected) + self._narrow(ids[half:], pins, full, rejected)

    @staticmethod
    def _align(o: _Outcome, full: PlanResult) -> PlanResult:
        """Cluster ids of a narrowed plan, renumbered to the full plan's (same first note)."""
        first = {id(cl.notes[0]): cl.id for cl in full.clusters if cl.notes}
        remap = {cl.id: first.get(id(cl.notes[0]), cl.id) for cl in o.plan.clusters if cl.notes}
        for cl in o.plan.clusters:
            cl.id = remap.get(cl.id, cl.id)
        o.plan.cluster_of = {k: remap.get(v, v) for k, v in o.plan.cluster_of.items()}
        o.accepted = {remap.get(c, c) for c in o.accepted}
        return o.plan


_LENGTH_FIELDS = {"duration", "durationTicks", "end", "noteLengthType"}
_PINNABLE = _LENGTH_FIELDS | {"velocity"}


def _differs_in(a: dict, b: dict) -> Set[str]:
    return {k for k in set(a) | set(b) if k not in _HAND_FIELDS and a.get(k) != b.get(k)}


def _plain_event(ev) -> bool:
    return ev.velocity_multiplier == 1.0 and ev.play and not ev.offset and not ev.slide


def _event_group(n: Note, moving: Dict[int, Note]) -> List[Note]:
    """The moving notes whose play events are pinned together with ``n``'s: its chord (a chord's events
    are all automatic or all written) and its tied continuations (they make up its length)."""
    out: Dict[int, Note] = {}
    todo = [n]
    while todo:
        m = todo.pop()
        if id(m) in out:
            continue
        out[id(m)] = m
        todo.extend(x for x in m.chord.notes if id(x) in moving)
        if m.tie_for is not None and m.tie_for.end is not None:
            todo.append(m.tie_for.end)
    return list(out.values())


def _structure_problems(old: Score, new: Score) -> List[str]:
    """Ties and spanners must survive the edit (a dangling one is dropped by the reader)."""
    def census(s: Score) -> Counter:
        c = Counter(sp.tag for sp in s.spanners)
        for m in s.measures:
            for seg in m.segments:
                for cr in seg.elements.values():
                    if cr.is_chord:
                        for ch in [cr] + cr.grace_all:
                            for n in ch.notes:
                                if n.tie_for is not None:
                                    c["Tie"] += 1
                                c["Note"] += 1
        return c
    a, b = census(old), census(new)
    if a == b:
        return []
    diff = [f"{k} {a.get(k, 0)} -> {b.get(k, 0)}" for k in sorted(set(a) | set(b)) if a.get(k, 0) != b.get(k, 0)]
    return ["the score structure changed: " + ", ".join(diff)]


# ==============================================================================
# the headset
# ==============================================================================

def pull(adb, device_dir: str, inbox: str) -> List[str]:
    """Copy the sidecars (not ``applied/``) from the headset into ``inbox``; earlier copies there
    are replaced, but only once every file arrived (a failed pull leaves the inbox as it was).
    Returns the file names."""
    adb.connect()
    d = shlex.quote(device_dir)
    out = adb.run("shell", f"cd {d} 2>/dev/null && for f in *{SIDECAR_EXT}; do [ -f \"$f\" ] && echo \"$f\"; done; true")
    names = [ln.strip() for ln in out.splitlines() if ln.strip().endswith(SIDECAR_EXT)]
    os.makedirs(inbox, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".pull-", dir=inbox)
    try:
        for n in names:
            adb.run("pull", f"{device_dir.rstrip('/')}/{n}", os.path.join(staging, n))
        for f in os.listdir(inbox):
            if f.endswith(SIDECAR_EXT):
                os.remove(os.path.join(inbox, f))
        for n in names:
            os.replace(os.path.join(staging, n), os.path.join(inbox, n))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return names


def glob_literal(name: str) -> str:
    """A glob (fnmatch) that matches exactly ``name`` (Tools/deploy_songs.py --only takes globs)."""
    return "".join(f"[{c}]" if c in "[]*?" else c for c in name)


def archivable(adb, device_dir: str, songs_dir: str, sidecars: Sequence[Sidecar], song_md5: Dict[str, str],
               log: Callable[[str], None] = lambda s: None) -> List[str]:
    """Names of the sidecars that may go to ``applied/``: the copy on the headset is still the one
    that was reviewed (HAND REC saved nothing new since the pull: those edits would be lost), and
    Note Waterfall's copy of the song (``songs_dir``) is the rebuilt one (``song_md5``: song file
    name -> md5), so the app plays the moved notes from the song itself once the sidecar is gone."""
    on_device = adb.md5s(device_dir)
    songs = adb.md5s(songs_dir)
    ok: List[str] = []
    for sc in sidecars:
        if not sc.md5 or on_device.get(sc.name) != sc.md5:
            log(f"  {sc.name} stays on the headset: it changed there since it was pulled (pull and review again)")
        elif not song_md5.get(sc.song) or songs.get(sc.song) != song_md5[sc.song]:
            log(f"  {sc.name} stays on the headset: Note Waterfall does not have the rebuilt {sc.song} yet")
        else:
            ok.append(sc.name)
    return ok


def archive_on_device(adb, device_dir: str, names: List[str], stamp: str) -> List[str]:
    """Move applied sidecars to ``HandEdits/applied/`` (as ``<name>.<stamp>.hands.json``)."""
    if not names:
        return []
    base = device_dir.rstrip("/")
    adb.run("shell", f"mkdir -p {shlex.quote(base + '/' + APPLIED_DIR)}")
    done = []
    for n in names:
        dst = f"{base}/{APPLIED_DIR}/{n[:-len(SIDECAR_EXT)]}.{stamp}{SIDECAR_EXT}"
        adb.run("shell", f"mv -f {shlex.quote(base + '/' + n)} {shlex.quote(dst)}")
        done.append(n)
    return done


# ==============================================================================
# reviews of a folder of sidecars, and applying them
# ==============================================================================

def load_sidecars(paths: Sequence[str]) -> Dict[str, List[Sidecar]]:
    """Sidecars by song file name."""
    by_song: Dict[str, List[Sidecar]] = {}
    for p in paths:
        sc = read_sidecar(p)
        by_song.setdefault(sc.song or song_of_sidecar(p), []).append(sc)
    return by_song


def review_all(lib, paths: Sequence[str], all_repeats: bool = False,
               log: Callable[[str], None] = lambda s: None) -> List[ScoreReview]:
    out = []
    for song, scs in sorted(load_sidecars(paths).items()):
        log(f"  reviewing {song} ...")
        out.append(review_song(lib, song, scs, all_repeats=all_repeats, log=log))
    return out


def format_review(rv: ScoreReview) -> List[str]:
    lines = [f"{rv.rel or '?'}  ->  {rv.song}"]
    for sc in rv.sidecars:
        lines.append(f"  {sc.name}: {len(sc.edits)} edit(s)" + (f", recorded {sc.updated}" if sc.updated else ""))
    for n in rv.notes:
        lines.append(f"  note: {n}")
    if rv.error:
        lines.append(f"  NOT APPLICABLE: {rv.error}")
        return lines
    moves = [r for r in rv.results if r.status == "move"]
    if moves:
        lines.append(f"  change ({rv.moved_notes} note(s) of the song: {rv.to_right} to the right hand, "
                     f"{rv.to_left} to the left):")
        for r in sorted(moves, key=lambda r: (r.edit.ticks, r.edit.midi)):
            e = r.edit
            extra = []
            if r.passes > 1:
                extra.append(f"{r.passes} passes")
            if r.velocity_pinned:
                extra.append("velocity kept")
            if r.events_pinned:
                extra.append("timing kept")
            extra.append(f"votes {e.votes}:{e.against}")
            lines.append(f"    {r.where:28} {e.from_hand:>5} -> {e.to_hand:5}  ({', '.join(extra)})")
    for c in rv.carried:
        lines.append(f"    + {c}")
    for status, title in (("conflict", "conflicts (not applied)"), ("skip", "skipped"), ("stale", "stale (not applied)")):
        rs = [r for r in rv.results if r.status == status]
        if rs:
            lines.append(f"  {title}:")
            for r in rs:
                e = r.edit
                w = r.where or f"{note_name(e.midi)} at tick {e.ticks}"
                lines.append(f"    {w:28} {e.from_hand:>5} -> {e.to_hand:5}  {r.reason}")
    already = sum(1 for r in rv.results if r.status == "already")
    if already:
        lines.append(f"  {already} edit(s) already in the score")
    if rv.ready:
        lines.append("  verified: the edited score renders to the same song with exactly these notes in the other hand")
    return lines


@dataclass
class ApplyReport:
    written: List[str] = field(default_factory=list)       # score rels overwritten
    backups: List[str] = field(default_factory=list)
    declined: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)
    built: List[str] = field(default_factory=list)          # outputs written by the build
    archived: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)


def apply_reviews(lib, reviews: Sequence[ScoreReview], yes: bool = False,
                  ask: Callable[[str], str] = input, log: Callable[[str], None] = print,
                  deploy_pianovision: Optional[Callable[[List[str]], None]] = None,
                  deploy_waterfall: Optional[Callable[[List[str]], None]] = None,
                  archive: Optional[Callable[[List[str]], List[str]]] = None) -> ApplyReport:
    """Overwrite the approved scores (after a backup), build them, then deploy and archive.

    Only reviews that are ``ready`` are offered; each needs a "y" from ``ask`` unless ``yes``.
    The three callbacks are skipped when None."""
    rep = ApplyReport()
    approved: List[ScoreReview] = []
    for rv in reviews:
        if not rv.ready:
            continue
        if not yes:
            try:
                ans = ask(f"Overwrite {rv.rel} ({rv.moved_notes} note(s) change hands)? [y/N] ")
            except EOFError:
                ans = ""
            if ans.strip().lower() not in ("y", "yes"):
                rep.declined.append(rv.rel)
                continue
        approved.append(rv)
    if not approved:
        return rep
    from .library import ATTIC_NAME, atomic_write
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    attic = os.path.join(lib.out, ATTIC_NAME, stamp, "scores")
    written: List[ScoreReview] = []
    for rv in approved:
        try:
            with open(rv.score_path, "rb") as f:
                cur = f.read()
            if _sha256(cur) != rv.score_sha256:
                rep.failed.append((rv.rel, "the score changed since the review; run review again"))
                continue
            dst = os.path.join(attic, rv.rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(rv.score_path, dst)
            with open(dst, "rb") as f:
                if f.read() != cur:
                    raise OSError("backup did not arrive intact")
            rep.backups.append(dst)
            mode = os.stat(rv.score_path).st_mode
            atomic_write(rv.score_path, rv.new_mscz)
            os.chmod(rv.score_path, mode & 0o7777)
            rep.written.append(rv.rel)
            written.append(rv)
        except OSError as e:
            rep.failed.append((rv.rel, str(e)))
    if not written:
        return rep
    build = lib.build(restrict={rv.rel for rv in written}, compat_for={rv.rel: rv.compat or {} for rv in written})
    for rel, err in build.failed:
        rep.failed.append((rel, f"build: {err}"))
    outs = []
    for rv in written:
        path = lib.out_path(rv.song)
        with open(path, "rb") as f:
            got = f.read()
        if got != rv.new_json:
            rep.failed.append((rv.rel, "the built song differs from the verified one (not deployed)"))
            continue
        outs.append(rv.song)
        rep.built.append(rv.song)
    if not outs:
        return rep
    for fn, label in ((deploy_pianovision, "PianoVision"), (deploy_waterfall, "Note Waterfall")):
        if fn is None:
            continue
        try:
            fn(outs)
        except Exception as e:
            rep.messages.append(f"deploy to {label} failed: {e}; sidecars left in place")
            return rep
    if archive is not None:
        names = [sc.name for rv in written if rv.song in outs for sc in rv.resolved_sidecars()]
        kept = [sc.name for rv in written if rv.song in outs for sc in rv.sidecars
                if sc.name not in names]
        try:
            rep.archived = archive(names)
        except Exception as e:
            rep.messages.append(f"archiving the sidecars failed: {e}")
        for n in names:
            if n not in rep.archived:
                rep.messages.append(f"{n} stays on the headset: it was not moved to {APPLIED_DIR}/")
        for n in kept:
            rep.messages.append(f"{n} stays on the headset: some of its edits were not applied")
    return rep
