import xml.etree.ElementTree as ET
import json
import os
from typing import Dict, Any, List
from pv_util import format_output_filename, extract_metadata_from_musescore, extract_mscx_from_mscz, ticks_to_seconds, find_matching_midi, get_duration_ticks
from notes import Note, Track
from track_organizer import organize_tracks_v2
from midi_to_json import extract_tempo_events

def extract_tempo_changes(root: ET.Element) -> List[Dict[str, Any]]:
    """Extract tempo changes from MuseScore file"""
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
    
    # Calculate absolute times - this is the correct implementation matching MuseScore's approach
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

def get_measure_ticks_from_midi(midi_path: str) -> Dict[int, Dict[int, int]]:
    """Extract measure tick positions from MIDI file"""
    import mido
    
    try:
        midi = mido.MidiFile(midi_path)
        staff_measure_ticks = {1: {}, 2: {}}  # Initialize with two staves
        current_tick = 0
        measure_idx = 0
        last_time_sig = (4, 4)  # Default 4/4 time
        
        # Look for time signature changes and calculate measure positions
        for track in midi.tracks:
            track_tick = 0
            track_measure_idx = 0
            
            for msg in track:
                track_tick += msg.time
                
                if msg.type == 'time_signature':
                    last_time_sig = (msg.numerator, msg.denominator)
                    
                    # If this is not the first time signature, record previous measure
                    if track_measure_idx > 0:
                        for staff_id in [1, 2]:
                            staff_measure_ticks[staff_id][track_measure_idx - 1] = current_tick
                    
                    current_tick = track_tick
                    track_measure_idx += 1
            
            # Break after first track (usually contains all time signatures)
            break
        
        # Calculate remaining measures based on last time signature
        numerator, denominator = last_time_sig
        ticks_per_measure = midi.ticks_per_beat * 4 * numerator // denominator
        
        # Find the last tick in the MIDI file
        last_tick = 0
        for track in midi.tracks:
            track_tick = 0
            for msg in track:
                track_tick += msg.time
                last_tick = max(last_tick, track_tick)
        
        # Fill in measure positions up to the last tick
        while current_tick <= last_tick:
            for staff_id in [1, 2]:
                staff_measure_ticks[staff_id][measure_idx] = current_tick
            current_tick += ticks_per_measure
            measure_idx += 1
        
        return staff_measure_ticks
        
    except Exception as e:
        print(f"Warning: Failed to extract measure ticks from MIDI: {str(e)}")
        return None

def create_measure_ticks_map(score: ET.Element, mscz_path: str, resolution: int) -> Dict[int, Dict[int, int]]:
    """Create a mapping from (staff_id, measure_idx) to absolute tick position.
    Primary source: companion MIDI (authoritative for tempo & measure starts).
    Fallback: rough estimation from MuseScore XML when MIDI missing (less accurate).
    """
    # BUG this currently does not handle pickup measures correctly

    if mscz_path:
        matching_midi = find_matching_midi(mscz_path)
        if matching_midi:
            print(f"Using measure timing from MIDI file: {matching_midi}")
            midi_measure_ticks = get_measure_ticks_from_midi(matching_midi)
            if midi_measure_ticks:
                return midi_measure_ticks
            
    print("WARNING: No companion MIDI timing available; falling back to approximate MuseScore-derived measure timing (may be inaccurate)")
    
    # Original MuseScore timing logic as fallback
    staff_measure_ticks = {}
    measure_lengths = {}
    
    # First pass: determine time signatures and measure lengths for first staff
    first_staff = None
    for staff in score.findall(".//Staff"):
        if int(staff.get('id', '1')) == 1:
            first_staff = staff
            break
    
    if first_staff is None:
        return {}
    
    # Calculate measure lengths from first staff
    current_tick = 0
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
        
        # Check for irregular measure (pickup)
        irregular = measure.find(".//irregular")
        if irregular is not None:
            # Get actual duration of pickup measure from its contents
            measure_length = 0
            for voice in measure.findall(".//voice"):
                voice_length = 0
                for elem in voice:
                    if elem.tag in ["Chord", "Rest"]:
                        duration_type = elem.find("durationType")
                        if duration_type is not None:
                            duration_ticks = get_duration_ticks(duration_type.text, elem.findall("dots"), resolution)
                            voice_length += duration_ticks
                measure_length = max(measure_length, voice_length)
            if measure_length > 0:
                measure_lengths[measure_idx] = measure_length
    
    # Second pass: calculate absolute tick positions for each measure in each staff
    for staff in score.findall(".//Staff"):
        staff_id = int(staff.get('id', '1'))
        staff_measure_ticks[staff_id] = {}
        
        current_tick = 0
        measures = staff.findall("Measure")
        
        for measure_idx, measure in enumerate(measures):
            staff_measure_ticks[staff_id][measure_idx] = current_tick
            current_tick += measure_lengths[measure_idx]
    
    return staff_measure_ticks


