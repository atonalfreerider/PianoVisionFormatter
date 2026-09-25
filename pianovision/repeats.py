"""Playback order of measures: repeats, voltas, D.C./D.S./coda jumps.

A literal port of MuseScore's ``RepeatList::unwind`` (dom/repeatlist.cpp).
"""

from __future__ import annotations

from typing import List, Optional

from .score import Element, Measure, Score, Spanner, ticks

SECTION_BREAK, VOLTA_START, VOLTA_END, REPEAT_START, REPEAT_END, JUMP, MARKER = range(7)


class RepeatSegment:
    def __init__(self, playback_count: int):
        self.tick = 0
        self.utick = 0
        self.utime = 0.0
        self.time_offset = 0.0
        self.pause = 0.0
        self.playback_count = playback_count
        self.measures: List[Measure] = []

    def add_measure(self, m: Optional[Measure]) -> None:
        if m is None:
            return
        if not self.measures:
            self.tick = ticks(m.tick)
        if not self.measures or self.measures[-1] is not m:
            self.measures.append(m)

    def add_measures(self, m: Measure, score: Score) -> None:
        if self.measures:
            last = _next_measure(score, self.measures[-1])
            if last is not None and last.tick < m.tick:
                while last is not m:
                    self.measures.append(last)
                    last = _next_measure(score, last)
            while self.measures and self.measures[-1].tick >= m.tick:
                self.measures.pop()
        self.add_measure(m)

    def is_empty(self) -> bool:
        return not self.measures

    def len(self) -> int:
        return 0 if not self.measures else ticks(self.measures[-1].end_tick) - self.tick

    @property
    def end_tick(self) -> int:
        return self.tick + self.len()

    def pop_measure(self) -> None:
        if self.measures:
            self.measures.pop()

    @property
    def first_measure(self) -> Measure:
        return self.measures[0]

    @property
    def last_measure(self) -> Measure:
        return self.measures[-1]


def _next_measure(score: Score, m: Measure) -> Optional[Measure]:
    i = m.index + 1
    return score.measures[i] if i < len(score.measures) else None


def _prev_measure(score: Score, m: Measure) -> Optional[Measure]:
    i = m.index - 1
    return score.measures[i] if i >= 0 else None


class Volta:
    """Working copy of a volta spanner (cloned so overlaps can be split)."""

    def __init__(self, sp: Spanner, start: Measure, end: Measure, endings: List[int], end_hook_none: bool):
        self.sp = sp
        self.start = start
        self.end = end
        self.endings = list(endings)
        self.end_hook_none = end_hook_none

    def clone(self) -> "Volta":
        return Volta(self.sp, self.start, self.end, self.endings, self.end_hook_none)

    def has_ending(self, n: int) -> bool:
        return n in self.endings

    def first_ending(self) -> int:
        return self.endings[0] if self.endings else 0

    def last_ending(self) -> int:
        return self.endings[-1] if self.endings else 0


class RLE:
    __slots__ = ("type", "element", "measure", "repeat_count")

    def __init__(self, rtype: int, element, measure: Measure):
        self.type = rtype
        self.element = element
        self.measure = measure
        self.repeat_count = 1 if rtype == REPEAT_START else 0


def volta_endings(sp: Spanner) -> List[int]:
    text = sp.props.get("endings", "")
    out = []
    for part in str(text).replace(",", " ").split():
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


def volta_measures(score: Score, sp: Spanner):
    start = score.tick2measure(sp.tick)
    # the end measure of a volta is the measure containing tick2 - 1
    end_tick = sp.tick2
    end = None
    for m in score.measures:
        if m.tick < end_tick <= m.end_tick:
            end = m
            break
    return start, end


def jump_attr(j: Element, name: str) -> str:
    v = j.props.get(name, "")
    return v if isinstance(v, str) else ""


def jump_play_repeats(j: Element) -> bool:
    v = j.props.get("playRepeats")
    return str(v).strip() in ("1", "true")


def marker_label(mk: Element) -> str:
    return str(mk.props.get("label", ""))


RIGHT_MARKERS = {"fine", "toCoda", "toCodaSym", "da_coda", "da_double_coda", "user"}


def marker_is_right(mk: Element) -> bool:
    sub = str(mk.props.get("subtype", ""))
    return sub in ("fine", "toCoda", "toCodaSym", "da_coda", "da_double_coda") or \
        str(mk.props.get("placement", "")) == "right"


