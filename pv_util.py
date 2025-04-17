import xml.etree.ElementTree as ET
import os
import re
from typing import Tuple, Optional, List, Dict, Any, Set
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

def extract_metadata_from_musescore(root: ET.Element) -> Tuple[str, str, Dict[str, Set[int]]]:
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

    # If either value is still empty, use fallbacks
    if not title:
        title = fallback_metadata["fallback_filename"]
    if not artist:
        artist = fallback_metadata["fallback_folder"]
        
    # Extract merge markers
    merge_measures = extract_merge_markers_from_musescore(root)

    return title, artist, merge_measures

def extract_merge_markers_from_musescore(root: ET.Element) -> Dict[str, Set[int]]:
    """Extract merge markers from MuseScore staff text
    
    Returns:
        Dictionary mapping hand ('right', 'left') to set of measure numbers to merge with orchestra
    """
    merge_measures = {'right': set(), 'left': set()}
    
    # Get all staffs to understand the structure
    all_staffs = root.findall(".//Staff")
    
    # Find all measures grouped by staff
    staff_measures = {}
    for staff_idx, staff in enumerate(all_staffs):
        measures = staff.findall("./Measure") or staff.findall(".//Measure")
        staff_measures[staff_idx] = measures
    
    # Find all StaffText elements with merge or end merge
    merge_markers = []  # List of (is_start, measure_number, staff_idx) tuples
    
    for staff_idx, staff in enumerate(all_staffs):
        # Determine which hand this staff represents (typically even=right, odd=left)
        hand_key = 'right' if staff_idx % 2 == 0 else 'left'
        
        # Get all measures for this staff
        measures = staff_measures.get(staff_idx, [])
        
        # Process each measure in this staff
        for measure_idx, measure in enumerate(measures):
            # Measure number is the index+1 within this staff's measures
            # This ensures we're using parallel measure numbering, not serial
            measure_number = measure_idx + 1
            
            # Find any merge markers in this measure
            # Check in voices first
            for voice in measure.findall("./voice"):
                for staff_text in voice.findall("./StaffText"):
                    text_elem = staff_text.find("text")
                    if text_elem is not None and text_elem.text:
                        text_content = text_elem.text.lower().strip()
                        
                        if text_content in ["merge", "end merge"]:
                            is_start = (text_content == "merge")
                            merge_markers.append((is_start, measure_number, hand_key, staff_idx))
            
            # Also check directly under the measure
            for staff_text in measure.findall("./StaffText"):
                text_elem = staff_text.find("text")
                if text_elem is not None and text_elem.text:
                    text_content = text_elem.text.lower().strip()
                    
                    if text_content in ["merge", "end merge"]:
                        is_start = (text_content == "merge")
                        merge_markers.append((is_start, measure_number, hand_key, staff_idx))
    
    # Process markers for each hand separately, sorted by measure number
    for hand_key in ['right', 'left']:
        # Get markers for this hand
        hand_markers = [m for m in merge_markers if m[2] == hand_key]
        
        # Sort by measure number
        hand_markers.sort(key=lambda x: x[1])
        
        # Process markers sequentially
        active_region = None
        
        for is_start, measure_number, _, staff_idx in hand_markers:
            if is_start:
                # Start a new merge region
                if active_region is None:
                    active_region = measure_number
                else:
                    print(f"Warning: Found 'merge' while already in merge region at measure {measure_number} for {hand_key} hand")
            else:
                # End an active merge region
                if active_region is not None:
                    # Add all measures from start to end-1
                    for m in range(active_region, measure_number):
                        merge_measures[hand_key].add(m)
                    
                    # Reset active region
                    active_region = None
                else:
                    print(f"Warning: Found 'end merge' without matching 'merge' at measure {measure_number} for {hand_key} hand")
        
        # Handle unended merge region
        if active_region is not None:
            # Get max measure number for this staff
            max_measure = max([len(measures) for staff_idx, measures in staff_measures.items() 
                              if staff_idx % 2 == (0 if hand_key == 'right' else 1)], default=0)
            
            if max_measure > 0:
                print(f"Handling unended merge region for {hand_key} hand from {active_region} to {max_measure}")
                
                # Add all measures from start to end
                for m in range(active_region, max_measure + 1):
                    merge_measures[hand_key].add(m)

    # Union markers across hands so either hand's marker applies to both
    all_measures = merge_measures['right'] | merge_measures['left']
    merge_measures['right'] = set(all_measures)
    merge_measures['left'] = set(all_measures)
    return merge_measures

