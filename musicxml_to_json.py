import xml.etree.ElementTree as ET
import json
import os
from typing import List, Dict, Any, Optional, Tuple
from pv_util import standardize_title, standardize_artist, format_output_filename, ticks_to_seconds, find_matching_midi
from midi_to_json import extract_tempo_events
from notes import Note, Track
from track_organizer import organize_tracks_v2

MIDI_NOTE_NAMES = {
    'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11
}

def extract_metadata_from_xml(xml_path: str) -> Tuple[str, str]:
    """Extract title and artist from MusicXML file"""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        title = None
        subtitle = None
        artist = None

        # Create fallback metadata dictionary
        fallback_metadata = {
            "fallback_filename": os.path.splitext(os.path.basename(xml_path))[0].replace('_', ' '),
            "fallback_folder": os.path.basename(os.path.dirname(xml_path))
        }

        # Try to get title and subtitle from credit elements
        for credit in root.findall('.//credit'):
            credit_type = credit.find('credit-type')
            if credit_type is not None:
                if credit_type.text == 'title':
                    credit_words = credit.find('credit-words')
                    if credit_words is not None:
                        title = standardize_title(credit_words.text)
                elif credit_type.text == 'subtitle':
                    credit_words = credit.find('credit-words')
                    if credit_words is not None:
                        subtitle = standardize_title(credit_words.text)

        # Fallback to work-title if no credit title found
        if not title:
            work = root.find('.//work-title')
            if work is not None:
                title = standardize_title(work.text)

        # Final fallback to filename
        if not title:
            title = os.path.splitext(os.path.basename(xml_path))[0].replace('_', ' ')

        # Combine title and subtitle if both exist
        if subtitle:
            title = f"{title} - {subtitle}"

        # Try to get composer from credit elements
        for credit in root.findall('.//credit'):
            credit_type = credit.find('credit-type')
            if credit_type is not None and credit_type.text == 'composer':
                credit_words = credit.find('credit-words')
                if credit_words is not None:
                    artist = standardize_artist(credit_words.text)
                    break

        # Fallback to creator field if no credit composer found
        if not artist:
            creator = root.find('.//creator[@type="composer"]')
            if creator is not None:
                artist = standardize_artist(creator.text)

        # Final fallback to parent folder name
        if not artist:
            artist = os.path.basename(os.path.dirname(xml_path))

        # If either value is empty after extraction, use fallbacks
        if not title:
            title = fallback_metadata["fallback_filename"]
        if not artist:
            artist = fallback_metadata["fallback_folder"]

        return title, artist

    except Exception as e:
        print(f"Error extracting metadata from {xml_path}: {str(e)}")
        # Return default values based on the filename
        return (os.path.splitext(os.path.basename(xml_path))[0].replace('_', ' '),
                os.path.basename(os.path.dirname(xml_path)))

def note_to_midi(step: str, octave: int, alter: int = 0) -> int:
    """Convert note name and octave to MIDI note number"""
    return MIDI_NOTE_NAMES[step] + (octave + 1) * 12 + alter

def extract_key_signatures(root) -> List[Dict[str, Any]]:
    """Extract key signatures from MusicXML"""
    key_signatures = []
    current_ticks = 0
    
    # Map fifths to key names
    key_map = {
        0: 'C', 1: 'G', 2: 'D', 3: 'A', 4: 'E', 5: 'B', 6: 'F#', 7: 'C#',
        -1: 'F', -2: 'Bb', -3: 'Eb', -4: 'Ab', -5: 'Db', -6: 'Gb', -7: 'Cb'
    }
    
    for key in root.findall('.//key'):
        fifths = int(key.find('fifths').text)
        mode = key.find('mode')
        mode = mode.text if mode is not None else 'major'
        
        key_signatures.append({
            "ticks": current_ticks,
            "key": key_map.get(fifths, 'C'),  # Default to C if fifths not found
            "scale": mode.lower()
        })
        
    return key_signatures

