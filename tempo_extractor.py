from typing import List, Dict, Any
import math

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
        prev_tempo = tempos[i-1]
        
        # Calculate time precisely using microseconds per quarter note
        delta_ticks = curr_tempo["ticks"] - prev_tempo["ticks"]
        microseconds_per_beat = 60000000 / prev_tempo["bpm"]
        delta_seconds = (delta_ticks * microseconds_per_beat) / (ticks_per_beat * 1000000)
        
        # Accumulate time precisely
        curr_tempo["time"] = prev_tempo["time"] + delta_seconds

def ticks_to_seconds(ticks: int, tempos: List[Dict[str, Any]], ticks_per_beat: int) -> float:
    """
    Convert tick position to seconds considering tempo changes
    Improved to match MuseScore's C++ implementation for precise temporal placement
    """
    if not tempos:
        # Default tempo of 120 BPM (500000 microseconds per beat)
        return (ticks * 500000) / (ticks_per_beat * 1000000)
        
    # Find correct tempo segment
    last_tempo = tempos[0]
    for tempo in tempos:
        if tempo["ticks"] > ticks:
            break
        last_tempo = tempo
    
    # If this is exactly at a tempo mark, return the exact time
    if ticks == last_tempo["ticks"]:
        return last_tempo["time"]
    
    # Calculate time since last tempo change with high precision
    delta_ticks = ticks - last_tempo["ticks"]
    microseconds_per_beat = 60000000 / last_tempo["bpm"]
    delta_time = (delta_ticks * microseconds_per_beat) / (ticks_per_beat * 1000000)
    
    return last_tempo["time"] + delta_time

def seconds_to_ticks(seconds: float, tempos: List[Dict[str, Any]], ticks_per_beat: int) -> int:
    """Convert seconds back to ticks considering tempo changes"""
    if not tempos:
        # Default tempo of 120 BPM
        return int(seconds * ticks_per_beat * 1000000 / 500000)
    
    # Find the tempo section that contains our time
    last_tempo = tempos[0]
    for tempo in tempos:
        if tempo["time"] > seconds:
            break
        last_tempo = tempo
    
    # Calculate ticks since the last tempo change
    delta_time = seconds - last_tempo["time"]
    microseconds_per_beat = 60000000 / last_tempo["bpm"]
    delta_ticks = int((delta_time * ticks_per_beat * 1000000) / microseconds_per_beat)
    
    return last_tempo["ticks"] + delta_ticks

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
        print(f"  {i+1}: {tempo['bpm']} BPM at tick {tempo['ticks']} (time: {tempo['time']:.3f}s)")

    # Verify time calculations
    for i in range(1, len(tempos)):
        calculated_time = ticks_to_seconds(tempos[i]["ticks"], tempos, 480)
        delta = abs(calculated_time - tempos[i]["time"])
        if delta > 0.001:  # More than 1ms difference
            print(f"WARNING: Tempo time calculation error at tempo {i+1}: {delta:.6f}s")