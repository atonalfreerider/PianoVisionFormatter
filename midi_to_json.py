import xml.etree.ElementTree as ET
import mido
import json
import os
from typing import List, Dict, Any, Tuple
from pv_util import extract_metadata_from_musescore, format_output_filename, extract_mscx_from_mscz, ticks_to_seconds, extract_accented_notes
from notes import Note, Track
from track_organizer import get_note_name, get_note_length_type, calculate_rests

def extract_tempo_events(mid: mido.MidiFile) -> List[Dict[str, Any]]:
    """Extract tempo events from MIDI with improved reliability"""
    tempo_events = []
    last_time = 0.0
    
    # First, collect all tempo events from all tracks
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            
            if msg.type == 'set_tempo':
                tempo_events.append({
                    'tick': abs_tick,
                    'tempo': msg.tempo
                })
    
    # Sort tempo events by tick position
    tempo_events.sort(key=lambda x: x['tick'])
    
    # Ensure we have a tempo at tick 0
    if not tempo_events or tempo_events[0]['tick'] > 0:
        tempo_events.insert(0, {
            'tick': 0,
            'tempo': 500000  # 120 BPM
        })
    
    # Convert to the format we need
    formatted_tempos = []
    for i, event in enumerate(tempo_events):
        if i > 0:
            # Calculate time based on previous tempo
            prev = tempo_events[i-1]
            delta_ticks = event['tick'] - prev['tick']
            delta_time = (delta_ticks * prev['tempo']) / (mid.ticks_per_beat * 1000000)
            last_time += delta_time
        
        formatted_tempos.append({
            "bpm": 60000000 / event['tempo'],
            "ticks": event['tick'],
            "time": last_time
        })
    
    return formatted_tempos

