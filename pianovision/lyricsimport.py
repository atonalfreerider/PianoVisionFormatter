"""Lyrics from a text file onto a score's vocal line: ``python -m pianovision lyrics import SCORE LYRICS.txt``.

The text file is plain UTF-8; blank lines are ignored::

    [Verse 1]
    one sung line per text line
    a lov-er's word          "-" splits a word into syllables yourself (your split wins)
    hold_ me now__           "_" holds the syllable over one more note (a melisma), "__" over two
    [Chorus]
    ...
    [Piano Solo]             a header without lines: an instrumental section
    [Chorus]                 a header without lines, named like an earlier sung section: that section again
    [Verse 3 @ m.10 pass 3]  optional pin: the section's first line starts in measure 10, its 3rd time through
    [Outro x2]               the section twice; a line ending in "(x2)": the line twice

* A header is ``[...]``; ``[Chorus: who sings]`` is fine (only the name before the colon is used).  Lines before
  the first header form an untitled section.
* Parenthesized text, ``(ooh)``, is taken as backing vocals and left out (``keep_parentheses`` keeps it).
* Pasted junk is tolerated: a line that obviously is not lyrics ("You might also like", "12 Contributors",
  "See ... Live", "Get tickets ...", "Translations", writer and licence footers, "Embed") starts a block that is
  skipped up to the next header; a "<Title> Lyrics" first line and a "<n>Embed" glued to the end of a line are
  dropped.  Every skip is reported by line number.

Syllables: your "-" first; else pyphen (en_US) when it is installed; else a small rule-based English syllabifier
(:func:`syllables`).

How the text meets the score (:func:`plan_import`):

1. The vocal line is the score's vocal staff (:func:`pianovision.pvjson.staff_roles`, or ``staff``), its first
   voice, in playback order: repeats, voltas and jumps unrolled as the formatter (and MuseScore) plays them.  A
   tied note counts once.  Rests split it into phrases.
2. The sung lines are matched to the phrases in order (:func:`align`, a dynamic programme): a line on one phrase,
   a line over several phrases (a rest inside the line), several lines in one phrase (no rest between them), a
   phrase no line is sung on, a line sung nowhere; each costed by how its syllables fit the notes, with the
   section headers as soft anchors (a section starts after a rest; an instrumental header stands for a long rest;
   a long rest inside a section is unlikely) and the ``@ m.N`` pins as hard ones.
3. In each match the syllables go onto the notes in order (:func:`assign`): one syllable a note; extra notes hold
   the syllable before them (a melisma: preferably the last syllable of a word, under a slur, a change of pitch,
   the end of a line; never across a rest); more syllables than notes are merged (the syllables of one word
   together, else two words on one note with MuseScore's elision); notes left over get no syllable.  Every merge
   and every note left over is a mismatch in the report.
4. Every syllable is written on its chord with the verse number of its pass: on the k-th pass through a measure,
   verse k (``<no>`` k-1), as :mod:`pianovision.lyrics` and MuseScore read them, so repeated music carries stacked
   verses.  A word's last syllable held over more notes gets an extender (``<ticks_f>``, up to the last note it
   holds in the written score), as MuseScore writes one; inside a word the hyphen says it.

The ``<Lyrics>`` elements are written as MuseScore 4.7.5 saves them (``no``, ``syllabic``, ``ticks_f``, ``text``,
right after the chord's duration); the rest of the .mscx stays byte for byte.  :func:`verify` checks the edited
score: the formatter reads it and renders the same notes (the PianoVision JSON byte for byte, the parts file but for
its lyrics), :func:`pianovision.lyrics.collect_lyrics` finds every syllable on its note and pass, and MuseScore
headless opens it and exports it (MusicXML, its lyrics counted).

Reports give counts, line numbers and measure numbers, never the text.  Measures are numbered in file order, as
``scorefix report`` numbers them.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, replace as _dc_replace
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .score import SEG_CHORDREST, VOICES, ticks

ELISION = "\u203f"            # two syllables on one note (MuseScore's <sym>lyricsElision</sym>)
WORK_DIR = "~/.cache/nw-lyricsimport"
MIN_FREE_MB = 2048                 # MuseScore's check is skipped below this much available memory
MUSESCORE_TIMEOUT = 300            # s

# matching costs (align): a syllable short of notes costs more than a note held over
C_HELD = 0.35            # per note more than the line's syllables (held syllables)
C_SHORT = 1.0            # per syllable more than the notes (merged)
C_SKIP_NOTE = 0.2        # per note of a phrase no line is sung on (less than C_HELD: no bias to long phrases)...
C_SKIP_PHRASE = 2.0      # ... plus this
C_SKIP_SYL = 1.2         # per syllable of a line sung nowhere ...
C_SKIP_LINE = 2.0        # ... plus this
C_SPLIT = 0.4            # a line over one more phrase (plus the rest between them) ...
C_HELD_SPAN = 0.5        # ... and per note it holds (a line stretched over phrases is rather a phrase without one)
C_JOIN = 1.0             # one more line in a phrase
C_JOIN_SECTION = 1.5     # ... from the next section
KMAX, RMAX = 4, 3        # at most KMAX phrases for a line, RMAX lines in a phrase
LONG_REST = 8.0          # quarter notes: a rest this long is an instrumental break

# placing (assign)
P_HOLD = 0.4             # a note held by the syllable before it ...
P_HOLD_SLUR = -0.3       # ... under a slur
P_HOLD_SLUR_START = 0.8  # ... where a slur starts (its first note sings the syllable the slur holds)
P_HOLD_SAME = 0.6        # ... on the pitch of the note before (a repeated note usually sings a new syllable)
P_HOLD_WORD = 0.3        # ... inside a word (not its last syllable)
P_HOLD_END = -0.15       # ... by the last syllable of a line
P_HOLD_MISSING = 0.5     # a "_" hold the notes do not have
P_SPLIT_WORD = 1.5       # a rest inside a word
P_MERGE_WORD = 1.0       # a syllable merged with the one before (same word)
P_ELIDE = 2.0            # two words on one note
P_UNSUNG = 1.2           # a note left without a syllable
HOLD_MAX = 12            # notes a syllable holds at most (beyond its "_" holds)


class LyricsImportError(Exception):
    """Nothing can be imported (no vocal staff, lyrics already there, unreadable text, ...)."""


# ==============================================================================
# the text file
# ==============================================================================

@dataclass
class Syl:
    text: str
    syllabic: str = "single"         # MuseScore's single | begin | middle | end
    holds: int = 0                   # "_": extra notes the syllable is held over
    word_start: bool = True
    word_end: bool = True


@dataclass
class TextLine:
    lineno: int                      # 1-based line of the file
    syls: List[Syl]
    section: int = -1                # index in LyricsText.sections (the header it is sung under)
    again: bool = False              # sung again: a repeated section, "(x2)", "[... x2]"
    first: bool = False              # the first line of its section (each time the section is sung)
    after_break: bool = False        # an instrumental header comes before it

    @property
    def need(self) -> int:
        """The notes it needs: a syllable each, plus its holds."""
        return sum(1 + s.holds for s in self.syls)


@dataclass
class Section:
    name: str
    lineno: int                      # its header's line (0: the lines before the first header)
    lines: List[TextLine] = field(default_factory=list)
    pin: Optional[Tuple[int, Optional[int]]] = None      # (measure, pass) its first line starts at
    times: int = 1
    repeat_of: Optional[int] = None  # an empty header repeating an earlier section's lines
    instrumental: bool = False


@dataclass
class LyricsText:
    sections: List[Section]
    sung: List[TextLine]             # the lines as they are sung (repeats expanded)
    warnings: List[str]
    syllabifier: str


_APOS = "'\u2019\u2018`\u02bc"
_DASHES = "\u2013\u2014\u2015"
_HEADER = re.compile(r"^\[([^\[\]]*)\]$")
_PIN = re.compile(r"@\s*(?:m(?:easure)?\.?\s*)?(\d+)(?:\s*[,;/]?\s*(?:pass|time|p\.?)\s*(\d+))?\s*$", re.I)
_TIMES = re.compile(r"(?:^|\s)[(\[]?\s*[x\u00d7]\s*(\d{1,2})\s*[)\]]?$", re.I)
_EMBED = re.compile(r"(?<=\D)\d*\s*Embed$")
_TITLE_LINE = re.compile(r"\blyrics$", re.I)
_JUNK = tuple(re.compile(p, re.I) for p in (
    r"^you might also like\b",
    r"^see .{1,80}\blive\b",
    r"^get tickets\b",
    r"^\d+\s*contributors?\b",
    r"^translations?\b",
    r"^(?:song\s*)?writers?(?:\s*\(s\))?\s*:",
    r"^lyrics?\s+(?:licensed|provided|powered)\b",
    r"^source\s*:",
    r"\b(?:musixmatch|lyricfind|azlyrics)\b",
    r"\bgenius\.com\b",
    r"^\d*\s*embed$",
))


def read_text(path: str) -> str:
    """The lyrics file as text (UTF-8, a byte order mark allowed)."""
    with open(path, "rb") as f:
        data = f.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        line = data[:e.start].count(b"\n") + 1
        raise LyricsImportError(f"{path}: not UTF-8 (line {line}); save it as UTF-8") from None


def _lines_text(nums: Sequence[int]) -> str:
    from .scorefix import ranges
    r = ranges(nums)
    return ("lines " if len(nums) > 1 else "line ") + r


def _header(raw: str, no: int) -> Section:
    s = raw.strip()
    pin = None
    m = _PIN.search(s)
    if m:
        pin = (int(m.group(1)), int(m.group(2)) if m.group(2) else None)
        s = s[:m.start()].strip()
    times = 1
    t = _TIMES.search(s)
    if t:
        times = max(1, int(t.group(1)))
        s = s[:t.start()].strip()
    name = re.sub(r"\s+", " ", s.split(":")[0]).strip() or "(untitled)"
    return Section(name, no, pin=pin, times=times)


def _drop_parentheses(s: str) -> Tuple[str, int]:
    """``s`` without its parenthesized parts (innermost first); how many words were left out."""
    dropped = 0
    while True:
        m = re.search(r"\([^()]*\)", s)
        if not m:
            break
        dropped += max(1, len(m.group(0)[1:-1].split()))
        s = s[:m.start()] + " " + s[m.end():]
    return re.sub(r"\s+", " ", s).strip(), dropped


def parse_text(text: str, keep_parentheses: bool = False,
               split: Optional[Callable[[str], List[str]]] = None) -> LyricsText:
    """The sections and sung lines of a lyrics file (module docstring); ``split``: a word -> its syllables
    (default: :func:`syllabifier`)."""
    name = "given"
    if split is None:
        split, name = syllabifier()
    sections: List[Section] = []
    warnings: List[str] = []
    junk: List[int] = []
    in_junk = False
    first_content = True

    def flush_junk() -> None:
        if junk:
            warnings.append(f"{_lines_text(junk)}: not lyrics (a junk marker on line {junk[0]}), skipped up to "
                            f"the next header")
            junk.clear()

    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.replace("\ufeff", "").strip()
        if not s:
            continue
        h = _HEADER.match(s)
        if h:
            flush_junk()
            in_junk = False
            first_content = False
            sections.append(_header(h.group(1), no))
            continue
        if in_junk:
            junk.append(no)
            continue
        if any(p.search(s) for p in _JUNK):
            in_junk = True
            junk.append(no)
            continue
        if first_content and _TITLE_LINE.search(s):
            first_content = False
            warnings.append(f"line {no}: a title line (\"... Lyrics\") before the first header, skipped")
            continue
        first_content = False
        m = _EMBED.search(s)
        if m:
            s = s[:m.start()].rstrip()
            warnings.append(f"line {no}: a trailing \"Embed\" left out")
            if not s:
                continue
        times = 1
        t = _TIMES.search(s)
        if t:
            times = max(1, int(t.group(1)))
            s = s[:t.start()].rstrip()
            if not s:
                warnings.append(f"line {no}: a repeat mark alone on its line, not applied")
                continue
        if not keep_parentheses:
            s, dropped = _drop_parentheses(s)
            if dropped:
                warnings.append(f"line {no}: parenthesized text left out ({dropped} word{'s' if dropped > 1 else ''}"
                                f"{'; the whole line' if not s else ''})")
            if not s:
                continue
        syls = line_syllables(s, split)
        if not syls:
            warnings.append(f"line {no}: no words, skipped")
            continue
        if not sections:
            sections.append(Section("(untitled)", 0))
        for k in range(times):
            sections[-1].lines.append(TextLine(no, syls, again=k > 0))
    flush_junk()

    def key(n: str) -> str:
        return re.sub(r"\s+", " ", n).strip().lower()

    sung: List[TextLine] = []
    pending_break = False
    for i, sec in enumerate(sections):
        lines = sec.lines
        if not lines:
            prev = next((j for j in range(i - 1, -1, -1) if sections[j].lines and sections[j].repeat_of is None
                         and key(sections[j].name) == key(sec.name)), None)
            if prev is None:
                sec.instrumental = True
                pending_break = True
                continue
            sec.repeat_of = prev
            lines = sections[prev].lines
        for rep in range(sec.times):
            for k, ln in enumerate(lines):
                sung.append(_dc_replace(ln, section=i, again=ln.again or rep > 0 or sec.repeat_of is not None,
                                    first=k == 0, after_break=pending_break and k == 0 and rep == 0))
        pending_break = False
    return LyricsText(sections, sung, warnings, name)


def _split_punct(tok: str) -> Tuple[str, str, str]:
    """``tok`` -> (leading punctuation, the word, trailing punctuation); apostrophes inside or around letters, "-"
    and "_" belong to the word."""
    i, j = 0, len(tok)
    while i < j and not (tok[i].isalnum() or tok[i] == "_"
                         or (tok[i] in _APOS and i + 1 < j and tok[i + 1].isalpha())):
        i += 1
    while j > i and not (tok[j - 1].isalnum() or tok[j - 1] in "_-"
                         or (tok[j - 1] in _APOS and j - 2 >= i and tok[j - 2].isalpha())):
        j -= 1
    return tok[:i], tok[i:j], tok[j:]


def line_syllables(s: str, split: Callable[[str], List[str]]) -> List[Syl]:
    """The syllables of one sung line: words split by ``split`` unless written with "-"; "_" holds."""
    for d in _DASHES:
        s = s.replace(d, " ")
    words: List[List[list]] = []           # [[text, holds], ...] per word
    lead_over = ""                         # punctuation waiting for the next word
    cont = False                           # the last word ended with "-": the next token goes on with it
    for tok in s.split():
        if set(tok) <= {"-"}:
            cont = bool(words)
            continue
        if set(tok) <= {"_"}:
            if words:
                words[-1][-1][1] += len(tok)
            continue
        lead, core, trail = _split_punct(tok)
        if core.startswith("-"):
            core = core.lstrip("-")
            cont = cont or bool(words)
        if not core.replace("_", "").replace("-", ""):
            if core and words:                       # holds alone, e.g. "_," : on the last syllable
                words[-1][-1][1] += core.count("_")
            if words:
                words[-1][-1][0] += lead + trail
            else:
                lead_over += lead + trail
            continue
        lead, lead_over = lead_over + lead, ""
        pieces = core.split("-")
        open_end = core.endswith("-")
        explicit = cont or open_end or sum(1 for p in pieces if p.replace("_", "")) > 1
        sy: List[list] = []
        for p in pieces:
            h, t = p.count("_"), p.replace("_", "")
            if not t:
                target = sy[-1] if sy else (words[-1][-1] if words else None)
                if h and target is not None:
                    target[1] += h
                continue
            if explicit:
                sy.append([t, h])
            else:
                sy.extend([q, 0] for q in split(t))
                sy[-1][1] += h
        if not sy:
            continue
        sy[0][0] = lead + sy[0][0]
        sy[-1][0] += trail
        if cont and words:
            words[-1].extend(sy)
        else:
            words.append(sy)
        cont = open_end
    out: List[Syl] = []
    for w in words:
        n = len(w)
        for k, (t, h) in enumerate(w):
            syl = "single" if n == 1 else "begin" if k == 0 else "end" if k == n - 1 else "middle"
            out.append(Syl(t, syl, h, k == 0, k == n - 1))
    return out


# ==============================================================================
# syllables of English words
# ==============================================================================

def syllabifier() -> Tuple[Callable[[str], List[str]], str]:
    """(word -> syllables, its name): pyphen's en_US hyphenation when pyphen is installed, else :func:`syllables`."""
    try:
        import pyphen                                     # optional; never installed by this tool
        dic = pyphen.Pyphen(lang="en_US", left=1, right=1)
    except Exception:
        return syllables, "rule-based English syllabifier (pyphen is not installed)"

    def split(word: str) -> List[str]:
        try:
            pos = sorted({int(p) for p in dic.positions(word) if 0 < int(p) < len(word)})
        except Exception:
            return syllables(word)
        return _cut(word, pos)

    return split, "pyphen (en_US)"


