"""Chord-symbol (Harmony) playback realization, ported from MuseScore 4.6.

Sources: ``dom/harmony.cpp``, ``dom/realizedharmony.cpp``, ``dom/chordlist.cpp``
(``ParsedChord::parse``, ``ChordList::read``), ``dom/pitchspelling.cpp`` and the
Harmony readers in ``rw/read400|read410|read460``.  Stdlib only.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from bisect import insort_right
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

__all__ = [
    "ChordList", "ParsedChord", "HarmonyData", "load_chord_list", "load_harmony_style", "read_harmony",
    "harmony_plays", "harmony_is_realizable", "realize_harmony", "parse_chord", "actual_duration",
    "VOICING_AUTO", "VOICING_ROOT_ONLY", "VOICING_CLOSE", "VOICING_DROP_2", "VOICING_SIX_NOTE",
    "VOICING_FOUR_NOTE", "VOICING_THREE_NOTE", "UNTIL_NEXT_CHORD_SYMBOL", "STOP_AT_MEASURE_END",
    "SEGMENT_DURATION",
]

# enum class Voicing (realizedharmony.h)
VOICING_INVALID, VOICING_AUTO, VOICING_ROOT_ONLY, VOICING_CLOSE = -1, 0, 1, 2
VOICING_DROP_2, VOICING_SIX_NOTE, VOICING_FOUR_NOTE, VOICING_THREE_NOTE = 3, 4, 5, 6
# enum class HDuration
HDURATION_INVALID, UNTIL_NEXT_CHORD_SYMBOL, STOP_AT_MEASURE_END, SEGMENT_DURATION = -1, 0, 1, 2
# enum class HarmonyType
HARMONY_STANDARD, HARMONY_ROMAN, HARMONY_NASHVILLE = 0, 1, 2

# Style defaults (style/styledef.cpp) for the values used here.
DEFAULT_STYLE: Dict[str, object] = {
    "harmonyVoiceLiteral": True,
    "harmonyVoicing": VOICING_AUTO,
    "harmonyDuration": UNTIL_NEXT_CHORD_SYMBOL,
    "concertPitch": False,
    "chordsXmlFile": False,
    "chordDescriptionFile": "chords_std.xml",
    "chordStyle": "std",
}

# ---------------------------------------------------------------------------
# C++ integer helpers
# ---------------------------------------------------------------------------


def _cmod(a: int, b: int) -> int:
    """C++ ``a % b`` (truncates toward zero)."""
    r = abs(a) % abs(b)
    return -r if a < 0 else r


def _shl1(n: int) -> int:
    """``1 << n`` as executed on x86 for an int (shift count masked to 5 bits)."""
    return 1 << (n & 31)


def _to_int(s: str) -> int:
    """muse::String::toInt(): strtol base 10, 0 unless the whole string is consumed."""
    m = re.fullmatch(r"[ \t\n\v\f\r]*([+-]?[0-9]+)", s)
    if not m:
        return 0
    v = int(m.group(1))
    if not -2 ** 63 <= v < 2 ** 63:          # strtol saturates at LONG_MIN/LONG_MAX
        v = 2 ** 63 - 1 if v > 0 else -2 ** 63
    v &= 0xFFFFFFFF                          # static_cast<int>
    return v - 2 ** 32 if v >= 2 ** 31 else v


def _isdigit(c: str) -> bool:
    return "0" <= c <= "9"                   # Char::isDigit is ASCII only


def _lower(c: str) -> str:
    lc = c.lower()
    return lc if len(lc) == len(c) else c


# ---------------------------------------------------------------------------
# Pitch spelling (dom/pitchspelling.cpp)
# ---------------------------------------------------------------------------

TPC_INVALID, TPC_MIN, TPC_MAX = -9, -8, 40
_TPC_PITCHES = (2, -3, 4, -1, 6, 1, 8, 3, -2, 5, 0, 7, 2, 9, 4, -1, 6, 1, 8, 3, 10, 5, 0, 7, 2, 9, 4, 11,
                6, 1, 8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 13, 8, 3, 10, 5, 12, 7, 14)


def tpc_is_valid(tpc: int) -> bool:
    return TPC_MIN <= tpc <= TPC_MAX


def tpc2pitch(tpc: int) -> int:
    """Pitch class of a TPC; not normalized (Cb = -1, B# = 12)."""
    return _TPC_PITCHES[tpc - TPC_MIN]


def _step2pitch_interval(step: int, alter: int) -> int:
    return (0, 2, 4, 5, 7, 9, 11)[(step - 1) % 7] + alter


def _tpc_interval(start_tpc: int, interval: int, alter: int) -> int:
    result = start_tpc + (0, 2, 4, -1, 1, 3, 5)[(interval - 1) % 7] + alter * 7
    while result > TPC_MAX:
        result -= 12
    while result < TPC_MIN:
        result += 12
    return result


def _function2tpc(s: str, key: int) -> int:
    """function2Tpc(): Nashville number -> TPC in ``key`` (-7..7)."""
    alter = 0
    if s and _isdigit(s[0]):
        step = int(s[0])
    elif len(s) > 1:
        acc, num = s[:-1], s[-1]
        if not _isdigit(num):
            return TPC_INVALID
        step = int(num)
        if acc.startswith("bb"):
            alter = -2
        elif acc.startswith("b"):
            alter = -1
        elif acc.startswith("#"):
            alter = 1
    else:
        return TPC_INVALID
    if step <= 0:
        return TPC_INVALID
    return _tpc_interval(key + 14, step, alter)


# ---------------------------------------------------------------------------
# ParsedChord::parse (dom/chordlist.cpp) -- only the semantic outputs
# ---------------------------------------------------------------------------

_MAJOR = ("ma", "maj", "major", "t", "^")
_MINOR = ("mi", "min", "minor", "-", "=")
_DIMINISHED = ("dim", "o")
_AUGMENTED = ("aug", "+")
_LOWER = ("b", "-", "dim")
_RAISE = ("#", "+", "aug")
_MOD = ("sus", "add", "no", "omit", "^", "type")
_EXT_DIGITS = "123456789"
_SPECIAL = "()[],/\\ "
_LEADING = "([ "
_TRAILING = ")],/\\ "


@dataclass
class ParsedChord:
    name: str = ""
    quality: str = ""
    extension: str = ""
    modifier_list: List[str] = field(default_factory=list)
    understandable: bool = True
    parseable: bool = True


def parse_chord(s: str, prefer_minor: bool = False) -> ParsedChord:
    """ParsedChord::parse(s, cl, syntaxOnly=false, preferMinor) -> quality/extension/modifiers."""
    pc = ParsedChord(name=s)
    n = len(s)
    i = 0

    # --- quality
    tok1 = tok1L = initial = ""
    while i < n:
        c = s[i]
        if c in _EXT_DIGITS or c in _SPECIAL:
            break
        tok1 += c
        tok1L += _lower(c)
        if tok1L == "m" or tok1L in _MAJOR or tok1L in _MINOR or tok1L in _DIMINISHED or tok1L in _AUGMENTED:
            initial = tok1
        i += 1
    if tok1L.startswith("madd"):
        initial = tok1[0]
    if initial and initial != tok1 and tok1L not in ("tristan", "omit", "type"):
        i -= len(tok1) - len(initial)
        tok1 = initial
        tok1L = "".join(_lower(c) for c in initial)
    if tok1 == "M" or tok1L in _MAJOR:
        pc.quality = "major"
    elif tok1 == "m" or tok1L in _MINOR:
        pc.quality = "minor"
    elif tok1L in _DIMINISHED:
        pc.quality = "diminished"
    elif tok1L in _AUGMENTED:
        pc.quality = "augmented"
    elif tok1L == "0":
        pc.quality = "half-diminished"
    elif tok1L == "":
        pc.quality = ""
        if prefer_minor:
            pc.name = "=" + pc.name
    else:
        pc.quality = ""
        tok1 = tok1L = ""
        i = 0                                   # lastLeadingToken of a fresh ParsedChord
    while i < n and s[i] in _TRAILING:
        i += 1

    # --- type ("typeN" or roman numerals)
    prev = i
    tok1 = ""
    while i < n and not _isdigit(s[i]):
        tok1 += s[i]
        i += 1
    if tok1 == "type":
        while i < n and _isdigit(s[i]):
            i += 1
    elif re.search(r"[IVX]+", tok1):
        i = len(tok1)                           # C++ reuses i as the token index
    else:
        i = prev

    # --- extension
    tok1 = ""
    while i < n and (_isdigit(s[i]) or s[i] in ",/"):
        tok1 += s[i]
        i += 1
    pc.extension = tok1
    if pc.quality == "":
        if pc.extension in ("7", "9", "11", "13"):
            pc.quality = "minor" if prefer_minor else "dominant"
        else:
            pc.quality = "minor" if prefer_minor else "major"
    if tok1 in ("69", "6,9", "6/9"):
        pc.extension = "69"
    while i < n and s[i] in _TRAILING:
        i += 1

    # --- modifiers
    add_pending = sus_pending = False
    mods: List[str] = []
    guard = 0
    while i < n:
        guard += 1
        if guard > 4 * n + 8:                   # cannot happen for real input; C++ would not stop
            break
        while i < n and s[i] in _LEADING:
            i += 1
        tok1 = tok1L = initial = ""
        while i < n:
            if _isdigit(s[i]) or s[i] in _SPECIAL:
                break
            tok1 += s[i]
            tok1L += _lower(s[i])
            if tok1L in _MOD:
                initial = tok1
            i += 1
        if i == n and not tok1:
            break
        if initial and initial != tok1:
            i -= len(tok1) - len(initial)
            tok1 = initial
            tok1L = "".join(_lower(c) for c in initial)
        if tok1L == "add":
            add_pending = True
            continue
        if tok1L == "sus" and i != n:
            sus_pending = True
            continue
        while i < n and s[i] == " ":
            i += 1
        tok2 = ""
        while i < n:
            if not _isdigit(s[i]):
                break
            if len(tok2) == 1 and (tok2[0] != "1" or s[i] > "3"):
                break
            tok2 += s[i]
            i += 1
        suffix = s[i:i + 2]
        if re.search(r"st|nd|rd|th", suffix):
            i += 2
        else:
            suffix = ""
        tok2L = tok2.lower()
        for pending, word in ((add_pending, "add"), (sus_pending, "sus")):
            if pending:
                if tok1L in _RAISE:
                    tok1L = "#"
                elif tok1L in _LOWER:
                    tok1L = "b"
                elif tok1 == "M" or tok1L in _MAJOR:
                    tok1L = "major"
                tok2L = tok1L + tok2L
                tok1L = word
        # standardize spelling
        if tok1 == "M" or tok1L in _MAJOR:
            tok1L = "major"
        elif tok1L == "omit":
            tok1L = "no"
        elif tok1L == "sus" and not tok2L:
            tok2L = "4"
        elif tok1L in _AUGMENTED and not tok2L:
            if pc.quality == "dominant" and pc.extension == "7":
                pc.quality = "augmented"
                tok1L = ""
            else:
                tok1L = "#"
                tok2L = "5"
        elif tok1 in _DIMINISHED:
            pc.quality = "diminished"
            tok1L = ""
        elif (tok1L in _LOWER or tok1L in _RAISE) and not tok2L:
            tok2L = pc.extension                # trailing alteration applies to the extension
            if pc.quality == "dominant":
                pc.quality = "major"
            pc.extension = ""
            tok1L = "b" if tok1L in _LOWER else "#"
        elif tok1L in _LOWER:
            tok1L = "b"
        elif tok1L in _RAISE:
            tok1L = "#"
        m = tok1L + tok2L + suffix
        if m:
            mods.append(m)
        known = (tok1L in ("add", "no", "sus", "major", "alt", "blues", "lyd", "phryg", "tristan")
                 or add_pending or sus_pending or (not tok1L and tok2L)
                 or tok1L in _LOWER or tok1L in _RAISE or not tok1L)
        if not known:
            pc.understandable = False
        while i < n and s[i] in _TRAILING:
            i += 1
        add_pending = sus_pending = False
    pc.modifier_list = mods
    return pc


# ---------------------------------------------------------------------------
# ChordList (only what realization needs: chord id -> names)
# ---------------------------------------------------------------------------

# First <name> of every <chord id> in share chords.xml (loaded when style chordsXmlFile=1).
_CHORDS_XML = (
    "1=|2=Maj|3=5b|4=+|5=6|6=Maj7|7=Maj9|8=Maj9#11|9=Maj13#11|10=Maj13|11=Maj9(no 3)|12=+|13=Maj7#5|14=69|15=2|"
    "16=m|17=m+|18=mMaj7|19=m7|20=m9|21=m11|22=m13|23=m6|24=m#5|25=m7#5|26=m69|27=Lyd|28=Maj7Lyd|29=Maj7b5|"
    "32=m7b5|33=dim|34=m9b5|40=5|56=7+|57=9+|58=13+|59=(blues)|60=7(Blues)|64=7|65=13|66=7b13|67=7#11|68=13#11|"
    "69=7#11b13|70=9|72=9b13|73=9#11|74=13#11|75=9#11b13|76=7b9|77=13b9|78=7b9b13|79=7b9#11|80=13b9#11|"
    "81=7b9#11b13|82=7#9|83=13#9|84=7#9b13|85=9#11|86=13#9#11|87=7#9#11b13|88=7b5|89=13b5|90=7b5b13|91=9b5|"
    "92=9b5b13|93=7b5b9|94=13b5b9|95=7b5b9b13|96=7b5#9|97=13b5#9|98=7b5#9b13|99=7#5|100=13#5|101=7#5#11|"
    "102=13#5#11|103=9#5|104=9#5#11|105=7#5b9|106=13#5b9|107=7#5b9#11|108=13#5b9#11|109=7#5#9|110=13#5#9#11|"
    "111=7#5#9#11|112=13#5#9#11|113=7alt|128=7sus|129=13sus|130=7susb13|131=7sus#11|132=13sus#11|133=7sus#11b13|"
    "134=9sus|135=9susb13|136=9sus#11|137=13sus#11|138=13sus#11|139=9sus#11b13|140=7susb9|141=13susb9|"
    "142=7susb9b13|143=7susb9#11|144=13susb9#11|145=7susb9#11b13|146=7sus#9|147=13sus#9|148=7sus#9b13|"
    "149=9sus#11|150=13sus#9#11|151=7sus#9#11b13|152=7susb5|153=13susb5|154=7susb5b13|155=9susb5|156=9susb5b13|"
    "157=7susb5b9|158=13susb5b9|159=7susb5b9b13|160=7susb5#9|161=13susb5#9|162=7susb5#9b13|163=7sus#5|"
    "164=13sus#5|165=7sus#5#11|166=13sus#5#11|167=9sus#5|168=9sus#5#11|169=7sus#5b9|170=13sus#5b9|"
    "171=7sus#5b9#11|172=13sus#5b9#11|173=7sus#5#9|174=13sus#5#9#11|175=7sus#5#9#11|176=13sus#5#9#11|177=4|"
    "184=sus|185=dim7|186=sus2|187=maddb13|188=add#13|189=add#11#13|190=add#13|191=6add9|192=sus4|193=11|"
    "194=Maj11|195=Tristan|196=m7add11|197=Maj7add13|198=madd9|199=m9Maj7|200=5|201=m11b5|202=dim7add#7|"
    "203=aug9|204=omit5|205=aug7|206=aug9|207=aug13|210=Maj7#11|211=Maj9#5|212=Maj7#9|213=add2|214=add9|"
    "215=susb9|216=Maj7sus|217=Maj9sus|220=m7b9|221=m7b13|222=Phryg|223=madd2|230=7b9#9|240=sus#4|241=Maj7b13"
)

# Legacy "custom" description files (cchords_*.xml, same 176 ids as chords.xml).  Stored realization-
# equivalently: only first names that parse to a different quality/extension/modifier set than the
# chords.xml name are listed; the other named ids behave exactly like chords.xml.  Ids in
# _CCHORDS_NAMELESS define no <name> in these files.
_CCHORDS_MUSE = "159=7susb5b913|199=mi(ma9)|202=o7addma7|203=augadd9"
_SUS_IDS = (130, 131, 132, 133, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150,
            151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170,
            171, 172, 173, 174, 175, 176)


def _sus_last(suffix: str) -> str:
    # cchords_rb/sym spell "7sus#11" as "7#11sus4"/"7#11sus" (and 159 "7b5b913..", 172 without sus)
    out = []
    base = _decode_table(_CHORDS_XML)
    for k in _SUS_IDS:
        name = base[k].replace("sus", "", 1)
        if k == 159:
            name = "7b5b913"
        out.append(f"{k}={name}{'' if k == 172 else suffix}")
    return "|".join(out)


_CCHORDS_OVERRIDES: Dict[str, str] = {
    "cchords_muse.xml": _CCHORDS_MUSE,
    "cchords_nrb.xml": _CCHORDS_MUSE,
    "cchords_rb.xml": "@sus4|199=-(maj9)|202=o7addmaj7|203=augadd9|215=sus4b9",
    "cchords_sym.xml": "@sus|199=-ma9|202=o7addma7|203=augadd9",
}
_CCHORDS_NAMELESS = frozenset([4, 5, 15, 27, 40, 56, 57, 58, 59, 60, 64, 65, 66, 67, 68, 69, 70, 128, 129, 134, 177,
                               186, 189, 190, 191, 192, 193, 195, 200, 204] + list(range(72, 114)))


def _decode_table(s: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for item in s.split("|") if s else ():
        if item.startswith("@"):
            out.update(_decode_table(_sus_last(item[1:])))
            continue
        k, _, v = item.partition("=")
        out[int(k)] = v
    return out


class ChordList:
    """The score's chord list reduced to ``id -> names`` (names[0] is ChordDescription::names.front())."""

    def __init__(self) -> None:
        self.names: Dict[int, List[str]] = {}
        self.source: str = ""

    def first_name(self, chord_id: int) -> Optional[str]:
        names = self.names.get(chord_id)
        return names[0] if names else None

    def _add(self, chord_id: int, names: List[str]) -> None:
        # ChordList::read: an existing description is taken over and new names are stacked on top.
        if not chord_id:
            return                              # private id, never referenced by <extension>
        self.names[chord_id] = list(names) + self.names.get(chord_id, [])

    def read_element(self, root: ET.Element) -> None:
        """ChordList::read(XmlReader&): the children of <museScore> / <ChordList>."""
        for c in root:
            if c.tag == "chord":
                self._add(_to_int(c.get("id", "0")), [n.text or "" for n in c.findall("name")])

    def read_bytes(self, data: bytes) -> bool:
        """ChordList::read(IODevice*): succeeds only for a <museScore> root."""
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return False
        if root.tag != "museScore":
            return False
        self.read_element(root)
        return True

    def read_named(self, name: str) -> None:
        """ChordList::read(const String&): a shipped description file (unknown names -> chords_std.xml)."""
        if not name:
            return
        if os.path.isabs(name) and os.path.exists(name):
            with open(name, "rb") as f:
                self.read_bytes(f.read())
            return
        if name == "chords.xml":
            for k, v in _decode_table(_CHORDS_XML).items():
                self._add(k, [v])
        elif name in _CCHORDS_OVERRIDES:
            table = _decode_table(_CHORDS_XML)
            table.update(_decode_table(_CCHORDS_OVERRIDES[name]))
            for k, v in table.items():
                if k not in _CCHORDS_NAMELESS:
                    self._add(k, [v])
        elif name in ("stdchords.xml", "jazzchords.xml"):
            self._add(1, [""])                  # their only <name> (empty: major triad)
        # everything else (chords_std.xml, chords_jazz.xml, chords_legacy.xml, missing files) has no names


def _style_from_element(style_el: Optional[ET.Element]) -> Tuple[Dict[str, object], Optional[ET.Element]]:
    style = dict(DEFAULT_STYLE)
    chordlist_el = None
    if style_el is None:
        return style, None
    for c in style_el:
        t = (c.text or "").strip()
        if c.tag in ("harmonyVoiceLiteral", "chordsXmlFile", "concertPitch"):
            style[c.tag] = _to_int(t) != 0 if t not in ("true", "false") else t == "true"
        elif c.tag == "displayInConcertPitch":
            style["concertPitch"] = _to_int(t) != 0
        elif c.tag in ("harmonyVoicing", "harmonyDuration"):
            style[c.tag] = _to_int(t)
        elif c.tag in ("chordDescriptionFile", "chordStyle"):
            style[c.tag] = c.text or ""
        elif c.tag == "ChordList":
            chordlist_el = c
    return style, chordlist_el


def _read_mscz(mscz_path: str):
    """-> (msc_version, style Element or None, chordlist.xml bytes or None)."""
    if mscz_path.lower().endswith(".mscx"):
        with open(mscz_path, "rb") as f:
            mscx = f.read()
        style_bytes = chordlist = None
        side = os.path.join(os.path.dirname(mscz_path), "score_style.mss")
        if os.path.exists(side):
            with open(side, "rb") as f:
                style_bytes = f.read()
    else:
        with zipfile.ZipFile(mscz_path) as z:
            names = z.namelist()
            main = None
            if "META-INF/container.xml" in names:
                cont = ET.fromstring(z.read("META-INF/container.xml"))
                for rf in cont.iter("rootfile"):
                    p = rf.get("full-path", "")
                    if p.endswith(".mscx"):
                        main = p
                        break
            if main is None:
                main = next(n for n in names if n.endswith(".mscx"))
            mscx = z.read(main)
            style_bytes = z.read("score_style.mss") if "score_style.mss" in names else None
            chordlist = z.read("chordlist.xml") if "chordlist.xml" in names else None
    root = ET.fromstring(mscx)
    ver = root.get("version", "4.60")
    try:
        major, _, minor = ver.partition(".")
        msc_version = int(major) * 100 + int(minor)
    except ValueError:
        msc_version = 460
    style_el = None
    if style_bytes:
        sroot = ET.fromstring(style_bytes)
        style_el = sroot if sroot.tag == "Style" else sroot.find("Style")
    if style_el is None:
        sc = root.find("Score")
        style_el = sc.find("Style") if sc is not None else None
    return msc_version, style_el, chordlist


def load_harmony_style(mscz_path: str) -> Dict[str, object]:
    """Style values used by chord-symbol playback (``DEFAULT_STYLE`` keys) read from a .mscz/.mscx."""
    _v, style_el, _cl = _read_mscz(mscz_path)
    return _style_from_element(style_el)[0]


def load_chord_list(mscz_path: str) -> ChordList:
    """The score's ChordList as MuseScore loads it (MscLoader / ReadChordListHook / checkChordList).

    1. ``chordlist.xml`` in the .mscz (4.x custom list) if it has a <museScore> root;
    2. else a <ChordList> inside the style (3.x files);
    3. else ``chords.xml`` when style ``chordsXmlFile`` is set, then ``chordDescriptionFile`` on top.
    """
    msc_version, style_el, chordlist = _read_mscz(mscz_path)
    style, chordlist_el = _style_from_element(style_el)
    cl = ChordList()
    if chordlist is not None and cl.read_bytes(chordlist):
        cl.source = "chordlist.xml"
        return cl
    cl = ChordList()
    if chordlist_el is not None:
        cl.read_element(chordlist_el)
        cl.source = "<ChordList>"
        return cl
    descr = str(style.get("chordDescriptionFile") or "")
    xml_file = bool(style.get("chordsXmlFile"))
    if msc_version < 400 and descr != "chords_std.xml" and not descr.startswith("chords_") \
            and str(style.get("chordStyle")) == "std":
        xml_file = True                         # ReadChordListHook::validate
    if xml_file:
        cl.read_named("chords.xml")
    cl.read_named(descr)
    cl.source = ("chords.xml+" if xml_file else "") + descr
    return cl


# ---------------------------------------------------------------------------
# Harmony element reading (rw/read400|read410|read460 TRead::read(Harmony*), Harmony::afterRead)
# ---------------------------------------------------------------------------


@dataclass
class HarmonyInfo:
    root_tpc: int = TPC_INVALID
    bass_tpc: int = TPC_INVALID
    text_name: str = ""
    id: int = 0


@dataclass
class HarmonyData:
    infos: List[HarmonyInfo]
    play: bool = True
    harmony_type: int = HARMONY_STANDARD
    literal: bool = True
    voicing: int = VOICING_AUTO
    duration: int = UNTIL_NEXT_CHORD_SYMBOL
    _parsed: Optional[ParsedChord] = None

    @property
    def root_tpc(self) -> int:
        return self.infos[0].root_tpc if self.infos else TPC_INVALID

    @property
    def bass_tpc(self) -> int:
        return self.infos[0].bass_tpc if self.infos else TPC_INVALID

    @property
    def realizable(self) -> bool:
        """Harmony::isRealizable(): every sub-chord has a valid root (vacuously true with none)."""
        return all(tpc_is_valid(i.root_tpc) for i in self.infos)

    @property
    def parsed(self) -> Optional[ParsedChord]:
        """Harmony::parsedForm(): first sub-chord's text name, parsed with preferMinor=false."""
        if not self.infos:
            return None
        if self._parsed is None:
            self._parsed = parse_chord(self.infos[0].text_name)
        return self._parsed


def _el_int(el: ET.Element) -> int:
    return _to_int((el.text or "").strip())


def _el_bool(el: ET.Element) -> bool:
    t = (el.text or "").strip()
    return t == "true" if t in ("true", "false") else _to_int(t) != 0


def read_harmony(harmony_elem: ET.Element, chord_list: Optional[ChordList] = None,
                 style: Optional[dict] = None, msc_version: Optional[int] = None, key: int = 0) -> HarmonyData:
    """Read a <Harmony> element like MuseScore 4.6 does.

    ``msc_version``: file version (e.g. 440); 460+ uses <harmonyInfo> blocks, older files flat
    <root>/<name>/<base>/<extension>/<function> tags.  ``None`` auto-detects from the element.
    ``key``: staff key (-7..7) at the harmony tick; only used for Nashville <function> roots (< 4.6 files).
    """
    st = dict(DEFAULT_STYLE)
    if style:
        st.update(style)
    if msc_version is None:
        msc_version = 460 if harmony_elem.find("harmonyInfo") is not None else 410
    hd = HarmonyData(infos=[], literal=bool(st["harmonyVoiceLiteral"]), voicing=int(st["harmonyVoicing"]),
                     duration=int(st["harmonyDuration"]))
    flat = HarmonyInfo()
    for c in harmony_elem:
        t = c.tag
        if t == "play":
            hd.play = _el_bool(c)
        elif t == "harmonyType":
            hd.harmony_type = _el_int(c)
        elif t == "harmonyVoiceLiteral":
            hd.literal = _el_bool(c)
        elif t == "harmonyVoicing":
            hd.voicing = _el_int(c)
        elif t == "harmonyDuration":
            hd.duration = _el_int(c)
        elif msc_version >= 460:
            if t == "harmonyInfo":
                info = HarmonyInfo()
                for d in c:
                    if d.tag == "bass":
                        info.bass_tpc = _el_int(d)
                    elif d.tag == "extension":
                        info.id = _el_int(d)
                    elif d.tag == "name":
                        info.text_name = d.text or ""
                    elif d.tag == "root":
                        info.root_tpc = _el_int(d)
                hd.infos.append(info)
        else:
            if t == "base":
                flat.bass_tpc = _el_int(c)
            elif t == "extension":
                flat.id = _el_int(c)
            elif t == "name":
                flat.text_name = c.text or ""
            elif t == "root":
                flat.root_tpc = _el_int(c)
            elif t == "function":
                flat.root_tpc = _function2tpc(c.text or "", key)
    if msc_version < 460:
        hd.infos.append(flat)
    # Harmony::afterRead -> HarmonyInfo::getDescription(): a positive id found in the chord list replaces
    # the text name by the description's first name.
    for info in hd.infos:
        if tpc_is_valid(info.root_tpc) and info.id > 0 and chord_list is not None:
            first = chord_list.first_name(info.id)
            if first is not None:
                info.text_name = first
    return hd


def harmony_plays(harmony_elem: ET.Element) -> bool:
    """Harmony::play(): <play>0</play> disables playback (default true)."""
    el = harmony_elem.find("play")
    return True if el is None else _el_bool(el)


def harmony_is_realizable(harmony_elem: Union[ET.Element, HarmonyData], msc_version: Optional[int] = None,
                          key: int = 0) -> bool:
    """Harmony::isRealizable(): all sub-chord roots are valid TPCs ("N.C.", Roman numerals fail).

    Note: a 4.6 <Harmony> without any <harmonyInfo> is "realizable" but produces no pitches.
    """
    hd = harmony_elem if isinstance(harmony_elem, HarmonyData) else read_harmony(harmony_elem, None, None,
                                                                                  msc_version, key)
    return hd.realizable


# ---------------------------------------------------------------------------
# RealizedHarmony (dom/realizedharmony.cpp)
# ---------------------------------------------------------------------------

_RANK_MULT = 128
_RANK_3RD, _RANK_7TH, _RANK_9TH, _RANK_ADD, _RANK_OMIT = 0, 1, 2, 3, 4
_FIFTH = 7 + _RANK_MULT * _RANK_OMIT


def _get_intervals(pc: ParsedChord, literal: bool, next_hd: Optional[HarmonyData], root_tpc: int) -> List[int]:
    """RealizedHarmony::getIntervals(): sorted multimap keys (interval + 128 * rank)."""
    ret: List[int] = []

    def ins(k: int) -> None:
        insort_right(ret, k)

    quality = pc.quality
    ext = _to_int(pc.extension)
    omit = 0
    alt5 = False
    for s in pc.modifier_list:
        modded = False
        for c, ch in enumerate(s):
            if not _isdigit(ch):
                continue
            alter = 0
            cutoff = c
            deg = _to_int(re.sub(r"[^0-9]+", "", s))
            if c:
                if s[c - 1] == "#":
                    cutoff -= 1
                    alter = 0 if deg == 7 else 1
                elif s[c - 1] == "b":
                    cutoff -= 1
                    alter = -1
            ext_type = s[:cutoff]
            if ext_type in ("", "major"):
                ins(_step2pitch_interval(deg, alter) + _RANK_MULT * (_RANK_9TH if deg == 9 else _RANK_ADD))
                if deg == 5:
                    alt5 = True
                omit |= _shl1(deg)
                modded = True
            elif ext_type == "sus":
                ins(_step2pitch_interval(deg, alter) + _RANK_MULT * _RANK_3RD)
                omit |= 1 << 3
                modded = True
            elif ext_type == "no":
                omit |= _shl1(deg)
                modded = True
            elif ext_type == "add":
                ins(_step2pitch_interval(deg, alter) + _RANK_MULT * _RANK_ADD)
                omit |= _shl1(deg)
                modded = True
            break
        if not modded:
            if s == "phryg":
                ins(_step2pitch_interval(9, -1) + _RANK_MULT * _RANK_9TH)
                omit |= 1 << 9
            elif s == "lyd":
                ins(_step2pitch_interval(11, 1) + _RANK_MULT * _RANK_ADD)
                omit |= 1 << 11
            elif s == "blues":
                ins(_step2pitch_interval(9, 1) + _RANK_MULT * _RANK_ADD)
                omit |= 1 << 9
            elif s == "alt":
                ins(_step2pitch_interval(5, -1) + _RANK_MULT * _RANK_ADD)
                ins(_step2pitch_interval(5, 1) + _RANK_MULT * _RANK_ADD)
                omit |= 1 << 5
                ins(_step2pitch_interval(9, -1) + _RANK_MULT * _RANK_9TH)
                ins(_step2pitch_interval(9, 1) + _RANK_MULT * _RANK_9TH)
                omit |= 1 << 9
            else:
                omit = ~0
    if ext == 5:
        omit |= 1 << 3

    def has(bit: int) -> bool:
        return bool(omit & (1 << bit))

    if quality == "minor":
        if not has(3):
            ins(_step2pitch_interval(3, -1) + _RANK_MULT * _RANK_3RD)
        if not has(5):
            ins(_step2pitch_interval(5, 0) + _RANK_MULT * _RANK_OMIT)
    elif quality == "augmented":
        if not has(3):
            ins(_step2pitch_interval(3, 0) + _RANK_MULT * _RANK_3RD)
        if not has(5):
            ins(_step2pitch_interval(5, 1) + _RANK_MULT * _RANK_3RD)
    elif quality in ("diminished", "half-diminished"):
        if not has(3):
            ins(_step2pitch_interval(3, -1) + _RANK_MULT * _RANK_3RD)
        if not has(5):
            ins(_step2pitch_interval(5, -1) + _RANK_MULT * _RANK_3RD)
        if not has(7) and quality == "half-diminished":
            ins(_step2pitch_interval(7, -1) + _RANK_MULT * _RANK_7TH)
        alt5 = True
    else:
        if not has(3):
            ins(_step2pitch_interval(3, 0) + _RANK_MULT * _RANK_3RD)
        if not has(5):
            ins(_step2pitch_interval(5, 0) + _RANK_MULT * _RANK_OMIT)

    if ext in (13, 11, 9, 7):
        if ext == 13 and not has(13):
            ins(9 + _RANK_MULT * _RANK_ADD)
            omit |= 1 << 13
        if ext >= 11 and not has(11):
            if quality == "minor":
                ins(5 + _RANK_MULT * _RANK_ADD)
            elif literal:
                ins(5 + _RANK_MULT * _RANK_OMIT)
            omit |= 1 << 11
        if ext >= 9 and not has(9):
            ins(2 + _RANK_MULT * _RANK_9TH)
            omit |= 1 << 9
        if not has(7):
            if quality == "major":
                ins(11 + _RANK_MULT * _RANK_7TH)
            elif quality == "diminished":
                ins(9 + _RANK_MULT * _RANK_7TH)
            elif quality == "half-diminished":
                pass
            else:
                ins(10 + _RANK_MULT * _RANK_7TH)
    elif ext == 6:
        if not has(6):
            ins(9 + _RANK_MULT * _RANK_ADD)
            omit |= 1 << 13
    elif ext == 4:
        if not has(4):
            ins(5 + _RANK_MULT * _RANK_ADD)
    elif ext == 2:
        if not has(2):
            ins(2 + _RANK_MULT * _RANK_ADD)
        omit |= 1 << 9
    elif ext == 69:
        ins(9 + _RANK_MULT * _RANK_ADD)
        ins(2 + _RANK_MULT * _RANK_ADD)
        omit = ~0

    # jazz interpretation (harmonyVoiceLiteral = false) looks at the next chord symbol on the track
    if not literal and next_hd is not None and tpc_is_valid(next_hd.root_tpc):
        npc = next_hd.parsed
        q_next = npc.quality if npc else ""
        pitch_between = (tpc2pitch(next_hd.root_tpc) + 12 - tpc2pitch(root_tpc)) % 12
        maj7 = q_next == "major" and npc is not None and _to_int(npc.extension) >= 7
        if not has(9):
            if quality == "dominant" and pitch_between == 5 and (q_next == "minor" or maj7):
                ins(1 + _RANK_MULT * _RANK_9TH)
            else:
                ins(2 + _RANK_MULT * _RANK_9TH)
        if not has(13) and not alt5:
            if quality == "dominant" and pitch_between == 5 and q_next == "minor":
                if _FIFTH in ret:
                    ret.remove(_FIFTH)
                ins(8 + _RANK_MULT * _RANK_ADD)
    return ret


def _normalize(intervals: List[int], root_pitch: int, max_n: int, enforce: bool = False) -> List[int]:
    """RealizedHarmony::normalizeNoteMap(): keys reduced to 0..11 (C++ %), sorted."""
    ret: List[int] = []
    for k in intervals[:max_n]:
        insort_right(ret, _cmod(_cmod(k, 128) + root_pitch, 12))
    if enforce:
        while len(ret) < max_n:
            insort_right(ret, root_pitch)
            size = max_n - len(ret)
            for k in intervals[:size]:
                insort_right(ret, _cmod(_cmod(k, 128) + root_pitch, 12))
    elif len(ret) < max_n:
        insort_right(ret, root_pitch)
    return ret


def _generate_notes(hd: HarmonyData, root_tpc: int, bass_tpc: int, literal: bool, voicing: int,
                    offset: int, next_hd: Optional[HarmonyData]) -> List[int]:
    offset = _cmod(offset, 12)
    octave = 5
    root_pitch = tpc2pitch(root_tpc) + offset
    if root_pitch < 0:
        root_pitch = _cmod(root_pitch, 12) + 12
    else:
        root_pitch %= 12
    notes: List[int] = []
    if bass_tpc != TPC_INVALID and voicing != VOICING_ROOT_ONLY:
        notes.append(tpc2pitch(bass_tpc) + offset + (octave - 2) * 12)
    else:
        notes.append(root_pitch + (octave - 2) * 12)

    pc = hd.parsed
    if voicing == VOICING_ROOT_ONLY:
        pass
    elif voicing in (VOICING_AUTO, VOICING_CLOSE):
        if voicing == VOICING_CLOSE or (pc is not None and pc.understandable):
            notes.append(root_pitch + octave * 12)
            for k in _get_intervals(pc, literal, next_hd, root_tpc):
                notes.append(_cmod(root_pitch + _cmod(k, 128), 12) + octave * 12)
    elif voicing == VOICING_DROP_2:
        iv = _normalize(_get_intervals(pc, literal, next_hd, root_tpc), root_pitch, 4)
        for counter, p in enumerate(reversed(iv), 1):
            notes.append(p + (octave - 1) * 12 if counter == 2 else p + octave * 12)
    elif voicing == VOICING_THREE_NOTE:
        iv = _normalize(_get_intervals(pc, literal, next_hd, root_tpc), root_pitch, 2, True)
        notes.append(iv[0] + octave * 12)
        notes.append(iv[1] + octave * 12)
    elif voicing in (VOICING_FOUR_NOTE, VOICING_SIX_NOTE):
        iv = _normalize(_get_intervals(pc, literal, next_hd, root_tpc), root_pitch,
                        3 if voicing == VOICING_FOUR_NOTE else 5, True)
        for counter, p in enumerate(reversed(iv)):
            notes.append(p + (octave - 1) * 12 if counter % 2 else p + octave * 12)
    return sorted(notes)


def realize_harmony(harmony: Union[ET.Element, HarmonyData], chord_list: Optional[ChordList] = None,
                    style: Optional[dict] = None, transpose_offset: int = 0,
                    next_harmony: Union[ET.Element, HarmonyData, None] = None,
                    msc_version: Optional[int] = None, key: int = 0) -> List[int]:
    """``h->getRealizedHarmony().pitches()``: MIDI pitches in the order MuseScore emits them.

    The result is the key list of RealizedHarmony's ``std::multimap<pitch, tpc>``: ascending, duplicates
    kept (each duplicate is emitted as its own NOTEON/NOTEOFF).  Empty when the first sub-chord's root is
    not a valid TPC.  Only the first sub-chord of a polychord ("C|D") sounds.

    transpose_offset: ``capo fret (if a Capo is active at the tick) + instrument transpose chromatic when
        style concertPitch is false`` (Harmony::getRealizedHarmony); reduced with C++ ``%= 12``.
    style: DEFAULT_STYLE keys (harmonyVoiceLiteral / harmonyVoicing / harmonyDuration); the element's
        own <harmonyVoiceLiteral>/<harmonyVoicing>/<harmonyDuration> override them.
    next_harmony: only for harmonyVoiceLiteral = false (jazz): the next Harmony on the same track in
        score order (Harmony::findNext: segment->next1() across measures, no repeat unwinding; a
        FretDiagram's harmony counts), regardless of its play flag.
    """
    hd = harmony if isinstance(harmony, HarmonyData) else read_harmony(harmony, chord_list, style,
                                                                       msc_version, key)
    if not tpc_is_valid(hd.root_tpc):
        return []
    nxt = next_harmony
    if nxt is not None and not isinstance(nxt, HarmonyData):
        nxt = read_harmony(nxt, chord_list, style, msc_version, key)
    bass = hd.bass_tpc
    if bass != TPC_INVALID and not tpc_is_valid(bass):
        bass = TPC_INVALID                      # out-of-range TPC would be UB in C++
    return _generate_notes(hd, hd.root_tpc, bass, hd.literal, hd.voicing, transpose_offset, nxt)


# ---------------------------------------------------------------------------
# Duration (RealizedHarmony::getActualDuration / Harmony::ticksTillNext)
# ---------------------------------------------------------------------------


def actual_duration(duration_rule: int, tick: int, utick: int, segment_ticks: int, measure_end_tick: int,
                    harmony_ticks: Sequence[int], repeat_segments: Sequence[Tuple[int, int, int]]) -> int:
    """Note-off offset for a realized Harmony: ``off = utick + actual_duration(...)``.

    Inputs (all in MIDI ticks, division 480):
      duration_rule   HarmonyData.duration (element <harmonyDuration> or style harmonyDuration).
      tick            score tick of the harmony's segment (``h->tick()``).
      utick           ``tick + tickOffset`` of the RepeatSegment being rendered (the note-on tick).
      segment_ticks   ``Segment::ticks()`` of the harmony's ChordRest segment: rtick of the next *active*
                      segment of the measure (any type/staff: next ChordRest onset in any track, Breath,
                      mid-measure Clef/KeySig/TimeTick...) or the measure length, minus its own rtick.
      measure_end_tick  end tick of the harmony's measure.
      harmony_ticks   sorted score ticks of every segment carrying a Harmony annotation (or a FretDiagram
                      with a harmony) on exactly the same track -- including play=0 / "N.C." symbols.
      repeat_segments RepeatList in playback order as (tick, utick, len).

    Rules (C++ ``Harmony::ticksTillNext(utick, stopAtMeasureEnd)``):
      SEGMENT_DURATION         -> segment_ticks.
      (no RepeatSegment contains utick -> segment_ticks.)
      STOP_AT_MEASURE_END      -> next harmony tick h with tick < h < measure_end_tick: h - tick;
                                  otherwise the summed segment ticks to the measure end
                                  (= measure_end_tick - tick).
      UNTIL_NEXT_CHORD_SYMBOL  -> search the rest of the current RepeatSegment rs0 (ticks in
                                  (tick, rs0.tick + rs0.len)); if found: h - tick.  Otherwise search each
                                  later RepeatSegment rs from its first measure (ticks in
                                  [rs.tick, rs.tick + rs.len)); the first hit gives
                                  h + (rs.utick - rs.tick) - utick.  With no hit at all the duration is the
                                  summed ticks walked: (rs0 end - tick) + sum(len of all later segments),
                                  i.e. until the end of playback.
      anything else            -> 0.
    Not modelled: when a walked measure starts a multi-measure rest (``mmRest()``, only with the
    createMultiMeasureRests style), C++ adds the mmRest length and skips the grouped measures, which can
    step past rs0's last measure; results only differ when no next harmony is found.
    """
    if duration_rule == SEGMENT_DURATION:
        return segment_ticks
    if duration_rule not in (UNTIL_NEXT_CHORD_SYMBOL, STOP_AT_MEASURE_END):
        return 0
    k0 = next((k for k, (_t, ut, ln) in enumerate(repeat_segments) if ut <= utick < ut + ln), None)
    if k0 is None:
        return segment_ticks
    if duration_rule == STOP_AT_MEASURE_END:
        for h in harmony_ticks:
            if tick < h < measure_end_tick:
                return h - tick
        return measure_end_tick - tick
    rs_tick, _rs_utick, rs_len = repeat_segments[k0]
    for h in harmony_ticks:
        if tick < h < rs_tick + rs_len:
            return h - tick
    total = rs_tick + rs_len - tick
    for t, ut, ln in repeat_segments[k0 + 1:]:
        for h in harmony_ticks:
            if t <= h < t + ln:
                return h + (ut - t) - utick
        total += ln
    return total
