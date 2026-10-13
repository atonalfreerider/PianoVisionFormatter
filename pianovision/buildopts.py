"""Per-score options of ``build``: ``scores.json``, checked in next to ``pianovision.toml``.

Keys of ``"scores"`` are score paths relative to the scores folder (as ``accompaniment.json``'s); keys starting with
``_`` are comments::

    {"scores": {
       "Gershwin/Rhapsody_in_blue__George_Gershwin.mscz": {
         "title": "Rhapsody in Blue (Piano and Orchestra)",   # the song's title and composer: PianoVision's name and
         "composer": "Gershwin",                              #   artist, the output name (instead of the score's)
         "simplified_from": "Gershwin/Rhapsody_in_Blue_Piano_solo.mscz",   # the simplified part: that score's piano
         "measure_map": "auto"},                              #   part, measure-aligned (pianovision.arrangement)
       "Rachmaninoff/pagvar18.mscz": {
         "piano_staves": [11, 12],                            # the piano part: these staves (1-based, right hand first)
         "hands": "split"}}}                                  # its notes to the hands by pitch and reach (a MIDI
                                                              #   import that wrote both hands on one staff)

``title`` / ``composer``: the song is named after them (``<first 4 letters of the composer>_<title>.json``, as for
any score) and PianoVision shows them; the score's own title frame and metadata are not read.  ``piano_staves``:
the staves that are the piano part, whatever their names and programs (every other staff is orchestra by its program
as usual); the first is the right hand, the others the left.  ``hands``: ``"staves"`` (default) or ``"split"``
(:mod:`pianovision.handsplit`).  ``simplified_from`` / ``measure_map``: see :mod:`pianovision.arrangement`; the
arrangement score is a dependency (an edit of either score writes the parts file again).

Options change what ``build`` makes of a score, so they are part of what the manifest records for it (``options``:
a hash of them, and the arrangement's content hash): a change renders the score again (its parts file only, when
only the arrangement changed).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

OPTIONS_NAME = "scores.json"
KNOWN = ("title", "composer", "piano_staves", "hands", "simplified_from", "measure_map")


class OptionsError(ValueError):
    pass


@dataclass
class BuildOptions:
    title: str = ""
    composer: str = ""
    piano_staves: List[int] = field(default_factory=list)   # 1-based
    hands: str = "staves"
    simplified_from: str = ""
    measure_map: Any = "auto"
    raw: Dict[str, Any] = field(default_factory=dict)        # the entry as written (comments left out)

    @classmethod
    def from_dict(cls, rel: str, d: Dict[str, Any]) -> "BuildOptions":
        d = {k: v for k, v in (d or {}).items() if not k.startswith("_")}
        unknown = sorted(k for k in d if k not in KNOWN)
        if unknown:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: unknown option(s) {', '.join(unknown)} ({', '.join(KNOWN)})")
        o = cls(title=str(d.get("title", "") or ""), composer=str(d.get("composer", "") or ""),
                piano_staves=[int(x) for x in d.get("piano_staves", []) or []], hands=str(d.get("hands", "staves")),
                simplified_from=str(d.get("simplified_from", "") or ""), measure_map=d.get("measure_map", "auto"),
                raw=d)
        if o.hands not in ("staves", "split"):
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: hands must be \"staves\" or \"split\", not {o.hands!r}")
        if o.hands == "split" and not o.piano_staves:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: hands \"split\" needs piano_staves")
        if any(x < 1 for x in o.piano_staves):
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: piano_staves are 1-based staff numbers")
        if o.simplified_from == rel:
            raise OptionsError(f"{OPTIONS_NAME}: {rel}: simplified_from names the score itself")
        return o

    def digest(self) -> str:
        """Hash of the options as written (comments left out)."""
        return hashlib.sha256(json.dumps(self.raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def render_options(self, scores_root: str, arrangement_compat: Optional[dict] = None) -> dict:
        """What :func:`pianovision.convert.convert_mscz` takes as ``options`` (plain data: it goes to worker
        processes)."""
        out: Dict[str, Any] = {}
        if self.title:
            out["title"] = self.title
        if self.composer:
            out["composer"] = self.composer
        if self.piano_staves:
            out["piano_staves"] = list(self.piano_staves)
            out["hands"] = self.hands
        if self.simplified_from:
            out["arrangement"] = {"path": os.path.join(scores_root, self.simplified_from), "rel": self.simplified_from,
                                  "compat": arrangement_compat, "measure_map": self.measure_map}
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


def without_arrangement(options: Optional[dict]) -> Optional[dict]:
    """Render options without the arrangement (what the song's PianoVision JSON depends on)."""
    if not options:
        return options
    return {k: v for k, v in options.items() if k != "arrangement"} or None
