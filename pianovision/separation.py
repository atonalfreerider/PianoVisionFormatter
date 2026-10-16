"""Vocals stems for companion audio: the singing of a recording you own, isolated by a source-separation model
(Demucs ``htdemucs_ft`` by default), cached by the input's content under ``[recordings] stems_dir``.

The model runs in another Python (``[recordings] separator_python``, or the first conda environment found with the
``demucs`` package): ``tools/separate_vocals.py`` wraps its command line.  Nothing is installed or downloaded here:
:func:`status` says what is missing.

Cache: ``<stems_dir>/<sha256 of the input, 16 hex>/<model>/vocals.wav`` and ``meta.json`` (input path, size, the
model, the device and settings that worked, how long it took, the GPU's peak memory).  The model runs with the
Hugging Face hub offline: its weights must already be in the cache.  The stem has the input's length and timing
(Demucs keeps them), so the alignment made from the full mix applies to it unchanged.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import time
from typing import Callable, Dict, List, Optional

PACKAGE = "demucs"
Log = Callable[[str], None]


class SeparationUnavailable(RuntimeError):
    """No Python with the separation package: the message says what is missing and how it could be added."""


def candidate_pythons(configured: str = "") -> List[str]:
    """The configured Python first, then every conda environment's."""
    out = []
    if configured:
        out.append(os.path.expanduser(configured))
    for root in ("~/miniconda3", "~/anaconda3", "~/mambaforge", "~/miniforge3"):
        base = os.path.expanduser(root)
        out += sorted(glob.glob(os.path.join(base, "envs", "*", "bin", "python")))
        if os.path.isfile(os.path.join(base, "bin", "python")):
            out.append(os.path.join(base, "bin", "python"))
    seen, uniq = set(), []
    for p in out:
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            uniq.append(p)
    return uniq


def has_modules(python: str, modules: List[str], timeout: float = 30.0) -> Dict[str, bool]:
    """Which of ``modules`` that Python can import (``find_spec``: nothing is imported, so it is quick)."""
    code = ("import importlib.util as u, json, sys; "
            f"print(json.dumps({{m: u.find_spec(m) is not None for m in {modules!r}}}))")
    try:
        r = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout.strip() or "{}") if r.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def status(configured: str = "") -> dict:
    """``{"available", "python" (or None), "checked" (Pythons), "torch" (those with torch), "missing" (text)}``."""
    checked, torch_envs = [], []
    for py in candidate_pythons(configured):
        mods = has_modules(py, [PACKAGE, "torch"])
        checked.append(py)
        if mods.get("torch"):
            torch_envs.append(py)
        if mods.get(PACKAGE):
            return {"available": True, "python": py, "checked": checked, "torch": torch_envs, "missing": ""}
    missing = (f"no Python has the '{PACKAGE}' package (checked {len(checked)}: "
               + ", ".join(_env_name(p) for p in checked) + "). "
               + (f"torch is installed in {', '.join(_env_name(p) for p in torch_envs)}; " if torch_envs else "")
               + f"installing it there (pip install {PACKAGE}) and its model weights (htdemucs_ft, downloaded on first "
               "use) would enable the vocal stems; then set [recordings] separator_python to that Python.")
    return {"available": False, "python": None, "checked": checked, "torch": torch_envs, "missing": missing}


def _env_name(python: str) -> str:
    parts = python.split(os.sep)
    return parts[-3] if len(parts) >= 3 and parts[-2] == "bin" and "envs" in parts else python


def content_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()[:16]


def stem_dir(stems_dir: str, src: str, model: str) -> str:
    return os.path.join(os.path.expanduser(stems_dir), content_hash(src), model)


def cached_vocals(stems_dir: str, src: str, model: str) -> Optional[str]:
    p = os.path.join(stem_dir(stems_dir, src, model), "vocals.wav")
    return p if os.path.isfile(p) else None


