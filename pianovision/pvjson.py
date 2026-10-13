"""Build the PianoVision song JSON from MIDI-equivalent data.

The input is a :class:`pianovision.smf.MidiFile` -- either rendered directly
from a MuseScore score (the normal path) or read from a ``.mid`` file.

This is a faithful port of the legacy ``midi_to_json.py`` output stage.  The
arithmetic and ordering are kept exactly as they were so that every song
already on the headset stays byte-for-byte identical.  Known oddities that
are preserved on purpose are marked ``LEGACY``.
"""

from __future__ import annotations

import bisect
import dataclasses
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .metadata import extract_accented_notes, extract_merge_markers, title_artist
from .smf import MidiFile, merged_messages_seconds


@dataclass
class Note:
    midi: int
    time: float
    velocity: float
    duration: float
    ticks: int
    duration_ticks: int
    staff: int
    group: int
    accent: int = 0
    # provenance (build_song(provenance=...)): the score Note played and the note_on velocity
    src: Any = field(default=None, repr=False, compare=False)
    raw_velocity: int = field(default=0, repr=False, compare=False)
    track: int = field(default=-1, repr=False, compare=False)     # MIDI track = content staff of the score


@dataclass
class Track:
    notes: List[Note]
    myInstrument: int = -5
    theirInstrument: int = 0


_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def get_note_name(midi_note: int) -> str:
    return f"{_NOTE_NAMES[midi_note % 12]}{(midi_note // 12) - 1}"


def get_note_length_type(duration_ticks: int) -> str:
    if duration_ticks >= 360:
        return "quarter"
    if duration_ticks >= 180:
        return "eighth"
    return "dottedsixteenth"


def _rest_type(duration: float) -> str:
    return "dottedquarter" if duration > 0.75 else ("dottedeighth" if duration > 0.375 else "dottedsixteenth")