def parse_musescore(mscx_content: str, mscz_path: str) -> Dict[str, Any]:
    """Parse MuseScore file and convert to Piano Vision format.
    Relies on companion MIDI for tempo & measure accuracy if present.
    """
    root = ET.fromstring(mscx_content)
    
    # Extract metadata
    title, artist, _ = extract_metadata_from_musescore(root, mscz_path)  # previously ignored third value
    division_elem = root.find(".//Division")
    resolution = int(division_elem.text) if division_elem is not None else 480
    
    # Find the Score element
    score = root.find("Score")
    if score is None:
        raise ValueError("No Score element found")

    # Check for matching MIDI file to extract tempos
    matching_midi = find_matching_midi(mscz_path)
    if matching_midi:
        print(f"Found companion MIDI: {matching_midi} (authoritative tempo source).")
        import mido
        midi_file = mido.MidiFile(matching_midi)
        tempos = extract_tempo_events(midi_file)
    else:
        print("WARNING: Companion MIDI missing. Falling back to embedded MuseScore tempo parsing (less accurate).")
        tempos = extract_tempo_changes(root)

    # Create tick mapping for measures across staves
    staff_measure_ticks = create_measure_ticks_map(score, mscz_path, resolution)

    # Initialize tracking variables
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
    
    # COMPLETE REWRITE OF NOTE COLLECTION LOGIC
    all_notes = []

    # Process each staff separately
    for staff in staves:
        staff_id = int(staff.get('id', '0'))
        staff_hand = staff_map.get(staff_id, 2)  # Default to left hand if not found
        
        # Process measures in this staff
        measures = staff.findall("Measure")
        for measure_idx, measure in enumerate(measures):
            # Get measure start tick position
            if staff_id in staff_measure_ticks and measure_idx in staff_measure_ticks[staff_id]:
                measure_start_ticks = staff_measure_ticks[staff_id][measure_idx]
            else:
                measure_start_ticks = measure_idx * resolution * 4
            
            # Process each voice independently
            voice_elements = measure.findall("voice")
            for voice_idx, voice in enumerate(voice_elements):
                # Start position for this voice in this measure
                current_tick = measure_start_ticks
                
                # Process elements in this voice sequentially
                active_tuplet = None
                for elem in voice:
                    # Handle tuplets that affect timing
                    if elem.tag == "Tuplet":
                        # Extract tuplet ratio (e.g., 3:2 for triplets)
                        normal_notes = elem.find("normalNotes")
                        actual_notes = elem.find("actualNotes")
                        base_note = elem.find("baseNote")
                        
                        if normal_notes is not None and actual_notes is not None and base_note is not None:
                            normal = int(normal_notes.text)
                            actual = int(actual_notes.text)
                            base_type = base_note.text
                            
                            # Calculate tuplet ratio
                            tuplet_ratio = normal / actual
                            
                            # Save active tuplet context
                            active_tuplet = {
                                "ratio": tuplet_ratio,
                                "base_type": base_type,
                                "normal": normal,
                                "actual": actual
                            }
                    
                    elif elem.tag == "endTuplet":
                        # End current tuplet
                        active_tuplet = None
                        
                    elif elem.tag == "location":
                        # Handle explicit position changes within a voice
                        fraction_elem = elem.find("fractions")
                        if fraction_elem is not None and fraction_elem.text:
                            try:
                                # Parse the fraction (e.g., "1/4" or "-1/12")
                                num, denom = fraction_elem.text.split('/')
                                fraction_value = int(num) / int(denom)
                                # Adjust the current tick position
                                current_tick += int(fraction_value * resolution * 4)
                            except (ValueError, ZeroDivisionError):
                                pass
                    
                    elif elem.tag == "Chord":
                        # Handle chord (a group of notes played simultaneously)
                        duration_elem = elem.find("durationType")
                        if duration_elem is None:
                            continue

                        # Check if this chord has an accent articulation
                        has_accent = False
                        for articulation in elem.findall(".//Articulation"):
                            subtype = articulation.find("subtype")
                            if (subtype is not None and
                                subtype.text and
                                ("accent" in subtype.text.lower() or "marcato" in subtype.text.lower())): # Include marcato as accent
                                has_accent = True
                                break

                        duration_type = duration_elem.text
                        # Get base duration without tuplet adjustment
                        base_duration_ticks = get_duration_ticks(duration_type, elem.findall("dots"), resolution)

                        # Adjust duration if in tuplet (e.g., triplet eighth notes)
                        duration_ticks = base_duration_ticks
                        if active_tuplet:
                            # Apply tuplet ratio to get actual duration (e.g., triplet eighth = 1/3 of a quarter)
                            duration_ticks = int(base_duration_ticks * active_tuplet["ratio"])
                        
                        # Check if this is part of another chord (continuation)
                        is_chord_continuation = elem.find("chord") is not None
                        
                        # Determine chord start position
                        chord_start_tick = current_tick
                        
                        # Only advance position if this isn't a chord continuation
                        if not is_chord_continuation:
                            # Advance position for next element
                            current_tick += duration_ticks
                        
                        # Process each note in this chord
                        for note_elem in elem.findall("Note"):
                            pitch_elem = note_elem.find("pitch")
                            if pitch_elem is None:
                                continue
                                
                            try:
                                pitch = int(pitch_elem.text)
                                velocity_elem = note_elem.find("velocity")
                                velocity = float(velocity_elem.text) / 127.0 if velocity_elem is not None else 0.8

                                # Boost velocity for accented notes
                                if has_accent and velocity < 0.9:
                                    velocity = min(1.0, velocity * 1.25)  # Apply 25% boost but cap at 1.0

                                # Calculate precise timing
                                note_time = ticks_to_seconds(chord_start_tick, tempos, resolution)
                                note_end_time = ticks_to_seconds(chord_start_tick + duration_ticks, tempos, resolution)

                                # Create note with accurate timing and accent info
                                note = Note(
                                    midi=pitch,
                                    time=note_time,
                                    velocity=velocity,
                                    duration=note_end_time - note_time,
                                    ticks=chord_start_tick,
                                    duration_ticks=duration_ticks,
                                    staff=staff_hand,
                                    group=measure_idx,
                                    accent=1 if has_accent else 0
                                )

                                all_notes.append(note)
                                
                            except (ValueError, AttributeError, TypeError):
                                continue
                    
                    elif elem.tag == "Rest":
                        # Handle rest - adjust current position
                        duration_elem = elem.find("durationType")
                        if duration_elem is not None:
                            duration_type = duration_elem.text
                            # Get base duration
                            base_duration_ticks = get_duration_ticks(duration_type, elem.findall("dots"), resolution)
                            
                            # Apply tuplet adjustment if needed
                            duration_ticks = base_duration_ticks
                            if active_tuplet:
                                duration_ticks = int(base_duration_ticks * active_tuplet["ratio"])
                            
                            # Advance position
                            current_tick += duration_ticks
    
    # Split notes by hand
    right_hand_notes = [note for note in all_notes if note.staff == 1]
    left_hand_notes = [note for note in all_notes if note.staff == 2]
    
    # Sort notes by actual time first then MIDI number for consistent temporal ordering
    # This is critical for proper playback since two notes with same tick position 
    # might have different actual times due to tempo changes
    right_hand_notes.sort(key=lambda x: (x.time, x.midi))
    left_hand_notes.sort(key=lambda x: (x.time, x.midi))
    
    # Ensure we have at least one time signature
    if not time_signatures:
        time_signatures.append({
            "ticks": 0,
            "timeSignature": ["4", "4"],
            "measures": "0"
        })
    
    # Create measure list with proper time signatures and accurate timing
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
        
        # Calculate accurate temporal time for measure start
        measure_start_time = ticks_to_seconds(measure_start_ticks, tempos, resolution)
        
        measure_ticks.append({
            "time": measure_start_time,
            "timeSignature": current_time_sig["timeSignature"],
            "ticksPerMeasure": measure_length,
            "ticksStart": measure_start_ticks,
            "totalTicks": measure_length,
            "type": "0.000" if i == 0 else "2"
        })
    
    # Create tracks with properly sorted notes
    right_track = Track(
        notes=right_hand_notes,
        myInstrument=-5, 
        theirInstrument=0
    )
    
    left_track = Track(
        notes=left_hand_notes, 
        myInstrument=-5, 
        theirInstrument=0
    )
    
    # Calculate song length
    song_length = 0
    if right_hand_notes or left_hand_notes:
        all_notes = right_hand_notes + left_hand_notes
        song_length = max([note.time + note.duration for note in all_notes]) if all_notes else 0
    
    # Create final output with all notes and their precise timing
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
    
    # Handle command line arguments with optional output dir and feature flags
    if len(sys.argv) < 2:
        print("Usage: python musescore_to_json.py <input_file> [output_dir] [orchestra_mode] [simplified_mode]")
        sys.exit(1)

    mscz_path = sys.argv[1]
    
    # If no output directory is specified, use the same directory as the input file
    if len(sys.argv) >= 3:
        output_dir = sys.argv[2]
    else:
        output_dir = os.path.dirname(mscz_path)
        
    # Get optional flags with defaults
    orchestra_mode = False
    simplified_mode = False
    
    if len(sys.argv) > 3:
        orchestra_mode = sys.argv[3].lower() == "true"
    if len(sys.argv) > 4:
        simplified_mode = sys.argv[4].lower() == "true"

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
        output_json = parse_musescore(mscx_content, mscz_path)
        
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
