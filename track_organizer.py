from typing import List, Dict, Any
from notes import Track
from pv_util import ticks_to_seconds

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

def organize_tracks_v2(tracks: List[Track], sorted_measures: List[Dict[str, Any]], tempos: List[Dict[str, Any]], 
                       ticks_per_beat: int = 480) -> Dict[str, List[Dict[str, Any]]]:
    """
    Organize notes into measures for each hand
    Improved implementation that ensures precise temporal placement
    """
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
                "ticksPerMeasure": ticks_per_measure,
                "totalTicks": ticks_per_measure,
                "rests": [{"time": measure["time"], "noteLengthType": "dottedquarter"}],
                "type": measure.get("type", 0 if measure_idx == 0 else 2)
            }
            
            if track_idx == 0:
                right_measures.append(measure_data)
            else:
                left_measures.append(measure_data)
    
    # Fill in notes for each track with precise timing
    for track_idx, track in enumerate(tracks):
        measures = right_measures if track_idx == 0 else left_measures
        # Use sorted notes to ensure proper temporal ordering
        sorted_notes = sorted(track.notes, key=lambda x: (x.ticks, x.midi))
        
        for note in sorted_notes:
            # Find correct measure with strict tick-based comparison
            measure_idx = next(
                (i for i, m in enumerate(measures)
                 if m["measureTicksStart"] <= note.ticks < m["measureTicksEnd"]),
                len(measures) - 1  # Default to last measure if not found
            )
            
            measure = measures[measure_idx]
            # Calculate precise position within measure in ticks
            measure_ticks = note.ticks - measure["measureTicksStart"]
            
            # Ensure accurate measure bars calculation (0.0 - 1.0)
            measure_bars = float(measure_ticks) / float(measure["ticksPerMeasure"])
            
            # Create note data with precise timing values
            note_data = {
                "note": note.midi,
                "durationTicks": note.duration_ticks,
                "noteOffVelocity": 0,
                "ticksStart": note.ticks,
                "velocity": note.velocity,
                "measureBars": measure_bars,
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
                "id": f"{'r' if track_idx == 0 else 'l'}{len(measure['notes'])}",
                "accent": note.accent
            }
            
            measure["notes"].append(note_data)
            measure["max"] = max(measure["max"], note_data["note"])
            measure["min"] = min(measure["min"], note_data["note"])
    
    # Update rests for all measures
    for measures in [right_measures, left_measures]:
        for measure in measures:
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
