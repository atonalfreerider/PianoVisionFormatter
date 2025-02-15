import xml.etree.ElementTree as ET
import json
import os
from typing import List, Dict, Any
from dataclasses import dataclass

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

def extract_tempo_from_xml(root) -> List[Dict[str, Any]]:
    """Extract tempo markings from MusicXML with proper timing"""
    tempos = []
    total_ticks = 0
    divisions = 480
    
    # First pass: Find all explicit tempo changes and calculate total ticks
    measures = root.findall('.//measure')
    current_tempo = None
    ritardando_start = None
    start_tempo = None
    
    # Calculate total ticks first
    for measure in measures:
        measure_duration = 0
        for attributes in measure.findall('attributes'):
            time = attributes.find('time')
            if time is not None:
                beats = int(time.find('beats').text)
                beat_type = int(time.find('beat-type').text)
                measure_duration = int((beats * 4 * divisions) / beat_type)
        if not measure_duration:
            measure_duration = 4 * divisions
        total_ticks += measure_duration
    
    # Second pass: Process tempo markings and ritardando
    current_ticks = 0
    for measure in measures:
        for direction in measure.findall('direction'):
            offset = 0
            offset_elem = direction.find('offset')
            if offset_elem is not None:
                offset = int(offset_elem.text)
            
            # Check for tempo marking
            sound = direction.find('.//sound')
            if sound is not None and 'tempo' in sound.attrib:
                tempo = float(sound.attrib['tempo'])
                tempo_ticks = current_ticks + offset
                tempo_time = ticks_to_seconds(tempo_ticks, tempos, divisions)
                
                tempos.append({
                    "bpm": tempo,
                    "ticks": tempo_ticks,
                    "time": tempo_time
                })
                current_tempo = tempo
                if ritardando_start is None:
                    start_tempo = tempo
            
            # Check for ritardando
            words = direction.find('direction-type/words')
            if words is not None:
                text = words.text.lower()
                if 'rit' in text or 'rall' in text:
                    ritardando_start = current_ticks + offset
                    if start_tempo is None:
                        start_tempo = current_tempo or 120
        
        # Get measure duration
        measure_duration = 0
        for attributes in measure.findall('attributes'):
            time = attributes.find('time')
            if time is not None:
                beats = int(time.find('beats').text)
                beat_type = int(time.find('beat-type').text)
                measure_duration = int((beats * 4 * divisions) / beat_type)
        if not measure_duration:
            measure_duration = 4 * divisions
        
        current_ticks += measure_duration
    
    # Add initial tempo if none found
    if not tempos:
        tempos.append({
            "bpm": current_tempo or 120,
            "ticks": 0,
            "time": 0
        })
    
    # Handle ritardando if present
    if ritardando_start is not None:
        remaining_ticks = total_ticks - ritardando_start
        end_tempo = start_tempo * 0.7  # End at 70% of initial tempo
        steps = 10  # Number of intermediate tempo points
        
        for i in range(steps):
            progress = i / steps
            point_ticks = ritardando_start + (remaining_ticks * progress)
            point_tempo = start_tempo - (start_tempo - end_tempo) * progress
            point_time = ticks_to_seconds(point_ticks, tempos, divisions)
            
            tempos.append({
                "bpm": round(point_tempo),
                "ticks": int(point_ticks),
                "time": point_time
            })
    
    # Sort tempos by tick position
    tempos.sort(key=lambda x: x["ticks"])
    
    # Remove any duplicates or too-close tempo points
    unique_tempos = []
    last_tempo = None
    
    for tempo in tempos:
        if not last_tempo or (
            (abs(tempo["bpm"] - last_tempo["bpm"]) >= 1 or  # Different tempo
             tempo["ticks"] - last_tempo["ticks"] >= 480)    # Or at least one beat apart
        ):
            unique_tempos.append(tempo)
            last_tempo = tempo
    
    return unique_tempos

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

