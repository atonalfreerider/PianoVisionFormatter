"""Sung lyrics for Note Waterfall: each syllable on the falling note that sings it, in the parts file.

PianoVision's JSON has no place for lyrics (and must stay byte for byte as it is), so they go into the parts file
(:mod:`pianovision.parts`): a note that sings a syllable gets ::

    "lyric": {"text": "beau", "syllabic": "begin", "extend": false}

* ``text``: the syllable as written, trimmed, UTF-8 (the JSON escapes it): formatting read through, SMuFL glyphs
  left out except the elision sign (``<sym>lyricsElision</sym>`` -> "‿"), mojibake repaired, whitespace collapsed,
  a verse's number before its first syllable left out (``"1. Some"`` -> ``"Some"``); accents, curly apostrophes and
  punctuation kept, no hyphen added (the app draws it for begin/middle);
* ``syllabic``: ``single`` | ``begin`` | ``middle`` | ``end``, as MuseScore has it;
* ``extend``: the syllable is held across the following notes: MuseScore's melisma (``Lyrics::isMelisma``): an
  extender (``_``, ``ticks_f`` > 0), or a hyphenated syllable whose next chord or rest on its track has no
  syllable of that verse.

Which syllable a note sings (one at most):

* repeats and jumps are unrolled as the notes are; on the k-th pass through a measure a chord sings its verse k
  (MuseScore ``<no>`` k-1) when it has one; when verse k is sung elsewhere in the measure (on the chord's track),
  the chord has no syllable on this pass (verse k holds a syllable over it, or rests); otherwise it sings its
  lowest verse (a refrain written once, a third pass of two verses).  A verse with an empty text is no verse;
* a syllable belongs to a chord (MuseScore puts lyrics on the ChordRest of a track): it goes on the chord's highest
  note struck at that moment (the melody note of a piano staff with harmony beneath it); a syllable on a rest, or on
  a chord none of whose notes is struck there (all tied over), has no note and is left out;
* a staff that is not part of the piano part, or whose instrument is a voice (a soprano's line, wherever the
  formatter puts it), is a vocal staff: its syllable goes on its note in the part where that note lands, which also
  gets ``"sung": true``, and is copied onto the piano part's note struck at the same moment (within
  :data:`COPY_WINDOW` s) with the same pitch, else the same pitch class an octave away (the right hand first), when
  one is free.  The app shows the text on the piano note when one carries it and falls back to the sung note;
* the simplified part: a syllable of the original part is carried to the simplified part's note of the same
  onset and pitch when the note survives simplification (a syllable written on the simplified staves goes there
  directly).

The document gets a summary after ``markers`` (``null`` when the score has no lyrics)::

    "lyrics": {"syllables": 312, "verses": 2, "staves": ["Piano"], "sources": ["piano"]}

``syllables``: the syllables on the song's timeline that found a note; ``verses``: how many verse numbers the
score's syllables have; ``staves``: the names of the staves whose syllables found a note, in score order;
``sources``: ``piano`` (a staff of the piano part) and/or ``vocal`` (a vocal staff).
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .markers import _fix_mojibake, element_text
from .pvjson import VOICE_PROGRAMS
from .score import SEG_CHORDREST, VOICES, ticks

COPY_WINDOW = 0.03            # s: a piano note struck this close to a sung note doubles it
SYLLABIC = ("single", "begin", "middle", "end")
HANDS = ("right", "left")
# SMuFL glyphs a lyric may embed: the elision (two syllables on one note) is text, the others are not
_LYRIC_SYMS = {"lyricsElision": "‿", "lyricsElisionNarrow": "‿", "lyricsElisionWide": "‿"}
_VERSE_NUMBER = re.compile(r"^\d{1,2}[.)] (?=\S)")


@dataclass(eq=False)
class LyricEvent:
    """The syllable a chord (or rest) sings at one of its places on the unrolled timeline (:func:`collect_lyrics`)."""
    tick: int              # MIDI tick of the render, pauses included (the notes' ticksStart scale)
    length: int            # the chord's length in ticks
    staff: int             # score staff (0-based) = MIDI track of the render
    track: int             # score track (staff * 4 + voice)
    chord: Any             # the score's ChordRest (pianovision.score.Chord for a chord)
    verse: int             # MuseScore <no> of the syllable sung
    text: str
    syllabic: str
    extend: bool
    measure: int           # 1-based number of its measure in the score
    passes: int            # this is the measure's k-th pass
    staff_name: str
    pitch: int = -1        # the chord's highest pitch as it sounds (-1: a rest)

    def lyric(self) -> Dict[str, Any]:
        return {"text": self.text, "syllabic": self.syllabic, "extend": self.extend}


def lyric_text(el) -> str:
    """The shown text of a ``<Lyrics>`` element (module docstring)."""
    text = element_text(el, syms=_LYRIC_SYMS, other_sym="")
    text = _fix_mojibake(unicodedata.normalize("NFC", text))
    text = "".join(ch for ch in text if not 0xE000 <= ord(ch) <= 0xF8FF)        # SMuFL glyphs as characters
    text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    return _VERSE_NUMBER.sub("", text)                 # "1. Some" -> "Some": the verse's number is not sung


def _fraction(s: Optional[str]) -> Fraction:
    try:
        return Fraction((s or "0").strip())
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def _melisma_ticks(el) -> Fraction:
    """Length of a syllable's extender (``ticks_f`` in whole notes; before MuseScore 3.1 ``ticks`` in ticks)."""
    f = el.findtext("ticks_f")
    if f is not None:
        return _fraction(f)
    t = el.findtext("ticks")
    return _fraction(t) / (480 * 4) if t else Fraction(0)


