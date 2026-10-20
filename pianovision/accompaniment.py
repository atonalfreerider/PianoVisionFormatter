"""Note Waterfall's companion audio: a score's accompaniment (everything but the piano part) rendered by
MuseScore Studio with Muse Sounds, on the score's own timeline (``python -m pianovision accompaniment``).

The scores are the ones whose Note Waterfall parts file has orchestra notes (the manifest's parts statistics,
so run ``build`` first).  For each one:

1. **Analyse.**  The score is rendered as the library renders it (the manifest entry's knobs) and the formatter
   says which staves are the piano part (:func:`pianovision.pvjson.classify_tracks`): the 'piano' and
   'piano-simplified' staves, a staff whose program is 0-7, the first two staves when nothing is called piano.
   Those instruments are muted; so are the 'piano-orchestral' staves (the orchestra reduced for the falling notes)
   when the score also has the orchestra, and vocal staves (``mute_vocals``).  MuseScore mutes whole instruments,
   so an instrument with a piano-part staff is muted with all its staves (reported as a conflict when another of
   its staves has notes).
2. **Copy.**  The ``.mscz`` is copied to the work folder.  Its ``audiosettings.json`` gets solo off on every
   track and mute on the muted instruments, plus a -120 dB volume (MuseScore 4.7.5's command-line export plays
   muted Muse Sounds tracks); instruments listed in ``accompaniment.json`` are relabelled (the Instrument's id
   and instrumentId, its instrument changes, the matching audiosettings track) so Muse Sounds picks the sound the
   music needs.  Notes are never changed.  The real score is only read.
3. **Render.**  ``mscore -o render.wav --sound-profile MuseSounds copy.mscz``, headless, with MuseScore's
   config, cache and data folders in the work folder (the Muse Sounds engine is linked in).  A render without
   the engine, or with NaN samples (a Muse Sounds instrument's bug), fails.
4. **Probe.**  Which instruments Muse Sounds plays: a probe score with the copy's instruments, one note each in
   turn, is rendered with and without the Muse Sounds engine.  An instrument that sounds the same in both fell
   back to MuseScore's basic sounds (no library installed for it); one that is silent with Muse Sounds plays
   outside the library instrument's range.  Relabelled instruments are probed before and after.
5. **Check.**  Duration against the score (MuseScore's trailing silence is cut), silences where the score has
   notes, sound where only the muted piano plays (bleed), peak level; the timing against the formatter's note-on
   times (``tools/accompaniment_timing.py``, run with a Python that has numpy, soundfile and librosa) when
   configured, and the same for the score's piano alone rendered with the basic sounds (MuseScore's timeline
   apart from the Muse Sounds attacks).
6. **Encode.**  A linear gain to ``lufs`` integrated loudness (ffmpeg loudnorm measurement), capped so the true
   peak stays at or below ``true_peak``; no compression.  LAME mp3, constant bit rate, with the codec delay cut
   from the start and no Xing/LAME header: decoded without gapless handling, it lines up at sample 0.

A song with a partner score (``scores.json`` ``simplified_from``, ``orchestra_from``: :mod:`pianovision.timeline`)
plays on the piano solo's clock, and so does its audio: the score rendered is the orchestral score edited the way the
formatter renders the song (its own tempo marks, fermatas and pauses off, the solo's tempo written in), and with
``orchestra_from`` (the song is the solo, the orchestral partner is rendered, with its options in this file) the
render is cut into the runs of measures both scores share, each placed at the solo's time (:func:`place_wav`).  The
report checks the song's orchestra (its parts file) against the rendered score's notes on that clock.

The output is ``<out>/<song>.mp3`` (``<song>`` = the PianoVision JSON's name) and the report
``accompaniment-report.md`` / ``.json``.  The JSON is also the cache: a score is rendered again only when its
file, its options in ``accompaniment.json``, the muted instruments, MuseScore, the Muse Sounds engine or the
encoding settings changed, or when the Muse Sounds libraries changed in a way that can change its sound: a
library it used was removed or updated, or a library was installed while some of its instruments fell back to
the basic sounds.
"""

from __future__ import annotations

import bisect
import copy
import dataclasses
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import statistics
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

PIPELINE_VERSION = 3            # bump when rendering, probing or encoding changes what a file sounds like
REPORT_NAME = "accompaniment-report"
OPTIONS_NAME = "accompaniment.json"
SOUND_PROFILE = "MuseSounds"
MP3_SAMPLE_RATE = 44100
MP3_CODEC_DELAY = 576 + 529     # LAME's encoder delay + the MPEG layer III decoder's delay, in samples
PROBE_BPM = 120
PROBE_MEASURE_S = 4 * 60.0 / PROBE_BPM      # 4/4 at 120 bpm
PROBE_SLOT_MEASURES = 3                     # one whole note, two measures' rest for the tail

KIND_PIANO, KIND_SIMPLIFIED, KIND_ORCHESTRAL = "piano", "piano-simplified", "piano-orchestral"
KIND_ORCHESTRA, KIND_VOCAL, KIND_OTHER = "orchestra", "vocal", "other"

Log = Callable[[str], None]


class AccompanimentError(RuntimeError):
    pass


# ------------------------------------------------------------------------------
# settings ([accompaniment] in pianovision.toml) and per-score options (accompaniment.json)
# ------------------------------------------------------------------------------

@dataclass
class Settings:
    out_dir: str = "~/Music/Note Waterfall Accompaniments"
    work_dir: str = "~/.cache/nw-accomp"
    mscore: str = "~/.local/bin/mscore"
    musesampler: str = "~/.local/share/MuseSampler"     # the Muse Sounds engine (lib/ and .config)
    options: str = OPTIONS_NAME                          # per-score options, relative to pianovision.toml
    bitrate: int = 256                                   # kbit/s, constant
    lufs: float = -16.0                                  # integrated loudness target
    true_peak: float = -1.0                              # dBTP ceiling
    timing_python: str = ""                              # a Python with numpy/soundfile/librosa ("" = no check)
    timing_script: str = "tools/accompaniment_timing.py"
    timeout: int = 5400                                  # seconds per MuseScore run
    keep_wav: bool = False

    def path(self, name: str) -> str:
        return os.path.abspath(os.path.expanduser(getattr(self, name)))


def load_settings(config_path: Optional[str], **overrides) -> Settings:
    """``[accompaniment]`` of pianovision.toml; relative paths are relative to that file."""
    import tomllib
    raw: dict = {}
    base = os.getcwd()
    if config_path and os.path.isfile(config_path):
        with open(config_path, "rb") as f:
            raw = tomllib.load(f).get("accompaniment", {})
        base = os.path.dirname(os.path.abspath(config_path))
    s = Settings()
    for f in dataclasses.fields(Settings):
        if f.name in raw:
            setattr(s, f.name, type(getattr(s, f.name))(raw[f.name]))
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, v)
    for name in ("options", "timing_script"):
        v = os.path.expanduser(getattr(s, name))
        if v and not os.path.isabs(v):
            setattr(s, name, os.path.normpath(os.path.join(base, v)))
    return s


@dataclass
class Relabel:
    """Play an instrument with another instrument's sound: ``part`` (the Part's id in the score) currently has
    the Instrument ``old`` (its id attribute, e.g. "lupophone"); it becomes ``new`` ("bassoon") with the
    instrumentId ``musicxml`` ("wind.reed.bassoon")."""
    part: str
    old: str
    new: str
    musicxml: str
    why: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Relabel":
        return cls(part=str(d["part"]), old=d["from"], new=d["to"], musicxml=d["instrumentId"], why=d.get("why", ""))

    def to_dict(self) -> dict:
        return {"part": self.part, "from": self.old, "to": self.new, "instrumentId": self.musicxml, "why": self.why}


@dataclass
class Technique:
    """Start a part's staves with a playing technique (a ``PlayTechAnnotation`` before their first note or rest), e.g.
    pizzicato for basses a MIDI import wrote for a harp: ``technique`` is MuseScore's playTechType, ``text`` what it
    shows."""
    part: str
    technique: str
    text: str = ""
    why: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Technique":
        return cls(part=str(d["part"]), technique=str(d["technique"]), text=str(d.get("text", "")),
                   why=d.get("why", ""))

    def to_dict(self) -> dict:
        return {"part": self.part, "technique": self.technique, "text": self.text, "why": self.why}


@dataclass
class ScoreOptions:
    mute_vocals: bool = True                 # vocal staves (instrumentId voice.*, or the formatter's vocal lines:
                                             # pvjson.staff_roles) are not part of the companion
    piano_orchestral: str = "auto"           # "auto": muted when the score has other instruments; "mute"; "play"
    mute_parts: List[str] = field(default_factory=list)     # more instruments to mute (Part ids)
    play_parts: List[str] = field(default_factory=list)     # instruments to play although they would be muted
    relabel: List[Relabel] = field(default_factory=list)
    reset_reverb: bool = True                # drop the saved reverb sends: Muse Sounds' own per-instrument levels
    techniques: List[Technique] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["relabel"] = [r.to_dict() for r in self.relabel]
        d["techniques"] = [t.to_dict() for t in self.techniques]
        if not self.techniques:
            d.pop("techniques")              # (the cache keys of scores without techniques stay as they were)
        return d


def load_options(path: str) -> Tuple[dict, Dict[str, dict]]:
    """accompaniment.json -> (defaults, {score path relative to the scores folder: options})."""
    if not path or not os.path.isfile(path):
        return {}, {}
    with open(path, "rb") as f:
        doc = json.load(f)
    return doc.get("defaults", {}), doc.get("scores", {})


def score_options(defaults: dict, per_score: Optional[dict]) -> ScoreOptions:
    d = dict(defaults)
    d.update({k: v for k, v in (per_score or {}).items() if not k.startswith("_")})
    known = {f.name for f in dataclasses.fields(ScoreOptions)}
    unknown = sorted(k for k in d if k not in known and not k.startswith("_") and k not in ("why", "notes"))
    if unknown:
        raise AccompanimentError(f"unknown option(s) in {OPTIONS_NAME}: {', '.join(unknown)}")
    o = ScoreOptions(mute_vocals=bool(d.get("mute_vocals", True)),
                     piano_orchestral=str(d.get("piano_orchestral", "auto")),
                     mute_parts=[str(p) for p in d.get("mute_parts", [])],
                     play_parts=[str(p) for p in d.get("play_parts", [])],
                     relabel=[Relabel.from_dict(r) for r in d.get("relabel", [])],
                     reset_reverb=bool(d.get("reset_reverb", True)),
                     techniques=[Technique.from_dict(t) for t in d.get("techniques", [])])
    if o.piano_orchestral not in ("auto", "mute", "play"):
        raise AccompanimentError(f"piano_orchestral must be auto, mute or play, not {o.piano_orchestral!r}")
    return o


# ------------------------------------------------------------------------------
# analysis: the formatter's view of the score
# ------------------------------------------------------------------------------

@dataclass
class StaffInfo:
    idx: int
    part: str                     # the Part's id attribute
    kind: str                     # KIND_*: the formatter's classification of the staff's MIDI track
    notes: int = 0
    pitches: List[int] = field(default_factory=list)
    percussion: bool = False
    spans: List[Tuple[float, float]] = field(default_factory=list)     # sounding notes (on, off), seconds
    attacks: List[Tuple[float, int]] = field(default_factory=list)     # (note-on time, pitch)


@dataclass
class PartInfo:
    id: str
    name: str
    instruments: List[Tuple[str, str]] = field(default_factory=list)   # (id attribute, instrumentId), distinct
    staves: List[int] = field(default_factory=list)


@dataclass
class Analysis:
    staves: List[StaffInfo]
    parts: Dict[str, PartInfo]            # in score order
    end_time: float                       # the last MIDI event (note-offs included), seconds
    title: str = ""
    artist: str = ""

    def part_notes(self, pid: str) -> int:
        return sum(self.staves[i].notes for i in self.parts[pid].staves)