def extract_metadata_from_xml(xml_path: str) -> Tuple[str, str, Dict[str, Set[int]]]:
    """Extract title, artist, and measure merge information from MusicXML file"""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        title = None
        subtitle = None
        artist = None
        
        # Create fallback metadata dictionary
        fallback_metadata = {
            "fallback_filename": os.path.splitext(os.path.basename(xml_path))[0].replace('_', ' '),
            "fallback_folder": os.path.basename(os.path.dirname(xml_path))
        }
        
        # Try to get title and subtitle from credit elements
        for credit in root.findall('.//credit'):
            credit_type = credit.find('credit-type')
            if credit_type is not None:
                if credit_type.text == 'title':
                    credit_words = credit.find('credit-words')
                    if credit_words is not None:
                        title = standardize_title(credit_words.text)
                elif credit_type.text == 'subtitle':
                    credit_words = credit.find('credit-words')
                    if credit_words is not None:
                        subtitle = standardize_title(credit_words.text)
        
        # Fallback to work-title if no credit title found
        if not title:
            work = root.find('.//work-title')
            if work is not None:
                title = standardize_title(work.text)
        
        # Final fallback to filename
        if not title:
            title = os.path.splitext(os.path.basename(xml_path))[0].replace('_', ' ')
        
        # Combine title and subtitle if both exist
        if subtitle:
            title = f"{title} - {subtitle}"

        # Try to get composer from credit elements
        for credit in root.findall('.//credit'):
            credit_type = credit.find('credit-type')
            if credit_type is not None and credit_type.text == 'composer':
                credit_words = credit.find('credit-words')
                if credit_words is not None:
                    artist = standardize_artist(credit_words.text)
                    break
        
        # Fallback to creator field if no credit composer found
        if not artist:
            creator = root.find('.//creator[@type="composer"]')
            if creator is not None:
                artist = standardize_artist(creator.text)
        
        # Final fallback to parent folder name
        if not artist:
            artist = os.path.basename(os.path.dirname(xml_path))
        
        return title, artist, extract_merge_markers_from_xml(root)
    except Exception as e:
        print(f"Error extracting metadata from {xml_path}: {str(e)}")
        # Return default values based on the filename
        base_name = os.path.splitext(os.path.basename(xml_path))[0]
        return base_name.replace('_', ' '), os.path.basename(os.path.dirname(xml_path)), extract_merge_markers_from_xml(tree.getroot())

def extract_merge_markers_from_xml(root) -> Dict[str, Set[int]]:
    """Extract merge markers from MusicXML staff text
    
    Returns:
        Dictionary mapping hand ('right', 'left') to set of measure numbers to merge with orchestra
    """
    merge_measures = {'right': set(), 'left': set()}
    
    # Find all piano-orchestral parts
    orchestral_part_ids = []
    
    for part_list in root.findall('.//part-list'):
        for score_part in part_list.findall('.//score-part'):
            part_id = score_part.get('id')
            part_name_elem = score_part.find('.//part-name')
            
            if part_name_elem is not None and part_name_elem.text:
                part_name = part_name_elem.text.lower()
                if 'piano-orchestral' in part_name or 'orchestral piano' in part_name:
                    orchestral_part_ids.append(part_id)
    
    # Look for merge markers in each orchestral part
    for part_id in orchestral_part_ids:
        part = root.find(f'.//part[@id="{part_id}"]')
        if part is None:
            continue
        
        # Process each hand (assuming first part is right hand, second is left)
        hand_idx = orchestral_part_ids.index(part_id)
        hand_key = 'right' if hand_idx == 0 or len(orchestral_part_ids) == 1 else 'left'
        
        # Track active merge regions
        current_merge_start = None
        
        # Check each measure for merge markers
        for measure in part.findall('.//measure'):
            measure_number = int(measure.get('number'))
            
            # Look for any text elements containing "merge" or "end merge"
            for direction in measure.findall('.//direction'):
                for text in direction.findall('.//words'):
                    if text.text:
                        text_content = text.text.lower().strip()
                        
                        # Start merge region
                        if text_content == "merge" and current_merge_start is None:
                            current_merge_start = measure_number
                        
                        # End merge region (exclusive of this measure)
                        elif text_content == "end merge" and current_merge_start is not None:
                            # Add all measures from start to end-1
                            for m in range(current_merge_start, measure_number):
                                merge_measures[hand_key].add(m)
                            
                            # Reset for potential next region
                            current_merge_start = None
        
        # Handle case where "end merge" was never found
        if current_merge_start is not None:
            # Get the last measure number
            last_measure = max([int(m.get('number')) for m in part.findall('.//measure')])
            
            # Add all measures from start to last
            for m in range(current_merge_start, last_measure + 1):
                merge_measures[hand_key].add(m)
    
    # Union markers across hands so either hand's marker applies to both
    all_measures = merge_measures['right'] | merge_measures['left']
    merge_measures['right'] = set(all_measures)
    merge_measures['left'] = set(all_measures)
    return merge_measures

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

