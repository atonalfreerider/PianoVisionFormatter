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

def extract_tempo_from_xml(root, ticks_per_beat: int) -> List[Dict[str, Any]]:
    """Extract tempo markings from MusicXML with absolute precision for timing"""
    # First create a complete map of measures with divisions and durations
    measure_map = []
    current_divisions = None
    current_ticks = 0
    
    # Get total length of the piece in ticks for boundary checking
    total_ticks = 0
    
    # Build measure structure for accurate positioning
    for part in root.findall('.//part'):
        part_ticks = 0
        for measure in part.findall('measure'):
            measure_number = int(measure.get('number', '1'))
            
            # Extend measure_map if needed
            while len(measure_map) < measure_number:
                measure_map.append({
                    'start_ticks': current_ticks,
                    'divisions': current_divisions,
                    'duration_ticks': 4 * ticks_per_beat,  # Default 4/4 time
                })
                current_ticks += measure_map[-1]['duration_ticks']
                part_ticks += measure_map[-1]['duration_ticks']
            
            # Process attributes
            for attributes in measure.findall('attributes'):
                div = attributes.find('divisions')
                if div is not None:
                    current_divisions = int(div.text)
                    measure_map[measure_number - 1]['divisions'] = current_divisions
                
                time = attributes.find('time')
                if time is not None:
                    beats = int(time.find('beats').text)
                    beat_type = int(time.find('beat-type').text)
                    duration_ticks = int((beats * 4 * ticks_per_beat) / beat_type)
                    measure_map[measure_number - 1]['duration_ticks'] = duration_ticks
                    part_ticks += duration_ticks - measure_map[measure_number - 1]['duration_ticks']  # Adjust for changed duration
        
        # Update total_ticks if this part is longer
        total_ticks = max(total_ticks, part_ticks)
    
    # Find all explicit tempo markings in the score
    explicit_tempos = []
    tempo_directions = []  # Initialize the tempo_directions list here
    tempo_changes = []     # Initialize the tempo_changes list
    
    # Add history tracking for tempo context
    tempo_history = []  # For tracking tempo changes for "a tempo" reference
    first_tempo = None  # For "tempo primo" reference
    last_stable_tempo = None  # For "a tempo" reference
    
    # Create a more comprehensive list of tempo-related terms
    rit_indicators = [
        'rit.', 'rit', 'ritard', 'ritardando', 'ritard.', 'ritardante', 
        'rall.', 'rall', 'rallent', 'rallentando', 'rallent.', 'allargando',
        'poco rit', 'molto rit', 'gradually slower', 'slower', 'slowing',
        'allarg.', 'allarg', 'poco a poco rit', 'riten.', 'ritenuto',
        'meno mosso', 'più lento', 'piu lento', 'calando', 'slentando',
        'smorzando', 'morendo', 'lentando'
    ]
    
    accel_indicators = [
        'accel.', 'accel', 'accelerando', 'accelerato', 'stretto', 'stringendo',
        'piu mosso', 'più mosso', 'poco accel', 'gradually faster', 'faster',
        'affrettando', 'incalzando', 'stringendo', 'stringen.', 'animato',
        'più animato', 'piu animato', 'animando', 'precipitando',
        'doppio movimento', 'poco a poco accel', 'veloce', 'più vivo',
        'piu vivo', 'vivo'
    ]
    
    # Improve a_tempo detection - MuseScore often just has "a" in text marking
    a_tempo_indicators = [
        'a tempo', 'a tmp', 'a tem', 'a t', 'a', 
        'tempo i', 'tempo uno', 'in tempo', 'im tempo',
        'tempo', 'tmp', 'tem'
    ]
    
    # Improve tempo_primo detection
    tempo_primo_indicators = [
        'tempo primo', 'tempo i', 'tempo 1', 'tempo 1mo', 'tempo uno',
        'tempo originale', 'original tempo', 'first tempo', 'tempo prim.',
        't. primo', 't. 1', 't.p.', 'tmp. i', 'tempo p.'
    ]
    
    tempo_markings = {
        'adagio': 66, 'andante': 76, 'moderato': 108, 'allegro': 120, 'vivace': 140,
        'presto': 168, 'grave': 45, 'largo': 50, 'larghetto': 60, 'adagietto': 70,
        'andantino': 80, 'maestoso': 88, 'sostenuto': 66, 'comodo': 100, 'allegretto': 112,
        'vivacissimo': 150, 'prestissimo': 180, 'lento': 52, 'molto': 0,  # 'molto' modifies other terms
        'poco': 0,  # 'poco' modifies other terms
        'con moto': 10,  # adds to base tempo
        'assai': 0,  # modifies other terms
    }
    
    # Modified tempo extraction approach:
    # First, check through all score parts to find any clear tempo markings at the beginning
    # This helps us establish a true "first tempo" for tempo primo reference
    initial_explicit_tempo = None
    
    # Look for the very first tempo marking in the score
    for part in root.findall('.//part'):
        first_measure = part.find('measure')
        if first_measure is not None:
            # Check for sound elements with tempo attribute
            for direction in first_measure.findall('.//direction'):
                sound = direction.find('.//sound')
                if sound is not None and 'tempo' in sound.attrib:
                    tempo_value = float(sound.attrib['tempo'])
                    # Only accept non-120 values or verify 120 is intentional
                    is_real_tempo = False
                    
                    # Check if this tempo has text confirmation
                    for words in direction.findall('.//words'):
                        if words.text and any(tempo_word in words.text.lower() for tempo_word in tempo_markings):
                            is_real_tempo = True
                            break
                    
                    # If it's not 120 or it's confirmed by text, accept it
                    if tempo_value != 120 or is_real_tempo:
                        initial_explicit_tempo = tempo_value
                        break
            
            # If found, break out of parts loop
            if initial_explicit_tempo is not None:
                break
    
    # Now proceed with normal tempo processing
    for part in root.findall('.//part'):
        for measure_idx, measure in enumerate(part.findall('measure')):
            measure_number = int(measure.get('number', '1'))
            if measure_number > len(measure_map):
                continue
            
            measure_info = measure_map[measure_number - 1]
            divisions = measure_info.get('divisions')
            if divisions is None:
                continue
            
            # Track position within measure for accurate positioning of tempo changes
            measure_position = 0
            
            for element_idx, element in enumerate(measure):
                # Process normal tempo changes from direction elements
                if element.tag == 'direction':
                    # Look for explicit tempo markings in sound elements
                    sound = element.find('.//sound')
                    
                    # Check for text-based tempo markings first (they provide context)
                    tempo_text_found = False
                    a_tempo_found = False
                    tempo_primo_found = False
                    tempo_text_value = None
                    
                    for direction_type in element.findall('.//direction-type'):
                        for words in direction_type.findall('words'):
                            if words.text:
                                text = words.text.lower()
                                
                                # Check for a tempo or tempo primo first
                                if any(a_tempo in text for a_tempo in a_tempo_indicators):
                                    a_tempo_found = True
                                    tempo_text_found = True  # Flag that we found meaningful text
                                    break
                                
                                if any(primo in text for primo in tempo_primo_indicators):
                                    tempo_primo_found = True
                                    tempo_text_found = True  # Flag that we found meaningful text
                                    break
                                
                                # Check for standard tempo markings
                                for tempo_mark, base_tempo in tempo_markings.items():
                                    if base_tempo > 0 and tempo_mark in text:
                                        tempo_text_found = True
                                        # We found an actual tempo text marking
                                        break
                    
                    # Process sound element for tempo
                    if sound is not None and 'tempo' in sound.attrib:
                        # Calculate precise position
                        offset = 0
                        offset_elem = element.find('offset')
                        if offset_elem is not None and divisions > 0:
                            offset = int(float(offset_elem.text) * ticks_per_beat / divisions)
                        
                        tempo_ticks = measure_info['start_ticks'] + measure_position + offset
                        new_tempo = float(sound.attrib['tempo'])
                        
                        # Handle suspicious 120 BPM values - only accept if:
                        # 1. There's confirming text or
                        # 2. It's the first tempo in the piece
                        suspicious_value = (new_tempo == 120 and not tempo_text_found and 
                                           first_tempo is not None)
                        
                        # Handle "a tempo" - use last stable tempo
                        if a_tempo_found and last_stable_tempo is not None:
                            new_tempo = last_stable_tempo["bpm"]
                            print(f"Applied 'a tempo' = {new_tempo} BPM at tick {tempo_ticks}")
                        
                        # Handle "tempo primo" - ALWAYS use first tempo regardless of provided BPM value
                        elif tempo_primo_found and first_tempo is not None:
                            new_tempo = first_tempo["bpm"]
                            print(f"Applied 'tempo primo' = {new_tempo} BPM at tick {tempo_ticks}")
                        
                        # Instead of using suspicious 120 values, try to find a better value
                        elif suspicious_value:
                            # If initial explicit tempo was found, use that
                            if initial_explicit_tempo is not None and initial_explicit_tempo != 120:
                                new_tempo = initial_explicit_tempo
                                print(f"Using initial tempo {new_tempo} instead of suspicious 120 BPM at tick {tempo_ticks}")
                            
                            # Otherwise, warn but still use the value
                            else:
                                print(f"WARNING: Potentially incorrect default 120 BPM at tick {tempo_ticks}")
                        
                        # Check for tempo change type attributes
                        tempo_type = "immediate"
                        if 'tempo-type' in sound.attrib:
                            tempo_type = sound.attrib['tempo-type']
                        
                        # Store first tempo for "tempo primo" reference
                        if first_tempo is None:
                            first_tempo = {"bpm": new_tempo, "ticks": tempo_ticks, "type": tempo_type}
                        
                        # Store this tempo in history if it's not a gradual change
                        if tempo_type == "immediate":
                            tempo_history.append({"bpm": new_tempo, "ticks": tempo_ticks})
                            last_stable_tempo = {"bpm": new_tempo, "ticks": tempo_ticks}
                        
                        # Add specific source information
                        source = "explicit"
                        if a_tempo_found:
                            source = "a_tempo"
                        elif tempo_primo_found:
                            source = "tempo_primo"
                        elif tempo_text_found:
                            source = "text_marking"
                        
                        explicit_tempos.append({
                            "bpm": new_tempo,
                            "ticks": tempo_ticks,
                            "type": tempo_type,
                            "source": source
                        })
                    
                    # Handle textual tempo markings if no sound element is present
                    elif tempo_text_found:
                        # Calculate position
                        offset = 0
                        offset_elem = element.find('offset')
                        if offset_elem is not None and divisions > 0:
                            offset = int(float(offset_elem.text) * ticks_per_beat / divisions)
                        
                        tempo_ticks = measure_info['start_ticks'] + measure_position + offset
                        
                        # Handle "a tempo" - revert to last stable tempo
                        if a_tempo_found:
                            if last_stable_tempo is not None:
                                explicit_tempos.append({
                                    "bpm": last_stable_tempo["bpm"],
                                    "ticks": tempo_ticks,
                                    "type": "immediate",
                                    "source": "a_tempo"
                                })
                                print(f"Applied 'a tempo' = {last_stable_tempo['bpm']} at tick {tempo_ticks}")
                            else:
                                print("WARNING: 'a tempo' found but no previous tempo to reference")
                            continue
                        
                        # Handle "tempo primo" - revert to first tempo, IGNORE any BPM value in XML
                        if tempo_primo_found:
                            if first_tempo is not None:
                                explicit_tempos.append({
                                    "bpm": first_tempo["bpm"],
                                    "ticks": tempo_ticks,
                                    "type": "immediate",
                                    "source": "tempo_primo"
                                })
                                print(f"Applied 'tempo primo' = {first_tempo['bpm']} at tick {tempo_ticks}")
                            else:
                                print("WARNING: 'tempo primo' found but no first tempo to reference")
                            continue
                    
                    # Continue with normal text processing for other tempos
                    for direction_type in element.findall('.//direction-type'):
                        for words in direction_type.findall('words'):
                            if words.text:
                                text = words.text.lower()
                                
                                # Skip if we already processed this as a tempo marking
                                if any(a_tempo in text for a_tempo in a_tempo_indicators) or any(primo in text for primo in tempo_primo_indicators):
                                    continue
                                
                                # Calculate position
                                offset = 0
                                offset_elem = element.find('offset')
                                if offset_elem is not None and divisions > 0:
                                    offset = int(float(offset_elem.text) * ticks_per_beat / divisions)
                                
                                tempo_ticks = measure_info['start_ticks'] + measure_position + offset
                                
                                # Check for standard tempo markings
                                found_tempo = False
                                modifier = 1.0  # Default modifier
                                
                                # Check for modifiers that increase/decrease tempo
                                if 'molto' in text:
                                    modifier = 1.2  # molto increases tempo by 20%
                                elif 'poco' in text:
                                    modifier = 1.1  # poco increases tempo by 10%
                                elif 'assai' in text:
                                    modifier = 1.3  # assai increases tempo significantly
                                
                                for tempo_mark, base_tempo in tempo_markings.items():
                                    if base_tempo > 0 and tempo_mark in text:
                                        new_tempo = base_tempo * modifier
                                        
                                        # Update last stable tempo when adding a new fixed tempo
                                        last_stable_tempo = {"bpm": new_tempo, "ticks": tempo_ticks}
                                        tempo_history.append(last_stable_tempo.copy())
                                        
                                        # If this is the first tempo, record it for tempo primo
                                        if first_tempo is None:
                                            first_tempo = {"bpm": new_tempo, "ticks": tempo_ticks, "type": "immediate"}
                                        
                                        explicit_tempos.append({
                                            "bpm": new_tempo,
                                            "ticks": tempo_ticks,
                                            "type": "immediate",
                                            "source": "text_marking"
                                        })
                                        found_tempo = True
                                        print(f"Found text tempo marking '{tempo_mark}' = {new_tempo} BPM at tick {tempo_ticks}")
                                        break
                                
                                # If no standard marking found, check for ritardando/accelerando
                                if not found_tempo:
                                    if any(ri in text for ri in rit_indicators):
                                        tempo_directions.append({
                                            "type": "ritardando",
                                            "start_tick": tempo_ticks,
                                            "measure": measure_number,
                                            "text": text
                                        })
                                    elif any(ai in text for ai in accel_indicators):
                                        tempo_directions.append({
                                            "type": "accelerando",
                                            "start_tick": tempo_ticks,
                                            "measure": measure_number,
                                            "text": text
                                        })
                
                # Track note/rest durations to calculate measure position
                elif element.tag == 'note':
                    # Skip grace notes
                    if element.find('grace') is not None:
                        continue
                    
                    # Only advance position for the first note of a chord
                    if element.find('chord') is None:
                        duration_elem = element.find('duration')
                        if duration_elem is not None and divisions > 0:
                            duration = int(duration_elem.text)
                            duration_ticks = int(round(duration * ticks_per_beat / divisions))
                            measure_position += duration_ticks
                
                # Process backup and forward elements
                elif element.tag == 'backup':
                    duration_elem = element.find('duration')
                    if duration_elem is not None and divisions > 0:
                        duration = int(duration_elem.text)
                        duration_ticks = int(round(duration * ticks_per_beat / divisions))
                        measure_position = max(0, measure_position - duration_ticks)
                
                elif element.tag == 'forward':
                    duration_elem = element.find('duration')
                    if duration_elem is not None and divisions > 0:
                        duration = int(duration_elem.text)
                        duration_ticks = int(round(duration * ticks_per_beat / divisions))
                        measure_position += duration_ticks
    
    # Process tempo directions to create actual tempo changes
    for direction in tempo_directions:
        start_tick = direction["start_tick"]
        
        # Default end position is 2 measures after start or until next tempo marking
        end_measure = min(direction["measure"] + 2, len(measure_map))
        end_tick = measure_map[end_measure-1]['start_ticks'] if end_measure <= len(measure_map) else total_ticks
        
        # Look for next tempo marking or another direction that might end this one
        for temp in explicit_tempos:
            if temp["ticks"] > start_tick:
                end_tick = temp["ticks"]
                break
                
        for other_dir in tempo_directions:
            if other_dir["start_tick"] > start_tick:
                end_tick = other_dir["start_tick"]
                break
        
        # Find the effective tempo at this point - don't use a default
        if not explicit_tempos:
            raise ValueError("No tempo markings found when processing tempo direction.")
            
        # Find the current tempo from the explicit tempos list
        current_tempo = explicit_tempos[0]["bpm"]  # Start with first tempo
        for temp in sorted(explicit_tempos, key=lambda x: x["ticks"]):
            if temp["ticks"] <= start_tick:
                current_tempo = temp["bpm"]
            else:
                break
        
        # Create tempo change
        if direction["type"] == "ritardando":
            # Match reference implementation exactly:
            # - Use 160 tick intervals
            # - Decreases by ~3-4 BPM per step
            
            # Round starting tempo to nearest integer
            current_tempo = round(current_tempo)
            
            # Determine BPM decrease per step based on intensity
            bpm_step = 4  # Default decrease per step
            if 'molto' in direction['text'] or 'assai' in direction['text']:
                bpm_step = 5  # Stronger decrease
            elif 'poco' in direction['text']:
                bpm_step = 3  # Milder decrease
            
            # Use fixed 160 tick intervals (1/3 beat at 480 ticks/beat)
            tick_interval = 160
            
            # Calculate how many steps to create 
            # (ensure we don't exceed the end tick or go below minimum tempo)
            start_tick_rounded = ((start_tick + 159) // 160) * 160  # Round to nearest 160 multiple
            num_steps = min(8, (end_tick - start_tick_rounded) // tick_interval)
            
            # Generate the tempo points
            for i in range(num_steps):
                tick_pos = start_tick_rounded + (tick_interval * i)
                step_tempo = int(current_tempo - (bpm_step * i))
                if step_tempo < 40:  # Don't go below reasonable tempo
                    step_tempo = 40
                
                tempo_changes.append({
                    "bpm": step_tempo,
                    "ticks": tick_pos,
                    "type": "gradual" 
                })
                
        elif direction["type"] == "accelerando":
            # Match reference implementation exactly:
            # - Use 160 tick intervals
            # - Increases by ~3-4 BPM per step
            
            # Round starting tempo to nearest integer
            current_tempo = round(current_tempo)
            
            # Determine BPM increase per step based on intensity
            bpm_step = 4  # Default increase per step
            if 'molto' in direction['text'] or 'assai' in direction['text']:
                bpm_step = 5  # Stronger increase
            elif 'poco' in direction['text']:
                bpm_step = 3  # Milder increase
            
            # Use fixed 160 tick intervals (1/3 beat at 480 ticks/beat)
            tick_interval = 160
            
            # Calculate how many steps to create
            start_tick_rounded = ((start_tick + 159) // 160) * 160  # Round to nearest 160 multiple
            num_steps = min(8, (end_tick - start_tick_rounded) // tick_interval)
            
            # Generate the tempo points
            for i in range(num_steps):
                tick_pos = start_tick_rounded + (tick_interval * i)
                step_tempo = int(current_tempo + (bpm_step * i))
                
                tempo_changes.append({
                    "bpm": step_tempo,
                    "ticks": tick_pos,
                    "type": "gradual"
                })
    
    # Make sure explicit tempos are sorted by tick position
    explicit_tempos.sort(key=lambda x: x["ticks"])
    
    # Check if we have any explicit tempos - throw error if not
    if not explicit_tempos:
        # Look for any "a tempo" or "tempo primo" indicators that might help diagnose the problem
        missing_tempo_info = ""
        for direction in tempo_directions:
            missing_tempo_info += f"Found {direction['type']} at tick {direction['start_tick']}\n"
        
        raise ValueError(f"No tempo markings found in MusicXML. A valid tempo marking is required.\n{missing_tempo_info}")
    
    # Fix: If the first tempo is 120 BPM but there are other non-120 tempos, consider using the first non-120 tempo
    # This helps avoid the common issue of MuseScore inserting a default 120 BPM at the beginning
    if explicit_tempos[0]["bpm"] == 120 and len(explicit_tempos) > 1:
        # Look for first non-120 tempo
        for i in range(1, len(explicit_tempos)):
            if explicit_tempos[i]["bpm"] != 120:
                print(f"WARNING: First tempo is suspicious 120 BPM but found {explicit_tempos[i]['bpm']} BPM later")
                # We don't automatically replace it, but issue a warning
                break
    
    # Check if the first tempo starts after tick 0 - throw error if it does  
    if explicit_tempos[0]["ticks"] > 0:
        raise ValueError(f"First tempo marking starts at tick {explicit_tempos[0]['ticks']} instead of tick 0. A tempo marking at the beginning is required.")
    
    # Replace our old approach with the reference approach:
    # Instead of generating discrete tempos from tempo_changes, we'll use the tempo_changes directly
    all_tempos = explicit_tempos.copy()
    for change in tempo_changes:
        all_tempos.append({
            "bpm": change["bpm"],
            "ticks": change["ticks"],
            "type": change.get("type", "gradual")
        })
    
    all_tempos.sort(key=lambda x: x["ticks"])
    
    # Handle duplicates - keep only one tempo at each tick position, favor explicit over gradual
    unique_tempos = []
    seen_ticks = {}
    
    for tempo in all_tempos:
        tick_pos = tempo["ticks"]
        # If we've seen this tick position before, only replace if current is explicit and previous was gradual
        if tick_pos in seen_ticks:
            existing = seen_ticks[tick_pos]
            if tempo.get("type") == "immediate" and existing.get("type") == "gradual":
                seen_ticks[tick_pos] = tempo
        else:
            seen_ticks[tick_pos] = tempo
    
    # Get the unique tempos in order
    result_tempos = []
    for tick in sorted(seen_ticks.keys()):
        tempo = seen_ticks[tick]
        result_tempos.append({
            "bpm": int(tempo["bpm"]),  # Make sure BPM is integer
            "ticks": tempo["ticks"]
        })
    
    # Calculate absolute time values
    result = []
    current_time = 0.0
    current_ticks = 0
    current_tempo = result_tempos[0]["bpm"]
    
    for tempo in result_tempos:
        # Calculate elapsed time since previous tempo
        tick_diff = tempo["ticks"] - current_ticks
        if tick_diff > 0:
            seconds = (tick_diff / ticks_per_beat) * (60.0 / current_tempo)
            current_time += seconds
        
        # Add to final result with integer BPM
        result.append({
            "bpm": tempo["bpm"],  # Already converted to int above
            "ticks": tempo["ticks"],
            "time": round(current_time, 3)  # Always use 3 decimal places for consistent precision
        })
        
        # Update current position and tempo
        current_ticks = tempo["ticks"]
        current_tempo = tempo["bpm"]
    
    return result

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
    """Convert tick position to seconds using MuseScore-compatible tempo interpretation"""
    if not tempos:
        raise ValueError("No tempo markings available. Cannot convert ticks to seconds.")
    
    if ticks <= 0:
        return 0.0
    
    # Sort tempos to ensure correct processing
    sorted_tempos = sorted(tempos, key=lambda x: x["ticks"])
    
    # Early return if we have a pre-calculated tempo exactly at this tick position
    for tempo in sorted_tempos:
        if tempo["ticks"] == ticks:
            return tempo.get("time", 0.0)
    
    current_time = 0.0
    current_ticks = 0
    current_tempo = sorted_tempos[0]["bpm"]  # Use the first tempo, which should be at tick 0
    
    for i, tempo in enumerate(sorted_tempos):
        if tempo["ticks"] >= ticks:
            break
        
        # Get ticks and time of this tempo marker
        next_tempo_ticks = ticks if i == len(sorted_tempos) - 1 or sorted_tempos[i+1]["ticks"] > ticks else sorted_tempos[i+1]["ticks"]
        ticks_delta = next_tempo_ticks - current_ticks
        
        # Convert delta ticks to seconds using current tempo
        seconds_delta = (ticks_delta / ticks_per_beat) * (60.0 / current_tempo)
        current_time += seconds_delta
        current_ticks = next_tempo_ticks
        
        if i < len(sorted_tempos) - 1:
            # Update tempo for next segment
            current_tempo = sorted_tempos[i+1]["bpm"]
        
        if current_ticks >= ticks:
            break
    
    # Calculate any remaining time if we haven't reached the target
    if current_ticks < ticks:
        remaining_ticks = ticks - current_ticks
        # Use last available tempo
        current_time += (remaining_ticks / ticks_per_beat) * (60.0 / current_tempo)
    
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
        
        time_sig_changed = False
        
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

def verify_tempo_markings(tempos: List[Dict[str, Any]]) -> None:
    """Verify if tempo markings are valid and print diagnostics"""
    if not tempos:
        raise ValueError("No tempo markings available. Cannot validate tempos.")
    
    # Check for a_tempo and tempo_primo markings
    a_tempo_count = sum(1 for t in tempos if t.get("source") == "a_tempo")
    tempo_primo_count = sum(1 for t in tempos if t.get("source") == "tempo_primo")
    
    print(f"Found {len(tempos)} tempo markings:")
    print(f"  - 'a tempo' markings: {a_tempo_count}")
    print(f"  - 'tempo primo' markings: {tempo_primo_count}")
    
    # Check for suspicious 120 BPM values
    suspicious_120 = [i for i, t in enumerate(tempos) if t.get("bpm") == 120]
    if suspicious_120:
        print(f"  - Found {len(suspicious_120)} tempo(s) with 120 BPM at indices: {suspicious_120}")
    
    # Check the first tempo and report it
    print(f"  - First tempo: {tempos[0].get('bpm')} BPM")

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