def analyse(path: str, compat: Optional[dict] = None, options: Optional[dict] = None) -> Analysis:
    """Render ``path`` as the library does and classify its staves the way the formatter does (``options``: the
    score's build options, :mod:`pianovision.buildopts`: its piano staves, title and composer)."""
    from .convert import _named, score_root
    from .handsplit import apply_piano_options
    from .metadata import title_artist
    from .mscx import read_score
    from .pvjson import (TRACK_ORCHESTRAL, TRACK_PRIMARY, TRACK_SIMPLIFIED, TempoIndex, extract_tempo_events,
                         staff_roles)
    from .render import Compat, render_score
    c = Compat.from_dict(compat)
    score = read_score(path)
    mid = render_score(score, c)
    apply_piano_options(mid, options)
    roles = staff_roles(mid)
    kinds = {i: {TRACK_PRIMARY: KIND_PIANO, TRACK_SIMPLIFIED: KIND_SIMPLIFIED, TRACK_ORCHESTRAL: KIND_ORCHESTRAL}[t]
             for i, t in roles.piano}
    kinds.update({i: KIND_ORCHESTRA for i, _p in roles.orchestra if i not in kinds})
    kinds.update({i: KIND_VOCAL for i, _p in roles.vocal if i not in kinds})      # a vocal line, whatever its instrument
    seconds = TempoIndex(extract_tempo_events(mid), mid.ticks_per_beat)   # every track's set_tempo
    staves: List[StaffInfo] = []
    parts: Dict[str, PartInfo] = {}
    end = 0.0
    for i, st in enumerate(score.staves):
        part = st.part
        info = StaffInfo(idx=i, part=part.id, kind=kinds.get(i, KIND_OTHER), percussion=st.group == "percussion")
        if part.id not in parts:
            pi = PartInfo(id=part.id, name=part.track_name or part.instrument().long_name)
            for _tick, ins in sorted(part.instruments.items()):
                key = (ins.id, ins.instrument_id)
                if key not in pi.instruments:
                    pi.instruments.append(key)
            parts[part.id] = pi
        parts[part.id].staves.append(i)
        tick = 0
        active: Dict[Tuple[int, int], List[float]] = {}
        for msg in (mid.tracks[i] if i < len(mid.tracks) else []):
            tick += msg.time
            t = seconds(tick)
            end = max(end, t)
            if msg.type == "note_on" and msg.velocity > 0:
                info.notes += 1
                info.pitches.append(msg.note)
                info.attacks.append((t, msg.note))
                active.setdefault((msg.channel, msg.note), []).append(t)
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                ons = active.get((msg.channel, msg.note))
                if ons:
                    info.spans.append((ons.pop(0), t))
        for ons in active.values():
            info.spans.extend((t0, t0) for t0 in ons)
        info.spans.sort()
        staves.append(info)
    title, artist = _named(*title_artist(score_root(path), path, c.metadata), options)
    return Analysis(staves=staves, parts=parts, end_time=end, title=title, artist=artist)


# ------------------------------------------------------------------------------
# what to mute (pure)
# ------------------------------------------------------------------------------

@dataclass
class MutePlan:
    muted: Dict[str, List[str]]           # Part id -> reasons
    kept: Dict[str, str]                  # Part id -> why it plays although it would be muted (play_parts)
    conflicts: List[str]                  # staves with notes muted only because they share an instrument
    audible_staves: List[int]             # staves with notes that sound in the render

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def is_vocal(instruments: Sequence[Tuple[str, str]]) -> bool:
    return any(mx.startswith("voice.") for _id, mx in instruments)


def plan_mutes(a: Analysis, o: ScoreOptions) -> MutePlan:
    muted: Dict[str, List[str]] = {}

    def mute(pid: str, why: str) -> None:
        muted.setdefault(pid, [])
        if why not in muted[pid]:
            muted[pid].append(why)

    piano_kinds = {KIND_PIANO: "piano part (original)", KIND_SIMPLIFIED: "piano part (simplified)"}
    for st in a.staves:
        if st.kind in piano_kinds:
            mute(st.part, piano_kinds[st.kind])
    vocal = {pid for pid, p in a.parts.items() if is_vocal(p.instruments)} | {st.part for st in a.staves
                                                                               if st.kind == KIND_VOCAL}
    if o.mute_vocals:
        for pid in vocal:
            mute(pid, "vocal staff (mute_vocals)")
    for pid in o.mute_parts:
        if pid not in a.parts:
            raise AccompanimentError(f"mute_parts: the score has no part {pid!r}")
        mute(pid, "mute_parts in " + OPTIONS_NAME)
    orchestral = [st for st in a.staves if st.kind == KIND_ORCHESTRAL]
    if orchestral:
        others = any(st.notes and st.kind not in (KIND_PIANO, KIND_SIMPLIFIED, KIND_ORCHESTRAL)
                     and st.part not in muted for st in a.staves)
        if o.piano_orchestral == "mute" or (o.piano_orchestral == "auto" and others):
            for st in orchestral:
                mute(st.part, "piano-orchestral staves (the orchestra arranged for piano; the score has the "
                              "orchestra itself)" if others else "piano-orchestral staves (piano_orchestral: mute)")
    kept: Dict[str, str] = {}
    for pid in o.play_parts:
        if pid not in a.parts:
            raise AccompanimentError(f"play_parts: the score has no part {pid!r}")
        if pid in muted:
            kept[pid] = "; ".join(muted.pop(pid))
    conflicts = []
    for pid in muted:
        for i in a.parts[pid].staves:
            st = a.staves[i]
            if st.notes and st.kind not in (KIND_PIANO, KIND_SIMPLIFIED, KIND_ORCHESTRAL) \
                    and not (o.mute_vocals and pid in vocal) and pid not in o.mute_parts:
                conflicts.append(f"staff {i + 1} ({a.parts[pid].name}, {st.notes} notes) is muted with its "
                                 f"instrument's piano-part staff")
    audible = [st.idx for st in a.staves if st.notes and st.part not in muted]
    return MutePlan(muted=muted, kept=kept, conflicts=conflicts, audible_staves=audible)


# ------------------------------------------------------------------------------
# editing the copy (pure)
# ------------------------------------------------------------------------------

MUTED_VOLUME_DB = -120.0       # MuseScore 4.7.5's command-line export plays muted Muse Sounds tracks; a volume this
                               # low silences them (the track volume is applied when the track is created)


def edit_audiosettings(data: Optional[bytes], tracks: Sequence[Tuple[str, str]], muted_parts: Set[str],
                       renames: Optional[Dict[Tuple[str, str], str]] = None, reset_reverb: bool = False,
                       dry: bool = False) -> bytes:
    """``audiosettings.json`` of a render copy: solo off on every track (and aux), mute on exactly the tracks of
    ``muted_parts`` and their volume at :data:`MUTED_VOLUME_DB` (the command-line export ignores the mute of Muse
    Sounds tracks), track instrumentIds renamed (``{(partId, old): new}``), a track entry added for every
    ``(partId, instrumentId)`` of ``tracks`` the file lacks (a track without a saved state plays at 0 dB).
    ``reset_reverb`` drops the tracks' saved aux sends, so MuseScore applies its defaults for the sound it
    resolves (Muse Sounds instruments have their own reverb levels).  ``dry`` turns every track's aux sends off
    (the probe: the reverb is not the same without the Muse Sounds engine)."""
    doc = json.loads(data) if data else {}
    renames = renames or {}
    out_tracks = []
    seen = set()
    for t in doc.get("tracks", []):
        t = dict(t)
        pid, iid = str(t.get("partId", "")), str(t.get("instrumentId", ""))
        iid = renames.get((pid, iid), iid)
        if (pid, iid) in seen:          # relabelled onto an instrument the part already had
            continue
        t["instrumentId"] = iid
        t["soloMuteState"] = {"mute": pid in muted_parts, "solo": False}
        if reset_reverb and isinstance(t.get("out"), dict):
            t["out"] = {k: v for k, v in t["out"].items() if k != "auxSends"}
        if dry:
            t["out"] = dict(t.get("out") or {}, auxSends=_DRY_SENDS)
        if pid in muted_parts:
            t["out"] = dict(t.get("out") or {}, volumeDb=MUTED_VOLUME_DB)
        seen.add((pid, iid))
        out_tracks.append(t)
    for pid, iid in tracks:
        if (pid, iid) not in seen:
            t = {"instrumentId": iid, "partId": pid, "soloMuteState": {"mute": pid in muted_parts, "solo": False}}
            if dry:
                t["out"] = {"auxSends": _DRY_SENDS}
            if pid in muted_parts:
                t["out"] = dict(t.get("out") or {}, volumeDb=MUTED_VOLUME_DB)
            out_tracks.append(t)
            seen.add((pid, iid))
    doc["tracks"] = out_tracks
    for aux in doc.get("aux", []) or []:
        if isinstance(aux, dict) and isinstance(aux.get("soloMuteState"), dict):
            aux["soloMuteState"]["solo"] = False
    doc["activeSoundProfile"] = SOUND_PROFILE
    return (json.dumps(doc, indent=4, ensure_ascii=False) + "\n").encode("utf-8")


_DRY_SENDS = [{"active": False, "signalAmount": 0}, {"active": False, "signalAmount": 0}]

_PART_RE = re.compile(r'<Part\b[^>]*\bid="([^"]*)"[^>]*>')
_INSTR_RE = re.compile(r'<Instrument\b([^>]*)>')


def _relabel_instrument(text: str, start: int, end: int, r: Relabel, what: str) -> Tuple[str, bool]:
    """Relabel the first ``<Instrument id="r.old">`` between ``start`` and ``end``; (text, done)."""
    for im in _INSTR_RE.finditer(text, start, end):
        idm = re.search(r'\bid="([^"]*)"', im.group(1))
        if idm is None or idm.group(1) != r.old:
            continue
        iend = text.find("</Instrument>", im.end(), end)
        if iend < 0:
            raise AccompanimentError(f"relabel: {what}'s Instrument is not closed")
        attrs = im.group(1)[:idm.start()] + f'id="{r.new}"' + im.group(1)[idm.end():]
        body = text[im.end():iend]
        body, n = re.subn(r"<instrumentId>[^<]*</instrumentId>", f"<instrumentId>{r.musicxml}</instrumentId>",
                          body, count=1)
        if n == 0:
            body = f"<instrumentId>{r.musicxml}</instrumentId>" + body
        return text[:im.start()] + f"<Instrument{attrs}>" + body + text[iend:], True
    return text, False


def relabel_mscx(mscx: bytes, relabels: Sequence[Relabel],
                 part_staves: Optional[Dict[str, Sequence[int]]] = None) -> bytes:
    """The score with each relabelled Part's own Instrument given a new id attribute and instrumentId, and so the
    instrument changes to that same instrument on the part's staves (``part_staves``: Part id -> 0-based staff
    indices); every other byte stays.  Raises when a Part or its expected instrument is not there."""
    text = mscx.decode("utf-8")
    for r in relabels:
        m = None
        for pm in _PART_RE.finditer(text):
            if pm.group(1) == r.part:
                m = pm
                break
        if m is None:
            raise AccompanimentError(f"relabel: no Part id={r.part!r} in the score")
        end = text.find("</Part>", m.end())
        im = _INSTR_RE.search(text, m.end(), end)
        idm = re.search(r'\bid="([^"]*)"', im.group(1)) if im else None
        if idm is None or idm.group(1) != r.old:
            # an instrument the part only changes to (a clarinet in A for some passages): its changes are relabelled
            have = idm.group(1) if idm else None
            blocks = _staff_blocks(text)
            changes = any(f'id="{r.old}"' in ic.group(0)
                          for i in (part_staves or {}).get(r.part, ()) if i < len(blocks)
                          for ic in _IC_RE.finditer(text, *blocks[i]))
            if not changes:
                raise AccompanimentError(f"relabel: Part {r.part}'s instrument is {have!r}, not {r.old!r}, and it "
                                         f"has no instrument change to {r.old!r} (update {OPTIONS_NAME})")
        else:
            text, _ = _relabel_instrument(text, m.end(), end, r, f"Part {r.part}")
        if part_staves and part_staves.get(r.part):
            blocks = _staff_blocks(text)
            for i in sorted(part_staves[r.part], reverse=True):     # from the end: earlier offsets stay valid
                if i < len(blocks):
                    b0, b1 = blocks[i]
                    chunk = _IC_RE.sub(lambda mm: _relabel_instrument(mm.group(0), 0, len(mm.group(0)), r,
                                                                      f"Part {r.part}'s instrument change")[0],
                                       text[b0:b1])
                    text = text[:b0] + chunk + text[b1:]
    return text.encode("utf-8")


