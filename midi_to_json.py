import mido
import json
import os
from typing import List, Dict, Any
from datetime import datetime

class MidiNote:
    def __init__(self, note: int, start_time: float, end_time: float):
        self.note = note
        self.start_time = start_time
        self.end_time = end_time

def get_notes_from_midi(midi_path: str) -> List[MidiNote]:
    mid = mido.MidiFile(midi_path)
    notes: Dict[int, float] = {}  # note -> start_time
    current_time = 0
    notes_list: List[MidiNote] = []

    for msg in mid:
        current_time += msg.time
        if msg.type == 'note_on' and msg.velocity > 0:
            notes[msg.note] = current_time
        elif (msg.type == 'note_off') or (msg.type == 'note_on' and msg.velocity == 0):
            if msg.note in notes:
                start_time = notes[msg.note]
                notes_list.append(MidiNote(msg.note, start_time, current_time))
                del notes[msg.note]

    return notes_list

def create_piano_vision_json(midi_path: str) -> Dict[str, Any]:
    notes = get_notes_from_midi(midi_path)
    
    # Extract title and author from path
    filename = os.path.basename(midi_path)
    title = os.path.splitext(filename)[0]
    author = os.path.basename(os.path.dirname(midi_path))

    return {
        "header": {
            "title": title,
            "author": author,
            "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "content_version": 1
        },
        "metadata": {
            "duration": max(note.end_time for note in notes),
            "midi_file_name": filename,
            "midi_time_division": 480
        },
        "events": [
            {
                "type": "note",
                "time": note.start_time,
                "note": note.note,
                "duration": note.end_time - note.start_time
            }
            for note in notes
        ]
    }

def main():
    import sys
    if len(sys.argv) != 2:
        print("Usage: python midi_to_json.py <midi_file>")
        sys.exit(1)

    midi_path = sys.argv[1]
    output_json = create_piano_vision_json(midi_path)
    
    output_path = os.path.splitext(midi_path)[0] + '.json'
    with open(output_path, 'w') as f:
        json.dump(output_json, f, indent=2)
    
    print(f"JSON file created: {output_path}")

if __name__ == "__main__":
    main()
