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
    current_time = 0
    
    for msg in mid:
        current_time += msg.time
        if msg.type == 'set_tempo':
            tempo_events.append({
                "bpm": 60000000 / msg.tempo,
                "ticks": int(current_time * mid.ticks_per_beat),
                "time": current_time
            })
    
    if not tempo_events:
        tempo_events.append({
            "bpm": 120,
            "ticks": 0,
            "time": 0
        })
    
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
    notes: Dict[Tuple[int, int], Tuple[float, float]] = {}  # (channel, note) -> (start_time, velocity)
    current_time = 0
    tracks: List[List[Note]] = [[] for _ in range(16)]  # One list per MIDI channel
    
    for msg in mid:
        current_time += msg.time
        if hasattr(msg, 'channel'):
            if msg.type == 'note_on' and msg.velocity > 0:
                notes[(msg.channel, msg.note)] = (current_time, msg.velocity / 127.0)
            elif (msg.type == 'note_off') or (msg.type == 'note_on' and msg.velocity == 0):
                if (msg.channel, msg.note) in notes:
                    start_time, velocity = notes[(msg.channel, msg.note)]
                    duration = current_time - start_time
                    tracks[msg.channel].append(Note(
                        midi=msg.note,
                        time=start_time,
                        velocity=velocity,
                        duration=duration
                    ))
                    del notes[(msg.channel, msg.note)]
    
    # Filter out empty tracks and convert to Track objects
    final_tracks = []
    for channel, notes in enumerate(tracks):
        if notes:
            final_tracks.append(Track(
                notes=sorted(notes, key=lambda x: x.time),
                myInstrument=-5,  # Default piano instrument
                theirInstrument=0
            ))
    
    return final_tracks, current_time

