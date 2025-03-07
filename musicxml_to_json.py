import xml.etree.ElementTree as ET
import json
import os
from typing import List, Dict, Any
from dataclasses import dataclass
from tempo_extractor import extract_tempo_from_xml, ticks_to_seconds, verify_tempo_markings

@dataclass
class Note:
    midi: int
    time: float  # in seconds
    velocity: float
    duration: float  # in seconds
    ticks: int
    duration_ticks: int
    staff: int  # 1 = right hand, 2 = left hand
    group: int  # group ID for related notes

@dataclass
class Track:
    notes: List[Note]
    myInstrument: int
    theirInstrument: int

MIDI_NOTE_NAMES = {
    'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11
}

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

def extract_metadata(root, xml_path: str) -> tuple[str, str]:
    """Extract title and subtitle from MusicXML"""
    title = None
    subtitle = None
    
    # Try to get title and subtitle from credit elements
    for credit in root.findall('.//credit'):
        credit_type = credit.find('credit-type')
        if credit_type is not None:
            if credit_type.text == 'title':
                credit_words = credit.find('credit-words')
                if credit_words is not None:
                    title = credit_words.text
            elif credit_type.text == 'subtitle':
                credit_words = credit.find('credit-words')
                if credit_words is not None:
                    subtitle = credit_words.text
    
    # Fallback to work-title if no credit title found
    if not title:
        work = root.find('.//work-title')
        if work is not None:
            title = work.text
    
    # Final fallback to filename
    if not title:
        title = os.path.splitext(os.path.basename(xml_path))[0].replace('_', ' ')
    
    # Combine title and subtitle if both exist
    if subtitle:
        title = f"{title} - {subtitle}"

    # Try to get composer from credit elements
    artist = None
    for credit in root.findall('.//credit'):
        credit_type = credit.find('credit-type')
        if credit_type is not None and credit_type.text == 'composer':
            credit_words = credit.find('credit-words')
            if credit_words is not None:
                artist = credit_words.text
                break
    
    # Fallback to creator field if no credit composer found
    if not artist:
        creator = root.find('.//creator[@type="composer"]')
        if creator is not None:
            artist = creator.text
    
    # Final fallback to parent folder name
    if not artist:
        artist = os.path.basename(os.path.dirname(xml_path))
    
    return title, artist

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

