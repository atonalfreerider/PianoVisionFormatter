#!/usr/bin/env python3
"""Alignment, retiming and verification of a recording against a song's timeline, for
``python3 -m pianovision recordings`` (``pianovision/recordings.py``).

Needs numpy, scipy, soundfile and librosa (not dependencies of the pianovision package): the command runs this
file with ``[recordings] python`` from pianovision.toml, e.g. a conda environment's Python.  ffmpeg decodes the
audio and time-stretches it (its ``rubberband`` filter, librubberband).

    recording_align.py align   --ref ref.json --audio REC [--out align.json]
    recording_align.py retime  --ref ref.json --align align.json --audio FILE [--source REC] --out out.wav
                               [--retune-cents C] [--latency-cache FILE]
    recording_align.py verify  --ref ref.json --audio out.wav [--key K] [--out verify.json]
    recording_align.py latency [--out latency.json]

``ref.json`` (written by pianovision.recordings): the song's measures as played (``start``/``end`` seconds,
``number``, ``pass``, ``beats``) and notes (``[start, duration, pitch, velocity, part]``) on the song's clock.

**align.**  The song's notes are synthesised (a few harmonics, piano notes decaying) and both signals turned into
chroma (librosa's CQT chroma; the recording's tuning estimated) at 23 ms frames, log-compressed and normalised.

1. *Key.*  The global pitch-class profiles under the 12 rotations give the two likeliest transpositions; each is
   tried in step 2 and the one whose alignment costs least wins (``key.offset``: + = the recording is higher).
2. *Coarse DTW* (frames averaged to 46-400 ms so the cost matrix stays small): cosine distance, steps (1,1), (1,0),
   (0,1) with a small penalty on the last two, open begin and end on the recording's side (subsequence DTW), so a
   recording's intro and outro cost nothing.
3. *Pieces and gaps* (:func:`pianovision.recmap.pieces_from_path`): the path's similarity (1 - cost / median cost)
   smoothed over 4 s; long horizontal or vertical runs and low similarity are unmatched; matched stretches of 4 s
   or more are the pieces.
4. *Refinement* at the song's beats: the coarse path smoothed (a straight line through it within +-0.5 s of each
   beat; +-1.5 s made a pop song's chord changes land 20-50 ms off), then the onset envelopes (spectral flux,
   5.8 ms) of the synthesis and the recording cross-correlated in a window of +-2 s around each beat, the synthesis
   warped by the local tempo, within +-80-250 ms; where that is clear (correlation 0.35 or more, in at least 30 % of
   a piece's beats) the lags, smoothed (running median of 5, mean of 3), move the beats.  A monotone fit (the
   recording never stands still), stretches where the recording runs at an implausible speed against the rest of
   the piece taken out as gaps (:func:`pianovision.recmap.split_irregular`: song measures squeezed into almost no
   recording are measures it does not have) and a Douglas-Peucker simplification (8 ms) give the anchors.
5. The edit plan (:func:`pianovision.recmap.edit_plan`) and the tempo summary.

**retime.**  The recording (``--audio``: the variant to deliver, e.g. the piano-removed stem; ``--source``: the
file the alignment was made from, when different: the stem's offset against it is measured and applied, unless
``--assume-aligned``: a stem separated from it has its timing) is cut and
padded per the plan into one continuous stream with 8 ms fades at the cuts, then stretched in a single pass by
ffmpeg's rubberband filter whose tempo is switched per operation (``asendcmd``; the commands are sent the filter's
measured latency later, ``latency``).  The filter does not land the content exactly where it is asked to after
large tempo changes (tens to hundreds of ms), so the output is compared with the recording resampled through the
plan (timing exact) in 3 s windows, and the stretch is run again with the operations' targets moved by that error,
up to 5 passes, until 95 % of the windows are within 8 ms (``passes``, ``fidelity_p95_ms``; the best pass is kept).  ``--retune-cents``:
also pitch-shifted by that many cents.

**verify.**  The retimed audio against the song's notes, on the song's clock: per measure the chroma agreement
(cosine similarity against the synthesis, rescaled so 0 is chance and 1 identical) and the onset lag (cross-
correlation of the onset envelopes within +-150 ms).  Timing over the whole song: ``lag``, the shift of the audio's
chroma against the synthesis' that agrees best in 8 s windows every 4 s (within +-500 ms; windows whose best shift
stands out by 0.02 count): the chord changes time it, which works for a band recording too; ``onset_lag``, the
same from the onset envelopes in 6 s windows (correlation 0.3 or more), reliable for piano and orchestra.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pianovision import recmap as RM          # noqa: E402

VERSION = 3                 # bump when align / retime / verify change their results
SR = 22050
HOP = 512                   # chroma frames: 23.2 ms
OHOP = 128                  # onset envelope: 5.8 ms
OUT_SR = 44100
MAX_CELLS = 18_000_000      # coarse DTW cost matrix size
SMOOTH_S = 0.5              # half-width (s) of the straight line through the coarse path at each beat


# ------------------------------------------------------------------------------
# audio and the song's notes
# ------------------------------------------------------------------------------

def load(path, sr=SR, channels=1):
    """Decode with ffmpeg to float32 (frames x channels, or a vector for mono)."""
    args = ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", str(channels), "-ar", str(sr), "-"]
    raw = subprocess.run(args, capture_output=True, check=True).stdout
    y = np.frombuffer(raw, dtype=np.float32).copy()
    return y if channels == 1 else y.reshape(-1, channels)


def read_ref(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def synthesize(ref, sr=SR, end=None):
    """The song's notes as audio: harmonics 1-4, a 5 ms attack, piano notes decaying (slower when low), the other
    parts held for their length; velocity as amplitude."""
    notes = ref["notes"]
    end = end or max([ref.get("song_end", 0.0)] + [n[0] + n[1] for n in notes]) + 1.0
    y = np.zeros(int(end * sr) + sr, dtype=np.float32)
    harm = np.array([1.0, 0.5, 0.3, 0.2])
    for start, dur, pitch, vel, part in notes:
        f0 = 440.0 * 2 ** ((pitch - 69) / 12.0)
        piano = part == "p"
        length = min(max(dur, 0.08), 6.0) + (0.25 if piano else 0.06)
        n = int(length * sr)
        i0 = int(round(start * sr))
        if n <= 0 or i0 >= len(y):
            continue
        n = min(n, len(y) - i0)
        t = np.arange(n, dtype=np.float32) / sr
        if piano:
            tau = 0.4 + 2.5 * (1 - (pitch - 21) / 87.0)
            env = np.exp(-t / tau)
            rel = np.clip((t - max(dur, 0.08)) / 0.25, 0, 1)
            env *= 1 - rel
        else:
            env = np.clip((max(dur, 0.08) + 0.06 - t) / 0.06, 0, 1)
        env *= np.clip(t / 0.005, 0, 1)
        s = np.zeros(n, dtype=np.float32)
        for k, a in enumerate(harm, 1):
            if f0 * k < sr / 2 - 500:
                s += a * np.sin(2 * np.pi * f0 * k * t)
        y[i0:i0 + n] += (float(vel) ** 1.5) * env * s
    peak = np.abs(y).max()
    return y / peak * 0.5 if peak > 0 else y


def chroma(y, tuning=None):
    import librosa
    c = librosa.feature.chroma_cqt(y=y, sr=SR, hop_length=HOP, norm=None, tuning=tuning, bins_per_octave=36)
    return c.astype(np.float32)


def normalize(c, floor_db=-50.0, comp=1.0):
    """Mild log compression and unit length per frame; frames far below the loudest are made uniform (silence).
    (Strong compression, log(1 + 100 x), made every frame of a pop song look alike: the alignment's contrast between
    the right and a wrong verse halved.)"""
    e = c.sum(axis=0)
    loud = e > (e.max() * 10 ** (floor_db / 10) if e.max() > 0 else 1)
    c = np.log1p(comp * c / (c.max() + 1e-12))
    n = np.linalg.norm(c, axis=0)
    out = c / np.maximum(n, 1e-9)
    out[:, ~loud] = 1.0 / np.sqrt(12)
    return out, loud


def estimate_tuning(y, chunk=30.0, chunks=6):
    """The recording's deviation from A440 in fractions of a semitone (-0.5..0.5), from up to ``chunks`` stretches
    of ``chunk`` seconds spread over it (librosa's piptrack-based estimate)."""
    import librosa
    n = int(chunk * SR)
    if len(y) <= n * chunks:
        parts = [y]
    else:
        starts = np.linspace(0, len(y) - n, chunks).astype(int)
        parts = [y[s:s + n] for s in starts]
    return float(librosa.estimate_tuning(y=np.concatenate(parts), sr=SR, bins_per_octave=12))


def onset_env(y):
    import librosa
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=OHOP, n_fft=1024, max_size=3)
    env = env.astype(np.float64)
    return env / (np.percentile(env, 99) + 1e-12)