_IC_RE = re.compile(r"<InstrumentChange>.*?</InstrumentChange>", re.S)
_STAFF_RE = re.compile(r'<Staff id="[^"]*">')
_FIRST_CR_RE = re.compile(r"<(?:Chord|Rest|MeasureRepeat)\b")


def add_techniques(mscx: bytes, techniques: Sequence[Technique],
                   part_staves: Dict[str, Sequence[int]]) -> bytes:
    """The score with each technique's part starting with it: a ``PlayTechAnnotation`` before the first chord or rest
    of each of the part's staves (in its first measure's first voice).  Every other byte stays."""
    text = mscx.decode("utf-8")
    for t in techniques:
        staves = list(part_staves.get(t.part) or ())
        if not staves:
            raise AccompanimentError(f"techniques: the score has no part {t.part!r}")
        label = t.text or t.technique
        el = (f"<PlayTechAnnotation><playTechType>{t.technique}</playTechType><text>{label}</text>"
              f"</PlayTechAnnotation>")
        for i in sorted(staves, reverse=True):          # from the end: earlier offsets stay valid
            blocks = _staff_blocks(text)
            if i >= len(blocks):
                continue
            b0, b1 = blocks[i]
            m = _FIRST_CR_RE.search(text, b0, b1)
            if m is None:
                continue
            text = text[:m.start()] + el + text[m.start():]
    return text.encode("utf-8")


def _staff_blocks(text: str) -> List[Tuple[int, int]]:
    """(start, end) of the score's staves (the ``<Staff id=..>`` elements after the last Part), in order."""
    last = text.rfind("</Part>")
    out = []
    pos = last if last >= 0 else 0
    while True:
        m = _STAFF_RE.search(text, pos)
        if not m:
            break
        e = text.find("</Staff>", m.end())
        if e < 0:
            break
        out.append((m.start(), e))
        pos = e
    return out


def part_staves(a: Analysis) -> Dict[str, List[int]]:
    return {pid: list(p.staves) for pid, p in a.parts.items()}


def renames_for(relabels: Sequence[Relabel]) -> Dict[Tuple[str, str], str]:
    return {(r.part, r.old): r.new for r in relabels}


def instrument_tracks(a: Analysis, relabels: Sequence[Relabel] = ()) -> List[Tuple[str, str]]:
    """Every (partId, Instrument id) MuseScore makes a playback track of, after relabelling."""
    ren = renames_for(relabels)
    return [(pid, ren.get((pid, iid), iid)) for pid, p in a.parts.items() for iid, _mx in p.instruments]


def _main_mscx(z: zipfile.ZipFile) -> str:
    try:
        root = ET.fromstring(z.read("META-INF/container.xml"))
        for rf in root.iter("rootfile"):
            p = rf.get("full-path", "")
            if p.endswith(".mscx"):
                return p
    except KeyError:
        pass
    names = [n for n in z.namelist() if n.endswith(".mscx") and "/" not in n]
    if not names:
        raise AccompanimentError("no .mscx in the score")
    return names[0]


def write_copy(src: str, dst: str, a: Analysis, plan: MutePlan, o: ScoreOptions,
               drop_excerpts: bool = False, mscx_override: Optional[bytes] = None,
               muted_override: Optional[Set[str]] = None, relabel: bool = True, dry: bool = False) -> None:
    """The render copy of ``src`` at ``dst`` (every member as it is, except the score and audiosettings)."""
    relabels = o.relabel if relabel else []
    muted = set(plan.muted) if muted_override is None else set(muted_override)
    with zipfile.ZipFile(src) as zin:
        main = _main_mscx(zin)
        names = zin.namelist()
        tmp = dst + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                n = item.filename
                if drop_excerpts and n.startswith("Excerpts/"):
                    continue
                data = zin.read(n)
                if n == main:
                    if mscx_override is not None:
                        data = mscx_override
                    else:
                        data = relabel_mscx(data, relabels, part_staves(a))
                        if o.techniques:
                            data = add_techniques(data, o.techniques, part_staves(a))
                elif n == "audiosettings.json":
                    data = edit_audiosettings(data, instrument_tracks(a, relabels), muted, renames_for(relabels),
                                              o.reset_reverb, dry)
                elif n == "META-INF/container.xml" and drop_excerpts:
                    data = re.sub(rb'\s*<rootfile full-path="Excerpts/[^"]*"\s*/>', b"", data)
                zout.writestr(item, data)
            if "audiosettings.json" not in names:
                zout.writestr("audiosettings.json", edit_audiosettings(None, instrument_tracks(a, relabels), muted,
                                                                       renames_for(relabels), o.reset_reverb, dry))
    os.replace(tmp, dst)


# ------------------------------------------------------------------------------
# the probe score: one note per instrument
# ------------------------------------------------------------------------------

@dataclass
class Slot:
    part: str
    instrument: str             # Instrument id attribute (after relabelling)
    musicxml: str
    staff: int
    pitch: int
    change: bool                # reached by an instrument change in the score
    role: str = "median"        # the staff's "median" / "lowest" / "highest" pitch, or a "drum" of a percussion staff
    count: int = 0              # notes at that pitch (lowest: at or below, highest: at or above)
    start: float = 0.0          # seconds in the probe render

    def label(self, a: Analysis) -> str:
        return f"{a.parts[self.part].name} [{self.instrument}]"


_TPC = {0: 14, 1: 21, 2: 16, 3: 11, 4: 18, 5: 13, 6: 20, 7: 15, 8: 10, 9: 17, 10: 12, 11: 19}


def probe_slots(a: Analysis, plan: MutePlan, relabels: Sequence[Relabel] = (),
                parts: Optional[Set[str]] = None) -> List[Slot]:
    """One slot per instrument of every part that sounds in the render (or of ``parts``)."""
    ren = renames_for(relabels)
    slots = []
    for pid, p in a.parts.items():
        if parts is not None and pid not in parts:
            continue
        if parts is None and (pid in plan.muted or not a.part_notes(pid)):
            continue
        staff = max(p.staves, key=lambda i: (a.staves[i].notes, -i))
        st = a.staves[staff]
        if not st.pitches:
            continue
        done = set()
        for k, (iid, mx) in enumerate(p.instruments):
            new = ren.get((pid, iid), iid)
            if new in done:             # relabelled onto an instrument the part changes to anyway
                continue
            done.add(new)
            relab = next((r for r in relabels if r.part == pid and r.old == iid), None)
            for pitch, role, count in probe_pitches(a, pid, staff):
                slots.append(Slot(part=pid, instrument=new, musicxml=relab.musicxml if relab else mx, staff=staff,
                                  pitch=pitch, change=k > 0, role=role, count=count))
    for k, s in enumerate(slots):
        s.start = (1 + PROBE_SLOT_MEASURES * k) * PROBE_MEASURE_S
    return slots