_V = "aeiou"
_VY = "aeiouy"
_OVERRIDES = {"every": [2], "maybe": [3], "evening": [3]}
_HEADS = ("every", "any", "some")
_DIGRAPHS = {"th", "sh", "ch", "ph", "wh", "gh", "ck", "qu"}
_ONSET_STRICT = {"bl", "br", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "pl", "pr", "tr", "thr", "shr", "chr", "phr"}
_ONSET2 = _ONSET_STRICT | {"sc", "sk", "sl", "sm", "sn", "sp", "st", "sw", "tw", "squ", "wr"}
_ONSET3 = {"str", "spr", "scr", "spl", "squ"}


def _fold(c: str) -> str:
    """A letter without its accent, lower case; apostrophes as "'" (one character for one)."""
    if c in _APOS:
        return "'"
    d = unicodedata.normalize("NFD", c)
    return ((d[0] if d else c).lower() or c)[:1]


def _has_vowel(w: str) -> bool:
    return any(c in _VY for c in w)


def _cut(word: str, positions: Sequence[int]) -> List[str]:
    out, last = [], 0
    for p in sorted(set(positions)):
        if last < p < len(word):
            out.append(word[last:p])
            last = p
    out.append(word[last:])
    return out


def syllables(word: str) -> List[str]:
    """A word's syllables by a few rules of English: vowel groups are the syllables (a "y" before a vowel is a
    consonant), a silent final e and the silent e of "-es", "-ed" and of "-ly", "-ment", "-ness", "-ful", "-less"
    are not, "-le" after a consonant is ("lit-tle"), some vowel pairs are two syllables ("li-on", "pi-a-no",
    "qui-et"), "-ing" after a vowel is its own ("go-ing"), "didn't" is two; the split falls before a single
    consonant ("ma-king"), between two ("hap-py"), before a cluster that can start a syllable ("ta-ble",
    "in-stru-ment").  Words starting with every / any / some are split there."""
    w = "".join(_fold(c) for c in word)
    if len(w) != len(word) or not _has_vowel(w):
        return [word]
    return _cut(word, _breaks(w))