def extract_time_signatures(mid: mido.MidiFile) -> List[Dict[str, Any]]:
    """Extract time signatures from MIDI"""
    time_sigs = []
    
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == 'time_signature':
                time_sigs.append({
                    "ticks": abs_tick,
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
    """Extract key signatures from MIDI"""
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
    """Extract notes from MIDI with improved staff assignment"""
    mid = mido.MidiFile(midi_path)
    tracks: List[List[Note]] = [[] for _ in range(2)]  # Right hand (1), Left hand (2)
    notes: Dict[Tuple[int, int], List[Tuple[int, float, float, int]]] = {}  # Channel, note -> [(tick, time, velocity, midi_note)]
    pending_notes = {0: [], 1: []}  # Collect notes that start at the same time for each hand
    tempos = extract_tempo_events(mid)
    max_time = 0.0
    
    # Check for accent information from MuseScore
    accented_notes = {}
    current_measure = 0
    
    # Try matching MuseScore file
    matching_mscore = find_matching_musescore(midi_path)
    if matching_mscore:
        print(f"Found matching MuseScore file for accent analysis: {matching_mscore}")
        mscx_content = extract_mscx_from_mscz(matching_mscore)
        if mscx_content:
            accented_notes = extract_accented_notes(mscx_content)
            print(f"Found {len(accented_notes)} accented note patterns in score")

    # Improved piano track detection with channel-based allocation
    piano_tracks = []
    track_channels = {}
    
    # First pass: identify piano tracks by looking for "piano" in track names or program changes
    for track_idx, track in enumerate(mid.tracks):
        found_piano = False
        used_channels = set()
        
        for msg in track:
            if hasattr(msg, 'channel'):
                used_channels.add(msg.channel)
                
            if msg.type == 'track_name' and 'piano' in msg.name.lower():
                found_piano = True
            elif msg.type == 'program_change' and 0 <= msg.program <= 7:  # Piano family
                found_piano = True
                
        if found_piano:
            piano_tracks.append(track_idx)
            track_channels[track_idx] = used_channels
    
    # If no piano tracks found, use first two tracks if available, or all tracks
    if not piano_tracks:
        if len(mid.tracks) >= 2:
            piano_tracks = [0, 1]
        else:
            piano_tracks = list(range(len(mid.tracks)))

    last_tick = 0
    for track_idx, track in enumerate(mid.tracks):
        if track_idx not in piano_tracks:
            continue

        track_ticks = 0
        for msg in track:
            track_ticks += msg.time
            
            # Note start
            if msg.type == 'note_on' and msg.velocity > 0:
                # Store note start info
                if track_ticks != last_tick:
                    # Process and clear pending notes for both hands
                    for hand_idx in [0, 1]:
                        if pending_notes[hand_idx]:
                            process_chord(pending_notes[hand_idx], hand_idx, tracks, 
                                       accented_notes, current_measure)
                            pending_notes[hand_idx] = []
                
                hand_idx = 0 if piano_tracks.index(track_idx) == 0 else 1
                pending_notes[hand_idx].append((
                    track_ticks,
                    ticks_to_seconds(track_ticks, tempos, mid.ticks_per_beat),
                    msg.velocity / 127.0,
                    msg.note
                ))
                last_tick = track_ticks
            
            # Note end
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                # Process note end
                end_time = ticks_to_seconds(track_ticks, tempos, mid.ticks_per_beat)
                hand_idx = 0 if piano_tracks.index(track_idx) == 0 else 1
                process_note_end(track_ticks, end_time, msg.note, hand_idx, tracks)

            # Update measure tracking
            if track_ticks >= (current_measure + 1) * mid.ticks_per_beat * 4:
                current_measure = track_ticks // (mid.ticks_per_beat * 4)
    
    # Process any remaining pending notes
    for hand_idx in [0, 1]:
        if pending_notes[hand_idx]:
            process_chord(pending_notes[hand_idx], hand_idx, tracks, 
                        accented_notes, current_measure)

    # Create final tracks
    final_tracks = []
    for hand_idx, hand_notes in enumerate(tracks):
        if hand_notes:
            final_tracks.append(Track(
                notes=sorted(hand_notes, key=lambda x: x.time),
                myInstrument=-5,  # Piano
                theirInstrument=0
            ))

    return final_tracks, max_time

def process_chord(pending_notes: List[Tuple[int, float, float, int]], 
                 hand_idx: int, 
                 tracks: List[List[Note]], 
                 accented_notes: Dict[Tuple[int, int, int, List[int]], List[int]],
                 measure_idx: int) -> None:
    """Process a group of notes that start at the same time (a chord)"""
    if not pending_notes:
        return
        
    # Sort notes by pitch for consistent matching
    pending_notes.sort(key=lambda x: x[3])  # Sort by MIDI note number
    pitches = [note[3] for note in pending_notes]
    
    # Try to find matching chord pattern in accent data
    position_key = (hand_idx + 1, measure_idx, 0, tuple(pitches))
    accented_indices = accented_notes.get(position_key, [])
    
    # Create notes with accent information
    for i, (tick, time, velocity, pitch) in enumerate(pending_notes):
        is_accented = i in accented_indices
        
        if is_accented and velocity < 0.9:
            velocity = min(1.0, velocity * 1.25)  # Boost accented notes
        
        # Add note to appropriate track
        tracks[hand_idx].append(Note(
            midi=pitch,
            time=time,
            velocity=velocity,
            duration=0,  # Will be set by process_note_end
            ticks=tick,
            duration_ticks=0,  # Will be set by process_note_end
            staff=hand_idx + 1,
            group=measure_idx,
            accent=1 if is_accented else 0
        ))

def process_note_end(tick: int, end_time: float, pitch: int, hand_idx: int, 
                    tracks: List[List[Note]]) -> None:
    """Process the end of a note by setting its duration"""
    # Find the matching note start
    for note in reversed(tracks[hand_idx]):
        if note.midi == pitch and note.duration == 0:
            note.duration = end_time - note.time
            note.duration_ticks = tick - note.ticks
            break

def organize_tracks_v2(tracks: List[Track], time_sigs: List[Dict[str, Any]], tempos: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Organize tracks into measures for tracksV2 format"""
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
                "id": f"{'r' if track_idx == 0 else 'l'}{len(right_hand_notes) if track_idx == 0 else len(left_hand_notes)}",
                "accent": 1 if note.accent else 0
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

def create_measure_data(time_sigs: List[Dict[str, Any]], 
                       right_notes: List[Dict[str, Any]], 
                       left_notes: List[Dict[str, Any]],
                       tempos: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Create measure data for both hands"""
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
                "rests": calculate_rests(start_time, end_time, measure_right_notes),
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
                "rests": calculate_rests(start_time, end_time, measure_left_notes),
                "type": 0 if measure_idx == 0 else 2
            })
        
        current_measure_tick = end_tick
        measure_idx += 1

    return {
        "right": measures_right,
        "left": measures_left
    }

def create_piano_vision_json(midi_path: str) -> Dict[str, Any]:
    mid = mido.MidiFile(midi_path)
    tempos = extract_tempo_events(mid)
    tracks, song_length = get_notes_from_midi(midi_path)
    
    # Look for matching MusicXML file for metadata
    matching_mscz = find_matching_musescore(midi_path)
    mscx_content = extract_mscx_from_mscz(matching_mscz)
    
    if mscx_content:
        # Use metadata from MusicXML if available
        root = ET.fromstring(mscx_content)
        title, artist = extract_metadata_from_musescore(root)
        print(f"Using metadata from matching MuseScore file: {matching_mscz}")
    else:
        # Extract metadata from filename and directory
        filename = os.path.basename(midi_path)
        title = os.path.splitext(filename)[0].replace('_', ' ')
        artist = os.path.basename(os.path.dirname(midi_path))

    time_sigs = extract_time_signatures(mid)
    key_sigs = extract_key_signatures(mid)

    # Get total number of measures from tracks
    tracks_v2 = organize_tracks_v2(tracks, time_sigs, tempos)
    
    # Create measure list
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

    # Create supporting tracks
    supporting_tracks = []
    for track in tracks:
        supporting_track_notes = []
        for note in track.notes:
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
        "tempos": tempos,
        "keySignatures": key_sigs,
        "timeSignatures": time_sigs,
        "measures": measures,
        "tracksV2": tracks_v2,
        "accompanyingInstruments": [-2, -1],
        "accompanyingChannels": [0, 0],
        "name": title,
        "artist": artist,
        "accompanyingTracks": []
    }