def organize_tracks_v2(tracks: List[Track], time_sigs: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    right_hand_notes = []
    left_hand_notes = []
    
    # Combine all notes and sort by time
    all_notes = []
    for track in tracks:
        all_notes.extend(track.notes)
    all_notes.sort(key=lambda x: x.time)
    
    # Split notes between hands (middle C = 60)
    for note in all_notes:
        note_data = {
            "note": note.midi,
            "durationTicks": int(note.duration * 480),  # Convert to ticks
            "noteOffVelocity": 0,
            "ticksStart": int(note.time * 480),  # Convert to ticks
            "velocity": note.velocity,
            "measureBars": note.time / 2,  # Assuming 4/4 time
            "duration": note.duration,
            "noteName": get_note_name(note.midi),
            "octave": (note.midi // 12) - 1,
            "notePitch": get_note_name(note.midi)[0],
            "start": note.time,
            "end": note.time + note.duration,
            "noteLengthType": "quarter",  # This would need more precise calculation
            "group": -1,
            "measureInd": int(note.time / 2),  # Assuming 4/4 time
            "noteMeasureInd": len(right_hand_notes) if note.midi >= 60 else len(left_hand_notes),
            "id": f"{'r' if note.midi >= 60 else 'l'}{len(right_hand_notes) if note.midi >= 60 else len(left_hand_notes)}"
        }
        
        if note.midi >= 60:  # Middle C and above
            right_hand_notes.append(note_data)
        else:
            left_hand_notes.append(note_data)
    
    # Create measure sections based on time signatures
    measure_data = create_measure_data(time_sigs, right_hand_notes, left_hand_notes)
    
    return {
        "right": measure_data["right"],
        "left": measure_data["left"]
    }

def get_note_name(midi_note: int) -> str:
    notes = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    return notes[midi_note % 12]

def create_measure_data(time_sigs: List[Dict[str, Any]], 
                       right_notes: List[Dict[str, Any]], 
                       left_notes: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    measures_right = []
    measures_left = []
    
    if not time_sigs:
        return {"right": [], "left": []}
        
    # Calculate measure durations based on time signatures
    measure_boundaries = []
    current_time = 0
    
    for i in range(len(time_sigs)):
        start_time = current_time
        # For last measure, use next measure's start or add one full measure
        if i + 1 < len(time_sigs):
            end_time = time_sigs[i + 1]["ticks"] / 480
        else:
            # Last measure: add one full measure duration (4 beats in 4/4 time)
            beats_per_measure = time_sigs[i]["timeSignature"][0]
            end_time = start_time + (beats_per_measure * 4)
        
        measure_boundaries.append((start_time, end_time))
        current_time = end_time
    
    for start_time, end_time in measure_boundaries:
        measure_right_notes = [n for n in right_notes if start_time <= n["start"] < end_time]
        measure_left_notes = [n for n in left_notes if start_time <= n["start"] < end_time]
        
        if measure_right_notes:
            measures_right.append({
                "direction": "up",
                "time": start_time,
                "timeEnd": end_time,
                "timeSignature": time_sigs[measure_boundaries.index((start_time, end_time))]["timeSignature"],
                "notes": measure_right_notes,
                "max": max(n["note"] for n in measure_right_notes),
                "min": min(n["note"] for n in measure_right_notes),
                "measureTicksStart": time_sigs[measure_boundaries.index((start_time, end_time))]["ticks"],
                "measureTicksEnd": time_sigs[measure_boundaries.index((start_time, end_time)) + 1]["ticks"] if measure_boundaries.index((start_time, end_time)) + 1 < len(time_sigs) else time_sigs[measure_boundaries.index((start_time, end_time))]["ticks"] + 1920,
                "rests": []  # Would need additional calculation for rests
            })
            
        if measure_left_notes:
            measures_left.append({
                "direction": "down",
                "time": start_time,
                "timeEnd": end_time,
                "timeSignature": time_sigs[measure_boundaries.index((start_time, end_time))]["timeSignature"],
                "notes": measure_left_notes,
                "max": max(n["note"] for n in measure_left_notes),
                "min": min(n["note"] for n in measure_left_notes),
                "measureTicksStart": time_sigs[measure_boundaries.index((start_time, end_time))]["ticks"],
                "measureTicksEnd": time_sigs[measure_boundaries.index((start_time, end_time)) + 1]["ticks"] if measure_boundaries.index((start_time, end_time)) + 1 < len(time_sigs) else time_sigs[measure_boundaries.index((start_time, end_time))]["ticks"] + 1920,
                "rests": []  # Would need additional calculation for rests
            })
        
    return {
        "right": measures_right,
        "left": measures_left
    }

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
    
    # Extract metadata from filename and path
    filename = os.path.basename(midi_path)
    title = os.path.splitext(filename)[0].replace('_', ' ')
    author = os.path.basename(os.path.dirname(midi_path))
    
    time_sigs = extract_time_signatures(mid)
    measure_timings = get_midi_timings(midi_path)
    
    # Create measures list from timings with modified structure
    measures = []
    sorted_measures = sorted(measure_timings.items())
    ticks_per_beat = mid.ticks_per_beat
    
    for i in range(len(sorted_measures)):
        measure_num, start_time = sorted_measures[i]
        end_time = sorted_measures[i + 1][1] if i + 1 < len(sorted_measures) else song_length
        
        # Find the time signature for this measure
        time_sig = next((ts["timeSignature"] for ts in reversed(time_sigs) 
                        if ts["ticks"] <= start_time * ticks_per_beat), [4, 4])
        
        # Calculate ticksPerMeasure based on time signature
        ticks_per_measure = ticks_per_beat * 4 * time_sig[0] // time_sig[1]
        
        measures.append({
            "time": start_time,
            "timeSignature": time_sig,
            "ticksPerMeasure": ticks_per_measure,
            "ticksStart": start_time * ticks_per_beat,
            "type": 2,  # This appears to be a constant in the reference
            "totalTicks": ticks_per_measure  # This might need adjustment based on actual measure length
        })

    return {
        "supportingTracks": [
            {
                "notes": [
                    {
                        "midi": note.midi,
                        "time": note.time,
                        "velocity": note.velocity,
                        "duration": note.duration
                    }
                    for note in track.notes
                ],
                "myInstrument": track.myInstrument,
                "theirInstrument": track.theirInstrument
            }
            for track in tracks
        ],
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
        "artist": author,
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