def identify_piano_part(root) -> str:
    """Identify the first piano part ID in the score"""
    piano_names = [
        'piano', 'pianoforte', 'pf', 'pno', 'pian', 'pia',
        'grand piano', 'upright piano', 'electric piano',
        'piano-forte', 'klavier', 'fortepiano'
    ]
    
    for score_part in root.findall('.//score-part'):
        part_id = score_part.get('id')
        
        # Check instrument name
        instrument = score_part.find('.//instrument-name')
        if instrument is not None:
            instrument_name = instrument.text.lower()
            if any(piano_name in instrument_name for piano_name in piano_names):
                return part_id
            
        # Check abbreviated instrument name
        abbrev = score_part.find('.//instrument-abbreviation')
        if abbrev is not None:
            abbrev_name = abbrev.text.lower()
            if any(piano_name in abbrev_name for piano_name in piano_names):
                return part_id
            
        # Check midi instrument
        midi_instrument = score_part.find('.//midi-instrument')
        if midi_instrument is not None:
            program = midi_instrument.find('midi-program')
            if program is not None and 0 <= int(program.text) <= 7:  # Piano family
                return part_id
    
    # If no piano found, return first part
    first_part = root.find('.//score-part')
    return first_part.get('id') if first_part is not None else 'P1'

def get_note_staff(note_elem, measure) -> int:
    """Get the explicit staff assignment from a note element, using measure as context."""

    # Explicit staff assignment
    staff = note_elem.find('staff')
    if staff is not None and staff.text.isdigit():
        return int(staff.text)

    # Voice-based heuristic
    voice = note_elem.find('voice')
    if voice is not None:
        try:
            voice_num = int(voice.text)
            return 1 if voice_num <= 2 else 2
        except ValueError:
            pass  # If the voice is not a number, ignore it and move to next logic

    # Iterate through measure elements to determine if the note is after a backup
    is_after_backup = False
    for elem in measure:
        if elem == note_elem:
            break  # Stop iterating once we reach the current element
        if elem.tag == 'backup':
            is_after_backup = True
        elif elem.tag == 'forward':
            is_after_backup = False

    return 2 if is_after_backup else 1  # Default to staff 1 if unknown

def extract_tempo_from_xml(root, ticks_per_beat: int = 480) -> List[Dict[str, Any]]:
    """Extract tempo markings from MusicXML with improved reliability"""
    tempos = []
    current_ticks = 0
    current_divisions = None

    # Find all parts and process in order
    for part in root.findall('.//part'):
        measure_pos = 0

        # Process each measure
        for measure in part.findall('measure'):
            measure_tempos = []

            # Process attributes for divisions
            for attr in measure.findall('attributes'):
                div_elem = attr.find('divisions')
                if div_elem is not None:
                    current_divisions = int(div_elem.text)

            if current_divisions is None:
                continue  # Skip if no divisions defined yet

            # Find all direction elements with tempo markings
            for direction in measure.findall('direction'):
                # Look for sound element with tempo
                sound = direction.find('.//sound[@tempo]')
                if sound is not None:
                    tempo_bpm = float(sound.get('tempo'))

                    # Calculate position within measure
                    pos_in_measure = 0
                    if direction.find('offset') is not None:
                        offset = int(direction.find('offset').text)
                        pos_in_measure = offset * ticks_per_beat / current_divisions

                    tick_pos = current_ticks + measure_pos + pos_in_measure

                    measure_tempos.append({
                        "bpm": tempo_bpm,
                        "ticks": int(tick_pos),
                        "time": 0  # Will calculate actual time later
                    })

            # Add note durations to measure position
            for note in measure.findall('note'):
                if note.find('grace') is None and note.find('chord') is None:
                    duration = note.find('duration')
                    if duration is not None:
                        measure_pos += int(duration.text) * ticks_per_beat / current_divisions

            # Add measure tempos to main list
            tempos.extend(measure_tempos)

            # Reset measure position for next measure
            current_ticks += measure_pos
            measure_pos = 0

    # Ensure we have at least one tempo marking at tick 0
    if not tempos or tempos[0]["ticks"] > 0:
        tempos.insert(0, {
            "bpm": 120,  # Default tempo
            "ticks": 0,
            "time": 0
        })

    # Sort tempos by tick position
    tempos.sort(key=lambda x: x["ticks"])

    # Calculate times based on tempo changes - IMPROVED for precise timing
    calculate_tempo_times(tempos, ticks_per_beat)

    return tempos

