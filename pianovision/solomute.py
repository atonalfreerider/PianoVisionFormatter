"""A concerto's recording as its companion audio with the piano's solo passages muted (``recordings mute-solos``).

The user plays the solo part.  Where the orchestra plays, the recording's own piano may blend in; where the piano
plays alone (a cadenza, a solo episode) the recording would play the user's part for them, so those passages are
muted in a copy of the recording.  Nothing is moved or stretched: the score follows the recording (``recordings
tempo``; a song whose piano part comes from a partner solo keeps that clock with ``clock: "own"`` in scores.json),
and the copy plays at the same offset as the recording.

**The passages** (:func:`solo_spans`): from the song's parts file, the stretches of the song's clock where no
orchestra note sounds (each from its start to its end) and the piano part plays, at least ``min_seconds`` long;
shorter ones (a fill between two phrases) are left as they are.

**The gain** (:func:`gain_expr`): on the recording's clock (song time + the offset Note Waterfall plays the audio at),
1 outside the passages and 0 inside, with a fade out of ``fade_out`` seconds from where the passage starts (the
orchestra's last note ends: its release and the hall die away in the fade) and a fade in of ``fade_in`` seconds
ending ``lead`` seconds before the orchestra comes back.

**The copy** (:func:`write`): the recording times the gain (ffmpeg's ``volume`` evaluated every 32 samples), encoded
as the generated companions are (:func:`pianovision.accompaniment.encode_args`: constant bit rate, the codec delay cut,
no Xing/LAME header, so every decoder lines it up at sample 0 of the recording as ffmpeg decodes it), with
``<song>.solo-mute.json`` next to it: the passages on both clocks, the settings and the source.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

MIN_SECONDS = 2.0       # a piano-alone stretch shorter than this is not muted
FADE_OUT = 0.35         # s from the orchestra's last note-off to silence
FADE_IN = 0.12          # s from silence back to full level ...
LEAD = 0.05             # ... ending this long before the orchestra's next onset
FRAME = 32              # samples per gain step


@dataclass
class Span:
    start: float            # song seconds: the orchestra's last note ends
    end: float              # song seconds: the orchestra's next note starts (or the song's end)
    notes: int              # piano notes starting inside

    def to_dict(self, offset: float = 0.0) -> Dict[str, Any]:
        return {"song": [round(self.start, 3), round(self.end, 3)],
                "recording": [round(self.start + offset, 3), round(self.end + offset, 3)],
                "seconds": round(self.end - self.start, 3), "piano_notes": self.notes}


def _notes(doc: dict, part: str) -> List[dict]:
    return [n for hand in ((doc.get("parts") or {}).get(part) or {}).values() for n in hand]


def solo_spans(doc: dict, min_seconds: float = MIN_SECONDS, piano: str = "original") -> List[Span]:
    """The piano-alone passages of a parts file (:mod:`pianovision.parts`), in song seconds, in order."""
    orch = sorted((n["start"], n["start"] + n["duration"]) for n in _notes(doc, "orchestra"))
    keys = sorted((n["start"], n["start"] + n["duration"]) for n in _notes(doc, piano))
    if not keys:
        return []
    song_end = max(e for _s, e in keys)
    gaps, sounding = [], 0.0
    for s, e in orch:
        if s > sounding:
            gaps.append((sounding, s))
        sounding = max(sounding, e)
    if song_end > sounding:
        gaps.append((sounding, song_end))
    out = []
    for a, b in gaps:
        n = sum(1 for s, _e in keys if a <= s < b)
        if n and b - a >= min_seconds:
            out.append(Span(a, b, n))
    return out


def gain_breakpoints(spans: Sequence[Span], offset: float, fade_out: float = FADE_OUT, fade_in: float = FADE_IN,
                     lead: float = LEAD) -> List[tuple]:
    """Per passage (recording seconds): (fade out from, silent from, fade in from, full again at)."""
    out = []
    for sp in spans:
        a, b = sp.start + offset, sp.end + offset
        full = b - lead
        c = full - fade_in
        fo = min(fade_out, max(0.0, (c - a) / 2))     # a short passage: the fade out takes half of it at most
        out.append((a, a + fo, c, full))
    return out


def gain_expr(points: Sequence[tuple]) -> str:
    """ffmpeg expression of ``t`` (seconds): 1 - the sum over the passages of (fade-out ramp - fade-in ramp)."""
    terms = []
    for a, s, c, full in points:
        fo, fi = max(s - a, 1e-3), max(full - c, 1e-3)
        terms.append(f"clip((t-{a:.4f})/{fo:.4f},0,1)-clip((t-{c:.4f})/{fi:.4f},0,1)")
    return "1" if not terms else "max(0,1-(" + "+".join(terms) + "))"


def gain_at(points: Sequence[tuple], t: float) -> float:
    """The gain :func:`gain_expr` gives at ``t`` (for the report and the tests)."""
    g = 1.0
    for a, s, c, full in points:
        fo, fi = max(s - a, 1e-3), max(full - c, 1e-3)
        g -= min(1.0, max(0.0, (t - a) / fo)) - min(1.0, max(0.0, (t - c) / fi))
    return max(0.0, g)


def ffmpeg_args(src: str, mp3: str, points: Sequence[tuple], bitrate: int, title: str = "", artist: str = "",
                comment: str = "") -> List[str]:
    """ffmpeg arguments: ``src`` times the gain, encoded like a generated companion."""
    from .accompaniment import MP3_CODEC_DELAY, MP3_SAMPLE_RATE
    af = (f"aresample={MP3_SAMPLE_RATE},asetnsamples=n={FRAME}:p=0,"
          f"volume='{gain_expr(points)}':eval=frame:precision=float,"
          f"atrim=start_sample={MP3_CODEC_DELAY},asetpts=PTS-STARTPTS")
    args = ["-y", "-i", src, "-af", af, "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-ar", str(MP3_SAMPLE_RATE),
            "-ac", "2", "-map_metadata", "-1", "-id3v2_version", "3", "-write_xing", "0"]
    for k, v in (("title", title), ("artist", artist), ("comment", comment)):
        if v:
            args += ["-metadata", f"{k}={v}"]
    return args + [mp3]


def write(src: str, mp3: str, doc: dict, offset: float, bitrate: int = 320, min_seconds: float = MIN_SECONDS,
          title: str = "", artist: str = "", comment: str = "", log=print) -> Dict[str, Any]:
    """The muted copy of ``src`` at ``mp3`` and its report (``<mp3 without .mp3>.solo-mute.json``)."""
    from .accompaniment import _ffmpeg, duration, window_levels, block_seconds
    spans = solo_spans(doc, min_seconds)
    points = gain_breakpoints(spans, offset)
    tmp = mp3 + ".tmp.mp3"
    _ffmpeg(ffmpeg_args(src, tmp, points, bitrate, title, artist, comment))
    # check: silence inside every passage (away from its fades), sound around them
    window = block_seconds(0.1)
    rms, _peak = window_levels([tmp], window)
    checks = []
    for (a, s, c, full), sp in zip(points, spans):
        inside = [v for i, v in enumerate(rms) if s + 0.05 <= i * window and (i + 1) * window <= c - 0.05]
        checks.append({"fades": [round(a, 3), round(s, 3), round(c, 3), round(full, 3)],
                       "inside_max_db": round(max(inside), 1) if inside else None})
    os.replace(tmp, mp3)
    total = sum(sp.end - sp.start for sp in spans)
    rep = {"format": 1, "source": src, "mp3": mp3, "offset": round(offset, 4), "min_seconds": min_seconds,
           "fade_out": FADE_OUT, "fade_in": FADE_IN, "lead": LEAD, "bitrate": bitrate,
           "song_seconds": round(max((sp.end for sp in spans), default=0.0), 3),
           "mp3_seconds": round(duration(mp3), 3), "muted_seconds": round(total, 1),
           "passages": [dict(sp.to_dict(offset), **ck) for sp, ck in zip(spans, checks)]}
    with open(os.path.splitext(mp3)[0] + ".solo-mute.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1)
    log(f"  {len(spans)} solo passage(s) muted, {total:.1f} s: " +
        ", ".join(f"{sp.start + offset:.1f}-{sp.end + offset:.1f}" for sp in spans))
    loud = [p for p in rep["passages"] if p["inside_max_db"] is not None and p["inside_max_db"] > -60]
    if loud:
        log(f"  WARNING: sound inside {len(loud)} muted passage(s): {loud}")
    return rep


def offset_of(state: Optional[dict], override: Optional[float]) -> float:
    """The offset the audio plays at: ``override``, else the song's ``recordings tempo`` state."""
    if override is not None:
        return float(override)
    if not state or "offset" not in state:
        raise ValueError("no offset: run `recordings tempo SONG` first, or give --offset")
    return float(state["offset"])