def _verses(cr) -> Dict[int, Tuple[str, str, Fraction]]:
    """{verse: (text, syllabic, melisma)} of a ChordRest's visible, non-empty syllables."""
    out: Dict[int, Tuple[str, str, Fraction]] = {}
    for el in cr.lyrics:
        if (el.findtext("visible") or "1").strip() == "0":
            continue
        text = lyric_text(el)
        if not text:
            continue
        try:
            no = int((el.findtext("no") or "0").strip())
        except ValueError:
            no = 0
        syl = (el.findtext("syllabic") or "single").strip()
        out.setdefault(no, (text, syl if syl in SYLLABIC else "single", _melisma_ticks(el)))
    return out


def is_voice(instrument) -> bool:
    """A singer's instrument (MuseScore's voice.* instruments, or a voice program)."""
    if instrument is None:
        return False
    if (instrument.instrument_id or "").startswith("voice."):
        return True
    return bool(instrument.channels) and instrument.channels[0].program in VOICE_PROGRAMS


def staff_name(staff) -> str:
    name = _fix_mojibake(staff.part.track_name or "")
    if not name:
        ins = staff.part.instrument()
        name = _fix_mojibake(ins.track_name or ins.long_name or "")
    return re.sub(r"\s+", " ", name).strip()


@dataclass
class LyricTimeline:
    """A render's lyrics (:func:`collect_lyrics`)."""
    events: List[LyricEvent] = field(default_factory=list)
    voice_staves: List[int] = field(default_factory=list)    # staves whose instrument is a voice
    verses: List[int] = field(default_factory=list)          # the verse numbers (<no>) of the score's syllables