def probe_pitches(a: Analysis, pid: str, staff: int, drums: int = 8) -> List[Tuple[int, str, int]]:
    """(pitch, role, notes) to probe an instrument at: the median, lowest and highest pitch of all its staves (on
    ``staff``, the one with the most notes); a percussion staff's most used pitches (its instruments)."""
    st = a.staves[staff]
    if st.percussion:
        counts: Dict[int, int] = {}
        for x in st.pitches:
            counts[x] = counts.get(x, 0) + 1
        return [(x, "drum", n) for x, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:drums]]
    allp = sorted(x for i in a.parts[pid].staves for x in a.staves[i].pitches if not a.staves[i].percussion)
    if not allp:
        return []
    med = allp[(len(allp) - 1) // 2]
    out = [(med, "median", allp.count(med))]
    if allp[0] != med:
        out.append((allp[0], "lowest", sum(1 for x in allp if x <= allp[0])))
    if allp[-1] != med:
        out.append((allp[-1], "highest", sum(1 for x in allp if x >= allp[-1])))
    return out


def probe_mscx(mscx: bytes, a: Analysis, slots: Sequence[Slot]) -> bytes:
    """The copy's score (relabelled already) with every staff's music replaced: a measure's rest, then each slot's
    instrument plays one whole note at 120 bpm followed by two measures' rest."""
    root = ET.fromstring(mscx)
    score = root.find("Score")
    staves = score.findall("Staff")
    if len(staves) != len(a.staves):
        raise AccompanimentError(f"probe: {len(staves)} staves in the score, {len(a.staves)} analysed")
    changes: Dict[Tuple[str, str], ET.Element] = {}
    for i, el in enumerate(staves):
        for ic in el.iter("InstrumentChange"):
            ins = ic.find("Instrument")
            if ins is not None:
                changes.setdefault((a.staves[i].part, ins.get("id", "")), ic)
    by_measure = {1 + PROBE_SLOT_MEASURES * k: s for k, s in enumerate(slots)}
    n_measures = 1 + PROBE_SLOT_MEASURES * len(slots) + 1
    for i, el in enumerate(staves):
        for child in list(el):
            el.remove(child)
        perc = a.staves[i].percussion
        for m in range(n_measures):
            meas = ET.SubElement(el, "Measure")
            voice = ET.SubElement(meas, "voice")
            if m == 0:
                if not perc:
                    ks = ET.SubElement(voice, "KeySig")
                    ET.SubElement(ks, "concertKey").text = "0"
                ts = ET.SubElement(voice, "TimeSig")
                ET.SubElement(ts, "sigN").text = "4"
                ET.SubElement(ts, "sigD").text = "4"
                if i == 0:
                    tempo = ET.SubElement(voice, "Tempo")
                    ET.SubElement(tempo, "tempo").text = f"{PROBE_BPM / 60:g}"
                    ET.SubElement(tempo, "followText").text = "1"
                    ET.SubElement(tempo, "text").text = f"= {PROBE_BPM}"
            s = by_measure.get(m)
            if s is not None and s.staff == i:
                if s.change:
                    ic = changes.get((s.part, s.instrument))
                    if ic is not None:
                        ic = copy.deepcopy(ic)
                        for e in list(ic.iter()):
                            for sub in [c for c in e if c.tag == "eid"]:
                                e.remove(sub)
                        voice.append(ic)
                chord = ET.SubElement(voice, "Chord")
                ET.SubElement(chord, "durationType").text = "whole"
                note = ET.SubElement(chord, "Note")
                ET.SubElement(note, "pitch").text = str(s.pitch)
                ET.SubElement(note, "tpc").text = str(_TPC[s.pitch % 12])
            else:
                rest = ET.SubElement(voice, "Rest")
                ET.SubElement(rest, "durationType").text = "measure"
                ET.SubElement(rest, "duration").text = "4/4"
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8", xml_declaration=False)


def probe_verdicts(slots: Sequence[Slot], muse_db: Sequence[float], basic_db: Sequence[float],
                   diff_db: Sequence[float], window: float) -> List[dict]:
    """Per slot: "muse" (Muse Sounds plays it), "basic" (the same sound without the engine: no library for it),
    "silent-muse" (Muse Sounds chose an instrument that plays nothing at that pitch), "silent" (nothing either
    way).  The arguments are RMS levels (dBFS) of consecutive ``window``-second blocks of the probe rendered with
    and without the Muse Sounds engine and of their difference."""
    def level(seq, t0, t1):
        i0, i1 = int(round(t0 / window)), int(round(t1 / window))
        vals = [10 ** (v / 10) for v in seq[i0:i1] if v is not None and v > -200]
        return 10 * __import__("math").log10(sum(vals) / max(1, i1 - i0)) if vals else -200.0

    out = []
    for s in slots:
        t0, t1 = s.start + 0.1, s.start + PROBE_MEASURE_S - 0.1
        m, b, d = level(muse_db, t0, t1), level(basic_db, t0, t1), level(diff_db, t0, t1)
        if m < -75 and b < -75:
            v = "silent"
        elif d < max(m, b) - 30:
            v = "basic"
        elif m < -75 or m < b - 35:
            v = "silent-muse"
        else:
            v = "muse"
        out.append({"part": s.part, "instrument": s.instrument, "pitch": s.pitch, "role": s.role, "notes": s.count,
                    "verdict": v, "muse_db": round(m, 1), "basic_db": round(b, 1), "diff_db": round(d, 1)})
    return out


def summarize_probe(verdicts: Sequence[dict]) -> Tuple[List[dict], List[dict]]:
    """(instruments that play the basic sounds or nothing, probed pitches Muse Sounds leaves silent)."""
    fallback, seen = [], set()
    for v in verdicts:
        key = (v["part"], v["instrument"])
        if key in seen or v["role"] not in ("median", "drum"):
            continue
        seen.add(key)
        if v["verdict"] in ("basic", "silent"):
            fallback.append(v)
    silent = [v for v in verdicts if v["verdict"] == "silent-muse"]
    return fallback, silent


# ------------------------------------------------------------------------------
# Muse Sounds libraries, tools, cache key
# ------------------------------------------------------------------------------

def muse_sounds_dir(musesampler: str) -> Optional[str]:
    try:
        with open(os.path.join(musesampler, ".config"), encoding="utf-8") as f:
            d = f.read().strip()
        return d or None
    except OSError:
        return None


def muse_libraries(musesampler: str) -> Dict[str, Any]:
    """The installed Muse Sounds packages: ``{name: {"guid", "instruments": [[name, version], ...]}}``, read
    (read-only) from the Muse Sounds folder's ``.instruments`` database."""
    d = muse_sounds_dir(musesampler)
    if not d:
        return {}
    db = os.path.join(d, ".instruments")
    if not os.path.isfile(db):
        return {}
    uri = "file:" + urllib.parse.quote(db) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        pk = {k: (g, n or p) for k, g, p, n in con.execute("select key, guid, path, name from packages")}
        out: Dict[str, Any] = {}
        for _id, path, state, version, pkey, name in con.execute(
                "select id, path, state, version, package_key, name from instruments order by id"):
            if state != 1 or pkey not in pk:          # 1 = installed
                continue
            guid, pname = pk[pkey]
            e = out.setdefault(pname, {"guid": guid, "instruments": []})
            e["instruments"].append([(name or path).strip(), version or ""])
        return out
    finally:
        con.close()


def libraries_current(recorded: Optional[Dict[str, Any]], installed: Dict[str, Any], had_fallback: bool) -> bool:
    """Whether a render made with the libraries ``recorded`` still sounds as the libraries ``installed`` would
    render it: every library it had is still there unchanged; if some of its instruments fell back to the basic
    sounds, also no library was added (a new library may have a sound for them)."""
    if recorded is None:
        return False
    for name, e in recorded.items():
        if installed.get(name) != e:
            return False
    if had_fallback and set(installed) - set(recorded):
        return False
    return True


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tools_signature(s: Settings) -> Dict[str, str]:
    mscore = os.path.realpath(s.path("mscore"))
    lib = os.path.join(s.path("musesampler"), "lib", "libMuseSamplerCoreLib.so")
    sig = {"mscore": os.path.basename(mscore)}
    try:
        st = os.stat(mscore)
        sig["mscore"] += f" {st.st_size}"
    except OSError:
        sig["mscore"] += " missing"
    sig["musesampler"] = _sha256_file(lib)[:16] if os.path.isfile(lib) else "missing"
    return sig


def cache_key(score_sha256: str, options: dict, plan: dict, tools: dict, encode: dict,
              timeline: Optional[dict] = None) -> str:
    doc = {"pipeline": PIPELINE_VERSION, "score": score_sha256, "options": options, "plan": plan, "tools": tools,
           "encode": encode}
    if timeline is not None:                 # a song on a piano solo's clock (the other scores' keys stay)
        doc["timeline"] = timeline
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


# ------------------------------------------------------------------------------
# running MuseScore and ffmpeg
# ------------------------------------------------------------------------------

def _run(argv: List[str], timeout: Optional[int] = None, env=None, log_path: Optional[str] = None
         ) -> subprocess.CompletedProcess:
    """Run with a lower priority; the whole process group is killed on timeout.  Stdin is closed: ffmpeg reads
    keys from it (each "-" lowers its log level, which hides loudnorm's report), and a calling shell loop's input
    must not be eaten."""
    out = open(log_path, "wb") if log_path else subprocess.PIPE
    try:
        p = subprocess.Popen(["nice", "-n", "10"] + argv, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT if log_path else subprocess.PIPE, env=env,
                             start_new_session=True)
        try:
            so, se = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            raise AccompanimentError(f"{os.path.basename(argv[0])} timed out after {timeout}s")
        return subprocess.CompletedProcess(argv, p.returncode, so, se)
    finally:
        if log_path:
            out.close()


@dataclass
class RenderResult:
    seconds: float
    muse_engine: str              # the engine version from MuseScore's log, "" when it was not loaded
    muse_tracks: int              # tracks MuseScore gave a Muse Sounds instrument
    fallback_tracks: int          # tracks without a sound in the profile (basic sounds instead)
    log: str


def _xdg(work: str, muse: bool, musesampler: str) -> Dict[str, str]:
    base = os.path.join(work, "xdg-muse" if muse else "xdg-basic")
    env = dict(os.environ)
    for k in ("config", "cache", "data"):
        os.makedirs(os.path.join(base, k), exist_ok=True)
        env[f"XDG_{k.upper()}_HOME"] = os.path.join(base, k)
    link = os.path.join(base, "data", "MuseSampler")
    if muse:
        if os.path.islink(link) and os.readlink(link) != musesampler:
            os.unlink(link)
        if not os.path.lexists(link):
            os.symlink(musesampler, link)
    elif os.path.lexists(link):
        os.unlink(link)
    env.pop("MUSESAMPLER_PATH", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def render(s: Settings, mscz: str, wav: str, muse: bool = True, log_copy: Optional[str] = None) -> RenderResult:
    """``mscore -o wav --sound-profile MuseSounds mscz``; ``muse=False`` hides the Muse Sounds engine (every
    instrument falls back to the basic sounds through the same path an instrument without a library takes)."""
    work = s.path("work_dir")
    env = _xdg(work, muse, s.path("musesampler"))
    logs = os.path.join(env["XDG_DATA_HOME"], "MuseScore", "MuseScore4", "logs")
    if os.path.isdir(logs):
        for n in os.listdir(logs):
            if n.endswith(".log"):
                os.unlink(os.path.join(logs, n))
    if os.path.exists(wav):
        os.unlink(wav)
    t = time.monotonic()
    console = (log_copy or wav) + ".console"
    r = _run([s.path("mscore"), "-o", wav, "--sound-profile", SOUND_PROFILE, mscz], timeout=s.timeout, env=env,
             log_path=console)
    secs = time.monotonic() - t
    text = ""
    if os.path.isdir(logs):
        files = sorted((os.path.join(logs, n) for n in os.listdir(logs) if n.endswith(".log")), key=os.path.getmtime)
        if files:
            with open(files[-1], encoding="utf-8", errors="replace") as f:
                text = f.read()
    if log_copy:
        with open(log_copy, "w", encoding="utf-8") as f:
            f.write(text)
    if os.path.exists(console):
        os.unlink(console)
    if r.returncode != 0 or not os.path.isfile(wav):
        tail = "\n".join(line for line in text.splitlines() if "| ERROR" in line)[-1500:]
        raise AccompanimentError(f"MuseScore failed (exit {r.returncode}) on {os.path.basename(mscz)}\n{tail}")
    m = re.search(r"MuseSampler successfully inited: .*?version: ([\w.]+)", text)
    engine = m.group(1) if m else ""
    m = re.search(r"^MuseSamplerWrapper::addTrack\s+[\d.]+ ms\s+(\d+)", text, re.M)
    muse_tracks = int(m.group(1)) if m else 0
    fallback = len(re.findall(r"invalid audio source params for trackId", text))
    if muse and not engine:
        raise AccompanimentError("MuseScore did not load the Muse Sounds engine (see the log); not rendering "
                                 "with the basic sounds instead")
    if not muse and engine:
        raise AccompanimentError("the reference render loaded the Muse Sounds engine")
    return RenderResult(seconds=secs, muse_engine=engine, muse_tracks=muse_tracks, fallback_tracks=fallback,
                        log=log_copy or "")


def _ffmpeg(args: List[str], timeout: int = 3600) -> str:
    r = _run(["ffmpeg", "-hide_banner", "-nostats"] + args, timeout=timeout)
    err = (r.stderr or b"").decode("utf-8", "replace") + (r.stdout or b"").decode("utf-8", "replace")
    if r.returncode != 0:
        raise AccompanimentError(f"ffmpeg failed: {' '.join(args)[:300]}\n{err[-1500:]}")
    return err


def loudness(path: str) -> Dict[str, float]:
    """Integrated loudness (LUFS), true peak (dBTP), loudness range (LU) by ffmpeg's loudnorm measurement."""
    err = _ffmpeg(["-i", path, "-af", "loudnorm=I=-16:TP=-1:LRA=20:print_format=json", "-f", "null", "-"])
    j = json.loads(err[err.rindex("{"):err.rindex("}") + 1])
    return {"lufs": float(j["input_i"]), "true_peak": float(j["input_tp"]), "lra": float(j["input_lra"])}


def block_seconds(window: float, sample_rate: int = 44100) -> float:
    """The length of the blocks :func:`window_levels` measures for ``window`` (a whole number of samples)."""
    return max(1, int(round(window * sample_rate))) / sample_rate


def window_levels(inputs: List[str], window: float, expr: Optional[str] = None,
                  sample_rate: int = 44100) -> Tuple[List[float], List[float]]:
    """RMS and peak level (dBFS) of consecutive blocks of :func:`block_seconds` (``window``) of the mono mix of
    ``inputs[0]`` (at ``sample_rate``); with two inputs and ``expr`` "diff", of the first minus the second."""
    n = max(1, int(round(window * sample_rate)))
    flt = "aformat=sample_fmts=flt|fltp|dbl"          # float all the way: a render may peak above 0 dBFS
    mono = f"aresample={sample_rate},{flt},pan=mono|c0=0.5*c0+0.5*c1,{flt}"
    if len(inputs) == 2 and expr == "diff":
        graph = f"[0]{mono}[a];[1]{mono}[b];[a][b]amerge=inputs=2,pan=mono|c0=c0-c1,"
    else:
        graph = f"[0]{mono},"
    tmp = os.path.join(os.path.dirname(os.path.abspath(inputs[0])), f".levels-{os.getpid()}.txt")
    graph += (f"asetnsamples=n={n}:p=0,astats=metadata=1:reset=1:measure_overall=RMS_level+Peak_level:"
              f"measure_perchannel=none,ametadata=mode=print:file={tmp}")
    args = []
    for i in inputs:
        args += ["-i", i]
    _ffmpeg(args + ["-filter_complex", graph, "-f", "null", "-"])
    rms: List[float] = []
    peak: List[float] = []
    try:
        with open(tmp, encoding="utf-8") as f:
            for line in f:
                if line.startswith("lavfi.astats.Overall.RMS_level="):
                    rms.append(_db(line.split("=", 1)[1]))
                elif line.startswith("lavfi.astats.Overall.Peak_level="):
                    peak.append(_db(line.split("=", 1)[1]))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return rms, peak


def _db(v: str) -> float:
    v = v.strip()
    return -200.0 if v in ("-inf", "nan", "") else max(-200.0, float(v))


def nan_blocks(path: str, window: float = 0.1) -> List[float]:
    """Start times of the ``window``-second blocks of ``path`` that have NaN or infinite samples (a synthesizer
    bug: the mix of every instrument is lost there, and LAME refuses the file)."""
    n = max(1, int(round(window * 44100)))
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)), f".nan-{os.getpid()}.txt")
    _ffmpeg(["-i", path, "-af", f"aresample=44100,aformat=sample_fmts=flt|fltp|dbl,asetnsamples=n={n}:p=0,"
             f"astats=metadata=1:reset=1:measure_overall=Number_of_NaNs+Number_of_Infs:measure_perchannel=none,"
             f"ametadata=mode=print:file={tmp}", "-f", "null", "-"])
    out: List[float] = []
    try:
        t = 0.0
        with open(tmp, encoding="utf-8") as f:
            for line in f:
                if line.startswith("frame:"):
                    m = re.search(r"pts_time:([\d.]+)", line)
                    t = float(m.group(1)) if m else t
                elif line.startswith("lavfi.astats.") and ("NaN" in line or "Inf" in line):
                    v = line.split("=", 1)[1].strip()        # "Number of NaNs" (ffmpeg 8) or "...Number_of_NaNs"
                    if float(v) > 0 and (not out or out[-1] != t):
                        out.append(t)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return out


