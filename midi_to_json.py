import mido
import json
import os
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass

@dataclass
class Note:
    midi: int
    time: float  # in seconds
    velocity: float
    duration: float  # in seconds
    ticks: int  # Store original tick position
    duration_ticks: int  # Store original duration in ticks

@dataclass
class Track:
    notes: List[Note]
    myInstrument: int
    theirInstrument: int

def extract_tempo_events(mid: mido.MidiFile) -> List[Dict[str, Any]]:
    tempo_events = []
    current_tempo = 500000  # Default tempo
    TICKS_PER_BEAT = 480
    
    for track in mid.tracks:
        track_time = 0.0
        track_ticks = 0
        for msg in track:
            track_ticks += msg.time
            # Convert message time to seconds based on current tempo
            msg_time = (msg.time * current_tempo) / (TICKS_PER_BEAT * 1000000)
            track_time += msg_time
            
            if msg.type == 'set_tempo':
                current_tempo = msg.tempo
                tempo_events.append({
                    "bpm": 60000000 / msg.tempo,
                    "ticks": track_ticks,
                    "time": track_time
                })
    
    if not tempo_events:
        tempo_events.append({
            "bpm": 120,
            "ticks": 0,
            "time": 0
        })
    
    # Sort tempo events by time
    tempo_events.sort(key=lambda x: x["time"])
    return tempo_events

def extract_time_signatures(mid: mido.MidiFile) -> List[Dict[str, Any]]:
    time_sigs = []
    current_ticks = 0
    
    for track in mid.tracks:
        track_ticks = 0
        for msg in track:
            track_ticks += msg.time
            if msg.type == 'time_signature':
                time_sigs.append({
                    "ticks": track_ticks,
                    "timeSignature": [msg.numerator, msg.denominator],
                    "measures": len(time_sigs)
                })
    
    if not time_sigs:
        time_sigs.append({
            "ticks": 0,
            "timeSignature": [4, 4],
            "measures": 0
        })
    
    # Sort by tick position
    time_sigs.sort(key=lambda x: x["ticks"])
    return time_sigs

def extract_key_signatures(mid: mido.MidiFile) -> List[Dict[str, Any]]:
    key_sigs = []
    current_time = 0
    
    for msg in mid:
        current_time += msg.time
        if msg.type == 'key_signature':
            # Parse the key signature string (e.g., "Cm" or "F#")
            if len(msg.key) > 1 and msg.key.endswith('m'):
                key = msg.key[:-1]  # Remove 'm' from the end
                scale = "minor"
            else:
                key = msg.key
                scale = "major"
                
            key_sigs.append({
                "key": key,
                "scale": scale,
                "ticks": int(current_time * mid.ticks_per_beat)
            })
    
    if not key_sigs:
        key_sigs.append({
            "key": "C",
            "scale": "major",
            "ticks": 0
        })
    
    return key_sigs

def ticks_to_seconds(ticks: int, tempos: List[Dict[str, Any]], ticks_per_beat: int) -> float:
    """Convert tick position to seconds considering tempo changes"""
    if not tempos:
        # Default tempo of 120 BPM (500000 microseconds per beat)
        return (ticks * 500000) / (ticks_per_beat * 1000000)
    
    current_time = 0.0
    current_ticks = 0
    current_tempo = 500000  # Default tempo
    
    for tempo in tempos:
        if ticks < tempo["ticks"]:
            # Calculate remaining time until target ticks
            delta_ticks = ticks - current_ticks
            return current_time + (delta_ticks * current_tempo) / (ticks_per_beat * 1000000)
        
        # Add time until this tempo change
        delta_ticks = tempo["ticks"] - current_ticks
        current_time += (delta_ticks * current_tempo) / (ticks_per_beat * 1000000)
        current_ticks = tempo["ticks"]
        current_tempo = int(60000000 / tempo["bpm"])
    
    # Calculate remaining time after last tempo change
    delta_ticks = ticks - current_ticks
    return current_time + (delta_ticks * current_tempo) / (ticks_per_beat * 1000000)