def vocals_stem(src: str, stems_dir: str, model: str = "htdemucs_ft", python: str = "", script: str = "",
                device: str = "auto", timeout: int = 7200, log: Log = print) -> str:
    """The vocals stem of the audio file ``src``, separated once and then served from the cache; raises
    :class:`SeparationUnavailable` when no Python has the separation package."""
    hit = cached_vocals(stems_dir, src, model)
    if hit:
        return hit
    st = status(python)
    if not st["available"]:
        raise SeparationUnavailable(st["missing"])
    out = stem_dir(stems_dir, src, model)
    os.makedirs(out, exist_ok=True)
    log(f"  separating the vocals of {os.path.basename(src)} ({model}) ...")
    t0 = time.monotonic()
    env = dict(os.environ, HF_HUB_OFFLINE="1")           # the weights come from the cache; nothing is downloaded
    r = subprocess.run([st["python"], script, "--input", src, "--out-dir", out, "--model", model, "--device", device],
                       capture_output=True, text=True, timeout=timeout, env=env)
    vocals = os.path.join(out, "vocals.wav")
    if r.returncode != 0 or not os.path.isfile(vocals):
        raise RuntimeError(f"separation failed: {(r.stderr or r.stdout)[-1500:]}")
    meta = {"input": os.path.abspath(src), "size": os.path.getsize(src), "model": model, "device": device,
            "wall_seconds": round(time.monotonic() - t0, 1), "python": st["python"]}
    try:
        with open(os.path.join(out, "tool.json"), encoding="utf-8") as f:
            meta.update(json.load(f))
    except (OSError, ValueError):
        pass
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    return vocals


def rms_levels(path: str, window: float, scratch: str, sample_rate: int = 44100) -> List[float]:
    """RMS level (dBFS) of consecutive ``window``-second blocks of the mono mix of ``path``; ffmpeg's measurement file
    goes to ``scratch`` (never next to the input: the music folder is only read)."""
    n = max(1, int(round(window * sample_rate)))
    os.makedirs(scratch, exist_ok=True)
    tmp = os.path.join(scratch, f".levels-{os.getpid()}-{time.monotonic_ns()}.txt")
    graph = (f"aresample={sample_rate},aformat=sample_fmts=flt,pan=mono|c0=0.5*c0+0.5*c1,asetnsamples=n={n}:p=0,"
             f"astats=metadata=1:reset=1:measure_overall=RMS_level:measure_perchannel=none,"
             f"ametadata=mode=print:file={tmp}")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-i", path, "-af", graph, "-f", "null",
                        "-"], capture_output=True, text=True)
    out: List[float] = []
    try:
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed on {path}: {r.stderr[-500:]}")
        with open(tmp, encoding="utf-8") as f:
            for line in f:
                if line.startswith("lavfi.astats.Overall.RMS_level="):
                    v = line.split("=", 1)[1].strip()
                    out.append(-200.0 if v in ("-inf", "nan", "") else max(-200.0, float(v)))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return out


def vocal_activity(vocals: str, mix: str, scratch: str, start: float = 0.0, end: Optional[float] = None,
                   window: float = 0.5, floor_db: float = -45.0, below_mix_db: float = 25.0) -> dict:
    """How much singing the stem has between ``start`` and ``end`` (recording seconds): the share of ``window``-second
    blocks where the stem is louder than ``floor_db`` and no more than ``below_mix_db`` under the mix, and the stem's
    energy against the mix's (dB)."""
    return activity_from_levels(rms_levels(vocals, window, scratch), rms_levels(mix, window, scratch), start, end,
                                window, floor_db, below_mix_db)


def activity_from_levels(rv: List[float], rm: List[float], start: float = 0.0, end: Optional[float] = None,
                         window: float = 0.5, floor_db: float = -45.0, below_mix_db: float = 25.0) -> dict:
    import math
    n = min(len(rv), len(rm))
    a = int(start / window)
    b = n if end is None else min(n, int(end / window) + 1)
    blocks = list(range(max(0, a), b))
    if not blocks:
        return {"activity": 0.0, "vocals_to_mix_db": None, "blocks": 0}
    active = [k for k in blocks if rv[k] > floor_db and rv[k] >= rm[k] - below_mix_db]
    ev = sum(10 ** (rv[k] / 10) for k in blocks)
    em = sum(10 ** (rm[k] / 10) for k in blocks)
    return {"activity": round(len(active) / len(blocks), 3),
            "vocals_to_mix_db": round(10 * math.log10(ev / em), 1) if ev > 0 and em > 0 else None,
            "blocks": len(blocks)}
