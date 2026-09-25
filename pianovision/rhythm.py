"""Rhythmic splitting of note/rest durations.

Port of MuseScore 4.6 toRhythmicDurationList() (dom/durationtype.cpp) with
populateRhythmicList, splitCompoundBeatsForList, forceRhythmicSplit*,
toDurationList, the TDuration(Fraction, ...) constructor and the TimeSigFrac
beat/subbeat helpers from dom/sig.cpp.
"""

from __future__ import annotations

from fractions import Fraction
from typing import List, Optional, Tuple

DIVISION = 480            # ticks per quarter note (Constants::DIVISION)
MAX_DOTS = 4              # note.h

# DurationType (types.h), in enum order
(V_LONG, V_BREVE, V_WHOLE, V_HALF, V_QUARTER, V_EIGHTH, V_16TH, V_32ND, V_64TH,
 V_128TH, V_256TH, V_512TH, V_1024TH, V_ZERO, V_MEASURE, V_INVALID) = range(16)

# TConv XML names; V_ZERO and V_INVALID have empty names in MuseScore
DURATION_NAMES = ("long", "breve", "whole", "half", "quarter", "eighth", "16th",
                  "32nd", "64th", "128th", "256th", "512th", "1024th", "", "measure", "")

# BeatType (sig.h): lower value = stronger beat
(DOWNBEAT, COMPOUND_STRESSED, SIMPLE_STRESSED, COMPOUND_UNSTRESSED,
 SIMPLE_UNSTRESSED, COMPOUND_SUBBEAT, SUBBEAT) = range(7)


# ---------------------------------------------------------------------------
# C++ integer / Fraction emulation
# ---------------------------------------------------------------------------

