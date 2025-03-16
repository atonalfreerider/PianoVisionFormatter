import xml.etree.ElementTree as ET
import json
import os
import zipfile
import tempfile
from typing import Dict, Any, Optional, Tuple
from metadata_extractor import format_output_filename
from musicxml_to_json import (Note, Track, organize_tracks_v2, ticks_to_seconds)

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

def extract_metadata(root: ET.Element) -> Tuple[str, str]:
    """Extract title and artist from MuseScore file"""
    title = ""
    artist = ""
    
    # Look for title and composer in VBox/Text elements
    for text_elem in root.findall(".//VBox/Text"):
        style = text_elem.find("style")
        if style is not None:
            if style.text == "title":
                title_text = text_elem.find("text")
                if title_text is not None:
                    title = title_text.text
            elif style.text == "composer":
                composer_text = text_elem.find("text")
                if composer_text is not None:
                    artist = composer_text.text
    
    # Look for subtitle to append to title
    for text_elem in root.findall(".//VBox/Text"):
        style = text_elem.find("style")
        if style is not None and style.text == "subtitle":
            subtitle_text = text_elem.find("text")
            if subtitle_text is not None and title:
                title = f"{title} - {subtitle_text.text}"
    
    return title, artist

def parse_musescore(mscx_content: str) -> Dict[str, Any]:
    """Parse MuseScore file and convert to Piano Vision format"""
    root = ET.fromstring(mscx_content)
    
    # Extract metadata
    title, artist = extract_metadata(root)
    
    # Get resolution (division) from the score
    division_elem = root.find(".//Division")
    resolution = int(division_elem.text) if division_elem is not None else 480
    
    # Initialize tracking variables
    right_hand_notes = []
    left_hand_notes = []
    current_measure = 0
    tempos = []
    time_signatures = []
    key_signatures = []
    
    # Process each staff (1 = right hand, 2 = left hand)
    for staff_idx, staff in enumerate(root.findall(".//Staff"), 1):
        current_ticks = 0
        
        for measure in staff.findall(".//Measure"):
            for voice in measure.findall("voice"):
                tick_position = current_ticks
                
                for elem in voice:
                    # Process tempo markings
                    if elem.tag == "Tempo":
                        tempo_value = float(elem.find("tempo").text)
                        bpm = tempo_value * 60
                        tempos.append({
                            "bpm": bpm,
                            "ticks": tick_position,
                            "time": ticks_to_seconds(tick_position, tempos, resolution)
                        })
                    
                    # Process time signatures
                    elif elem.tag == "TimeSig":
                        numerator = int(elem.find("sigN").text)
                        denominator = int(elem.find("sigD").text)
                        time_signatures.append({
                            "ticks": tick_position,
                            "timeSignature": [str(numerator), str(denominator)],
                            "measures": current_measure
                        })
                    
                    # Process key signatures
                    elif elem.tag == "KeySig":
                        key = elem.find("concertKey")
                        if key is not None:
                            key_value = int(key.text)
                            # Convert key value to key name (simplified)
                            key_map = {0: "C", 1: "G", 2: "D", -1: "F", -2: "Bb"}
                            key_signatures.append({
                                "ticks": tick_position,
                                "key": key_map.get(key_value, "C"),
                                "scale": "major"
                            })
                    
                    # Process notes
                    elif elem.tag == "Chord":
                        duration_type = elem.find("durationType").text
                        duration_ticks = {
                            "whole": resolution * 4,
                            "half": resolution * 2,
                            "quarter": resolution,
                            "eighth": resolution // 2,
                            "16th": resolution // 4,
                        }.get(duration_type, resolution)
                        
                        for note_elem in elem.findall("Note"):
                            pitch = int(note_elem.find("pitch").text)
                            velocity_elem = note_elem.find("velocity")
                            velocity = float(velocity_elem.text) / 127.0 if velocity_elem is not None else 0.8
                            
                            note = Note(
                                midi=pitch,
                                time=ticks_to_seconds(tick_position, tempos, resolution),
                                velocity=velocity,
                                duration=ticks_to_seconds(tick_position + duration_ticks, tempos, resolution) - 
                                        ticks_to_seconds(tick_position, tempos, resolution),
                                ticks=tick_position,
                                duration_ticks=duration_ticks,
                                staff=staff_idx,
                                group=0
                            )
                            
                            if staff_idx == 1:
                                right_hand_notes.append(note)
                            else:
                                left_hand_notes.append(note)
                        
                        tick_position += duration_ticks
            
            current_ticks += resolution * 4  # Advance to next measure
            current_measure += 1
    
    # Create tracks
    right_track = Track(notes=right_hand_notes, myInstrument=-5, theirInstrument=0)
    left_track = Track(notes=left_hand_notes, myInstrument=-5, theirInstrument=0)
    
    # Calculate song length
    song_length = 0
    if right_hand_notes or left_hand_notes:
        all_notes = right_hand_notes + left_hand_notes
        song_length = max([note.time + note.duration for note in all_notes])
    
    # Create measure list
    measure_ticks = []
    for i in range(current_measure):
        measure_start_ticks = i * resolution * 4
        measure_ticks.append({
            "time": ticks_to_seconds(measure_start_ticks, tempos, resolution),
            "timeSignature": time_signatures[0]["timeSignature"] if time_signatures else ["4", "4"],
            "ticksPerMeasure": resolution * 4,
            "ticksStart": measure_start_ticks,
            "totalTicks": resolution * 4,
            "type": "0.000" if i == 0 else "2"
        })
    
    # Create final output
    return {
        "supportingTracks": [
            {
                "notes": [
                    {
                        "midi": note.midi,
                        "time": note.time,
                        "velocity": note.velocity,
                        "duration": note.duration
                    }
                    for note in track.notes
                ],
                "myInstrument": track.myInstrument,
                "theirInstrument": track.theirInstrument
            }
            for track in [right_track, left_track]
        ],
        "start_time": 0,
        "song_length": song_length,
        "resolution": resolution,
        "tempos": tempos,
        "keySignatures": key_signatures,
        "timeSignatures": time_signatures,
        "measures": measure_ticks,
        "tracksV2": organize_tracks_v2([right_track, left_track], measure_ticks, tempos, resolution),
        "accompanyingInstruments": [-2, -1],
        "accompanyingChannels": [0, 0],
        "name": title,
        "artist": artist,
        "accompanyingTracks": []
    }

def main():
    import sys
    if len(sys.argv) != 3:
        print("Usage: python musescore_to_json.py <input_file> <output_dir>")
        sys.exit(1)

    mscz_path = sys.argv[1]
    output_dir = sys.argv[2]

    if not os.path.isfile(mscz_path):
        print(f"Error: {mscz_path} is not a file")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Extract .mscx content from .mscz
        mscx_content = extract_mscx_from_mscz(mscz_path)
        if not mscx_content:
            raise Exception("Failed to extract MSCX content from MSCZ file")
        
        # Parse the content
        output_json = parse_musescore(mscx_content)
        
        # Generate output filename
        output_filename = format_output_filename(
            output_json['name'],
            output_json['artist'],
            mscz_path
        )
        
        # Write output file
        output_path = os.path.join(output_dir, output_filename)
        with open(output_path, 'w') as f:
            json.dump(output_json, f)
        
        print(f"Converted: {mscz_path} -> {output_path}")
    except Exception as e:
        print(f"Error processing {mscz_path}: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
