"""libstdc++ ``std::sort`` (introsort), reproduced so that elements comparing
equal end up in the same order as in MuseScore's Linux builds.

``std::sort`` is not stable; MuseScore sorts note-event lists with it, so the
relative order of simultaneous events depends on this exact algorithm.
"""

from __future__ import annotations

from typing import Callable, List, TypeVar

T = TypeVar("T")
_THRESHOLD = 16


def std_sort(a: List[T], less: Callable[[T, T], bool]) -> None:
    n = len(a)
    if n > 1:
        _introsort_loop(a, 0, n, 2 * (n.bit_length() - 1), less)
        _final_insertion_sort(a, 0, n, less)


def _introsort_loop(a, first, last, depth, less) -> None:
    while last - first > _THRESHOLD:
        if depth == 0:
            _partial_sort(a, first, last, last, less)
            return
        depth -= 1
        cut = _unguarded_partition_pivot(a, first, last, less)
        _introsort_loop(a, cut, last, depth, less)
        last = cut


def _move_median_to_first(a, result, i, j, k, less) -> None:
    if less(a[i], a[j]):
        if less(a[j], a[k]):
            a[result], a[j] = a[j], a[result]
        elif less(a[i], a[k]):
            a[result], a[k] = a[k], a[result]
        else:
            a[result], a[i] = a[i], a[result]
    elif less(a[i], a[k]):
        a[result], a[i] = a[i], a[result]
    elif less(a[j], a[k]):
        a[result], a[k] = a[k], a[result]
    else:
        a[result], a[j] = a[j], a[result]


def _unguarded_partition(a, first, last, pivot, less) -> int:
    while True:
        while less(a[first], a[pivot]):
            first += 1
        last -= 1
        while less(a[pivot], a[last]):
            last -= 1
        if not first < last:
            return first
        a[first], a[last] = a[last], a[first]
        first += 1


def _unguarded_partition_pivot(a, first, last, less) -> int:
    mid = first + (last - first) // 2
    _move_median_to_first(a, first, first + 1, mid, last - 1, less)
    return _unguarded_partition(a, first + 1, last, first, less)


def _unguarded_linear_insert(a, last, less) -> None:
    val = a[last]
    nxt = last - 1
    while less(val, a[nxt]):
        a[last] = a[nxt]
        last = nxt
        nxt -= 1
    a[last] = val


def _insertion_sort(a, first, last, less) -> None:
    if first == last:
        return
    for i in range(first + 1, last):
        if less(a[i], a[first]):
            val = a[i]
            a[first + 1:i + 1] = a[first:i]
            a[first] = val
        else:
            _unguarded_linear_insert(a, i, less)


def _final_insertion_sort(a, first, last, less) -> None:
    if last - first > _THRESHOLD:
        _insertion_sort(a, first, first + _THRESHOLD, less)
        for i in range(first + _THRESHOLD, last):
            _unguarded_linear_insert(a, i, less)
    else:
        _insertion_sort(a, first, last, less)


# --- heap helpers (used only when the recursion depth limit is hit) ---

def _adjust_heap(a, first, hole, length, value, less) -> None:
    top = hole
    child = hole
    while child < (length - 1) // 2:
        child = 2 * (child + 1)
        if less(a[first + child], a[first + child - 1]):
            child -= 1
        a[first + hole] = a[first + child]
        hole = child
    if (length & 1) == 0 and child == (length - 2) // 2:
        child = 2 * (child + 1)
        a[first + hole] = a[first + child - 1]
        hole = child - 1
    parent = (hole - 1) // 2
    while hole > top and less(a[first + parent], value):
        a[first + hole] = a[first + parent]
        hole = parent
        parent = (hole - 1) // 2
    a[first + hole] = value


def _make_heap(a, first, last, less) -> None:
    length = last - first
    if length < 2:
        return
    parent = (length - 2) // 2
    while True:
        _adjust_heap(a, first, parent, length, a[first + parent], less)
        if parent == 0:
            return
        parent -= 1


def _pop_heap(a, first, last, result, less) -> None:
    value = a[result]
    a[result] = a[first]
    _adjust_heap(a, first, 0, last - first, value, less)


def _partial_sort(a, first, middle, last, less) -> None:
    _make_heap(a, first, middle, less)
    for i in range(middle, last):
        if less(a[i], a[first]):
            _pop_heap(a, first, middle, i, less)
    while middle - first > 1:
        middle -= 1
        _pop_heap(a, first, middle, middle, less)
