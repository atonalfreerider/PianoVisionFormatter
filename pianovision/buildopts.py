"""Per-score options of ``build``: ``scores.json``, checked in next to ``pianovision.toml``.

Keys of ``"scores"`` are score paths relative to the scores folder (as ``accompaniment.json``'s); keys starting with
``_`` are comments::

    {"scores": {
       "Gershwin/Rhapsody_in_blue__George_Gershwin.mscz": {
         "title": "Rhapsody in Blue (Piano and Orchestra)",   # the song's title and composer: PianoVision's name and
         "composer": "Gershwin",                              #   artist, the output name (instead of the score's)
         "simplified_from": "Gershwin/Rhapsody_in_Blue_Piano_solo.mscz",   # a piano solo of the same music: its
         "measure_map": "auto",                               #   piano part the simplified part, its clock the song's
         "piano_from": "simplified_from"},                    # ... and the song's piano part (original part and
                                                              #   PianoVision's hands) where it plays the measure
       "Rachmaninoff/Rhapsody_on_a_theme_of_Paganini_Variation_18_Piano_Solo.mscz": {
         "orchestra_from": "Rachmaninoff/pagvar18.mscz"},     # an orchestral score of the same music: its orchestra
       "Rachmaninoff/pagvar18.mscz": {                        #   the song's orchestra, on this solo's clock
         "song": false,                                       # not a song of its own (only read as a partner)
         "piano_staves": [11, 12],                            # the piano part: these staves (1-based, right hand first)
         "hands": "split"}}}                                  # its notes to the hands by pitch and reach (a MIDI
                                                              #   import that wrote both hands on one staff)

``title`` / ``composer``: the song is named after them (``<first 4 letters of the composer>_<title>.json``, as for
any score) and PianoVision shows them; the score's own title frame and metadata are not read.  ``piano_staves``:
the staves that are the piano part, whatever their names and programs (every other staff is orchestra by its program
as usual); the first is the right hand, the others the left.  ``hands``: ``"staves"`` (default) or ``"split"``
(:mod:`pianovision.handsplit`).

**Partner scores** (:mod:`pianovision.timeline`): a piano solo and an orchestral score of the same piece, aligned
measure by measure (:mod:`pianovision.arrangement`; ``measure_map``: ``"auto"`` or overrides, measures given as
``[solo, orchestral]`` whichever score carries the option), make one song that plays on the solo's clock.  The
option goes on the score that is the song: ``simplified_from: <solo>`` on an orchestral score (its own piano part,
orchestra and measures; the solo's piano part is the simplified part), ``orchestra_from: <orchestral score>`` on a
solo (its own piano part and measures; the orchestral score's orchestra, its piano staves left out as that score's
options say).  The partner is rendered with its own options (piano staves, hands) and, when it is a song of its own,
its manifest knobs.  ``song: false``: the score is not a song of its own (``build`` renders nothing for it and
retires an output it made before); it can still be another score's partner.

``clock: "own"`` (with ``simplified_from`` only; default ``"solo"``): the song plays on the orchestral score's own
clock, its tempo marks, fermatas and pauses (a score whose tempo map follows a recording: ``recordings tempo``),
instead of the solo's; the solo's notes (the simplified part, and the piano part with ``piano_from``) are placed on
it through the same measure alignment.

``reduction: "orchestra"`` (with ``piano_from``): the solo's notes where the orchestral score's piano part rests and
its orchestra plays (the orchestra arranged for piano: a tutti the solo plays) are the song's orchestra, in the hand
the solo writes them, instead of its piano part; there they stand for the orchestra's instruments.

``piano_from: "simplified_from"`` (with ``simplified_from`` only): the solo is the source of truth for the piano part
too.  Wherever the alignment maps a solo measure, the song's piano part (Note Waterfall's ``original`` part and
PianoVision's hands) is the solo's notes on the song's clock, exactly as the simplified part has them; the orchestral
measures the solo leaves out keep the orchestral score's own piano part.  The song has no separate simplified part
then (it would be the same notes).  The solo's notes keep ``"simplified": 1`` in the original part (Note Waterfall
counts their hand-edit identity apart from the orchestral score's notes, as before); their hand edits are not
written back through this song (``hands review`` says so: record them on the solo's own song).

Options change what ``build`` makes of a score, so they are part of what the manifest records for it (``options``:
a hash of them, and the partner's content hash, knobs and options): a change renders the score again.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

OPTIONS_NAME = "scores.json"
KNOWN = ("title", "composer", "piano_staves", "hands", "simplified_from", "orchestra_from", "measure_map", "piano_from",
         "clock", "reduction", "song")
# what makes the song's own render (the manifest's "song" hash; the partner is hashed apart)
SONG_KEYS = ("title", "composer", "piano_staves", "hands", "piano_from", "clock", "reduction")
CLOCKS = ("solo", "own")              # clock: the solo's (default) or, with simplified_from, the orchestral score's own
PIANO_FROM = ("simplified_from",)     # piano_from: the partner whose piano part is the song's
PARTNER_KINDS = {"simplified_from": "simplified", "orchestra_from": "orchestra"}


class OptionsError(ValueError):
    pass


@dataclass
class BuildOptions:
    title: str = ""
    composer: str = ""
    piano_staves: List[int] = field(default_factory=list)   # 1-based
    hands: str = "staves"
    simplified_from: str = ""
    orchestra_from: str = ""
    measure_map: Any = "auto"
    piano_from: str = ""                                     # "simplified_from": the solo is the piano part too
    clock: str = "solo"                                      # "own": the orchestral score's own clock (simplified_from)
    reduction: str = ""                                      # "orchestra": the solo's orchestra reduction (piano_from)
    song: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)        # the entry as written (comments left out)

    @classmethod
    def from_dict(cls, rel: str, d: Dict[str, Any]) -> "BuildOptions":
        d = {k: v for k, v in (d or {}).items() if not k.startswith("_")}
        unknown = sorted(k for k in d if k not in KNOWN)
        if unknown:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: unknown option(s) {', '.join(unknown)} ({', '.join(KNOWN)})")
        song = d.get("song", True)
        if not isinstance(song, bool):
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: song must be true or false, not {song!r}")
        o = cls(title=str(d.get("title", "") or ""), composer=str(d.get("composer", "") or ""),
                piano_staves=[int(x) for x in d.get("piano_staves", []) or []], hands=str(d.get("hands", "staves")),
                simplified_from=str(d.get("simplified_from", "") or ""),
                orchestra_from=str(d.get("orchestra_from", "") or ""), measure_map=d.get("measure_map", "auto"),
                piano_from=str(d.get("piano_from", "") or ""), clock=str(d.get("clock", "solo") or "solo"),
                reduction=str(d.get("reduction", "") or ""),
                song=song, raw=d)
        if o.hands not in ("staves", "split"):
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: hands must be \"staves\" or \"split\", not {o.hands!r}")
        if o.hands == "split" and not o.piano_staves:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: hands \"split\" needs piano_staves")
        if any(x < 1 for x in o.piano_staves):
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: piano_staves are 1-based staff numbers")
        if o.simplified_from and o.orchestra_from:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: simplified_from and orchestra_from: a score has one partner")
        for key in PARTNER_KINDS:
            if getattr(o, key) == rel:
                raise OptionsError(f"{OPTIONS_NAME}: {rel}: {key} names the score itself")
        if not o.song and o.partner:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: song false and {o.partner_key}: a score that is not a song "
                               f"has no partner of its own")
        if o.piano_from:
            if o.piano_from not in PIANO_FROM:
                raise OptionsError(f"{OPTIONS_NAME}: {rel}: piano_from must be \"simplified_from\" (the piano solo the "
                                   f"simplified part comes from), not {o.piano_from!r}")
            if not getattr(o, o.piano_from):
                raise OptionsError(f"{OPTIONS_NAME}: {rel}: piano_from \"{o.piano_from}\" needs {o.piano_from} "
                                   f"(a piano solo of the same music)")
        if o.clock not in CLOCKS:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: clock must be \"solo\" or \"own\", not {o.clock!r}")
        if o.clock == "own" and not o.simplified_from:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: clock \"own\" needs simplified_from (an orchestral score "
                               f"that keeps its own clock while a piano solo gives its piano part)")
        if o.reduction not in ("", "orchestra"):
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: reduction must be \"orchestra\", not {o.reduction!r}")
        if o.reduction and not o.piano_from:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: reduction \"orchestra\" needs piano_from (the solo's notes are "
                               f"the piano part, but for its orchestra reduction)")
        return o

    @property
    def partner_key(self) -> str:
        return "simplified_from" if self.simplified_from else ("orchestra_from" if self.orchestra_from else "")

    @property
    def partner(self) -> str:
        """The partner score (scores.json path), or ""."""
        return self.simplified_from or self.orchestra_from

    @property
    def partner_kind(self) -> str:
        """What the partner gives the song: "simplified" (``simplified_from``), "orchestra" (``orchestra_from``)."""
        return PARTNER_KINDS.get(self.partner_key, "")

    def digest(self) -> str:
        """Hash of the options as written (comments left out)."""
        return hashlib.sha256(json.dumps(self.raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def own_options(self) -> dict:
        """The options that make this score's own render (title, composer, piano staves and hands)."""
        out: Dict[str, Any] = {}
        if self.title:
            out["title"] = self.title
        if self.composer:
            out["composer"] = self.composer
        if self.piano_staves:
            out["piano_staves"] = list(self.piano_staves)
            out["hands"] = self.hands
        return out

    def render_options(self, scores_root: str, partner_compat: Optional[dict] = None,
                       partner_options: Optional["BuildOptions"] = None) -> dict:
        """What :func:`pianovision.convert.convert_mscz` takes as ``options`` (plain data: it goes to worker
        processes); ``partner_compat`` / ``partner_options``: the partner's knobs and build options."""
        out = self.own_options()
        if self.partner:
            out["partner"] = {"kind": self.partner_kind, "path": os.path.join(scores_root, self.partner),
                              "rel": self.partner, "compat": partner_compat, "measure_map": self.measure_map,
                              "options": (partner_options.own_options() if partner_options else None) or None}
            if self.piano_from:
                # the solo is the song's piano part too (pianovision.arrangement.PianoFrom); the clock does not change
                out["partner"]["piano"] = True
            if self.reduction:
                out["partner"]["reduction"] = self.reduction
            if self.clock == "own":
                # the song keeps this score's own tempo map; the solo's notes are placed on it
                out["partner"]["clock"] = "own"
        return out


def load(path: Optional[str]) -> Dict[str, BuildOptions]:
    """``scores.json`` -> {score rel: options}; no file, no options."""
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "rb") as f:
        try:
            doc = json.load(f)
        except ValueError as e:
            raise OptionsError(f"{path}: {e}")
    scores = doc.get("scores", {}) if isinstance(doc, dict) else {}
    return {rel: BuildOptions.from_dict(rel, d) for rel, d in scores.items() if not rel.startswith("_")}


def without_partner(options: Optional[dict]) -> Optional[dict]:
    """Render options without the partner score (the score's own render: title, piano staves, hands)."""
    if not options:
        return options
    return {k: v for k, v in options.items() if k != "partner"} or None
