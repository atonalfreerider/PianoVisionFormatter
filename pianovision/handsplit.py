"""The hands of a piano part whose staves say nothing about them (``hands: "split"`` in ``scores.json``).

A score imported from MIDI can write both hands of the piano on one staff (with a clef change every measure) and
leave the other nearly empty.  PianoVision's hands are the piano part's staves, so such a score plays everything in
one hand.  :func:`split_hands` gives each note of the part to a hand instead and moves it to that hand's staff of the
render (the first piano staff is the right hand, the second the left), before the notes are read: the song, the
parts file and the accompaniment's view of the score follow.  The score is not changed.

:func:`assign_hands` decides, chord by chord (the notes struck together, low to high), where the split between the
hands lies, with a beam search over the whole piece.  A choice costs:

* reach: a hand spanning more than an octave with the notes it strikes and still holds (more for more than a
  tenth, and for more than five notes);
* crossing: the left hand striking above notes the right hand holds, or the reverse;
* movement: how far each hand's new notes are from where that hand has been playing (a running mean);
* register: right-hand notes below G3, left-hand notes above G4, a little per semitone.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

RH_START, LH_START = 72.0, 48.0       # where the hands are before the first chord
SPAN_EASY, SPAN_HARD = 12, 16         # semitones one hand reaches easily / at most
W_SPAN, P_HARD, P_COUNT = 2.0, 25.0, 25.0
W_CROSS, P_CROSS = 1.0, 3.0
W_MOVE = 0.12
W_REGISTER, RH_LOW, LH_HIGH = 0.12, 55, 67
CENTER_KEEP = 0.5                     # running mean: weight of the old position


def _span_cost(pitches: Sequence[int]) -> float:
    if not pitches:
        return 0.0
    span = max(pitches) - min(pitches)
    c = W_SPAN * max(0, span - SPAN_EASY) + (P_HARD if span > SPAN_HARD else 0.0)
    return c + (P_COUNT if len(pitches) > 5 else 0.0)


def assign_hands(notes: Sequence[Tuple[int, int, int]], beam: int = 16) -> List[int]:
    """0 (right hand) or 1 (left) for each note ``(on, off, pitch)`` (any time unit)."""
    if not notes:
        return []
    order = sorted(range(len(notes)), key=lambda k: (notes[k][0], notes[k][2]))
    groups: List[Tuple[int, List[int]]] = []
    for k in order:
        if groups and groups[-1][0] == notes[k][0]:
            groups[-1][1].append(k)
        else:
            groups.append((notes[k][0], [k]))
    # a state: (cost, rh centre, lh centre, rh held ((off, pitch), ...), lh held, back pointer, split)
    states: List[tuple] = [(0.0, RH_START, LH_START, (), (), -1, 0)]
    history: List[List[tuple]] = []
    for onset, idx in groups:
        pitches = [notes[k][2] for k in idx]
        cand: Dict[tuple, tuple] = {}
        for si, (cost, rc, lc, rh, lh, _b, _s) in enumerate(states):
            rh = tuple(x for x in rh if x[0] > onset)
            lh = tuple(x for x in lh if x[0] > onset)
            rhp, lhp = [p for _o, p in rh], [p for _o, p in lh]
            for split in range(len(pitches) + 1):
                left, right = pitches[:split], pitches[split:]
                c = cost + _span_cost(right + rhp) + _span_cost(left + lhp)
                if left and rhp and max(left) > min(rhp):
                    c += P_CROSS + W_CROSS * (max(left) - min(rhp))
                if right and lhp and min(right) < max(lhp):
                    c += P_CROSS + W_CROSS * (max(lhp) - min(right))
                nrc, nlc = rc, lc
                if right:
                    m = sum(right) / len(right)
                    c += W_MOVE * abs(m - rc) + W_REGISTER * sum(max(0, RH_LOW - p) for p in right)
                    nrc = CENTER_KEEP * rc + (1 - CENTER_KEEP) * m
                if left:
                    m = sum(left) / len(left)
                    c += W_MOVE * abs(m - lc) + W_REGISTER * sum(max(0, p - LH_HIGH) for p in left)
                    nlc = CENTER_KEEP * lc + (1 - CENTER_KEEP) * m
                nrh = rh + tuple((notes[k][1], notes[k][2]) for k in idx[split:])
                nlh = lh + tuple((notes[k][1], notes[k][2]) for k in idx[:split])
                key = (round(nrc), round(nlc), tuple(sorted(p for _o, p in nrh)), tuple(sorted(p for _o, p in nlh)))
                st = (c, nrc, nlc, nrh, nlh, si, split)
                if key not in cand or c < cand[key][0]:
                    cand[key] = st
        states = sorted(cand.values(), key=lambda s: s[0])[:beam]
        history.append(states)
    hands = [0] * len(notes)
    si = 0                                   # the cheapest final state
    for g in range(len(groups) - 1, -1, -1):
        st = history[g][si]
        split = st[6]
        for x, k in enumerate(groups[g][1]):
            hands[k] = 1 if x < split else 0
        si = st[5]
    return hands


def split_hands(midi, tracks: Sequence[int]) -> int:
    """Give every note of the piano ``tracks`` (MIDI tracks = staves; the first is the right hand, the second the left)
    to a hand (:func:`assign_hands`) and move it, note-on and note-off, to that hand's track; every other message stays
    where it is.  Messages at one tick: the others first, then note-offs, then note-ons.  Returns the number of notes
    moved."""
    tracks = [t for t in tracks if 0 <= t < len(midi.tracks)]
    if len(tracks) < 2:
        return 0
    right, left = tracks[0], tracks[1]
    keep: Dict[int, List[Tuple[int, int, object]]] = {t: [] for t in tracks}     # (tick, seq, msg)
    ends: Dict[int, int] = {}
    pairs: List[Tuple[int, int, int, int, object, object]] = []                 # (on, off, pitch, track, on, off)
    seq = 0
    for t in tracks:
        tick = 0
        active: Dict[Tuple[int, int], List[Tuple[int, object, int]]] = {}
        for msg in midi.tracks[t]:
            tick += msg.time
            seq += 1
            if msg.type == "end_of_track":
                ends[t] = tick
                continue
            if msg.type == "note_on" and msg.velocity > 0:
                active.setdefault((msg.channel, msg.note), []).append((tick, msg, seq))
                continue
            if msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                ons = active.get((msg.channel, msg.note))
                if ons:
                    t0, m0, s0 = ons.pop(0)
                    pairs.append((t0, tick, msg.note, t, (t0, s0, m0), (tick, seq, msg)))
                    continue
            keep[t].append((tick, seq, msg))
        for ons in active.values():                      # never released: they stay
            for t0, m0, s0 in ons:
                keep[t].append((t0, s0, m0))
    hands = assign_hands([(p[0], p[1], p[2]) for p in pairs])
    moved = 0
    for p, h in zip(pairs, hands):
        dest = right if h == 0 else left
        moved += dest != p[3]
        keep[dest].append(p[4])
        keep[dest].append(p[5])

    def rank(msg) -> int:
        if msg.type == "note_on" and msg.velocity > 0:
            return 2
        if msg.type in ("note_on", "note_off"):
            return 1
        return 0

    for t in tracks:
        items = sorted(keep[t], key=lambda x: (x[0], rank(x[2]), x[1]))
        out = []
        now = 0
        for tick, _s, msg in items:
            msg.time = tick - now
            now = tick
            out.append(msg)
        end = midi.tracks[t][-1] if midi.tracks[t] and midi.tracks[t][-1].type == "end_of_track" else None
        if end is not None:                              # the track ends where it ended, or after its last note
            end.time = max(ends.get(t, 0), now + 1) - now
            out.append(end)
        midi.tracks[t] = out
    return moved


def apply_piano_options(midi, options: Optional[dict]) -> Optional[dict]:
    """The render options' piano part (``piano_staves``, ``hands``; :mod:`pianovision.buildopts`) applied to a render:
    ``midi.piano_staves`` (0-based) for the staff classification (:func:`pianovision.pvjson.classify_tracks`), and the
    hands split when asked.  Returns what was done (None: nothing)."""
    if not options or not options.get("piano_staves"):
        return None
    staves = [int(s) - 1 for s in options["piano_staves"] if 0 < int(s) <= len(midi.tracks)]
    if not staves:
        return None
    midi.piano_staves = staves
    out = {"piano_staves": [s + 1 for s in staves]}
    if options.get("hands") == "split":
        out["moved"] = split_hands(midi, staves)
    return out