def _breaks(w: str) -> List[int]:
    if w in _OVERRIDES:
        return list(_OVERRIDES[w])
    for head in _HEADS:
        rest = w[len(head):]
        if w.startswith(head) and len(rest) >= 3 and _has_vowel(rest) and \
                (rest[0] not in _VY or rest.startswith("one") or head.endswith("y")):
            return _breaks(head) + [len(head)] + [len(head) + p for p in _breaks(rest)]
    return _simple_breaks(w)


def _cle(x: str, k: int) -> bool:
    """The e at ``k`` is the vowel of "-Cle" ("ta-ble", "lit-tle"), pronounced."""
    return k >= 2 and x[k - 1] == "l" and x[k - 2].isalpha() and x[k - 2] not in _VY + "l"


def _hiatus(x: str, k: int, before: bool) -> bool:
    """Two syllables between the vowels at ``k`` and ``k`` + 1 ("li-on"); ``before``: a syllable comes before."""
    a, b = x[k], x[k + 1]
    prev = x[k - 1] if k > 0 else ""
    nxt = x[k + 2:]
    pair = a + b
    if pair in ("ia", "io", "iu"):
        if not prev or prev in "ctsxg":                  # nation, social, vision, religion
            return False
        if pair == "io" and nxt.startswith("n") and (prev == "n" or x[:k].endswith("ll")):
            return False                                 # union, onion, million
        return True                                      # lion, piano, radio, medium
    if pair in ("ua", "uo"):
        return prev not in "qg"                          # usual, duo (not quality, guard)
    if pair == "ue":
        return prev not in "qg" and nxt[:1] in ("l", "t")    # cruel, fuel, duet
    if pair == "eo":
        return prev != "g" and nxt in ("", "n", "s")     # video, neon, rodeos
    if pair == "oe":
        return nxt[:1] in ("m", "t", "l")                # poem, poet, noel
    if pair == "ea":
        return nxt == "" and before                      # idea, area
    if pair == "ie":
        if before and nxt in ("r", "st", "rs"):
            return True                                  # happier, happiest
        if nxt[:1] == "t" or nxt.startswith(("nt", "nce")):
            return prev not in "ctsxg" and not x.startswith("fr")     # quiet, diet, client (not friend, ancient)
    return False


