"""One score in, one PianoVision JSON document out.

    mscz --(mscx.read_score)--> Score --(render.render_score)--> MIDI events
         --(pvjson.build_song)--> PianoVision JSON

The MIDI stage is an in-memory model of what MuseScore exports (4.6, or 4.7 for
scores saved by 4.7: see render.Compat.engine); nothing is
written to disk and MuseScore itself is never run.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .metadata import format_output_filename, title_artist
from .mscx import load_mscx_bytes, read_score
from .pvjson import build_song, staves_revised
from .render import Compat, Renderer
from .smf import MidiFile, read_midi


@dataclass
class NoteSource:
    """Where one ``tracksV2`` note of the output comes from (``convert_mscz(provenance=True)``).

    ``staff`` is the content staff (0-based), ``measure`` the Nth ``<Measure>`` of that staff,
    ``voice`` the Nth ``<voice>`` of the measure, ``chord`` the Nth child element of the voice
    (the grace chord's own element for a grace note, ``grace`` then being its index before or
    after the main chord) and ``element`` the Nth child element of the ``<Chord>`` (the ``<Note>``).
    Repeats and voltas emit one source note several times.  ``note`` is None for chord-symbol
    playback, which has no note in the score."""
    hand: str                      # "right" | "left": the tracksV2 key
    index: int                     # position in tracksV2[hand], measures and their notes in order
    ticks: int                     # ticksStart
    midi: int
    velocity: int                  # note_on velocity (before PianoVision's accent boost)
    note: Any = field(default=None, repr=False, compare=False)   # pianovision.score.Note
    staff: int = -1
    measure: int = -1
    voice: int = -1
    chord: int = -1
    element: int = -1
    grace: Optional[int] = None
    tick: Optional[Fraction] = None   # score position (whole notes) of the chord
    track: int = -1                # the song's MIDI track (a staff; ``orchestra_from``: another score's after its own)
    arranged: bool = False         # a piano solo's note standing in for the score's piano part (piano_from): no note

    def to_dict(self) -> dict:
        d = {"hand": self.hand, "index": self.index, "ticksStart": self.ticks, "midi": self.midi,
             "velocity": self.velocity}
        if self.arranged:
            d["arranged"] = True
        if self.note is not None:
            d.update(staff=self.staff, measure=self.measure, voice=self.voice, chord=self.chord,
                     element=self.element, tick=str(self.tick))
            if self.grace is not None:
                d["grace"] = self.grace
        return d


def note_source(hand: str, index: int, n) -> NoteSource:
    """NoteSource of a pvjson Note built from a provenance render."""
    src = n.src
    ns = NoteSource(hand=hand, index=index, ticks=n.ticks, midi=n.midi, velocity=n.raw_velocity, note=src,
                    track=getattr(n, "track", -1), arranged=getattr(n, "arranged", None) is not None)
    if src is not None and src.chord is not None and src.chord.xml_path is not None:
        ns.staff, ns.measure, ns.voice, ns.chord = src.chord.xml_path
        ns.element = src.xml_index
        ns.grace = src.chord.grace_index if src.chord.is_grace else None
        ns.tick = src.chord.tick
    return ns


@dataclass
class Converted:
    title: str
    artist: str
    name: str              # default output file name (<auth>_<title>.json)
    data: bytes            # the JSON document, exactly as written to disk
    midi: Optional[MidiFile] = None
    # with provenance=True: the score model that was rendered and the source of every tracksV2 note
    score: Any = None
    provenance: Optional[Dict[str, List[NoteSource]]] = None
    # with parts=True: Note Waterfall's parts (pianovision.parts; the document without song/songMd5/scoreContent)
    parts: Optional[dict] = None
    # with parts=True and provenance=True: {part: {hand: [NoteSource per note of the parts document]}}, plus
    # "simplified_staff": the simplified part's notes written on the simplified staves (the hand-edit identity)
    parts_provenance: Optional[Dict[str, Dict[str, List[NoteSource]]]] = None
    # with parts=True and a partner score in the options (simplified_from, orchestra_from): what the alignment did
    # (pianovision.arrangement.report_dict, with the clock's plan: pianovision.timeline)
    arrangement: Optional[dict] = None
    # the score's staves have other roles than the legacy classification gave them (pvjson.staves_revised)
    staves_revised: bool = False


def song_bytes(song: dict) -> bytes:
    """Serialise like the legacy converters (``json.dump`` defaults, ASCII-only)."""
    return json.dumps(song).encode("ascii")


def score_root(mscz_path: str) -> ET.Element:
    """Parsed main .mscx of a score (the one META-INF/container.xml names)."""
    return ET.fromstring(load_mscx_bytes(mscz_path))


def convert_mscz(mscz_path: str, compat: Optional[Compat] = None, orchestra: bool = True,
                 simplified: bool = True, keep_midi: bool = False, provenance: bool = False,
                 parts: bool = False, options: Optional[dict] = None) -> Converted:
    """Render ``mscz_path`` directly to PianoVision JSON.

    ``provenance`` also returns the score model and, for every tracksV2 note, the
    score note it comes from (:class:`NoteSource`); ``parts`` also returns Note Waterfall's
    parts of the song (:mod:`pianovision.parts`).  The JSON is the same either way.
    ``options``: the score's build options (:meth:`pianovision.buildopts.BuildOptions.render_options`): its title
    and composer, its piano staves and hands (:mod:`pianovision.handsplit`), and a partner score
    (:mod:`pianovision.timeline`): with ``simplified_from`` the song is this orchestral score on the piano solo's
    clock and, with ``parts``, the solo's piano part its simplified part (and, with the partner's ``piano``,
    ``piano_from``, the song's piano part: :class:`pianovision.arrangement.PianoFrom`); with ``orchestra_from`` the
    song is this solo with the orchestral score's orchestra (the report in ``Converted.arrangement``)."""
    from .handsplit import apply_piano_options
    compat = compat or Compat()
    partner = (options or {}).get("partner")
    own = {k: v for k, v in (options or {}).items() if k != "partner"} or None
    root = score_root(mscz_path)
    pr = None
    if partner and partner["kind"] == "simplified":
        # the song is this orchestral score, rendered on the solo's clock
        from .timeline import SONG_ORCHESTRA, pair
        pr = pair(mscz_path, compat, own, partner["path"], partner.get("compat"), partner.get("options"),
                  SONG_ORCHESTRA, partner.get("measure_map", "auto"), provenance=provenance or parts)
        score, renderer, midi = pr.orch_render.score, pr.orch_render.renderer, pr.orch_render.midi
    else:
        score = read_score(mscz_path)
        # the parts' lyrics find the notes that sing them through the notes' provenance (the JSON is the same)
        renderer = Renderer(score, compat, provenance=provenance or parts)
        midi = renderer.render()
        apply_piano_options(midi, own)
    orch_stats = None
    if partner and partner["kind"] == "orchestra":
        # the song is this solo; the orchestral score's orchestra, on the solo's clock, onto its measures
        from .timeline import SONG_SOLO, Rendered, inject_orchestra, pair
        pr = pair(partner["path"], partner.get("compat"), partner.get("options"), mscz_path, compat, own, SONG_SOLO,
                  partner.get("measure_map", "auto"), solo_render=Rendered(score, renderer, midi))
        orch_stats = inject_orchestra(midi, pr, partner.get("rel", ""))
    piano = None
    if partner and partner["kind"] == "simplified" and partner.get("piano"):
        # piano_from: the solo is the song's piano part where the alignment maps its measures
        from .arrangement import PianoFrom
        piano = PianoFrom(pr, score)
    prov: Optional[Dict[str, list]] = {} if provenance else None
    parts_out: Optional[dict] = {} if parts else None
    c = _finish(midi, root, mscz_path, orchestra, simplified, keep_midi, compat.metadata, prov,
                parts_out, own, piano)
    c.staves_revised = staves_revised(midi)
    if provenance:
        c.score = score
        c.provenance = {hand: [note_source(hand, i, n) for i, n in enumerate(prov[hand])]
                        for hand in ("right", "left")}
    if parts:
        if partner and partner["kind"] == "simplified":
            from .arrangement import apply_arrangement
            c.arrangement = apply_arrangement(partner, pr, score, parts_out, piano)
        elif partner:
            from .arrangement import orchestra_report
            c.arrangement = orchestra_report(partner, pr, orch_stats, parts_out)
        c.parts = parts_out["doc"]
        if provenance:
            c.parts_provenance = _parts_provenance(parts_out)
    return c


