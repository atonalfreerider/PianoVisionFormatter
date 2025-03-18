import xml.etree.ElementTree as ET
import os
import re
from typing import Tuple, Optional, List, Dict, Any
import zipfile
import tempfile

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

def extract_mscx_from_mscz(mscz_path: str) -> Optional[str]:
    """Extract the .mscx file from a .mscz archive"""
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            with zipfile.ZipFile(mscz_path, 'r') as zip_ref:
                # Find the .mscx file in the archive
                mscx_files = [f for f in zip_ref.namelist() if f.endswith('.mscx')]
                if not mscx_files:
                    return None

                # Extract the .mscx file
                mscx_path = os.path.join(temp_dir, mscx_files[0])
                zip_ref.extract(mscx_files[0], temp_dir)

                # Read the content and return it
                with open(mscx_path, 'r', encoding='utf-8') as f:
                    return f.read()
    except Exception as e:
        print(f"Error extracting MSCX from {mscz_path}: {str(e)}")
        return None

def standardize_title(title: str) -> str:
    """Standardize a title by removing font tags and extra spaces"""
    if not title:
        return ""
    # Remove font tags
    title = re.sub(r'<font[^>]*>|</font>', '', title)
    # Replace newlines with spaces, then normalize spaces
    return re.sub(r'\s+', ' ', title.replace('\n', ' ')).strip()

def standardize_artist(artist: str) -> str:
    """Standardize an artist name by removing font tags and normalizing spaces"""
    if not artist:
        return ""
    # Remove font tags
    artist = re.sub(r'<font[^>]*>|</font>', '', artist)
    # Replace newlines with spaces
    artist = artist.replace('\n', ' ')
    # Remove text in parentheses
    artist = re.sub(r'\([^)]*\)', '', artist)
    # Remove non-alphabetic characters (except spaces)
    artist = re.sub(r'[^a-zA-ZÀ-ÿ\s]', '', artist)
    # Normalize spaces
    return re.sub(r'\s+', ' ', artist).strip()

def extract_text_content(elem: ET.Element) -> str:
    """Extract text content from element, including all child text nodes"""
    text_parts = []
    if elem.text:
        text_parts.append(elem.text)
    for child in elem:
        if child.text:
            text_parts.append(child.text)
        if child.tail:
            text_parts.append(child.tail)
    return ''.join(text_parts)

def extract_metadata_from_musescore(root: ET.Element) -> Tuple[str, str]:
    """Extract title and artist from MuseScore file"""
    title = ""
    subtitle = ""
    artist = ""
    
    # Look for title, subtitle, and composer in VBox/Text elements
    for vbox in root.findall(".//VBox"):
        for text_elem in vbox.findall("Text"):
            style = text_elem.find("style")
            text = text_elem.find("text")
            
            if style is not None and text is not None:
                content = extract_text_content(text)
                
                if style.text == "title":
                    title = standardize_title(content)
                elif style.text == "subtitle":
                    subtitle = standardize_title(content)
                elif style.text == "composer":
                    artist = standardize_artist(content)
    
    # Combine title and subtitle if both exist
    if title and subtitle:
        title = f"{title} - {subtitle}"
    
    # Create fallback metadata dictionary
    fallback_metadata = {
        "fallback_filename": "",
        "fallback_folder": ""
    }

    # Store fallback values
    if not title or not artist:
        file_path = root.get("source", "")
        if file_path:
            fallback_metadata["fallback_filename"] = os.path.splitext(os.path.basename(file_path))[0].replace('_', ' ')
            fallback_metadata["fallback_folder"] = os.path.basename(os.path.dirname(file_path))

    # Use fallbacks if needed
    if not title:
        title = fallback_metadata["fallback_filename"]
    if not artist:
        artist = fallback_metadata["fallback_folder"]

    return title, artist

