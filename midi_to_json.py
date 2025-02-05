import mido
import json
import os
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass

@dataclass
class Note:
    midi: int
    time: float
    velocity: float
    duration: float

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
    current_time = 0
    
    for msg in mid:
        current_time += msg.time
        if msg.type == 'time_signature':
            time_sigs.append({
                "ticks": int(current_time * mid.ticks_per_beat),
                "timeSignature": [msg.numerator, msg.denominator],
                "measures": len(time_sigs)
            })
    
    if not time_sigs:
        time_sigs.append({
            "ticks": 0,
            "timeSignature": [4, 4],
            "measures": 0
        })
    
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

def get_notes_from_midi(midi_path: str) -> Tuple[List[Track], float]:
    mid = mido.MidiFile(midi_path)
    tracks: List[List[Note]] = [[] for _ in range(2)]
    notes: Dict[Tuple[int, int], Tuple[int, float]] = {}
    current_tempo = 500000  # Default tempo
    max_time = 0.0
    
    # First pass: analyze channels to determine hand assignment
    right_hand_channel = None
    left_hand_channel = None
    
    for track_idx, track in enumerate(mid.tracks):
        highest_note = 0
        current_channel = None
        for msg in track:
            if hasattr(msg, 'channel'):
                current_channel = msg.channel
            if hasattr(msg, 'note') and msg.type == 'note_on' and msg.velocity > 0:
                highest_note = max(highest_note, msg.note)
        if current_channel is not None:
            if right_hand_channel is None or highest_note > 60:
                right_hand_channel = current_channel
            elif left_hand_channel is None:
                left_hand_channel = current_channel

    # Second pass: collect notes
    for track in mid.tracks:
        track_time = 0.0
        track_ticks = 0
        for msg in track:
            track_ticks += msg.time
            msg_time = (msg.time * current_tempo) / (mid.ticks_per_beat * 1000000)
            track_time += msg_time
            max_time = max(max_time, track_time)
            
            if msg.type == 'set_tempo':
                current_tempo = msg.tempo
                
            if hasattr(msg, 'channel'):
                current_channel = msg.channel
                if msg.type == 'note_on' and msg.velocity > 0:
                    notes[(current_channel, msg.note)] = (track_ticks, msg.velocity / 127.0)
                elif (msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0)):
                    if (current_channel, msg.note) in notes:
                        start_tick, velocity = notes[(current_channel, msg.note)]
                        duration_ticks = track_ticks - start_tick
                        
                        hand_idx = 0 if current_channel == right_hand_channel else 1
                        tracks[hand_idx].append(Note(
                            midi=msg.note,
                            time=start_tick,
                            velocity=velocity,
                            duration=duration_ticks
                        ))
                        del notes[(current_channel, msg.note)]

    final_tracks = []
    for hand_idx, notes in enumerate(tracks):
        if notes:
            final_tracks.append(Track(
                notes=sorted(notes, key=lambda x: x.time),
                myInstrument=-5,  # Piano
                theirInstrument=0
            ))

    return final_tracks, max_time

