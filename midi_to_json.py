import xml.etree.ElementTree as ET
import mido
import json
import os
from typing import List, Dict, Any, Tuple, Set
from pv_util import extract_metadata_from_musescore, format_output_filename, extract_mscx_from_mscz, ticks_to_seconds
from notes import Note, Track
from track_organizer import get_note_name, get_note_length_type, calculate_rests
from orchestra_utils import is_valid_orchestra_instrument

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


def calculate_measure_map(mid: mido.MidiFile) -> List[Dict[str, Any]]:
    """Calculate precise measure boundaries based on time signatures in the MIDI file"""
    # Extract time signatures
    time_signatures = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == 'time_signature':
                time_signatures.append({
                    'tick': abs_tick,
                    'numerator': msg.numerator,
                    'denominator': msg.denominator
                })
    
    # Sort by tick position
    time_signatures.sort(key=lambda x: x['tick'])
    
    # Default to 4/4 if no time signature found
    if not time_signatures:
        time_signatures.append({'tick': 0, 'numerator': 4, 'denominator': 4})
    
    # Calculate measure boundaries
    measures = []
    current_tick = 0
    current_measure = 1
    current_time_sig_idx = 0
    
    # Calculate the total duration in ticks
    max_tick = 0
    for track in mid.tracks:
        track_ticks = 0
        for msg in track:
            track_ticks += msg.time
        max_tick = max(max_tick, track_ticks)
    
    # Generate measure map
    while current_tick < max_tick:
        # Get current time signature
        while (current_time_sig_idx + 1 < len(time_signatures) and 
               time_signatures[current_time_sig_idx + 1]['tick'] <= current_tick):
            current_time_sig_idx += 1
        
        time_sig = time_signatures[current_time_sig_idx]
        
        # Calculate length of this measure in ticks
        ticks_per_beat = mid.ticks_per_beat
        beats_per_measure = time_sig['numerator']
        measure_length = ticks_per_beat * 4 * beats_per_measure // time_sig['denominator']
        
        measures.append({
            'measure_num': current_measure,
            'start_tick': current_tick,
            'end_tick': current_tick + measure_length,
            'time_signature': (time_sig['numerator'], time_sig['denominator'])
        })
        
        current_tick += measure_length
        current_measure += 1
    
    return measures

