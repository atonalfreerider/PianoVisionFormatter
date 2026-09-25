"""Chord/rest navigation along a track (MuseScore prevChordRest / nextChordRest)."""

from __future__ import annotations

from typing import Dict, List, Optional

from .score import Chord, ChordRest, Score


class Navigator:
    def __init__(self, score: Score):
        self.by_track: Dict[int, List[ChordRest]] = {}
        self.pos: Dict[int, int] = {}
        for m in score.measures:
            for seg in m.chordrest_segments():
                for track, e in seg.elements.items():
                    lst = self.by_track.setdefault(track, [])
                    self.pos[id(e)] = len(lst)
                    lst.append(e)

    def _main_neighbour(self, cr: ChordRest, step: int) -> Optional[ChordRest]:
        lst = self.by_track.get(cr.track, [])
        i = self.pos.get(id(cr))
        if i is None:
            return None
        j = i + step
        return lst[j] if 0 <= j < len(lst) else None

    def prev_chord_rest(self, cr: Optional[ChordRest]) -> Optional[ChordRest]:
        if cr is None:
            return None
        if isinstance(cr, Chord) and cr.is_grace:
            pc = cr.parent
            if cr in pc.grace_before:
                i = pc.grace_before.index(cr)
                if i > 0:
                    return pc.grace_before[i - 1]
                cr = pc
            else:
                i = pc.grace_after.index(cr)
                if i > 0:
                    return pc.grace_after[i - 1]
                return pc
        elif isinstance(cr, Chord) and cr.grace_before:
            return cr.grace_before[-1]
        e = self._main_neighbour(cr, -1)
        if e is not None and isinstance(e, Chord) and e.grace_after:
            return e.grace_after[-1]
        return e

    def next_chord_rest(self, cr: Optional[ChordRest]) -> Optional[ChordRest]:
        if cr is None:
            return None
        if isinstance(cr, Chord) and cr.is_grace:
            pc = cr.parent
            if cr in pc.grace_before:
                i = pc.grace_before.index(cr)
                if i + 1 < len(pc.grace_before):
                    return pc.grace_before[i + 1]
                return pc
            i = pc.grace_after.index(cr)
            if i + 1 < len(pc.grace_after):
                return pc.grace_after[i + 1]
            cr = pc
        elif isinstance(cr, Chord) and cr.grace_after:
            return cr.grace_after[0]
        e = self._main_neighbour(cr, 1)
        if e is not None and isinstance(e, Chord) and e.grace_before:
            return e.grace_before[0]
        return e
