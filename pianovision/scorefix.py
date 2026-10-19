"""A score's repeat structure checked and fixed: what it plays (repeats, voltas, jumps unrolled as MuseScore plays
them), what can never be played, and structural fixes written into a copy of the score.

Used to compare a transcription with a recording of the song (``tools/score_check.py`` measures the recording;
this module is stdlib only):

* :func:`structure` -- the measures as played, the voltas and repeats, the markers and jumps, the voltas whose
  endings no pass reaches (e.g. "99", left by a PDF import), the measures that are never played, and the measures
  whose actual length differs from the time signature;
* :class:`ScoreEdit` / :func:`apply_fixes` -- the tempo marking, volta endings and spans, end-repeat counts,
  segno / coda / "To Coda" markers and D.S. / D.C. jumps written into the .mscx text as MuseScore 4 writes them;
  everything else in the file stays byte for byte;
* :func:`score_reference` -- a score's notes and measures on its own clock, in the format of
  ``tools/recording_align.py``'s ``ref.json``, so a (fixed) copy can be aligned with a recording.

Measures are numbered 1, 2, ... in file order (as the formatter's reports number them); MuseScore's displayed
number differs when a measure is excluded from the count (:func:`displayed_numbers`).
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .handedits import Patches, XNode, _main_mscx_name, parse_xml, replace_mscx


class FixError(Exception):
    pass


# ------------------------------------------------------------------------------
# reading: what the score plays
# ------------------------------------------------------------------------------

def ranges(nums: Iterable[int]) -> str:
    """``[1, 2, 3, 7, 8, 3]`` -> ``"1-3, 7-8, 3"``: runs of consecutive numbers, in order."""
    out: List[str] = []
    run: List[int] = []
    for n in nums:
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(str(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}")
        run = [n]
    if run:
        out.append(str(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}")
    return ", ".join(out)


def play_order(score) -> List[int]:
    """The measure numbers (1-based, file order) as the score plays them: repeats, voltas and jumps unrolled by
    :mod:`pianovision.repeats` (MuseScore's rules)."""
    from .repeats import build_repeat_list
    index = {id(m): i + 1 for i, m in enumerate(score.measures)}
    out: List[int] = []
    for seg in build_repeat_list(score):
        for m in seg.measures:
            out.append(index[id(m)])
    return out


def displayed_numbers(score) -> Dict[int, int]:
    """File-order number -> the number MuseScore shows (a measure excluded from the count, ``<irregular>``, shares
    the number of the measure after it; the first measure is 1)."""
    out: Dict[int, int] = {}
    no = 0
    adjust = 0
    for i, m in enumerate(score.measures):
        no += adjust
        out[i + 1] = no + 1
        adjust = 0 if m.irregular else 1
    return out


def _volta_info(score) -> List[dict]:
    from .repeats import volta_endings, volta_measures
    index = {id(m): i + 1 for i, m in enumerate(score.measures)}
    out = []
    for sp in score.spanners:
        if sp.tag != "Volta":
            continue
        sm, em = volta_measures(score, sp)
        if sm is None or em is None:
            continue
        out.append({"start": index[id(sm)], "end": index[id(em)], "endings": volta_endings(sp),
                    "text": str(sp.props.get("beginText", "")),
                    "closed": str(sp.props.get("endHookType", "0")) != "0"})
    out.sort(key=lambda v: v["start"])
    return out


def unreachable_voltas(voltas: Sequence[dict], repeats: Sequence[dict]) -> List[dict]:
    """The voltas no pass plays: none of their endings is a pass of the repeat they belong to (its end repeat is
    inside the volta or in the measure before it, else the next end repeat; a volta after the last end repeat
    belongs to none and plays ending 1 only).
    ``repeats``: ``{"start": m, "end": m, "count": n}``."""
    bad = []
    for v in voltas:
        # its own end repeat: inside the volta (a first ending) or just before it (a later ending); else the next
        rep = next((r for r in repeats if v["start"] - 1 <= r["end"] <= v["end"]), None) or \
            next((r for r in repeats if r["end"] >= v["start"]), None)
        passes = set(range(1, (rep["count"] if rep else 1) + 1))
        if not passes & set(v["endings"]):
            bad.append(dict(v, passes=sorted(passes), repeat=rep))
    return bad


def _repeats(score) -> List[dict]:
    starts = []
    out = []
    for i, m in enumerate(score.measures):
        if m.repeat_start:
            starts.append(i + 1)
        if m.repeat_end:
            s = max([x for x in starts if x <= i + 1], default=1)
            out.append({"start": s, "end": i + 1, "count": m.repeat_count})
    return out


def structure(score) -> dict:
    """The score's repeat structure and what it plays, JSON-ready."""
    from .repeats import marker_label
    n = len(score.measures)
    order = play_order(score)
    played = set(order)
    voltas = _volta_info(score)
    reps = _repeats(score)
    markers, jumps = [], []
    for i, m in enumerate(score.measures):
        for mk in m.markers:
            markers.append({"measure": i + 1, "label": marker_label(mk),
                            "type": str(mk.props.get("markerType", mk.props.get("subtype", "")))})
        for j in m.jumps:
            jumps.append({"measure": i + 1, "jumpTo": str(j.props.get("jumpTo", "")),
                          "playUntil": str(j.props.get("playUntil", "")),
                          "continueAt": str(j.props.get("continueAt", ""))})
    irregular = []
    for i, m in enumerate(score.measures):
        if m.len != m.timesig:
            irregular.append({"measure": i + 1, "beats": float(m.len * 4), "timesig": f"{m.timesig_nd[0]}/"
                              f"{m.timesig_nd[1]}", "excluded_from_count": bool(m.irregular)})
    return {
        "measures": n,
        "play_order": order,
        "play_order_text": ranges(order),
        "played": len(order),
        "distinct_played": len(played),
        "never_played": ranges(sorted(set(range(1, n + 1)) - played)),
        "repeats": reps,
        "voltas": voltas,
        "unreachable_voltas": unreachable_voltas(voltas, reps),
        "markers": markers,
        "jumps": jumps,
        "irregular_lengths": irregular,
    }


# ------------------------------------------------------------------------------
# writing: fixes in a copy, byte for byte elsewhere
# ------------------------------------------------------------------------------

# MuseScore 4's names (file format 4.x): the coda sign is "codab", "To Coda" is "coda"
MARKERS = {
    "segno": ("segno", "segno", "repeat_left", "<sym>segno</sym>"),
    "coda": ("codab", "codab", "repeat_left", "<sym>coda</sym>"),
    "tocoda": ("coda", "coda", "repeat_right", "To Coda"),
    "fine": ("fine", "fine", "repeat_right", "Fine"),
}
JUMPS = {
    "ds_al_coda": ("D.S. al Coda", "segno", "coda", "codab"),
    "dc_al_coda": ("D.C. al Coda", "start", "coda", "codab"),
    "ds_al_fine": ("D.S. al Fine", "segno", "fine", ""),
    "dc_al_fine": ("D.C. al Fine", "start", "fine", ""),
    "ds": ("D.S.", "segno", "end", ""),
    "dc": ("D.C.", "start", "end", ""),
}


def _children_indent(data: bytes, node: XNode) -> str:
    """The indentation MuseScore gives the children of ``node`` (its first child's)."""
    c = node.children[0] if node.children else None
    if c is None:
        raise FixError(f"<{node.tag}> has no children")
    ls = data.rfind(b"\n", 0, c.start) + 1
    return data[ls:c.start].decode()


def _block(lines: Sequence[str], indent: str) -> str:
    """Element lines in MuseScore's layout (a closing tag indented like the children)."""
    return "".join(indent + ln + "\n" for ln in lines)


class ScoreEdit:
    """Structural edits on a score's main .mscx, applied together by :meth:`result`."""

    def __init__(self, data: bytes):
        self.data = data
        self.root = parse_xml(data)
        score = self.root.find("Score")
        if score is None:
            raise FixError("no <Score>")
        staves = [c for c in score.children if c.tag == "Staff" and c.find("Measure") is not None]
        if not staves:
            raise FixError("no staff with measures")
        self.staff_measures: List[List[XNode]] = [s.findall("Measure") for s in staves]
        self.measures = self.staff_measures[0]          # system elements (voltas, markers, jumps) live here
        self.patches = Patches(data)
        self.log: List[str] = []

    # -- helpers --------------------------------------------------------------
    def measure(self, n: int) -> XNode:
        if not 1 <= n <= len(self.measures):
            raise FixError(f"no measure {n}")
        return self.measures[n - 1]

    def _text_of(self, node: XNode) -> str:
        return self.data[node.start:node.end].decode()

    def _replace_text(self, node: XNode, value: str) -> None:
        """``<tag>old</tag>`` -> ``<tag>value</tag>`` (a leaf element)."""
        if node.close is None:
            raise FixError(f"<{node.tag}> is empty")
        s = self.data.index(b">", node.start) + 1
        self.patches.add(s, node.close, value)

    def _volta_start(self, n: int) -> XNode:
        m = self.measure(n)
        for v in m.findall("voice"):
            for sp in v.findall("Spanner"):
                if sp.attrs.get("type") == "Volta" and sp.find("Volta") is not None:
                    return sp
        raise FixError(f"no volta starts in measure {n}")

    def _volta_end(self, n_start: int, span: int) -> Tuple[int, XNode]:
        """The end element of the volta that starts in ``n_start`` and spans ``span`` measures."""
        k = n_start + span
        m = self.measure(k)
        for v in m.findall("voice"):
            for sp in v.findall("Spanner"):
                if sp.attrs.get("type") == "Volta" and sp.find("prev") is not None:
                    meas = sp.find("prev").find("location")
                    if meas is not None and meas.value("measures") == str(-span):
                        return k, sp
        raise FixError(f"the volta of measure {n_start} does not end in measure {k}")

    # -- edits ----------------------------------------------------------------
    def set_tempo(self, bpm: float, measure: int = 1) -> None:
        """The first tempo marking of ``measure``: its value and the number in its text."""
        m = self.measure(measure)
        for v in m.findall("voice"):
            for t in v.findall("Tempo"):
                val = t.find("tempo")
                txt = t.find("text")
                if val is None:
                    break
                bps = f"{bpm / 60:.6f}".rstrip("0").rstrip(".")
                self._replace_text(val, bps)
                if txt is not None:
                    raw = self.data[self.data.index(b">", txt.start) + 1:txt.close].decode()
                    new, k = re.subn(r"=\s*\d+(\.\d+)?", f"= {bpm:g}", raw, count=1)
                    if k:
                        self._replace_text(txt, new)
                self.log.append(f"m.{measure}: tempo marking -> {bpm:g} bpm")
                return
        raise FixError(f"no tempo marking in measure {measure}")

    def set_volta(self, measure: int, endings: Sequence[int], text: str, measures: Optional[int] = None,
                  closed: Optional[bool] = None) -> None:
        """The volta starting in ``measure``: its endings and text; ``measures``: its new length in measures
        (its end moves); ``closed``: an end hook (True) or open (False)."""
        sp = self._volta_start(measure)
        vol = sp.find("Volta")
        ind = _children_indent(self.data, vol)
        bt, en, hook = vol.find("beginText"), vol.find("endings"), vol.find("endHookType")
        ending_text = ", ".join(str(e) for e in endings)
        if bt is not None:
            self._replace_text(bt, text)
        if en is not None:
            self._replace_text(en, ending_text)
        if bt is None or en is None:
            first = vol.children[0]
            ls = self.data.rfind(b"\n", 0, first.start) + 1
            add = []
            if bt is None:
                add.append(f"<beginText>{text}</beginText>")
            if en is None:
                add.append(f"<endings>{ending_text}</endings>")
            self.patches.add(ls, ls, _block(add, ind))
        if closed is not None:
            if closed and hook is None:
                first = vol.children[0]
                ls = self.data.rfind(b"\n", 0, first.start) + 1
                self.patches.add(ls, ls, _block(["<endHookType>1</endHookType>"], ind))
            elif not closed and hook is not None:
                self.patches.remove(hook)
        nxt = sp.find("next")
        old_span = int(nxt.find("location").value("measures")) if nxt is not None else 0
        if measures is not None and measures != old_span:
            if nxt is None:
                raise FixError(f"the volta of measure {measure} has no end")
            k_old, end_sp = self._volta_end(measure, old_span)
            self._replace_text(nxt.find("location").find("measures"), str(measures))
            # the end element moves to the measure after the volta's last one, first in its first voice
            self.patches.remove(end_sp)
            target = self.measure(measure + measures)
            voice = target.find("voice")
            if voice is None or not voice.children:
                raise FixError(f"measure {measure + measures} has no voice to hold the volta's end")
            vind = _children_indent(self.data, voice)
            first = voice.children[0]
            ls = self.data.rfind(b"\n", 0, first.start) + 1
            self.patches.add(ls, ls, _block(['<Spanner type="Volta">', "  <prev>", "    <location>",
                                             f"      <measures>{-measures}</measures>", "      </location>",
                                             "    </prev>", "  </Spanner>"], vind))
        self.log.append(f"m.{measure}: volta endings {ending_text} ('{text}')"
                        + (f", {measures} measures" if measures is not None else "")
                        + ("" if closed is None else (", closed" if closed else ", open")))

    def set_repeat_count(self, measure: int, count: int) -> None:
        m = self.measure(measure)
        er = m.find("endRepeat")
        if er is None:
            raise FixError(f"no end repeat in measure {measure}")
        self._replace_text(er, str(count))
        self.log.append(f"m.{measure}: end repeat played {count} times")

    def _insert_measure_element(self, measure: int, lines: Sequence[str]) -> None:
        """A system element (marker, jump) in the top staff's measure, before its first voice."""
        m = self.measure(measure)
        voice = m.find("voice")
        if voice is None:
            raise FixError(f"measure {measure} has no voice")
        ind = _children_indent(self.data, m)
        ls = self.data.rfind(b"\n", 0, voice.start) + 1
        self.patches.add(ls, ls, _block(lines, ind))

    def add_marker(self, measure: int, kind: str) -> None:
        mtype, label, style, text = MARKERS[kind]
        self._insert_measure_element(measure, ["<Marker>", f"  <style>{style}</style>", f"  <text>{text}</text>",
                                               f"  <label>{label}</label>", f"  <markerType>{mtype}</markerType>",
                                               "  </Marker>"])
        self.log.append(f"m.{measure}: marker {kind} (label '{label}')")

    def add_jump(self, measure: int, kind: str, play_repeats: bool = False) -> None:
        text, to, until, cont = JUMPS[kind]
        lines = ["<Jump>", "  <style>repeat_right</style>", f"  <text>{text}</text>", f"  <jumpTo>{to}</jumpTo>",
                 f"  <playUntil>{until}</playUntil>", f"  <continueAt>{cont}</continueAt>"]
        if play_repeats:
            lines.append("  <playRepeats>1</playRepeats>")
        lines.append("  </Jump>")
        self._insert_measure_element(measure, lines)
        self.log.append(f"m.{measure}: jump {text} (to '{to}', until '{until}', continue at '{cont}')")

    def result(self) -> bytes:
        return self.patches.result()


def apply_fixes(mscx: bytes, fixes: Sequence[dict]) -> Tuple[bytes, List[str]]:
    """``fixes`` (JSON-ready, in order): ``{"op": "tempo", "bpm": 126}``,
    ``{"op": "volta", "measure": 38, "endings": [1], "text": "1.", "measures": 4, "closed": true}``,
    ``{"op": "repeat_count", "measure": 71, "count": 3}``, ``{"op": "marker", "measure": 10, "kind": "segno"}``,
    ``{"op": "jump", "measure": 72, "kind": "ds_al_coda"}``.  Returns the new .mscx and what was done."""
    ed = ScoreEdit(mscx)
    for f in fixes:
        op = f["op"]
        if op == "tempo":
            ed.set_tempo(float(f["bpm"]), int(f.get("measure", 1)))
        elif op == "volta":
            ed.set_volta(int(f["measure"]), [int(e) for e in f["endings"]], str(f["text"]), f.get("measures"),
                         f.get("closed"))
        elif op == "repeat_count":
            ed.set_repeat_count(int(f["measure"]), int(f["count"]))
        elif op == "marker":
            ed.add_marker(int(f["measure"]), str(f["kind"]))
        elif op == "jump":
            ed.add_jump(int(f["measure"]), str(f["kind"]), bool(f.get("play_repeats", False)))
        else:
            raise FixError(f"unknown fix {op!r}")
    return ed.result(), ed.log


def apply_fixes_mscz(mscz: bytes, fixes: Sequence[dict]) -> Tuple[bytes, List[str]]:
    """The .mscz with :func:`apply_fixes` applied to its main .mscx; every other member copied as it is."""
    with zipfile.ZipFile(io.BytesIO(mscz)) as z:
        member = _main_mscx_name(z)
        data = z.read(member)
    new, log = apply_fixes(data, fixes)
    return replace_mscx(mscz, member, new), log


# ------------------------------------------------------------------------------
# a score's notes on its own clock (recording_align.py's ref.json)
# ------------------------------------------------------------------------------

def score_reference(score_path: str, compat=None) -> dict:
    """The score rendered as the formatter renders it, repeats unrolled: ``measures`` (number, pass, start/end and
    beat seconds) and ``notes`` ``[start, duration, pitch, velocity, part]`` (part "v" for a voice staff, else
    "p"), the reference format of ``tools/recording_align.py``."""
    from .arrangement import Timeline, sounding_notes
    from .mscx import read_score
    from .pvjson import TempoIndex, extract_tempo_events
    from .render import Renderer
    score = read_score(score_path)
    r = Renderer(score, compat)
    midi = r.render()
    tl = Timeline.from_render(r, midi)
    sec = TempoIndex(extract_tempo_events(midi), midi.ticks_per_beat)
    tpb = midi.ticks_per_beat
    measures = []
    for m in tl.measures:
        num, den = m.timesig
        beat = max(1, int(round(tpb * 4 / max(den, 1))))
        if den == 8 and num % 3 == 0 and num > 3:
            beat *= 3
        beats = [round(sec(tl.tick(m.utick + k)), 5) for k in range(0, max(m.length, 1), beat)]
        actual = max(1, int(round(m.length / (tpb * 4 / max(den, 1)))))
        measures.append({"index": m.index, "number": m.number, "pass": m.passno, "start": round(sec(m.tick), 5),
                         "end": round(sec(m.end_tick), 5), "timesig": [actual, den], "beats": beats})
    voice = set()
    for i, st in enumerate(score.staves):
        iid = st.part.instrument().instrument_id or ""
        if iid.startswith("voice."):
            voice.add(i)
    notes = []
    for n in sounding_notes(midi, range(min(len(midi.tracks), len(score.staves))), tl):
        t0, t1 = sec(tl.tick(n.on)), sec(tl.tick(n.off))
        notes.append([round(t0, 5), round(max(t1 - t0, 0.0), 5), int(n.pitch), 0.7,
                      "v" if n.track in voice else "p"])
    notes.sort()
    end = max([measures[-1]["end"] if measures else 0.0] + [n[0] + n[1] for n in notes])
    return {"format": 1, "sig": "", "song": os.path.basename(score_path), "score": score_path,
            "song_end": round(end, 4), "measures": measures, "notes": notes}