def parse_musicxml(xml_path: str) -> Dict[str, Any]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    
    # Get title and artist
    title, artist = extract_metadata(root, xml_path)
    
    # Identify piano part
    piano_part_id = identify_piano_part(root)
    
    # Use a standard output resolution that minimizes rounding errors
    output_resolution = 480  # MIDI standard resolution
    
    # Extract tempos first for consistent timing
    tempos = extract_tempo_from_xml(root, output_resolution)
    
    # Verify tempo markings for debugging
    verify_tempo_markings(tempos)
    
    # No need to check for empty tempos - the extract function will now throw an error
    # No need to add a default tempo - the extract function will verify one exists at tick 0
    
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
    
    # Initialize note collection
    right_hand_notes = []
    left_hand_notes = []
    right_hand_group = 0
    left_hand_group = -1
    
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
                    
                    # Calculate precise timing
                    midi_note = note_to_midi(step, octave, alter)
                    note_start_time = ticks_to_seconds(note_start_ticks, tempos, output_resolution)
                    note_end_time = ticks_to_seconds(note_start_ticks + duration_ticks, tempos, output_resolution)
                    
                    # Create note with accurate timing
                    note = Note(
                        midi=midi_note,
                        time=note_start_time,
                        velocity=velocity,
                        duration=note_end_time - note_start_time,
                        ticks=note_start_ticks,
                        duration_ticks=duration_ticks,
                        staff=staff,
                        group=left_hand_group if staff == 2 else right_hand_group
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
    
    # Sort notes by time within each hand
    right_hand_notes.sort(key=lambda x: x.time)
    left_hand_notes.sort(key=lambda x: x.time)
    
    # Create tracks
    right_track = Track(notes=right_hand_notes, myInstrument=-5, theirInstrument=0)
    left_track = Track(notes=left_hand_notes, myInstrument=-5, theirInstrument=0)
    
    # Organize tracks into measures
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

def calculate_rests(start_time: float, end_time: float, notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Calculate rest positions between notes in a measure"""
    rests = []
    current_time = start_time
    sorted_notes = sorted(notes, key=lambda x: x["start"])
    
    # Add initial rest if needed
    if not notes or sorted_notes[0]["start"] > start_time + 0.001:
        rests.append({
            "time": start_time,
            "noteLengthType": "dottedquarter" if end_time - start_time > 0.75 else (
                "dottedeighth" if end_time - start_time > 0.375 else "dottedsixteenth"
            )
        })
    
    # Add rests between notes
    for i in range(len(sorted_notes)):
        note = sorted_notes[i]
        if note["start"] > current_time + 0.001:
            duration = note["start"] - current_time
            rests.append({
                "time": current_time,
                "noteLengthType": "dottedquarter" if duration > 0.75 else (
                    "dottedeighth" if duration > 0.375 else "dottedsixteenth"
                )
            })
        current_time = note["end"]
        
        if i < len(sorted_notes) - 1 and sorted_notes[i + 1]["start"] > current_time + 0.001:
            duration = sorted_notes[i + 1]["start"] - current_time
            rests.append({
                "time": current_time,
                "noteLengthType": "dottedquarter" if duration > 0.75 else (
                    "dottedeighth" if duration > 0.375 else "dottedsixteenth"
                )
            })
    
    # Add final rest if needed
    if current_time < end_time - 0.001:
        duration = end_time - current_time
        rests.append({
            "time": current_time,
            "noteLengthType": "dottedquarter" if duration > 0.75 else (
                "dottedeighth" if duration > 0.375 else "dottedsixteenth"
            )
        })
    
    return rests

def organize_tracks_v2(tracks: List[Track], sorted_measures: List[Dict[str, Any]], tempos: List[Dict[str, Any]], ticks_per_beat: int = 480) -> Dict[str, List[Dict[str, Any]]]:
    """Organize notes into measures for each hand"""
    # Ensure measure consistency
    measure_count = len(sorted_measures)
    right_measures = []
    left_measures = []
    
    # Create base measures for both hands
    for track_idx in range(2):  # 0 = right, 1 = left
        for measure_idx in range(measure_count):
            measure = sorted_measures[measure_idx]
            next_measure = sorted_measures[measure_idx + 1] if measure_idx + 1 < measure_count else None
            
            # Calculate timeEnd using the tempo at this measure's end
            if next_measure:
                time_end = next_measure["time"]
            else:
                # For the last measure, calculate based on actual tempo
                measure_end_ticks = measure["ticksStart"] + measure["totalTicks"]
                time_end = ticks_to_seconds(measure_end_ticks, tempos, ticks_per_beat)
            
            # Ensure required fields exist
            ticks_per_measure = measure.get("totalTicks", measure.get("ticksPerMeasure", 1920))  # Default to 4/4 time
            
            measure_data = {
                "direction": "up" if track_idx == 0 else "down",
                "time": measure["time"],
                "timeEnd": time_end,
                "timeSignature": measure["timeSignature"],
                "notes": [],
                "max": 0,
                "min": 127,
                "measureTicksStart": measure["ticksStart"],
                "measureTicksEnd": measure["ticksStart"] + ticks_per_measure,
                "ticksPerMeasure": ticks_per_measure,  # Ensure this field is always present
                "totalTicks": ticks_per_measure,  # Add totalTicks as well for compatibility
                "rests": [{"time": measure["time"], "noteLengthType": "dottedquarter"}],
                "type": 0 if measure_idx == 0 else 2
            }
            
            if track_idx == 0:
                right_measures.append(measure_data)
            else:
                left_measures.append(measure_data)
    
    # Fill in notes for each track
    for track_idx, track in enumerate(tracks):
        measures = right_measures if track_idx == 0 else left_measures
        sorted_notes = sorted(track.notes, key=lambda x: x.ticks)
        
        for note in sorted_notes:
            # Find correct measure with strict comparison; if not found, assign to last measure.
            measure_idx = next(
                (i for i, m in enumerate(measures)
                 if m["measureTicksStart"] <= note.ticks < m["measureTicksEnd"]),
                None
            )
            if measure_idx is None:
                measure_idx = len(measures) - 1
            measure = measures[measure_idx]
            measure_ticks = note.ticks - measure["measureTicksStart"]
            
            # Create note data
            note_data = {
                "note": note.midi,
                "durationTicks": note.duration_ticks,
                "noteOffVelocity": 0,
                "ticksStart": note.ticks,
                "velocity": note.velocity,
                # Fix: Calculate measureBars correctly based on relative position within measure
                "measureBars": float(measure_ticks) / float(measure["ticksPerMeasure"]),
                "duration": note.duration,
                "noteName": get_note_name(note.midi),
                "octave": (note.midi // 12) - 1,
                "notePitch": get_note_name(note.midi).rstrip('0123456789'),
                "start": note.time,
                "end": note.time + note.duration,
                "noteLengthType": get_note_length_type(note.duration_ticks),
                "group": note.group,
                "measureInd": measure_idx,
                "noteMeasureInd": len(measure["notes"]),
                "id": f"{'r' if track_idx == 0 else 'l'}{len(measure['notes'])}"
            }
            
            measure["notes"].append(note_data)
            measure["max"] = max(measure["max"], note_data["note"])
            measure["min"] = min(measure["min"], note_data["note"])
            
            if measure["notes"]:
                measure["rests"] = calculate_rests(
                    measure["time"],
                    measure["timeEnd"],
                    measure["notes"]
                )
    
    return {
        "right": right_measures,
        "left": left_measures
    }

def get_note_name(midi_note: int) -> str:
    """Get note name from MIDI note number"""
    notes = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    note_name = notes[midi_note % 12]
    octave = (midi_note // 12) - 1
    return f"{note_name}{octave}"

def get_note_length_type(duration_ticks: int) -> str:
    """Determine note length type based on duration in ticks"""
    if duration_ticks >= 360:  # Roughly a quarter note
        return "quarter"
    elif duration_ticks >= 180:  # Roughly an eighth note
        return "eighth"
    else:
        return "dottedsixteenth"

def format_output_filename(title: str, artist: str, xml_path: str) -> str:
    """Format the output filename according to specifications"""
    import re
    
    # Format artist (first 4 letters of last name)
    if not artist or artist.isspace():
        # Use parent folder only if no artist found
        artist = os.path.basename(os.path.dirname(xml_path))
    
    # Get last word and clean it
    last_name = artist.strip().split()[-1]
    auth = re.sub(r'[^a-zA-Z]', '', last_name)[:4].lower()
    
    # Format title
    if not title or title.isspace():
        # Use original filename only if no title found
        title = os.path.splitext(os.path.basename(xml_path))[0]
    
    # Remove non-alphanumeric (except spaces), then replace spaces with underscores
    formatted_title = re.sub(r'[^a-zA-Z0-9\s]', '', title)
    formatted_title = formatted_title.strip().replace(' ', '_')
    
    return f"{auth}_{formatted_title}.json"

def main():
    import sys
    if len(sys.argv) != 3:
        print("Usage: python musicxml_to_json.py <input_file> <output_dir>")
        sys.exit(1)

    xml_path = sys.argv[1]
    output_dir = sys.argv[2]

    if not os.path.isfile(xml_path):
        print(f"Error: {xml_path} is not a file")
        sys.exit(1)

    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        output_json = parse_musicxml(xml_path)
        
        # Generate formatted output filename
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