def _simple_breaks(w: str) -> List[int]:
    n = len(w)
    breaks: Set[int] = set()
    silent: Set[int] = set()               # vowels that are not syllables
    forced: Set[int] = set()               # a syllable starts here (a suffix after a vowel)
    end = n
    if n >= 5 and w.endswith("n't") and w[-4] not in _VY + "'r":
        breaks.add(n - 3)                  # did-n't, was-n't
        end = n - 3
    elif w.endswith("ren't"):
        silent.add(n - 4)                  # aren't, weren't
    x = w[:end]
    m = len(x)
    ing = None
    for suf in ("ing", "in'"):
        if x.endswith(suf) and m > 3 and _has_vowel(x[:m - 3]):
            ing = m - 3
            if x[m - 4] in _VY:
                forced.add(m - 3)          # go-ing, be-ing, dy-ing
    if m >= 4 and x.endswith("ed") and x[m - 3] not in _V and _has_vowel(x[:m - 2]):
        if x[m - 3] not in "td" and not _cle(x, m - 2) and not (x[m - 3] == "r" and x[m - 4] not in _VY):
            silent.add(m - 2)              # loved, played (not want-ed, trou-bled, hun-dred)
    elif m >= 4 and x.endswith("es") and x[m - 3] not in _V and _has_vowel(x[:m - 2]):
        if not x[:m - 2].endswith(("s", "x", "z", "ch", "sh", "c", "g")) and not _cle(x, m - 2):
            silent.add(m - 2)              # loves, times, eyes (not ro-ses, pla-ces, ta-bles)
    elif m >= 3 and x[-1] == "e" and x[m - 2] not in _V and _has_vowel(x[:m - 2]) and not _cle(x, m - 1) \
            and not (x[m - 2] == "r" and x[m - 3].isalpha() and x[m - 3] not in _VY):
        silent.add(m - 1)                  # love, fire, eye (not ta-ble, a-cre)
    for suf in ("ly", "ment", "ments", "ness", "ful", "fully", "less", "lessly"):
        k = m - len(suf) - 1
        if k >= 2 and x.endswith(suf) and x[k] == "e" and x[k - 1] not in _VY and _has_vowel(x[:k - 1]) \
                and not _cle(x, k):
            silent.add(k)                  # love-ly, care-less, state-ment

    def vowel(i: int) -> bool:
        c = x[i]
        if i in silent:
            return False
        if c in _V:
            return not (c == "u" and i > 0 and x[i - 1] == "q")
        if c == "y":
            if i == 0 or i in forced:
                return False
            j = i + 1
            return not (j < m and j not in forced and j not in silent and x[j] in _V)
        return False

    nuclei: List[Tuple[int, int]] = []
    i = 0
    while i < m:
        if not vowel(i):
            i += 1
            continue
        j = i + 1
        while j < m and vowel(j) and j not in forced and not _hiatus(x, j - 1, bool(nuclei) or j - 1 > i):
            j += 1
        nuclei.append((i, j))
        i = j
    for (_a0, a1), (b0, _b1) in zip(nuclei, nuclei[1:]):
        if x[b0] == "e" and _cle(x, b0) and b0 >= m - 3 and b0 - 2 >= a1:
            breaks.add(b0 - 2)             # lit-tle, ta-bles, trou-bled
        elif ing is not None and b0 == ing and b0 not in forced and _ing_whole(x, a1, b0):
            breaks.add(b0)                 # sing-ing, walk-ing (not run-ning)
        else:
            breaks.add(_split_point(x, a1, b0, silent))
    return sorted(p for p in breaks if 0 < p < n)


def _ing_whole(x: str, a1: int, b0: int) -> bool:
    """"-ing" after two or more consonants (not a doubled one) stays whole: "sing-ing", "walk-ing"."""
    run = [c for c in x[a1:b0] if c.isalpha() and c not in _V]
    return len(run) >= 2 and run[-1] != run[-2]


def _split_point(x: str, a1: int, b0: int, silent: Set[int]) -> int:
    """Where the consonants between two vowel groups split (``x[a1:b0]``)."""
    units: List[List[int]] = []
    i = a1
    while i < b0:
        if x[i] == "'" or (i in silent and units):
            if units:
                units[-1][1] = i + 1
            i += 1
            continue
        if i in silent:
            i += 1
            continue
        if i + 1 < b0 and x[i:i + 2] in _DIGRAPHS:
            units.append([i, i + 2])
            i += 2
        else:
            units.append([i, i + 1])
            i += 1
    if not units:
        return b0
    letters = ["".join(x[k] for k in range(s, e) if x[k].isalpha() and k not in silent) for s, e in units]
    if len(units) == 1:
        return units[0][1] if letters[0] in ("x", "ck") else units[0][0]
    if len(units) == 2:
        return units[0][0] if "".join(letters) in _ONSET_STRICT else units[1][0]
    if "".join(letters[-3:]) in _ONSET3:
        return units[-3][0]
    if "".join(letters[-2:]) in _ONSET2:
        return units[-2][0]
    return units[-1][0]


# ==============================================================================
# the vocal line as it is sung
# ==============================================================================

@dataclass(eq=False)
class Slot:
    """One note of the vocal line on the unrolled timeline (a tied note once)."""
    chord: object                     # pianovision.score.Chord
    measure: int                      # 1-based, file order
    passno: int                       # the measure's k-th pass
    utick: int                        # playback tick (repeats unrolled)
    seg: int                          # its repeat segment
    pitch: int
    slurred: bool                     # under a slur that started on an earlier chord
    slur_start: bool = False          # a slur starts on it
    ties: List[Tuple[object, int, int]] = field(default_factory=list)   # (chord, measure, seg) tied on

    def last_measure(self) -> int:
        return self.ties[-1][1] if self.ties else self.measure


@dataclass
class Phrase:
    index: int
    slots: List[Slot]
    gap: float                        # quarter notes of rest before it (from the start for the first)


@dataclass
class VocalLine:
    staff: int                        # 0-based
    name: str
    instrument: str
    slots: List[Slot]
    phrases: List[Phrase]
    played: Dict[int, int]            # measure -> passes
    other_voices: List[int]           # measures where voices 2-4 of the staff have notes
    existing: int                     # syllables already on the staff
    existing_measures: List[int]


def vocal_staves(score) -> List[int]:
    """The vocal staves (0-based) as the formatter classifies them (:func:`pianovision.pvjson.staff_roles`)."""
    from .pvjson import staff_roles
    from .render import Renderer
    midi = Renderer(score).render()
    return [t for t, _p in staff_roles(midi).vocal]


def vocal_line(score, staff: int) -> VocalLine:
    """The first voice of ``staff`` in playback order, as slots and phrases."""
    from .lyrics import staff_name
    from .repeats import build_repeat_list
    track = staff * VOICES
    st = score.staves[staff]
    slurs = [(sp.tick, sp.tick2) for sp in score.spanners if sp.tag == "Slur" and sp.track == track]
    slots: List[Slot] = []
    phrases: List[Phrase] = []
    cur: List[Slot] = []
    gap = 0.0
    passes: Dict[int, int] = {}
    last: Optional[Slot] = None
    for si, rs in enumerate(build_repeat_list(score)):
        off = rs.utick - rs.tick
        end = rs.end_tick
        for m in rs.measures:
            k = passes[m.index] = passes.get(m.index, 0) + 1
            for seg in m.segments:
                t = ticks(seg.tick)
                if t >= end:
                    break
                if seg.type != SEG_CHORDREST:
                    continue
                cr = seg.elements.get(track)
                if cr is None:
                    continue
                if not cr.is_chord or not cr.notes:
                    if cur:
                        phrases.append(Phrase(len(phrases), cur, gap))
                        cur, gap = [], 0.0
                    gap += float(cr.actual_ticks()) * 4
                    last = None
                    continue
                if last is not None and all(n.tie_back is not None and n.tie_back.start is not None
                                            for n in cr.notes):
                    last.ties.append((cr, m.index + 1, si))
                    continue
                slot = Slot(cr, m.index + 1, k, t + off, si, cr.notes[-1].pitch,
                            any(a < cr.tick <= b for a, b in slurs), any(a == cr.tick < b for a, b in slurs))
                cur.append(slot)
                slots.append(slot)
                last = slot
    if cur:
        phrases.append(Phrase(len(phrases), cur, gap))
    other, existing, ex_meas = set(), 0, set()
    for m in score.measures:
        for seg in m.segments:
            if seg.type != SEG_CHORDREST:
                continue
            for tr, cr in seg.elements.items():
                if tr // VOICES != staff:
                    continue
                if tr != track and cr.is_chord and cr.notes:
                    other.add(m.index + 1)
                if cr.lyrics:
                    existing += len(cr.lyrics)
                    ex_meas.add(m.index + 1)
    ins = st.part.instrument()
    return VocalLine(staff, staff_name(st), ins.instrument_id or "", slots, phrases,
                     {k + 1: v for k, v in passes.items()}, sorted(other), existing, sorted(ex_meas))


# ==============================================================================
# lines <-> phrases
# ==============================================================================

@dataclass
class Match:
    kind: str                         # "sung" | "line" (a line sung nowhere) | "phrase" (a phrase without a line)
    lines: List[int]                  # indices into the sung lines
    phrases: List[int]


def _fit(need: int, notes: int) -> float:
    return C_HELD * (notes - need) if notes >= need else C_SHORT * (need - notes)