def format_output_filename(title: str, artist: str, file_path: str) -> str:
    """Format the output filename according to specifications"""
    
    # Format artist (first 4 letters of last name)
    if not artist or artist.isspace():
        # Use parent folder only if no artist found
        artist = os.path.basename(os.path.dirname(file_path))
    
    # Get last word and clean it
    last_name = artist.strip().split()[-1]
    auth = re.sub(r'[^a-zA-Z]', '', last_name)[:4].lower()
    
    # Format title
    if not title or title.isspace():
        # Use original filename only if no title found
        title = os.path.splitext(os.path.basename(file_path))[0]
    
    # Remove non-alphanumeric (except spaces), then replace spaces with underscores
    formatted_title = re.sub(r'[^a-zA-Z0-9\s]', '', title)
    formatted_title = formatted_title.strip().replace(' ', '_')
    
    return f"{auth}_{formatted_title}.json"

def find_matching_midi(path: str) -> Optional[str]:
    """Find a MIDI file with matching name in the same directory as the XML file"""
    xml_dir = os.path.dirname(path)
    xml_basename = os.path.splitext(os.path.basename(path))[0]

    # Check for .mid and .midi extensions
    for ext in ['.mid', '.midi']:
        midi_path = os.path.join(xml_dir, xml_basename + ext)
        if os.path.isfile(midi_path):
            return midi_path

    return None

def extract_accented_notes(mscx_content: str) -> Dict[Tuple[int, int, int, List[int]], List[int]]:
    """Extract a dictionary of accented notes from MuseScore content.
    Returns a dict with (staff_id, measure_idx, voice_idx, pitch_tuple) tuples as keys
    and a list of note indices within that chord/position as values.
    This allows matching based on pitch patterns within measures.
    """
    root = ET.fromstring(mscx_content)
    score = root.find("Score")
    if score is None:
        return {}

    accented_notes = {}
    
    # Process each staff
    for staff in score.findall(".//Staff"):
        staff_id = int(staff.get('id', '1'))
        
        # Process measures in this staff
        for measure_idx, measure in enumerate(staff.findall("Measure")):
            # Process each voice independently
            for voice_idx, voice in enumerate(measure.findall("voice")):
                position_in_measure = 0  # Keep track of position in ticks
                
                for elem in voice:
                    if elem.tag == "Chord":
                        # Get all pitches in this chord
                        chord_pitches = []
                        note_indices = []
                        
                        # Check for accent articulation
                        has_accent = False
                        for articulation in elem.findall(".//Articulation"):
                            subtype = articulation.find("subtype")
                            if (subtype is not None and 
                                subtype.text and 
                                "accent" in subtype.text.lower()):
                                has_accent = True
                                break
                        
                        # Process all notes in the chord
                        for note_idx, note_elem in enumerate(elem.findall("Note")):
                            pitch_elem = note_elem.find("pitch")
                            if pitch_elem is not None:
                                try:
                                    midi_note = int(pitch_elem.text)
                                    chord_pitches.append(midi_note)
                                    if has_accent:
                                        note_indices.append(note_idx)
                                except (ValueError, TypeError):
                                    continue
                        
                        if has_accent and chord_pitches:
                            # Sort pitches to create consistent pattern
                            chord_pitches.sort()
                            # Store position and accent information
                            position_key = (staff_id, measure_idx, voice_idx, tuple(chord_pitches))
                            accented_notes[position_key] = note_indices
                        
                        # Get duration to advance position
                        duration_elem = elem.find("durationType")
                        if duration_elem is not None:
                            duration_type = duration_elem.text
                            duration = get_duration_ticks(duration_type, elem.findall("dots"), 480)
                            position_in_measure += duration

                    elif elem.tag == "Rest":
                        # Handle rest duration
                        duration_elem = elem.find("durationType")
                        if duration_elem is not None:
                            duration_type = duration_elem.text
                            duration = get_duration_ticks(duration_type, elem.findall("dots"), 480)
                            position_in_measure += duration

    return accented_notes

def get_duration_ticks(duration_type: str, dots_elements: list, resolution: int) -> int:
    """Calculate duration in ticks based on note type and dots"""
    duration_map = {
        "whole": resolution * 4,
        "half": resolution * 2,
        "quarter": resolution,
        "eighth": resolution // 2,
        "16th": resolution // 4,
        "32nd": resolution // 8,
        "64th": resolution // 16,
    }

    base_duration = duration_map.get(duration_type, resolution)

    # Handle dots
    if dots_elements:
        dot_count = len(dots_elements)
        dot_factor = sum(0.5 ** (i + 1) for i in range(dot_count))
        base_duration = int(base_duration * (1 + dot_factor))

    return base_duration
