"""Note velocities from dynamics and hairpins (MuseScore VelocityMap)."""

from __future__ import annotations

import bisect
import math
from fractions import Fraction
from typing import List, Optional, Tuple

DEFAULT_VALUE = 80
MIN_VALUE = 1
MAX_VALUE = 127
STEP = 16

DYNAMIC, HAIRPIN, INVALID = 0, 1, 2
INCREASING, DECREASING = 0, 1


class VelocityEvent:
    __slots__ = ("type", "value", "length", "method", "direction", "start_val", "end_val")

    def __init__(self, etype=INVALID, value=0, length=Fraction(0), method="normal", direction=INCREASING):
        self.type = etype
        self.value = value
        self.length = length
        self.method = method
        self.direction = direction
        self.start_val = -1
        self.end_val = -1

    def copy(self) -> "VelocityEvent":
        e = VelocityEvent(self.type, self.value, self.length, self.method, self.direction)
        e.start_val, e.end_val = self.start_val, self.end_val
        return e


class VelocityMap:
    """std::multimap<Fraction, VelocityEvent>: sorted by tick, insertion order within a tick."""

    def __init__(self):
        self.ticks: List[Fraction] = []
        self.events: List[VelocityEvent] = []
        self._compiled = None

    # -- multimap primitives --
    def insert(self, tick: Fraction, ev: VelocityEvent) -> None:
        i = bisect.bisect_right(self.ticks, tick)
        self.ticks.insert(i, tick)
        self.events.insert(i, ev)
        self._compiled = None

    def _erase(self, i: int) -> None:
        del self.ticks[i]
        del self.events[i]
        self._compiled = None

    def equal_range(self, tick: Fraction) -> Tuple[int, int]:
        return bisect.bisect_left(self.ticks, tick), bisect.bisect_right(self.ticks, tick)

    # -- building --
    def add_dynamic(self, tick: Fraction, value: int) -> None:
        self.insert(tick, VelocityEvent(DYNAMIC, value))

    def add_hairpin(self, stick: Fraction, etick: Fraction, change: int, method: str, direction: int) -> None:
        change = abs(change)
        change *= 1 if direction == INCREASING else -1
        self.insert(stick, VelocityEvent(HAIRPIN, change, etick - stick, method, direction))

    def setup(self) -> None:
        self._sort_hairpins()
        self._resolve_hairpin_collisions()
        self._resolve_dynamic_inside_hairpin_collisions()
        self._add_missing_dynamics_after_hairpins()
        self._fill_hairpins_cache()

    def _sort_hairpins(self) -> None:
        for tick in sorted(set(self.ticks)):
            a, b = self.equal_range(tick)
            hairpins = [e for e in self.events[a:b] if e.type != DYNAMIC]
            if len(hairpins) > 1:
                hairpins.sort(key=lambda e: e.length, reverse=True)
                for e in hairpins:
                    self.insert(tick, e.copy())

    def _resolve_hairpin_collisions(self) -> None:
        current_end = Fraction(-1)
        in_hairpin = False
        end_points = []
        starts_in = []
        i = 0
        while i < len(self.events):
            tick, ev = self.ticks[i], self.events[i]
            etick = tick + ev.length
            if current_end < tick:
                in_hairpin = False
            if ev.type == HAIRPIN:
                if in_hairpin:
                    if etick <= current_end:
                        self._erase(i)
                        continue
                    current_end = etick
                    starts_in.append(True)
                else:
                    current_end = etick
                    in_hairpin = True
                    starts_in.append(False)
                end_points.append((tick, etick))
            i += 1
        self._adjust_colliding(starts_in, end_points)

    def _adjust_colliding(self, starts_in, end_points) -> None:
        move_to = {}
        i = 0
        j = -1
        while i < len(self.events):
            tick, ev = self.ticks[i], self.events[i]
            if ev.type != HAIRPIN:
                i += 1
                continue
            j += 1
            if not starts_in[j]:
                i += 1
                continue
            new_tick = end_points[j - 1][1]
            ev.length -= (new_tick - tick)
            move_to[new_tick] = ev
            self._erase(i)
        for k in sorted(move_to):
            self.insert(k, move_to[k])

    def _resolve_dynamic_inside_hairpin_collisions(self) -> None:
        last = None
        for i in range(len(self.events)):
            ev = self.events[i]
            if ev.type == DYNAMIC and last is not None and self.events[last].type == HAIRPIN:
                dyn_tick = self.ticks[i]
                hp_start = self.ticks[last]
                if hp_start + self.events[last].length > dyn_tick:
                    self.events[last].length = dyn_tick - hp_start
            last = i

    def _dynamic_exists_on_tick(self, tick: Fraction) -> bool:
        a, b = self.equal_range(tick)
        return any(e.type == DYNAMIC for e in self.events[a:b])

    def _dynamic_event_for_tick(self, tick: Fraction) -> VelocityEvent:
        a, b = self.equal_range(tick)
        for e in self.events[a:b]:
            if e.type == DYNAMIC:
                return e
        if a == 0:
            return VelocityEvent()
        prev_tick = self.ticks[a - 1]
        pa, pb = self.equal_range(prev_tick)
        for e in self.events[pa:pb]:
            if e.type == DYNAMIC:
                return e
        return VelocityEvent()

    def _add_missing_dynamics_after_hairpins(self) -> None:
        def next_val(prev: int, direction: int) -> int:
            v = prev + STEP if direction == INCREASING else prev - STEP
            return max(MIN_VALUE, min(MAX_VALUE, v))

        i = 0
        while i < len(self.events):
            ev = self.events[i]
            if ev.type == HAIRPIN and ev.value == 0:
                start = self.ticks[i]
                end = start + ev.length
                dyn_cur = self._dynamic_event_for_tick(start)
                if not self._dynamic_exists_on_tick(end):
                    if dyn_cur.type != INVALID:
                        self.insert(end, VelocityEvent(DYNAMIC, next_val(dyn_cur.value, ev.direction)))
                else:
                    dyn_end = self._dynamic_event_for_tick(end)
                    jump = dyn_end.value - dyn_cur.value
                    if (ev.direction == INCREASING and jump < 0) or (ev.direction == DECREASING and jump > 0):
                        ev.value = next_val(dyn_cur.value, ev.direction) - dyn_cur.value
            i += 1

    def _fill_hairpins_cache(self) -> None:
        for i in range(len(self.events)):
            tick, ev = self.ticks[i], self.events[i]
            if ev.type != HAIRPIN:
                continue
            a, b = self.equal_range(tick)
            found = False
            for e in self.events[a:b]:
                if e.type == DYNAMIC:
                    ev.start_val = e.value
                    found = True
                    break
            if not found:
                if i != 0:
                    ptick = self.ticks[i - 1]
                    pa, pb = self.equal_range(ptick)
                    found_hp = False
                    for e in self.events[pa:pb]:
                        if e.type == HAIRPIN:
                            ev.start_val = e.end_val
                            found_hp = True
                            break
                    if not found_hp:
                        ev.start_val = self.events[i - 1].value
                else:
                    ev.start_val = DEFAULT_VALUE
            if ev.value == 0:
                n = i + 1
                if n < len(self.events) and self.ticks[n] == tick:
                    n += 1
                if n >= len(self.events):
                    ev.end_val = ev.start_val
                else:
                    na, nb = self.equal_range(self.ticks[n])
                    found2 = False
                    for e in self.events[na:nb]:
                        if e.type == DYNAMIC:
                            ev.end_val = e.value
                            found2 = True
                            break
                    if not found2:
                        ev.end_val = ev.start_val
            else:
                ev.end_val = ev.start_val + ev.value
            if (ev.start_val > ev.end_val and ev.direction == INCREASING) or \
                    (ev.start_val < ev.end_val and ev.direction == DECREASING):
                ev.end_val = ev.start_val

    # -- query --
    def _compile(self):
        """Integer-tick view of the map: same answers as val() for whole-tick queries."""
        from .score import ticks as fticks
        n = len(self.ticks)
        ceil_keys = [-((-k * 1920).numerator // (k * 1920).denominator) for k in self.ticks]
        info = []
        i = 0
        while i < n:
            j = i
            while j + 1 < n and self.ticks[j + 1] == self.ticks[i]:
                j += 1
            hp = None
            for k in range(i, j + 1):
                if self.events[k].type == HAIRPIN:
                    hp = self.events[k]
            if hp is None:
                entry = (None, None, None, None, None)
            else:
                end = (self.ticks[i] + hp.length) * 1920
                end_ceil = -((-end.numerator) // end.denominator)
                entry = (hp, end_ceil, fticks(self.ticks[i]), fticks(hp.length) if hp.length else 0, self.ticks[i])
            for k in range(i, j + 1):
                info.append(entry)
            i = j + 1
        self._compiled = (ceil_keys, info)
        return self._compiled

    def val_ticks(self, t: int) -> int:
        """val(Fraction.fromTicks(t)) for an integer tick."""
        comp = self._compiled or self._compile()
        ceil_keys, info = comp
        i = bisect.bisect_right(ceil_keys, t)
        if i == 0:
            return DEFAULT_VALUE
        i -= 1
        hp, end_ceil, key_ticks, len_ticks, key = info[i]
        if hp is None:
            return self.events[i].value
        if t >= end_ceil:
            return hp.end_val
        if hp.start_val == hp.end_val or hp.length == 0:
            return hp.start_val
        return hp.start_val + _curve(hp.method, hp.end_val - hp.start_val, len_ticks, t - key_ticks)

    def val(self, tick: Fraction) -> int:
        if tick.denominator == 1 or 1920 % tick.denominator == 0:
            return self.val_ticks(int(tick * 1920))
        return self._val_exact(tick)

    def _val_exact(self, tick: Fraction) -> int:
        i = bisect.bisect_right(self.ticks, tick)
        if i == 0:
            return DEFAULT_VALUE
        i -= 1
        hp_tick = self.ticks[i]
        a, b = self.equal_range(hp_tick)
        found = None
        for e in self.events[a:b]:
            if e.type == HAIRPIN:
                found = e
        if found is None:
            return self.events[i].value
        if tick >= hp_tick + found.length:
            return found.end_val
        return self._interpolate(hp_tick, found, tick)

    @staticmethod
    def _interpolate(ev_tick: Fraction, ev: VelocityEvent, tick: Fraction) -> int:
        if ev.start_val == ev.end_val or ev.length == 0:
            return ev.start_val
        from .score import ticks as fticks
        return ev.start_val + _curve(ev.method, ev.end_val - ev.start_val, fticks(ev.length),
                                     fticks(tick) - fticks(ev_tick))


def _curve(m: str, diff: int, expr_ticks: int, ct: int) -> int:
    """VelocityMap::interpolate value function (C++ int truncation of doubles)."""
    if m == "exponential":
        if diff > 0:
            return int(math.pow(math.pow(diff + 1, 1.0 / expr_ticks), ct) - 1)
        return -int(math.pow(math.pow(-diff + 1, 1.0 / expr_ticks), ct) + 1)
    if m == "ease-in-out":
        return int((diff / 2.0) * (math.sin(ct * (math.pi / expr_ticks) - math.pi / 2.0) + 1))
    if m == "ease-in":
        return int(diff * (math.sin((ct - expr_ticks) * (math.pi / (2 * expr_ticks))) + 1))
    if m == "ease-out":
        return int(diff * math.sin(ct * (math.pi / (2 * expr_ticks))))
    return int(diff * (ct / expr_ticks))