def _rest_in_line(gap: float) -> float:
    return 0.1 * gap if gap <= 4 else 0.4 + 0.3 * (gap - 4)


def _anchor(line: TextLine, gap: float) -> float:
    c = 0.0
    if line.first and gap < 1.0:
        c += 0.6                                       # a section starts after a rest
    if not line.first and gap >= LONG_REST:
        c += min(3.0, 1.0 + 0.1 * (gap - LONG_REST))   # a long rest inside a section
    if line.after_break and gap < LONG_REST:
        c += 1.0                                       # an instrumental section without a long rest
    return c


def align(lines: Sequence[TextLine], phrases: Sequence[Phrase],
          pins: Optional[Dict[int, Set[int]]] = None) -> List[Match]:
    """The cheapest monotone matching of the sung lines to the phrases (module docstring); ``pins``: line index ->
    the phrases its match must start with."""
    pins = pins or {}
    n, m = len(lines), len(phrases)
    notes = [len(p.slots) for p in phrases]
    INF = float("inf")
    cost = [[INF] * (m + 1) for _ in range(n + 1)]
    back: List[List[Optional[Tuple[int, int, Match]]]] = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0

    def relax(i: int, j: int, c: float, how: Match) -> None:
        if c < cost[i][j] - 1e-9:
            cost[i][j] = c
            back[i][j] = (i0, j0, how)          # the cell being expanded (the loop's i0, j0)

    for i0 in range(n + 1):
        for j0 in range(m + 1):
            c0 = cost[i0][j0]
            if c0 == INF:
                continue
            if j0 < m:          # (on a tie, sing the earlier phrase: skipping costs a hair more early on)
                relax(i0, j0 + 1, c0 + C_SKIP_PHRASE + C_SKIP_NOTE * notes[j0] + 1e-3 * (m - j0) / m,
                      Match("phrase", [], [j0]))
            if i0 >= n:
                continue
            ln = lines[i0]
            relax(i0 + 1, j0, c0 + C_SKIP_LINE + C_SKIP_SYL * ln.need, Match("line", [i0], []))
            if j0 >= m or (i0 in pins and j0 not in pins[i0]):
                continue
            base = c0 + _anchor(ln, phrases[j0].gap if j0 else float("inf"))     # the song's start: a rest
            total, inner = 0, 0.0
            for k in range(1, KMAX + 1):
                j = j0 + k - 1
                if j >= m or k > len(ln.syls):
                    break
                total += notes[j]
                if k > 1:
                    inner += C_SPLIT + _rest_in_line(phrases[j].gap)
                span = C_HELD_SPAN * max(0, total - ln.need) if k > 1 else 0.0
                relax(i0 + 1, j + 1, base + _fit(ln.need, total) + inner + span,
                      Match("sung", [i0], list(range(j0, j + 1))))
            need, extra = ln.need, 0.0
            for r in range(2, RMAX + 1):
                i = i0 + r - 1
                if i >= n or i in pins:
                    break
                need += lines[i].need
                extra += C_JOIN + (C_JOIN_SECTION if lines[i].first else 0.0)
                relax(i + 1, j0 + 1, base + _fit(need, notes[j0]) + extra, Match("sung", list(range(i0, i + 1)), [j0]))
    out: List[Match] = []
    i, j = n, m
    while (i, j) != (0, 0):
        b = back[i][j]
        if b is None:                                   # cannot happen: skips always connect
            raise LyricsImportError("no matching of the lines to the phrases")
        i, j, how = b
        out.append(how)
    out.reverse()
    return out


# ==============================================================================
# syllables -> notes in one match
# ==============================================================================

@dataclass
class Unit:
    """One written syllable: one or more syllables of the text (merged) on its first slot, held over the others."""
    syls: List[int]                   # indices into the match's syllables
    slots: List[int]                  # indices into the match's slots


def assign(syls: Sequence[Syl], line_end: Sequence[bool], slots: Sequence[Slot],
           starts: Set[int]) -> Tuple[List[Unit], List[int]]:
    """The syllables of a match onto its notes, in order (module docstring): the units written and the slots left
    without a syllable.  ``line_end``: the syllable ends a line; ``starts``: the slots that begin a phrase."""
    S, N = len(syls), len(slots)
    INF = float("inf")
    cost = [[INF] * (N + 1) for _ in range(S + 1)]
    back: List[List[Optional[tuple]]] = [[None] * (N + 1) for _ in range(S + 1)]
    cost[0][0] = 0.0

    def held(k: int, a: int) -> float:
        s, p = slots[k], slots[k - 1]
        c = P_HOLD
        if s.slurred:
            c += P_HOLD_SLUR
        elif s.pitch == p.pitch:
            c += P_HOLD_SAME
        if s.slur_start:
            c += P_HOLD_SLUR_START
        if not syls[a].word_end:
            c += P_HOLD_WORD
        if line_end[a]:
            c += P_HOLD_END
        return max(c, 0.02)

    for a in range(S + 1):
        for b in range(N + 1):
            c0 = cost[a][b]
            if c0 == INF:
                continue
            if b < N and c0 + P_UNSUNG < cost[a][b + 1]:
                cost[a][b + 1] = c0 + P_UNSUNG
                back[a][b + 1] = ("unsung", a, b)
            if a >= S:
                continue
            sy = syls[a]
            if a > 0 and b > 0:
                c = c0 + (P_ELIDE if sy.word_start else P_MERGE_WORD)
                if c < cost[a + 1][b]:
                    cost[a + 1][b] = c
                    back[a + 1][b] = ("merge", a, b)
            if b >= N:
                continue
            c = c0 + (P_SPLIT_WORD if b in starts and b > 0 and not sy.word_start else 0.0)
            for t in range(1, sy.holds + HOLD_MAX + 2):
                e = b + t                                 # notes b .. e-1
                if e > N or (t > 1 and (e - 1) in starts):
                    break
                if t > 1:
                    c += 0.0 if t - 1 <= sy.holds else held(e - 1, a)
                miss = P_HOLD_MISSING * max(0, sy.holds - (t - 1))
                if c + miss < cost[a + 1][e]:
                    cost[a + 1][e] = c + miss
                    back[a + 1][e] = ("sing", a, b)
    units: List[Unit] = []
    unsung: List[int] = []
    a, b = S, N
    pending: List[int] = []                               # syllables merged into the unit before them
    while (a, b) != (0, 0):
        kind, pa, pb = back[a][b]
        if kind == "unsung":
            unsung.append(pb)
        elif kind == "merge":
            pending.append(pa)
        else:
            units.append(Unit([pa] + list(reversed(pending)), list(range(pb, b))))
            pending = []
        a, b = pa, pb
    units.reverse()
    unsung.reverse()
    return units, unsung


def _syllabic(syls: Sequence[Syl]) -> str:
    left = syls[0].syllabic in ("middle", "end")
    right = syls[-1].syllabic in ("begin", "middle")
    return {(False, False): "single", (False, True): "begin", (True, False): "end", (True, True): "middle"}[
        (left, right)]


def _unit_text(syls: Sequence[Syl]) -> str:
    out = syls[0].text
    for s in syls[1:]:
        out += (ELISION if s.word_start else "") + s.text
    return out


# ==============================================================================
# the plan
# ==============================================================================

@dataclass
class Placement:
    slot: Slot
    verse: int                        # MuseScore <no>: the pass - 1
    text: str                         # ELISION between two words on one note
    syllabic: str
    ticks_f: Optional[Fraction]       # the extender's length (whole notes), None: none
    line: int                         # index of the sung line of its first syllable
    held: List[Slot] = field(default_factory=list)


