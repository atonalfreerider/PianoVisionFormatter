"""Key moments written in the score, for Note Waterfall: the parts file's ``markers``.

Note Waterfall shows a song's rehearsal marks, section names and tempo words as small text falling with the
notes and as chapters of its song scrubber.  PianoVision's JSON has no place for them (and must stay byte for
byte as it is), so they go into the parts file (:mod:`pianovision.parts`) as a list, in time order::

    "markers": [{"time": 12.5, "text": "Più mosso", "kind": "tempo", "measure": 17}, ...]

* ``time``: seconds on the song's clock (the same tempo map, pauses and fermatas as the notes' ``start``),
  rounded to 0.1 ms; repeats and jumps are unrolled, so a mark in a repeated passage appears once per pass;
* ``text``: the text as written, UTF-8 (the JSON escapes it), whitespace collapsed, metronome marks, SMuFL
  glyphs and dynamics glyph names removed (``"Andante (♩ = 76)"`` -> ``"Andante"``);
* ``kind``: ``rehearsal`` (rehearsal marks: "A", "Verse 1"), ``section`` (a section named in a text or a
  tempo text: "Intro", "Cadenza", "Theme", a coda sign), ``tempo`` (a new tempo in words: "Andante",
  "a tempo", "Più mosso", "Tempo I"), ``gradual`` (a gradual tempo change, as a line or as words: "rit.",
  "accel.", "poco rall."), ``expression`` (character words: "dolce", "espressivo", "con brio") and ``text``
  (other system or piano staff text that survives the filter);
* ``measure``: the 1-based number of the measure in the score (its Nth measure; the same in every pass).

What is left out (it would be noise over the notes): invisible texts (tempo marks used to shape playback),
bare metronome marks, dynamics (``p``, ``mf``, ``cresc.``, ``dim.``), the formatter's own control texts
(``merge`` / ``end merge``, :func:`pianovision.metadata.extract_merge_markers`), harp pedal and chord names
(``"E G♯"``, ``"Cm"``), orchestra and hand directions (``a 2``, ``div.``, ``con sord.``, ``L.H.``, ``m.d.``),
articulation and pedal words (``ten.``, ``staccato``, ``Ped.``, ``una corda``), staff texts of the orchestra's
staves, jumps and their signs (``D.S. al Coda``, segno, fine), texts without two letters in a row and texts
longer than :data:`MAX_TEXT`.  The same text at the same moment (written on both piano staves, a ``rit.``
line with a ``rit.`` text) is kept once, as its most important kind.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from .score import SEG_CHORDREST, SEG_TIMETICK, VOICES, ticks

MAX_TEXT = 48

# kinds, most important first (dedup keeps the first)
KINDS = ("rehearsal", "section", "tempo", "gradual", "expression", "text")
_RANK = {k: i for i, k in enumerate(KINDS)}

# the score elements read
# MuseScore 4.7 saves a staff text that switches the playing technique ("non arpegg.") as a PlayTechAnnotation
TEXT_TAGS = ("RehearsalMark", "Tempo", "SystemText", "StaffText", "Expression", "PlayTechAnnotation")
STAFF_TAGS = ("StaffText", "Expression", "PlayTechAnnotation")   # belong to a staff: only the piano part's are read

_GRADUAL_TYPES = {
    "ritardando": "rit.", "accelerando": "accel.", "rallentando": "rall.", "allargando": "allarg.",
    "calando": "calando", "lentando": "lentando", "morendo": "morendo", "precipitando": "precipitando",
    "smorzando": "smorz.", "sostenuto": "sost.", "stringendo": "string.",
}

_DYNAMICS = {"p", "pp", "ppp", "pppp", "ppppp", "mp", "mf", "f", "ff", "fff", "ffff", "fffff", "sf", "sfz", "sffz",
             "sfp", "sfpp", "fp", "fz", "rf", "rfz", "n", "sfzp", "rinf", "rinforzando", "sforzando", "piano", "forte",
             "pianissimo", "fortissimo", "mezzo", "cresc", "crescendo", "dim", "dimin", "diminuendo", "decresc",
             "decrescendo", "rinfz"}
_MODIFIERS = {"poco", "a", "molto", "sempre", "più", "piu", "meno", "assai", "e", "ed", "il", "al", "ma", "non",
              "troppo", "subito", "sub", "simile", "sim", "pochissimo", "quasi", "con", "ben", "di", "del", "la",
              "le", "un", "una", "very", "more", "less", "and", "etc", "in", "the", "to"}
# orchestra, hand, articulation and pedal directions: a text of only these (with modifiers) is dropped
_DIRECTIONS = {"unis", "div", "divisi", "solo", "soli", "tutti", "pizz", "arco", "sord", "sordino", "sordini",
               "senza", "sul", "pult", "pultes", "tre", "corda", "corde", "ped", "pedale", "pedal", "with", "l", "r",
               "h", "m", "d", "s", "g", "lh", "rh", "md", "ms", "mg", "sopra", "sotto", "tr", "ten", "tenuto",
               "stacc", "staccato", "legato", "leg", "marc", "marcato", "marcatissimo", "martellato", "ossia",
               "lunga", "ad", "lib", "i", "ii", "iii", "iv", "v", "vi", "sp", "accentuato", "accent", "non", "loco",
               "sinistra", "destra", "right", "left", "hand", "hands", "over", "under", "both"}
# a text with any of these is a direction whatever else it says
_HARD_NOISE = {"merge", "arpeggio", "arpegg", "arp", "gliss", "glissando", "ossia", "pedal", "ped", "pedale",
               "sord", "sordino", "pizz", "arco", "divisi", "div", "unis", "pult", "keyboardpedalped",
               "ottava", "ottavabassaba", "coda",
               # instrument cues of a piano reduction ("[Oboe solo]", "Harp")
               "flute", "flutes", "piccolo", "oboe", "oboes", "clarinet", "clarinets", "bassoon", "bassoons", "horn",
               "horns", "trumpet", "trumpets", "trombone", "trombones", "tuba", "timpani", "harp", "violin",
               "violins", "viola", "violas", "cello", "celli", "cellos", "basses", "contrabass", "strings", "winds",
               "woodwinds", "brass", "percussion", "cymbals", "celesta", "tremolo", "orchestra", "tutti"}
_SECTIONS = {"intro", "introduction", "introduzione", "verse", "chorus", "prechorus", "bridge", "outro", "coda",
             "interlude", "refrain", "theme", "tema", "thema", "variation", "variazione", "var", "cadenza", "trio",
             "finale", "reprise", "ending", "postlude", "epilogue"}
_GRADUAL = {"rit", "ritard", "ritardando", "riten", "ritenuto", "rall", "rallent", "rallentando", "accel", "acceler",
            "accelerando", "accelerato", "stringendo", "string", "allargando", "allarg", "calando", "smorz",
            "smorzando", "smorsando", "morendo", "lentando", "precipitando", "deccel", "broaden", "slower",
            "faster", "poco", "a", "molto", "più", "piu", "e", "ancora", "assai", "un", "pochissimo",
            "pochissmo", "sempre", "dim", "dimin"}
_TEMPO = {"grave", "largo", "larghetto", "lento", "lentamente", "adagio", "adagietto", "andante", "andantino",
          "moderato", "allegretto", "allegro", "vivace", "vivo", "presto", "prestissimo", "tempo", "mosso", "moto",
          "stretto", "rubato", "istesso", "doppio", "movimento", "primo", "prima", "slowly", "slow", "freely",
          "moderately", "fast", "swing", "straight", "shuffle", "calmato", "animato", "veloce", "giusto",
          "sostenuto", "maestoso", "valse", "breve", "lentissimo", "vivacissimo", "allegrissimo", "tranquillo",
          "animando"}
_EXPRESSION = {"dolce", "dolciss", "dolcissimo", "espressivo", "espress", "espr", "cantabile", "cantando",
               "leggiero", "leggieramente", "leggieriss", "leggierissimo", "agitato", "anima", "brio", "brioso",
               "grazia", "grazioso", "giocoso", "scherzando", "misterioso", "pesante", "energico", "semplice",
               "mesto", "affettuoso", "delicato", "delicatezza", "delicatiss", "voce", "risoluto", "feroce",
               "fuoco", "forza", "passione", "appassionato", "calma", "grandioso", "grandisoso", "nobile",
               "brillante", "largamente", "duolo", "eleganza", "espressione", "tranquillamente", "calmando",
               "sostenuto", "rubato", "maestoso", "cantanto", "sotto", "express", "leggiere", "capriccioso",
               "capricciosamente", "deciso", "strepito"}

_NOTE_NAME = re.compile(r"^[A-G](?:♯|♭|#|b|x|𝄪|𝄫)*$")
_CHORD = re.compile(r"^[A-G](?:♯|♭|#|b)?(?:m|min|maj|M|dim|aug|sus|add|°|ø|\+)?\d*(?:/[A-G](?:♯|♭|#|b)?)?$")
_METRONOME = re.compile(r"\(?\s*=\s*\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)?\s*\)?")
_GLYPH_NAME = re.compile(r"\b(?:met|dynamic|keyboard|ottava|note|ornament|artic)[A-Z]\w*")
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


@dataclass
class ScoreText:
    """A text of the score at one of its places on the unrolled timeline (:func:`collect_score_texts`)."""
    tick: int              # MIDI tick of the render, pauses included (the notes' ticksStart scale)
    tag: str               # RehearsalMark | Tempo | SystemText | StaffText | Expression | Marker | GradualTempoChange
    text: str              # as written, glyph names (<sym>) left out
    staff: int             # score staff (0-based) the element belongs to
    measure: int           # 1-based number of its measure in the score
    visible: bool = True
    subtype: str = ""      # Marker: its subtype (coda, segno, fine, ...); GradualTempoChange: its tempoChangeType


def element_text(el, syms: Optional[Dict[str, str]] = None, other_sym: str = " ") -> str:
    """The text of a MuseScore text element (its ``<text>``), without the names of the SMuFL glyphs it embeds
    (``<sym>metNoteQuarterUp</sym>``: the glyph is not text) and with ``<font>`` and the like read through.
    ``syms``: glyphs that stand for text (name -> text); any other glyph reads as ``other_sym``."""
    if el is None:
        return ""
    t = el.find("text")
    if t is None:
        return ""
    out: List[str] = []

    def walk(node, top: bool) -> None:
        if not top and node.tag == "sym":
            name = (node.text or "").strip()
            out.append(syms[name] if syms and name in syms else other_sym)
        else:
            if node.text:
                out.append(node.text)
            for c in node:
                walk(c, False)
        if not top and node.tail:
            out.append(node.tail)

    walk(t, True)
    return "".join(out)


def _visible(props: dict) -> bool:
    return str(props.get("visible", "1")).strip() != "0"


def collect_score_texts(score, repeat_list, pause_map) -> List[ScoreText]:
    """Every rehearsal mark, tempo, system, staff and expression text, coda sign and gradual tempo change of
    the score, once per pass of its measure through the repeats (``repeat_list``), at the tick the render
    plays that place at (``pause_map.tick_with_pauses``, as the notes)."""
    out: List[ScoreText] = []
    for rs in repeat_list:
        off = rs.utick - rs.tick
        end = rs.end_tick
        for m in rs.measures:
            number = m.index + 1
            for e in m.markers:
                tick = pause_map.tick_with_pauses(ticks(m.tick) + off)
                el = e.props.get("_elem")
                text = element_text(el) if el is not None else str(e.props.get("text", ""))
                out.append(ScoreText(tick, "Marker", text, 0, number, _visible(e.props),
                                     str(e.props.get("subtype", ""))))
            for seg in m.segments:
                if ticks(seg.tick) >= end:
                    break
                if seg.type not in (SEG_CHORDREST, SEG_TIMETICK):
                    continue
                for a in seg.annotations:
                    if a.tag not in TEXT_TAGS:
                        continue
                    el = a.props.get("_elem")
                    text = element_text(el) if el is not None else str(a.props.get("text", ""))
                    tick = pause_map.tick_with_pauses(ticks(seg.tick) + off)
                    out.append(ScoreText(tick, a.tag, text, a.track // VOICES, number, _visible(a.props)))
    for sp in score.spanners:
        if sp.tag != "GradualTempoChange":
            continue
        start = ticks(sp.tick)
        el = sp.props.get("_elem")
        text = ""
        kind = str(sp.props.get("tempoChangeType", ""))
        if el is not None:
            b = el.find(".//beginText")
            if b is not None:
                text = "".join(b.itertext())
            kind = el.findtext(".//tempoChangeType") or kind
        m = score.tick2measure(sp.tick)
        for rs in repeat_list:
            if rs.tick <= start < rs.end_tick:
                tick = pause_map.tick_with_pauses(start + rs.utick - rs.tick)
                out.append(ScoreText(tick, "GradualTempoChange", text, sp.track // VOICES,
                                     (m.index + 1) if m is not None else 0, _visible(sp.props), kind.strip()))
    return out


def _fix_mojibake(text: str) -> str:
    """UTF-8 read as latin-1 somewhere upstream ("PiÃ¹" -> "Più")."""
    if "Ã" in text or "Â" in text:
        try:
            return text.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
    return text


def clean_text(text: str) -> str:
    """Written text -> shown text: SMuFL glyphs (private use area) and metronome marks out, whitespace collapsed,
    stray punctuation trimmed (``"Andante cantabile.  (♩ = 50)"`` -> ``"Andante cantabile."``)."""
    text = _fix_mojibake(unicodedata.normalize("NFC", text or ""))
    text = "".join(" " if 0xE000 <= ord(ch) <= 0xF8FF or ch in "♩♪♫\U0001D15F\U0001D160" else ch
                   for ch in text)
    text = _GLYPH_NAME.sub(" ", text)                              # a glyph's name written out as text
    text = _METRONOME.sub(" ", text)
    text = re.sub(r"(?:^|\s)=(?=\s|$)", " ", text)                # a lone "=" of a metronome mark
    text = re.sub(r"\(\s*\)|\[\s*\]", " ", text)
    text = re.sub(r"(?:^|\s)[-–_.·…]+(?=\s|$)", " ", text)       # dashes of a spread-out word, lone dots
    text = re.sub(r"\.{3,}", ".", text)                            # "rit......" -> "rit."
    text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    text = re.sub(r"\s+([,.;:!)\]])", r"\1", text)
    text = text.strip(" ,;:-–(")
    if text.count("(") > text.count(")"):
        text = text.replace("(", "").strip()
    if text.count(")") > text.count("("):
        text = text.replace(")", "").strip()
    return text


def _words(text: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _letters_in_a_row(text: str) -> bool:
    return re.search(r"[^\W\d_]{2}", text) is not None


def classify(tag: str, raw: str, subtype: str = "") -> Optional[tuple]:
    """(text, kind) of a score text, or None when it is left out (module docstring)."""
    if tag == "GradualTempoChange":
        text = clean_text(raw) or _GRADUAL_TYPES.get(subtype.lower(), "")
        return (text, "gradual") if text and len(text) <= MAX_TEXT else None
    if tag == "Marker":
        sub = subtype.lower()
        text = clean_text(raw)
        if sub in ("coda", "codab", "varcoda") and (not text or text.lower() in ("coda", "codab")):
            return ("Coda", "section")
        return None                                   # segno, fine, "To Coda": jump bookkeeping
    text = clean_text(raw)
    if not text or len(text) > MAX_TEXT:
        return None
    if tag == "RehearsalMark":
        return (text, "rehearsal")
    if not _letters_in_a_row(text):
        return None
    words = _words(text)
    if not words:
        return None
    tokens = text.replace(",", " ").split()
    if all(_NOTE_NAME.match(t) for t in tokens) or (len(tokens) == 1 and _CHORD.match(tokens[0])):
        return None                                   # harp pedals, chord names
    ws = set(words)
    if "ossia" in ws or (ws & _HARD_NOISE and not (ws & _SECTIONS - {"coda"}) and words[0] != "coda"):
        return None
    if all(w in _DYNAMICS or w in _MODIFIERS or w in _DIRECTIONS for w in words):
        return None                                   # dynamics, directions, "poco a poco"
    if words[0] in _SECTIONS or (words[0] == "pre" and len(words) > 1 and words[1] == "chorus"):
        return (text, "section")
    if all(w in _GRADUAL or w in _DYNAMICS or w in _DIRECTIONS for w in words) \
            and ws & (_GRADUAL - _MODIFIERS - {"dim", "dimin"}):
        return (text, "gradual")
    if ws & _TEMPO:
        return (text, "tempo")
    if tag == "Tempo":
        return (text, "tempo")
    if tag == "Expression" or ws & _EXPRESSION:
        return (text, "expression")
    return (text, "text")


def build_markers(texts: Iterable[ScoreText], seconds: Callable[[int], float],
                  piano_staves: Optional[Sequence[int]] = None) -> List[Dict[str, object]]:
    """The parts file's ``markers`` from the score's texts (:func:`collect_score_texts`): classified and filtered
    (:func:`classify`), staff and expression texts only from ``piano_staves`` (None: every staff), timed with
    ``seconds`` (MIDI tick -> s, the notes' tempo map), the same text at the same moment kept once (its most
    important kind), in time order."""
    piano = None if piano_staves is None else set(piano_staves)
    best: Dict[tuple, Dict[str, object]] = {}
    for t in texts:
        if not t.visible:
            continue
        if t.tag in STAFF_TAGS and piano is not None and t.staff not in piano:
            continue
        c = classify(t.tag, t.text, t.subtype)
        if c is None:
            continue
        text, kind = c
        time = round(float(seconds(t.tick)), 4)
        key = (round(time, 2), text.casefold().rstrip("."))
        m = {"time": time, "text": text, "kind": kind, "measure": int(t.measure)}
        old = best.get(key)
        if old is None or _RANK[kind] < _RANK[old["kind"]]:
            best[key] = m
    return sorted(best.values(), key=lambda m: (m["time"], _RANK[m["kind"]], m["text"]))
