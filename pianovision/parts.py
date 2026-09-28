"""Note Waterfall's parts file: the piano part, its simplified version and the orchestra, kept apart.

PianoVision's JSON merges everything into one stream per hand (the simplified staves replace the piano
part where they have notes; the orchestra fills marked and empty measures).  Note Waterfall shows the
original or the simplified piano part and draws the orchestra separately, so it gets the three streams in
a file of its own, ``<output>/NoteWaterfall/<song stem>.parts.json``.  PianoVision's JSON is not changed
by any of this: it is built from the same notes, exactly as before.

Format 1 (UTF-8 JSON, ``json.dumps`` compact separators, keys in this order)::

    {"format": 1,
     "song": "chop_Nocturne.json",            # the PianoVision JSON these parts go with
     "songMd5": "<md5 hex of that file>",     # the app ignores the parts of another version of the song
     "scoreContent": "<sha256>",              # content hash of the .mscz (the manifest's), rebuilt with it
     "resolution": 480,
     "simplified": true,                      # the score has simplified piano staves with notes
     "orchestra": "piano-orchestral",         # "piano-orchestral" | "instruments" | "none"
     "parts": {
       "original":   {"right": [NOTE...], "left": [NOTE...]},   # the piano staves ('piano')
       "simplified": {"right": [...], "left": [...]},           # only when "simplified": the simplified part
       "orchestra":  {"right": [...], "left": [...]}            # the orchestra arranged for piano
     }}

A NOTE carries the fields of a ``tracksV2`` note that Note Waterfall reads, with the same values PianoVision's
JSON has for it: ``note`` (MIDI), ``ticksStart``, ``durationTicks``, ``start`` and ``duration`` (s),
``velocity`` (0..1, after the accent boost), ``measureInd`` (the legacy ticks/1920) and ``accent`` (0/1).
Streams are in time order, as tracksV2 is.  Extra fields:

* simplified part: ``"simplified": 1`` on the notes written on the simplified staves (the others are the
  original part's notes of measures the simplified staves leave empty);
* orchestra: ``"staff"``: the score's staff (0-based) the note is written on; ``"program"``: the General MIDI
  program of an orchestra instrument (absent for the 'piano-orchestral' staves); ``"merged": 1`` on the notes
  PianoVision's JSON merges into the hands ('merge' markers, measures where the piano rests).

The simplified part is what PianoVision shows in simplified mode without the orchestra.  The orchestra is the
'piano-orchestral' staves (first staff right hand, second left) when the score has any; otherwise the orchestra
instruments PianoVision's orchestra mode takes (program 0-7, 40-51, 64-95 except 112-119), in the hands by pitch
(middle C and up: right).  Accents are matched per part exactly as PianoVision's JSON matches them.

Hand edits (the sidecar's note identity): a note of the ORIGINAL part is (hand, ticksStart, note, occurrence)
counted among the original part's notes; a note written on the simplified staves is counted among the
simplified part's notes that carry ``"simplified": 1``.  Notes with the same hand, ticksStart and pitch always
come from the same staves (same tick, same measure), so the original part's notes have the same identity in
the simplified part, and in PianoVision's tracksV2 (merged orchestra notes come after them).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from .pvjson import Note, NoteStreams, _apply_accents, _measure_lookup, simplify_hands

PARTS_FORMAT = 1
PARTS_DIR = "NoteWaterfall"
PARTS_EXT = ".parts.json"
HANDS = ("right", "left")
PART_NAMES = ("original", "simplified", "orchestra")


def parts_name(song: str) -> str:
    """``chop_Nocturne.json`` -> ``chop_Nocturne.parts.json``."""
    base = os.path.basename(song)
    return (base[:-5] if base.lower().endswith(".json") else base) + PARTS_EXT


def parts_rel(song: str) -> str:
    """Path of a song's parts file below the output folder: ``NoteWaterfall/<stem>.parts.json``."""
    return PARTS_DIR + "/" + parts_name(song)


def _copy(hands: List[List[Note]]) -> List[List[Note]]:
    return [[dataclasses.replace(n) for n in hand] for hand in hands]