def get_notes_from_midi(midi_path: str,
                       orchestra_mode: bool = False, 
                       simplified_mode: bool = False,
                       merge_measures: Dict[str, Set[int]] = None) -> Tuple[List[Track], float]:
    """Extract notes from MIDI preserving original hand assignments with optional orchestra infill"""
    mid = mido.MidiFile(midi_path)
    primary_tracks: List[List[Note]] = [[], []]  # Right hand, Left hand
    simplified_tracks: List[List[Note]] = [[], []]  # Simplified RH, LH
    orchestral_tracks: List[List[Note]] = [[], []]  # Orchestral RH, LH
    other_orchestra_tracks: List[List[Note]] = [[], []]  # Other orchestra instruments RH, LH
    tempos = extract_tempo_events(mid)
    max_time = 0.0

    # Look for matching MusicXML file for merge markers
    if merge_measures is None:
        merge_measures = {'right': set(), 'left': set()}
    matching_musescore = find_matching_musescore(midi_path)
    if matching_musescore:
        try:
            # Extract only merge measures information
            mscx_content = extract_mscx_from_mscz(matching_musescore)
            root = ET.fromstring(mscx_content)
            _, _, merge_measures = extract_metadata_from_musescore(root)

            if merge_measures['right'] or merge_measures['left']:
                print(f"Found merge markers: {len(merge_measures['right'])} measures for right hand, "
                      f"{len(merge_measures['left'])} measures for left hand")
        except Exception as e:
            print(f"Error extracting merge markers: {e}")

    # Calculate measure map for accurate measure detection
    measure_map = calculate_measure_map(mid)
    
    # First pass: Identify track types and their channels
    piano_tracks = []  # [(track_idx, track_type)]
    orchestra_tracks = []  # [(track_idx, instrument)]
    track_channels = {}  # track_idx -> set of channels used

    # Track type constants
    TRACK_PRIMARY = 1
    TRACK_SIMPLIFIED = 2
    TRACK_ORCHESTRAL = 3

    # First pass: identify piano tracks by looking for "piano" in track names or program changes
    for track_idx, track in enumerate(mid.tracks):
        found_piano = False
        track_type = TRACK_PRIMARY
        used_channels = set()
        
        for msg in track:
            if hasattr(msg, 'channel'):
                used_channels.add(msg.channel)
                
            if msg.type == 'track_name':
                name = msg.name.lower()
                if 'piano-simplified' in name:
                    track_type = TRACK_SIMPLIFIED
                    found_piano = True
                elif 'piano-orchestral' in name:
                    track_type = TRACK_ORCHESTRAL
                    found_piano = True
                elif 'piano' in name:
                    track_type = TRACK_PRIMARY
                    found_piano = True
                    
            elif msg.type == 'program_change' and 0 <= msg.program <= 7:  # Piano family
                found_piano = True
                
        if found_piano:
            piano_tracks.append((track_idx, track_type))
            track_channels[track_idx] = used_channels
        elif orchestra_mode:
            # Only collect orchestra tracks if in orchestra mode
            for msg in track:
                if msg.type == 'program_change' and is_valid_orchestra_instrument(msg.program):
                    orchestra_tracks.append((track_idx, msg.program))
                    track_channels[track_idx] = used_channels
                    break
    
    # If no piano tracks found, use first two tracks
    if not piano_tracks:
        piano_tracks = [(0, TRACK_PRIMARY)]
        if len(mid.tracks) > 1:
            piano_tracks.append((1, TRACK_PRIMARY))

    # Process notes from primary piano tracks
    for track_idx, track_type in piano_tracks:
        track = mid.tracks[track_idx]
        active_notes = {}  # (channel, note) -> (start_tick, start_time, velocity)
        track_ticks = 0
        
        for msg in track:
            track_ticks += msg.time
            track_time = ticks_to_seconds(track_ticks, tempos, mid.ticks_per_beat)
            max_time = max(max_time, track_time)
            
            if not hasattr(msg, 'channel'):
                continue
                
            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[(msg.channel, msg.note)] = (track_ticks, track_time, msg.velocity / 127.0)
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                if (msg.channel, msg.note) in active_notes:
                    start_tick, start_time, velocity = active_notes[(msg.channel, msg.note)]
                    duration_seconds = track_time - start_time
                    duration_ticks = track_ticks - start_tick
                    
                    # Determine hand based on track position
                    hand_idx = 0  # Default to right hand
                    
                    # Determine the track type and hand assignment
                    track_type = next(t[1] for t in piano_tracks if t[0] == track_idx)
                    
                    # If we have at least 2 piano tracks of the same type, use track index for hand assignment
                    same_type_tracks = [t[0] for t in piano_tracks if t[1] == track_type]
                    if len(same_type_tracks) >= 2:
                        hand_idx = 0 if same_type_tracks.index(track_idx) == 0 else 1
                    
                    note = Note(
                        midi=msg.note,
                        time=start_time,
                        velocity=velocity,
                        duration=duration_seconds,
                        ticks=start_tick,
                        duration_ticks=duration_ticks,
                        staff=hand_idx + 1,
                        group=-1,
                    )
                    
                    # Add note to the appropriate track based on type
                    if track_type == TRACK_PRIMARY:
                        primary_tracks[hand_idx].append(note)
                    elif track_type == TRACK_SIMPLIFIED:
                        simplified_tracks[hand_idx].append(note)
                    elif track_type == TRACK_ORCHESTRAL:
                        orchestral_tracks[hand_idx].append(note)
                    
                    del active_notes[(msg.channel, msg.note)]
    
    # Process orchestra instrument tracks if needed
    if orchestra_mode:
        for track_idx, _ in orchestra_tracks:
            track = mid.tracks[track_idx]
            active_notes = {}  # (channel, note) -> (start_tick, start_time, velocity)
            track_ticks = 0
            
            for msg in track:
                track_ticks += msg.time
                track_time = ticks_to_seconds(track_ticks, tempos, mid.ticks_per_beat)
                max_time = max(max_time, track_time)
                
                if not hasattr(msg, 'channel'):
                    continue
                    
                if msg.type == 'note_on' and msg.velocity > 0:
                    active_notes[(msg.channel, msg.note)] = (track_ticks, track_time, msg.velocity / 127.0)
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    if (msg.channel, msg.note) in active_notes:
                        start_tick, start_time, velocity = active_notes[(msg.channel, msg.note)]
                        duration_seconds = track_time - start_time
                        duration_ticks = track_ticks - start_tick
                        
                        # Since these are orchestra tracks, use note-based assignment if needed
                        hand_idx = 0 if msg.note >= 60 else 1
                            
                        note = Note(
                            midi=msg.note,
                            time=start_time,
                            velocity=velocity,
                            duration=duration_seconds,
                            ticks=start_tick,
                            duration_ticks=duration_ticks,
                            staff=hand_idx + 1,
                            group=-1,
                        )
                        
                        # Add to orchestra instruments collection
                        other_orchestra_tracks[hand_idx].append(note)
                        del active_notes[(msg.channel, msg.note)]

    # Create the final result tracks based on mode flags and measure information
    result_tracks = merge_tracks_with_measure_info(
        primary_tracks,
        simplified_tracks,
        orchestral_tracks, 
        other_orchestra_tracks,
        mid,
        measure_map,
        simplified_mode,
        orchestra_mode,
        merge_measures
    )

    # Create final Track objects
    final_tracks = []
    for hand_idx, hand_notes in enumerate(result_tracks):
        if hand_notes:
            final_tracks.append(Track(
                notes=sorted(hand_notes, key=lambda x: x.time),
                myInstrument=-5,
                theirInstrument=0
            ))

    return final_tracks, max_time