def _cdiv(a: int, b: int) -> int:
    """C++ int division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _cmod(a: int, b: int) -> int:
    """C++ int remainder (sign follows the dividend)."""
    return a - b * _cdiv(a, b)


def ticks(f: Fraction) -> int:
    """Fraction::ticks(): 1920 ticks per whole, rounded half away from zero."""
    # HACK in MuseScore: Fraction(-1, 1) maps to -1 tick. (C++ checks the
    # unreduced representation; Python fractions are always reduced.)
    if f == -1:
        return -1
    num, den = f.numerator, f.denominator
    sgn = -1 if num < 0 else 1
    return sgn * ((sgn * num * DIVISION * 4 + den // 2) // den)


def from_ticks(t: int) -> Fraction:
    """Fraction::fromTicks()."""
    if t == -1:
        return Fraction(-1, 1)   # HACK mirror of ticks()
    return Fraction(t, DIVISION * 4)


# ---------------------------------------------------------------------------
# TimeSigFrac (sig.h / sig.cpp)
# ---------------------------------------------------------------------------

class TimeSigFrac:
    """Nominal time signature; numerator/denominator are kept unreduced (6/8 != 3/4)."""

    __slots__ = ("num", "den")

    def __init__(self, num: int, den: int):
        if den < 0:                      # Fraction(int, int) sign normalisation
            num, den = -num, -den
        self.num = num
        self.den = den

    def numerator(self) -> int:
        return self.num

    def denominator(self) -> int:
        return self.den

    # isCompound? Note: 3/8, 3/16, ... are NOT considered compound.
    def is_compound(self) -> bool:
        return self.num > 3 and _cmod(self.num, 3) == 0

    def d_unit_ticks(self) -> int:
        return _cdiv(4 * DIVISION, self.den)

    def ticks_per_measure(self) -> int:
        return self.num * self.d_unit_ticks()

    def d_units_per_beat(self) -> int:
        return 3 if self.is_compound() else 1

    def beat_ticks(self) -> int:
        return self.d_unit_ticks() * self.d_units_per_beat()

    def beats_per_measure(self) -> int:
        return _cdiv(self.num, self.d_units_per_beat())

    def subbeat_ticks(self, level: int) -> int:
        # assert(level <= maxSubbeatLevel()) is debug-only in C++
        st = self.d_unit_ticks()
        while level > 0:
            st = _cdiv(st, 2)
            level -= 1
        return st

    def max_subbeat_level(self) -> int:
        level = 0
        st = self.d_unit_ticks()
        while _cmod(st, 2) == 0:
            st = _cdiv(st, 2)
            level += 1
        return level

    def is_triple(self) -> bool:
        return _cmod(self.beats_per_measure(), 3) == 0

    def is_duple(self) -> bool:
        # C++ asserts !isTriple() (debug only); always test is_triple() first
        return _cmod(self.beats_per_measure(), 2) == 0

    def rtick2beat_type(self, rtick: int) -> int:
        if rtick == 0:
            return DOWNBEAT      # only for rtick == 0, not for rtick == measureTicks
        if _cmod(rtick, self.d_unit_ticks()) != 0:
            return SUBBEAT
        if self.is_compound():
            if _cmod(rtick, self.beat_ticks()) != 0:
                return COMPOUND_SUBBEAT
        beat_num = _cdiv(rtick, self.beat_ticks())
        if self.is_triple():
            stress_beat = 3
        elif self.is_duple():
            stress_beat = 2
        else:
            stress_beat = _cdiv(self.num + 1, 2)   # 5/4 = (3+2)/4
        if stress_beat and _cmod(beat_num, stress_beat) == 0:
            return COMPOUND_STRESSED if self.is_compound() else SIMPLE_STRESSED
        return COMPOUND_UNSTRESSED if self.is_compound() else SIMPLE_UNSTRESSED

    def strongest_beat_in_range(self, rtick1: int, rtick2: int, d_units_crossed: int = 0,
                                subbeat_tick: int = 0, save_last: bool = False) -> Tuple[int, int, int]:
        """Returns (strongest BeatType, d_units_crossed, subbeat_tick).

        The two int out-params of the C++ version are passed in and returned
        updated; subbeat_tick is only overwritten when a new strongest is found.
        """
        # assert(rtick2 > rtick1) is debug-only
        strongest = SUBBEAT
        rtick = rtick1 + self.ticks_to_next_d_unit(rtick1)
        while rtick < rtick2:
            d_units_crossed += 1
            beat_type = self.rtick2beat_type(rtick)
            if beat_type < strongest + int(save_last):   # "<" behaves like "<=" if save_last
                strongest = beat_type
                subbeat_tick = rtick
            rtick += self.d_unit_ticks()
        return strongest, d_units_crossed, subbeat_tick

    def rtick2subbeat_level(self, rtick: int) -> int:
        """0 on a dUnit, n on a multiple of dUnit/2**n, -(n+1) if not found."""
        level = 0
        st = self.d_unit_ticks()
        remainder = _cmod(rtick, st)
        while remainder != 0:
            level += 1
            if _cmod(st, 2) != 0:
                return -level
            st = _cdiv(st, 2)
            remainder = _cmod(remainder, st)
        return level

    def strongest_subbeat_level_in_range(self, rtick1: int, rtick2: int,
                                         subbeat_tick: int = 0) -> Tuple[int, int]:
        """Returns (level, subbeat_tick); level < 0 if none found (subbeat_tick unchanged)."""
        # assert(rtick2 > rtick1) is debug-only
        level = 0
        st = self.d_unit_ticks()
        while True:
            n = _cdiv(rtick1, st)
            m = _cdiv(rtick2 - 1, st)      # -1 to make the range exclusive
            if m > n:
                return level, m * st
            level += 1
            if _cmod(st, 2) != 0:
                return -level, subbeat_tick
            st = _cdiv(st, 2)

    def ticks_past_d_unit(self, rtick: int) -> int:
        return _cmod(rtick, self.d_unit_ticks())

    def ticks_to_next_d_unit(self, rtick: int) -> int:
        return self.d_unit_ticks() - self.ticks_past_d_unit(rtick)

    def ticks_past_beat(self, rtick: int) -> int:
        return _cmod(rtick, self.beat_ticks())

    def ticks_to_next_beat(self, rtick: int) -> int:
        return self.beat_ticks() - self.ticks_past_beat(rtick)

    def ticks_past_subbeat(self, rtick: int, level: int) -> int:
        return _cmod(rtick, self.subbeat_ticks(level))

    def ticks_to_next_subbeat(self, rtick: int, level: int) -> int:
        return self.subbeat_ticks(level) - self.ticks_past_subbeat(rtick, level)


# ---------------------------------------------------------------------------
# TDuration (durationtype.cpp)
# ---------------------------------------------------------------------------

class TDuration:
    __slots__ = ("val", "dots")

    def __init__(self, val: int = V_INVALID, dots: int = 0):
        self.val = val
        self.dots = dots

    @classmethod
    def from_fraction(cls, l: Fraction, truncate: bool = False, max_dots: int = 4,
                      max_type: int = V_LONG) -> "TDuration":
        """TDuration(const Fraction&, bool truncate, int maxDots, DurationType maxType):
        longest duration (starting at max_type) that fits into l."""
        d = cls()
        d.set_type(max_type)
        d.set_dots(max_dots)
        d._truncate_to_fraction(l, max_dots)
        # assert(truncate || exact fit) is debug-only in C++
        return d

    def is_valid(self) -> bool:
        return self.val != V_INVALID

    def set_type(self, t: int) -> None:
        self.val = t
        if t == V_MEASURE:
            self.dots = 0

    def set_dots(self, v: int) -> None:
        self.dots = max(0, min(v, MAX_DOTS))

    def _raw_fraction(self) -> Tuple[int, int]:
        """TDuration::fraction() as the unreduced (numerator, denominator) pair."""
        z = 1
        v = self.val
        if V_WHOLE <= v <= V_1024TH:
            n = 1 << (v - V_WHOLE)
        elif v == V_BREVE:
            z, n = 2, 1
        elif v == V_LONG:
            z, n = 4, 1
        elif v == V_ZERO:
            z, n = 0, 1
        else:                               # V_MEASURE, V_INVALID -> 0/0
            z, n = 0, 0
        dot_n = (1 << (self.dots + 1)) - 1
        dot_d = 1 << self.dots
        return z * dot_n, n * dot_d

    def fraction(self) -> Optional[Fraction]:
        """None stands for MuseScore's invalid 0/0 fraction (V_MEASURE / V_INVALID)."""
        z, n = self._raw_fraction()
        return None if n == 0 else Fraction(z, n)

    def _fits(self, l: Fraction) -> bool:
        # (fraction() - l).numerator() <= 0; for 0/0 the C++ difference is 0/0 -> true
        f = self.fraction()
        return f is None or f <= l

    def equals_fraction(self, l: Fraction) -> bool:
        # Fraction::operator== cross-multiplies, so 0/0 compares equal to anything
        f = self.fraction()
        return f is None or f == l

    def shift_type(self, n_steps: int, step_dotted: bool = False) -> None:
        if self.val in (V_MEASURE, V_INVALID, V_ZERO):
            self.set_type(V_INVALID)
            return
        if step_dotted:
            round_down_single_dots = -1 if self.dots > 0 else 0
            steps = self.val * 2 + round_down_single_dots + n_steps
            new_dots = _cmod(steps, 2)
            new_value = _cdiv(steps, 2) + new_dots
        else:
            new_dots = self.dots
            new_value = self.val + n_steps
        if (new_value < V_LONG or new_value > V_1024TH
                or (new_value >= V_1024TH and new_dots >= 1)
                or (new_value >= V_512TH and new_dots >= 2)
                or (new_value >= V_256TH and new_dots >= 3)
                or (new_value >= V_128TH and new_dots >= 4)):
            self.set_type(V_INVALID)
        else:
            self.set_type(new_value)
            self.set_dots(new_dots)

    def _truncate_to_fraction(self, l: Fraction, max_dots: int) -> None:
        # try to fit in l by reducing number of duration dots
        if self._set_dots_to_fit_fraction(l, self.dots):
            return
        # that wasn't enough so now change type too
        self.shift_type(1)
        while self.is_valid():
            if self._set_dots_to_fit_fraction(l, max_dots):
                return
            self.shift_type(1)

    def _set_dots_to_fit_fraction(self, l: Fraction, max_dots: int) -> bool:
        while max_dots >= 0:
            self.dots = max_dots            # ensures dots >= 0 if this returns False
            if self._fits(l):
                return True
            max_dots -= 1
        return False

    def __repr__(self) -> str:
        return f"TDuration({DURATION_NAMES[self.val] or self.val}, dots={self.dots})"