def block(c, k):
    """Average every ``k`` frames (and renormalise)."""
    n = c.shape[1] // k
    if n == 0:
        return c
    b = c[:, :n * k].reshape(c.shape[0], n, k).mean(axis=2)
    return b / np.maximum(np.linalg.norm(b, axis=0), 1e-9)


# ------------------------------------------------------------------------------
# align
# ------------------------------------------------------------------------------

def coarse_dtw(S, R, penalty):
    """Subsequence DTW of song frames ``S`` against recording frames ``R`` (columns are unit vectors): path
    (song frame, recording frame) ascending, the cost matrix and its median, the mean cost along the path.  The start
    and end are free on the recording's side only; a song longer than the recording gets columns of the median cost
    after the recording's end (librosa would otherwise free the song's side), which the path leaves out."""
    import librosa
    C = (1.0 - S.T @ R).astype(np.float64)
    n, m = C.shape
    med = float(np.median(C))
    Cp = np.concatenate([C, np.full((n, n - m + 1), med)], axis=1) if n > m else C
    steps = np.array([[1, 1], [1, 0], [0, 1]])
    D, wp = librosa.sequence.dtw(C=Cp, subseq=True, step_sizes_sigma=steps,
                                 weights_add=np.array([0.0, penalty * med, penalty * med]),
                                 weights_mul=np.array([1.0, 1.0, 1.0]), backtrack=True)
    wp = wp[::-1]
    cost = float(D[wp[-1][0], wp[-1][1]]) / max(len(wp), 1)
    wp = wp[wp[:, 1] < m]
    return wp, C, med, cost