def calculate_rests(start_time: float, end_time: float, notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rests = []
    current_time = start_time
    sorted_notes = sorted(notes, key=lambda x: x["start"])
    if not notes or sorted_notes[0]["start"] > start_time + 0.001:
        rests.append({"time": start_time, "noteLengthType": _rest_type(end_time - start_time)})
    for i in range(len(sorted_notes)):
        note = sorted_notes[i]
        if note["start"] > current_time + 0.001:
            rests.append({"time": current_time, "noteLengthType": _rest_type(note["start"] - current_time)})
        current_time = note["end"]
        if i < len(sorted_notes) - 1 and sorted_notes[i + 1]["start"] > current_time + 0.001:
            rests.append({"time": current_time,
                          "noteLengthType": _rest_type(sorted_notes[i + 1]["start"] - current_time)})
    if current_time < end_time - 0.001:
        rests.append({"time": current_time, "noteLengthType": _rest_type(end_time - current_time)})
    return rests


def ticks_to_seconds(ticks: int, tempos: List[Dict[str, Any]], ticks_per_beat: int) -> float:
    if not tempos:
        return (ticks * 500000) / (ticks_per_beat * 1000000)
    last_tempo = tempos[0]
    for tempo in tempos:
        if tempo["ticks"] > ticks:
            break
        last_tempo = tempo
    return _seconds_from(last_tempo, ticks, ticks_per_beat)


def _seconds_from(last_tempo: Dict[str, Any], ticks: int, ticks_per_beat: int) -> float:
    if ticks == last_tempo["ticks"]:
        return last_tempo["time"]
    delta_ticks = ticks - last_tempo["ticks"]
    microseconds_per_beat = 60000000 / last_tempo["bpm"]
    delta_time = (delta_ticks * microseconds_per_beat) / (ticks_per_beat * 1000000)
    return last_tempo["time"] + delta_time


class TempoIndex:
    """Same result as :func:`ticks_to_seconds`, with a bisect lookup."""

    def __init__(self, tempos: List[Dict[str, Any]], ticks_per_beat: int):
        self.tempos = tempos
        self.ticks = [t["ticks"] for t in tempos]
        self.tpb = ticks_per_beat

    def __call__(self, ticks: int) -> float:
        if not self.tempos:
            return (ticks * 500000) / (self.tpb * 1000000)
        i = bisect.bisect_right(self.ticks, ticks) - 1
        return _seconds_from(self.tempos[max(i, 0)], ticks, self.tpb)


# --------------------------------------------------------------------------
# global maps
# --------------------------------------------------------------------------

def extract_tempo_events(mid: MidiFile) -> List[Dict[str, Any]]:
    events = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == 'set_tempo':
                events.append({'tick': abs_tick, 'tempo': msg.tempo})
    events.sort(key=lambda x: x['tick'])
    if not events or events[0]['tick'] > 0:
        events.insert(0, {'tick': 0, 'tempo': 500000})
    formatted = []
    last_time = 0.0
    for i, event in enumerate(events):
        if i > 0:
            prev = events[i - 1]
            delta_ticks = event['tick'] - prev['tick']
            last_time += (delta_ticks * prev['tempo']) / (mid.ticks_per_beat * 1000000)
        formatted.append({"bpm": 60000000 / event['tempo'], "ticks": event['tick'], "time": last_time})
    return formatted


def extract_time_signatures(mid: MidiFile) -> List[Dict[str, Any]]:
    sigs = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == 'time_signature':
                sigs.append({"ticks": abs_tick, "timeSignature": [msg.numerator, msg.denominator],
                             "measures": len(sigs)})
    if not sigs:
        sigs.append({"ticks": 0, "timeSignature": [4, 4], "measures": 0})
    sigs.sort(key=lambda x: x["ticks"])
    return sigs


def extract_key_signatures(mid: MidiFile) -> List[Dict[str, Any]]:
    # LEGACY: "ticks" is elapsed *seconds* times ticks_per_beat, not a tick position.
    keys = []
    current_time = 0
    for msg, secs in merged_messages_seconds(mid):
        current_time += secs
        if msg.type == 'key_signature':
            if len(msg.key) > 1 and msg.key.endswith('m'):
                key, scale = msg.key[:-1], "minor"
            else:
                key, scale = msg.key, "major"
            keys.append({"key": key, "scale": scale, "ticks": int(current_time * mid.ticks_per_beat)})
    if not keys:
        keys.append({"key": "C", "scale": "major", "ticks": 0})
    return keys


def _total_ticks(mid: MidiFile) -> int:
    total = 0
    for track in mid.tracks:
        total = max(total, sum(msg.time for msg in track))
    return total


def calculate_measure_map(mid: MidiFile) -> List[Dict[str, Any]]:
    sigs = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == 'time_signature':
                sigs.append({'tick': abs_tick, 'numerator': msg.numerator, 'denominator': msg.denominator})
    sigs.sort(key=lambda x: x['tick'])
    if not sigs:
        sigs.append({'tick': 0, 'numerator': 4, 'denominator': 4})

    measures = []
    current_tick = 0
    current_measure = 1
    idx = 0
    max_tick = _total_ticks(mid)
    while current_tick < max_tick:
        while idx + 1 < len(sigs) and sigs[idx + 1]['tick'] <= current_tick:
            idx += 1
        sig = sigs[idx]
        length = mid.ticks_per_beat * 4 * sig['numerator'] // sig['denominator']
        measures.append({'measure_num': current_measure, 'start_tick': current_tick,
                         'end_tick': current_tick + length,
                         'time_signature': (sig['numerator'], sig['denominator'])})
        current_tick += length
        current_measure += 1
    return measures


def _measure_lookup(measure_map: List[Dict[str, Any]], ticks_per_beat: int):
    """Return get_measure_for_tick(tick) -> 1-based measure number."""
    starts = [m['start_tick'] for m in measure_map]

    def get_measure_for_tick(tick_pos: int) -> int:
        # measures are contiguous, so a bisect is equivalent to the linear scan
        lo, hi = 0, len(starts)
        while lo < hi:
            mid_ = (lo + hi) // 2
            if starts[mid_] <= tick_pos:
                lo = mid_ + 1
            else:
                hi = mid_
        i = lo - 1
        if i >= 0 and tick_pos < measure_map[i]['end_tick']:
            return measure_map[i]['measure_num']
        if measure_map and tick_pos >= measure_map[-1]['end_tick']:
            last = measure_map[-1]
            length = last['end_tick'] - last['start_tick']
            if length > 0:
                return last['measure_num'] + (tick_pos - last['end_tick']) // length + 1
            return last['measure_num']
        default_length = ticks_per_beat * 4
        return (tick_pos // default_length) + 1 if default_length > 0 else 1

    return get_measure_for_tick


# --------------------------------------------------------------------------
# instrument classification (orchestra_utils)
# --------------------------------------------------------------------------

def is_valid_orchestra_instrument(program: int) -> bool:
    if 112 <= program <= 119:
        return False
    return any(lo <= program <= hi for lo, hi in ((0, 7), (40, 51), (64, 79), (80, 95)))


TRACK_PRIMARY, TRACK_SIMPLIFIED, TRACK_ORCHESTRAL = 1, 2, 3
VOICE_PROGRAMS = (52, 53, 54)        # General MIDI choir aahs, voice oohs, synth voice
# Revision of the staff classification (:func:`staff_roles`): 1 the legacy rules (MIDI track names and programs),
# 2 with the score's parts, drumsets, voices and lyrics.  The library re-checks the outputs made before it.
STAVES_REVISION = 2


@dataclass(frozen=True)
class StaffFacts:
    """What the score says about one of its staves (= the render's MIDI track of that index), for
    :func:`staff_roles`.  The renderer sets ``MidiFile.staves``; a ``.mid`` read from disk has none."""
    part: int            # the part's index in the score
    index: int           # the staff's place in its part, 0 = top
    staves: int          # the part's number of staves
    instrument: str      # MuseScore instrument id ("keyboard.piano", "voice.soprano", "drum.group.set", ...)
    program: int         # General MIDI program of the instrument's main channel
    drums: bool          # a drumset: unpitched percussion, played on MIDI channel 10
    sung: bool           # some chord of the staff carries a lyric

    @property
    def voice(self) -> bool:
        return self.instrument.startswith("voice.") or self.program in VOICE_PROGRAMS


@dataclass
class StaffRoles:
    """What every staff of a render is (:func:`staff_roles`); tracks are MIDI track indices = content staves."""
    piano: List[Tuple[int, int]]           # (track, TRACK_*): 'piano', 'piano-simplified', 'piano-orchestral'
    orchestra: List[Tuple[int, int]]       # (track, program): the instruments PianoVision's orchestra mode takes
    vocal: List[Tuple[int, int]] = field(default_factory=list)    # (track, program): sung lines
    hands: Dict[int, int] = field(default_factory=dict)           # piano track -> 0 right hand, 1 left


# --------------------------------------------------------------------------
# notes
# --------------------------------------------------------------------------

def _legacy_piano_type(track) -> int:
    """TRACK_* of a piano track by the legacy rules (its name, or a piano program), 0 when it is not one."""
    found_piano = False
    track_type = TRACK_PRIMARY
    for msg in track:
        if msg.type == 'track_name':
            name = msg.name.lower()
            if 'piano-simplified' in name:
                track_type, found_piano = TRACK_SIMPLIFIED, True
            elif 'piano-orchestral' in name:
                track_type, found_piano = TRACK_ORCHESTRAL, True
            elif 'piano' in name:
                track_type, found_piano = TRACK_PRIMARY, True
        elif msg.type == 'program_change' and 0 <= msg.program <= 7:
            found_piano = True
    return track_type if found_piano else 0


def _orchestra_program(track) -> Optional[int]:
    for msg in track:
        if msg.type == 'program_change' and is_valid_orchestra_instrument(msg.program):
            return msg.program
    return None


def _forced_staves(mid: MidiFile) -> List[int]:
    """The piano staves the score's build options name (``MidiFile.piano_staves``, 0-based, right hand first;
    :mod:`pianovision.buildopts`), or []."""
    return list(getattr(mid, "piano_staves", None) or [])


def _legacy_roles(mid: MidiFile) -> StaffRoles:
    """The legacy rules (``midi_to_json.py``), from the MIDI tracks' names and programs alone; the piano staves
    the build options name replace the piano part, every other staff with an orchestra program is orchestra."""
    piano_tracks: List[Tuple[int, int]] = []
    orchestra_tracks: List[Tuple[int, int]] = []
    forced = _forced_staves(mid)
    for track_idx, track in enumerate(mid.tracks):
        kind = 0 if forced else _legacy_piano_type(track)
        if kind:
            piano_tracks.append((track_idx, kind))
        elif track_idx not in forced:
            program = _orchestra_program(track)
            if program is not None:
                orchestra_tracks.append((track_idx, program))
    if forced:
        piano_tracks = [(t, TRACK_PRIMARY) for t in forced]
    elif not piano_tracks:
        piano_tracks = [(0, TRACK_PRIMARY)]
        if len(mid.tracks) > 1:
            piano_tracks.append((1, TRACK_PRIMARY))
    hands = {t: piano_hand(piano_tracks, t, kind) for t, kind in piano_tracks}
    return StaffRoles(piano_tracks, orchestra_tracks, [], hands)


def staff_roles(mid: MidiFile) -> StaffRoles:
    """The piano, orchestra and vocal staves of a render, and the hand of each piano staff.

    The legacy rules: a staff named 'piano-simplified' / 'piano-orchestral' / '...piano...', or with a program 0-7
    (on any of its channels), is a piano staff, the first of its kind the right hand and the others the left; with
    none, the first two staves are; the others with an orchestra program (:func:`is_valid_orchestra_instrument`) are
    PianoVision's orchestra.  A render (``mid.staves``, :class:`StaffFacts`) corrects them with what the score says:

    * percussion (a drumset) is neither piano nor orchestra: its notes are drum sounds, not pitches;
    * a voice (a ``voice.*`` instrument or a voice program) is a vocal staff, whatever its other channels play
      (chord symbols get a piano program); so is a one-staff piano part with lyrics in a score that has a piano
      part of two or more staves: a vocal line written for a piano instrument above the accompaniment.  Vocal staves
      go to Note Waterfall's orchestra (:mod:`pianovision.parts`), not to PianoVision, whose orchestra never took
      voices; only their sung line does (:func:`note_streams`), not the chord symbols written on them;
    * the staves of a piano part stay together: MuseScore writes the program only on a part's top staff, so a
      lower staff of a part the program made piano is piano too (a 'Klavier' or 'Фортепиано' part);
    * the piano staves of several parts that all carry lyrics are a choir written on piano staves: reduced like a
      rehearsal piano, the first part's staves are the right hand, the other parts' the left.

    Piano staves named by the score's build options (``MidiFile.piano_staves``, ``scores.json``) are the piano part,
    the first the right hand; of the others, percussion and voices are as above and the rest is orchestra by its
    program.  A ``.mid`` file keeps the legacy rules (the legacy outputs it reproduces were made by them)."""
    legacy = _legacy_roles(mid)
    facts: Optional[List[StaffFacts]] = getattr(mid, "staves", None)
    if not facts or len(facts) != len(mid.tracks):
        return legacy
    forced = _forced_staves(mid)
    if forced:
        piano = [(t, TRACK_PRIMARY) for t in forced]
        orchestra = [(t, p) for t, p in legacy.orchestra if not facts[t].drums and not facts[t].voice]
        vocal = [(i, f.program) for i, f in enumerate(facts) if i not in forced and not f.drums and f.voice]
        return StaffRoles(piano, orchestra, vocal, {t: piano_hand(piano, t, TRACK_PRIMARY) for t in forced})
    kinds = [_legacy_piano_type(track) for track in mid.tracks]
    grand_piano = any(kinds[i] == TRACK_PRIMARY and f.staves >= 2 and f.index == 0 and not f.drums and not f.voice
                      for i, f in enumerate(facts))
    piano: List[Tuple[int, int]] = []
    orchestra: List[Tuple[int, int]] = []
    vocal: List[Tuple[int, int]] = []
    role: Dict[int, int] = {}                  # piano track -> TRACK_*
    for i, f in enumerate(facts):
        kind = kinds[i]
        if f.drums:
            continue
        if f.voice or (kind == TRACK_PRIMARY and f.staves == 1 and f.sung and grand_piano):
            vocal.append((i, f.program))
            continue
        if not kind and f.index > 0:
            kind = role.get(i - f.index, 0)    # the part's top staff
        if kind:
            role[i] = kind
            piano.append((i, kind))
        else:
            program = _orchestra_program(mid.tracks[i])
            if program is not None:
                orchestra.append((i, program))
    if not piano:
        pitched = [i for i, f in enumerate(facts) if not f.drums and i not in dict(vocal)]
        piano = [(i, TRACK_PRIMARY) for i in pitched[:2]]
    hands = {t: piano_hand(piano, t, kind) for t, kind in piano}
    primary = [t for t, kind in piano if kind == TRACK_PRIMARY]
    parts: List[int] = []
    for t in primary:
        if facts[t].part not in parts:
            parts.append(facts[t].part)
    if len(parts) >= 2 and all(any(facts[t].sung for t in primary if facts[t].part == p) for p in parts):
        hands.update((t, 0 if facts[t].part == parts[0] else 1) for t in primary)
    return StaffRoles(piano, orchestra, vocal, hands)


def staves_revised(mid: MidiFile) -> bool:
    """The render's staves have other roles than the legacy rules gave them (:data:`STAVES_REVISION`)."""
    return staff_roles(mid) != _legacy_roles(mid)


def classify_tracks(mid: MidiFile, orchestra_mode: bool) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """(piano tracks as (track index, TRACK_* type), orchestra tracks as (track index, program)); the
    MIDI tracks are the score's staves in order (:func:`staff_roles`)."""
    roles = staff_roles(mid)
    return roles.piano, (roles.orchestra if orchestra_mode else [])


def _orchestra_programs(mid: MidiFile) -> List[Tuple[int, int]]:
    """(track, its first orchestra program) of every track that has one."""
    out = []
    for track_idx, track in enumerate(mid.tracks):
        for msg in track:
            if msg.type == 'program_change' and is_valid_orchestra_instrument(msg.program):
                out.append((track_idx, msg.program))
                break
    return out


def piano_hand(piano_tracks: List[Tuple[int, int]], track_idx: int, track_type: int) -> int:
    """Hand of a piano track by the legacy rule, 0 = right, 1 = left: the first track of its type plays the right
    hand (:attr:`StaffRoles.hands` has the hands of a render)."""
    same_type = [t[0] for t in piano_tracks if t[1] == track_type]
    return (0 if same_type.index(track_idx) == 0 else 1) if len(same_type) >= 2 else 0


@dataclass
class NoteStreams:
    """The notes of a render, per source, before PianoVision merges them into two hands.

    ``primary`` / ``simplified`` / ``orchestral``: the piano staves named 'piano', 'piano-simplified' and
    'piano-orchestral' ([right, left]); ``other_orchestra``: the other orchestra instruments, split into the
    hands at middle C (collected even when orchestra mode is off: PianoVision's JSON then ignores them);
    ``vocal``: the vocal staves, split the same way (Note Waterfall's orchestra only, :func:`staff_roles`).
    The Note objects are the ones PianoVision's JSON is built from."""
    primary: List[List[Note]]
    simplified: List[List[Note]]
    orchestral: List[List[Note]]
    other_orchestra: List[List[Note]]
    measure_map: List[Dict[str, Any]]
    get_measure_for_tick: Any
    piano_tracks: List[Tuple[int, int]]
    orchestra_tracks: List[Tuple[int, int]]     # (track index, program)
    max_time_piano: float
    max_time_orchestra: float
    ticks_per_beat: int
    vocal: List[List[Note]] = field(default_factory=lambda: [[], []])
    vocal_tracks: List[Tuple[int, int]] = field(default_factory=list)    # (track index, program)


def note_streams(mid: MidiFile) -> NoteStreams:
    primary: List[List[Note]] = [[], []]
    simplified: List[List[Note]] = [[], []]
    orchestral: List[List[Note]] = [[], []]
    other_orchestra: List[List[Note]] = [[], []]
    tempos = extract_tempo_events(mid)
    tpb = mid.ticks_per_beat
    seconds = TempoIndex(tempos, tpb)
    max_time = 0.0

    measure_map = calculate_measure_map(mid)
    get_measure_for_tick = _measure_lookup(measure_map, tpb)

    # the piano tracks do not depend on orchestra mode; the orchestra tracks are all collected
    roles = staff_roles(mid)
    piano_tracks, orchestra_tracks = roles.piano, roles.orchestra

    # LEGACY: one active-note table shared by all piano tracks.
    active: Dict[Tuple[int, int], Note] = {}
    for track_idx, track_type in piano_tracks:
        hand_idx = roles.hands[track_idx]
        target = {TRACK_PRIMARY: primary, TRACK_SIMPLIFIED: simplified,
                  TRACK_ORCHESTRAL: orchestral}[track_type][hand_idx]
        ticks = 0
        for msg in mid.tracks[track_idx]:
            ticks += msg.time
            t = seconds(ticks)
            max_time = max(max_time, t)
            if msg.channel is None:
                continue
            if msg.type == 'note_on' and msg.velocity > 0:
                note = Note(midi=msg.note, time=t, velocity=msg.velocity / 127.0, duration=0, ticks=ticks,
                            duration_ticks=0, staff=hand_idx + 1,
                            group=get_measure_for_tick(ticks) - 1,
                            src=getattr(msg, "src", None), raw_velocity=msg.velocity, track=track_idx)
                target.append(note)
                active[(msg.channel, msg.note)] = note
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                note = active.pop((msg.channel, msg.note), None)
                if note is not None:
                    note.duration = max(t - note.time, 0)
                    note.duration_ticks = max(ticks - note.ticks, 0)

    max_time_orch = 0.0
    active_orch: Dict[Tuple[int, int], Note] = {}
    for track_idx, _ in orchestra_tracks:
        ticks = 0
        for msg in mid.tracks[track_idx]:
            ticks += msg.time
            t = seconds(ticks)
            max_time_orch = max(max_time_orch, t)
            if msg.channel is None:
                continue
            if msg.type == 'note_on' and msg.velocity > 0:
                hand_idx = 0 if msg.note >= 60 else 1
                note = Note(midi=msg.note, time=t, velocity=msg.velocity / 127.0, duration=0, ticks=ticks,
                            duration_ticks=0, staff=hand_idx + 1,
                            group=get_measure_for_tick(ticks) - 1,
                            src=getattr(msg, "src", None), raw_velocity=msg.velocity, track=track_idx)
                other_orchestra[hand_idx].append(note)
                active_orch[(msg.channel, msg.note)] = note
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                note = active_orch.pop((msg.channel, msg.note), None)
                if note is not None:
                    # LEGACY: no clamping of negative durations for orchestra notes
                    note.duration = t - note.time
                    note.duration_ticks = ticks - note.ticks

    # the vocal staves: the sung line, on the channels of the voice's program (chord symbols written on a vocal staff
    # play on a channel of their own, MuseScore's "harmony" one, and are not sung); an active-note table of their own
    # (the others' notes stay as they always were)
    vocal: List[List[Note]] = [[], []]
    for track_idx, program in roles.vocal:
        top = track_idx - mid.staves[track_idx].index          # the part's programs are on its top staff
        programs: Dict[int, int] = {}
        for msg in mid.tracks[top]:
            if msg.type == 'program_change':
                programs.setdefault(msg.channel, msg.program)
        ticks = 0
        active_vocal: Dict[Tuple[int, int], Note] = {}
        for msg in mid.tracks[track_idx]:
            ticks += msg.time
            if msg.channel is None or programs.get(msg.channel, program) != program:
                continue
            if msg.type == 'note_on' and msg.velocity > 0:
                hand_idx = 0 if msg.note >= 60 else 1
                note = Note(midi=msg.note, time=seconds(ticks), velocity=msg.velocity / 127.0, duration=0,
                            ticks=ticks, duration_ticks=0, staff=hand_idx + 1, group=get_measure_for_tick(ticks) - 1,
                            src=getattr(msg, "src", None), raw_velocity=msg.velocity, track=track_idx)
                vocal[hand_idx].append(note)
                active_vocal[(msg.channel, msg.note)] = note
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                note = active_vocal.pop((msg.channel, msg.note), None)
                if note is not None:
                    note.duration = max(seconds(ticks) - note.time, 0)
                    note.duration_ticks = max(ticks - note.ticks, 0)

    return NoteStreams(primary, simplified, orchestral, other_orchestra, measure_map, get_measure_for_tick,
                       piano_tracks, orchestra_tracks, max_time, max_time_orch, tpb, vocal, roles.vocal)


def _copy_hands(hands: List[List[Note]]) -> List[List[Note]]:
    return [[dataclasses.replace(n) for n in hand] for hand in hands]


def get_notes(mid: MidiFile, orchestra_mode: bool, simplified_mode: bool,
              merge_measures: Dict[str, Set[int]],
              accented_notes: Dict[Tuple[int, int, int, Tuple[int, ...]], List[int]],
              streams: Optional[NoteStreams] = None) -> Tuple[List[Track], float]:
    """The two hands of PianoVision's JSON (``streams``: :func:`note_streams` of ``mid``, when already made)."""
    s = streams or note_streams(mid)
    max_time = max(s.max_time_piano, s.max_time_orchestra) if orchestra_mode else s.max_time_piano
    other_orchestra = s.other_orchestra if orchestra_mode else [[], []]

    result = merge_tracks(s.primary, s.simplified, s.orchestral, other_orchestra, s.measure_map, s.ticks_per_beat,
                          simplified_mode, orchestra_mode, merge_measures)

    if accented_notes:
        _apply_accents(result, accented_notes, s.get_measure_for_tick)

    final = [Track(notes=sorted(hand, key=lambda x: x.time)) for hand in result if hand]
    while len(final) < 2:
        final.append(Track(notes=[]))
    return final, max_time


def _apply_accents(result: List[List[Note]], accented_notes, get_measure_for_tick) -> None:
    by_tick_hand: Dict[Tuple[int, int], List[Note]] = {}
    for hand_idx, hand in enumerate(result):
        for note in hand:
            by_tick_hand.setdefault((note.ticks, hand_idx), []).append(note)
    for (tick, _hand_idx), notes in by_tick_hand.items():
        if not notes:
            continue
        notes.sort(key=lambda x: x.midi)
        pitches = tuple(n.midi for n in notes)
        measure_num = get_measure_for_tick(tick)
        staff_id = notes[0].staff
        found = None
        for voice_idx in range(4):
            key = (staff_id, measure_num - 1, voice_idx, pitches)
            if key in accented_notes:
                found = accented_notes[key]
                break
        if found is None:
            continue
        for i, note in enumerate(notes):
            if i in found:
                note.accent = 1
                if note.velocity < 0.95:
                    note.velocity = min(1.0, note.velocity * 1.2)


def simplify_hands(primary: List[List[Note]], simplified: List[List[Note]], fast) -> List[List[Note]]:
    """The simplified piano part: per hand and measure, the simplified staff's notes where it has any, else the
    primary staff's (``fast``: tick -> measure number).  Without simplified notes: the primary hands (copies)."""
    result = [primary[0].copy(), primary[1].copy()]
    if not any(s for s in simplified if s):
        return result
    for hand_idx in (0, 1):
        if not simplified[hand_idx]:
            continue
        prim_by_measure: Dict[int, List[Note]] = {}
        for n in primary[hand_idx]:
            prim_by_measure.setdefault(fast(n.ticks), []).append(n)
        simp_by_measure: Dict[int, List[Note]] = {}
        for n in simplified[hand_idx]:
            simp_by_measure.setdefault(fast(n.ticks), []).append(n)
        new_track = []
        for m in sorted(set(prim_by_measure) | set(simp_by_measure)):
            if simp_by_measure.get(m):
                new_track.extend(simp_by_measure[m])
            elif prim_by_measure.get(m):
                new_track.extend(prim_by_measure[m])
        result[hand_idx] = sorted(new_track, key=lambda n: n.ticks)
    return result


def merge_tracks(primary, simplified, orchestral, other_orchestra, measure_map, tpb,
                 simplified_mode: bool, orchestra_mode: bool, merge_measures) -> List[List[Note]]:
    fast = _measure_lookup(measure_map, tpb)
    result = [primary[0].copy(), primary[1].copy()]

    if simplified_mode:
        result = simplify_hands(primary, simplified, fast)

    if orchestra_mode or any(merge_measures.values()):
        for hand_idx in (0, 1):
            hand_key = 'right' if hand_idx == 0 else 'left'
            result_by_measure: Dict[int, List[Note]] = {}
            for n in result[hand_idx]:
                result_by_measure.setdefault(fast(n.ticks), []).append(n)

            if merge_measures[hand_key]:
                orch_by_measure: Dict[int, List[Note]] = {}
                for n in orchestral[hand_idx]:
                    orch_by_measure.setdefault(fast(n.ticks), []).append(n)
                merged = []
                for m in merge_measures[hand_key]:
                    merged.extend(orch_by_measure.get(m, []))
                result[hand_idx].extend(merged)

            if orchestra_mode:
                possible = {m['measure_num'] for m in measure_map}
                possible.update(result_by_measure.keys())
                empty = possible - set(result_by_measure.keys())
                if empty:
                    piano_fill: Dict[int, List[Note]] = {}
                    for n in orchestral[hand_idx]:
                        m = fast(n.ticks)
                        if m in empty:
                            piano_fill.setdefault(m, []).append(n)
                    remaining = empty - set(piano_fill.keys())
                    other_fill: Dict[int, List[Note]] = {}
                    if remaining and not orchestral[hand_idx]:
                        for n in other_orchestra[hand_idx]:
                            m = fast(n.ticks)
                            if m in remaining:
                                other_fill.setdefault(m, []).append(n)
                    for notes in piano_fill.values():
                        result[hand_idx].extend(notes)
                    for notes in other_fill.values():
                        result[hand_idx].extend(notes)

    for hand_idx in (0, 1):
        result[hand_idx] = sorted(result[hand_idx], key=lambda n: n.time)
    return result


# --------------------------------------------------------------------------
# tracksV2 / measures
# --------------------------------------------------------------------------

def organize_tracks_v2(tracks: List[Track], time_sigs, tempos, resolution: int = 480):
    right, left = [], []
    tpb = resolution
    for track_idx, track in enumerate(tracks):
        for note in track.notes:
            idx = len(right) if track_idx == 0 else len(left)
            data = {
                "note": note.midi,
                "durationTicks": note.duration_ticks,
                "noteOffVelocity": 0,
                "ticksStart": note.ticks,
                "velocity": note.velocity,
                "measureBars": (note.ticks / tpb) / 4,
                "duration": note.duration,
                "noteName": get_note_name(note.midi),
                "octave": (note.midi // 12) - 1,
                "notePitch": get_note_name(note.midi).rstrip('0123456789'),
                "start": note.time,
                "end": note.time + note.duration,
                "noteLengthType": get_note_length_type(note.duration_ticks),
                "group": -1,
                "measureInd": int((note.ticks / tpb) / 4),
                "noteMeasureInd": idx,
                "id": f"{'r' if track_idx == 0 else 'l'}{idx}",
                "accent": int(note.accent),
            }
            (right if track_idx == 0 else left).append(data)
    return create_measure_data(time_sigs, right, left, tempos)


def create_measure_data(time_sigs, right_notes, left_notes, tempos):
    measures_right, measures_left = [], []
    if not time_sigs:
        return {"right": [], "left": []}
    last_tick = 0
    if right_notes:
        last_tick = max(last_tick, max(n["ticksStart"] + n["durationTicks"] for n in right_notes))
    if left_notes:
        last_tick = max(last_tick, max(n["ticksStart"] + n["durationTicks"] for n in left_notes))

    # LEGACY: 480 ticks per beat is hard-coded here.
    TPB = 480
    seconds = TempoIndex(tempos, TPB)
    bars = []  # (start, end, timeSignature)
    current = 0
    while current <= last_tick:
        ts = next((t for t in reversed(time_sigs) if t["ticks"] <= current), time_sigs[0])
        numerator, denominator = ts["timeSignature"]
        end_tick = current + TPB * 4 * numerator // denominator
        bars.append((current, end_tick, ts["timeSignature"]))
        current = end_tick
    starts = [b[0] for b in bars]

    def bucket(notes):
        # same membership and order as scanning every note for every bar
        groups: Dict[int, List[Dict[str, Any]]] = {}
        for n in notes:
            i = bisect.bisect_right(starts, n["ticksStart"]) - 1
            if i >= 0 and n["ticksStart"] < bars[i][1]:
                groups.setdefault(i, []).append(n)
        return groups

    right_by_bar, left_by_bar = bucket(right_notes), bucket(left_notes)
    for measure_idx, (start, end_tick, time_signature) in enumerate(bars):
        mr = right_by_bar.get(measure_idx)
        ml = left_by_bar.get(measure_idx)
        if not mr and not ml:
            continue
        start_time = seconds(start)
        end_time = seconds(end_tick)
        if mr:
            measures_right.append({
                "direction": "up",
                "time": start_time,
                "timeSignature": time_signature,
                "notes": mr,
                "max": max(n["note"] for n in mr),
                "min": min(n["note"] for n in mr),
                "measureTicksStart": start,
                "measureTicksEnd": end_tick,
                "rests": calculate_rests(start_time, end_time, mr),
                "type": 0 if measure_idx == 0 else 2,
            })
        if ml:
            measures_left.append({
                "direction": "down",
                "time": start_time,
                "timeEnd": end_time,
                "timeSignature": time_signature,
                "notes": ml,
                "max": max(n["note"] for n in ml),
                "min": min(n["note"] for n in ml),
                "measureTicksStart": start,
                "measureTicksEnd": end_tick,
                "rests": calculate_rests(start_time, end_time, ml),
                "type": 0 if measure_idx == 0 else 2,
            })
    return {"right": measures_right, "left": measures_left}


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

def build_song(mid: MidiFile, mscx_root: Optional[ET.Element], source_path: str,
               orchestra_mode: bool = True, simplified_mode: bool = True,
               legacy_metadata: Union[bool, str] = True,
               provenance: Optional[Dict[str, List[Note]]] = None,
               parts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Assemble the PianoVision JSON document.

    ``mscx_root`` supplies title/composer, merge markers and accents; without
    it the file name and folder are used for metadata.  ``legacy_metadata``
    selects the old title rules (see metadata.extract_title_artist), or names the
    rules ("v2" | "legacy" | "file", as ``Compat.metadata``).
    ``provenance``, when given, receives ``"right"`` and ``"left"``: the
    :class:`Note` behind every ``tracksV2`` note of that hand, in file order
    (``src`` is the score note when the MIDI came from the renderer with provenance).
    ``parts``, when given, receives Note Waterfall's parts of the song (:func:`pianovision.parts.build_parts`:
    ``"doc"``, ``"notes"``, ``"simplified_staff"``); the JSON is the same either way.
    """
    tempos = extract_tempo_events(mid)
    if mscx_root is not None:
        rules = legacy_metadata if isinstance(legacy_metadata, str) else ("legacy" if legacy_metadata else "v2")
        title, artist = title_artist(mscx_root, source_path, rules)
        merge_markers = extract_merge_markers(mscx_root)
        accents = extract_accented_notes(mscx_root)
    else:
        title = os.path.splitext(os.path.basename(source_path))[0].replace('_', ' ')
        artist = os.path.basename(os.path.dirname(source_path))
        merge_markers = {'right': set(), 'left': set()}
        accents = {}

    streams = note_streams(mid)
    if parts is not None:
        # copies of the notes, taken before PianoVision's accents change them in place
        from .parts import build_parts
        streams_copy = dataclasses.replace(streams, primary=_copy_hands(streams.primary),
                                           simplified=_copy_hands(streams.simplified),
                                           orchestral=_copy_hands(streams.orchestral),
                                           other_orchestra=_copy_hands(streams.other_orchestra),
                                           vocal=_copy_hands(streams.vocal))
        originals = {id(c): o for name in ("primary", "simplified", "orchestral", "other_orchestra")
                     for hc, ho in zip(getattr(streams_copy, name), getattr(streams, name)) for c, o in zip(hc, ho)}
    tracks, song_length = get_notes(mid, orchestra_mode, simplified_mode, merge_markers, accents, streams=streams)
    if parts is not None:
        pv_ids = {id(n) for t in tracks for n in t.notes}
        # the copies of PianoVision's notes stand for them when telling merged orchestra notes
        pv_copies = [[c for hand in (getattr(streams_copy, name)) for c in hand if id(originals[id(c)]) in pv_ids]
                     for name in ("orchestral", "other_orchestra")]
        parts.update(build_parts(streams_copy, accents, pv_copies))
        # the score's rehearsal marks, sections and tempo words (pianovision.markers), on the notes' clock
        from .markers import build_markers
        piano_staves = [t for t, kind in classify_tracks(mid, True)[0] if kind in (TRACK_PRIMARY, TRACK_SIMPLIFIED)]
        markers = build_markers(getattr(mid, "annotations", None) or [], TempoIndex(tempos, mid.ticks_per_beat),
                                piano_staves)
        # the sung syllables onto the notes that sing them (pianovision.lyrics; needs the notes' provenance)
        from .lyrics import attach_lyrics
        lyrics = attach_lyrics(parts, getattr(mid, "lyrics", None), piano_staves,
                               TempoIndex(tempos, mid.ticks_per_beat))
        doc = parts["doc"]
        # after the header, before the (long) note streams
        parts["doc"] = {k: v for k, v in doc.items() if k != "parts"}
        parts["doc"]["markers"] = markers
        parts["doc"]["lyrics"] = lyrics
        parts["doc"]["parts"] = doc["parts"]
    time_sigs = extract_time_signatures(mid)
    key_sigs = extract_key_signatures(mid)
    tracks_v2 = organize_tracks_v2(tracks, time_sigs, tempos, mid.ticks_per_beat)
    if provenance is not None:
        provenance["right"] = list(tracks[0].notes)
        provenance["left"] = list(tracks[1].notes) if len(tracks) > 1 else []

    tpb = mid.ticks_per_beat
    seconds = TempoIndex(tempos, tpb)
    total_ticks = _total_ticks(mid)
    measures = []
    current = 0
    while current < total_ticks:
        ts = next((t["timeSignature"] for t in reversed(time_sigs) if t["ticks"] <= current), [4, 4])
        numerator, denominator = ts
        ticks_per_measure = tpb * 4 * numerator // denominator
        # LEGACY: fractional tick offset that has always been emitted.
        offset = 0.35 * ticks_per_measure / 480
        measures.append({
            "time": seconds(current),
            "timeSignature": ts,
            "ticksPerMeasure": ticks_per_measure,
            "ticksStart": current + offset,
            "totalTicks": ticks_per_measure + offset,
            "type": 0 if not measures else 2,
        })
        current += ticks_per_measure

    supporting = [{
        "notes": [{"midi": n.midi, "time": n.time, "velocity": n.velocity, "duration": n.duration}
                  for n in track.notes],
        "myInstrument": track.myInstrument,
        "theirInstrument": track.theirInstrument,
    } for track in tracks]

    return {
        "supportingTracks": supporting,
        "start_time": 0,
        "song_length": song_length,
        "resolution": tpb,
        "tempos": tempos,
        "keySignatures": key_sigs,
        "timeSignatures": time_sigs,
        "measures": measures,
        "tracksV2": tracks_v2,
        "accompanyingInstruments": [-2, -1],
        "accompanyingChannels": [0, 0],
        "name": title,
        "artist": artist,
        "accompanyingTracks": [],
    }
