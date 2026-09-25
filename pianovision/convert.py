"""One score in, one PianoVision JSON document out.

    mscz --(mscx.read_score)--> Score --(render.render_score)--> MIDI events
         --(pvjson.build_song)--> PianoVision JSON

The MIDI stage is an in-memory model of what MuseScore 4.6 exports; nothing is
written to disk and MuseScore itself is never run.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Optional

from .metadata import extract_title_artist, format_output_filename
from .mscx import load_mscx_bytes, read_score
from .pvjson import build_song
from .render import Compat, render_score
from .smf import MidiFile, read_midi


@dataclass
class Converted:
    title: str
    artist: str
    name: str              # default output file name (<auth>_<title>.json)
    data: bytes            # the JSON document, exactly as written to disk
    midi: Optional[MidiFile] = None


def song_bytes(song: dict) -> bytes:
    """Serialise like the legacy converters (``json.dump`` defaults, ASCII-only)."""
    return json.dumps(song).encode("ascii")


def score_root(mscz_path: str) -> ET.Element:
    """Parsed main .mscx of a score (the one META-INF/container.xml names)."""
    return ET.fromstring(load_mscx_bytes(mscz_path))


def convert_mscz(mscz_path: str, compat: Optional[Compat] = None, orchestra: bool = True,
                 simplified: bool = True, keep_midi: bool = False) -> Converted:
    """Render ``mscz_path`` directly to PianoVision JSON."""
    root = score_root(mscz_path)
    midi = render_score(read_score(mscz_path), compat or Compat())
    return _finish(midi, root, mscz_path, orchestra, simplified, keep_midi)


def convert_midi(midi_path: str, mscz_path: Optional[str] = None, orchestra: bool = True,
                 simplified: bool = True) -> Converted:
    """Legacy path: JSON from a MuseScore-exported .mid (metadata from the .mscz when given)."""
    root = score_root(mscz_path) if mscz_path else None
    return _finish(read_midi(midi_path), root, mscz_path or midi_path, orchestra, simplified, False)


def _finish(midi: MidiFile, root: Optional[ET.Element], path: str, orchestra: bool, simplified: bool,
            keep_midi: bool) -> Converted:
    song = build_song(midi, root, path, orchestra_mode=orchestra, simplified_mode=simplified)
    title, artist = song["name"], song["artist"]
    if root is not None:
        title, artist = extract_title_artist(root, path)
    return Converted(title=title, artist=artist, name=format_output_filename(title, artist, path),
                     data=song_bytes(song), midi=midi if keep_midi else None)


def output_name(mscz_path: str) -> str:
    """The default output file name without rendering the score."""
    title, artist = extract_title_artist(score_root(mscz_path), mscz_path)
    return format_output_filename(title, artist, mscz_path)


# Members of an .mscz that can change what gets rendered: the score itself, its
# style and its chord list.  Thumbnails, parts (Excerpts/), audio and view
# settings are ignored, so re-saving a score without changing it does not force
# a re-render.
_CONTENT_SUFFIXES = (".mscx", ".mss", ".xml")


def content_hash(mscz_path: str) -> str:
    """SHA-256 over the score-defining members of the .mscz archive."""
    h = hashlib.sha256()
    with zipfile.ZipFile(mscz_path) as z:
        for name in sorted(z.namelist()):
            if "/" in name or not name.lower().endswith(_CONTENT_SUFFIXES):
                continue
            h.update(name.encode("utf-8") + b"\0")
            h.update(z.read(name))
            h.update(b"\0")
    return h.hexdigest()


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