def ticks_to_seconds(ticks: int, tempos: List[Dict[str, Any]], ticks_per_beat: int = 480) -> float:
    """Convert tick position to seconds considering tempo changes"""
    if not tempos:
        return (ticks * 60.0) / (120 * ticks_per_beat)  # Default 120 BPM
    
    current_time = 0.0
    current_ticks = 0
    current_tempo_idx = 0
    
    while current_ticks < ticks and current_tempo_idx < len(tempos):
        current_tempo = tempos[current_tempo_idx]["bpm"]
        next_tempo_ticks = (tempos[current_tempo_idx + 1]["ticks"] 
                          if current_tempo_idx + 1 < len(tempos) 
                          else ticks)
        
        if ticks <= next_tempo_ticks:
            # Target is within this tempo section
            delta_ticks = ticks - current_ticks
            return current_time + (delta_ticks * 60.0) / (current_tempo * ticks_per_beat)
        
        # Add time for this complete tempo section
        delta_ticks = next_tempo_ticks - current_ticks
        current_time += (delta_ticks * 60.0) / (current_tempo * ticks_per_beat)
        current_ticks = next_tempo_ticks
        current_tempo_idx += 1
    
    return current_time

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
    
    # Set correct resolution and initial values
    divisions = 480  # Standard MIDI resolution
    xml_divisions = None  # Will be set from XML
    
    # Get actual divisions from XML
    for attributes in root.findall('.//attributes'):
        div = attributes.find('divisions')
        if div is not None:
            xml_divisions = int(div.text)
            break
    
    if xml_divisions is None:
        xml_divisions = divisions  # Default if not found
    
    # Scale factor for converting XML divisions to output divisions (480)
    division_scale = divisions / xml_divisions
    
    # Extract tempos first (pass xml_divisions since we need original timing)
    tempos = extract_tempo_from_xml(root)
    
    # Extract key signatures
    key_signatures = extract_key_signatures(root)

    # Initialize timing variables
    current_time = 0.0
    current_ticks = 0
    measure_ticks = []
    time_signatures = []
    
    # Initialize tracks for both hands
    right_hand_notes: List[Note] = []
    left_hand_notes: List[Note] = []

    # Initialize group tracking
    right_hand_group = 0
    left_hand_group = -1  # Left hand always uses -1

    # Process measures only for piano part
    current_measure = 0
    
    for measure in root.findall(f'.//part[@id="{piano_part_id}"]/measure'):
        # Get time signature and calculate measure length first
        measure_duration_ticks = 0
        for attributes in measure.findall('attributes'):
            time = attributes.find('time')
            if time is not None:
                beats = int(time.find('beats').text)
                beat_type = int(time.find('beat-type').text)
                time_signatures.append({
                    "ticks": current_ticks,
                    "timeSignature": [beats, beat_type],
                    "measures": current_measure
                })
                # Calculate measure length based on time signature
                measure_duration_ticks = int((beats * 4 * divisions) / beat_type)
        
        if not measure_duration_ticks:
            if time_signatures:
                beats, beat_type = time_signatures[-1]["timeSignature"]
                measure_duration_ticks = int((beats * 4 * divisions) / beat_type)
            else:
                measure_duration_ticks = divisions * 4  # Default 4/4

        # Now we can safely use measure_duration_ticks
        measure_start_ticks = current_ticks
        
        # Calculate exact measure timings using tempo
        measure_time = ticks_to_seconds(measure_start_ticks, tempos)
        next_measure_time = ticks_to_seconds(measure_start_ticks + measure_duration_ticks, tempos)
        
        # Store measure timing info
        measure_ticks.append({
            "time": measure_time,
            "timeSignature": time_signatures[-1]["timeSignature"] if time_signatures else [4, 4],
            "ticksPerMeasure": measure_duration_ticks,
            "ticksStart": measure_start_ticks,
            "totalTicks": measure_duration_ticks,
            "type": 0 if current_measure == 0 else 2
        })

        # Use a dictionary to track separate voice positions for each staff.
        voice_positions = {}
        
        # Track notes for both staves in this measure
        measure_elements = []
        
        # First pass: collect all notes with voice-aware positions
        for elem in measure:
            if elem.tag in ['backup', 'forward']:
                continue
            elif elem.tag == 'note':
                if elem.find('grace') is not None:
                    continue
                try:
                    staff = get_note_staff(elem, measure)
                except ValueError:
                    staff = 1
                # Get voice from note; default to 1 if missing or non-numeric.
                voice_elem = elem.find('voice')
                try:
                    voice = int(voice_elem.text) if voice_elem is not None else 1
                except ValueError:
                    voice = 1
                duration = int(elem.find('duration').text)
                duration_ticks = int(duration * division_scale)
                
                # Initialize voice position if not set
                if (staff, voice) not in voice_positions:
                    voice_positions[(staff, voice)] = 0 if staff == 1 else int(measure_duration_ticks / 3)
                
                # Calculate note position using voice_positions.
                if elem.find('chord') is not None:
                    # For chords, reuse the previous note’s ticks.
                    note_start_ticks = measure_elements[-1]['ticks'] if measure_elements else measure_start_ticks
                else:
                    note_start_ticks = measure_start_ticks + voice_positions[(staff, voice)]
                    voice_positions[(staff, voice)] += duration_ticks
                
                measure_elements.append({
                    'elem': elem,
                    'staff': staff,
                    'voice': voice,
                    'ticks': note_start_ticks,
                    'duration': duration_ticks,
                    'is_chord': elem.find('chord') is not None
                })
        
        # Second pass: process notes and deduplicate duplicates between voices.
        current_group_notes = []
        notes_in_current_chord = []
        last_note_end = {1: 0, 2: 0}
        seen = set()  # to deduplicate key: (staff, note tick, midi)
        
        for data in measure_elements:
            elem = data['elem']
            staff = data['staff']
            note_start_ticks = data['ticks']
            duration_ticks = data['duration']
            is_chord = data['is_chord']
            
            if not is_chord and notes_in_current_chord:
                current_group_notes.extend(notes_in_current_chord)
                notes_in_current_chord = []
            
            if elem.find('rest') is None:
                pitch_elem = elem.find('pitch')
                step = pitch_elem.find('step').text
                octave = int(pitch_elem.find('octave').text)
                alter_elem = pitch_elem.find('alter')
                alter = int(alter_elem.text) if alter_elem is not None else 0
                
                velocity = 0.63
                dynamics = elem.find('.//dynamics/*')
                if dynamics is not None:
                    dynamics_map = {
                        'ppp': 0.1, 'pp': 0.2, 'p': 0.3, 'mp': 0.4,
                        'mf': 0.5, 'f': 0.6, 'ff': 0.7, 'fff': 0.8
                    }
                    velocity = dynamics_map.get(dynamics.tag, 0.63)
                
                midi_note = note_to_midi(step, octave, alter)
                note_start_time = ticks_to_seconds(note_start_ticks, tempos)
                note_end_time = ticks_to_seconds(note_start_ticks + duration_ticks, tempos)
                
                key = (staff, note_start_ticks, midi_note)
                # Skip duplicate if the same key was processed.
                if key in seen:
                    continue
                seen.add(key)
                
                note_data = {
                    'midi': midi_note,
                    'time': note_start_time,
                    'velocity': velocity,
                    'duration': note_end_time - note_start_time,
                    'ticks': note_start_ticks,
                    'duration_ticks': duration_ticks,
                    'staff': staff,
                    'group': left_hand_group if staff == 2 else right_hand_group
                }
                
                if is_chord:
                    notes_in_current_chord.append(note_data)
                else:
                    current_group_notes.append(note_data)
                    if staff == 1 and note_start_time - last_note_end[staff] > 0.1:
                        right_hand_group += 1
                    last_note_end[staff] = note_end_time
        
        if notes_in_current_chord:
            current_group_notes.extend(notes_in_current_chord)
        
        for note_data in current_group_notes:
            note = Note(**note_data)
            if note.staff == 1:
                right_hand_notes.append(note)
            else:
                left_hand_notes.append(note)
        
        current_ticks += measure_duration_ticks
        current_time = next_measure_time
        current_measure += 1

    # Create tracks
    right_track = Track(notes=sorted(right_hand_notes, key=lambda x: x.time), myInstrument=-5, theirInstrument=0)
    left_track = Track(notes=sorted(left_hand_notes, key=lambda x: x.time), myInstrument=-5, theirInstrument=0)
    
    # Organize tracks into measures
    tracks_v2 = organize_tracks_v2([right_track, left_track], measure_ticks)
    
    # Default tempo if none found
    if not tempos:
        tempos.append({
            "bpm": 120,
            "ticks": 0,
            "time": 0
        })
    
    # Create final JSON structure
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
        "song_length": current_time,
        "resolution": divisions,
        "tempos": tempos,
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

def organize_tracks_v2(tracks: List[Track], sorted_measures: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
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
            time_end = (next_measure["time"] if next_measure
                      else measure["time"] + (measure["ticksPerMeasure"] * 60.0) / (120 * 480))
            
            measure_data = {
                "direction": "up" if track_idx == 0 else "down",
                "time": measure["time"],
                "timeEnd": time_end,
                "timeSignature": measure["timeSignature"],
                "notes": [],
                "max": 0,
                "min": 127,
                "measureTicksStart": measure["ticksStart"],
                "measureTicksEnd": measure["ticksStart"] + measure["ticksPerMeasure"],
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
                "measureBars": measure_ticks / float(measure["measureTicksEnd"] - measure["measureTicksStart"]),
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
