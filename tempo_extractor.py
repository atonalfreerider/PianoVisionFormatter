from typing import List, Dict, Any

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
    
    # Check for any gradual tempo changes
    gradual_changes = sum(1 for t in tempos if t.get("type") == "gradual")
    if gradual_changes:
        print(f"  - Contains {gradual_changes} gradual tempo changes (ritardando/accelerando)")
    
    # Verify tempos are in ascending order by ticks
    is_sorted = all(tempos[i]["ticks"] < tempos[i+1]["ticks"] for i in range(len(tempos)-1))
    if not is_sorted:
        print("  - WARNING: Tempos are not in ascending order by ticks!")