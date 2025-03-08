from typing import List, Dict, Any

def is_percussion_program(program: int) -> bool:
    """Check if MIDI program number is percussion"""
    return program >= 112 and program <= 119  # Percussion sound effects

def merge_orchestral_parts(primary_notes: List[Dict[str, Any]], 
                         secondary_notes: List[Dict[str, Any]],
                         measure_start: float,
                         measure_duration: float,
                         is_upper_staff: bool = True) -> List[Dict[str, Any]]:
    """
    Merge primary piano with secondary piano/orchestra parts based on complexity
    """
    # Get notes in this measure
    measure_primary = [n for n in primary_notes 
                      if measure_start <= n.get("start", 0) < measure_start + measure_duration]
    
    # Always include primary piano notes
    result_notes = measure_primary[:]
    
    # Check if we can include additional notes
    if len(measure_primary) <= 1:  # Empty or single note measure
        # Filter secondary notes for this measure and staff
        measure_secondary = [n for n in secondary_notes 
                           if measure_start <= n.get("start", 0) < measure_start + measure_duration]
        
        # Filter by pitch range for appropriate staff
        if is_upper_staff:
            measure_secondary = [n for n in measure_secondary if n.get("midi", 0) >= 60]
        else:
            measure_secondary = [n for n in measure_secondary if n.get("midi", 0) < 60]
        
        # Add filtered secondary notes
        result_notes.extend(measure_secondary)
    
    # Sort by start time
    return sorted(result_notes, key=lambda x: x.get("start", 0))

def is_valid_orchestra_instrument(program: int) -> bool:
    """Check if instrument is valid for orchestra mode"""
    if is_percussion_program(program):
        return False
        
    # Include strings, woodwinds, brass, and piano family
    valid_ranges = [
        (0, 7),    # Piano family
        (40, 51),  # Strings
        (64, 79),  # Brass/Reed
        (80, 95),  # Pipe/Woodwinds
    ]
    
    return any(start <= program <= end for start, end in valid_ranges)