def calculate_tempo_times(tempos: List[Dict[str, Any]], ticks_per_beat: int):
    """Calculate actual times for each tempo marking based on previous tempos"""
    if not tempos:
        return

    # First tempo always at time 0
    tempos[0]["time"] = 0.0

    for i in range(1, len(tempos)):
        curr_tempo = tempos[i]
        prev_tempo = tempos[i - 1]

        # Calculate time precisely using microseconds per quarter note
        delta_ticks = curr_tempo["ticks"] - prev_tempo["ticks"]
        microseconds_per_beat = 60000000 / prev_tempo["bpm"]
        delta_seconds = (delta_ticks * microseconds_per_beat) / (ticks_per_beat * 1000000)

        # Accumulate time precisely
        curr_tempo["time"] = prev_tempo["time"] + delta_seconds

def verify_tempo_markings(tempos: List[Dict[str, Any]]):
    """Verify tempo markings for debugging"""
    if not tempos:
        print("WARNING: No tempo markings found, using default 120 BPM")
        return

    if tempos[0]["ticks"] != 0:
        print("WARNING: First tempo marking not at tick 0, adding default tempo")

    # Print all tempo markings
    print(f"Found {len(tempos)} tempo markings:")
    for i, tempo in enumerate(tempos):
        print(f"  {i + 1}: {tempo['bpm']} BPM at tick {tempo['ticks']} (time: {tempo['time']:.3f}s)")

    # Verify time calculations
    for i in range(1, len(tempos)):
        calculated_time = ticks_to_seconds(tempos[i]["ticks"], tempos, 480)
        delta = abs(calculated_time - tempos[i]["time"])
        if delta > 0.001:  # More than 1ms difference
            print(f"WARNING: Tempo time calculation error at tempo {i + 1}: {delta:.6f}s")

