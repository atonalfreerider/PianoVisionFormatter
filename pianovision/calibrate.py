"""Find the Compat knobs that reproduce an existing JSON byte-for-byte.

Outputs made by the old workflow came from MuseScore's own MIDI export, run
with different MuseScore versions (4.5 and 4.6) and sometimes from an open
GUI session.  Each difference has a Compat knob; this module searches them.
"""

from __future__ import annotations

import collections
import itertools
import json
import time
from typing import Iterable, List, Optional, Tuple

from .convert import convert_mscz
from .mscx import read_score
from .render import Compat, _play, tempo_value
from .score import SEG_CHORDREST, SEG_TIMETICK, ticks


def knob_combinations() -> Iterable[Compat]:
    """Every combination of the switch knobs; plain 4.6 and 4.5 exports first."""
    yield Compat(dynamics="4.6")
    yield Compat(dynamics="4.5")
    for text, model, volta, dyn in itertools.product((False, True), ("auto", "read", "layout"),
                                                     (None, True, False), ("4.6", "4.5")):
        if text or model != "auto" or volta is not None:
            yield Compat(dynamics=dyn, tempo_text=text, tempo_model=model, volta_tempo=volta)


def note_signature(data: bytes) -> Tuple[tuple, tuple]:
    """(pitch, start tick, length) of every note per hand: what the player sees,
    independent of tempo and velocity."""
    doc = json.loads(data)
    sig = []
    for hand in ("right", "left"):
        sig.append(tuple((n["note"], n["ticksStart"], n["durationTicks"])
                         for m in doc.get("tracksV2", {}).get(hand, []) for n in m.get("notes", [])))
    return tuple(sig)


def _tempo_pairs(data: bytes) -> collections.Counter:
    doc = json.loads(data)
    return collections.Counter((t["ticks"], round(60000000 / t["bpm"])) for t in doc.get("tempos", []))


def text_tempo_candidates(mscz_path: str) -> List[int]:
    """Ticks of tempo markings whose text implies a different value than the stored one."""
    score = read_score(mscz_path)
    out = []
    for m in score.measures:
        for seg in m.segments:
            if seg.type not in (SEG_CHORDREST, SEG_TIMETICK):
                continue
            for e in seg.annotations:
                if e.tag == "Tempo" and _play(e) and tempo_value(e, False) != tempo_value(e, True):
                    out.append(ticks(seg.tick))
    return out


def calibrate(mscz_path: str, target: bytes, orchestra: bool = True, simplified: bool = True,
              budget: float = 180.0, metadata: str = "v2") -> Optional[Compat]:
    """Knobs under which ``mscz_path`` renders to exactly ``target`` (None if not found in ``budget`` s)."""
    deadline = time.monotonic() + budget

    def render(c: Compat) -> bytes:
        c.metadata = metadata
        return convert_mscz(mscz_path, c, orchestra, simplified).data

    for c in knob_combinations():
        if time.monotonic() > deadline:
            return None
        if render(c) == target:
            return c

    # Individual tempo markings recomputed from their text (edited in an open MuseScore session).
    cands = text_tempo_candidates(mscz_path)
    if not cands:
        return None
    want = _tempo_pairs(target)
    for dyn in ("4.6", "4.5"):
        chosen: List[int] = []

        def fit(tt: List[int]) -> Tuple[int, bytes]:
            data = render(Compat(dynamics=dyn, text_tempo_ticks=tuple(sorted(tt))))
            got = _tempo_pairs(data)
            return sum((got & want).values()) - sum((got - want).values()), data

        best, data = fit(chosen)
        for t in cands:
            if time.monotonic() > deadline:
                return None
            s, d = fit(chosen + [t])
            if s > best:
                chosen.append(t)
                best, data = s, d
        if data == target:
            return Compat(dynamics=dyn, text_tempo_ticks=tuple(sorted(chosen)), metadata=metadata)
    return None