def collect_lyrics(score, repeat_list, pause_map) -> LyricTimeline:
    """The syllable sung by every ChordRest with lyrics, once per pass of its measure through the repeats
    (``repeat_list``), at the tick the render plays that place at (``pause_map.tick_with_pauses``, as the notes)."""
    staves = score.staves
    timeline = LyricTimeline(voice_staves=[s.idx for s in staves if is_voice(s.part.instrument())])
    tracks: Dict[int, List[Any]] = {}
    verses: Dict[int, Dict[int, Tuple[str, str, Fraction]]] = {}
    in_measure: Dict[Tuple[int, int], set] = {}        # (measure index, track) -> the verses sung there
    for m in score.measures:
        for seg in m.segments:
            if seg.type != SEG_CHORDREST:
                continue
            for tr, cr in seg.elements.items():
                tracks.setdefault(tr, []).append(cr)
                if cr.lyrics and staves[tr // VOICES].linked_primary:
                    vs = _verses(cr)
                    if vs:
                        verses[id(cr)] = vs
                        in_measure.setdefault((m.index, tr), set()).update(vs)
    if not verses:
        return timeline
    timeline.verses = sorted({v for vs in verses.values() for v in vs})
    following: Dict[int, Any] = {}
    for crs in tracks.values():
        for a, b in zip(crs, crs[1:]):
            following[id(a)] = b

    def extend(cr, verse: int, syllabic: str, melisma: Fraction) -> bool:
        if melisma > 0:
            return True
        if syllabic in ("begin", "middle"):
            nxt = following.get(id(cr))
            return nxt is not None and verse not in verses.get(id(nxt), {})
        return False

    out = timeline.events
    passes: Dict[int, int] = {}
    for rs in repeat_list:
        off = rs.utick - rs.tick
        end = rs.end_tick
        for m in rs.measures:
            k = passes[m.index] = passes.get(m.index, 0) + 1
            for seg in m.segments:
                if ticks(seg.tick) >= end:
                    break
                if seg.type != SEG_CHORDREST:
                    continue
                for tr in sorted(seg.elements):
                    cr = seg.elements[tr]
                    vs = verses.get(id(cr))
                    if not vs:
                        continue
                    staff = staves[tr // VOICES]
                    if (k - 1) in vs:
                        verse = k - 1
                    elif (k - 1) in in_measure[(m.index, tr)]:
                        continue                 # this pass's verse is sung here, without a syllable on this chord
                    else:
                        verse = min(vs)
                    text, syl, mel = vs[verse]
                    stick = ticks(cr.tick)
                    pitch = -1
                    if cr.is_chord and cr.notes:
                        pitch = cr.notes[-1].pitch + staff.pitch_offset(stick)
                    out.append(LyricEvent(
                        tick=pause_map.tick_with_pauses(stick + off), length=max(ticks(cr.actual_ticks()), 1),
                        staff=staff.idx, track=tr, chord=cr, verse=verse, text=text, syllabic=syl,
                        extend=extend(cr, verse, syl, mel), measure=m.index + 1, passes=k,
                        staff_name=staff_name(staff), pitch=pitch))
    return timeline


# ------------------------------------------------------------------------------
# onto the parts' notes
# ------------------------------------------------------------------------------

class _Part:
    """A part's notes (:class:`pianovision.pvjson.Note`, ``src`` set) beside their dicts in the document."""

    def __init__(self, notes: List[List[Any]], dicts: Dict[str, List[dict]]):
        self.notes = notes
        self.dicts = [dicts[h] for h in HANDS]
        self.by_chord: Dict[int, List[Tuple[int, int]]] = {}
        for h, hand in enumerate(notes):
            for i, n in enumerate(hand):
                ch = getattr(n.src, "chord", None)
                if ch is not None:
                    self.by_chord.setdefault(id(ch), []).append((h, i))
        # notes in time order (the streams are): for the moment lookups
        self.times = [[n.time for n in hand] for hand in notes]

    def free(self, h: int, i: int) -> bool:
        return "lyric" not in self.dicts[h][i]

    def put(self, h: int, i: int, lyric: dict, sung: bool = False) -> None:
        d = self.dicts[h][i]
        d["lyric"] = dict(lyric)
        if sung:
            d["sung"] = True

    def chord_note(self, ev: LyricEvent) -> Optional[Tuple[int, int]]:
        """The note of this pass of the event's chord that sings: its highest struck note (the earliest strike)."""
        cands = self.by_chord.get(id(ev.chord))
        if not cands:
            return None
        lo, hi = ev.tick - ev.length // 2, ev.tick + ev.length
        here = [(h, i) for h, i in cands if lo <= self.notes[h][i].ticks < hi]
        if not here:
            return None

        def rank(hi_):
            n = self.notes[hi_[0]][hi_[1]]
            return (-n.src.pitch, -n.midi, n.ticks, n.time)

        return min(here, key=rank)

    def at(self, h: int, t: float, window: float):
        """Indices of the hand's notes struck within ``window`` of ``t``."""
        ts = self.times[h]
        i = bisect.bisect_left(ts, t - window - 1e-9)
        while i < len(ts) and ts[i] <= t + window + 1e-9:
            yield i
            i += 1


def attach_lyrics(parts: Dict[str, Any], timeline: Optional[LyricTimeline], piano_staves: Sequence[int],
                  seconds) -> Optional[Dict[str, Any]]:
    """Puts the timeline's syllables on the notes of ``parts`` (:func:`pianovision.parts.build_parts`' result: the
    document's note dicts get ``lyric`` / ``sung``) and returns the document's ``lyrics`` summary, None when the
    score has no syllables.  ``piano_staves``: the staves of the piano part (MIDI tracks of the primary and
    simplified piano staves); ``seconds``: MIDI tick -> s (the notes' tempo map)."""
    events = timeline.events if timeline is not None else []
    if not events:
        return None
    doc_parts = parts["doc"]["parts"]
    P = {name: _Part(notes, doc_parts[name]) for name, notes in parts["notes"].items() if name in doc_parts}
    piano = set(piano_staves)
    voices = set(timeline.voice_staves)
    vocal_staves = {e.staff for e in events if e.staff in voices or e.staff not in piano}
    placed: Dict[int, Tuple[str, str]] = {}          # id(event) -> (staff name, source)
    sung: List[Tuple[LyricEvent, Optional[Any]]] = []

    order = sorted(events, key=lambda e: (e.staff in vocal_staves, e.tick, e.staff, e.track))
    for ev in order:
        if ev.staff not in vocal_staves:
            # the original part; a chord of the simplified staves is only in the simplified part
            for name in ("original", "simplified"):
                p = P.get(name)
                hit = p.chord_note(ev) if p else None
                if hit is None:
                    continue
                if p.free(*hit):
                    p.put(*hit, ev.lyric())
                    placed[id(ev)] = (ev.staff_name, "piano")
                break
            continue
        # a vocal staff: on its own note where it lands, then on the piano note that doubles it
        note = None
        for name in ("original", "orchestra"):
            p = P.get(name)
            hit = p.chord_note(ev) if p else None
            if hit is None:
                continue
            if p.free(*hit):
                p.put(*hit, ev.lyric(), sung=True)
                note = p.notes[hit[0]][hit[1]]
                placed[id(ev)] = (ev.staff_name, "vocal")
            break
        sung.append((ev, note))

    orig = P.get("original")
    if orig is not None:
        for ev, note in sung:
            t0 = note.time if note is not None else seconds(ev.tick)
            p0 = note.midi if note is not None else ev.pitch
            if p0 < 0:
                continue
            best = None
            for h in (0, 1):
                for i in orig.at(h, t0, COPY_WINDOW):
                    n = orig.notes[h][i]
                    src = n.src
                    ch = getattr(src, "chord", None)
                    if ch is None or ch.track // VOICES in voices or not orig.free(h, i):
                        continue
                    if n.midi != p0 and abs(n.midi - p0) != 12:
                        continue
                    key = (n.midi != p0, h, abs(n.time - t0), -n.midi)
                    if best is None or key < best[0]:
                        best = (key, h, i)
            if best is not None:
                orig.put(best[1], best[2], ev.lyric())
                placed.setdefault(id(ev), (ev.staff_name, "vocal"))

    simp = P.get("simplified")
    if simp is not None and orig is not None:
        _carry(orig, simp)

    names: List[str] = []
    for ev in sorted(events, key=lambda e: e.staff):
        if id(ev) in placed and ev.staff_name not in names:
            names.append(ev.staff_name)
    sources = [s for s in ("piano", "vocal") if any(v[1] == s for v in placed.values())]
    return {"syllables": len(placed), "verses": len(timeline.verses), "staves": names, "sources": sources}


def _carry(orig: _Part, simp: _Part) -> None:
    """The original part's syllables onto the simplified part's notes of the same onset and pitch."""
    exact: Dict[Tuple[int, int, int], List[int]] = {}
    for h, hand in enumerate(simp.notes):
        for i, n in enumerate(hand):
            exact.setdefault((h, n.ticks, n.midi), []).append(i)
    for h, hand in enumerate(orig.dicts):
        for i, d in enumerate(hand):
            lyr = d.get("lyric")
            if lyr is None:
                continue
            n = orig.notes[h][i]
            target = None
            for hh in (h, 1 - h):
                for j in exact.get((hh, n.ticks, n.midi), ()):
                    if simp.free(hh, j):
                        target = (hh, j)
                        break
                if target is None:
                    for j in simp.at(hh, n.time, COPY_WINDOW):
                        if simp.notes[hh][j].midi == n.midi and simp.free(hh, j):
                            target = (hh, j)
                            break
                if target is not None:
                    break
            if target is not None:
                simp.put(*target, lyr, sung=bool(d.get("sung")))