@dataclass
class LineReport:
    index: int                        # in LyricsText.sung
    line: TextLine
    match: int                        # index of its match (lines sung in one phrase share it)
    phrases: List[int]
    syllables: int = 0
    notes: int = 0                    # notes of its syllables (held ones too) and notes left over after them
    held: int = 0                     # notes held over (melismas)
    merged: int = 0                   # syllables merged onto another's note
    unsung: int = 0                   # notes left without a syllable
    slots: List[Slot] = field(default_factory=list)

    @property
    def placed(self) -> bool:
        return self.match >= 0

    @property
    def mismatch(self) -> bool:
        return not self.placed or self.merged > 0 or self.unsung > 0


@dataclass
class ImportPlan:
    score_path: str
    mscz: bytes
    score: object
    vocal: VocalLine
    text: LyricsText
    matches: List[Match]
    lines: List[LineReport]
    placements: List[Placement]
    unsung_phrases: List[int]
    warnings: List[str]
    replace: bool = False


def _contiguous(prev_measure: int, prev_seg: int, measure: int, seg: int) -> bool:
    """Two consecutive places of the playback follow each other in the written score too."""
    return seg == prev_seg or measure in (prev_measure, prev_measure + 1)


def _extender(first: Slot, held: Sequence[Slot]) -> Optional[Fraction]:
    """The extender of a syllable on ``first`` held over ``held``: up to the last chord it holds that follows on
    in the written score (a jump or a repeat ends it)."""
    last = None
    pm, ps = first.last_measure(), (first.ties[-1][2] if first.ties else first.seg)
    for s in held:
        if not _contiguous(pm, ps, s.measure, s.seg):
            break
        last, pm, ps = s.chord, s.measure, s.seg
        for c, meas, seg in s.ties:
            if not _contiguous(pm, ps, meas, seg):
                break
            last, pm, ps = c, meas, seg
    if last is None:
        return None
    d = last.tick - first.chord.tick
    return d if d > 0 else None


def _pins(text: LyricsText, phrases: Sequence[Phrase], warnings: List[str]) -> Dict[int, Set[int]]:
    """Line index -> the phrases it must start with: the first line of a pinned section (its first time)."""
    pins: Dict[int, Set[int]] = {}
    done: Set[int] = set()
    for i, ln in enumerate(text.sung):
        sec = text.sections[ln.section]
        if not ln.first or sec.pin is None or ln.section in done:
            continue
        done.add(ln.section)
        meas, pas = sec.pin
        cand = {p.index for p in phrases if p.slots[0].measure == meas and (pas is None or p.slots[0].passno == pas)}
        if cand:
            pins[i] = cand
        else:
            where = f"m.{meas}" + (f" pass {pas}" if pas else "")
            warnings.append(f"line {sec.lineno}: [{sec.name}] is pinned to {where}, where no phrase starts; "
                            f"pin not used")
    return pins


def plan_import(score_path: str, text: str, staff: Optional[int] = None, keep_parentheses: bool = False,
                replace: bool = False, split: Optional[Callable[[str], List[str]]] = None) -> ImportPlan:
    """Everything :func:`build_mscz` writes, and the report (:func:`format_plan`); writes nothing.  ``staff``:
    the vocal staff, 1-based (default: the score's one vocal staff)."""
    from .mscx import read_score
    with open(score_path, "rb") as f:
        mscz = f.read()
    score = read_score(score_path)
    warnings: List[str] = []
    if staff is not None:
        if not 1 <= staff <= len(score.staves):
            raise LyricsImportError(f"--staff {staff}: the score has staves 1-{len(score.staves)}")
        st = staff - 1
        if st not in vocal_staves(score):
            warnings.append(f"staff {staff} is not a vocal staff for the formatter; its lyrics are used as given")
    else:
        vs = vocal_staves(score)
        if len(vs) != 1:
            from .lyrics import staff_name
            names = ", ".join(f"{i + 1} ({staff_name(s)})" for i, s in enumerate(score.staves))
            raise LyricsImportError(("no vocal staff" if not vs else
                                     "several vocal staves (" + ", ".join(str(v + 1) for v in vs) + ")")
                                    + f"; name one with --staff N (staves: {names})")
        st = vs[0]
    vocal = vocal_line(score, st)
    if vocal.existing and not replace:
        from .scorefix import ranges
        raise LyricsImportError(f"staff {st + 1} already has {vocal.existing} syllables "
                                f"(m.{ranges(vocal.existing_measures)}); --replace removes them first")
    if not vocal.slots:
        raise LyricsImportError(f"staff {st + 1} has no notes in its first voice")
    lt = parse_text(text, keep_parentheses, split)
    warnings = lt.warnings + warnings
    if not lt.sung:
        raise LyricsImportError("the lyrics file has no sung line")
    pins = _pins(lt, vocal.phrases, warnings)
    matches = align(lt.sung, vocal.phrases, pins)
    reports = [LineReport(i, ln, -1, []) for i, ln in enumerate(lt.sung)]
    placements: List[Placement] = []
    unsung_phrases: List[int] = []
    for mi, mt in enumerate(matches):
        if mt.kind == "phrase":
            unsung_phrases.extend(mt.phrases)
            continue
        if mt.kind == "line":
            continue
        slots: List[Slot] = []
        starts: Set[int] = set()
        for pj in mt.phrases:
            starts.add(len(slots))
            slots.extend(vocal.phrases[pj].slots)
        syls: List[Syl] = []
        owner: List[int] = []
        ends: List[bool] = []
        for li in mt.lines:
            ln = lt.sung[li]
            for k, s in enumerate(ln.syls):
                syls.append(s)
                owner.append(li)
                ends.append(k == len(ln.syls) - 1)
            rp = reports[li]
            rp.match, rp.phrases, rp.syllables = mi, list(mt.phrases), len(ln.syls)
        units, unsung = assign(syls, ends, slots, starts)
        for u in units:
            first = slots[u.slots[0]]
            held = [slots[k] for k in u.slots[1:]]
            us = [syls[k] for k in u.syls]
            syl = _syllabic(us)
            ext = _extender(first, held) if held and syl in ("single", "end") else None
            li = owner[u.syls[0]]
            placements.append(Placement(first, first.passno - 1, _unit_text(us), syl, ext, li, held))
            rp = reports[li]
            rp.notes += len(u.slots)
            rp.held += len(u.slots) - 1
            rp.merged += len(u.syls) - 1
            rp.slots.extend(slots[k] for k in u.slots)
        for k in unsung:
            # a note left over belongs to the line sung before it (the match's first line before any)
            before = [owner[u.syls[0]] for u in units if u.slots[0] < k]
            rp = reports[before[-1] if before else mt.lines[0]]
            rp.notes += 1
            rp.unsung += 1
            rp.slots.append(slots[k])
    return ImportPlan(score_path, mscz, score, vocal, lt, matches, reports, placements, unsung_phrases, warnings,
                      replace)


# ==============================================================================
# the report (counts, line and measure numbers; never the text)
# ==============================================================================

def _ordinal(k: int) -> str:
    return f"{k}{'th' if 10 <= k % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(k % 10, 'th')}"


def measures_text(slots: Sequence[Slot], played: Dict[int, int]) -> str:
    """``m.41, m.10-13 (2nd time)``: the measures of the slots in playback order, with the pass where the measure
    is played more than once."""
    runs: List[List[int]] = []                   # [first, last, pass]
    for s in slots:
        for meas in [s.measure] + [t[1] for t in s.ties]:
            if runs and runs[-1][2] == s.passno and meas in (runs[-1][1], runs[-1][1] + 1):
                runs[-1][1] = meas
            else:
                runs.append([meas, meas, s.passno])
    out = []
    for a, b, k in runs:
        t = f"m.{a}" if a == b else f"m.{a}-{b}"
        if played.get(a, 1) > 1:
            t += f" ({_ordinal(k)} time)"
        out.append(t)
    return ", ".join(out)


