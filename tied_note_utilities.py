"""
Utilities for properly handling tied notes in MusicXML to JSON conversion.
This follows the MuseScore implementation to correctly identify and connect tied notes.
"""

from typing import Dict, Tuple, Any, List, Optional


class TiedNoteTracker:
    """
    Track and manage tied notes throughout the score.
    Ensures that tied notes are properly combined into single longer notes.
    """
    
    def __init__(self):
        # Key format: (staff, voice, pitch)
        self.active_notes: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    
    def handle_note(self, note_data: Dict[str, Any], tie_start: bool, tie_stop: bool) -> Optional[Dict[str, Any]]:
        """
        Process a note with possible tie connections. Returns None if the note should be skipped
        (because it continues a previous tie), or returns the note data if it should be added.
        
        Args:
            note_data: Dictionary with note information (midi, time, duration, etc.)
            tie_start: Boolean indicating if note starts a tie
            tie_stop: Boolean indicating if note ends a tie
            
        Returns:
            The note data to add, or None if note continues a previous tie
        """
        # Create a key to identify this note
        note_key = (note_data["staff"], note_data["voice"], note_data["midi"])
        
        # Case 1: This note continues and possibly extends a tie
        if tie_stop and note_key in self.active_notes:
            prev_note = self.active_notes[note_key]
            
            # Extend the previous note's duration
            prev_note["duration"] += note_data["duration"]
            prev_note["duration_ticks"] += note_data["duration_ticks"]
            
            # If this note also starts a new tie, keep tracking it
            if tie_start:
                # Keep the existing note in the tracking
                pass
            else:
                # End of tie chain, remove from tracking
                del self.active_notes[note_key]
            
            # Don't add this note as a separate entity
            return None
        
        # Case 2: This note starts a new tie
        elif tie_start:
            # Store this note for future extension
            self.active_notes[note_key] = note_data
            # Add the note to the output
            return note_data
            
        # Case 3: Regular note (no ties)
        else:
            return note_data


def process_tied_notes(notes: List[Dict[str, Any]], ties: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Process a list of notes and apply tie connections to create properly combined notes.
    
    Args:
        notes: List of note data dictionaries
        ties: List of tie information
        
    Returns:
        List of processed notes with tied notes combined
    """
    # Implementation depends on how ties are represented in your data
    # This is a placeholder for a more complex implementation
    tracker = TiedNoteTracker()
    processed_notes = []
    
    for note in notes:
        # Determine if this note has tie connections (implementation depends on your data structure)
        # For example:
        tie_start = note.get("tie_start", False) 
        tie_stop = note.get("tie_stop", False)
        
        processed_note = tracker.handle_note(note, tie_start, tie_stop)
        if processed_note:
            processed_notes.append(processed_note)
    
    return processed_notes