class RepeatList(list):
    def __init__(self, score: Score):
        super().__init__()
        self.score = score
        self.rl_elements: List[List[RLE]] = []
        self.jumps_taken = set()

    # --------------------------------------------------------------
    def collect(self) -> None:
        score = self.score
        section: List[RLE] = []
        mbs = score.measure_bases
        first = score.measures[0]
        start_from = RLE(REPEAT_START, first, first)
        section.append(start_from)
        section_end = None

        pre = []
        for sp in score.spanners:
            if sp.tag != "Volta" or not _play_spanner(sp):
                continue
            sm, em = volta_measures(score, sp)
            if sm is None or em is None:
                continue
            hook = str(sp.props.get("endHookType", "0"))
            volta = Volta(sp, sm, em, volta_endings(sp), hook == "0")
            if not pre:
                pre.append(volta)
                continue
            to_merge = []
            start_tick = volta.start.tick
            while pre and start_tick <= pre[-1].end.tick:
                to_merge.append(pre.pop())
            while to_merge:
                remainder = to_merge[-1].clone()
                if volta.start is not remainder.start:
                    to_merge[-1].end = _prev_measure(score, volta.start)
                    remainder.start = volta.start
                    pre.append(to_merge[-1])
                to_merge.pop()
                if volta.end.tick < remainder.end.tick:
                    volta, remainder = remainder, volta
                remainder.endings = [e for e in remainder.endings if volta.has_ending(e)]
                pre.append(remainder)
                if volta.end is not remainder.end:
                    volta.start = _next_measure(score, remainder.end)
                else:
                    pre.extend(to_merge)
                    to_merge = []
                    volta = None
                    break
            if volta is not None:
                pre.append(volta)

        volta = None
        i = mbs.index(first)
        while i < len(mbs):
            mb = mbs[i]
            if mb.is_measure:
                m: Measure = mb
                section_end = m
                if pre and pre[0].start is m:
                    if volta is not None:
                        section.append(RLE(VOLTA_END, volta, _prev_measure(score, m)))
                    volta = pre.pop(0)
                    section.append(RLE(VOLTA_START, volta, m))
                if m.repeat_start:
                    if volta is not None and volta.start is not m:
                        section.append(RLE(VOLTA_END, volta, _prev_measure(score, m)))
                        volta = None
                    start_from = RLE(REPEAT_START, m, m)
                    section.append(start_from)
                for e in _measure_elements(m):
                    if e.tag == "Jump":
                        section.append(RLE(JUMP, e, m))
                        if volta is not None:
                            if volta.end.tick < m.tick or (volta.end.tick == m.tick and volta.end_hook_none):
                                if not m.repeat_end:
                                    section.append(RLE(VOLTA_END, volta, m))
                                    volta = None
                    elif e.tag == "Marker":
                        rle = RLE(MARKER, e, m)
                        is_right = marker_is_right(e)
                        pos = len(section) - 1
                        while section[pos].measure is m:
                            go_before = False
                            if section[pos].type == MARKER and not is_right:
                                go_before = marker_is_right(section[pos].element)
                            if go_before or section[pos].type == JUMP:
                                pos -= 1
                            else:
                                break
                        section.insert(pos + 1, rle)
                if m.repeat_end:
                    section.append(RLE(REPEAT_END, m, m))
                    if start_from is not None:
                        start_from.repeat_count += m.repeat_count - 1
                    if volta is not None:
                        section.append(RLE(VOLTA_END, volta, m))
                        volta = None
                if volta is not None and volta.end.tick == m.tick and not volta.end_hook_none:
                    section.append(RLE(VOLTA_END, volta, m))
                    volta = None
            next_m = _next_measure_base_measure(mbs, i)
            if mb.section_break is not None or next_m is None:
                if section_end is not None:
                    if volta is not None:
                        section.append(RLE(VOLTA_END, volta, section_end))
                        volta = None
                    section.append(RLE(SECTION_BREAK, mb, section_end))
                    section_end = None
                    self.rl_elements.append(section)
                if next_m is not None:
                    section = []
                    start_from = RLE(REPEAT_START, next_m, next_m)
                    section.append(start_from)
                    i = mbs.index(next_m)
                    continue
                break
            i += 1

    # --------------------------------------------------------------
    def find_marker(self, label: str, sec_i: int, el_i: int):
        secs = self.rl_elements
        if label == "start":
            return sec_i, 0
        if label == "end":
            return sec_i, len(secs[sec_i]) - 1
        sec = secs[sec_i]
        j = el_i
        while j > 0:
            j -= 1
            if sec[j].type == MARKER and marker_label(sec[j].element) == label:
                return sec_i, j
        j = el_i + 1
        while j < len(sec):
            if sec[j].type == MARKER and marker_label(sec[j].element) == label:
                return sec_i, j
            j += 1
        s = sec_i
        while s > 0:
            s -= 1
            j = len(secs[s])
            while j > 0:
                j -= 1
                if secs[s][j].type == MARKER and marker_label(secs[s][j].element) == label:
                    return s, j
        s = sec_i + 1
        while s < len(secs):
            for j, rle in enumerate(secs[s]):
                if rle.type == MARKER and marker_label(rle.element) == label:
                    return s, j
            s += 1
        return None, None

    def perform_jump(self, sec_i: int, target_i: int, with_repeats: bool):
        sec = self.rl_elements[sec_i]
        active_volta = None
        start_ref = sec[0]
        for j in range(target_i):
            rle = sec[j]
            if rle.type == VOLTA_START:
                active_volta = rle.element
            elif rle.type == VOLTA_END:
                active_volta = None
            elif rle.type == REPEAT_START:
                start_ref = rle
        if with_repeats:
            if active_volta is not None:
                pc = active_volta.first_ending() or 1
            else:
                pc = 1
        else:
            if active_volta is not None:
                pc = active_volta.last_ending() or start_ref.repeat_count
            else:
                pc = start_ref.repeat_count
        return pc, active_volta, start_ref

    # --------------------------------------------------------------
    def unwind(self) -> None:
        self.clear()
        self.jumps_taken = set()
        if not self.score.measures:
            return
        self.collect()
        score = self.score
        secs = self.rl_elements
        rs = None
        active_jump = None
        play_until = (None, None)
        continue_at = (None, None)

        sec_i = 0
        while sec_i < len(secs):
            sec = secs[sec_i]
            playback_count = 1
            el_i = 0
            start_ref = sec[0]
            active_volta = None
            play_until = (None, None)
            continue_at = (None, None)
            force_final = False
            rs = RepeatSegment(playback_count)
            rs.add_measure(sec[0].measure)
            el_i = 1
            while el_i < len(sec):
                rle = sec[el_i]
                if rs is not None:
                    rs.add_measures(rle.measure, score)
                t = rle.type
                if t == SECTION_BREAK:
                    if rs is not None and not rs.is_empty():
                        self.append(rs)
                elif t == VOLTA_START:
                    active_volta = rle.element
                    if not active_volta.has_ending(playback_count):
                        rs.pop_measure()
                        if not rs.is_empty():
                            self.append(rs)
                        el_i += 1
                        while sec[el_i].type != VOLTA_END:
                            el_i += 1
                        active_volta = None
                        nxt = _next_measure(score, sec[el_i].measure)
                        if nxt is None:
                            rs = None
                        else:
                            rs = RepeatSegment(playback_count)
                            rs.add_measure(nxt)
                elif t == VOLTA_END:
                    active_volta = None
                elif t == REPEAT_START:
                    if rs is None:
                        rs = RepeatSegment(playback_count)
                        rs.add_measure(rle.measure)
                    else:
                        desired = rle.repeat_count if force_final else 1
                        if rs.playback_count != desired:
                            rs.pop_measure()
                            if not rs.is_empty():
                                self.append(rs)
                            playback_count = desired
                            rs = RepeatSegment(playback_count)
                            rs.add_measure(rle.measure)
                        start_ref = rle
                elif t == REPEAT_END:
                    rle.repeat_count += 1
                    if playback_count < start_ref.repeat_count and rle.repeat_count < rle.measure.repeat_count:
                        if rs is not None and not rs.is_empty():
                            self.append(rs)
                        rs = None
                        while True:
                            el_i -= 1
                            if sec[el_i].type == VOLTA_START:
                                active_volta = None
                            elif sec[el_i].type == VOLTA_END:
                                active_volta = sec[el_i].element
                            if sec[el_i] is start_ref:
                                break
                        playback_count += 1
                        continue
                elif t == JUMP:
                    jump = rle.element
                    if active_jump is jump:
                        force_final = False
                    if playback_count >= start_ref.repeat_count or (
                            active_volta is not None and playback_count == active_volta.last_ending()):
                        occ = (id(jump), playback_count)
                        if occ not in self.jumps_taken:
                            self.jumps_taken.add(occ)
                            jt = self.find_marker(jump_attr(jump, "jumpTo"), sec_i, el_i)
                            play_until = self.find_marker(jump_attr(jump, "playUntil"), sec_i, el_i)
                            continue_at = self.find_marker(jump_attr(jump, "continueAt"), sec_i, el_i)
                            if jt[0] is not None:
                                self.append(rs)
                                rs = None
                                active_jump = jump
                                playback_count, active_volta, start_ref = self.perform_jump(
                                    jt[0], jt[1], jump_play_repeats(jump))
                                sec_i, el_i = jt
                                sec = secs[sec_i]
                                force_final = not jump_play_repeats(jump)
                                if play_until[0] is not None:
                                    for k in range(el_i + 1, len(sec)):
                                        r = sec[k]
                                        if r.type == REPEAT_END and r.repeat_count != 0:
                                            r.repeat_count = 0
                                            if force_final:
                                                r.repeat_count = r.measure.repeat_count
                                if active_volta is not None and playback_count < start_ref.repeat_count:
                                    k = el_i
                                    while True:
                                        k -= 1
                                        if sec[k] is start_ref:
                                            break
                                    k += 1
                                    volta_ref = None
                                    processed = 1
                                    while k < len(sec) and sec[k].type != REPEAT_START and \
                                            processed < start_ref.repeat_count:
                                        r = sec[k]
                                        if r.type == VOLTA_START:
                                            volta_ref = r.element
                                            if volta_ref.last_ending() < playback_count:
                                                volta_ref = None
                                        elif r.type == REPEAT_END:
                                            if volta_ref is not None:
                                                remaining = sum(1 for e in volta_ref.endings if e >= playback_count)
                                                r.repeat_count = r.measure.repeat_count - remaining - 1
                                            processed += r.measure.repeat_count - 1
                                        k += 1
                                rs = RepeatSegment(playback_count)
                                rs.add_measure(sec[el_i].measure)
                elif t == MARKER:
                    if (sec_i, el_i) == play_until and (
                            playback_count == start_ref.repeat_count or
                            (active_volta is not None and playback_count == active_volta.last_ending())):
                        self.append(rs)
                        rs = None
                        play_until = (None, None)
                        force_final = False
                        if continue_at[0] is not None:
                            playback_count, active_volta, start_ref = self.perform_jump(
                                continue_at[0], continue_at[1], True)
                            sec_i, el_i = continue_at
                            sec = secs[sec_i]
                            if start_ref.measure is not sec[el_i].measure:
                                start_ref = sec[el_i]
                            rs = RepeatSegment(playback_count)
                            rs.add_measure(sec[el_i].measure)
                            continue_at = (None, None)
                        else:
                            el_i = len(sec)
                            continue
                el_i += 1
            if len(self) > 0:
                last = self[-1]
                sb = sec[-1].element
                if getattr(sb, "section_break", None) is not None:
                    last.pause = float(sb.section_break.props.get("pause", 0.0) or 0.0)
            sec_i += 1
        # None entries can be appended by jumps taken with no active segment
        self[:] = [r for r in self if r is not None]
        self.update_tempo()

    def update_tempo(self) -> None:
        utick = 0
        for s in self:
            s.utick = utick
            utick += s.len()


def _play_spanner(sp: Spanner) -> bool:
    v = sp.props.get("play")
    return v is None or str(v).strip() not in ("0", "false")


def _measure_elements(m: Measure):
    els = list(m.jumps) + list(m.markers)
    els.sort(key=lambda e: e.props.get("_order", 0))
    return els


def _next_measure_base_measure(mbs, i: int):
    for k in range(i + 1, len(mbs)):
        if mbs[k].is_measure:
            return mbs[k]
    return None


def build_repeat_list(score: Score, expand: bool = True) -> RepeatList:
    rl = RepeatList(score)
    if expand:
        rl.unwind()
    else:
        rs = RepeatSegment(1)
        for m in score.measures:
            rs.add_measure(m)
        rl.append(rs)
        rl.update_tempo()
    return rl