def find_matching_musescore(midi_path: str) -> str:
    """Look for a matching MusicXML file for a given MIDI file"""
    base_name = os.path.splitext(os.path.basename(midi_path))[0]
    parent_dir = os.path.dirname(midi_path)

    # Try common MusicXML extensions
    xml_extensions = ['.mscz', '.mscx']

    for ext in xml_extensions:
        potential_path = os.path.join(parent_dir, base_name + ext)
        if os.path.exists(potential_path):
            return potential_path

    return None

def find_matching_musicxml(midi_path: str) -> Optional[str]:
    """Look for a matching MusicXML file for a given MIDI file"""
    base_name = os.path.splitext(os.path.basename(midi_path))[0]
    parent_dir = os.path.dirname(midi_path)
    
    # Try common MusicXML extensions
    xml_extensions = ['.xml', '.musicxml', '.mxl']
    
    for ext in xml_extensions:
        potential_path = os.path.join(parent_dir, base_name + ext)
        if os.path.exists(potential_path):
            return potential_path
    
    return None

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

def extract_accented_notes(mscx_content: str) -> Dict[Tuple[int, int, int, Tuple[int]], List[int]]:
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
    resolution_elem = root.find(".//Division")
    resolution = int(resolution_elem.text) if resolution_elem is not None else 480

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
                        note_index_counter = 0
                        for note_elem in elem.findall("Note"):
                            pitch_elem = note_elem.find("pitch")
                            if pitch_elem is not None:
                                try:
                                    midi_note = int(pitch_elem.text)
                                    chord_pitches.append(midi_note)
                                    if has_accent:
                                        note_indices.append(note_index_counter)
                                    note_index_counter += 1
                                except (ValueError, TypeError):
                                    continue

                        if has_accent and chord_pitches:
                            # Sort pitches to create consistent pattern
                            chord_pitches.sort()
                            # Store position and accent information
                            # Using position_in_measure as a proxy for chord index within voice
                            position_key = (staff_id, measure_idx, voice_idx, tuple(chord_pitches))
                            accented_notes[position_key] = note_indices

                        # Get duration to advance position if it's not a continuation chord
                        is_chord_continuation = elem.find("chord") is not None
                        if not is_chord_continuation:
                            duration_elem = elem.find("durationType")
                            if duration_elem is not None:
                                duration_type = duration_elem.text
                                duration = get_duration_ticks(duration_type, elem.findall("dots"), resolution)
                                # Handle tuplets if necessary (simplified for now)
                                position_in_measure += duration

                    elif elem.tag == "Rest":
                        # Handle rest duration
                        duration_elem = elem.find("durationType")
                        if duration_elem is not None:
                            duration_type = duration_elem.text
                            duration = get_duration_ticks(duration_type, elem.findall("dots"), resolution)
                            # Handle tuplets if necessary (simplified for now)
                            position_in_measure += duration

    return accented_notes
