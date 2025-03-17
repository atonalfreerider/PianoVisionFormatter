import xml.etree.ElementTree as ET
import os
import re
from typing import Tuple, Optional
import zipfile
import tempfile

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
    """Standardize a title by removing newlines and extra spaces"""
    if not title:
        return ""
    # Replace newlines with spaces, then normalize spaces
    return re.sub(r'\s+', ' ', title.replace('\n', ' ')).strip()

def standardize_artist(artist: str) -> str:
    """Standardize an artist name by removing non-alphabetic characters and extra spaces"""
    if not artist:
        return ""
    # Replace newlines with spaces
    artist = artist.replace('\n', ' ')
    # Remove non-alphabetic characters (except spaces)
    artist = re.sub(r'[^a-zA-Z\s]', '', artist)
    # Normalize spaces
    return re.sub(r'\s+', ' ', artist).strip()

def extract_metadata_from_musescore(root: ET.Element) -> Tuple[str, str]:
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
                    title = standardize_title(title_text.text)
            elif style.text == "composer":
                composer_text = text_elem.find("text")
                if composer_text is not None:
                    artist = standardize_artist(composer_text.text)

    # Look for subtitle to append to title
    for text_elem in root.findall(".//VBox/Text"):
        style = text_elem.find("style")
        if style is not None and style.text == "subtitle":
            subtitle_text = text_elem.find("text")
            if subtitle_text is not None and title:
                subtitle = standardize_title(subtitle_text.text)
                title = f"{title} - {subtitle}"

    return title, artist

def extract_metadata_from_xml(xml_path: str) -> Tuple[str, str]:
    """Extract title and artist from MusicXML file"""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        title = None
        subtitle = None
        
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
        artist = None
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
        
        return title, artist
    except Exception as e:
        print(f"Error extracting metadata from {xml_path}: {str(e)}")
        # Return default values based on the filename
        base_name = os.path.splitext(os.path.basename(xml_path))[0]
        return base_name.replace('_', ' '), os.path.basename(os.path.dirname(xml_path))

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

def find_matching_musicxml(midi_path: str) -> str:
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