def to_duration_list(l: Fraction, use_dots: bool, max_dots: int = 4) -> List[TDuration]:
    """toDurationList(): greedy split into the largest possible durations."""
    if not use_dots:
        max_dots = 0
    d_list: List[TDuration] = []
    dd = TDuration.from_fraction(l, True, max_dots)
    while dd.is_valid() and l > 0:
        d_list.append(dd)
        l -= dd.fraction()
        dd = TDuration.from_fraction(l, True, max_dots, dd.val)
    # (C++ logs "rest remains" here only when printRestRemains is set)
    return d_list


# ---------------------------------------------------------------------------
# Rhythmic splitting (durationtype.cpp)
# ---------------------------------------------------------------------------

def _to_rhythmic_duration_list(l: Fraction, is_rest: bool, rtick_start: Fraction,
                               nominal: TimeSigFrac, measure_ticks: Fraction,
                               is_anacrusis: bool, anacrusis_offset: Fraction,
                               max_dots: int) -> List[TDuration]:
    d_list: List[TDuration] = []
    if is_anacrusis:
        rtick_start = rtick_start + anacrusis_offset
    elif is_rest and l == measure_ticks:
        d_list.append(TDuration(V_MEASURE))
        return d_list

    if nominal.is_compound():
        split_compound_beats_for_list(d_list, l, is_rest, rtick_start, nominal, max_dots)
    else:
        populate_rhythmic_list(d_list, l, is_rest, rtick_start, nominal, max_dots)
    return d_list


