import xml.etree.ElementTree as ET
import json
import os
from typing import Dict, Any, List, Tuple
from metadata_extractor import format_output_filename, extract_metadata_from_musescore, extract_mscx_from_mscz
from notes import Note, Track
from musicxml_to_json import organize_tracks_v2, ticks_to_seconds

def extract_tempo_changes(root: ET.Element) -> List[Dict[str, Any]]:
    """Extract tempo changes from MuseScore file"""
    tempos = []
    division_elem = root.find(".//Division")
    resolution = int(division_elem.text) if division_elem is not None else 480
    
    # First scan all measures to get absolute tick positions
    measure_starts = {}  # map measure number to tick position
    measure_lengths = {} # map measure number to length in ticks
    current_tick = 0
    
    # First pass: scan first staff for measures and time signatures to determine measure lengths
    first_staff = None
    for staff in root.findall(".//Staff"):
        if int(staff.get('id', '1')) == 1:
            first_staff = staff
            break
            
    if first_staff is not None:
        for i, measure in enumerate(first_staff.findall(".//Measure")):
            measure_starts[i] = current_tick
            measure_length = resolution * 4  # Default 4/4 time
            
            # Check for time signature in this measure
            time_sig = measure.find(".//TimeSig")
            if time_sig is not None:
                try:
                    numerator = int(time_sig.find("sigN").text)
                    denominator = int(time_sig.find("sigD").text)
                    measure_length = (resolution * 4 * numerator) // denominator
                except (AttributeError, TypeError, ValueError):
                    pass
                    
            measure_lengths[i] = measure_length
            current_tick += measure_length
    
    # Process explicit tempo markings
    explicit_tempos = []
    for staff in root.findall(".//Staff"):
        if int(staff.get('id', '1')) == 1:  # Only process first staff
            for i, measure in enumerate(staff.findall(".//Measure")):
                measure_tick = measure_starts.get(i, i * resolution * 4)
                
                for voice in measure.findall("voice"):
                    voice_tick = measure_tick
                    for elem in voice:
                        if elem.tag == "Tempo":
                            tempo_value = float(elem.find("tempo").text)
                            bpm = tempo_value * 60
                            bpm = round(bpm)  # Quantize to whole number
                            
                            explicit_tempos.append({
                                "bpm": bpm,
                                "ticks": voice_tick,
                                "time": 0,  # Will be calculated later
                            })
                        
                        # Track position within measure for accurate tempo placement
                        if elem.tag == "Chord" and not elem.find("chord"):
                            duration_type = elem.find("durationType").text
                            duration_ticks = get_duration_ticks(duration_type, elem.findall("dots"), resolution)
                            voice_tick += duration_ticks
                        elif elem.tag == "Rest":
                            duration_elem = elem.find("durationType")
                            if duration_elem is not None:
                                duration_type = duration_elem.text
                                duration_ticks = get_duration_ticks(duration_type, elem.findall("dots"), resolution)
                                voice_tick += duration_ticks
    
    # Ensure we have at least one tempo
    if not explicit_tempos:
        explicit_tempos.append({
            "bpm": 120,  # Default tempo
            "ticks": 0,
            "time": 0
        })
    
    # Process gradual tempo changes (ritardando, accelerando, etc.)
    gradual_tempos = []
    
    # Find all gradual tempo changes in the score
    for spanner in root.findall(".//Spanner[@type='GradualTempoChange']"):
        gtc = spanner.find("GradualTempoChange")
        if gtc is None:
            continue
            
        change_type = gtc.find("tempoChangeType")
        if change_type is None or not change_type.text:
            continue
        
        # Determine start and end positions
        # Look for Segment elements that identify where the tempo change starts and ends
        segments = gtc.findall("Segment")
        if len(segments) < 1:
            continue
        
        # Try to extract measure numbers from attributes directly
        start_measure = None
        end_measure = None
        
        # Look for explicit measure attributes in segments
        for segment in segments:
            measure_attr = segment.get("measure")
            if measure_attr is not None:
                try:
                    measure_num = int(measure_attr)
                    if start_measure is None or measure_num < start_measure:
                        start_measure = measure_num
                    if end_measure is None or measure_num > end_measure:
                        end_measure = measure_num
                except (ValueError, TypeError):
                    pass
        
        # If we couldn't find measure numbers directly, try to infer from context
        if start_measure is None:
            # Try to find context using measure or staff elements
            # Find nearby measure elements in the same hierarchy
            measure_elements = root.findall(".//Measure")
            for i, measure in enumerate(measure_elements):
                # Search for spanner as a child of this measure
                for child_spanner in measure.findall(".//Spanner[@type='GradualTempoChange']"):
                    if child_spanner == spanner:
                        start_measure = i
                        end_measure = i + 1  # Assume one measure span as fallback
                        break
        
        if start_measure is None:
            # Last resort - look for measure-based elements and their indices
            # Let's estimate based on the position in the score - which half of the piece
            # This is imprecise but better than nothing
            total_measures = len(measure_starts)
            if total_measures > 0:
                # Try to find some element in the XML tree to associate with a position
                all_tempo_elements = root.findall(".//Tempo")
                all_tempo_positions = []
                
                for i, tempo in enumerate(all_tempo_elements):
                    # Try to determine the position of this tempo marking
                    if i < len(measure_starts) // 3:  # Early in the piece
                        all_tempo_positions.append(0)
                    elif i > 2 * len(measure_starts) // 3:  # Late in the piece
                        all_tempo_positions.append(2)
                    else:  # Middle of the piece
                        all_tempo_positions.append(1)
                
                # Make a guess about where this gradual change might be
                position_guess = 1  # Default to middle
                if all_tempo_positions:
                    position_guess = max(set(all_tempo_positions), key=all_tempo_positions.count)
                
                # Now guess the measure range
                if position_guess == 0:  # Early
                    start_measure = total_measures // 4
                    end_measure = total_measures // 2
                elif position_guess == 2:  # Late
                    start_measure = 2 * total_measures // 3
                    end_measure = total_measures - 1
                else:  # Middle
                    start_measure = total_measures // 3
                    end_measure = 2 * total_measures // 3
            else:
                # No measure info at all, use arbitrary values
                start_measure = 5
                end_measure = 10
        
        # Convert measure numbers to tick positions
        start_tick = measure_starts.get(start_measure, start_measure * resolution * 4)
        end_tick = measure_starts.get(end_measure, end_measure * resolution * 4)
        
        # If end tick equals start tick, extend to the end of the measure
        if end_tick == start_tick and start_measure in measure_lengths:
            end_tick = start_tick + measure_lengths[start_measure]
        
        # Default to spanning at least one measure if we couldn't determine
        if end_tick <= start_tick:
            end_tick = start_tick + resolution * 4
        
        # Calculate tempo change based on type
        change_factors = {
            "ritardando": 0.75,  # Slow down to 75%
            "rallentando": 0.75,
            "rall.": 0.75,
            "rit.": 0.75, 
            "rall": 0.75,
            "rit": 0.75,
            "accelerando": 1.33,  # Speed up to 133%
            "accel.": 1.33,
            "accel": 1.33
        }
        
        change_type_text = change_type.text.lower()
        change_factor = change_factors.get(change_type_text, 1.0)
        
        # Find the current tempo at the start position
        current_tempo = 120  # Default
        for tempo in reversed(explicit_tempos):
            if tempo["ticks"] <= start_tick:
                current_tempo = tempo["bpm"]
                break
        
        # Calculate end tempo
        end_tempo = int(current_tempo * change_factor)
        
        print(f"Found gradual tempo change: {change_type_text} from measure {start_measure} to {end_measure}")
        print(f"  Starting tempo: {current_tempo} BPM, ending tempo: {end_tempo} BPM")
        
        # Create intermediate tempo points for smooth transition
        span_ticks = end_tick - start_tick
        if span_ticks > 0:
            # Create more points for longer spans
            num_points = min(max(8, span_ticks // (resolution // 2)), 16)
            
            for i in range(num_points):
                progress = (i + 1) / num_points
                point_tick = start_tick + int(progress * span_ticks)
                
                # Use exponential curve for more natural tempo change
                if change_factor < 1.0:  # Ritardando
                    # Use exponential curve that slows down more towards the end
                    curve_progress = 1 - (1 - progress) ** 2
                    point_tempo = int(current_tempo + (end_tempo - current_tempo) * curve_progress)
                else:  # Accelerando
                    # Use exponential curve that speeds up more towards the end
                    curve_progress = progress ** 2
                    point_tempo = int(current_tempo + (end_tempo - current_tempo) * curve_progress)
                
                gradual_tempos.append({
                    "bpm": point_tempo,
                    "ticks": point_tick,
                    "time": 0  # Will be calculated later
                })
    
    # Combine explicit and gradual tempos
    all_tempos = explicit_tempos + gradual_tempos
    all_tempos.sort(key=lambda x: x["ticks"])
    
    # Calculate absolute times
    last_time = 0
    last_ticks = 0
    last_tempo_bpm = all_tempos[0]["bpm"]
    
    for tempo in all_tempos:
        if last_ticks != tempo["ticks"]:
            delta_ticks = tempo["ticks"] - last_ticks
            delta_time = (delta_ticks * 60.0) / (last_tempo_bpm * resolution)
            last_time += delta_time
        
        tempo["time"] = last_time
        last_ticks = tempo["ticks"]
        last_tempo_bpm = tempo["bpm"]
    
    return all_tempos

def get_duration_ticks(duration_type: str, dots_elements: list, resolution: int) -> int:
    """Calculate duration in ticks based on note type and dots"""
    duration_map = {
        "whole": resolution * 4,
        "half": resolution * 2,
        "quarter": resolution,
        "eighth": resolution // 2,
        "16th": resolution // 4,
        "32nd": resolution // 8,
        "64th": resolution // 16,
    }
    
    base_duration = duration_map.get(duration_type, resolution)
    
    # Handle dots
    if dots_elements:
        dot_count = len(dots_elements)
        dot_factor = sum(0.5 ** (i + 1) for i in range(dot_count))
        base_duration = int(base_duration * (1 + dot_factor))
    
    return base_duration

def create_measure_ticks_map(score: ET.Element, resolution: int) -> Dict[int, Dict[int, int]]:
    """Create a mapping from (staff_id, measure_idx) to absolute tick position"""
    staff_measure_ticks = {}  # Maps (staff_id, measure_idx) to absolute tick position
    measure_lengths = {}  # Maps measure_idx to length in ticks
    
    # First pass: determine time signatures and measure lengths for first staff
    current_tick = 0
    first_staff = None
    
    for staff in score.findall(".//Staff"):
        if int(staff.get('id', '1')) == 1:
            first_staff = staff
            break
    
    if first_staff is None:
        return {}
    
    # Calculate measure lengths from first staff
    measures = first_staff.findall("Measure")
    for measure_idx, measure in enumerate(measures):
        measure_lengths[measure_idx] = resolution * 4  # Default 4/4 time
        
        # Check for time signature
        time_sig = measure.find(".//TimeSig")
        if time_sig is not None:
            try:
                numerator = int(time_sig.find("sigN").text)
                denominator = int(time_sig.find("sigD").text)
                measure_lengths[measure_idx] = (resolution * 4 * numerator) // denominator
            except (AttributeError, TypeError, ValueError):
                pass
    
    # Second pass: calculate absolute tick positions for each measure in each staff
    for staff in score.findall(".//Staff"):
        staff_id = int(staff.get('id', '1'))
        staff_measure_ticks[staff_id] = {}
        
        current_tick = 0
        measures = staff.findall("Measure")
        
        for measure_idx, measure in enumerate(measures):
            staff_measure_ticks[staff_id][measure_idx] = current_tick
            
            # Use the length calculated from first staff's time signatures
            measure_length = measure_lengths.get(measure_idx, resolution * 4)
            current_tick += measure_length
    
    return staff_measure_ticks

def parse_musescore(mscx_content: str) -> Dict[str, Any]:
    """Parse MuseScore file and convert to Piano Vision format"""
    root = ET.fromstring(mscx_content)
    
    # Extract metadata
    title, artist = extract_metadata_from_musescore(root)
    division_elem = root.find(".//Division")
    resolution = int(division_elem.text) if division_elem is not None else 480
    
    # Find the Score element
    score = root.find("Score")
    if score is None:
        raise ValueError("No Score element found")
        
    # Create tick mapping for measures across staves
    staff_measure_ticks = create_measure_ticks_map(score, resolution)
    
    # Extract tempo changes after we have measure positions
    tempos = extract_tempo_changes(root)
    
    # Initialize tracking variables
    right_hand_notes = []
    left_hand_notes = []
    measure_count = 0
    time_signatures = []
    key_signatures = []
    
    # Map staves to hands - explicitly assign staff 1 to right hand, all others to left hand
    staff_map = {}
    staves = score.findall(".//Staff")
    
    for staff in staves:
        staff_id = int(staff.get('id', '0'))
        staff_map[staff_id] = 1 if staff_id == 1 else 2
    
    # First pass: process time signatures and key signatures
    for staff in staves:
        staff_id = int(staff.get('id', '0'))
        if staff_id == 1:  # Only process first staff for signatures
            measures = staff.findall("Measure")
            measure_count = max(measure_count, len(measures))
            
            for measure_idx, measure in enumerate(measures):
                # Get the absolute tick position for this measure
                if staff_id in staff_measure_ticks and measure_idx in staff_measure_ticks[staff_id]:
                    measure_start_ticks = staff_measure_ticks[staff_id][measure_idx]
                else:
                    measure_start_ticks = measure_idx * resolution * 4
                
                # Process time signature
                time_sig = measure.find(".//TimeSig")
                if time_sig is not None:
                    try:
                        numerator = int(time_sig.find("sigN").text)
                        denominator = int(time_sig.find("sigD").text)
                        time_signatures.append({
                            "ticks": measure_start_ticks,
                            "timeSignature": [str(numerator), str(denominator)],
                            "measures": str(measure_idx)
                        })
                    except (AttributeError, TypeError):
                        pass
                
                # Process key signature
                key_sig = measure.find(".//KeySig")
                if key_sig is not None:
                    key = key_sig.find("concertKey")
                    if key is not None:
                        key_value = int(key.text)
                        key_map = {0: "C", 1: "G", 2: "D", -1: "F", -2: "Bb"}
                        key_signatures.append({
                            "ticks": measure_start_ticks,
                            "key": key_map.get(key_value, "C"),
                            "scale": "major"
                        })
    
    # Second pass: process notes for each staff independently
    for staff in staves:
        staff_id = int(staff.get('id', '0'))
        staff_hand = staff_map.get(staff_id, 2)  # Default to left hand if not found
        measures = staff.findall("Measure")
        
        # Process measures in this staff
        for measure_idx, measure in enumerate(measures):
            # Get the absolute tick position for this measure
            if staff_id in staff_measure_ticks and measure_idx in staff_measure_ticks[staff_id]:
                measure_start_ticks = staff_measure_ticks[staff_id][measure_idx]
            else:
                measure_start_ticks = measure_idx * resolution * 4
                
            # Process each voice in the measure
            for voice_idx, voice in enumerate(measure.findall("voice")):
                voice_tick = measure_start_ticks  # Start at beginning of measure
                
                # Process each element in the voice sequentially
                for elem in voice:
                    if elem.tag == "Chord":
                        duration_elem = elem.find("durationType")
                        if duration_elem is None:
                            continue
                        
                        # Calculate duration considering dots
                        duration_type = duration_elem.text
                        duration_ticks = get_duration_ticks(duration_type, elem.findall("dots"), resolution)
                        
                        # Check if this is part of a chord
                        is_chord = elem.find("chord") is not None
                        
                        # Process notes in the chord
                        for note_elem in elem.findall("Note"):
                            pitch_elem = note_elem.find("pitch")
                            if pitch_elem is None:
                                continue
                            
                            try:
                                pitch = int(pitch_elem.text)
                                velocity_elem = note_elem.find("velocity")
                                velocity = float(velocity_elem.text) / 127.0 if velocity_elem is not None else 0.8
                                
                                # Calculate precise timing based on current position
                                note_time = ticks_to_seconds(voice_tick, tempos, resolution)
                                note_end_time = ticks_to_seconds(voice_tick + duration_ticks, tempos, resolution)
                                
                                # Create note with accurate timing
                                note = Note(
                                    midi=pitch,
                                    time=note_time,
                                    velocity=velocity,
                                    duration=note_end_time - note_time,
                                    ticks=voice_tick,
                                    duration_ticks=duration_ticks,
                                    staff=staff_hand,
                                    group=measure_idx
                                )
                                
                                # Add note to correct hand based on staff_hand
                                if staff_hand == 1:
                                    right_hand_notes.append(note)
                                else:
                                    left_hand_notes.append(note)
                                    
                            except (ValueError, AttributeError, TypeError):
                                continue
                        
                        # Only advance tick position if not part of a chord
                        if not is_chord:
                            voice_tick += duration_ticks
                    
                    # Handle rests 
                    elif elem.tag == "Rest":
                        duration_elem = elem.find("durationType")
                        if duration_elem is not None:
                            duration_type = duration_elem.text
                            duration_ticks = get_duration_ticks(duration_type, elem.findall("dots"), resolution)
                            voice_tick += duration_ticks
    
    # Ensure we have at least one time signature
    if not time_signatures:
        time_signatures.append({
            "ticks": 0,
            "timeSignature": ["4", "4"],
            "measures": "0"
        })
    
    # Sort notes by time within each hand
    right_hand_notes.sort(key=lambda x: (x.ticks, x.midi))
    left_hand_notes.sort(key=lambda x: (x.ticks, x.midi))
    
    # Create measure list with proper time signatures
    measure_ticks = []
    for i in range(measure_count):
        # Find measure start time based on first staff's measure positions
        if 1 in staff_measure_ticks and i in staff_measure_ticks[1]:
            measure_start_ticks = staff_measure_ticks[1][i]
        else:
            measure_start_ticks = i * resolution * 4
        
        # Find the current time signature for this measure
        current_time_sig = next(
            (ts for ts in reversed(time_signatures) 
             if float(ts["measures"]) <= i),
            time_signatures[0]  # Default to first time signature if none found
        )
        
        # Calculate measure length based on time signature
        numerator = int(current_time_sig["timeSignature"][0])
        denominator = int(current_time_sig["timeSignature"][1])
        measure_length = (resolution * 4 * numerator) // denominator
        
        measure_ticks.append({
            "time": ticks_to_seconds(measure_start_ticks, tempos, resolution),
            "timeSignature": current_time_sig["timeSignature"],
            "ticksPerMeasure": measure_length,
            "ticksStart": measure_start_ticks,
            "totalTicks": measure_length,
            "type": "0.000" if i == 0 else "2"
        })
    
    # Create tracks
    right_track = Track(notes=sorted(right_hand_notes, key=lambda x: (x.ticks, x.midi)), 
                       myInstrument=-5, theirInstrument=0)
    left_track = Track(notes=sorted(left_hand_notes, key=lambda x: (x.ticks, x.midi)), 
                      myInstrument=-5, theirInstrument=0)
    
    # Calculate song length
    song_length = 0
    if right_hand_notes or left_hand_notes:
        all_notes = right_hand_notes + left_hand_notes
        song_length = max([note.time + note.duration for note in all_notes])
    
    # Create final output
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
            for track in [right_track, left_track]
        ],
        "start_time": 0,
        "song_length": song_length,
        "resolution": resolution,
        "tempos": tempos,
        "keySignatures": key_signatures,
        "timeSignatures": time_signatures,
        "measures": measure_ticks,
        "tracksV2": organize_tracks_v2([right_track, left_track], measure_ticks, tempos, resolution),
        "accompanyingInstruments": [-2, -1],
        "accompanyingChannels": [0, 0],
        "name": title,
        "artist": artist,
        "accompanyingTracks": []
    }

def main():
    import sys
    if len(sys.argv) != 3:
        print("Usage: python musescore_to_json.py <input_file> <output_dir>")
        sys.exit(1)

    mscz_path = sys.argv[1]
    output_dir = sys.argv[2]

    if not os.path.isfile(mscz_path):
        print(f"Error: {mscz_path} is not a file")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Extract .mscx content from .mscz
        mscx_content = extract_mscx_from_mscz(mscz_path)
        if not mscx_content:
            raise Exception("Failed to extract MSCX content from MSCZ file")
        
        # Parse the content
        output_json = parse_musescore(mscx_content)
        
        # Generate output filename
        output_filename = format_output_filename(
            output_json['name'],
            output_json['artist'],
            mscz_path
        )
        
        # Write output file
        output_path = os.path.join(output_dir, output_filename)
        with open(output_path, 'w') as f:
            json.dump(output_json, f)
        
        print(f"Converted: {mscz_path} -> {output_path}")
    except Exception as e:
        print(f"Error processing {mscz_path}: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