def get_notes_from_midi(midi_path: str) -> Tuple[List[Track], float]:
    mid = mido.MidiFile(midi_path)
    tracks: List[List[Note]] = [[] for _ in range(2)]  # Right hand (1), Left hand (2)
    notes: Dict[Tuple[int, int], Tuple[int, float, float]] = {}
    tempos = extract_tempo_events(mid)
    max_time = 0.0
    
    # First pass: identify piano tracks by looking for "piano" in track names
    piano_tracks = set()
    for track_idx, track in enumerate(mid.tracks):
        for msg in track:
            if msg.type == 'track_name' and 'piano' in msg.name.lower():
                piano_tracks.add(track_idx)
                break
    
    # If no explicit piano tracks found, fallback to program change detection
    if not piano_tracks:
        for track_idx, track in enumerate(mid.tracks):
            for msg in track:
                if msg.type == 'program_change' and msg.program == 0:  # Program 0 is piano
                    piano_tracks.add(track_idx)
    
    # Process piano tracks and collect notes
    for track_idx, track in enumerate(mid.tracks):
        if track_idx not in piano_tracks:
            continue

        track_ticks = 0
        current_channel = None
        
        for msg in track:
            track_ticks += msg.time
            track_time = ticks_to_seconds(track_ticks, tempos, mid.ticks_per_beat)
            max_time = max(max_time, track_time)
            
            if hasattr(msg, 'channel'):
                current_channel = msg.channel
            
            if msg.type == 'note_on' and msg.velocity > 0:
                if current_channel is not None:
                    notes[(current_channel, msg.note)] = (track_ticks, track_time, msg.velocity / 127.0)
            elif (msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0)):
                if current_channel is not None and (current_channel, msg.note) in notes:
                    start_tick, start_time, velocity = notes[(current_channel, msg.note)]
                    duration_seconds = track_time - start_time
                    duration_ticks = track_ticks - start_tick
                    
                    # Use track 1 for left hand, track 0 for right hand
                    hand_idx = 1 if track_idx == 1 else 0
                    tracks[hand_idx].append(Note(
                        midi=msg.note,
                        time=start_time,
                        velocity=velocity,
                        duration=duration_seconds,
                        ticks=start_tick,
                        duration_ticks=duration_ticks
                    ))
                    del notes[(current_channel, msg.note)]

    # Create final tracks
    final_tracks = []
    for hand_idx, notes in enumerate(tracks):
        if notes:
            final_tracks.append(Track(
                notes=sorted(notes, key=lambda x: x.time),
                myInstrument=-5,  # Piano
                theirInstrument=0
            ))

    return final_tracks, max_time

def get_note_length_type(duration_ticks: int) -> str:
    """Determine note length type based on duration in ticks"""
    if duration_ticks >= 360:  # Roughly a quarter note
        return "quarter"
    elif duration_ticks >= 180:  # Roughly an eighth note
        return "eighth"
    else:
        return "dottedsixteenth"