def parse_musicxml(xml_path: str) -> Dict[str, Any]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    
    # Get title and artist using the metadata extractor
    title, artist = extract_metadata_from_xml(xml_path)
    
    # Identify piano part
    piano_part_id = identify_piano_part(root)
    
    # Use a standard output resolution that minimizes rounding errors
    output_resolution = 480  # MIDI standard resolution
    
    # Check for matching MIDI file to extract tempos
    matching_midi = find_matching_midi(xml_path)
    if matching_midi:
        print(f"Found matching MIDI file: {matching_midi}")
        print(f"Using tempo from MIDI file instead of MusicXML")
        import mido
        midi_file = mido.MidiFile(matching_midi)
        tempos = extract_tempo_events(midi_file)
    else:
        # Extract tempos using the improved tempo extractor from MusicXML
        tempos = extract_tempo_from_xml(root, output_resolution)
    
    # Verify tempo markings for debugging
    verify_tempo_markings(tempos)
    
    # Extract key signatures
    key_signatures = extract_key_signatures(root)

    # Build complete measure map for precise positioning
    measure_map = []
    current_ticks = 0
    current_divisions = None
    
    # First pass: build measure structure and track time signatures
    piano_part = root.find(f'.//part[@id="{piano_part_id}"]')
    if piano_part is None:  # Fix DeprecationWarning
        raise ValueError(f"Piano part {piano_part_id} not found in the MusicXML file")
    
    # Track the current time signature
    current_time_signature = {
        'beats': 4,
        'beat_type': 4
    }  # Default 4/4
    
    # Create complete measure map with precise tick boundaries
    for measure in piano_part.findall('measure'):
        measure_info = {
            'start_ticks': current_ticks,
            'divisions': current_divisions,
            # Always include current time signature for every measure
            'beats': current_time_signature['beats'],
            'beat_type': current_time_signature['beat_type']
        }
        
        # Process attributes
        for attributes in measure.findall('attributes'):
            div = attributes.find('divisions')
            if div is not None:
                current_divisions = int(div.text)
                measure_info['divisions'] = current_divisions
            
            time_elem = attributes.find('time')
            if time_elem is not None:
                # Update current time signature when it changes
                beats = int(time_elem.find('beats').text)
                beat_type = int(time_elem.find('beat-type').text)
                
                current_time_signature = {
                    'beats': beats,
                    'beat_type': beat_type
                }
                
                measure_info['beats'] = beats
                measure_info['beat_type'] = beat_type
                
        # Calculate duration ticks based on time signature
        # (whether it changed in this measure or is carried forward)
        measure_info['duration_ticks'] = int((current_time_signature['beats'] * 4 * output_resolution) / 
                                            current_time_signature['beat_type'])
        
        # Handle pickup measure
        if len(measure_map) == 0:
            # Check actual content duration
            note_duration_sum = 0
            for note in measure.findall('note'):
                if note.find('grace') is not None:
                    continue
                if note.find('chord') is not None:
                    continue  # Don't count chord notes twice
                duration = int(note.find('duration').text)
                if measure_info['divisions']:
                    note_duration_sum += int(round(duration * output_resolution / measure_info['divisions']))
            
            if 0 < note_duration_sum < measure_info['duration_ticks']:
                # This is likely a pickup measure
                measure_info['duration_ticks'] = note_duration_sum
                measure_info['is_pickup'] = True
        
        measure_map.append(measure_info)
        current_ticks += measure_info['duration_ticks']
    
    # Record time signatures - only when they actually change
    time_signatures = []
    last_beats = None
    last_beat_type = None
    
    for i, measure in enumerate(measure_map):
        if 'beats' in measure and 'beat_type' in measure:
            # Only add if time signature is different from previous one
            if last_beats != measure['beats'] or last_beat_type != measure['beat_type']:
                # Format the measures value as float with decimal places if it's 0
                measure_index_str = "0.000" if i == 0 else str(i)
                time_signatures.append({
                    "ticks": measure['start_ticks'],
                    "timeSignature": [str(measure['beats']), str(measure['beat_type'])],
                    "measures": measure_index_str
                })
                last_beats = measure['beats']
                last_beat_type = measure['beat_type']
    
    # Generate measure_ticks list - ensure time signatures are consistent
    measure_ticks = []
    for i, measure in enumerate(measure_map):
        measure_start_ticks = measure['start_ticks']
        measure_duration = measure['duration_ticks']
        
        measure_ticks.append({
            "time": ticks_to_seconds(measure_start_ticks, tempos, output_resolution),
            "timeSignature": [str(measure['beats']), str(measure['beat_type'])],
            "ticksPerMeasure": measure_duration,
            "ticksStart": measure_start_ticks,
            "totalTicks": measure_duration,
            # Format type as a float string with decimal places if it's measure 0, otherwise as integer
            "type": "0.000" if i == 0 and measure.get('is_pickup') else (
                "0.000" if i == 0 else "2")
        })
    
    # Initialize note collection with tie tracking
    right_hand_notes = []
    left_hand_notes = []
    right_hand_group = 0
    left_hand_group = -1
    
    # Track tied notes to combine them
    tied_notes = {}  # key = (staff, voice, pitch), value = last note that has tie
    
    # Process all notes with precise timing
    for measure_idx, (measure, measure_info) in enumerate(zip(piano_part.findall('measure'), measure_map)):
        divisions = measure_info.get('divisions')
        if divisions is None:
            continue
        
        measure_start_ticks = measure_info['start_ticks']
        measure_duration = measure_info['duration_ticks']
        
        # Track voice positions within measure
        voice_positions = {}
        local_position = 0
        
        # Process measure elements for timing
        for elem in measure:
            if elem.tag == 'backup':
                backup_duration = int(elem.find('duration').text)
                backup_ticks = int(round(backup_duration * output_resolution / divisions))
                local_position = max(0, local_position - backup_ticks)
                
            elif elem.tag == 'forward':
                forward_duration = int(elem.find('duration').text)
                forward_ticks = int(round(forward_duration * output_resolution / divisions))
                local_position = min(measure_duration, local_position + forward_ticks)
                
            elif elem.tag == 'note':
                # Skip grace notes
                if elem.find('grace') is not None:
                    continue
                
                # Check for accent articulation
                has_accent = False
                articulations = elem.find('.//notations/articulations/accent')
                if articulations is not None:
                    has_accent = True
                
                # Get note properties
                staff = get_note_staff(elem, measure)
                voice_elem = elem.find('voice')
                voice = int(voice_elem.text) if voice_elem is not None else 1
                voice_key = (staff, voice)
                
                # Initialize voice position if needed
                if voice_key not in voice_positions:
                    voice_positions[voice_key] = local_position
                
                # Get duration
                duration = int(elem.find('duration').text)
                duration_ticks = int(round(duration * output_resolution / divisions))
                
                # Check for tie elements
                tie_start = elem.find('.//tie[@type="start"]') is not None
                tie_stop = elem.find('.//tie[@type="stop"]') is not None
                
                # Calculate start position
                is_chord = elem.find('chord') is not None
                if is_chord:
                    # Use position of previous note in chord
                    note_start_ticks = measure_start_ticks + voice_positions[voice_key]
                else:
                    # Regular note starts at current voice position
                    note_start_ticks = measure_start_ticks + voice_positions[voice_key]
                    # Advance voice position
                    voice_positions[voice_key] += duration_ticks
                    # Advance local position for non-chord notes
                    if elem.find('chord') is None:  # Fix DeprecationWarning
                        local_position = min(measure_duration, local_position + duration_ticks)
                
                # Only process pitch notes (skip rests)
                if elem.find('rest') is None:
                    # Get pitch info
                    pitch_elem = elem.find('pitch')
                    step = pitch_elem.find('step').text
                    octave = int(pitch_elem.find('octave').text)
                    alter_elem = pitch_elem.find('alter')
                    alter = int(alter_elem.text) if alter_elem is not None else 0
                    
                    # Get dynamics
                    velocity = 0.63  # Default
                    dynamics = elem.find('.//dynamics/*')
                    if dynamics is not None:
                        dynamics_map = {
                            'ppp': 0.1, 'pp': 0.2, 'p': 0.3, 'mp': 0.4,
                            'mf': 0.5, 'f': 0.6, 'ff': 0.7, 'fff': 0.8
                        }
                        velocity = dynamics_map.get(dynamics.tag, 0.63)
                    
                    # Boost velocity for accented notes
                    if has_accent and velocity < 0.9:
                        velocity = min(1.0, velocity * 1.25)  # Apply 25% boost but cap at 1.0
                    
                    # Calculate precise timing
                    midi_note = note_to_midi(step, octave, alter)
                    note_start_time = ticks_to_seconds(note_start_ticks, tempos, output_resolution)
                    note_end_time = ticks_to_seconds(note_start_ticks + duration_ticks, tempos, output_resolution)
                    
                    # Create unique key for this note for tie tracking
                    tie_key = (staff, voice, midi_note)
                    
                    # Handle tie situations
                    if tie_stop and tie_key in tied_notes:
                        # This note is tied to a previous one - extend the previous note instead of adding a new one
                        prev_note = tied_notes[tie_key]
                        
                        # Update the duration of the previous note
                        prev_note.duration = note_end_time - prev_note.time
                        prev_note.duration_ticks += duration_ticks
                        
                        # Keep this tied note in our tracking if it starts a new tie
                        if tie_start:
                            tied_notes[tie_key] = prev_note
                        else:
                            # Remove from tracking if this is the end of the tie chain
                            tied_notes.pop(tie_key, None)
                    else:
                        # Create note with accurate timing
                        note = Note(
                            midi=midi_note,
                            time=note_start_time,
                            velocity=velocity,
                            duration=note_end_time - note_start_time,
                            ticks=note_start_ticks,
                            duration_ticks=duration_ticks,
                            staff=staff,
                            group=left_hand_group if staff == 2 else right_hand_group,
                            accent=1 if has_accent else 0
                        )
                        
                        # Update group for right hand based on timing gaps
                        if staff == 1 and right_hand_notes:
                            last_note_end = right_hand_notes[-1].time + right_hand_notes[-1].duration
                            if note_start_time - last_note_end > 0.2:  # Significant gap
                                right_hand_group += 1
                        
                        # Add to appropriate hand
                        if staff == 1:
                            right_hand_notes.append(note)
                        else:
                            left_hand_notes.append(note)
                        
                        # If this note starts a tie, track it
                        if tie_start:
                            tied_notes[tie_key] = note
    
    # Sort notes by time within each hand
    right_hand_notes.sort(key=lambda x: x.time)
    left_hand_notes.sort(key=lambda x: x.time)
    
    # Create tracks
    right_track = Track(notes=right_hand_notes, myInstrument=-5, theirInstrument=0)
    left_track = Track(notes=left_hand_notes, myInstrument=-5, theirInstrument=0)
    
    # Organize tracks into measures using the shared utility
    tracks_v2 = organize_tracks_v2([right_track, left_track], measure_ticks, tempos, output_resolution)
    
    # Calculate song length - max of last note end or last measure end
    song_length = 0
    if right_hand_notes or left_hand_notes:
        all_notes = right_hand_notes + left_hand_notes
        song_length = max([note.time + note.duration for note in all_notes]) if all_notes else 0
    
    # Use last measure end time if longer
    if measure_ticks:
        last_measure = measure_ticks[-1]
        last_measure_end = ticks_to_seconds(
            last_measure["ticksStart"] + last_measure["totalTicks"], 
            tempos, 
            output_resolution
        )
        song_length = max(song_length, last_measure_end)
    
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
        "resolution": output_resolution,
        "tempos": tempos,  # This already includes the discrete tempo points
        "keySignatures": key_signatures,
        "timeSignatures": time_signatures,
        "measures": measure_ticks,
        "tracksV2": tracks_v2,
        "accompanyingInstruments": [-2, -1],
        "accompanyingChannels": [0, 0],
        "name": title,
        "artist": artist,
        "accompanyingTracks": []
    }

def main():
    import sys
    
    # Handle command line arguments
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: python musicxml_to_json.py <input_file> [output_dir]")
        sys.exit(1)

    xml_path = sys.argv[1]

    # If no output directory is specified, use the same directory as the input file
    if len(sys.argv) == 3:
        output_dir = sys.argv[2]
    else:
        output_dir = os.path.dirname(xml_path)

    if not os.path.isfile(xml_path):
        print(f"Error: {xml_path} is not a file")
        sys.exit(1)

    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        output_json = parse_musicxml(xml_path)
        
        # Generate formatted output filename using the shared utility
        output_filename = format_output_filename(
            output_json['name'],
            output_json['artist'],
            xml_path
        )
        
        # Create output path in output directory
        output_path = os.path.join(output_dir, output_filename)
        
        with open(output_path, 'w') as f:
            json.dump(output_json, f)
        
        print(f"Converted: {xml_path} -> {output_path}")
    except Exception as e:
        print(f"Error processing {xml_path}: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
