#!/usr/bin/env python3
"""Beat detection with madmom for ``python3 -m pianovision recordings tempo`` (``tools/recording_align.py bars``).

madmom's recurrent networks (RNNDownBeatProcessor, the 2016 models it ships with) give a beat and a downbeat
activation at 100 frames per second, and its dynamic Bayesian network (DBNBeatTrackingProcessor) the beats.  This
runs in another Python than recording_align.py (``[recordings] beats_python``): madmom 0.16.1 is in the beat_this
conda environment here.  Nothing is installed or downloaded: madmom 0.16.1 predates Python 3.10 and numpy 2, so the
aliases it still uses (``collections.MutableSequence``, ``np.float``, ...) are put back for this process only.

    beat_activations.py --audio REC --out act.npz

``act.npz``: ``beat`` (activation of a beat, downbeats included), ``downbeat`` (of a downbeat), ``beats`` (the DBN's
beat times, s), ``fps`` (100), ``version``.  madmom decodes the audio itself (ffmpeg, mono, 44.1 kHz int16: its
networks were trained at that rate; the same memory as 22.05 kHz float32).  About 1-2 GB of memory and 1-3 minutes
for a song on the CPU.
"""

import argparse
import collections
import collections.abc
import os
import sys

VERSION = 1
FPS = 100


def _compat():
    for name in ("MutableSequence", "MutableMapping", "Sequence", "Iterable", "Mapping", "Callable"):
        if not hasattr(collections, name):
            setattr(collections, name, getattr(collections.abc, name))
    import numpy as np
    for name, typ in (("float", float), ("int", int), ("complex", complex), ("bool", bool), ("object", object)):
        if name not in np.__dict__:
            setattr(np, name, typ)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-bpm", type=float, default=40.0)
    ap.add_argument("--max-bpm", type=float, default=220.0)
    args = ap.parse_args(argv)
    _compat()
    import warnings
    warnings.filterwarnings("ignore")
    import numpy as np
    from madmom.features.beats import DBNBeatTrackingProcessor
    from madmom.features.downbeats import RNNDownBeatProcessor
    act = RNNDownBeatProcessor()(os.path.abspath(args.audio))          # [:, 0] beat (not downbeat), [:, 1] downbeat
    beat = (act[:, 0] + act[:, 1]).astype(np.float32)
    beats = DBNBeatTrackingProcessor(fps=FPS, min_bpm=args.min_bpm, max_bpm=args.max_bpm)(beat)
    tmp = args.out + ".tmp.npz"
    np.savez_compressed(tmp, beat=beat, downbeat=act[:, 1].astype(np.float32), beats=np.asarray(beats, np.float64),
                        fps=FPS, version=VERSION)
    os.replace(tmp, args.out)
    print(f"{len(beats)} beats, {len(beat) / FPS:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
