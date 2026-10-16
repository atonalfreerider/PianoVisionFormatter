#!/usr/bin/env python3
"""The vocals of a recording, isolated by Demucs, for ``python3 -m pianovision recordings`` (pianovision/separation.py).

Runs in a Python that has the ``demucs`` package (``[recordings] separator_python``); not a dependency of the
pianovision package.  The model's weights are downloaded by Demucs on first use.

    separate_vocals.py --input REC --out-dir DIR [--model htdemucs_ft] [--device auto|cuda|cpu]

writes ``DIR/vocals.wav`` (32-bit float, the input's sample rate and length) and ``DIR/tool.json``.
"""

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--model", default="htdemucs_ft")
    ap.add_argument("--device", default="auto")
    args = ap.parse_args(argv)
    import demucs
    import demucs.separate
    import torch
    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    os.makedirs(args.out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="demucs-", dir=args.out_dir)
    try:
        demucs.separate.main(["--two-stems", "vocals", "-n", args.model, "-d", device, "--float32",
                              "-o", tmp, "--filename", "{stem}.{ext}", args.input])
        found = glob.glob(os.path.join(tmp, "**", "vocals.wav"), recursive=True)
        if not found:
            sys.exit("demucs wrote no vocals.wav")
        shutil.move(found[0], os.path.join(args.out_dir, "vocals.wav"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with open(os.path.join(args.out_dir, "tool.json"), "w", encoding="utf-8") as f:
        json.dump({"demucs": getattr(demucs, "__version__", "?"), "torch": torch.__version__, "device": device,
                   "model": args.model}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