def nan_bursts(blocks: Sequence[float], window: float = 0.1) -> List[Tuple[float, float]]:
    """Consecutive NaN blocks joined: [(from, to)] in seconds."""
    out: List[Tuple[float, float]] = []
    for t in blocks:
        if out and t - out[-1][1] <= window * 1.5:
            out[-1] = (out[-1][0], t + window)
        else:
            out.append((t, t + window))
    return out


def duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def encode_args(wav: str, mp3: str, gain_db: float, bitrate: int, title: str = "", artist: str = "",
                comment: str = "", end: Optional[float] = None) -> List[str]:
    """ffmpeg arguments for :func:`encode_mp3`."""
    af = (f"atrim=start_sample={MP3_CODEC_DELAY}" + (f":end={end:.3f}" if end else "")
          + f",asetpts=PTS-STARTPTS,volume={gain_db:.3f}dB")
    args = ["-y", "-i", wav, "-af", af, "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-ar", str(MP3_SAMPLE_RATE),
            "-ac", "2", "-map_metadata", "-1", "-id3v2_version", "3", "-write_xing", "0"]
    for k, v in (("title", title), ("artist", artist), ("comment", comment)):
        if v:
            args += ["-metadata", f"{k}={v}"]
    return args + [mp3]


def encode_mp3(wav: str, mp3: str, gain_db: float, bitrate: int, title: str = "", artist: str = "",
               comment: str = "", end: Optional[float] = None) -> None:
    """``wav`` times ``gain_db`` (a plain gain; ``end``: cut there) as a LAME mp3, constant bit rate, that every
    decoder plays on the score's timeline: the encoder and decoder delay (:data:`MP3_CODEC_DELAY` samples) is cut
    from the start and no Xing/LAME header is written, so no decoder can apply gapless trimming and a plain
    frame-by-frame decode (what a Sonos fed from a frame index does) lines up at sample 0."""
    _ffmpeg(encode_args(wav, mp3, gain_db, bitrate, title, artist, comment, end))


def lame_tag(mp3: str) -> Dict[str, Any]:
    """Encoder delay and padding from the LAME tag of the Xing/Info frame (the gapless information a decoder may
    honour), and where the first audio frame starts."""
    with open(mp3, "rb") as f:
        data = f.read(1 << 16)
    pos = 0
    if data[:3] == b"ID3":
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        pos = 10 + size
        with open(mp3, "rb") as f:
            f.seek(pos)
            data = f.read(1 << 16)
        base = pos
        pos = 0
    else:
        base = 0
    while pos + 4 < len(data) and not (data[pos] == 0xFF and (data[pos + 1] & 0xE0) == 0xE0):
        pos += 1
    out: Dict[str, Any] = {"info_frame": False}
    if pos + 4 >= len(data):
        return out
    hdr = data[pos:pos + 4]
    mpeg1 = (hdr[1] >> 3) & 3 == 3
    mono = (hdr[3] >> 6) == 3
    side = (17 if mono else 32) if mpeg1 else (9 if mono else 17)
    x = pos + 4 + side
    tag = data[x:x + 4]
    if tag not in (b"Xing", b"Info"):
        return out
    out["info_frame"] = True
    out["kind"] = tag.decode()
    flags = int.from_bytes(data[x + 4:x + 8], "big")
    off = x + 8 + (4 if flags & 1 else 0) + (4 if flags & 2 else 0) + (100 if flags & 4 else 0) + (4 if flags & 8 else 0)
    if data[off:off + 4] in (b"LAME", b"Lavc", b"Lavf") or data[off:off + 4].isalnum():
        enc = data[off:off + 9].decode("latin-1", "replace")
        d = data[off + 21:off + 24]
        out["encoder"] = enc.strip("\x00 ")
        out["encoder_delay"] = (d[0] << 4) | (d[1] >> 4)
        out["padding"] = ((d[1] & 0x0F) << 8) | d[2]
    out["first_frame_offset"] = base + pos
    return out


# ------------------------------------------------------------------------------
# listening by analysis (pure helpers over window levels)
# ------------------------------------------------------------------------------

def _active(spans: Sequence[Tuple[float, float]], n: int, window: float, pre: float = 0.0, post: float = 0.0
            ) -> List[bool]:
    act = [False] * n
    for on, off in spans:
        i0 = max(0, int((on - pre) / window))
        i1 = min(n, int((off + post) / window) + 1)
        for i in range(i0, i1):
            act[i] = True
    return act


def _runs(flags: Sequence[bool], window: float, min_s: float) -> List[Tuple[float, float]]:
    out, start = [], None
    for i, f in enumerate(list(flags) + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            if (i - start) * window >= min_s:
                out.append((start * window, i * window))
            start = None
    return out


def listen_checks(a: Analysis, plan: MutePlan, rms: Sequence[float], window: float) -> Dict[str, Any]:
    """Silence where the render's instruments have notes; sound where only muted instruments have notes."""
    n = len(rms)
    audible = [sp for i in plan.audible_staves for sp in a.staves[i].spans]
    muted = [sp for st in a.staves if st.part in plan.muted for sp in st.spans]
    sounding = _active(audible, n, window, post=0.3)
    gaps = _runs([sounding[i] and rms[i] < -70 for i in range(n)], window, 1.0)
    tails = _active(audible, n, window, pre=0.3, post=3.0)
    muted_only = _active(muted, n, window, post=0.2)
    solo = [muted_only[i] and not tails[i] for i in range(n)]
    stretches = _runs(solo, window, 2.0)
    bleed = []
    for t0, t1 in stretches:
        i0, i1 = int(t0 / window), int(t1 / window)
        lv = max(rms[i0:i1]) if i1 > i0 else -200
        bleed.append({"from": round(t0, 1), "to": round(t1, 1), "max_rms_db": round(lv, 1)})
    loud = [b for b in bleed if b["max_rms_db"] > -55]
    return {"silent_gaps": [{"from": round(x, 1), "to": round(y, 1)} for x, y in gaps],
            "piano_only_stretches": len(bleed),
            "piano_only_seconds": round(sum(b["to"] - b["from"] for b in bleed), 1),
            "piano_only_max_db": max((b["max_rms_db"] for b in bleed), default=None),
            "bleed_suspects": loud}


def onset_times(a: Analysis, plan: MutePlan) -> List[float]:
    return sorted(on for i in plan.audible_staves for on, _off in a.staves[i].spans)


# ------------------------------------------------------------------------------
# the command
# ------------------------------------------------------------------------------

@dataclass
class Candidate:
    rel: str
    path: str
    song: str                 # PianoVision JSON name
    entry: dict
    options: Optional[dict] = None    # the score's build options (scores.json), as convert_mscz takes them
    # the score MuseScore renders and its knobs and own build options: the song's score, or with orchestra_from the
    # orchestral partner (its options in accompaniment.json apply)
    source_rel: str = ""
    source_path: str = ""
    source_compat: Optional[dict] = None
    source_options: Optional[dict] = None
    parts_path: str = ""                # the song's Note Waterfall parts file

    @property
    def stem(self) -> str:
        return self.song[:-5] if self.song.lower().endswith(".json") else self.song

    @property
    def partner(self) -> Optional[dict]:
        """The build options' partner score (:mod:`pianovision.timeline`): the song plays on the solo's clock."""
        return (self.options or {}).get("partner")


def candidates(manifest: dict, scores_dir: str, only: Sequence[str] = (), lib=None) -> List[Candidate]:
    """Scores whose parts file has orchestra notes besides vocal staves' (the manifest's parts statistics),
    optionally filtered by globs matched against the score's path (relative to the scores folder) or its song name;
    ``lib``: the library, for the scores' build options.  A song with ``orchestra_from`` renders its orchestral
    partner (globs match that score's path too)."""
    from .buildopts import without_partner
    out = []
    for rel, e in sorted(manifest.get("entries", {}).items()):
        p = e.get("parts") or {}
        notes = (p.get("notes") or {}).get("orchestra", 0) - p.get("vocal", 0)     # a vocal line alone is no accompaniment
        if not notes or not e.get("output"):
            continue
        opts = lib.render_options(rel) if lib is not None else None
        partner = (opts or {}).get("partner")
        path = os.path.join(scores_dir, rel)
        if partner and partner["kind"] == "orchestra":
            src = (partner["rel"], partner["path"], partner.get("compat"), partner.get("options"))
        else:
            src = (rel, path, _verify_compat(e), without_partner(opts))
        if only and not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(e["output"], g)
                            or fnmatch.fnmatch(e["output"][:-5], g) or fnmatch.fnmatch(src[0], g) for g in only):
            continue
        out.append(Candidate(rel=rel, path=path, song=e["output"], entry=e, options=opts, source_rel=src[0],
                             source_path=src[1], source_compat=src[2], source_options=src[3],
                             parts_path=lib.parts_path(e["output"]) if lib is not None else ""))
    return out


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _verify_compat(e: dict) -> Optional[dict]:
    from .library import Library
    return Library._verify_compat(e)