def organize_tracks_v2(tracks: List[Track], time_sigs: List[Dict[str, Any]], tempos: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    right_hand_notes = []
    left_hand_notes = []
    
    TICKS_PER_BEAT = 480
    
    for track_idx, track in enumerate(tracks):
        for note in track.notes:
            note_data = {
                "note": note.midi,
                "durationTicks": note.duration_ticks,  # Use stored tick duration
                "noteOffVelocity": 0,
                "ticksStart": note.ticks,  # Use stored tick position
                "velocity": note.velocity,
                "measureBars": (note.ticks / TICKS_PER_BEAT) / 4,
                "duration": note.duration,
                "noteName": get_note_name(note.midi),
                "octave": (note.midi // 12) - 1,
                "notePitch": get_note_name(note.midi).rstrip('0123456789'),
                "start": note.time,
                "end": note.time + note.duration,
                "noteLengthType": get_note_length_type(note.duration_ticks),
                "group": -1,
                "measureInd": int((note.ticks / TICKS_PER_BEAT) / 4),
                "noteMeasureInd": len(right_hand_notes) if track_idx == 0 else len(left_hand_notes),
                "id": f"{'r' if track_idx == 0 else 'l'}{len(right_hand_notes) if track_idx == 0 else len(left_hand_notes)}"
            }
            
            if track_idx == 0:
                right_hand_notes.append(note_data)
            else:
                left_hand_notes.append(note_data)
    
    measure_data = create_measure_data(time_sigs, right_hand_notes, left_hand_notes, tempos)
    return {
        "right": measure_data["right"],
        "left": measure_data["left"]
    }

def get_note_name(midi_note: int) -> str:
    notes = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    note_name = notes[midi_note % 12]
    octave = (midi_note // 12) - 1
    return f"{note_name}{octave}"

def create_measure_data(time_sigs: List[Dict[str, Any]], 
                       right_notes: List[Dict[str, Any]], 
                       left_notes: List[Dict[str, Any]],
                       tempos: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    measures_right = []
    measures_left = []
    
    if not time_sigs:
        return {"right": [], "left": []}

    # Calculate total ticks needed based on the last note
    last_tick = 0
    if right_notes:
        last_tick = max(last_tick, max(n["ticksStart"] + n["durationTicks"] for n in right_notes))
    if left_notes:
        last_tick = max(last_tick, max(n["ticksStart"] + n["durationTicks"] for n in left_notes))

    TICKS_PER_BEAT = 480
    current_measure_tick = 0
    measure_idx = 0
    
    # Process measures until we've covered all notes
    while current_measure_tick <= last_tick:
        time_sig = next((ts for ts in reversed(time_sigs) 
                        if ts["ticks"] <= current_measure_tick), time_sigs[0])
        
        numerator, denominator = time_sig["timeSignature"]
        ticks_per_measure = TICKS_PER_BEAT * 4 * numerator // denominator
        end_tick = current_measure_tick + ticks_per_measure
        
        # Use tempo-aware timing
        start_time = ticks_to_seconds(current_measure_tick, tempos, TICKS_PER_BEAT)
        end_time = ticks_to_seconds(end_tick, tempos, TICKS_PER_BEAT)
        
        # Get notes for this measure
        measure_right_notes = [
            n for n in right_notes 
            if current_measure_tick <= n["ticksStart"] < end_tick
        ]
        measure_left_notes = [
            n for n in left_notes 
            if current_measure_tick <= n["ticksStart"] < end_tick
        ]
        
        if measure_right_notes:
            measures_right.append({
                "direction": "up",
                "time": start_time,
                "timeSignature": time_sig["timeSignature"],
                "notes": measure_right_notes,
                "max": max(n["note"] for n in measure_right_notes),
                "min": min(n["note"] for n in measure_right_notes),
                "measureTicksStart": current_measure_tick,
                "measureTicksEnd": end_tick,
                "rests": calculate_rests(measure_right_notes, start_time, end_time),
                "type": 0 if measure_idx == 0 else 2
            })
        
        if measure_left_notes:
            measures_left.append({
                "direction": "down",
                "time": start_time,
                "timeEnd": end_time,
                "timeSignature": time_sig["timeSignature"],
                "notes": measure_left_notes,
                "max": max(n["note"] for n in measure_left_notes),
                "min": min(n["note"] for n in measure_left_notes),
                "measureTicksStart": current_measure_tick,
                "measureTicksEnd": end_tick,
                "rests": calculate_rests(measure_left_notes, start_time, end_time),
                "type": 0 if measure_idx == 0 else 2
            })
        
        current_measure_tick = end_tick
        measure_idx += 1

    return {
        "right": measures_right,
        "left": measures_left
    }

def calculate_rests(notes: List[Dict[str, Any]], start_time: float, end_time: float) -> List[Dict[str, Any]]:
    rests = []
    current_time = start_time
    
    sorted_notes = sorted(notes, key=lambda x: x["start"])
    
    for note in sorted_notes:
        if note["start"] > current_time:
            rests.append({
                "time": current_time,
                "noteLengthType": "dottedsixteenth"
            })
        current_time = note["end"]
    
    if current_time < end_time:
        rests.append({
            "time": current_time,
            "noteLengthType": "dottedsixteenth"
        })
    
    return rests

def create_piano_vision_json(midi_path: str) -> Dict[str, Any]:
    mid = mido.MidiFile(midi_path)
    tempos = extract_tempo_events(mid)
    tracks, song_length = get_notes_from_midi(midi_path)
    
    # Extract metadata from filename and full path
    filename = os.path.basename(midi_path)
    title = os.path.splitext(filename)[0].replace('_', ' ')
    
    # Get the full path to extract correct artist name
    parent_dir = os.path.dirname(midi_path)
    artist = os.path.basename(parent_dir)

    time_sigs = extract_time_signatures(mid)
    
    # Get total number of measures from tracks
    tracks_v2 = organize_tracks_v2(tracks, time_sigs, tempos)
    max_measure_idx = 0
    if tracks_v2["right"]:
        max_measure_idx = max(max_measure_idx, len(tracks_v2["right"]))
    if tracks_v2["left"]:
        max_measure_idx = max(max_measure_idx, len(tracks_v2["left"]))

    measures = []
    ticks_per_beat = mid.ticks_per_beat
    current_measure_tick = 0
    
    # Calculate total ticks in the MIDI file
    total_ticks = 0
    for track in mid.tracks:
        track_ticks = 0
        for msg in track:
            track_ticks += msg.time
        total_ticks = max(total_ticks, track_ticks)
    
    # Create measures until we reach the end of the MIDI
    while current_measure_tick < total_ticks:
        time_sig = next((ts["timeSignature"] for ts in reversed(time_sigs) 
                        if ts["ticks"] <= current_measure_tick), [4, 4])
        
        numerator, denominator = time_sig
        ticks_per_measure = ticks_per_beat * 4 * numerator // denominator
        
        # Calculate measure time using tempo-aware timing
        measure_start_time = ticks_to_seconds(current_measure_tick, tempos, ticks_per_beat)
        measure_end_time = ticks_to_seconds(current_measure_tick + ticks_per_measure, tempos, ticks_per_beat)
        
        # Add small offset to match reference behavior
        tick_offset = 0.35 * ticks_per_measure / 480
        
        measures.append({
            "time": measure_start_time,
            "timeSignature": time_sig,
            "ticksPerMeasure": ticks_per_measure,
            "ticksStart": current_measure_tick + tick_offset,
            "totalTicks": ticks_per_measure + tick_offset,
            "type": 0 if len(measures) == 0 else 2
        })
        
        current_measure_tick += ticks_per_measure

    # Remove arbitrary scaling for supporting tracks
    supporting_tracks = []
    for track in tracks:
        supporting_track_notes = []
        for note in track.notes:
            # Note time and duration are already in seconds from get_notes_from_midi
            supporting_track_notes.append({
                "midi": note.midi,
                "time": note.time,
                "velocity": note.velocity,
                "duration": note.duration
            })
        
        supporting_tracks.append({
            "notes": supporting_track_notes,
            "myInstrument": track.myInstrument,
            "theirInstrument": track.theirInstrument
        })

    return {
        "supportingTracks": supporting_tracks,
        "start_time": 0,
        "song_length": song_length,
        "resolution": mid.ticks_per_beat,
        "tempos": extract_tempo_events(mid),
        "keySignatures": extract_key_signatures(mid),
        "timeSignatures": time_sigs,
        "measures": measures,
        "tracksV2": organize_tracks_v2(tracks, time_sigs, tempos),
        "accompanyingInstruments": [-2, -1],
        "accompanyingChannels": [0, 0],
        "name": title,
        "artist": artist,
        "accompanyingTracks": []
    }

def main():
    import sys
    if len(sys.argv) != 2:
        print("Usage: python midi_to_json.py <midi_file>")
        sys.exit(1)

    midi_path = sys.argv[1]
    output_json = create_piano_vision_json(midi_path)
    
    output_path = os.path.splitext(midi_path)[0] + '.json'
    with open(output_path, 'w') as f:
        json.dump(output_json, f)
    
    print(f"JSON file created: {output_path}")

if __name__ == "__main__":
    main()