def refine_piece(piece, beats, env_s, env_r, fr, search, win=2.0):
    """Recording time of each beat in the piece: the onset envelopes cross-correlated around the piece's current
    mapping; returns [(song t, mapped rec t, lag s, weight = peak correlation)]."""
    xs = [a[0] for a in piece.anchors]
    ys = [a[1] for a in piece.anchors]
    out = []
    u = np.arange(-win, win, 1.0 / fr)
    lags = np.arange(-search, search + 1e-9, 1.0 / fr)
    t_s = np.arange(len(env_s)) / fr
    t_r = np.arange(len(env_r)) / fr
    for b in beats:
        if b < piece.song0 or b > piece.song1:
            continue
        r = RM.interp(xs, ys, b)
        k = (RM.interp(xs, ys, min(b + 1.0, piece.song1)) - RM.interp(xs, ys, max(b - 1.0, piece.song0))) / \
            max(min(b + 1.0, piece.song1) - max(b - 1.0, piece.song0), 1e-6)
        a = np.interp(b + u, t_s, env_s, left=0.0, right=0.0)
        a = a - a.mean()
        na = np.linalg.norm(a)
        if na < 1e-6:
            out.append((b, r, 0.0, 0.0))
            continue
        tr = r + k * u                                   # recording times of the window under the current map
        grid = tr[None, :] + lags[:, None]
        bb = np.interp(grid, t_r, env_r, left=0.0, right=0.0)
        bb = bb - bb.mean(axis=1, keepdims=True)
        nb = np.linalg.norm(bb, axis=1)
        cs = (bb @ a) / (nb * na + 1e-12)
        cs = cs - 0.08 * np.abs(lags) / max(search, 1e-6)    # a little prior towards the current mapping
        i = int(np.argmax(cs))
        frac = 0.0
        if 0 < i < len(cs) - 1:
            den = cs[i - 1] - 2 * cs[i] + cs[i + 1]
            frac = 0.5 * (cs[i - 1] - cs[i + 1]) / den if den else 0.0
        peak = float(cs[i])
        out.append((b, r, float(lags[i] + frac / fr), max(peak, 0.0)))
    return out


def local_linear(xs, ys, x0, half):
    """The straight line through the points within ``half`` of ``x0`` (least squares), at ``x0``."""
    lo, hi = np.searchsorted(xs, x0 - half), np.searchsorted(xs, x0 + half, side="right")
    if hi - lo < 3:
        return float(np.interp(x0, xs, ys))
    x, y = xs[lo:hi], ys[lo:hi]
    mx = x.mean()
    vx = ((x - mx) ** 2).sum()
    k = ((x - mx) * (y - y.mean())).sum() / vx if vx > 0 else 0.0
    return float(y.mean() + k * (x0 - mx))


def smooth_lags(lags, conf, min_conf=0.35):
    """Beat lags kept where the correlation is at least ``min_conf``, interpolated in between, then a running median
    of 5 and a running mean of 3: a lag follows its neighbours, an isolated jump (half a beat off) does not stick."""
    n = len(lags)
    ok = conf >= min_conf
    if not ok.any():
        return np.zeros(n)
    idx = np.arange(n)
    filled = np.interp(idx, idx[ok], lags[ok])
    med = np.array([np.median(filled[max(0, i - 2):i + 3]) for i in range(n)])
    return np.array([med[max(0, i - 1):i + 2].mean() for i in range(n)])


def refine(tm, ref, env_s, env_r, fr, coarse_hop, min_share=0.3):
    """Beat-level anchors for each piece: the coarse path smoothed (a straight line through it within ``SMOOTH_S`` of
    each beat), then moved by the onset cross-correlation where it is clear (correlation 0.35 or more; a piece where
    fewer than ``min_share`` of the beats are clear keeps the smoothed path: a band's onsets against a piano
    synthesis mislead more than they help), monotone, implausible speeds cut out, simplified to 8 ms."""
    beats = sorted({round(b, 6) for m in ref["measures"] for b in m.get("beats", [m["start"]])})
    pieces = []
    stats = {"beats": 0, "clear": 0, "pieces_refined": 0}
    for pc in tm.pieces:
        xs = np.array([a[0] for a in pc.anchors])
        ys = np.array([a[1] for a in pc.anchors])
        bs = sorted(set([pc.song0] + [b for b in beats if pc.song0 <= b <= pc.song1] + [pc.song1]))
        rb = [local_linear(xs, ys, b, SMOOTH_S) for b in bs]
        smooth = RM.Piece(RM.monotone(list(zip(bs, rb)), 0.3 * pc.ratio), pc.quality)
        search = float(min(0.25, max(0.08, 1.5 * coarse_hop)))
        pts = refine_piece(smooth, bs, env_s, env_r, fr, search=search)
        lags = np.array([p[2] for p in pts])
        conf = np.array([p[3] for p in pts])
        clear = conf >= 0.35
        stats["beats"] += len(pts)
        stats["clear"] += int(clear.sum())
        anchors = [(p[0], p[1]) for p in pts]
        if len(pts) and clear.mean() >= min_share:
            sl = smooth_lags(lags, conf)
            anchors = [(p[0], p[1] + float(d)) for p, d in zip(pts, sl)]
            stats["pieces_refined"] += 1
        anchors = RM.monotone(anchors, 0.3 * pc.ratio)
        parts = RM.split_irregular(anchors)
        stats["irregular_cuts"] = stats.get("irregular_cuts", 0) + max(0, len(parts) - 1)
        for part in parts:
            pieces.append(RM.Piece(RM.simplify(part, 0.008), pc.quality))
    return RM.TimeMap(pieces), stats


