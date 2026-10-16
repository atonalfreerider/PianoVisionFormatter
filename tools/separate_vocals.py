#!/usr/bin/env python3
"""The vocals of a recording, isolated by Demucs, for ``python3 -m pianovision recordings`` (pianovision/separation.py).

Runs in a Python that has the ``demucs`` package (``[recordings] separator_python``); not a dependency of the
pianovision package.  The model's weights must already be in Demucs' cache: this runs with the Hugging Face hub
offline (``HF_HUB_OFFLINE=1``), so nothing is downloaded.

    separate_vocals.py --input REC --out-dir DIR [--model htdemucs_ft] [--device auto|cuda|cpu]
                       [--segment S] [--overlap 0.25]

writes ``DIR/vocals.wav`` (32-bit float, the input's sample rate and length) and ``DIR/tool.json`` (versions, the
device and settings that worked, seconds, the GPU's peak memory).  On the GPU, a run that runs out of memory is tried
again with shorter segments (5 s, then 3 s), then on the CPU.
"""

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
import time


def run_demucs(args, device, segment, out):
    import demucs.separate
    argv = ["--two-stems", "vocals", "-n", args.model, "-d", device, "--float32", "--overlap", str(args.overlap),
            "-o", out, "--filename", "{stem}.{ext}"]
    if segment:
        argv += ["--segment", str(segment)]
    demucs.separate.main(argv + [args.input])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--model", default="htdemucs_ft")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--segment", type=float, default=0.0, help="seconds per segment (0: the model's own, 7.8 s)")
    ap.add_argument("--overlap", type=float, default=0.25)
    args = ap.parse_args(argv)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import demucs
    import torch
    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    tries = [(device, args.segment or None)]
    if device == "cuda":
        tries += [("cuda", 5), ("cuda", 3), ("cpu", args.segment or None)]
    os.makedirs(args.out_dir, exist_ok=True)
    attempts = []
    done = None
    for dev, seg in tries:
        tmp = tempfile.mkdtemp(prefix="demucs-", dir=args.out_dir)
        t0 = time.monotonic()
        try:
            if dev == "cuda":
                torch.cuda.empty_cache()
                torch.cuda.reset_peak_memory_stats()
            run_demucs(args, dev, seg, tmp)
            found = glob.glob(os.path.join(tmp, "**", "vocals.wav"), recursive=True)
            if not found:
                raise RuntimeError("demucs wrote no vocals.wav")
            shutil.move(found[0], os.path.join(args.out_dir, "vocals.wav"))
            done = {"device": dev, "segment": seg, "seconds": round(time.monotonic() - t0, 1)}
            if dev == "cuda":
                done["gpu_peak_mb"] = round(torch.cuda.max_memory_allocated() / 2 ** 20)
                done["gpu"] = torch.cuda.get_device_name(0)
            break
        except (RuntimeError, MemoryError) as e:           # torch.cuda.OutOfMemoryError is a RuntimeError
            msg = str(e)
            attempts.append({"device": dev, "segment": seg, "error": msg[:300],
                             "seconds": round(time.monotonic() - t0, 1)})
            if "out of memory" not in msg.lower() or dev == "cpu":
                raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if done is None:
        sys.exit("demucs failed on every device")
    with open(os.path.join(args.out_dir, "tool.json"), "w", encoding="utf-8") as f:
        json.dump(dict(done, demucs=getattr(demucs, "__version__", "?"), torch=torch.__version__, model=args.model,
                       overlap=args.overlap, attempts=attempts), f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