def build_parts(streams: NoteStreams, accented_notes, pv_notes: List[List[Note]]) -> Dict[str, Any]:
    """The parts of a render.  ``streams`` must be taken before PianoVision's JSON is built from them (accents
    change the notes in place); ``pv_notes``: the notes of PianoVision's JSON (right, left), to tell which
    orchestra notes it merges into the hands.

    Returns ``{"doc": the parts document without song/songMd5/scoreContent, "notes": {part: [right, left]}}``,
    the notes being the :class:`Note` objects of the document (``src`` set when the render has provenance)."""
    fast = _measure_lookup(streams.measure_map, streams.ticks_per_beat)
    has_simplified = any(streams.simplified)
    original = _copy(streams.primary)
    simplified_staff = _copy(streams.simplified)
    staff_ids = {id(n) for hand in simplified_staff for n in hand}
    # own copies: accents are applied per part, in place
    simplified = simplify_hands(_copy(streams.primary), simplified_staff, fast) if has_simplified else None
    if any(streams.orchestral):
        source, orch_src = "piano-orchestral", streams.orchestral
    elif any(streams.other_orchestra):
        source, orch_src = "instruments", streams.other_orchestra
    else:
        source, orch_src = "none", [[], []]
    merged_ids = {id(n) for hand in pv_notes for n in hand}
    merged = set()
    orchestra: List[List[Note]] = [[], []]
    for h, hand in enumerate(orch_src):
        for n in hand:
            c = dataclasses.replace(n)
            if id(n) in merged_ids:
                merged.add(id(c))
            orchestra[h].append(c)
    programs = dict(streams.orchestra_tracks)

    parts: Dict[str, List[List[Note]]] = {"original": original}
    if simplified is not None:
        parts["simplified"] = simplified
    parts["orchestra"] = orchestra
    tpb = streams.ticks_per_beat
    doc_parts: Dict[str, Any] = {}
    for name, hands in parts.items():
        if accented_notes:
            _apply_accents(hands, accented_notes, streams.get_measure_for_tick)
        hands[:] = [sorted(hand, key=lambda x: x.time) for hand in hands]
        out = {}
        for h, key in enumerate(HANDS):
            notes = []
            for n in hands[h]:
                d = {"note": n.midi, "ticksStart": n.ticks, "durationTicks": n.duration_ticks, "start": n.time,
                     "duration": n.duration, "velocity": n.velocity, "measureInd": int((n.ticks / tpb) / 4),
                     "accent": int(n.accent)}
                if name == "simplified" and id(n) in staff_ids:
                    d["simplified"] = 1
                elif name == "orchestra":
                    d["staff"] = n.track
                    if source == "instruments" and n.track in programs:
                        d["program"] = programs[n.track]
                    if id(n) in merged:
                        d["merged"] = 1
                notes.append(d)
            out[key] = notes
        doc_parts[name] = out
    doc = {"format": PARTS_FORMAT, "resolution": tpb, "simplified": has_simplified, "orchestra": source,
           "parts": doc_parts}
    return {"doc": doc, "notes": parts, "simplified_staff": staff_ids}


def parts_bytes(doc: Dict[str, Any], song: str, song_data: bytes, score_content: str) -> bytes:
    """The parts file for the song file ``song`` whose bytes are ``song_data``."""
    full = {"format": doc["format"], "song": os.path.basename(song),
            "songMd5": hashlib.md5(song_data).hexdigest(), "scoreContent": score_content or ""}
    full.update((k, v) for k, v in doc.items() if k != "format")
    return (json.dumps(full, separators=(",", ":")) + "\n").encode("ascii")


def parts_stats(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Counts for the manifest: notes per part, whether the score has a simplified part, the orchestra's source."""
    counts = {name: sum(len(v) for v in hands.values()) for name, hands in doc.get("parts", {}).items()}
    return {"simplified": bool(doc.get("simplified")), "orchestra": doc.get("orchestra", "none"), "notes": counts}


def read_parts(path: str) -> Optional[dict]:
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None