def find_matching_musescore(midi_path: str) -> str:
    """Look for a matching MusicXML file for a given MIDI file"""
    base_name = os.path.splitext(os.path.basename(midi_path))[0]
    parent_dir = os.path.dirname(midi_path)

    # Try common MusicXML extensions
    xml_extensions = ['.mscz', '.mscx']

    for ext in xml_extensions:
        potential_path = os.path.join(parent_dir, base_name + ext)
        if os.path.exists(potential_path):
            return potential_path

    return None

def main():
    import sys
    
    # Handle command line arguments
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: python midi_to_json.py <input_file> [output_dir]")
        sys.exit(1)

    midi_path = sys.argv[1]

    # If no output directory is specified, use the same directory as the input file
    if len(sys.argv) == 3:
        output_dir = sys.argv[2]
    else:
        output_dir = os.path.dirname(midi_path)

    if not os.path.isfile(midi_path):
        print(f"Error: {midi_path} is not a file")
        sys.exit(1)

    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        output_json = create_piano_vision_json(midi_path)
        
        # Generate formatted output filename
        output_filename = format_output_filename(
            output_json['name'],
            output_json['artist'],
            midi_path
        )
        
        # Create output path in output directory
        output_path = os.path.join(output_dir, output_filename)
        
        with open(output_path, 'w') as f:
            json.dump(output_json, f)
        
        print(f"Converted: {midi_path} -> {output_path}")
    except Exception as e:
        print(f"Error processing {midi_path}: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