def _parts_provenance(parts_out: dict) -> Dict[str, Dict[str, List[NoteSource]]]:
    hands = ("right", "left")
    out = {name: {hand: [note_source(hand, i, n) for i, n in enumerate(notes[h])] for h, hand in enumerate(hands)}
           for name, notes in parts_out["notes"].items()}
    staff = parts_out["simplified_staff"]
    simp = parts_out["notes"].get("simplified")
    out["simplified_staff"] = {hand: [out["simplified"][hand][i] for i, n in enumerate(simp[h]) if id(n) in staff]
                               if simp is not None else [] for h, hand in enumerate(hands)}
    return out


def convert_midi(midi_path: str, mscz_path: Optional[str] = None, orchestra: bool = True,
                 simplified: bool = True) -> Converted:
    """Legacy path: JSON from a MuseScore-exported .mid (metadata from the .mscz when given),
    exactly as the old midi_to_json.py made it."""
    root = score_root(mscz_path) if mscz_path else None
    return _finish(read_midi(midi_path), root, mscz_path or midi_path, orchestra, simplified, False, "legacy")


def _finish(midi: MidiFile, root: Optional[ET.Element], path: str, orchestra: bool, simplified: bool,
            keep_midi: bool, rules: str, provenance: Optional[dict] = None,
            parts: Optional[dict] = None, options: Optional[dict] = None, piano=None) -> Converted:
    song = build_song(midi, root, path, orchestra_mode=orchestra, simplified_mode=simplified,
                      legacy_metadata=rules, provenance=provenance, parts=parts, piano=piano)
    title, artist = song["name"], song["artist"]
    if root is not None:
        title, artist = title_artist(root, path, rules)
    title, artist = _named(title, artist, options)
    if options and (options.get("title") or options.get("composer")):
        song["name"], song["artist"] = title, artist
    return Converted(title=title, artist=artist, name=format_output_filename(title, artist, path),
                     data=song_bytes(song), midi=midi if keep_midi else None)


def _named(title: str, artist: str, options: Optional[dict]) -> tuple:
    """The title and composer, as the build options give them (``title``, ``composer``) or as read."""
    if not options:
        return title, artist
    return options.get("title") or title, options.get("composer") or artist


def output_name(mscz_path: str, legacy: bool = False, rules: Optional[str] = None,
                options: Optional[dict] = None) -> str:
    """The default output file name without rendering the score (``rules``: the title rules as
    ``Compat.metadata``; ``legacy`` is short for "legacy"; ``options``: the build options' title and composer)."""
    title, artist = title_artist(score_root(mscz_path), mscz_path, rules or ("legacy" if legacy else "v2"))
    title, artist = _named(title, artist, options)
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