def format_plan(plan: ImportPlan) -> List[str]:
    """The dry run's report: per section and line, syllables against notes, holds, merges and notes left over,
    with measure numbers."""
    v, lt = plan.vocal, plan.text
    out: List[str] = []
    nphr = len(v.phrases)
    nnotes = sum(len(p.slots) for p in v.phrases)
    out.append(f"{os.path.basename(plan.score_path)}: vocal line staff {v.staff + 1} ({v.name or '?'}, "
               f"{v.instrument or 'no instrument id'}), voice 1: {nnotes} notes to sing (a tied note once) in "
               f"{nphr} phrase{'s' if nphr != 1 else ''}")
    if v.other_voices:
        from .scorefix import ranges
        out.append(f"  voices 2-4 of the staff have notes in m.{ranges(v.other_voices)}: the lyrics go on voice 1 only")
    if plan.replace and v.existing:
        out.append(f"  --replace: the staff's {v.existing} syllables are removed first")
    sung_secs = sum(1 for s in lt.sections if not s.instrumental)
    nsyl = sum(len(ln.syls) for ln in lt.sung)
    out.append(f"text: {len(lt.sections)} sections ({sung_secs} sung, {len(lt.sections) - sung_secs} instrumental), "
               f"{len(lt.sung)} sung lines, {nsyl} syllables; syllables by {lt.syllabifier}")
    if plan.warnings:
        out.append("warnings:")
        out.extend(f"  {w}" for w in plan.warnings)
    out.append("sections:")
    by_section: Dict[int, List[LineReport]] = {}
    for rp in plan.lines:
        by_section.setdefault(rp.line.section, []).append(rp)
    for si, sec in enumerate(lt.sections):
        head = f"  [{sec.name}]" + (f" (line {sec.lineno})" if sec.lineno else "")
        if sec.instrumental:
            out.append(head + ": instrumental")
            continue
        rps = by_section.get(si, [])
        if sec.repeat_of is not None:
            head += f": line{'s' if len(rps) > 1 else ''} {_lines_text_short([r.line.lineno for r in rps])} again"
        slots = [s for r in rps for s in r.slots]
        slots.sort(key=lambda s: s.utick)
        verses = sorted({s.passno for s in slots})
        where = measures_text(slots, v.played) if slots else "not placed"
        out.append(f"{head}: {where}" + (f"; verse{'s' if len(verses) > 1 else ''} "
                                         f"{', '.join(str(x) for x in verses)}" if verses else ""))
        for rp in rps:
            out.append("    " + _line_text(plan, rp))
    if plan.unsung_phrases:
        sl = [s for j in plan.unsung_phrases for s in v.phrases[j].slots]
        out.append(f"phrases no line is sung on: {len(plan.unsung_phrases)} ({len(sl)} notes): "
                   f"{measures_text(sl, v.played)}")
    out.extend(_summary(plan))
    return out


def _lines_text_short(nums: Sequence[int]) -> str:
    from .scorefix import ranges
    return ranges(sorted(set(nums)))


def _line_text(plan: ImportPlan, rp: LineReport) -> str:
    ln = rp.line
    tag = f"line {ln.lineno}" + (" (again)" if ln.again else "")
    if not rp.placed:
        return f"{tag}: {rp.syllables or len(ln.syls)} syllables, not placed (no phrase fits): mismatch"
    mt = plan.matches[rp.match]
    if len(mt.lines) > 1 and mt.lines[0] != rp.index:
        first = plan.text.sung[mt.lines[0]].lineno
        how = f"in one phrase with line {first}"
    elif len(mt.lines) > 1:
        others = ", ".join(str(plan.text.sung[i].lineno) for i in mt.lines[1:])
        how = f"one phrase with line{'s' if len(mt.lines) > 2 else ''} {others} (no rest between them)"
    elif len(mt.phrases) > 1:
        how = f"over {len(mt.phrases)} phrases (rests inside the line)"
    else:
        how = "one phrase"
    need_note = f" (+{ln.need - len(ln.syls)} held by _)" if ln.need > len(ln.syls) else ""
    parts = [f"{rp.syllables} syllables{need_note} on {rp.notes} notes"]
    if rp.held:
        parts.append(f"{rp.held} held")
    if rp.merged:
        parts.append(f"{rp.merged} merged")
    if rp.unsung:
        parts.append(f"{rp.unsung} without a syllable")
    slots = sorted(rp.slots, key=lambda s: s.utick)
    return (f"{tag}: {', '.join(parts)}; {how}; {measures_text(slots, plan.vocal.played)}"
            + (": mismatch" if rp.mismatch else ""))


def _summary(plan: ImportPlan) -> List[str]:
    rps = plan.lines
    sung = [m for m in plan.matches if m.kind == "sung"]
    one = sum(1 for m in sung if len(m.lines) == 1 and len(m.phrases) == 1)
    over = sum(1 for m in sung if len(m.phrases) > 1)
    joined = sum(len(m.lines) for m in sung if len(m.lines) > 1)
    unplaced = sum(1 for r in rps if not r.placed)
    held = sum(r.held for r in rps)
    merged = sum(r.merged for r in rps)
    unsung = sum(r.unsung for r in rps)
    mism = sum(1 for r in rps if r.mismatch)
    verses: Dict[int, int] = {}
    for p in plan.placements:
        verses[p.verse + 1] = verses.get(p.verse + 1, 0) + 1
    out = [f"matching: {len(rps)} lines on {len(plan.vocal.phrases)} phrases: {one} one line to one phrase, {over} "
           f"over several phrases, {joined} sharing a phrase, {unplaced} not placed; "
           f"{len(plan.unsung_phrases)} phrases without a line",
           f"syllables: {len(plan.placements)} written ({held} notes held, {merged} syllables merged, {unsung} notes "
           f"without a syllable); {mism} lines with a mismatch",
           "verses written: " + (", ".join(f"verse {k}: {verses[k]}" for k in sorted(verses)) or "none")]
    return out


# ==============================================================================
# writing
# ==============================================================================

def _xml_text(text: str) -> str:
    t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return t.replace(ELISION, "<sym>lyricsElision</sym>")


def lyrics_xml(p: Placement, indent: Optional[str]) -> str:
    """A ``<Lyrics>`` element as MuseScore 4.7.5 writes it (``indent`` None: on one line)."""
    kids = []
    if p.verse:
        kids.append(f"<no>{p.verse}</no>")
    if p.syllabic != "single":
        kids.append(f"<syllabic>{p.syllabic}</syllabic>")
    if p.ticks_f:
        kids.append(f"<ticks_f>{p.ticks_f.numerator}/{p.ticks_f.denominator}</ticks_f>")
    kids.append(f"<text>{_xml_text(p.text)}</text>")
    if indent is None:
        return "<Lyrics>" + "".join(kids) + "</Lyrics>"
    return "".join([f"{indent}<Lyrics>\n"] + [f"{indent}  {k}\n" for k in kids] + [f"{indent}  </Lyrics>\n"])