def populate_rhythmic_list(d_list: List[TDuration], l: Fraction, is_rest: bool,
                           rtick_start: Fraction, nominal: TimeSigFrac, max_dots: int) -> None:
    rtick_end = rtick_start + l
    t_start = ticks(rtick_start)
    t_end = ticks(rtick_end)

    need_to_split = False
    rtick_split = 0

    # CHECK AT SUBBEAT LEVEL
    start_level = nominal.rtick2subbeat_level(t_start)
    end_level = nominal.rtick2subbeat_level(t_end)
    strongest_level_crossed, rtick_split = nominal.strongest_subbeat_level_in_range(
        t_start, t_end, rtick_split)

    if start_level < 0 or end_level < 0 or strongest_level_crossed < 0:
        # Beyond maximum subbeat level so just split into largest possible durations.
        d_list.extend(to_duration_list(l, max_dots > 0, max_dots))
        return

    # split if we cross something stronger than where we start and end
    if strongest_level_crossed < start_level and strongest_level_crossed < end_level:
        need_to_split = True
    # but don't split for level 1 syncopation
    if start_level == end_level and strongest_level_crossed == start_level - 1:
        need_to_split = False
    # nor for level 2 syncopation, but disallow sixteenth-note, quarter, quarter...
    if start_level == end_level and strongest_level_crossed == start_level - 2:
        ticks_to_next = nominal.ticks_to_next_subbeat(t_start, start_level - 1)
        ticks_past_prev = nominal.ticks_past_subbeat(t_start, start_level - 1)
        need_to_split = ticks_to_next != ticks_past_prev

    if not need_to_split and strongest_level_crossed == 0:
        # NOW CHECK AT DENOMINATOR UNIT LEVEL AND BEAT LEVEL
        start_beat = nominal.rtick2beat_type(t_start)
        end_beat = nominal.rtick2beat_type(t_end)
        use_last = start_beat <= SIMPLE_UNSTRESSED    # split on the later beat if starting on a beat
        strongest_beat_crossed, d_units_crossed, rtick_split = nominal.strongest_beat_in_range(
            t_start, t_end, 0, rtick_split, use_last)
        need_to_split = force_rhythmic_split(is_rest, start_beat, end_beat, d_units_crossed,
                                             strongest_beat_crossed, nominal)

    if not need_to_split:
        # CHECK THERE IS A DURATION THAT FITS
        d = TDuration.from_fraction(l, True, max_dots)
        if d.equals_fraction(l):
            # push_back(l) converts via TDuration(l) with its defaults (maxDots = 4)
            d_list.append(TDuration.from_fraction(l))
            return
        # no single TDuration fits so must split anyway

    # Prevent infinite recursion if there is no splitting point other than the start and end ticks
    if not (t_start < rtick_split < t_end):
        d_list.extend(to_duration_list(l, max_dots > 0, max_dots))
        return

    # Split on the strongest beat or subbeat crossed
    left_split = from_ticks(rtick_split) - rtick_start
    right_split = l - left_split
    populate_rhythmic_list(d_list, left_split, is_rest, rtick_start, nominal, max_dots)
    populate_rhythmic_list(d_list, right_split, is_rest, from_ticks(rtick_split), nominal, max_dots)