def organize_tracks_v2(tracks: List[Track], time_sigs: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    right_hand_notes = []
    left_hand_notes = []
    
    TICKS_PER_BEAT = 480
    
    for track_idx, track in enumerate(tracks):
        for note in track.notes:
            duration_beats = note.duration / TICKS_PER_BEAT
            start_beats = note.time / TICKS_PER_BEAT
            
            note_data = {
                "note": note.midi,
                "durationTicks": int(note.duration),
                "noteOffVelocity": 0,
                "ticksStart": int(note.time),
                "velocity": note.velocity,
                "measureBars": start_beats / 4,
                "duration": duration_beats,
                "noteName": get_note_name(note.midi),
                "octave": (note.midi // 12) - 1,
                "notePitch": get_note_name(note.midi).rstrip('0123456789'),
                "start": start_beats,
                "end": start_beats + duration_beats,
                "noteLengthType": "eighth",
                "group": 0,
                "measureInd": int(start_beats / 4),
                "noteMeasureInd": len(right_hand_notes) if track_idx == 0 else len(left_hand_notes),
                "id": f"{'r' if track_idx == 0 else 'l'}{len(right_hand_notes) if track_idx == 0 else len(left_hand_notes)}"
            }
            
            if track_idx == 0:
                right_hand_notes.append(note_data)
            else:
                left_hand_notes.append(note_data)
    
    measure_data = create_measure_data(time_sigs, right_hand_notes, left_hand_notes)
    
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
                       left_notes: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    measures_right = []
    measures_left = []
    
    if not time_sigs:
        return {"right": [], "left": []}
    
    # Calculate actual measure durations based on time signatures
    for i, time_sig in enumerate(time_sigs):
        numerator, denominator = time_sig["timeSignature"]
        measure_ticks = (480 * 4 * numerator) // denominator  # Standard MIDI resolution
        end_ticks = time_sigs[i + 1]["ticks"] if i + 1 < len(time_sigs) else time_sig["ticks"] + measure_ticks
        
        # Convert ticks to time
        start_time = time_sig["time"] if "time" in time_sig else 0
        end_time = end_ticks * (60 / (120 * 480))  # Convert using standard tempo if not specified
        
        measure_right_notes = [n for n in right_notes if start_time <= n["start"] < end_time]
        measure_left_notes = [n for n in left_notes if start_time <= n["start"] < end_time]
        
        # Create measure data using actual timings
        if measure_right_notes:
            measures_right.append({
                "direction": "down",
                "time": start_time,
                "timeEnd": end_time,
                "timeSignature": time_sig["timeSignature"],
                "notes": measure_right_notes,
                "max": max(n["note"] for n in measure_right_notes),
                "min": min(n["note"] for n in measure_right_notes),
                "measureTicksStart": time_sig["ticks"],
                "measureTicksEnd": end_ticks,
                "rests": calculate_rests(measure_right_notes, start_time, end_time)
            })
        
        # Same for left hand measures
        if measure_left_notes:
            measures_left.append({
                "direction": "down",
                "time": start_time,
                "timeEnd": end_time,
                "timeSignature": time_sig["timeSignature"],
                "notes": measure_left_notes,
                "max": max(n["note"] for n in measure_left_notes),
                "min": min(n["note"] for n in measure_left_notes),
                "measureTicksStart": time_sig["ticks"],
                "measureTicksEnd": end_ticks,
                "rests": calculate_rests(measure_left_notes, start_time, end_time)
            })
    
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

def get_midi_timings(midi_file: str) -> Dict[int, float]:
    midi = mido.MidiFile(midi_file)
    ticks_per_beat = midi.ticks_per_beat
    tempo = 500000  # Default tempo (microseconds per beat)
    time_signature = (4, 4)  # Default time signature
    ticks_per_measure = ticks_per_beat * time_signature[0]

    # Collect all MIDI events
    events = []
    for track in midi.tracks:
        absolute_tick = 0
        for msg in track:
            absolute_tick += msg.time
            events.append((absolute_tick, msg))

    # Sort events by their absolute tick count
    events.sort(key=lambda x: x[0])

    midi_timings = {1: 0.0}  # Measure 1 starts at t=0
    current_measure = 1
    current_time = 0.0
    current_ticks = 0
    measure_start_ticks = 0
    next_measure_ticks = ticks_per_measure

    for event_ticks, msg in events:
        # Calculate time up to this event
        delta_ticks = event_ticks - current_ticks
        current_time += (delta_ticks * tempo) / (1000000 * ticks_per_beat)
        current_ticks = event_ticks

        # Check if we've reached or passed measure boundaries
        while current_ticks >= next_measure_ticks:
            current_measure += 1
            measure_start_time = current_time - ((current_ticks - next_measure_ticks) * tempo) / (1000000 * ticks_per_beat)
            midi_timings[current_measure] = measure_start_time
            measure_start_ticks = next_measure_ticks
            next_measure_ticks += ticks_per_measure

        # Process tempo and time signature changes
        if msg.type == 'set_tempo':
            tempo = msg.tempo
        elif msg.type == 'time_signature':
            # Update time signature and ticks per measure
            ticks_per_measure = ticks_per_beat * 4 * msg.numerator // msg.denominator
            
            # Adjust the next measure boundary
            next_measure_ticks = measure_start_ticks + ticks_per_measure

    return midi_timings

def create_piano_vision_json(midi_path: str) -> Dict[str, Any]:
    mid = mido.MidiFile(midi_path)
    tracks, song_length = get_notes_from_midi(midi_path)
    
    # Extract metadata from filename and full path
    filename = os.path.basename(midi_path)
    title = os.path.splitext(filename)[0].replace('_', ' ')
    
    # Get the full path to extract correct artist name
    parent_dir = os.path.dirname(midi_path)
    grandparent_dir = os.path.dirname(parent_dir)
    artist = os.path.basename(parent_dir)
    
    # If artist name is generic, try one level up
    if artist.lower() in ['film', 'music', 'piano', 'midi']:
        artist = os.path.basename(grandparent_dir)
    
    time_sigs = extract_time_signatures(mid)
    measure_timings = get_midi_timings(midi_path)
    
    measures = []
    sorted_measures = sorted(measure_timings.items())
    ticks_per_beat = mid.ticks_per_beat
    current_measure_tick = 0
    
    for i in range(len(sorted_measures)):
        measure_num, start_time = sorted_measures[i]
        
        time_sig = next((ts["timeSignature"] for ts in reversed(time_sigs) 
                        if ts["ticks"] <= current_measure_tick), [4, 4])
        
        numerator, denominator = time_sig
        ticks_per_measure = ticks_per_beat * 4 * numerator // denominator
        
        measures.append({
            "time": start_time,
            "timeSignature": time_sig,
            "ticksPerMeasure": ticks_per_measure,
            "ticksStart": current_measure_tick,
            "totalTicks": ticks_per_measure,  # No arbitrary offset
            "type": 0
        })
        
        current_measure_tick += ticks_per_measure

    # Remove arbitrary scaling for supporting tracks
    supporting_tracks = []
    for track in tracks:
        supporting_track_notes = []
        for note in track.notes:
            duration_in_beats = note.duration
            supporting_track_notes.append({
                "midi": note.midi,
                "time": note.time,
                "velocity": note.velocity,
                "duration": duration_in_beats
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
        "tracksV2": organize_tracks_v2(tracks, time_sigs),
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
        json.dump(output_json, f, indent=2)
    
    print(f"JSON file created: {output_path}")

if __name__ == "__main__":
    main()