def merge_tracks_with_measure_info(
    primary_tracks: List[List[Note]],
    simplified_tracks: List[List[Note]],
    orchestral_tracks: List[List[Note]],
    other_orchestra_tracks: List[List[Note]],
    mid: mido.MidiFile,
    measure_map: List[Dict[str, Any]],
    simplified_mode: bool,
    orchestra_mode: bool,
    merge_measures: Dict[str, Set[int]]
) -> List[List[Note]]:
    """Merge tracks using accurate measure information"""
    result_tracks = [[], []]  # RH, LH hands
    
    # Debug function to get measure number for a tick position using accurate measure map
    def get_measure_for_tick(tick_pos):
        for measure in measure_map:
            if measure['start_tick'] <= tick_pos < measure['end_tick']:
                return measure['measure_num']
        # If no match found (could be past the end), use the last measure + overflow calculation
        if measure_map and tick_pos >= measure_map[-1]['end_tick']:
            last_measure = measure_map[-1]
            overflow_ticks = tick_pos - last_measure['end_tick']
            measure_length = last_measure['end_tick'] - last_measure['start_tick']
            if measure_length > 0:
                overflow_measures = overflow_ticks // measure_length
                return last_measure['measure_num'] + overflow_measures + 1
        # Fallback to simple calculation if all else fails
        return (tick_pos // (mid.ticks_per_beat * 4)) + 1
    
    # Start with primary tracks as the base for both hands
    for hand_idx in [0, 1]:
        result_tracks[hand_idx] = primary_tracks[hand_idx].copy()

    # Step 1: If simplified mode is on, replace measures that have simplified notes
    if simplified_mode and any(simplified_tracks):
        for hand_idx in [0, 1]:

            if simplified_tracks[hand_idx]:  # Only process if there are simplified notes
                # Group notes by measure using accurate measure calculation
                primary_by_measure = {}
                simplified_by_measure = {}
                
                # Group primary notes by measure number
                for note in primary_tracks[hand_idx]:
                    measure_num = get_measure_for_tick(note.ticks)
                    if measure_num not in primary_by_measure:
                        primary_by_measure[measure_num] = []
                    primary_by_measure[measure_num].append(note)
                
                # Group simplified notes by measure number
                simplified_measure_counts = {}
                for note in simplified_tracks[hand_idx]:
                    measure_num = get_measure_for_tick(note.ticks)
                    if measure_num not in simplified_by_measure:
                        simplified_by_measure[measure_num] = []
                        simplified_measure_counts[measure_num] = 0
                    simplified_by_measure[measure_num].append(note)
                    simplified_measure_counts[measure_num] += 1

                # Create new track with measure-based selection
                new_track = []
                all_measures = set(list(primary_by_measure.keys()) + list(simplified_by_measure.keys()))
                
                for measure_num in sorted(all_measures):
                    use_simplified = False
                    
                    # Always use simplified if we have them and simplified mode is on
                    # (We're not relying on XML data for measure analysis as requested)
                    if measure_num in simplified_by_measure and simplified_by_measure[measure_num]:
                        use_simplified = True

                    if use_simplified:
                        new_track.extend(simplified_by_measure[measure_num])
                    elif measure_num in primary_by_measure:
                         new_track.extend(primary_by_measure[measure_num])
                
                # Replace with merged result
                result_tracks[hand_idx] = sorted(new_track, key=lambda note: note.ticks)
    
    # Step 2: Handle orchestra parts
    if orchestra_mode or any(merge_measures.values()):
        for hand_idx in [0, 1]:
            hand_key = 'right' if hand_idx == 0 else 'left'
            
            # Group result notes (after simplified replacement) by measure
            result_by_measure = {}
            
            for note in result_tracks[hand_idx]:
                measure_num = get_measure_for_tick(note.ticks)
                if measure_num not in result_by_measure:
                    result_by_measure[measure_num] = []
                result_by_measure[measure_num].append(note)
            
            # Step 2A: Handle measures with explicit merge markers first
            if merge_measures[hand_key]:
                # Group orchestral notes by measure
                orchestral_by_measure = {}
                if orchestral_tracks[hand_idx]:  # These are piano-orchestral tracks
                    for note in orchestral_tracks[hand_idx]:
                        measure_num = get_measure_for_tick(note.ticks)
                        if measure_num not in orchestral_by_measure:
                            orchestral_by_measure[measure_num] = []
                        orchestral_by_measure[measure_num].append(note)
                
                # Add orchestral notes for merge measures, even when primary notes exist
                merged_notes = []
                for measure_num in merge_measures[hand_key]:
                    if measure_num in orchestral_by_measure:
                        orch_notes = orchestral_by_measure[measure_num]
                        merged_notes.extend(orch_notes)
                
                # Add merged notes
                result_tracks[hand_idx].extend(merged_notes)
            
            # Step 2B: Handle standard orchestra infill (for empty measures)
            if orchestra_mode:
                # Determine empty measures
                all_possible_measures = set()
                if measure_map:
                    all_possible_measures.update(m['measure_num'] for m in measure_map)
                for tracks_list in [result_by_measure]:
                    all_possible_measures.update(tracks_list.keys())
                
                empty_measures = all_possible_measures - set(result_by_measure.keys())
                
                if empty_measures:
                    # First try to fill with piano-orchestral tracks
                    piano_orch_fill_measures = {}
                    if orchestral_tracks[hand_idx]:
                        for note in orchestral_tracks[hand_idx]:
                            measure_num = get_measure_for_tick(note.ticks)
                            if measure_num in empty_measures:
                                if measure_num not in piano_orch_fill_measures:
                                    piano_orch_fill_measures[measure_num] = []
                                piano_orch_fill_measures[measure_num].append(note)
                    
                    # Only use other orchestra tracks for measures that are still empty
                    remaining_empty = empty_measures - set(piano_orch_fill_measures.keys())
                    other_orch_fill_measures = {}
                    if remaining_empty and not orchestral_tracks[hand_idx]:  # Only if no piano-orchestral track exists
                        for note in other_orchestra_tracks[hand_idx]:
                            measure_num = get_measure_for_tick(note.ticks)
                            if measure_num in remaining_empty:
                                if measure_num not in other_orch_fill_measures:
                                    other_orch_fill_measures[measure_num] = []
                                other_orch_fill_measures[measure_num].append(note)
                    
                    # Add notes in order of priority
                    for measure_num, notes in piano_orch_fill_measures.items():
                        result_tracks[hand_idx].extend(notes)
                    for measure_num, notes in other_orch_fill_measures.items():
                        result_tracks[hand_idx].extend(notes)
    
    # Ensure the result is sorted by time
    for hand_idx in [0, 1]:
        result_tracks[hand_idx] = sorted(result_tracks[hand_idx], key=lambda note: note.time)
    
    return result_tracks

def organize_tracks_v2(tracks: List[Track], time_sigs: List[Dict[str, Any]], tempos: List[Dict[str, Any]], 
                     resolution: int = 480) -> Dict[str, List[Dict[str, Any]]]:
    """Organize tracks into measures for tracksV2 format"""
    right_hand_notes = []
    left_hand_notes = []
    
    TICKS_PER_BEAT = resolution
    
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

def create_piano_vision_json(midi_path: str, orchestra_mode: bool = False, simplified_mode: bool = False) -> Dict[str, Any]:
    """Create PianoVision JSON output"""
    mid = mido.MidiFile(midi_path)
    tempos = extract_tempo_events(mid)
    
    # Look for matching MuseScore file for metadata
    matching_mscz = find_matching_musescore(midi_path)
    merge_markers = {'right': set(), 'left': set()}
    
    if matching_mscz:
        try:
            # Extract metadata from MuseScore file
            mscx_content = extract_mscx_from_mscz(matching_mscz)
            if mscx_content:
                root = ET.fromstring(mscx_content)
                title, artist, merge_markers = extract_metadata_from_musescore(root)
                print(f"Using metadata from matching MuseScore file: {matching_mscz}")
            else:
                # Fallback to filename and directory if MSCX extraction fails
                filename = os.path.basename(midi_path)
                title = os.path.splitext(filename)[0].replace('_', ' ')
                artist = os.path.basename(os.path.dirname(midi_path))
        except Exception as e:
            print(f"Error extracting metadata from {matching_mscz}: {str(e)}")
            # Fallback to filename and directory
            filename = os.path.basename(midi_path)
            title = os.path.splitext(filename)[0].replace('_', ' ')
            artist = os.path.basename(os.path.dirname(midi_path))
    else:
        # Extract metadata from filename and directory
        filename = os.path.basename(midi_path)
        title = os.path.splitext(filename)[0].replace('_', ' ')
        artist = os.path.basename(os.path.dirname(midi_path))
    
    # Pass the measure information to the track extraction function
    tracks, song_length = get_notes_from_midi(
        midi_path,
        orchestra_mode, 
        simplified_mode,
        merge_markers
    )
    
    time_sigs = extract_time_signatures(mid)
    key_sigs = extract_key_signatures(mid)

    # Get total number of measures from tracks
    tracks_v2 = organize_tracks_v2(tracks, time_sigs, tempos, mid.ticks_per_beat)
    
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
    
    # Handle command line arguments with optional output dir and feature flags
    if len(sys.argv) < 2:
        print("Usage: python midi_to_json.py <input_file> [output_dir] [orchestra_mode] [simplified_mode]")
        sys.exit(1)

    midi_path = sys.argv[1]
    
    # If no output directory is specified, use the same directory as the input file
    if len(sys.argv) >= 3:
        output_dir = sys.argv[2]
    else:
        output_dir = os.path.dirname(midi_path)
        
    # Get optional flags with defaults
    orchestra_mode = False
    simplified_mode = False
    
    if len(sys.argv) > 3:
        orchestra_mode = sys.argv[3].lower() == "true"
    if len(sys.argv) > 4:
        simplified_mode = sys.argv[4].lower() == "true"

    if not os.path.isfile(midi_path):
        print(f"Error: {midi_path} is not a file")
        sys.exit(1)

    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Pass flags to create_piano_vision_json
        output_json = create_piano_vision_json(midi_path, orchestra_mode, simplified_mode)
        
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
