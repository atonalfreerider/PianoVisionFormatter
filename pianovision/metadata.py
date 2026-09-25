"""Score metadata: title/composer normalisation, output file naming,
"merge"/"end merge" staff-text markers and accent extraction.

Ported from the legacy ``pv_util.py``; the string handling is intentionally
unchanged so output file names and ``name``/``artist`` fields stay identical.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Dict, List, Optional, Set, Tuple


def read_mscx_text(mscz_path: str) -> Optional[str]:
    """Return the text of the first ``.mscx`` member of an ``.mscz`` archive."""
    try:
        with zipfile.ZipFile(mscz_path) as z:
            names = [n for n in z.namelist() if n.endswith(".mscx")]
            if not names:
                return None
            return z.read(names[0]).decode("utf-8")
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError) as e:
        print(f"Error extracting MSCX from {mscz_path}: {e}")
        return None


def standardize_title(title: str) -> str:
    if not title:
        return ""
    title = re.sub(r'<font[^>]*>|</font>', '', title)
    return re.sub(r'\s+', ' ', title.replace('\n', ' ')).strip()


def standardize_artist(artist: str) -> str:
    if not artist:
        return ""
    artist = re.sub(r'<font[^>]*>|</font>', '', artist)
    artist = artist.replace('\n', ' ')
    artist = re.sub(r'\([^)]*\)', '', artist)
    artist = re.sub(r'[^a-zA-ZÀ-ÿ\s]', '', artist)
    return re.sub(r'\s+', ' ', artist).strip()


# Canonical composer variants (substring -> canonical last name). Order matters.
_COMPOSER_VARIANTS = [
    (r'rachmaninov', 'Rachmaninoff'),
    (r'rachmaninoff', 'Rachmaninoff'),
    (r'\brach\b', 'Rachmaninoff'),
    (r'chopin', 'Chopin'),
    (r'beethoven', 'Beethoven'),
    (r'bach', 'Bach'),
    (r'mozart', 'Mozart'),
    (r'schubert', 'Schubert'),
    (r'schumann', 'Schumann'),
    (r'liszt', 'Liszt'),
    (r'debussy', 'Debussy'),
    (r'ravel', 'Ravel'),
    (r'prokofiev', 'Prokofiev'),
    (r'scriabin', 'Scriabin'),
    (r'shos|shostakovich', 'Shostakovich'),
    (r'bartok', 'Bartok'),
    (r'grieg', 'Grieg'),
    (r'tchaikovsky|chaikovsky|tschaikowsky', 'Tchaikovsky'),
]


def standardize_composer_last_name(raw: str) -> str:
    if not raw:
        return ""
    original = raw.strip()
    lower = original.lower()
    for pattern, canonical in _COMPOSER_VARIANTS:
        if re.search(pattern, lower):
            return canonical
    tokens = [re.sub(r'[^a-zA-ZÀ-ÿ\-]', '', t) for t in original.split()
              if re.sub(r'[^a-zA-ZÀ-ÿ\-]', '', t)]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0].capitalize()
    last = tokens[-1].capitalize()
    rest = ' '.join(t.capitalize() for t in tokens[:-1])
    return f"{last} {rest}"


_OPUS_RE = re.compile(r'\b(?:opus|op)\.?\s*(\d+)\s*(no\.?\s*\d+)?', re.IGNORECASE)


def normalize_opus_metadata(title: str, subtitle: str) -> Tuple[str, str]:
    search_space = ' '.join(filter(None, [title, subtitle]))
    m = _OPUS_RE.search(search_space)
    if not m:
        return title, subtitle
    op_num = m.group(1)
    no_raw = m.group(2) or ""
    no_part = ""
    if no_raw:
        no_clean = re.sub(r'no\.?', 'No.', no_raw, flags=re.IGNORECASE)
        no_part = f" {no_clean.strip()}"
    standardized = f"Op. {op_num}{no_part}"

    def _clean(s: str) -> str:
        if not s:
            return ""
        s = _OPUS_RE.sub('', s)
        s = re.sub(r'\s{2,}', ' ', s).strip()
        s = re.sub(r'^[\-\:\s]+', '', s)
        return s

    clean_title = _clean(title)
    clean_sub = _clean(subtitle)
    new_sub = standardized if not clean_sub else f"{standardized} - {clean_sub}"
    return clean_title, new_sub


def _text_content(elem: ET.Element) -> str:
    parts = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        if child.text:
            parts.append(child.text)
        if child.tail:
            parts.append(child.tail)
    return ''.join(parts)


def extract_title_artist(root: ET.Element, mscz_path: str) -> Tuple[str, str]:
    """Title (with normalised subtitle/opus) and composer last name."""
    title = subtitle = artist = ""
    for vbox in root.findall(".//VBox"):
        for text_elem in vbox.findall("Text"):
            style = text_elem.find("style")
            text = text_elem.find("text")
            if style is not None and text is not None:
                content = _text_content(text)
                if style.text == "title":
                    title = standardize_title(content)
                elif style.text == "subtitle":
                    subtitle = standardize_title(content)
                elif style.text == "composer":
                    artist = standardize_artist(content)

    if not title or not artist:
        fallback_title = os.path.splitext(os.path.basename(mscz_path))[0].replace('_', ' ')
        fallback_artist = os.path.basename(os.path.dirname(mscz_path))
        title = title or fallback_title
        artist = artist or fallback_artist

    artist = standardize_composer_last_name(artist)
    title, subtitle = normalize_opus_metadata(title, subtitle)
    combined = f"{title} - {subtitle}" if subtitle else title
    return combined, artist


def extract_merge_markers(root: ET.Element) -> Dict[str, Set[int]]:
    """Measures (1-based, per staff) between 'merge' and 'end merge' staff texts.

    Markers from either hand apply to both hands.
    """
    merge_measures: Dict[str, Set[int]] = {'right': set(), 'left': set()}
    all_staffs = root.findall(".//Staff")
    staff_measures = {i: (s.findall("./Measure") or s.findall(".//Measure"))
                      for i, s in enumerate(all_staffs)}

    markers = []
    for staff_idx, _staff in enumerate(all_staffs):
        hand_key = 'right' if staff_idx % 2 == 0 else 'left'
        for measure_idx, measure in enumerate(staff_measures.get(staff_idx, [])):
            measure_number = measure_idx + 1
            texts = []
            for voice in measure.findall("./voice"):
                texts.extend(voice.findall("./StaffText"))
            texts.extend(measure.findall("./StaffText"))
            for staff_text in texts:
                text_elem = staff_text.find("text")
                if text_elem is not None and text_elem.text:
                    content = text_elem.text.lower().strip()
                    if content in ("merge", "end merge"):
                        markers.append((content == "merge", measure_number, hand_key, staff_idx))

    for hand_key in ('right', 'left'):
        hand_markers = sorted((m for m in markers if m[2] == hand_key), key=lambda x: x[1])
        active = None
        for is_start, measure_number, _, _ in hand_markers:
            if is_start:
                if active is None:
                    active = measure_number
                else:
                    print(f"Warning: Found 'merge' while already in merge region at measure "
                          f"{measure_number} for {hand_key} hand")
            elif active is not None:
                merge_measures[hand_key].update(range(active, measure_number))
                active = None
            else:
                print(f"Warning: Found 'end merge' without matching 'merge' at measure "
                      f"{measure_number} for {hand_key} hand")
        if active is not None:
            max_measure = max((len(ms) for idx, ms in staff_measures.items()
                               if idx % 2 == (0 if hand_key == 'right' else 1)), default=0)
            if max_measure > 0:
                merge_measures[hand_key].update(range(active, max_measure + 1))

    union = merge_measures['right'] | merge_measures['left']
    return {'right': set(union), 'left': set(union)}


def format_output_filename(title: str, artist: str, file_path: str) -> str:
    """``<first 4 letters of composer last name>_<title>.json``."""
    if not artist or artist.isspace():
        artist = os.path.basename(os.path.dirname(file_path))
    tokens = [t for t in artist.strip().split() if t]
    last_name = tokens[0] if tokens else artist.strip()
    auth = re.sub(r'[^a-zA-Z]', '', last_name)[:4].lower()
    if not title or title.isspace():
        title = os.path.splitext(os.path.basename(file_path))[0]
    formatted_title = re.sub(r'[^a-zA-Z0-9\s]', '', title).strip().replace(' ', '_')
    return f"{auth}_{formatted_title}.json"


_DURATION_MAP = {"whole": 4, "half": 2, "quarter": 1}


def _legacy_duration_ticks(duration_type: str, dots: list, resolution: int) -> int:
    duration_map = {
        "whole": resolution * 4, "half": resolution * 2, "quarter": resolution,
        "eighth": resolution // 2, "16th": resolution // 4, "32nd": resolution // 8,
        "64th": resolution // 16,
    }
    base = duration_map.get(duration_type, resolution)
    if dots:
        factor = sum(0.5 ** (i + 1) for i in range(len(dots)))
        base = int(base * (1 + factor))
    return base


def extract_accented_notes(root: ET.Element) -> Dict[Tuple[int, int, int, Tuple[int, ...]], List[int]]:
    """Map (staff_id, measure_idx, voice_idx, sorted pitches) -> accented note indices.

    Only the pitch pattern is used for matching (the position bookkeeping of
    the original implementation never influenced the key).
    """
    score = root.find("Score")
    if score is None:
        return {}
    accented: Dict[Tuple[int, int, int, Tuple[int, ...]], List[int]] = {}
    for staff in score.findall(".//Staff"):
        staff_id = int(staff.get('id', '1'))
        for measure_idx, measure in enumerate(staff.findall("Measure")):
            for voice_idx, voice in enumerate(measure.findall("voice")):
                for elem in voice:
                    if elem.tag != "Chord":
                        continue
                    has_accent = False
                    for art in elem.findall(".//Articulation"):
                        sub = art.find("subtype")
                        if sub is not None and sub.text and "accent" in sub.text.lower():
                            has_accent = True
                            break
                    pitches, indices = [], []
                    counter = 0
                    for note_elem in elem.findall("Note"):
                        pitch_elem = note_elem.find("pitch")
                        if pitch_elem is None:
                            continue
                        try:
                            pitches.append(int(pitch_elem.text))
                        except (ValueError, TypeError):
                            continue
                        if has_accent:
                            indices.append(counter)
                        counter += 1
                    if has_accent and pitches:
                        pitches.sort()
                        accented[(staff_id, measure_idx, voice_idx, tuple(pitches))] = indices
    return accented
