"""Where a recording's singer sings, on the score's clock (``recordings vocal-spans``), for ``lyrics import --sung``.

A piano arrangement's right hand plays the tune and much else: fills, an intro, a solo.  The lyrics importer splits its
first voice into phrases at rests, and in an arrangement the rests are rarely where the singer breathes.  When the
score's tempo map follows the recording (``recordings tempo``), the recording says where the singing is: the vocals
stem (Demucs, :mod:`pianovision.separation`) is active where it is loud for itself and not far under the mix.

**The stretches** (:func:`spans_from_levels`): RMS levels of 50 ms blocks of the stem and of the mix; a block sings
when the stem is within ``rel_db`` of its own loud level (its 95th percentile) and no more than ``below_mix_db`` under
the mix; runs of singing blocks closer than ``merge_gap`` join, runs shorter than ``min_seconds`` go.

**On the score** (:func:`to_uticks`): recording seconds minus the offset Note Waterfall plays the recording at are the
song's seconds; those, through the score's tempo map (the formatter's render), its unrolled ticks.  Each stretch is
widened by ``pad_before`` (a note on the beat a singer comes in a little after) and ``pad_after``.
"""

from __future__ import annotations

import bisect
from typing import List, Sequence, Tuple

WINDOW = 0.05
REL_DB = 22.0
BELOW_MIX_DB = 18.0
MERGE_GAP = 0.3
MIN_SECONDS = 0.3
PAD_BEFORE = 0.25
PAD_AFTER = 0.15


def spans_from_levels(rv: Sequence[float], rm: Sequence[float], window: float = WINDOW, rel_db: float = REL_DB,
                      below_mix_db: float = BELOW_MIX_DB, merge_gap: float = MERGE_GAP,
                      min_seconds: float = MIN_SECONDS) -> List[Tuple[float, float]]:
    """The stretches (recording seconds) where the vocals stem sings, from the stem's and the mix's block levels."""
    n = min(len(rv), len(rm))
    if not n:
        return []
    loud = sorted(rv[:n])[int(0.95 * (n - 1))]
    act = [rv[k] > loud - rel_db and rv[k] >= rm[k] - below_mix_db for k in range(n)]
    runs: List[List[float]] = []
    k = 0
    while k < n:
        if not act[k]:
            k += 1
            continue
        s = k
        while k < n and act[k]:
            k += 1
        if runs and s * window - runs[-1][1] <= merge_gap + 1e-9:
            runs[-1][1] = k * window
        else:
            runs.append([s * window, k * window])
    return [(round(a, 3), round(b, 3)) for a, b in runs if b - a >= min_seconds - 1e-9]


def tick_at(seconds, t: float, last_tick: int) -> int:
    """The MIDI tick the render reaches ``t`` song seconds at (``seconds``: tick -> seconds, increasing)."""
    lo, hi = 0, max(0, last_tick)
    if t <= seconds(0):
        return 0
    if t >= seconds(hi):
        return hi
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if seconds(mid) <= t:
            lo = mid
        else:
            hi = mid
    return lo


def to_uticks(spans: Sequence[Tuple[float, float]], offset: float, seconds, utick, last_tick: int,
              pad_before: float = PAD_BEFORE, pad_after: float = PAD_AFTER) -> List[Tuple[int, int]]:
    """The stretches as unrolled ticks of the score, widened by the pads, joined where they now overlap."""
    out: List[List[int]] = []
    for a, b in spans:
        u0 = utick(tick_at(seconds, a - offset - pad_before, last_tick))
        u1 = utick(tick_at(seconds, b - offset + pad_after, last_tick))
        if u1 <= u0:
            continue
        if out and u0 <= out[-1][1]:
            out[-1][1] = max(out[-1][1], u1)
        else:
            out.append([u0, u1])
    return [(a, b) for a, b in out]


def measure_ranges(spans: Sequence[Tuple[int, int]], starts: Sequence[int], labels: Sequence[str]) -> str:
    """The measures (as played: their labels) the stretches touch, as ranges ("m. 4-32, 44-68")."""
    hit: List[int] = []
    for a, b in spans:
        j0 = max(0, bisect.bisect_right(starts, a) - 1)
        j1 = max(0, bisect.bisect_left(starts, b) - 1)
        hit.extend(range(j0, j1 + 1))
    hit = sorted(set(hit))
    out, k = [], 0
    while k < len(hit):
        s = k
        while k + 1 < len(hit) and hit[k + 1] == hit[k] + 1:
            k += 1
        out.append(labels[hit[s]] if s == k else f"{labels[hit[s]]}-{labels[hit[k]]}")
        k += 1
    return "m. " + ", ".join(out) if out else "none"