def measure_quality(ref, path, sim, song_hop):
    """Mean path similarity per measure (frames of the path inside it)."""
    ms = ref["measures"]
    acc = np.zeros(len(ms))
    cnt = np.zeros(len(ms))
    starts = np.array([m["start"] for m in ms])
    for (i, _j), s in zip(path, sim):
        t = i * song_hop
        k = int(np.searchsorted(starts, t, side="right")) - 1
        if 0 <= k < len(ms):
            acc[k] += s
            cnt[k] += 1
    return [round(float(a / c), 3) if c else None for a, c in zip(acc, cnt)]


def cmd_align(args):
    ref = read_ref(args.ref)
    y = load(args.audio)
    rec_end = len(y) / SR
    song_end = float(ref["song_end"])
    syn = synthesize(ref)
    tuning = estimate_tuning(y)                          # fraction of a semitone
    Cr_raw = chroma(y, tuning * 3)                       # chroma_cqt's bins are thirds of a semitone
    Cs_raw = chroma(syn, 0.0)
    Cr, _loud = normalize(Cr_raw)
    Cs, _loud = normalize(Cs_raw)
    ns = min(Cs.shape[1], int(np.ceil(song_end * SR / HOP)) + 4)
    Cs = Cs[:, :ns]
    # coarse resolution
    k = 2
    while (ns // k) * (Cr.shape[1] // k) > MAX_CELLS:
        k += 1
    hop_c = k * HOP / SR
    S, R = block(Cs, k), block(Cr, k)
    # key: every rotation of the song's chroma aligned at ~0.5 s frames; the two cheapest again at full resolution
    kq = max(k, int(round(0.5 / (HOP / SR))))
    Sq, Rq = block(Cs, kq), block(Cr, kq)
    quick = {}
    for sh in range(12):
        _wp, _C, med_q, cost_q = coarse_dtw(np.roll(Sq, sh, axis=0), Rq, args.penalty)
        quick[sh] = cost_q / med_q
    tries = sorted(quick, key=quick.get)[:2]
    results = {}
    for sh in tries:
        results[sh] = coarse_dtw(np.roll(S, sh, axis=0), R, args.penalty)
    best = min(results, key=lambda sh: results[sh][3] / results[sh][2])
    wp, C, med, cost = results[best]
    key_costs = {str((sh + 6) % 12 - 6): round(v, 4) for sh, v in sorted(quick.items(), key=lambda kv: kv[1])}
    path = [(int(i), int(j)) for i, j in wp]
    sim = [1.0 - C[i, j] / med for i, j in path]
    tm0 = RM.pieces_from_path(path, sim, hop_c, hop_c, RM.PathParams())
    # refinement on onset envelopes
    env_s = onset_env(syn)
    env_r = onset_env(y)
    fr = SR / OHOP
    if args.no_refine:
        tm, rstats = tm0, {"beats": 0, "weak": 0, "off": True}
    else:
        tm, rstats = refine(tm0, ref, env_s, env_r, fr, hop_c)
    gaps = tm.gaps(song_end, rec_end)
    plan = RM.edit_plan(tm, song_end, rec_end, tail_s=args.tail)
    RM.check_plan(plan)
    mq = measure_quality(ref, path, sim, hop_c)
    ms = ref["measures"]
    key = (best + 6) % 12 - 6
    second = sorted(quick.values())
    out = {
        "format": 1, "version": VERSION, "audio": os.path.abspath(args.audio),
        "duration": round(rec_end, 3), "song_end": round(song_end, 3),
        "key": {"offset": key, "alignment_cost": key_costs,
                "margin": round(second[1] - second[0], 4) if len(second) > 1 else None},
        "tuning_cents": round(tuning * 100.0, 1),       # + = the recording is sharp of A440
        "coarse_hop": round(hop_c, 4), "path_similarity": round(float(np.mean(sim)), 4),
        "coarse_map": tm0.to_dict(), "map": tm.to_dict(), "refine": rstats,
        "gaps": [dict(g.to_dict(), measures=RM.measure_ranges(ms, g.song0, g.song1)) for g in gaps],
        "plan": [o.to_dict() for o in plan],
        "tempo": RM.tempo_summary(tm, ms),
        "measures": [{"label": RM.measure_label(m, ()), "start": m["start"], "end": m["end"], "path_similarity": q}
                     for m, q in zip(ms, mq)],
        "path": [[round(i * hop_c, 3), round(j * hop_c, 3)] for i, j in path[::max(1, len(path) // 4000)]],
    }
    write_json(args.out, out)


# ------------------------------------------------------------------------------
# retime
# ------------------------------------------------------------------------------

def run_ffmpeg(args):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-v", "error"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(args)[:300]}\n{r.stderr[-1500:]}")


def stretch(y, sr, segments, latency, pitch=1.0, base=None):
    """``y`` (frames x 2) played with rubberband, the tempo switched at the input times of ``segments``
    [(input start s, tempo)]; the commands go ``latency`` seconds later (the filter applies a change to the input it
    still holds).  Scratch files in a temporary folder under ``base``."""
    import shutil
    import soundfile as sf
    work = tempfile.mkdtemp(prefix="nw-retime-", dir=base)
    try:
        src = os.path.join(work, "edited.wav")
        dst = os.path.join(work, "stretched.wav")
        cmds = os.path.join(work, "tempo.cmd")
        sf.write(src, y, sr, subtype="FLOAT")
        with open(cmds, "w") as f:
            for t, tempo in segments[1:]:
                f.write(f"{t + latency:.6f} rubberband tempo {tempo:.8f};\n")
        t0 = segments[0][1] if segments else 1.0
        af = ("asetnsamples=n=256:p=0," + (f"asendcmd=f={cmds}," if len(segments) > 1 else "")
              + f"rubberband=tempo={t0:.8f}:pitch={pitch:.8f}:channels=together:pitchq=quality")
        run_ffmpeg(["-y", "-i", src, "-af", af, "-c:a", "pcm_f32le", dst])
        z, _ = sf.read(dst, dtype="float32", always_2d=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return z


def clicks_signal(sr, length, every=0.25, start=0.5):
    rng = np.random.RandomState(7)
    y = np.zeros(int(length * sr), dtype=np.float32)
    times = np.arange(start, length - 0.5, every)
    burst = (rng.randn(64) * 0.5).astype(np.float32)
    for t in times:
        i = int(round(t * sr))
        y[i:i + 64] += burst
    return np.stack([y, y], axis=1), times


def detect_clicks(z, sr, min_gap=0.1):
    e = (z[:, 0] if z.ndim > 1 else z) ** 2
    hop = 32
    env = np.add.reduceat(e, np.arange(0, len(e), hop))
    thr = env.max() * 0.1
    out, last = [], -1e9
    for k, v in enumerate(env):
        t = k * hop / sr
        if v > thr and t - last > min_gap:
            out.append(t)
            last = t
    return np.array(out)


def _click_errors(got, exp, tol=0.2):
    """Detected minus expected time of each expected click (nearest detection within ``tol``; NaN without)."""
    out = np.full(len(exp), np.nan)
    if len(got):
        for k, e in enumerate(exp):
            j = int(np.argmin(np.abs(got - e)))
            if abs(got[j] - e) <= tol:
                out[k] = got[j] - e
    return out


def measure_latency(base=None):
    """The rubberband filter's effective command latency: a tempo change sent when the input reaches time t applies
    to input the filter still holds, from about t - latency on.  Measured on clicks every 0.37 s through a tempo that
    changes every 0.5 s (a random walk between 0.6 and 1.6, like a retime's): the latency that lands the clicks best,
    from a grid of 20-100 ms."""
    sr = OUT_SR
    rng = np.random.RandomState(3)
    y, times = clicks_signal(sr, 60.0, every=0.37)
    starts = np.arange(0.0, 60.0, 0.5)
    tempos = np.clip(1.0 + 0.03 * np.cumsum(rng.randn(len(starts))), 0.6, 1.6)
    segs = list(zip(starts.tolist(), tempos.tolist()))
    out_at = np.concatenate([[0.0], np.cumsum(0.5 / tempos)])
    k = np.minimum((times // 0.5).astype(int), len(tempos) - 1)
    exp = out_at[k] + (times - starts[k]) / tempos[k]
    best = None
    for lat in np.arange(0.02, 0.1001, 0.005):
        err = _click_errors(detect_clicks(stretch(y, sr, segs, float(lat), base=base), sr), exp, tol=0.15)
        err = err[~np.isnan(err)]
        if len(err) < 0.9 * len(exp):
            continue
        score = float(np.mean(np.abs(err)))
        if best is None or score < best[0]:
            best = (score, float(lat), err)
    if best is None:
        return {"latency_s": 0.05, "measured": False}
    _score, lat, err = best
    return {"latency_s": round(lat, 4), "measured": True, "clicks": int(len(err)),
            "residual_ms_median": round(float(np.median(err)) * 1000, 2),
            "residual_ms_p95": round(float(np.percentile(np.abs(err), 95)) * 1000, 2)}


def ffmpeg_version():
    r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    return (r.stdout.splitlines() or [""])[0]


def cached_latency(path):
    ver = ffmpeg_version()
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("ffmpeg") == ver and d.get("version") == VERSION:
                return d
        except (OSError, ValueError):
            pass
    d = measure_latency(os.path.dirname(path) if path else None)
    d.update({"ffmpeg": ver, "version": VERSION})
    if path:
        write_json(path, d)
    return d


def offset_between(a, b, sr=SR, seconds=90.0, maxlag=1.0):
    """Lag (s) of ``b`` against ``a`` (+ = ``b`` later) from their onset envelopes over the busiest stretch."""
    ea, eb = onset_env(a), onset_env(b)
    fr = sr / OHOP
    n = min(len(ea), len(eb))
    w = int(min(seconds, n / fr - 2 * maxlag - 1) * fr)
    M = int(maxlag * fr)
    if w <= fr:
        return 0.0, 0.0
    best_s, best_e = M, -1.0
    for s in range(M, n - w - M, int(fr * 10)):
        e = float(ea[s:s + w].sum())
        if e > best_e:
            best_s, best_e = s, e
    x = ea[best_s:best_s + w] - ea[best_s:best_s + w].mean()
    cs = []
    for L in range(-M, M + 1):
        z = eb[best_s + L:best_s + L + w]
        z = z - z.mean()
        cs.append(float(np.dot(x, z) / (np.linalg.norm(x) * np.linalg.norm(z) + 1e-12)))
    i = int(np.argmax(cs))
    return (i - M) / fr, cs[i]


def edit_stream(y, ops, shift, sr):
    """The recording cut and padded per the plan into one stream (frames x 2), 8 ms fades at every join that is not
    continuous, a 2 s fade-out at the end of a tail; with each operation's start in the stream."""
    n_in = len(y)
    fade = int(0.008 * sr)
    chunks, starts = [], []
    pos = 0
    joins = set(RM.cuts(ops))
    for k, op in enumerate(ops):
        if op.kind == "audio":
            i0 = int(round((op.rec0 + shift) * sr))
            i1 = int(round((op.rec1 + shift) * sr))
            seg = np.zeros((max(i1 - i0, 1), 2), dtype=np.float32)
            a0, a1 = max(i0, 0), min(i1, n_in)
            if a1 > a0:
                seg[a0 - i0:a1 - i0] = y[a0:a1]
        else:
            seg = np.zeros((max(int(round((op.song1 - op.song0) * sr)), 1), 2), dtype=np.float32)
        if k in joins and len(seg) > 2 * fade:
            seg[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)[:, None]
            if chunks and len(chunks[-1]) > 2 * fade:
                chunks[-1][-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)[:, None]
        starts.append(pos)
        chunks.append(seg)
        pos += len(seg)
    nf = min(len(chunks[-1]), int(2.0 * sr))
    if ops[-1].kind == "audio" and nf > 0:
        chunks[-1][-nf:] *= np.linspace(1, 0, nf, dtype=np.float32)[:, None]
    return np.concatenate(chunks), starts, [len(c) for c in chunks], len(joins)


def varispeed(y22, ops, shift, n):
    """The plan applied by resampling (pitch moves, timing exact): what the stretched output should line up with,
    mono at SR, ``n`` samples."""
    t = np.arange(n) / SR
    starts = np.array([o.song0 for o in ops])
    k = np.clip(np.searchsorted(starts, t, side="right") - 1, 0, len(ops) - 1)
    s0 = starts[k]
    s1 = np.array([o.song1 for o in ops])[k]
    r0 = np.array([o.rec0 for o in ops])[k] + shift
    r1 = np.array([o.rec1 for o in ops])[k] + shift
    audio = np.array([o.kind == "audio" for o in ops])[k] & (t <= s1)
    rt = r0 + (r1 - r0) * (t - s0) / np.maximum(s1 - s0, 1e-9)
    out = np.interp(rt * SR, np.arange(len(y22)), y22, left=0.0, right=0.0).astype(np.float32)
    out[~audio] = 0.0
    return out


def realized_lag(z22, ideal22, win=3.0, hop=1.0, maxlag=0.4, min_corr=0.5):
    """Lag of the stretched output against the varispeed (+ = output later), per window centre, from the onset
    envelopes; None where the windows do not correlate."""
    ea, eb = onset_env(z22), onset_env(ideal22)
    fr = SR / OHOP
    w = windows_lag(ea, eb, fr, win=win, hop=hop, maxlag=maxlag, min_corr=0.0)
    return [(t0 + win / 2, lag / 1000.0) for t0, lag, c in w if c >= min_corr]


def cmd_retime(args):
    """Stretch per the plan; then measure where the content landed against the plan (the output against the
    recording resampled through the plan) and run the stretch again with the operations' targets moved by the
    error, up to ``--passes`` times, until 95 % of the windows are within 8 ms; the best pass is kept."""
    import soundfile as sf
    al = read_ref(args.align)
    ops = [RM.Op.from_dict(o) for o in al["plan"]]
    RM.check_plan(ops)
    sr = OUT_SR
    y = load(args.audio, sr, channels=2)
    y22 = load(args.audio)
    shift, shift_corr = 0.0, None
    if args.source and not args.assume_aligned and os.path.abspath(args.source) != os.path.abspath(args.audio):
        shift, shift_corr = offset_between(load(args.source), y22)
        if abs(shift) < 0.003 or shift_corr < 0.4:            # no offset, or none that can be trusted
            shift = 0.0
    edited, starts, lens, n_joins = edit_stream(y, ops, shift, sr)
    lat = cached_latency(args.latency_cache)
    pitch = 2 ** (-(args.retune_cents or 0.0) / 1200.0)
    total = int(round(RM.output_length(ops) * sr))
    ideal = varispeed(y22, ops, shift, int(round(RM.output_length(ops) * SR)))
    targets = np.array([o.song0 for o in ops] + [ops[-1].song1])     # where each operation should start / end
    cmd = targets.copy()                                              # where the stretch is asked to put them
    passes = []
    base = os.path.dirname(os.path.abspath(args.out))
    best = None
    for it in range(max(1, args.passes)):
        durs = np.maximum(np.diff(cmd), 1e-3)
        segments = [(starts[k] / sr, (lens[k] / sr) / durs[k]) for k in range(len(ops))]
        z = stretch(edited, sr, segments, lat["latency_s"], pitch, base)
        lead = int(round(cmd[0] * sr))                                # the first operation's start in the output
        z = np.concatenate([np.zeros((lead, 2), np.float32), z]) if lead > 0 else z[-lead:]
        z = np.concatenate([z, np.zeros((max(0, total - len(z)), 2), np.float32)])[:total]
        z22 = z.mean(axis=1)
        if sr != SR:
            import librosa
            z22 = librosa.resample(z22, orig_sr=sr, target_sr=SR)
        lags = realized_lag(z22[:len(ideal)], ideal[:len(z22)])
        err = np.array([e for _t, e in lags]) if lags else np.zeros(0)
        stat = {"pass": it + 1, "windows": int(len(err)),
                "median_ms": round(float(np.median(err)) * 1000, 1) if len(err) else None,
                "p95_abs_ms": round(float(np.percentile(np.abs(err), 95)) * 1000, 1) if len(err) else None}
        passes.append(stat)
        score = stat["p95_abs_ms"] if stat["p95_abs_ms"] is not None else 1e9
        if best is None or score < best[0]:
            best = (score, z)
        if not len(err) or score <= 8.0:
            break
        # move the targets by the error, smoothed over the neighbouring windows
        tt = np.array([t for t, _e in lags])
        ee = np.array([np.median(err[max(0, i - 1):i + 2]) for i in range(len(err))])
        corr = np.interp(targets, tt, ee)
        cmd = cmd - (1.0 if it == 0 else 0.7) * corr          # damped after the first: the error is not linear
        cmd = np.maximum.accumulate(cmd + np.arange(len(cmd)) * 1e-4) - np.arange(len(cmd)) * 1e-4
    z = best[1]
    sf.write(args.out, z, sr, subtype="FLOAT")
    write_json(args.out + ".json", {"ops": len(ops), "cuts": n_joins, "duration": round(total / sr, 3),
                                    "stem_shift_s": round(shift, 4), "stem_shift_corr": shift_corr,
                                    "latency": lat, "pitch_scale": pitch, "retune_cents": args.retune_cents or 0.0,
                                    "passes": passes, "fidelity_p95_ms": best[0] if best[0] < 1e9 else None,
                                    "peak": round(float(np.abs(z).max()), 4) if len(z) else 0.0})


# ------------------------------------------------------------------------------
# verify
# ------------------------------------------------------------------------------

def windows_lag(env_a, env_s, fr, win=6.0, hop=3.0, maxlag=0.2, min_corr=0.3):
    """Lag (ms, + = audio later) of the audio's onset envelope against the synthesis' per window."""
    W, H, M = int(win * fr), int(hop * fr), int(maxlag * fr)
    out = []
    n = min(len(env_a), len(env_s))
    for s in range(M, n - W - M, H):
        b = env_s[s:s + W] - env_s[s:s + W].mean()
        nb = np.linalg.norm(b)
        if nb < 1e-6:
            continue
        cs = []
        for L in range(-M, M + 1):
            a = env_a[s + L:s + L + W]
            a = a - a.mean()
            cs.append(np.dot(a, b) / (np.linalg.norm(a) * nb + 1e-12))
        cs = np.array(cs)
        i = int(np.argmax(cs))
        frac = 0.0
        if 0 < i < len(cs) - 1:
            den = cs[i - 1] - 2 * cs[i] + cs[i + 1]
            frac = 0.5 * (cs[i - 1] - cs[i + 1]) / den if den else 0.0
        out.append((s / fr, (i - M + frac) / fr * 1000.0, float(cs[i])))
    return out


def chroma_lags(Ca, Cs, act, frame_s, win=8.0, hop=4.0, maxlag=0.5, min_prominence=0.02, min_active=0.5):
    """Per window: the shift of the audio's chroma against the synthesis' that agrees best (ms, + = audio later) and
    how much better it is than the median shift (prominence); the chord changes time the window."""
    W, H, M = int(win / frame_s), int(hop / frame_s), int(maxlag / frame_s)
    m = min(Ca.shape[1], Cs.shape[1])
    out = []
    for s in range(M, m - W - M, H):
        sel = act[s:s + W]
        if sel.mean() < min_active:
            continue
        b = Cs[:, s:s + W]
        vals = np.array([float((Ca[:, s + L:s + L + W] * b).sum(axis=0)[sel].mean()) for L in range(-M, M + 1)])
        i = int(np.argmax(vals))
        frac = 0.0
        if 0 < i < len(vals) - 1:
            den = vals[i - 1] - 2 * vals[i] + vals[i + 1]
            frac = 0.5 * (vals[i - 1] - vals[i + 1]) / den if den else 0.0
        prom = float(vals[i] - np.median(vals))
        out.append((s * frame_s, (i - M + frac) * frame_s * 1000.0, prom, prom >= min_prominence))
    return out


def lag_stats(windows, valid_index=3):
    good = [w for w in windows if w[valid_index]]
    lags = np.array([w[1] for w in good]) if good else np.zeros(0)
    return {"windows": len(good), "windows_total": len(windows),
            "median_ms": round(float(np.median(lags)), 1) if len(lags) else None,
            "p5_ms": round(float(np.percentile(lags, 5)), 1) if len(lags) else None,
            "p95_ms": round(float(np.percentile(lags, 95)), 1) if len(lags) else None,
            "within_50ms": round(float(np.mean(np.abs(lags) <= 50)), 3) if len(lags) else None,
            "worst": [{"t": round(w[0], 1), "lag_ms": round(w[1], 1)}
                      for w in sorted(good, key=lambda w: -abs(w[1]))[:5]]}


def cmd_verify(args):
    ref = read_ref(args.ref)
    y = load(args.audio)
    syn = synthesize(ref)
    n = min(len(y), len(syn))
    Ca, loud_a = normalize(chroma(y[:n]))
    Cs, loud_s = normalize(chroma(syn[:n], 0.0))
    Cs = np.roll(Cs, args.key, axis=0)
    m = min(Ca.shape[1], Cs.shape[1])
    sim = (Ca[:, :m] * Cs[:, :m]).sum(axis=0)
    act = loud_s[:m] & loud_a[:m]
    # chance: the same frames against the synthesis shifted by a third of the song
    sh = max(1, m // 3)
    chance_v = (Ca[:, :m] * np.roll(Cs[:, :m], sh, axis=1)).sum(axis=0)
    chance = float(np.median(chance_v[act])) if act.any() else 0.5
    env_a, env_s = onset_env(y[:n]), onset_env(syn[:n])
    fr = SR / OHOP
    frames = HOP / SR
    measures = []
    for mm in ref["measures"]:
        a, b = int(mm["start"] / frames), int(np.ceil(mm["end"] / frames))
        a, b = max(a, 0), min(b, m)
        sel = act[a:b] if b > a else np.zeros(0, bool)
        c = float(sim[a:b][sel].mean()) if sel.any() else None
        agree = None if c is None else (c - chance) / max(1 - chance, 1e-6)
        # onset lag within the measure (+-0.5 s context)
        oa, ob = int(max(mm["start"] - 0.5, 0) * fr), int((mm["end"] + 0.5) * fr)
        M = int(0.15 * fr)
        lag = corr = None
        if ob - oa > 2 * M + 10 and oa - M >= 0 and ob + M < min(len(env_a), len(env_s)):
            bseg = env_s[oa:ob] - env_s[oa:ob].mean()
            nb = np.linalg.norm(bseg)
            if nb > 1e-6:
                cs = []
                for L in range(-M, M + 1):
                    x = env_a[oa + L:ob + L]
                    x = x - x.mean()
                    cs.append(np.dot(x, bseg) / (np.linalg.norm(x) * nb + 1e-12))
                i = int(np.argmax(cs))
                lag, corr = (i - M) / fr * 1000.0, float(cs[i])
        measures.append({"label": RM.measure_label(mm, ()), "number": mm["number"], "pass": mm.get("pass", 1),
                         "start": mm["start"], "end": mm["end"],
                         "chroma": None if agree is None else round(agree, 3),
                         "onset_corr": None if corr is None else round(corr, 3),
                         "lag_ms": None if lag is None else round(lag, 1)})
    wl = [w + (w[2] >= 0.3,) for w in windows_lag(env_a, env_s, fr)]
    cl = chroma_lags(Ca, Cs, act, frames)
    out = {"format": 1, "version": VERSION, "audio": os.path.abspath(args.audio), "key": args.key,
           "chance": round(chance, 4), "measures": measures,
           "lag": lag_stats(cl), "onset_lag": lag_stats(wl),
           "chroma_mean": round(float(np.mean([x["chroma"] for x in measures if x["chroma"] is not None])), 3)
           if any(x["chroma"] is not None for x in measures) else None}
    write_json(args.out, out)


# ------------------------------------------------------------------------------

def write_json(path, d):
    if not path:
        json.dump(d, sys.stdout, indent=1)
        print()
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("align")
    p.add_argument("--ref", required=True)
    p.add_argument("--audio", required=True)
    p.add_argument("--out")
    p.add_argument("--penalty", type=float, default=0.15, help="cost of a horizontal/vertical step, x median cost")
    p.add_argument("--tail", type=float, default=8.0, help="seconds of the recording kept after the song's end")
    p.add_argument("--no-refine", action="store_true", help="keep the coarse DTW map (no beat-level refinement)")
    p.set_defaults(fn=cmd_align)
    p = sub.add_parser("retime")
    p.add_argument("--ref", required=True)
    p.add_argument("--align", required=True)
    p.add_argument("--audio", required=True, help="the file to retime (the variant to deliver)")
    p.add_argument("--source", help="the file the alignment was made from, when --audio is another variant of it")
    p.add_argument("--assume-aligned", action="store_true",
                   help="--audio has --source's timing (a stem separated from it): no offset is measured")
    p.add_argument("--out", required=True)
    p.add_argument("--retune-cents", type=float, default=0.0)
    p.add_argument("--latency-cache")
    p.add_argument("--passes", type=int, default=5, help="stretch again with corrected targets up to this many times")
    p.set_defaults(fn=cmd_retime)
    p = sub.add_parser("verify")
    p.add_argument("--ref", required=True)
    p.add_argument("--audio", required=True)
    p.add_argument("--key", type=int, default=0)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("latency")
    p.add_argument("--out")
    p.set_defaults(fn=lambda a: write_json(a.out, measure_latency()))
    args = ap.parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