def split_compound_beats_for_list(d_list: List[TDuration], l: Fraction, is_rest: bool,
                                  rtick_start: Fraction, nominal: TimeSigFrac, max_dots: int) -> None:
    """Split compound notes/rests where they enter a compound beat."""
    rtick_end = rtick_start + l
    start_beat = nominal.rtick2beat_type(ticks(rtick_start))
    end_beat = nominal.rtick2beat_type(ticks(rtick_end))

    if start_beat > COMPOUND_UNSTRESSED:
        # Not starting on a compound beat so mustn't extend into next compound beat
        split_ticks = nominal.ticks_to_next_beat(ticks(rtick_start))
        if ticks(rtick_end - rtick_start) > split_ticks:
            left_split = from_ticks(split_ticks)
            right_split = l - left_split
            populate_rhythmic_list(d_list, left_split, is_rest, rtick_start, nominal, max_dots)
            split_compound_beats_for_list(d_list, right_split, is_rest,
                                          rtick_start + from_ticks(split_ticks), nominal, max_dots)
            return

    if end_beat > COMPOUND_UNSTRESSED:
        # Not ending on a compound beat so mustn't extend into previous compound beat
        split_ticks = nominal.ticks_past_beat(ticks(rtick_end))
        if ticks(rtick_end - rtick_start) > split_ticks:
            right_split = from_ticks(split_ticks)
            left_split = l - right_split
            populate_rhythmic_list(d_list, left_split, is_rest, rtick_start, nominal, max_dots)
            populate_rhythmic_list(d_list, right_split, is_rest,
                                   rtick_end - from_ticks(split_ticks), nominal, max_dots)
            return

    # Duration either starts and ends on compound beats, or it remains within a single compound beat
    populate_rhythmic_list(d_list, l, is_rest, rtick_start, nominal, max_dots)


def force_rhythmic_split(is_rest: bool, start_beat: int, end_beat: int, d_units_crossed: int,
                         strongest_beat_crossed: int, nominal: TimeSigFrac) -> bool:
    """Whether to split (and tie) to show the rhythm. The C++ asserts are debug-only."""
    # nothing can cross a stressed beat in an irregular time signature
    if strongest_beat_crossed <= SIMPLE_STRESSED and not nominal.is_triple() and not nominal.is_duple():
        return True
    if is_rest:
        # rests must not cross the middle of a bar with numerator == 2 (e.g. 2/4)
        if strongest_beat_crossed <= SIMPLE_UNSTRESSED and nominal.numerator() == 2:
            return True
        # rests must not cross a beat in a triple meter (3/4, 9/8, ...)
        if strongest_beat_crossed <= SIMPLE_UNSTRESSED and nominal.is_triple():
            return True

    if nominal.is_compound():
        return force_rhythmic_split_compound(is_rest, start_beat, end_beat, d_units_crossed,
                                             strongest_beat_crossed)
    return force_rhythmic_split_simple(is_rest, start_beat, end_beat, d_units_crossed,
                                       strongest_beat_crossed)


