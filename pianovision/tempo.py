"""Tempo map, time-signature map and pause map (MuseScore TempoMap/TimeSigMap/PauseMap)."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

DEFAULT_TEMPO = 2.0          # beats (quarter notes) per second
DIVISION = 480

T_FIX = 1
T_PAUSE = 2
T_RAMP = 4


@dataclass
class TEvent:
    tempo: float
    pause: float = 0.0
    type: int = T_FIX
    time: float = 0.0


class SortedMap:
    """Minimal ordered int-keyed map with std::map-like operations."""

    def __init__(self):
        self.keys: List[int] = []
        self.values: Dict[int, object] = {}

    def __contains__(self, k: int) -> bool:
        return k in self.values

    def __len__(self) -> int:
        return len(self.keys)

    def get(self, k: int):
        return self.values.get(k)

    def insert(self, k: int, v) -> bool:
        """std::map::insert: does not overwrite."""
        if k in self.values:
            return False
        bisect.insort(self.keys, k)
        self.values[k] = v
        return True

    def assign(self, k: int, v) -> None:
        if k not in self.values:
            bisect.insort(self.keys, k)
        self.values[k] = v

    def erase_range(self, k1: int, k2: int) -> None:
        i = bisect.bisect_left(self.keys, k1)
        j = bisect.bisect_left(self.keys, k2)
        for k in self.keys[i:j]:
            del self.values[k]
        del self.keys[i:j]

    def lower_bound(self, k: int) -> int:
        return bisect.bisect_left(self.keys, k)

    def upper_bound(self, k: int) -> int:
        return bisect.bisect_right(self.keys, k)

    def items(self):
        return [(k, self.values[k]) for k in self.keys]

    def clear(self) -> None:
        self.keys.clear()
        self.values.clear()


class TempoMap(SortedMap):
    def __init__(self):
        super().__init__()
        self.pauses: Dict[int, float] = {}
        self.multiplier = 1.0

    def tempo(self, tick: int) -> float:
        if not self.keys:
            return DEFAULT_TEMPO
        i = self.lower_bound(tick)
        if i == len(self.keys):
            return self.values[self.keys[-1]].tempo
        if self.keys[i] == tick:
            return self.values[tick].tempo
        if i == 0:
            return DEFAULT_TEMPO
        return self.values[self.keys[i - 1]].tempo

    def set_tempo(self, tick: int, tempo: float) -> None:
        if not tempo > 0:
            tempo = 0.01
        e = self.values.get(tick)
        if e is not None:
            e.tempo = tempo
            e.type |= T_FIX
        else:
            self.insert(tick, TEvent(tempo, 0.0, T_FIX))
        self.normalize()

    def set_pause(self, tick: int, pause: float) -> None:
        e = self.values.get(tick)
        if e is not None:
            e.pause = pause
            e.type |= T_PAUSE
        else:
            self.insert(tick, TEvent(self.tempo(tick), pause, T_PAUSE))
        self.pauses[tick] = pause
        self.normalize()

    def clear_range(self, t1: int, t2: int) -> None:
        i = self.lower_bound(t1)
        j = self.lower_bound(t2)
        if i == j:
            return
        for k in self.keys[i:j]:
            self.pauses.pop(k, None)
        self.erase_range(t1, t2)

    def normalize(self) -> None:
        time = 0.0
        tick = 0
        tempo = 2.0
        for k in self.keys:
            e = self.values[k]
            if not (e.type & (T_FIX | T_RAMP)):
                e.tempo = tempo
            delta = k - tick
            time += delta / (DIVISION * tempo * self.multiplier)
            time += e.pause
            e.time = time
            tick = k
            tempo = e.tempo

    def tick2time(self, tick: int) -> float:
        time = 0.0
        delta = float(tick)
        tempo = 2.0
        if self.keys:
            ptick = 0
            i = self.lower_bound(tick)
            if i == len(self.keys):
                pe = self.values[self.keys[-1]]
                ptick, tempo, time = self.keys[-1], pe.tempo, pe.time
            elif self.keys[i] == tick:
                e = self.values[tick]
                ptick, tempo, time = tick, e.tempo, e.time
            elif i != 0:
                pe = self.values[self.keys[i - 1]]
                ptick, tempo, time = self.keys[i - 1], pe.tempo, pe.time
            delta = float(tick - ptick)
        return time + delta / (DIVISION * tempo * self.multiplier)


@dataclass
class SigEvent:
    ticks: Fraction        # actual measure length
    timesig: Fraction      # nominal time signature (reduced fraction is not used for export)
    bar: int = 0
    nominal: Tuple[int, int] = (4, 4)


class TimeSigMap(SortedMap):
    def add(self, tick: int, ev: SigEvent) -> None:
        self.assign(tick, ev)

    def timesig(self, tick: int) -> SigEvent:
        if not self.keys:
            return SigEvent(Fraction(1), Fraction(1), 0, (4, 4))
        i = self.upper_bound(tick)
        if i == 0:
            return self.values[self.keys[0]]
        return self.values[self.keys[i - 1]]


class PauseMap(SortedMap):
    """Extra ticks inserted into the MIDI stream for pauses (fermata-free caesuras,
    breath marks, section breaks)."""

    def __init__(self):
        super().__init__()
        self.tempomap_with_pauses = TempoMap()

    def offset_at_utick(self, utick: int) -> int:
        i = self.upper_bound(utick)
        if i != 0:
            i -= 1
        return self.values[self.keys[i]]

    def tick_with_pauses(self, utick: int) -> int:
        if not self.keys:
            return utick
        return utick + self.offset_at_utick(utick)

    def calculate(self, sigmap: TimeSigMap, tempomap: TempoMap, repeat_list) -> None:
        self.insert(0, 0)
        twp = self.tempomap_with_pauses
        twp.multiplier = tempomap.multiplier
        for rs in repeat_list:
            start_tick = rs.tick
            end_tick = rs.end_tick
            tick_offset = rs.utick - rs.tick
            i = tempomap.lower_bound(start_tick)
            j = tempomap.lower_bound(end_tick + 1)
            for k in tempomap.keys[i:j]:
                ev = tempomap.values[k]
                utick = k + tick_offset
                if ev.pause == 0.0:
                    if k != end_tick:
                        twp.insert(self.tick_with_pauses(utick), TEvent(ev.tempo, ev.pause, ev.type, ev.time))
                else:
                    if k != start_tick:
                        sig = sigmap.timesig(k)
                        num, den = sig.nominal
                        qn_per_measure = (4.0 * num) / den
                        ticks_per_measure = int(qn_per_measure * DIVISION)
                        twp.set_tempo(self.tick_with_pauses(utick), qn_per_measure / ev.pause)
                        self.insert(utick, ticks_per_measure + self.offset_at_utick(utick))
                        twp.set_tempo(self.tick_with_pauses(utick), ev.tempo)
