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
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .metadata import extract_accented_notes, extract_merge_markers, extract_title_artist
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


# --------------------------------------------------------------------------
# notes
# --------------------------------------------------------------------------

def get_notes(mid: MidiFile, orchestra_mode: bool, simplified_mode: bool,
              merge_measures: Dict[str, Set[int]],
              accented_notes: Dict[Tuple[int, int, int, Tuple[int, ...]], List[int]]) -> Tuple[List[Track], float]:
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

    piano_tracks: List[Tuple[int, int]] = []
    orchestra_tracks: List[Tuple[int, int]] = []
    for track_idx, track in enumerate(mid.tracks):
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
        if found_piano:
            piano_tracks.append((track_idx, track_type))
        elif orchestra_mode:
            for msg in track:
                if msg.type == 'program_change' and is_valid_orchestra_instrument(msg.program):
                    orchestra_tracks.append((track_idx, msg.program))
                    break

    if not piano_tracks:
        piano_tracks = [(0, TRACK_PRIMARY)]
        if len(mid.tracks) > 1:
            piano_tracks.append((1, TRACK_PRIMARY))

    # LEGACY: one active-note table shared by all piano tracks.
    active: Dict[Tuple[int, int], Note] = {}
    for track_idx, track_type in piano_tracks:
        same_type = [t[0] for t in piano_tracks if t[1] == track_type]
        hand_idx = (0 if same_type.index(track_idx) == 0 else 1) if len(same_type) >= 2 else 0
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
                            group=get_measure_for_tick(ticks) - 1)
                target.append(note)
                active[(msg.channel, msg.note)] = note
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                note = active.pop((msg.channel, msg.note), None)
                if note is not None:
                    note.duration = max(t - note.time, 0)
                    note.duration_ticks = max(ticks - note.ticks, 0)

    if orchestra_mode:
        active_orch: Dict[Tuple[int, int], Note] = {}
        for track_idx, _ in orchestra_tracks:
            ticks = 0
            for msg in mid.tracks[track_idx]:
                ticks += msg.time
                t = seconds(ticks)
                max_time = max(max_time, t)
                if msg.channel is None:
                    continue
                if msg.type == 'note_on' and msg.velocity > 0:
                    hand_idx = 0 if msg.note >= 60 else 1
                    note = Note(midi=msg.note, time=t, velocity=msg.velocity / 127.0, duration=0, ticks=ticks,
                                duration_ticks=0, staff=hand_idx + 1,
                                group=get_measure_for_tick(ticks) - 1)
                    other_orchestra[hand_idx].append(note)
                    active_orch[(msg.channel, msg.note)] = note
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    note = active_orch.pop((msg.channel, msg.note), None)
                    if note is not None:
                        # LEGACY: no clamping of negative durations for orchestra notes
                        note.duration = t - note.time
                        note.duration_ticks = ticks - note.ticks

    result = merge_tracks(primary, simplified, orchestral, other_orchestra, measure_map, tpb,
                          simplified_mode, orchestra_mode, merge_measures)

    if accented_notes:
        _apply_accents(result, accented_notes, get_measure_for_tick)

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


def merge_tracks(primary, simplified, orchestral, other_orchestra, measure_map, tpb,
                 simplified_mode: bool, orchestra_mode: bool, merge_measures) -> List[List[Note]]:
    fast = _measure_lookup(measure_map, tpb)
    result = [primary[0].copy(), primary[1].copy()]

    if simplified_mode and any(s for s in simplified if s):
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
               orchestra_mode: bool = True, simplified_mode: bool = True) -> Dict[str, Any]:
    """Assemble the PianoVision JSON document.

    ``mscx_root`` supplies title/composer, merge markers and accents; without
    it the file name and folder are used for metadata.
    """
    tempos = extract_tempo_events(mid)
    if mscx_root is not None:
        title, artist = extract_title_artist(mscx_root, source_path)
        merge_markers = extract_merge_markers(mscx_root)
        accents = extract_accented_notes(mscx_root)
    else:
        title = os.path.splitext(os.path.basename(source_path))[0].replace('_', ' ')
        artist = os.path.basename(os.path.dirname(source_path))
        merge_markers = {'right': set(), 'left': set()}
        accents = {}

    tracks, song_length = get_notes(mid, orchestra_mode, simplified_mode, merge_markers, accents)
    time_sigs = extract_time_signatures(mid)
    key_sigs = extract_key_signatures(mid)
    tracks_v2 = organize_tracks_v2(tracks, time_sigs, tempos, mid.ticks_per_beat)

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