def force_rhythmic_split_compound(is_rest: bool, start_beat: int, end_beat: int,
                                  d_units_crossed: int, strongest_beat_crossed: int) -> bool:
    if strongest_beat_crossed == COMPOUND_STRESSED:
        # notes start and end on a compound beat, so pretend it's a simple measure
        return force_rhythmic_split_simple(is_rest, start_beat, end_beat,
                                           _cdiv(d_units_crossed, 3), SIMPLE_STRESSED)
    if strongest_beat_crossed == COMPOUND_UNSTRESSED:
        return False
    if strongest_beat_crossed == COMPOUND_SUBBEAT:
        # don't split anything that takes up a full compound beat
        if start_beat <= COMPOUND_UNSTRESSED and end_beat <= COMPOUND_UNSTRESSED:
            return False
        # split rests that don't start on a compound beat
        if is_rest and start_beat > COMPOUND_UNSTRESSED:
            return True
        # remaining groupings within compound triplets are the same as for simple triple
        return force_rhythmic_split_simple(is_rest, start_beat, end_beat, d_units_crossed,
                                           SIMPLE_UNSTRESSED)
    # default (BeatType::SUBBEAT and anything else)
    return force_rhythmic_split_simple(is_rest, start_beat, end_beat, d_units_crossed,
                                       strongest_beat_crossed)


def force_rhythmic_split_simple(is_rest: bool, start_beat: int, end_beat: int,
                                beats_crossed: int, strongest_beat_crossed: int) -> bool:
    if strongest_beat_crossed == SIMPLE_STRESSED:
        if is_rest:
            return True
        # don't split notes that start or end on a stressed beat (enables double-dotting in 4/4)
        if start_beat <= SIMPLE_STRESSED or end_beat <= SIMPLE_STRESSED:
            return False
        # don't split notes that both start and end on unstressed beats or stronger
        if start_beat <= SIMPLE_UNSTRESSED and end_beat <= SIMPLE_UNSTRESSED:
            return False
        return True
    if strongest_beat_crossed == SIMPLE_UNSTRESSED:
        # don't split notes or rests if starting and ending on stressed beat
        if start_beat <= SIMPLE_STRESSED and end_beat <= SIMPLE_STRESSED:
            return False
        # split rests that don't start or end on a beat; notes may cross 1 unstressed beat
        if start_beat == SUBBEAT or end_beat == SUBBEAT:
            return is_rest or beats_crossed > 1
        return False
    return False   # BeatType::SUBBEAT and anything else


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rhythmic_duration_list(l, is_rest, rtick_start, nominal_num, nominal_den, measure_ticks,
                           is_anacrusis=False, anacrusis_offset=Fraction(0),
                           max_dots=0) -> List[Tuple[str, int, Optional[Fraction]]]:
    """toRhythmicDurationList(l, isRest, rtickStart, nominal, msr, maxDots).

    l, rtick_start and measure_ticks are Fractions of a whole note; rtick_start
    is relative to the measure start. nominal_num/nominal_den is the nominal time
    signature (kept unreduced, e.g. 6/8). measure_ticks is Measure::ticks(), the
    measure's actual length.

    is_anacrusis stands in for Measure::isAnacrusis(): True when the measure is
    shorter than its time signature (timesig() - ticks() > 0) and it is
    irregular ("exclude from measure count"), or has no previous MeasureBase, or
    the previous one carries a line/page/section break or is a V/H/F/T box
    frame. anacrusis_offset is Measure::anacrusisOffset(): timesig() - ticks()
    for such a measure, else 0. For an anacrusis the start is shifted by the
    offset and a full-measure rest is never produced.

    Returns (MuseScore XML type name, dots, fraction) per duration. "measure"
    (V_MEASURE full-measure rest) carries measure_ticks as its fraction. An
    invalid duration (only in degenerate input) is ("", 0, None).
    """
    l = Fraction(l)
    rtick_start = Fraction(rtick_start)
    measure_ticks = Fraction(measure_ticks)
    anacrusis_offset = Fraction(anacrusis_offset)
    nominal = TimeSigFrac(int(nominal_num), int(nominal_den))

    d_list = _to_rhythmic_duration_list(l, bool(is_rest), rtick_start, nominal, measure_ticks,
                                        bool(is_anacrusis), anacrusis_offset, int(max_dots))
    out: List[Tuple[str, int, Optional[Fraction]]] = []
    for d in d_list:
        frac = measure_ticks if d.val == V_MEASURE else d.fraction()
        out.append((DURATION_NAMES[d.val], d.dots, frac))
    return out


