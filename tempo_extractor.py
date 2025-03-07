import xml.etree.ElementTree as ET
from typing import List, Dict, Any

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
    
    # Calculate times based on tempo changes
    calculate_tempo_times(tempos, ticks_per_beat)
    
    return tempos

def calculate_tempo_times(tempos: List[Dict[str, Any]], ticks_per_beat: int):
    """Calculate actual times for each tempo marking based on previous tempos"""
    current_time = 0.0
    current_ticks = 0
    current_tempo = 500000  # Default 120 BPM (500000 microseconds per beat)
    
    for i, tempo in enumerate(tempos):
        # Calculate time difference based on ticks and current tempo
        ticks_diff = tempo["ticks"] - current_ticks
        time_diff = (ticks_diff * current_tempo) / (ticks_per_beat * 1000000)
        
        # Update current time
        current_time += time_diff
        tempo["time"] = current_time
        
        # Update current position and tempo for next calculation
        current_ticks = tempo["ticks"]
        current_tempo = int(60000000 / tempo["bpm"])

def verify_tempo_markings(tempos: List[Dict[str, Any]]):
    """Verify tempo markings for debugging"""
    if not tempos:
        raise ValueError("No tempo markings found")
    
    if tempos[0]["ticks"] != 0:
        raise ValueError("First tempo marking must be at tick 0")
    
    # Print all tempo markings
    print(f"Found {len(tempos)} tempo markings:")
    for i, tempo in enumerate(tempos):
        print(f"  {i+1}: {tempo['bpm']} BPM at tick {tempo['ticks']} (time: {tempo['time']})")

def ticks_to_seconds(ticks: int, tempos: List[Dict[str, Any]], ticks_per_beat: int) -> float:
    """Convert tick position to seconds considering tempo changes"""
    if not tempos:
        # Default tempo of 120 BPM (500000 microseconds per beat)
        return (ticks * 500000) / (ticks_per_beat * 1000000)
    
    current_time = 0.0
    current_ticks = 0
    current_tempo = 500000  # Default tempo
    
    for tempo in tempos:
        if ticks < tempo["ticks"]:
            # Calculate remaining time until target ticks
            delta_ticks = ticks - current_ticks
            return current_time + (delta_ticks * current_tempo) / (ticks_per_beat * 1000000)
        
        # Add time until this tempo change
        delta_ticks = tempo["ticks"] - current_ticks
        current_time += (delta_ticks * current_tempo) / (ticks_per_beat * 1000000)
        current_ticks = tempo["ticks"]
        current_tempo = int(60000000 / tempo["bpm"])
    
    # Calculate remaining time after last tempo change
    delta_ticks = ticks - current_ticks
    return current_time + (delta_ticks * current_tempo) / (ticks_per_beat * 1000000)