def build_mscz(plan: ImportPlan) -> bytes:
    """The score with the plan's syllables (and, with ``replace``, without the staff's old ones)."""
    from .handedits import Patches, PlanError, ScoreDoc, _indent, _span, replace_mscx
    try:
        doc = ScoreDoc(plan.mscz, plan.score)
    except PlanError as e:
        raise LyricsImportError(str(e)) from None
    P = Patches(doc.data)
    staff = plan.vocal.staff
    if plan.replace:
        for path, cr in doc.cr_by_path.items():
            if path[0] == staff and cr.lyrics:
                for node in doc.node(path).findall("Lyrics"):
                    try:
                        P.remove(node)
                    except PlanError:
                        P.add(node.start, node.end, b"")
    by_chord: Dict[int, List[Placement]] = {}
    for p in plan.placements:
        by_chord.setdefault(id(p.slot.chord), []).append(p)
    for ps in by_chord.values():
        chord = ps[0].slot.chord
        if chord.xml_path is None:
            raise LyricsImportError(f"m.{ps[0].slot.measure}: a chord the reader has no place for")
        node = doc.node(chord.xml_path)
        anchor = None
        for c in node.children:
            if c.tag in ("durationType", "duration"):
                anchor = c
            elif anchor is not None:
                break
        if anchor is None:
            raise LyricsImportError(f"m.{ps[0].slot.measure}: a chord without a duration")
        entries = sorted(ps, key=lambda p: p.verse)
        try:
            _s, e = _span(doc.data, anchor)
            own_line = doc.data[anchor.end:anchor.end + 1] in (b"\n", b"\r")
            ind = _indent(doc.data, anchor)
        except PlanError:
            own_line = False
        if own_line:
            P.add(e, e, "".join(lyrics_xml(p, ind) for p in entries))
        else:
            P.add(anchor.end, anchor.end, "".join(lyrics_xml(p, None) for p in entries))
    return replace_mscx(plan.mscz, doc.member, P.result())


# ==============================================================================
# verification
# ==============================================================================

@dataclass
class Verification:
    ok: bool
    lines: List[str]


def _without_lyrics(doc):
    if isinstance(doc, dict):
        return {k: _without_lyrics(v) for k, v in doc.items() if k not in ("lyric", "sung", "lyrics")}
    if isinstance(doc, list):
        return [_without_lyrics(v) for v in doc]
    return doc


def expected_lyrics(plan: ImportPlan) -> Dict[Tuple[tuple, int], Tuple[str, str]]:
    """(chord xml path, pass) -> (text as :mod:`pianovision.lyrics` reads it, syllabic) of every placement."""
    from .lyrics import lyric_text
    return {(p.slot.chord.xml_path, p.slot.passno): (lyric_text(ET.fromstring(lyrics_xml(p, None))), p.syllabic)
            for p in plan.placements}


def _mem_available_mb() -> Optional[int]:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def musescore_check(mscore: str, path: str, work: str) -> Tuple[Optional[bool], str, int]:
    """MuseScore headless opens ``path`` and exports it as MusicXML: (ok, what happened, lyrics exported);
    ok None: not checked."""
    if not mscore or not os.path.isfile(mscore):
        return None, f"MuseScore not found ({mscore or 'no path'}): not checked", 0
    avail = _mem_available_mb()
    if avail is not None and avail < MIN_FREE_MB:
        return None, f"only {avail} MB of memory available: MuseScore's check skipped", 0
    env = dict(os.environ)
    base = os.path.join(work, "xdg")
    for k in ("config", "cache", "data"):
        os.makedirs(os.path.join(base, k), exist_ok=True)
        env[f"XDG_{k.upper()}_HOME"] = os.path.join(base, k)
    env["QT_QPA_PLATFORM"] = "offscreen"
    out = os.path.splitext(path)[0] + ".check.musicxml"
    if os.path.exists(out):
        os.unlink(out)
    try:
        r = subprocess.run([mscore, "-o", out, path], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=MUSESCORE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"MuseScore did not finish in {MUSESCORE_TIMEOUT} s", 0
    except OSError as e:
        return False, f"MuseScore could not run: {e}", 0
    if r.returncode != 0 or not os.path.isfile(out):
        tail = r.stdout.decode("utf-8", "replace").strip().splitlines()[-3:]
        return False, f"MuseScore failed to open/export the score (exit {r.returncode})" + \
            (": " + " | ".join(tail) if tail else ""), 0
    with open(out, "rb") as f:
        n = len(re.findall(rb"<lyric[\s>]", f.read()))
    os.unlink(out)
    return True, "MuseScore opened and exported it", n


def _lyrics_elements(mscz: bytes) -> int:
    from .handedits import _main_mscx_name
    with zipfile.ZipFile(io.BytesIO(mscz)) as z:
        data = z.read(_main_mscx_name(z))
    return len(re.findall(rb"<Lyrics>", data))


def verify(plan: ImportPlan, new_path: str, mscore: Optional[str] = None, work: Optional[str] = None) -> Verification:
    """Checks the edited score at ``new_path`` (module docstring); ``mscore`` None skips MuseScore's check."""
    from .convert import convert_mscz
    from .scorefix import ranges
    work = os.path.expanduser(work or WORK_DIR)
    out: List[str] = []
    ok = True
    orig = os.path.join(os.path.dirname(new_path), "original-" + os.path.basename(new_path))
    with open(orig, "wb") as f:
        f.write(plan.mscz)
    try:
        c0 = convert_mscz(orig, parts=True, keep_midi=True)
        try:
            c1 = convert_mscz(new_path, parts=True, keep_midi=True)
        except Exception as e:                            # noqa: BLE001 - any failure to read is the finding
            return Verification(False, [f"FAILED: the formatter cannot read the edited score: {e}"])
    finally:
        os.unlink(orig)
    if c0.data == c1.data:
        out.append("the formatter reads it and renders the same notes (PianoVision JSON byte for byte)")
    else:
        ok = False
        out.append("FAILED: the rendered notes differ (PianoVision JSON)")
    if _without_lyrics(c0.parts) == _without_lyrics(c1.parts):
        out.append("the parts file is the same but for its lyrics")
    else:
        ok = False
        out.append("FAILED: the parts file differs beyond its lyrics")
    exp = expected_lyrics(plan)
    timeline = getattr(c1.midi, "lyrics", None)
    got: Dict[Tuple[tuple, int], Tuple[str, str]] = {}
    for e in (timeline.events if timeline else []):
        if e.staff == plan.vocal.staff:
            got[(e.chord.xml_path, e.passes)] = (e.text, e.syllabic)
    where = {(p.slot.chord.xml_path, p.slot.passno): p.slot.measure for p in plan.placements}
    missing = [k for k in exp if k not in got]
    wrong = [k for k in exp if k in got and got[k] != exp[k]]
    extra = [k for k in got if k not in exp]
    found = len(exp) - len(missing) - len(wrong)
    out.append(f"lyrics.collect_lyrics: {found} of {len(exp)} syllables on their notes and passes")
    if missing:
        ok = False
        out.append(f"FAILED: {len(missing)} not found, m.{ranges(sorted({where[k] for k in missing}))}")
    if wrong:
        ok = False
        out.append(f"FAILED: {len(wrong)} read differently, m.{ranges(sorted({where[k] for k in wrong}))}")
    if extra:
        ex = set(extra)
        meas = sorted({e.measure for e in timeline.events if (e.chord.xml_path, e.passes) in ex})
        out.append(f"note: {len(extra)} notes also show a syllable on a pass without one of their own (a measure "
                   f"whose pass sings no syllable shows its lowest verse): m.{ranges(meas)}")
    summary = (c1.parts or {}).get("lyrics")
    if summary:
        out.append(f"parts file: {summary['syllables']} syllables on notes, {summary['verses']} verses, "
                   f"staves {', '.join(summary['staves'])}")
    if mscore is not None:
        with open(new_path, "rb") as f:
            n_written = _lyrics_elements(f.read())
        res, msg, n = musescore_check(mscore, new_path, work)
        if res is False:
            ok = False
            out.append("FAILED: " + msg)
        elif res is None:
            out.append("note: " + msg)
        else:
            out.append(f"{msg} ({n} lyrics in its MusicXML; the score has {n_written} <Lyrics>)"
                       + ("" if n == n_written else ": counts differ"))
    else:
        out.append("MuseScore's check: not run")
    return Verification(ok, out)