if __name__ == "__main__":
    F = Fraction
    # (args, expected) -- each hand-traced through the C++ code
    cases = [
        # 4/4 rest gap 3/4 from beat 1: crosses stressed beat 3 -> split at 1/2
        ((F(3, 4), True, F(0), 4, 4, F(1)),
         [("half", 0, F(1, 2)), ("quarter", 0, F(1, 4))]),
        # 4/4 rest gap 3/4 from beat 2: rests split on stressed beat 3
        ((F(3, 4), True, F(1, 4), 4, 4, F(1)),
         [("quarter", 0, F(1, 4)), ("half", 0, F(1, 2))]),
        # same span as a note with max_dots=1: ends on stressed beat -> dotted half
        ((F(3, 4), False, F(1, 4), 4, 4, F(1), False, F(0), 1),
         [("half", 1, F(3, 4))]),
        # 6/8 rest gap 5/8 from 2nd eighth: split at compound beat 2, rest off-beat
        # part split per eighth; 3/8 needs a dotted quarter, so no dots -> quarter+eighth
        ((F(5, 8), True, F(1, 8), 6, 8, F(3, 4)),
         [("eighth", 0, F(1, 8)), ("eighth", 0, F(1, 8)),
          ("quarter", 0, F(1, 4)), ("eighth", 0, F(1, 8))]),
        ((F(5, 8), True, F(1, 8), 6, 8, F(3, 4), False, F(0), 1),
         [("eighth", 0, F(1, 8)), ("eighth", 0, F(1, 8)), ("quarter", 1, F(3, 8))]),
        # 3/4 rest gap 1/2 from beat 2: rests may not cross a beat in triple meter
        ((F(1, 2), True, F(1, 4), 3, 4, F(3, 4)),
         [("quarter", 0, F(1, 4)), ("quarter", 0, F(1, 4))]),
        # ... but a note may
        ((F(1, 2), False, F(1, 4), 3, 4, F(3, 4), False, F(0), 1),
         [("half", 0, F(1, 2))]),
        # full-measure rest
        ((F(1), True, F(0), 4, 4, F(1)),
         [("measure", 0, F(1))]),
        # 1/4 pickup in 4/4: without the anacrusis flag it is a measure rest ...
        ((F(1, 4), True, F(0), 4, 4, F(1, 4)),
         [("measure", 0, F(1, 4))]),
        # ... with it, start shifts to 3/4 and a quarter rest results
        ((F(1, 4), True, F(0), 4, 4, F(1, 4), True, F(3, 4)),
         [("quarter", 0, F(1, 4))]),
        # 1/12 (160 ticks) is off the binary subbeat grid -> greedy toDurationList fallback
        ((F(1, 12), False, F(0), 4, 4, F(1)),
         [("16th", 0, F(1, 16)), ("64th", 0, F(1, 64)),
          ("256th", 0, F(1, 256)), ("1024th", 0, F(1, 1024))]),
    ]
    failed = 0
    for args, expected in cases:
        got = rhythmic_duration_list(*args)
        ok = got == expected
        failed += not ok
        print(("ok  " if ok else "FAIL"), args, "->", [(n, d, str(f)) for n, d, f in got])
        if not ok:
            print("     expected", [(n, d, str(f)) for n, d, f in expected])
    raise SystemExit(1 if failed else 0)
