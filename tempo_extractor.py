from typing import List, Dict, Any, Optional

# Add constants from the MuseScore implementation
TEMPO_FACTORS = {
    'accelerando': 1.33,
    'accel': 1.33,
    'stringendo': 1.5,
    'allargando': 0.75,
    'rallentando': 0.75,
    'rall': 0.75,
    'ritardando': 0.75,
    'rit': 0.75,
    'calando': 0.5,
    'lentando': 0.75,
    'morendo': 0.5,
    'precipitando': 1.15,
    'smorzando': 0.5,
    'sostenuto': 0.95,
}

def tempo_factor_for_direction(direction_type: str) -> float:
    """Return the factor to apply for a gradual tempo change based on MuseScore factors"""
    direction_lower = direction_type.lower()
    
    # Find the matching factor
    for indicator, factor in TEMPO_FACTORS.items():
        if indicator in direction_lower:
            return factor
    
    # Default factors if not found
    if 'accel' in direction_lower or 'string' in direction_lower:
        return 1.33  # Default accelerando factor
    elif 'rit' in direction_lower or 'rall' in direction_lower or 'lent' in direction_lower:
        return 0.75  # Default ritardando factor
    
    return 1.0  # No change

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
    # First create map of segments and positioning
    segments_map = {}
    measure_map = []
    current_divisions = None
    current_ticks = 0
    
    # Get total length of the piece in ticks for boundary checking
    total_ticks = 0
    
    # Step 1: Build accurate segment and measure structure - similar to MuseScore's approach
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
                    'elements': []  # Store elements in order
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
                    part_ticks += duration_ticks - measure_map[measure_number - 1]['duration_ticks']
            
            # Store elements in order for this measure
            measure_elements = []
            for elem_idx, elem in enumerate(measure):
                measure_elements.append({
                    'type': elem.tag,
                    'element': elem,
                    'index': elem_idx
                })
            measure_map[measure_number - 1]['elements'] = measure_elements
        
        # Update total_ticks if this part is longer
        total_ticks = max(total_ticks, part_ticks)
    
    # Calculate end ticks for each measure
    for measure in measure_map:
        measure['end_ticks'] = measure['start_ticks'] + measure['duration_ticks']
    
    # Find all explicit tempo markings in the score
    explicit_tempos = []
    tempo_directions = []
    tempo_changes = []
    
    # Add history tracking for tempo context
    tempo_history = []
    first_tempo = None
    last_stable_tempo = None
    
    # Keep existing tempo indicator lists
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
    
    a_tempo_indicators = [
        'a tempo', 'a tmp', 'a tem', 'a t', 'a', 
        'tempo i', 'tempo uno', 'in tempo', 'im tempo',
        'tempo', 'tmp', 'tem'
    ]
    
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
    
    # Step 2: Process tempo markings with precise positioning based on segments
    for part in root.findall('.//part'):
        # Track position in each measure for precise placement
        for measure_idx, measure in enumerate(part.findall('measure')):
            measure_number = int(measure.get('number', '1'))
            if measure_number > len(measure_map):
                continue
            
            measure_info = measure_map[measure_number - 1]
            divisions = measure_info.get('divisions')
            if divisions is None:
                continue
            
            measure_start_ticks = measure_info['start_ticks']
            measure_duration = measure_info['duration_ticks']
            
            # Key change: Track exact segment positions for each element
            # This ensures tempo markings align precisely with where they appear in the score
            segments = []  # List of segments with their absolute tick positions
            
            # Calculate tick position for each element in the measure
            measure_position = 0
            
            for elem_idx, element in enumerate(measure):
                # Store the element's starting tick position
                segment_tick = measure_start_ticks + measure_position
                
                # Process tempo markings - attach them exactly to the segment
                if element.tag == 'direction':
                    # Find text markings first for context
                    tempo_text_found = False
                    a_tempo_found = False
                    tempo_primo_found = False
                    direction_text = ""
                    
                    for direction_type in element.findall('.//direction-type'):
                        for words in direction_type.findall('words'):
                            if words.text:
                                text = words.text.lower()
                                direction_text = text
                                
                                # Check important tempo indications
                                if any(a_tempo in text for a_tempo in a_tempo_indicators):
                                    a_tempo_found = True
                                    tempo_text_found = True
                                
                                if any(primo in text for primo in tempo_primo_indicators):
                                    tempo_primo_found = True
                                    tempo_text_found = True
                                
                                for tempo_mark, base_tempo in tempo_markings.items():
                                    if base_tempo > 0 and tempo_mark in text:
                                        tempo_text_found = True
                    
                    # Calculate exact tick position
                    # In MuseScore, tempos are attached to specific segments or chords
                    tempo_ticks = segment_tick
                    
                    # Apply offset if present
                    offset_elem = element.find('offset')
                    if offset_elem is not None and divisions > 0:
                        offset = int(float(offset_elem.text) * ticks_per_beat / divisions)
                        tempo_ticks += offset
                    
                    # Ensure tempo is within measure boundaries
                    if tempo_ticks >= measure_start_ticks + measure_duration:
                        tempo_ticks = measure_start_ticks + measure_duration - 1
                    
                    # Process sound element for tempo
                    sound = element.find('.//sound')
                    if sound is not None and 'tempo' in sound.attrib:
                        new_tempo = float(sound.attrib['tempo'])
                        
                        # Handle "a tempo" - return to last stable tempo
                        if a_tempo_found and last_stable_tempo is not None:
                            new_tempo = last_stable_tempo["bpm"]
                            print(f"Applied 'a tempo' = {new_tempo} BPM at tick {tempo_ticks}")
                        
                        # Handle "tempo primo" - return to first tempo
                        elif tempo_primo_found and first_tempo is not None:
                            new_tempo = first_tempo["bpm"]
                            print(f"Applied 'tempo primo' = {new_tempo} BPM at tick {tempo_ticks}")
                        
                        # Check for tempo change type attributes
                        tempo_type = "immediate"
                        if 'tempo-type' in sound.attrib:
                            tempo_type = sound.attrib['tempo-type']
                        
                        # Store first tempo for reference
                        if first_tempo is None:
                            first_tempo = {"bpm": new_tempo, "ticks": tempo_ticks, "type": tempo_type}
                        
                        # Update tempo history
                        if tempo_type == "immediate":
                            last_stable_tempo = {"bpm": new_tempo, "ticks": tempo_ticks}
                            tempo_history.append(last_stable_tempo.copy())
                        
                        # Add source information
                        source = "explicit"
                        if a_tempo_found:
                            source = "a_tempo"
                        elif tempo_primo_found:
                            source = "tempo_primo"
                        elif tempo_text_found:
                            source = "text_marking"
                        
                        # Add the tempo marking with precise positioning
                        explicit_tempos.append({
                            "bpm": new_tempo,
                            "ticks": tempo_ticks,
                            "type": tempo_type,
                            "source": source,
                            "measure": measure_number
                        })
                    
                    # Handle text-based tempo indicators
                    elif tempo_text_found:
                        # Handle "a tempo"
                        if a_tempo_found and last_stable_tempo is not None:
                            explicit_tempos.append({
                                "bpm": last_stable_tempo["bpm"],
                                "ticks": tempo_ticks,
                                "type": "immediate",
                                "source": "a_tempo",
                                "measure": measure_number
                            })
                        
                        # Handle "tempo primo"
                        elif tempo_primo_found and first_tempo is not None:
                            explicit_tempos.append({
                                "bpm": first_tempo["bpm"],
                                "ticks": tempo_ticks,
                                "type": "immediate",
                                "source": "tempo_primo",
                                "measure": measure_number
                            })
                    
                    # Process other text directions
                    for direction_type in element.findall('.//direction-type'):
                        for words in direction_type.findall('words'):
                            if words.text:
                                text = words.text.lower()
                                
                                # Skip already processed tempo words
                                if any(a_tempo in text for a_tempo in a_tempo_indicators) or any(primo in text for primo in tempo_primo_indicators):
                                    continue
                                
                                # Check for standard tempo markings
                                found_tempo = False
                                modifier = 1.0
                                
                                # Apply modifiers
                                if 'molto' in text:
                                    modifier = 1.2
                                elif 'poco' in text:
                                    modifier = 1.1
                                elif 'assai' in text:
                                    modifier = 1.3
                                
                                for tempo_mark, base_tempo in tempo_markings.items():
                                    if base_tempo > 0 and tempo_mark in text:
                                        new_tempo = base_tempo * modifier
                                        
                                        # Update tempo history
                                        last_stable_tempo = {"bpm": new_tempo, "ticks": tempo_ticks}
                                        tempo_history.append(last_stable_tempo.copy())
                                        
                                        # Check for first tempo
                                        if first_tempo is None:
                                            first_tempo = {"bpm": new_tempo, "ticks": tempo_ticks, "type": "immediate"}
                                        
                                        explicit_tempos.append({
                                            "bpm": new_tempo,
                                            "ticks": tempo_ticks,
                                            "type": "immediate",
                                            "source": "text_marking",
                                            "measure": measure_number
                                        })
                                        found_tempo = True
                                        break
                                
                                # Check for ritardando/accelerando
                                if not found_tempo:
                                    if any(ri in text for ri in rit_indicators):
                                        tempo_directions.append({
                                            "type": "ritardando",
                                            "start_tick": tempo_ticks,
                                            "measure": measure_number,
                                            "text": text,  # Store full text for factor calculation
                                            "recovery": False  # Don't auto-recover unless specified
                                        })
                                    elif any(ai in text for ai in accel_indicators):
                                        tempo_directions.append({
                                            "type": "accelerando",
                                            "start_tick": tempo_ticks,
                                            "measure": measure_number,
                                            "text": text,  # Store full text for factor calculation
                                            "recovery": False  # Don't auto-recover unless specified
                                        })
                
                # Track position for note elements
                elif element.tag == 'note':
                    # Skip grace notes
                    if element.find('grace') is not None:
                        continue
                    
                    # Only advance position for non-chord notes
                    if element.find('chord') is None:
                        duration_elem = element.find('duration')
                        if duration_elem is not None and divisions > 0:
                            duration = int(duration_elem.text)
                            duration_ticks = int(round(duration * ticks_per_beat / divisions))
                            measure_position += duration_ticks
                
                # Handle position adjustments
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
    
    # Step 3: Process gradual tempo changes (like ritardando)
    # Focus on precise positioning and correct factor application
    for direction in tempo_directions:
        start_tick = direction["start_tick"]
        
        # Find where this direction ends
        end_measure = min(direction["measure"] + 2, len(measure_map))
        end_tick = measure_map[end_measure-1]['start_ticks']
        
        # Find next tempo or direction
        for temp in explicit_tempos:
            if temp["ticks"] > start_tick:
                end_tick = temp["ticks"]
                break
        
        for other_dir in tempo_directions:
            if other_dir["start_tick"] > start_tick:
                end_tick = other_dir["start_tick"]
                break
        
        # Get current tempo at this point
        current_tempo = explicit_tempos[0]["bpm"]
        for temp in sorted(explicit_tempos, key=lambda x: x["ticks"]):
            if temp["ticks"] <= start_tick:
                current_tempo = temp["bpm"]
            else:
                break
        
        # Calculate target tempo using MuseScore factor approach
        tempo_factor = tempo_factor_for_direction(direction["text"])
        
        # Apply factor based on direction
        if direction["type"] == "ritardando":
            # Slow down - target is current × factor (factor is < 1)
            target_tempo = max(15, int(current_tempo * tempo_factor))
        else:  # accelerando
            # Speed up - target is current × factor (factor is > 1)
            target_tempo = min(240, int(current_tempo * tempo_factor))
        
        # Calculate gradual change
        duration_ticks = end_tick - start_tick
        num_steps = 10  # Standard easing steps
        
        # Ensure minimum interval
        tick_interval = max(10, duration_ticks // num_steps)
        num_steps = max(1, duration_ticks // tick_interval)
        
        # Calculate change per step
        bpm_change = (target_tempo - current_tempo) / num_steps
        
        # Generate tempo points
        for i in range(num_steps):
            tick_pos = start_tick + (tick_interval * i)
            # Linear interpolation
            step_tempo = int(round(current_tempo + (bpm_change * i)))
            
            # Apply bounds
            if direction["type"] == "ritardando":
                step_tempo = max(15, step_tempo)
            else:
                step_tempo = min(240, step_tempo)
            
            tempo_changes.append({
                "bpm": step_tempo,
                "ticks": tick_pos,
                "type": "gradual"
            })
        
        # IMPORTANT: In MuseScore, tempos DO NOT automatically recover
        # unless there's an explicit tempo marking afterwards or a specific "a tempo"
        # We should NOT automatically add recovery tempos
    
    # Step 4: Sort and merge all tempos
    explicit_tempos.sort(key=lambda x: x["ticks"])
    
    # Check if we have any explicit tempos (required)
    if not explicit_tempos:
        raise ValueError("No tempo markings found in MusicXML. A valid tempo marking is required.")
    
    # Ensure first tempo is at tick 0
    if explicit_tempos[0]["ticks"] > 0:
        print(f"First tempo starts at tick {explicit_tempos[0]['ticks']}, adding tempo at start")
        first_tempo = explicit_tempos[0].copy()
        first_tempo["ticks"] = 0
        explicit_tempos.insert(0, first_tempo)
    
    # Merge all tempo changes
    all_tempos = explicit_tempos.copy() + tempo_changes
    all_tempos.sort(key=lambda x: x["ticks"])
    
    # Handle duplicates - favor explicit over gradual
    seen_ticks = {}
    for tempo in all_tempos:
        tick_pos = tempo["ticks"]
        if tick_pos in seen_ticks:
            existing = seen_ticks[tick_pos]
            if tempo.get("type") == "immediate" and existing.get("type") == "gradual":
                seen_ticks[tick_pos] = tempo
        else:
            seen_ticks[tick_pos] = tempo
    
    # Create final result
    result_tempos = []
    for tick in sorted(seen_ticks.keys()):
        tempo = seen_ticks[tick]
        result_tempos.append({
            "bpm": int(tempo["bpm"]),
            "ticks": tempo["ticks"]
        })
    
    # Calculate time values
    result = []
    current_time = 0.0
    current_ticks = 0
    current_tempo = result_tempos[0]["bpm"]
    
    for tempo in result_tempos:
        # Calculate elapsed time
        tick_diff = tempo["ticks"] - current_ticks
        if tick_diff > 0:
            seconds = (tick_diff / ticks_per_beat) * (60.0 / current_tempo)
            current_time += seconds
        
        # Add to final result
        result.append({
            "bpm": tempo["bpm"],
            "ticks": tempo["ticks"],
            "time": round(current_time, 3)
        })
        
        # Update tracking
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