def load_state(out_dir: str) -> dict:
    p = os.path.join(out_dir, REPORT_NAME + ".json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("format") == 1 else {"format": 1, "scores": {}}
    except (OSError, ValueError):
        return {"format": 1, "scores": {}}


def save_state(out_dir: str, state: dict) -> None:
    p = os.path.join(out_dir, REPORT_NAME + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, p)


def process(c: Candidate, s: Settings, defaults: dict, per_score: Dict[str, dict], state: dict, force: bool,
            dry_run: bool, log: Log) -> Tuple[str, dict]:
    """One score: ("skipped" | "rendered" | "would render", its report record)."""
    out_dir, work_root = s.path("out_dir"), s.path("work_dir")
    src = c.source_path or c.path
    o = score_options(defaults, per_score.get(c.source_rel or c.rel))
    score_sha = _sha256_file(src)
    a = analyse(src, c.source_compat, c.source_options)
    plan = plan_mutes(a, o)
    tools = tools_signature(s)
    encode = {"bitrate": s.bitrate, "lufs": s.lufs, "true_peak": s.true_peak, "sample_rate": MP3_SAMPLE_RATE,
              "codec_delay_cut": MP3_CODEC_DELAY, "xing_header": False}
    key = cache_key(score_sha, o.to_dict(), plan.to_dict(), tools, encode, timeline_key(c))
    installed = muse_libraries(s.path("musesampler"))
    prev = state.get("scores", {}).get(c.rel) or {}
    mp3 = os.path.join(out_dir, c.stem + ".mp3")
    fresh = (not force and prev.get("key") == key and os.path.isfile(mp3)
             and prev.get("mp3_sha256") == _sha256_file(mp3)
             and libraries_current(prev.get("libraries"), installed, bool(prev.get("had_fallback", True))))
    if fresh:
        return "skipped", prev
    if dry_run:
        return "would render", prev
    work = os.path.join(work_root, c.stem)
    os.makedirs(work, exist_ok=True)
    # a song on a piano solo's clock (pianovision.timeline): the score rendered is the edited copy, and with
    # orchestra_from its render is cut into pieces placed on the solo's clock
    pr = None
    copy_src = src
    a_copy = a
    if c.partner:
        log(f"  putting {os.path.basename(src)} on the solo's clock ...")
        from . import timeline as TL
        pr = song_pair(c)
        copy_src = TL.write_edited(src, os.path.join(work, "timeline.mscz"), pr.plan)
        a_copy = analyse(copy_src, c.source_compat, c.source_options)
        a = a_copy
        a.title, a.artist = _song_title(c, a)
    rec: Dict[str, Any] = {"score": c.rel, "song": c.song, "mp3": os.path.basename(mp3), "title": a.title,
                           "artist": a.artist, "rendered": _now(), "options": o.to_dict(), "plan": plan.to_dict(),
                           "parts": {pid: {"name": p.name, "instruments": [i for i, _ in p.instruments],
                                           "staves": [i + 1 for i in p.staves], "notes": a.part_notes(pid)}
                                     for pid, p in a.parts.items()},
                           "staff_kinds": {str(st.idx + 1): st.kind for st in a.staves},
                           "score_seconds": round(a.end_time, 2), "warnings": []}
    if c.entry.get("content") and c.entry.get("content") != _content_hash(c.path):
        rec["warnings"].append("the score changed since the last `build`: the song JSON may not match it yet")
    if c.source_rel and c.source_rel != c.rel:
        rec["source"] = c.source_rel

    # 2. copy
    copy_path = os.path.join(work, "score.mscz")
    write_copy(copy_src, copy_path, a, plan, o)
    rec["relabels"] = [r.to_dict() for r in o.relabel]
    for r in o.relabel:
        p = a.parts.get(r.part)
        pitches = sorted(x for i in (p.staves if p else []) for x in a.staves[i].pitches)
        if pitches:
            r_rec = next(x for x in rec["relabels"] if x["part"] == r.part)
            r_rec["pitches"] = [pitches[0], pitches[-1]]

    # 3. render
    log(f"  rendering with Muse Sounds ...")
    wav = os.path.join(work, "render.wav")
    rr = render(s, copy_path, wav, muse=True, log_copy=os.path.join(work, "render.log"))
    rec["render"] = {"seconds": round(rr.seconds, 1), "muse_engine": rr.muse_engine, "muse_tracks": rr.muse_tracks,
                     "fallback_tracks": rr.fallback_tracks}
    rec["muse_engine"] = rr.muse_engine
    rec["libraries"] = installed
    wav_seconds = duration(wav)
    rec["render_seconds"] = round(wav_seconds, 2)
    if pr is not None:
        from . import timeline as TL
        if pr.plan.pieces:
            TL.place_pieces(pr.plan, pr.orch.timeline, pr.orch.seconds, wav_seconds)
            placed = os.path.join(work, "song.wav")
            place_wav(wav, placed, pr.plan.pieces)
            os.replace(placed, wav)
            wav_seconds = duration(wav)
            a = on_song_clock(a_copy, pr.plan.pieces)
        rec["timeline"] = timeline_record(c, pr, a, s)
        rec["score_seconds"] = round(a.end_time, 2)
    bursts = nan_bursts(nan_blocks(wav))
    if bursts:
        raise AccompanimentError(
            f"the render has NaN samples (a Muse Sounds instrument's bug; the whole mix is lost there) in "
            f"{len(bursts)} burst(s) at " + ", ".join(f"{_mmss(x)}" for x, _y in bursts[:12])
            + "; find the instrument by relabelling suspects to a sound without a Muse Sounds instrument in a "
              "test copy, then relabel it in " + OPTIONS_NAME)

    # 4. probe
    log(f"  probing the instruments ...")
    rec["probe"] = run_probe(s, c, a, plan, o, work, relabelled=True)
    if o.relabel:
        before = run_probe(s, c, a, plan, o, work, relabelled=False,
                           parts={r.part for r in o.relabel})
        rec["probe_before_relabel"] = before
    rec["fallback"], rec["silent_with_muse"] = summarize_probe(rec["probe"])
    rec["had_fallback"] = bool(rec["fallback"]) or not rec["probe"]

    # 5. checks
    log(f"  checking ...")
    window = block_seconds(0.1)
    rms, peak = window_levels([wav], window)
    rec["checks"] = listen_checks(a, plan, rms, window)
    rec["checks"]["render_peak_dbfs"] = round(max(peak, default=-200), 2)
    last_sound = (max((i for i, v in enumerate(rms) if v > -70), default=0) + 1) * window
    rec["checks"]["last_sound_s"] = round(last_sound, 1)
    # MuseScore's export can run on in digital silence for minutes after the music: cut after the last sound
    # (-110 dBFS: the tail stays), never before the end of the score; the start is never moved
    last_any = (max((i for i, v in enumerate(rms) if v > -110), default=0) + 1) * window
    trim = min(wav_seconds, max(last_any + 0.5, a.end_time + 1.0))
    rec["trimmed_from_s"] = round(wav_seconds, 2) if trim < wav_seconds - 0.05 else None
    onsets_path = os.path.join(work, "onsets.json")
    with open(onsets_path, "w", encoding="utf-8") as f:
        json.dump({"times": onset_times(a, plan), "score_seconds": a.end_time}, f)

    # 6. loudness and encoding
    log(f"  measuring loudness and encoding ...")
    before = loudness(wav)
    gain = min(s.lufs - before["lufs"], s.true_peak - before["true_peak"])
    tmp_mp3 = os.path.join(work, "out.mp3")
    comment = f"Note Waterfall accompaniment of {c.rel}: MuseScore {tools['mscore'].split()[0]} + Muse Sounds"
    after: Dict[str, float] = {}
    for _attempt in range(3):
        encode_mp3(wav, tmp_mp3, gain, s.bitrate, a.title, a.artist, comment, end=trim)
        after = loudness(tmp_mp3)
        if after["true_peak"] <= s.true_peak + 0.05:
            break
        gain -= after["true_peak"] - s.true_peak + 0.1
    rec["loudness"] = {"before": before, "gain_db": round(gain, 2), "after": after,
                       "capped_by_true_peak": gain < s.lufs - before["lufs"] - 0.01}
    rec["mp3_seconds"] = round(duration(tmp_mp3), 2)
    rec["lame"] = lame_tag(tmp_mp3)

    # timing (optional, another Python)
    rec["timing"] = rec["timing_reference"] = None
    if s.timing_python:
        log(f"  timing check ...")
        rec["timing"] = run_timing(s, wav, onsets_path, tmp_mp3, gain, work)
        if not s.keep_wav and os.path.exists(wav):
            os.unlink(wav)
        rec["timing_reference"] = timing_reference(s, c, a_copy, plan, o, work, copy_src)

    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(tmp_mp3, mp3 + ".tmp")
    os.replace(mp3 + ".tmp", mp3)
    rec["mp3_sha256"] = _sha256_file(mp3)
    rec["key"] = key
    if not s.keep_wav:
        _cleanup(work)
    return "rendered", rec


# ------------------------------------------------------------------------------
# songs on a piano solo's clock (pianovision.timeline)
# ------------------------------------------------------------------------------

def timeline_key(c: Candidate) -> Optional[dict]:
    """What the clock of a song with a partner score depends on (the cache key): both scores, the partner's knobs and
    options, the measure map and the revisions of the alignment and the clock; None without a partner."""
    p = c.partner
    if not p:
        return None
    from .arrangement import ARRANGEMENT_REVISION
    from .timeline import TIMELINE_REVISION
    return {"kind": p["kind"], "song": _sha256_file(c.path), "partner": p.get("rel"),
            "partner_sha": _sha256_file(p["path"]), "compat": p.get("compat"), "options": p.get("options"),
            "song_compat": _verify_compat(c.entry), "measure_map": p.get("measure_map", "auto"),
            "revision": [ARRANGEMENT_REVISION, TIMELINE_REVISION]}


def song_pair(c: Candidate):
    """The song's two scores aligned and the orchestral one rendered on the solo's clock
    (:func:`pianovision.timeline.pair`)."""
    from .buildopts import without_partner
    from .timeline import SONG_ORCHESTRA, SONG_SOLO, pair
    p = c.partner
    own = without_partner(c.options)
    if p["kind"] == "orchestra":
        return pair(p["path"], p.get("compat"), p.get("options"), c.path, _verify_compat(c.entry), own, SONG_SOLO,
                    p.get("measure_map", "auto"))
    return pair(c.path, _verify_compat(c.entry), own, p["path"], p.get("compat"), p.get("options"), SONG_ORCHESTRA,
                p.get("measure_map", "auto"))


def _song_title(c: Candidate, a: Analysis) -> Tuple[str, str]:
    e = c.entry
    return e.get("title") or a.title, e.get("artist") or a.artist


def place_wav(src: str, dst: str, pieces) -> None:
    """The render ``src`` cut into ``pieces`` (:class:`pianovision.timeline.Piece`: render ``start``..``end`` at the
    song's ``at``), each placed at its time, silence between (10 ms fades where the render is cut), as ``dst``
    (32-bit float WAV)."""
    sr = MP3_SAMPLE_RATE
    chains = []
    for i, pc in enumerate(pieces):
        last = i == len(pieces) - 1
        s0 = max(0, int(round(pc.start * sr)))
        f = f"[0:a]atrim=start_sample={s0}" + ("" if last else f":end_sample={int(round(pc.end * sr))}")
        f += ",asetpts=PTS-STARTPTS"
        if s0 > 0:
            f += ",afade=t=in:d=0.01"
        if not last:
            f += f",afade=t=out:st={max(pc.end - pc.start - 0.01, 0):.6f}:d=0.01"
        f += f",adelay=delays={int(round(pc.at * sr))}S:all=1[p{i}]"
        chains.append(f)
    if len(pieces) > 1:
        chains.append("".join(f"[p{i}]" for i in range(len(pieces)))
                      + f"amix=inputs={len(pieces)}:normalize=0:duration=longest[out]")
        out = "[out]"
    else:
        out = "[p0]"
    _ffmpeg(["-y", "-i", src, "-filter_complex", ";".join(chains), "-map", out, "-c:a", "pcm_f32le", "-ar", str(sr),
             dst])


def on_song_clock(a: Analysis, pieces) -> Analysis:
    """The analysis of the edited orchestral score's render with its times on the song's clock (notes in a cut
    left out)."""
    from .timeline import song_time
    staves = []
    for st in a.staves:
        spans = []
        for on, off in st.spans:
            t = song_time(pieces, on)
            if t is not None:
                spans.append((t, t + (off - on)))
        attacks = [(song_time(pieces, t), p) for t, p in st.attacks if song_time(pieces, t) is not None]
        staves.append(dataclasses.replace(st, spans=spans, attacks=attacks))
    end = song_time(pieces, a.end_time)
    return dataclasses.replace(a, staves=staves, end_time=end if end is not None else a.end_time)


def song_clock_check(a: Analysis, parts_doc: Optional[dict], tolerance: float = 0.05) -> Optional[dict]:
    """The song's orchestra (its parts file: what Note Waterfall shows) against the notes of the score MuseScore
    renders, on the song's clock: every orchestra note matched to the nearest attack of the same pitch (within
    ``tolerance``); the differences in ms."""
    if not parts_doc:
        return None
    by_pitch: Dict[int, List[float]] = {}
    for st in a.staves:
        for t, p in st.attacks:
            by_pitch.setdefault(p, []).append(t)
    for v in by_pitch.values():
        v.sort()
    diffs, unmatched = [], 0
    for hand in (parts_doc.get("parts", {}).get("orchestra") or {}).values():
        for n in hand:
            ts = by_pitch.get(n["note"], [])
            i = bisect.bisect_left(ts, n["start"])
            c = [ts[k] for k in (i - 1, i) if 0 <= k < len(ts)]
            if not c or min(abs(x - n["start"]) for x in c) > tolerance:
                unmatched += 1
                continue
            diffs.append(min(c, key=lambda x: abs(x - n["start"])) - n["start"])
    if not diffs:
        return {"notes": unmatched, "matched": 0}
    ab = sorted(abs(d) * 1000 for d in diffs)
    return {"notes": len(diffs) + unmatched, "matched": len(diffs), "median_ms": round(statistics.median(ab), 2),
            "p95_ms": round(ab[int(0.95 * (len(ab) - 1))], 2), "max_ms": round(ab[-1], 2),
            "over_5ms": sum(1 for x in ab if x > 5.0)}


def timeline_record(c: Candidate, pr, a: Analysis, s: Settings) -> dict:
    """The report's record of the clock: the plan and the song-clock check against the song's parts file."""
    from .timeline import plan_report
    rec = {"kind": c.partner["kind"], "partner": c.partner.get("rel"), "plan": plan_report(pr.plan, pr)}
    if pr.plan.pieces:
        rec["pieces"] = [x.to_dict() for x in pr.plan.pieces]
    doc = None
    if c.parts_path and os.path.isfile(c.parts_path):
        with open(c.parts_path, encoding="utf-8") as f:
            doc = json.load(f)
    rec["song_clock"] = song_clock_check(a, doc)
    return rec


def _cleanup(work: str) -> None:
    """The big files of a score's work folder (the copies, logs and small files stay for a look)."""
    for n in ("render.wav", "out.mp3", "probe-muse.wav", "probe-basic.wav", "probe0-muse.wav", "probe0-basic.wav"):
        p = os.path.join(work, n)
        if os.path.exists(p):
            os.unlink(p)


def _content_hash(path: str) -> str:
    from .convert import content_hash
    return content_hash(path)


def run_probe(s: Settings, c: Candidate, a: Analysis, plan: MutePlan, o: ScoreOptions, work: str,
              relabelled: bool = True, parts: Optional[Set[str]] = None) -> List[dict]:
    relabels = o.relabel if relabelled else []
    slots = probe_slots(a, plan, relabels, parts=parts)
    if not slots:
        return []
    tag = "probe" if relabelled else "probe0"
    src = c.source_path or c.path
    with zipfile.ZipFile(src) as z:
        mscx = relabel_mscx(z.read(_main_mscx(z)), relabels, part_staves(a))
    pm = probe_mscx(mscx, a, slots)
    path = os.path.join(work, tag + ".mscz")
    write_copy(src, path, a, plan, o, drop_excerpts=True, mscx_override=pm, muted_override=set(),
               relabel=relabelled, dry=True)
    window = block_seconds(0.05)
    mw, bw = os.path.join(work, tag + "-muse.wav"), os.path.join(work, tag + "-basic.wav")
    render(s, path, mw, muse=True, log_copy=os.path.join(work, tag + "-muse.log"))
    render(s, path, bw, muse=False, log_copy=os.path.join(work, tag + "-basic.log"))
    m, _ = window_levels([mw], window)
    b, _ = window_levels([bw], window)
    d, _ = window_levels([mw, bw], window, expr="diff")
    out = probe_verdicts(slots, m, b, d, window)
    mf, _ = window_levels([mw], 0.005)
    bf, _ = window_levels([bw], 0.005)
    fine = block_seconds(0.005)
    for v, sl in zip(out, slots):
        v["name"] = a.parts[sl.part].name
        v["attack_ms"] = attack_ms(mf if v["verdict"] == "muse" else bf, sl.start, fine)
    return out


def attack_ms(levels: Sequence[float], start: float, window: float, drop_db: float = 20.0) -> Optional[float]:
    """When a probe note gets loud: the first block (from 50 ms before the note) within ``drop_db`` of the note's
    loudest block, in ms after the note's time; None for a silent note."""
    i0 = max(0, int(round((start - 0.05) / window)))
    i1 = min(len(levels), int(round((start + PROBE_MEASURE_S - 0.1) / window)))
    seg = levels[i0:i1]
    if not seg or max(seg) < -90:
        return None
    top = max(seg)
    for k, v in enumerate(seg):
        if v >= top - drop_db:
            return round(((i0 + k) * window - start) * 1000, 1)
    return None


def timing_reference(s: Settings, c: Candidate, a: Analysis, plan: MutePlan, o: ScoreOptions, work: str,
                     src: Optional[str] = None) -> Optional[dict]:
    """MuseScore's own timeline, apart from how the accompaniment's samples begin: the score's piano (the
    formatter's 'piano' staves of a keyboard instrument; everything else muted) rendered with the basic sounds,
    whose attacks are quick, against its notes.  None when the piano part is not a keyboard.  ``src``: the score
    file rendered (the copy on a solo's clock), ``a`` its analysis."""
    piano = {st.part for st in a.staves if st.kind == KIND_PIANO and st.notes
             and any(mx.startswith("keyboard.") for _i, mx in a.parts[st.part].instruments)}
    if not piano:
        return None
    ref_plan = dataclasses.replace(plan, muted={pid: ["timing reference"] for pid in a.parts if pid not in piano},
                                   audible_staves=[st.idx for st in a.staves if st.part in piano and st.notes])
    path, wav = os.path.join(work, "reference.mscz"), os.path.join(work, "reference.wav")
    write_copy(src or c.source_path or c.path, path, a, ref_plan, o, relabel=False)
    render(s, path, wav, muse=False, log_copy=os.path.join(work, "reference.log"))
    onsets = os.path.join(work, "reference-onsets.json")
    with open(onsets, "w", encoding="utf-8") as f:
        json.dump({"times": onset_times(a, ref_plan)}, f)
    res = run_timing(s, wav, onsets, None, 0.0, work, name="reference-timing.json")
    if not s.keep_wav and os.path.exists(wav):
        os.unlink(wav)
    if res is not None:
        res["staves"] = [st.idx + 1 for st in a.staves if st.part in piano and st.notes]
    return res


def run_timing(s: Settings, wav: str, onsets: str, mp3: Optional[str], gain: float, work: str,
               name: str = "timing.json") -> Optional[dict]:
    py = os.path.expanduser(s.timing_python)
    if not os.path.isfile(py) or not os.path.isfile(s.timing_script):
        return {"error": f"timing check not run: {py} or {s.timing_script} missing"}
    out = os.path.join(work, name)
    r = _run([py, s.timing_script, "--wav", wav, "--onsets", onsets, "--out", out]
             + (["--mp3", mp3, "--gain-db", f"{gain:.3f}"] if mp3 else []), timeout=s.timeout)
    if r.returncode != 0 or not os.path.isfile(out):
        err = ((r.stderr or b"") + (r.stdout or b"")).decode("utf-8", "replace")
        return {"error": f"timing check failed: {err[-800:]}"}
    with open(out, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------------------
# the report
# ------------------------------------------------------------------------------

def _mmss(t: Optional[float]) -> str:
    if t is None:
        return "?"
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _f(v: Optional[float], fmt: str = "+.1f") -> str:
    return "?" if v is None else format(v, fmt)


def report_markdown(state: dict, settings: Settings) -> str:
    L: List[str] = []
    scores = sorted(state.get("scores", {}).items(), key=lambda kv: kv[1].get("mp3", ""))
    L.append("# Note Waterfall accompaniments")
    L.append("")
    L.append(f"Rendered by `python3 -m pianovision accompaniment` (MuseScore with Muse Sounds); updated {_now()}. "
             f"Loudness target {settings.lufs:g} LUFS integrated with the true peak at most {settings.true_peak:g} "
             f"dBTP (a linear gain, no compression); mp3 {settings.bitrate} kbit/s CBR. Timing: + = the audio is "
             f"later than the score's notes.")
    L.append("")
    L.append("| mp3 | length (score) | muted | basic sounds | timing median (p5..p95) | loudness |")
    L.append("|---|---|---|---|---|---|")
    for rel, r in scores:
        parts = r.get("parts") or {}
        muted = ", ".join(parts.get(p, {}).get("name", p) for p in (r.get("plan") or {}).get("muted", {}))
        fb = ", ".join(sorted({v["name"] for v in r.get("fallback") or []}))
        x = ((r.get("timing") or {}).get("xcorr") or {})
        tm = (f"{_f(x.get('median_ms'))} ms ({_f(x.get('p5_ms'))}..{_f(x.get('p95_ms'))})"
              if x.get("median_ms") is not None else "-")
        ld = (r.get("loudness") or {}).get("after") or {}
        lo = f"{ld['lufs']:.1f} LUFS, {ld['true_peak']:.1f} dBTP" if ld else "-"
        err = " **(last run failed)**" if r.get("error") else ""
        L.append(f"| {r.get('mp3', rel)}{err} | {_mmss(r.get('mp3_seconds'))} ({_mmss(r.get('score_seconds'))}) | "
                 f"{muted or '-'} | {fb or '-'} | {tm} | {lo} |")
    L.append("")
    retired = state.get("retired") or {}
    if retired:
        L.append("No longer songs of the library (their mp3 is left in the folder; nothing renders it again): "
                 + ", ".join(f"{v.get('mp3')} ({k})" for k, v in sorted(retired.items())))
        L.append("")
    for rel, r in scores:
        L.append(f"## {r.get('mp3', rel)}")
        L.append("")
        L.append(f"*{rel}*: {r.get('title', '')} / {r.get('artist', '')}; rendered {r.get('rendered')}")
        L.append("")
        if r.get("error"):
            L.append(f"- **Last run failed** ({r.get('error_time')}): {r['error']}")
        if not r.get("mp3_sha256"):
            L.append("")
            continue
        tr = r.get("trimmed_from_s")
        L.append(f"- **Duration:** mp3 {_mmss(r.get('mp3_seconds'))}, score {_mmss(r.get('score_seconds'))} "
                 f"(its last note-off; the mp3 keeps the release and reverb tail; last sound above -70 dBFS at "
                 f"{_mmss((r.get('checks') or {}).get('last_sound_s'))})"
                 + (f". MuseScore's render ran to {_mmss(tr)}; the digital silence after the music was cut" if tr
                    else ""))
        tl = r.get("timeline")
        if tl:
            pl = tl.get("plan") or {}
            how = ("the orchestra of " + r.get("source", "?") if tl.get("kind") == "orchestra"
                   else "the orchestral score itself")
            line = (f"- **Clock:** the piano solo's ({tl.get('partner') if tl.get('kind') == 'simplified' else rel}): "
                    f"{how}, rendered with {pl.get('marks')} tempo marks written in (its own tempo marks, fermatas "
                    f"and pauses off), {pl.get('seconds')} s (its own timing {pl.get('own_seconds')} s); positions "
                    f"within {pl.get('max_inside_ms')} ms of the solo's clock")
            if tl.get("pieces"):
                line += "; cut and placed: " + ", ".join(
                    f"render {x['render'][0]:.2f}-{x['render'][1]:.2f} s at {x['at']:.2f} s" for x in tl["pieces"])
            sc = tl.get("song_clock")
            if sc and sc.get("matched"):
                line += (f". The song's orchestra (parts file) against the rendered score's notes: {sc['matched']} of "
                         f"{sc['notes']} notes matched, median {sc['median_ms']} ms, p95 {sc['p95_ms']} ms, max "
                         f"{sc['max_ms']} ms ({sc['over_5ms']} over 5 ms)")
            L.append(line)
        rd = r.get("render") or {}
        if rd:
            L.append(f"- **MuseScore log:** Muse Sounds engine {rd.get('muse_engine') or 'NOT LOADED'}; "
                     f"{rd.get('muse_tracks')} track(s) on Muse Sounds, {rd.get('fallback_tracks')} on the basic "
                     f"sounds (muted ones and the metronome included); rendered in {rd.get('seconds')} s")
        plan = r.get("plan") or {}
        parts = r.get("parts") or {}
        muted = [f"{parts.get(p, {}).get('name', p)} (part {p}, staff "
                 f"{', '.join(map(str, parts.get(p, {}).get('staves', [])))}: {'; '.join(w)})"
                 for p, w in plan.get("muted", {}).items()]
        L.append("- **Muted:** " + ("; ".join(muted) if muted else "nothing"))
        for p, w in (plan.get("kept") or {}).items():
            L.append(f"  - plays by play_parts: {parts.get(p, {}).get('name', p)} (would be muted: {w})")
        for cf in plan.get("conflicts") or []:
            L.append(f"  - conflict: {cf}")
        rl = r.get("relabels") or []
        if rl:
            def first(probe):
                return {v["part"]: v["verdict"] for v in reversed(probe or []) if v["role"] in ("median", "drum")}
            before, after = first(r.get("probe_before_relabel")), first(r.get("probe"))
            L.append("- **Relabelled:**")
            for x in rl:
                L.append(f"  - part {x['part']} ({parts.get(x['part'], {}).get('name', '')}) {x['from']} → "
                         f"{x['to']}: sound before {before.get(x['part'], '?')}, after {after.get(x['part'], '?')}; "
                         f"MIDI {'–'.join(map(str, x.get('pitches', [])))}; {x.get('why', '')}")
        else:
            L.append("- **Relabelled:** nothing")
        for x in (r.get("options") or {}).get("techniques") or []:
            L.append(f"  - part {x['part']} ({parts.get(x['part'], {}).get('name', '')}) starts {x['technique']} "
                     f"(\"{x.get('text') or x['technique']}\"): {x.get('why', '')}")
        fb = r.get("fallback") or []
        L.append("- **Basic sounds (no Muse Sounds instrument):** "
                 + (", ".join(f"{v['name']} [{v['instrument']}]" + (" (silent)" if v["verdict"] == "silent" else "")
                              for v in fb) if fb else "none"))
        sm = r.get("silent_with_muse") or []
        if sm:
            L.append("- **Silent with Muse Sounds (probe):** "
                     + "; ".join(f"{v['name']} [{v['instrument']}] {v['role']} note MIDI {v['pitch']} "
                                 f"({v.get('notes', '?')} note{'s' if v.get('notes') != 1 else ''})" for v in sm))
        att = [v["attack_ms"] for v in r.get("probe") or [] if v.get("attack_ms") is not None
               and v["role"] in ("median", "drum") and v["verdict"] == "muse"]
        attb = [v["attack_ms"] for v in r.get("probe") or [] if v.get("attack_ms") is not None
                and v["role"] in ("median", "drum") and v["verdict"] == "basic"]
        if att or attb:
            L.append("- **Probe attacks** (a single note reaches 20 dB below its loudest, after its time): "
                     + ", ".join(f"{k} median {statistics.median(x):.0f} ms ({min(x):.0f} to {max(x):.0f})"
                                 for k, x in (("Muse Sounds", att), ("basic", attb)) if x))
        t = r.get("timing")
        if t and not t.get("error"):
            x, mt = t["xcorr"], t.get("matched") or {}
            if x.get("median_ms") is None:
                L.append(f"- **Timing:** no window correlated well enough ({x['windows_total']} windows)")
            else:
                L.append(f"- **Timing:** median {_f(x['median_ms'])} ms, p5 {_f(x['p5_ms'])}, p95 {_f(x['p95_ms'])} "
                         f"({x['windows']} of {x['windows_total']} 20 s windows), drift "
                         f"{_f(x['drift_ms_per_min'], '+.2f')} ms/min (quarters: "
                         f"{', '.join(_f(q, '+.0f') for q in x.get('quarters_median_ms', []))} ms); onset matching "
                         f"median {_f(mt.get('median_ms'))} ms ({mt.get('matched')}/{mt.get('onsets')} onsets); "
                         f"the method measures {_f(t.get('method_bias_ms'))} ms on clicks")
        elif t:
            L.append(f"- **Timing:** {t.get('error')}")
        else:
            L.append("- **Timing:** not checked (no timing_python)")
        tr_ = r.get("timing_reference")
        if tr_ and not tr_.get("error") and (tr_.get("xcorr") or {}).get("median_ms") is not None:
            x = tr_["xcorr"]
            L.append(f"  - MuseScore's timeline (the piano part, staves {', '.join(map(str, tr_.get('staves', [])))}, "
                     f"rendered with the basic sounds against its own notes): median {_f(x['median_ms'])} ms, "
                     f"p5 {_f(x['p5_ms'])}, p95 {_f(x['p95_ms'])} ({x['windows']}/{x['windows_total']} windows), "
                     f"drift {_f(x['drift_ms_per_min'], '+.2f')} ms/min; method {_f(tr_.get('method_bias_ms'))} ms "
                     f"on clicks")
        mp = (t or {}).get("mp3") if t and not t.get("error") else None
        if mp:
            parts_ = [f"{k.replace('_', ' ')} {v['samples']:+d} samples" for k, v in mp.items() if "samples" in v]
            L.append("- **mp3 alignment** against the WAV (+ = later): " + ", ".join(parts_)
                     + ("; Xing/LAME header: " + ("yes" if (r.get("lame") or {}).get("info_frame") else "none")))
        ld = r.get("loudness") or {}
        if ld:
            b, a_ = ld["before"], ld["after"]
            L.append(f"- **Loudness:** render {b['lufs']:.1f} LUFS, {b['true_peak']:.1f} dBTP, LRA {b['lra']:.1f} LU; "
                     f"gain {ld['gain_db']:+.1f} dB → {a_['lufs']:.1f} LUFS, {a_['true_peak']:.1f} dBTP, LRA "
                     f"{a_['lra']:.1f} LU" + (" (the gain is capped by the true peak)"
                                              if ld.get("capped_by_true_peak") else ""))
        ck = r.get("checks") or {}
        notes = []
        if ck.get("silent_gaps"):
            notes.append("silent where the score has notes: " + ", ".join(
                f"{_mmss(g['from'])}–{_mmss(g['to'])}" for g in ck["silent_gaps"][:8])
                + (f" (+{len(ck['silent_gaps']) - 8} more)" if len(ck["silent_gaps"]) > 8 else ""))
        else:
            notes.append("no silence where the score has notes")
        if ck.get("piano_only_stretches"):
            n = ck["piano_only_stretches"]
            notes.append(f"{n} stretch{'es' if n != 1 else ''} where only the muted staves play "
                         f"({ck['piano_only_seconds']} s), loudest {ck['piano_only_max_db']} dBFS RMS"
                         + ("; LOUD, piano bleeding through?: " + ", ".join(
                             f"{_mmss(b['from'])}–{_mmss(b['to'])} {b['max_rms_db']} dB"
                             for b in ck["bleed_suspects"][:6]) if ck.get("bleed_suspects") else ""))
        notes.append(f"render peak {ck.get('render_peak_dbfs')} dBFS (32-bit float WAV, before the gain)")
        L.append("- **Checks:** " + "; ".join(notes))
        for w in r.get("warnings") or []:
            L.append(f"- **Warning:** {w}")
        L.append("")
    return "\n".join(L) + "\n"


def listing(lib, s: Settings, only: Sequence[str] = (), log: Log = print) -> int:
    """``--list``: every accompaniment score's instruments, how the formatter classifies their staves and what the
    render would mute and relabel (nothing is rendered)."""
    with open(os.path.join(lib.out, ".manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    defaults, per_score = load_options(s.options)
    for c in candidates(manifest, lib.cfg.scores, only, lib):
        o = score_options(defaults, per_score.get(c.source_rel or c.rel))
        a = analyse(c.source_path or c.path, c.source_compat, c.source_options)
        plan = plan_mutes(a, o)
        ren = renames_for(o.relabel)
        log(f"{c.rel} -> {c.stem}.mp3  ({_mmss(a.end_time)}"
            + (f"; the orchestra of {c.source_rel}" if c.source_rel and c.source_rel != c.rel else "")
            + ("; on the piano solo's clock" if c.partner else "") + ")")
        for pid, p in a.parts.items():
            kinds = sorted({a.staves[i].kind for i in p.staves if a.staves[i].notes} or {"empty"})
            ins = ", ".join(iid + (f" -> {ren[(pid, iid)]}" if (pid, iid) in ren else "") for iid, _mx in p.instruments)
            state = ("MUTED: " + "; ".join(plan.muted[pid])) if pid in plan.muted else (
                "plays" + (f" (play_parts; would be muted: {plan.kept[pid]})" if pid in plan.kept else ""))
            log(f"  part {pid:>3}  {p.name[:34]:34}  [{ins}]  staves {','.join(str(i + 1) for i in p.staves)}  "
                f"{a.part_notes(pid)} notes  {'/'.join(kinds)}  {state}")
        for t in o.techniques:
            log(f"  part {t.part:>3}  starts {t.technique} ({t.text or t.technique})")
        for cf in plan.conflicts:
            log(f"  conflict: {cf}")
    return 0


def run(lib, s: Settings, only: Sequence[str] = (), force: bool = False, dry_run: bool = False,
        log: Log = print) -> int:
    """Render the accompaniment of every score that has one (``only``: globs).  Returns the number of failures."""
    if not dry_run:
        for tool in ("ffmpeg", "ffprobe", "nice"):
            if shutil.which(tool) is None:
                raise AccompanimentError(f"{tool} not found")
        if not os.path.isfile(s.path("mscore")):
            raise AccompanimentError(f"MuseScore not found: {s.path('mscore')}")
        if not os.path.isfile(os.path.join(s.path("musesampler"), "lib", "libMuseSamplerCoreLib.so")):
            raise AccompanimentError(f"the Muse Sounds engine is not installed in {s.path('musesampler')}")
    out_dir = s.path("out_dir")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(s.path("work_dir"), exist_ok=True)
    with open(os.path.join(lib.out, ".manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    defaults, per_score = load_options(s.options)
    cands = candidates(manifest, lib.cfg.scores, only, lib)
    sources = {c.source_rel for c in candidates(manifest, lib.cfg.scores, (), lib)}
    unknown = sorted(set(per_score) - {r for r in manifest.get("entries", {})} - sources)
    for u in unknown:
        log(f"warning: {OPTIONS_NAME} has options for {u!r}, which is not in the library")
    log(f"{len(cands)} score(s) with an accompaniment" + (f" matching {', '.join(only)}" if only else ""))
    state = load_state(out_dir)
    state.setdefault("scores", {})
    # songs that left the library (retired, or no longer songs of their own): their mp3 is left where it is
    for r in sorted(r for r in state["scores"] if r not in manifest.get("entries", {})):
        old = state["scores"].pop(r)
        state.setdefault("retired", {})[r] = {"mp3": old.get("mp3"), "song": old.get("song"), "date": _now()}
        log(f"{r}: no longer a song of the library; {old.get('mp3')} is left in {out_dir}")
    failures = 0
    for c in cands:
        log(f"{c.rel} -> {c.stem}.mp3")
        t = time.monotonic()
        try:
            status, rec = process(c, s, defaults, per_score, state, force, dry_run, log)
        except Exception as e:      # one failed score does not stop the others
            failures += 1
            log(f"  FAILED: {type(e).__name__}: {e}")
            if not s.keep_wav:
                _cleanup(os.path.join(s.path("work_dir"), c.stem))
            prev = state["scores"].get(c.rel) or {}
            prev["error"] = f"{type(e).__name__}: {e}"
            prev["error_time"] = _now()
            state["scores"][c.rel] = prev
            if not dry_run:
                save_state(out_dir, state)
            continue
        log(f"  {status} ({time.monotonic() - t:.0f}s)")
        if status == "rendered":
            state["scores"][c.rel] = rec
            save_state(out_dir, state)
    if not dry_run:
        state["updated"] = _now()
        save_state(out_dir, state)
        with open(os.path.join(out_dir, REPORT_NAME + ".md"), "w", encoding="utf-8") as f:
            f.write(report_markdown(state, s))
    return failures
