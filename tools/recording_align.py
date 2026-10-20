#!/usr/bin/env python3
"""Alignment, retiming and verification of a recording against a song's timeline, for
``python3 -m pianovision recordings`` (``pianovision/recordings.py``).

Needs numpy, scipy, soundfile and librosa (not dependencies of the pianovision package): the command runs this
file with ``[recordings] python`` from pianovision.toml, e.g. a conda environment's Python.  ffmpeg decodes the
audio and time-stretches it (its ``rubberband`` filter, librubberband).

    recording_align.py align   --ref ref.json --audio REC [--out align.json]
    recording_align.py retime  --ref ref.json --align align.json --audio FILE [--source REC] --out out.wav
                               [--retune-cents C] [--latency-cache FILE]
    recording_align.py verify  --ref ref.json --audio out.wav [--key K] [--offset S] [--out verify.json]
    recording_align.py bars    --ref ref.json --align align.json --audio REC [--activations act.npz] --out bars.json
    recording_align.py vocals  --ref ref.json --audio vocals.mp3 [--active spans.json] [--out vocals.json]
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
up to 5 passes, until 95 % of the windows are within 8 ms (``passes``, ``fidelity_p95_ms``; the best pass is
kept).  ``--retune-cents``: also pitch-shifted by that many cents.

**verify.**  The retimed audio against the song's notes, on the song's clock: per measure the chroma agreement
(cosine similarity against the synthesis, rescaled so 0 is chance and 1 identical) and the onset lag (cross-
correlation of the onset envelopes within +-150 ms).  Timing over the whole song: ``lag``, the shift of the audio's
chroma against the synthesis' that agrees best in 8 s windows every 4 s (within +-500 ms; windows whose best shift
stands out by 0.02 count): the chord changes time it, which works for a band recording too; ``onset_lag``, the
same from the onset envelopes in 6 s windows (correlation 0.3 or more), reliable for piano and orchestra.

**bars** (``recordings tempo``: the recording leads).  Where the recording plays every measure as played: the
alignment's map as a coarse map (straight across its gaps, its tempo carried on before the first and after the last
piece), the recording's beats detected (madmom's DBN beats from ``--activations``, ``tools/beat_activations.py``;
librosa's beat tracker with the coarse map's tempo frame by frame), each detection on the score's lattice of beats
(the phase they share snapped to the beat, half beat or third: :func:`lattice_of`) by a Viterbi path between the
coarse map and a steady tempo (:func:`assign_lattice`), and every position's time from those anchors with a
confidence per measure (:func:`cmd_bars`).  A measure of another length than its time signature counts the
signature's beats in the recording.

**vocals.**  Retimed vocals against the score's vocal line (:func:`cmd_vocals`): melody agreement (pYIN pitch against
the vocal note, by pitch class) at the plan's timing and the shift that agrees best, overall and per 10 s window;
the vocal notes' starts against the stem's onsets; the stem's level where the vocal line rests (bleed).
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

VERSION = 3                 # bump when align / retime / verify change their results (bars: BARS_VERSION)
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
    off = getattr(args, "offset", 0.0) or 0.0
    if off:
        # the audio as Note Waterfall plays it: file time = song time + offset
        k = int(round(abs(off) * SR))
        y = y[k:] if off > 0 else np.concatenate([np.zeros(k, np.float32), y])
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
# vocals: the singing against the score's vocal line
# ------------------------------------------------------------------------------

def vocal_notes(ref, part="v"):
    return sorted(n for n in ref["notes"] if n[4] == part)


def sounding_mask(notes, n_frames, frame_s, pad=0.0):
    m = np.zeros(n_frames, bool)
    for st, du, *_ in notes:
        a, b = int((st - pad) / frame_s), int(np.ceil((st + max(du, 0.05) + pad) / frame_s))
        m[max(a, 0):min(b, n_frames)] = True
    return m


def _r(v, nd=3):
    return None if v is None else round(float(v), nd)


def pitch_track(y, n):
    """MIDI pitch per chroma frame (pYIN; NaN where unvoiced)."""
    import librosa
    f0, voiced, _p = librosa.pyin(y, fmin=70.0, fmax=1100.0, sr=SR, hop_length=HOP, frame_length=1024)
    f0 = np.asarray(f0)[:n]
    voiced = np.asarray(voiced)[:n] & np.isfinite(f0)
    midi = np.full(n, np.nan)
    midi[:len(f0)][voiced] = librosa.hz_to_midi(f0[voiced])
    return midi


def melody_curve(midi, want, active, lo, hi, M, min_frames=20):
    """Share of frames lo..hi where the score's vocal note sounds, the track is voiced at frame + shift, and the pitch
    classes agree (within half a semitone, octaves alike), for shifts -M..M frames; and the best shift: the middle of
    the shifts within 0.01 of the best share (a plateau's centre, not its first edge)."""
    n = len(midi)
    curve = {}
    for k in range(-M, M + 1):
        idx = np.arange(lo, hi)
        j = idx + k
        ok = (j >= 0) & (j < n)
        idx, j = idx[ok], j[ok]
        sel = np.isfinite(want[idx]) & np.isfinite(midi[j]) & active[idx]
        if sel.sum() < min_frames:
            curve[k] = None
            continue
        d = np.abs(((midi[j][sel] - want[idx][sel]) + 6.0) % 12.0 - 6.0)
        curve[k] = float(np.mean(d <= 0.5))
    vals = {k: v for k, v in curve.items() if v is not None}
    if not vals:
        return curve, None
    top = max(vals.values())
    near = [k for k, v in vals.items() if v >= top - 0.01]
    return curve, int(round(float(np.median(near))))


def cmd_vocals(args):
    """The retimed vocals against the score's vocal line (``part`` "v" of ref.json):

    * **melody**: the stem's pitch (pYIN, 23 ms frames, voiced frames only) against the vocal note sounding at that
      moment, pitch class within half a semitone (an octave off counts as agreeing); the share of agreeing frames at
      the plan's timing and with the stem shifted by -300..+300 ms, the shift that agrees best (+ = the singing is
      later than the score), and the same per 10 s window (median, p10, p90 of the windows' best shifts).  The
      shifts are corrected by the measurement's own bias: the same measure on the vocal line synthesised in time;
    * **onsets**: each vocal note's start against the nearest onset detected in the stem within 200 ms;
    * **bleed**: the stem's level where the score's vocal line is silent (0.5 s margin) against where it sings,
      inside ``--active`` song spans (the parts of the song the recording plays)."""
    import librosa
    ref = read_ref(args.ref)
    notes = vocal_notes(ref)
    y = load(args.audio)
    fs = HOP / SR
    n = len(y) // HOP + 1
    out = {"format": 1, "version": VERSION, "audio": os.path.abspath(args.audio), "vocal_notes": len(notes)}
    if not notes:
        out["error"] = "the score has no vocal line"
        write_json(args.out, out)
        return
    active = np.ones(n, bool)
    if args.active:
        with open(args.active, encoding="utf-8") as f:
            spans = json.load(f)
        active[:] = False
        for a, b in spans:
            active[int(a / fs):int(np.ceil(b / fs))] = True
    # melody: the stem, and the score's vocal line synthesised (the measurement's own bias: pYIN's window makes a
    # pitch appear late and linger)
    want = np.full(n, np.nan)
    for st, du, pitch, *_ in notes:
        a, b = int(st / fs), int(np.ceil((st + max(du, 0.05)) / fs))
        want[max(a, 0):min(b, n)] = pitch
    M = int(round(0.3 / fs))
    syn = synthesize({"song_end": len(y) / SR, "notes": [[nn[0], nn[1], nn[2], nn[3], "o"] for nn in notes]})[:len(y)]
    m_stem, m_syn = pitch_track(y, n), pitch_track(syn, n)
    curve, best = melody_curve(m_stem, want, active, 0, n, M)
    curve_s, best_s = melody_curve(m_syn, want, active, 0, n, M)
    bias = best_s if best_s is not None else 0
    windows = []
    W = int(10.0 / fs)
    for lo in range(0, n - W, W // 2):
        c, kb = melody_curve(m_stem, want, active, lo, lo + W, M, min_frames=60)
        if kb is None or c[kb] - np.median([v for v in c.values() if v is not None]) < 0.05:
            continue                                        # too little sung, or no clear best shift
        windows.append((lo * fs, (kb - bias) * fs * 1000.0, c[kb]))
    wl = np.array([w[1] for w in windows]) if windows else np.zeros(0)
    sungm = np.isfinite(want) & active
    out["melody"] = {"agree_at_plan": _r(curve.get(0)),
                     "best_shift_ms": None if best is None else round((best - bias) * fs * 1000.0, 1),
                     "agree_at_best": _r(curve.get(best)) if best is not None else None,
                     "measurement_bias_ms": round(bias * fs * 1000.0, 1),
                     "voiced_where_the_score_sings": round(float(np.mean(np.isfinite(m_stem[sungm]))), 3)
                     if sungm.any() else None,
                     "windows": len(windows),
                     "window_shift_ms": {"median": round(float(np.median(wl)), 1) if len(wl) else None,
                                         "p10": round(float(np.percentile(wl, 10)), 1) if len(wl) else None,
                                         "p90": round(float(np.percentile(wl, 90)), 1) if len(wl) else None,
                                         "within_50ms": round(float(np.mean(np.abs(wl) <= 50)), 3) if len(wl)
                                         else None}}
    # onsets
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=OHOP)
    on = librosa.onset.onset_detect(onset_envelope=env, sr=SR, hop_length=OHOP, units="time", backtrack=True)
    starts = sorted({round(nn[0], 3) for nn in notes})
    afs = np.ones(len(starts), bool)
    if args.active:
        afs = np.array([active[min(int(t / fs), n - 1)] for t in starts])
    d = []
    for t, a in zip(starts, afs):
        if not a or not len(on):
            continue
        j = int(np.argmin(np.abs(on - t)))
        if abs(on[j] - t) <= 0.2:
            d.append(on[j] - t)
    d = np.array(d) * 1000.0
    considered = int(afs.sum())
    out["onsets"] = {"notes": considered, "matched": int(len(d)),
                     "median_ms": round(float(np.median(d)), 1) if len(d) else None,
                     "within_50ms": round(float(np.mean(np.abs(d) <= 50)), 3) if len(d) else None,
                     "within_100ms": round(float(np.mean(np.abs(d) <= 100)), 3) if len(d) else None}
    # bleed: the stem's level where the vocal line rests
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0][:n]
    sung = sounding_mask(notes, n, fs) & active
    rest = ~sounding_mask(notes, n, fs, pad=0.5) & active
    def level(m):
        return round(float(10 * np.log10(np.mean(rms[m] ** 2) + 1e-12)), 1) if m.any() else None
    ls, lr = level(sung), level(rest)
    loud = rms > 10 ** (-40 / 20)
    out["bleed"] = {"sung_db": ls, "rest_db": lr, "rest_minus_sung_db": None if ls is None or lr is None else
                    round(lr - ls, 1), "rest_seconds": round(float(rest.sum() * fs), 1),
                    "rest_active_share": round(float(np.mean(loud[rest])), 3) if rest.any() else None,
                    "sung_active_share": round(float(np.mean(loud[sung])), 3) if sung.any() else None}
    write_json(args.out, out)


# ------------------------------------------------------------------------------
# bars: where the recording plays each of the song's measures (the recording leads)
# ------------------------------------------------------------------------------

BARS_VERSION = 1
SIGMA_COARSE = 0.2          # beats: how far the alignment may put a detected beat from its place in the score
SIGMA_LOOSE = 0.6           # ... in and next to the measures of other lengths and the alignment's gaps
SIGMA_TEMPO = 0.12          # beats per beat of interval: how much the tempo may change from one detection to the next
SKIP_COST = 5.0             # a detection left out (an extra beat, a fill)
AGREE_S = 0.05              # two detectors agree on a beat within this
STEADY_SPAN = 4.0           # beats: between anchors this close, positions follow a steady tempo


def coarse_map(tm, song_end, lead_s=8.0):
    """(song, recording) anchors covering the whole song, increasing in both: the pieces' anchors; straight across
    the gaps between pieces (a passage the recording plays shorter or longer); before the first piece and after the
    last the neighbouring piece's tempo (over up to ``lead_s`` seconds) carried on: the recording's beat before its
    first match (an intro) and after its last.  Recording times may be negative (the song starts before the file)."""
    pts = []
    for p in tm.pieces:
        for s, r in p.anchors:
            if pts and (s <= pts[-1][0] + 1e-6 or r <= pts[-1][1] + 1e-6):
                continue
            pts.append((float(s), float(r)))
    if not pts:
        return np.array([0.0, song_end]), np.array([0.0, song_end])

    def ratio(seg):
        s0, r0 = seg[0]
        s1, r1 = seg[-1]
        return (r1 - r0) / (s1 - s0) if s1 > s0 else 1.0

    head = [a for a in pts if a[0] <= pts[0][0] + lead_s]
    tail = [a for a in pts if a[0] >= pts[-1][0] - lead_s]
    if pts[0][0] > 1e-6:
        pts.insert(0, (0.0, pts[0][1] - pts[0][0] * ratio(head if len(head) > 1 else pts)))
    if pts[-1][0] < song_end - 1e-6:
        pts.append((song_end, pts[-1][1] + (song_end - pts[-1][0]) * ratio(tail if len(tail) > 1 else pts)))
    S = np.array([a[0] for a in pts])
    R = np.array([a[1] for a in pts])
    return S, R


class BeatSpace:
    """The song's measures numbered in the recording's beats: a measure of the score's length counts its own beats; a
    measure whose length is not its time signature's (a 5/4 bar in 4/4) counts its time signature's beats, a plain
    bar on the recording (``estimate``: the beats its length on the coarse map holds at the beat of the regular
    measures around it, reported where that differs), and positions inside it follow the coarse map.  ``B``: a
    position in recording beats from the song's start.  ``loose``: per measure, whether the coarse map is less sure
    there (a measure of another length or next to one, a measure in one of the alignment's gaps)."""

    def __init__(self, measures, S, R, gaps=(), estimate=False, gap_beats=True):
        self.ms = measures
        self.S, self.R = S, R
        n = len(measures)
        self.r0 = np.array([self.rec(m["start"]) for m in measures])
        self.r1 = np.array([self.rec(m["end"]) for m in measures])
        self.n_score = np.array([max(1, len(m.get("beats") or [m["start"]])) for m in measures])
        odd = np.array([bool(m.get("odd")) for m in measures])
        self.odd = odd
        per = (self.r1 - self.r0) / self.n_score
        nrec = self.n_score.astype(float).copy()
        self.period_est = np.zeros(n)
        self.n_coarse = np.zeros(n, int)
        for j in range(n):
            near = [per[k] for k in range(max(0, j - 4), min(n, j + 5)) if not odd[k] and per[k] > 0]
            p = float(np.median(near)) if near else float(np.median(per[per > 0])) if (per > 0).any() else 0.5
            self.period_est[j] = p
            self.n_coarse[j] = max(0, int(round((self.r1[j] - self.r0[j]) / max(p, 1e-6))))
            if odd[j]:
                nom = measures[j].get("nominal") or measures[j].get("timesig") or [4, 4]
                beats = nom[0] // 3 if nom[1] == 8 and nom[0] % 3 == 0 and nom[0] > 3 else nom[0]
                nrec[j] = self.n_coarse[j] if estimate else max(1, int(beats))
        self.loose = np.zeros(n, bool)
        self.in_gap = np.zeros(n, bool)
        for j in range(n):
            if odd[max(0, j - 1):j + 2].any():
                self.loose[j] = True
            m = measures[j]
            for g in gaps:
                if g["song"][0] < m["end"] - 1e-3 and g["song"][1] > m["start"] + 1e-3 and not g.get("edge") and \
                        g["kind"] in ("song_missing", "recording_extra", "different"):
                    self.in_gap[j] = True
            # a regular measure in a gap whose length on the coarse map is a beat or more off its own: the recording
            # plays it with another number of beats
            # (not next to a measure of another length: that one's beats are what the coarse map spreads around)
            if gap_beats and self.in_gap[j] and not odd[max(0, j - 1):j + 2].any() and \
                    abs(self.n_coarse[j] - nrec[j]) >= 1:
                nrec[j] = max(1, self.n_coarse[j])
                self.loose[j] = True
        self.n_rec = nrec.astype(int)
        # positions inside spread over the recording's beats as the coarse map has them
        self.scaled = odd | (self.n_rec != self.n_score)
        self.b0 = np.concatenate([[0.0], np.cumsum(self.n_rec)])[:-1]
        self.song_starts = np.array([m["start"] for m in measures])

    def rec(self, s):
        return float(np.interp(s, self.S, self.R, left=self.R[0] + (s - self.S[0]), right=self.R[-1] + s - self.S[-1]))

    def song(self, r):
        return float(np.interp(r, self.R, self.S, left=self.S[0] + (r - self.R[0]), right=self.S[-1] + r - self.R[-1]))

    def measure_of_song(self, s):
        return int(np.clip(np.searchsorted(self.song_starts, s, side="right") - 1, 0, len(self.ms) - 1))

    def b_of_position(self, j, rtick, song_t):
        """B of a position ``rtick`` ticks into measure j (song time ``song_t`` on the score's own clock)."""
        m = self.ms[j]
        if self.n_rec[j] == 0:
            return float(self.b0[j])
        if self.scaled[j]:
            span = self.r1[j] - self.r0[j]
            f = (self.rec(song_t) - self.r0[j]) / span if span > 1e-9 else rtick / max(m["ticks"], 1)
            return float(self.b0[j] + self.n_rec[j] * min(max(f, 0.0), 1.0))
        return float(self.b0[j] + rtick / max(m["beat_ticks"], 1))

    def b_of_rec(self, r):
        """B of a recording time through the coarse map (the score's positions in between by its own clock)."""
        s = self.song(r)
        j = self.measure_of_song(s)
        m = self.ms[j]
        if self.n_rec[j] == 0:
            return float(self.b0[j])
        if self.scaled[j]:
            span = self.r1[j] - self.r0[j]
            f = (r - self.r0[j]) / span if span > 1e-9 else 0.0
            return float(self.b0[j] + self.n_rec[j] * f)
        pos = m.get("positions") or [[0, m["start"]]]
        ts = [p[1] for p in pos] + [m["end"]]
        tk = [p[0] for p in pos] + [m["ticks"]]
        rt = float(np.interp(s, ts, tk, left=0.0, right=float(m["ticks"])))
        if s > m["end"]:
            rt = m["ticks"] + (s - m["end"]) / max(m["end"] - m["start"], 1e-9) * m["ticks"]
        return float(self.b0[j] + rt / max(m["beat_ticks"], 1))

    def coarse_rec_of_b(self, B):
        """The coarse map's recording time of B."""
        total = float(self.b0[-1] + self.n_rec[-1])
        if B <= 0:
            return self.r0[0] + B * self.period_est[0]
        if B >= total:
            return self.r1[-1] + (B - total) * self.period_est[-1]
        j = int(np.clip(np.searchsorted(self.b0, B, side="right") - 1, 0, len(self.ms) - 1))
        while j > 0 and self.n_rec[j] == 0:
            j -= 1
        f = (B - self.b0[j]) / max(self.n_rec[j], 1)
        if self.scaled[j]:
            return float(self.r0[j] + f * (self.r1[j] - self.r0[j]))
        m = self.ms[j]
        pos = m.get("positions") or [[0, m["start"]]]
        tk = [p[0] for p in pos] + [m["ticks"]]
        ts = [p[1] for p in pos] + [m["end"]]
        return self.rec(float(np.interp(f * m["ticks"], tk, ts)))

    def period_at(self, B):
        return float((self.coarse_rec_of_b(B + 2.0) - self.coarse_rec_of_b(B - 2.0)) / 4.0)


def lattice_of(Bs, compound, window=16):
    """The lattice the detected beats sit on: their spacing (the score's beats, or half beats / thirds when a
    detector follows the subdivision, or every other beat) from the median step between consecutive detections, and
    the phase: the circular mean of the detections' places snapped to the nearest musical phase (on the beat, the
    half beat, a third in compound time: a transcription an eighth off the recording's pulse has its detections all
    on its off-beats), the whole song's (a region's own only where the song has no clear phase and the region a
    clear one: a coarse map a quarter beat off for a while must not move a whole region onto the off-beats).
    Returns (spacing, phases, how clear each region is: the mean resultant length 0..1)."""
    Bs = np.asarray(Bs, float)
    dB = np.diff(Bs)
    dB = dB[(dB > 0.15) & (dB < 4.0)]
    med = float(np.median(dB)) if len(dB) else 1.0
    options = [1.0 / 3, 1.0, 2.0] if compound else [0.5, 1.0, 2.0]
    spacing = min(options, key=lambda o: abs(np.log(med / o)))
    base = [0.0, 1.0 / 3, 2.0 / 3] if compound else [0.0, 0.5]
    allowed = sorted({(p + k) for p in base for k in range(3) if p + k < spacing - 1e-9} | {0.0})

    def snap(ph):
        return min(allowed + [spacing], key=lambda a: abs(a - ph)) % spacing

    ang = np.exp(2j * np.pi * (Bs / spacing))
    whole = ang.mean() if len(ang) else 1.0
    g_ph = snap((np.angle(whole) / (2 * np.pi)) % 1.0 * spacing)
    phases, clear = np.full(len(Bs), g_ph), np.zeros(len(Bs))
    for i in range(len(Bs)):
        w = ang[max(0, i - window):i + window + 1].mean()
        clear[i] = abs(w)
        # a region keeps the song's phase unless the song has none and the region a clear one of its own
        if abs(whole) < 0.3 <= 0.7 <= abs(w):
            phases[i] = snap((np.angle(w) / (2 * np.pi)) % 1.0 * spacing)
    return spacing, phases, clear


def assign_lattice(d, Bs, spacing, phases, period, sigma=None):
    """Each detected beat (times ``d``, coarse places ``Bs``) on a point of its lattice (``phases`` + k ``spacing``
    beats) or left out: a Viterbi path over the points within three sigmas of the coarse place, costing the distance
    from it (``sigma``: :data:`SIGMA_COARSE`, :data:`SIGMA_LOOSE` where the coarse map is less sure) and, between
    consecutive kept detections, the change of tempo against ``period`` (recording seconds per beat;
    :data:`SIGMA_TEMPO`), so a run of steady beats settles a place the coarse map leaves in doubt.  Returns per
    detection the lattice point or None."""
    n = len(d)
    sigma = np.full(n, SIGMA_COARSE) if sigma is None else np.asarray(sigma)
    cands, ecost = [], []
    for i in range(n):
        reach = max(3.0 * sigma[i], 0.55 * spacing)
        k0 = int(np.floor((Bs[i] - phases[i] - reach) / spacing))
        cs = [phases[i] + k * spacing for k in range(k0, k0 + int(2 * reach / spacing) + 3)
              if abs(phases[i] + k * spacing - Bs[i]) <= reach]
        cands.append(np.array(cs))
        ecost.append(((np.array(cs) - Bs[i]) / sigma[i]) ** 2)
    INF = 1e18
    cost = [None] * n
    back = [None] * n
    for i in range(n):
        c = ecost[i] + SKIP_COST * i                                  # start here (the ones before left out)
        bk = [(-1, -1)] * len(cands[i])
        c = c.copy()
        for gap in (1, 2, 3):
            ip = i - gap
            if ip < 0:
                break
            dt = d[i] - d[ip]
            for a, ca in enumerate(cands[i]):
                db = ca - cands[ip]
                exp = dt / max(period[i], 1e-3)
                sig = SIGMA_TEMPO * max(1.0, exp)
                t = np.where(db > 1e-6, ((db - exp) / sig) ** 2, INF)
                tot = cost[ip] + t + SKIP_COST * (gap - 1)
                b = int(np.argmin(tot))
                if tot[b] + ecost[i][a] < c[a]:
                    c[a] = tot[b] + ecost[i][a]
                    bk[a] = (ip, b)
        cost[i], back[i] = c, bk
    out = [None] * n
    if not n:
        return out
    # end: the best final state, the detections after it left out
    best, bi, ba = INF, -1, -1
    for i in range(n):
        if len(cost[i]):
            a = int(np.argmin(cost[i]))
            tot = cost[i][a] + SKIP_COST * (n - 1 - i)
            if tot < best:
                best, bi, ba = tot, i, a
    while bi >= 0 and ba >= 0:
        out[bi] = float(cands[bi][ba])
        bi, ba = back[bi][ba]
    return out


def local_period(det, Bs, spacing, bs, window=8):
    """Recording seconds per beat around each detection: the median interval between consecutive detections about
    one lattice step apart on the coarse map (+-``window``), over the step; the coarse map's where there are none."""
    dt = np.diff(det)
    dB = np.diff(Bs)
    ok = (dB > 0.6 * spacing) & (dB < 1.4 * spacing)
    out = np.zeros(len(det))
    for i in range(len(det)):
        lo, hi = max(0, i - window), min(len(dt), i + window)
        sel = [dt[k] for k in range(lo, hi) if ok[k]]
        out[i] = float(np.median(sel)) / spacing if len(sel) >= 3 else bs.period_at(Bs[i])
    return out


def detections_on_lattice(det, bs, compound, lo, hi):
    """Detected beats (times) between recording times lo..hi placed on the lattice: [(B, time, residual)]."""
    det = np.asarray([t for t in det if lo <= t <= hi])
    if not len(det):
        return [], {"detections": 0}
    Bs = np.array([bs.b_of_rec(t) for t in det])
    keep = np.concatenate([[True], np.diff(Bs) > 1e-6])                  # the coarse map stands still: the first
    det, Bs = det[keep], Bs[keep]
    spacing, phases, clear = lattice_of(Bs, compound)
    period = local_period(det, Bs, spacing, bs)
    sigma = np.array([SIGMA_LOOSE if bs.loose[bs.measure_of_song(bs.song(t))] else SIGMA_COARSE for t in det])
    q = assign_lattice(det, Bs, spacing, phases, period, sigma)
    out = [(b, float(t), float(B - b)) for t, B, b in zip(det, Bs, q) if b is not None]
    vals, counts = np.unique(np.round(phases, 3), return_counts=True)
    return out, {"detections": int(len(det)), "kept": len(out), "spacing_beats": round(spacing, 3),
                 "phases": {str(v): int(c) for v, c in zip(vals, counts)},
                 "phase_clarity_median": round(float(np.median(clear)), 3),
                 "residual_beats_median": round(float(np.median([abs(x[2]) for x in out])), 3) if out else None}


def cmd_bars(args):
    """Where the recording plays every measure of the song as played (module docstring of pianovision.rectempo):
    the alignment's coarse map, the recording's beats detected (madmom's activations from ``--activations``, else
    none; librosa's beat tracker with the tempo the alignment implies), each detected beat put on the score's
    lattice of beats, half beats (thirds in compound time), and every measure position's time from those anchors."""
    import librosa
    ref = read_ref(args.ref)
    al = read_ref(args.align)
    tm = RM.TimeMap.from_dict(al["map"])
    ms = ref["measures"]
    song_end = float(ms[-1]["end"])
    y = load(args.audio)
    rec_end = len(y) / SR
    S, R = coarse_map(tm, song_end)
    global SIGMA_TEMPO
    if args.sigma_tempo:
        SIGMA_TEMPO = args.sigma_tempo
    bs = BeatSpace(ms, S, R, al.get("gaps") or [], estimate=args.estimate_odd, gap_beats=not args.no_gap_beats)
    total_b = float(bs.b0[-1] + bs.n_rec[-1])
    compound = all((m.get("timesig") or [4, 4])[1] == 8 and (m.get("timesig") or [4, 4])[0] % 3 == 0
                   for m in ms if not m.get("odd"))
    # librosa: onset envelope (5.8 ms) and its beat tracker with the tempo the coarse map implies, frame by frame
    H = OHOP
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=H)
    fr = SR / H
    tf = np.arange(len(env)) / fr
    gb = np.arange(-4.0, total_b + 4.0, 0.5)
    gr = np.array([bs.coarse_rec_of_b(b) for b in gb])
    per = np.gradient(gr, gb)
    ok = per > 0.15
    bpm = 60.0 / np.clip(np.interp(tf, gr[ok], per[ok]), 0.25, 2.0)
    _t, lb = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=H, bpm=bpm, tightness=100, trim=False,
                                     units="time")
    det = {"librosa": np.asarray(lb, float)} if not args.coarse_only else {}
    act = None
    if args.activations and os.path.isfile(args.activations) and not args.coarse_only:
        z = np.load(args.activations)
        det["madmom"] = np.asarray(z["beats"], float)
        act = np.asarray(z["beat"], float)
    primary = "madmom" if "madmom" in det else "librosa" if det else "none"
    lo, hi = max(0.0, bs.coarse_rec_of_b(-1.0)), min(rec_end, bs.coarse_rec_of_b(total_b + 1.0))
    placed, stats = {}, {}
    for name, times in det.items():
        placed[name], stats[name] = detections_on_lattice(times, bs, compound, lo, hi)
        stats[name]["total"] = int(len(times))
    # agreement and the detectors' relative bias (the other one against the primary)
    anchors = {round(b, 4): (t, e) for b, t, e in placed.get(primary, [])}
    other = [k for k in placed if k != primary]
    confirm = {}
    bias = None
    if other:
        o = {round(b, 4): t for b, t, _e in placed[other[0]]}
        diffs = [o[k] - anchors[k][0] for k in anchors if k in o and abs(o[k] - anchors[k][0]) <= AGREE_S]
        bias = float(np.median(diffs)) if diffs else 0.0
        for k in anchors:
            confirm[k] = k in o and abs(o[k] - bias - anchors[k][0]) <= AGREE_S
        stats["agreement"] = round(float(np.mean(list(confirm.values()))), 3) if confirm else None
        stats["bias_ms"] = round(bias * 1000.0, 1)
    keys = sorted(anchors)
    AB = np.array(keys)
    AT = np.array([anchors[k][0] for k in keys])
    AE = np.array([anchors[k][1] for k in keys])
    AC = np.array([confirm.get(k, False) for k in keys])
    if args.activations and act is not None and len(AT):
        idx = np.clip(np.round(AT * 100).astype(int), 0, len(act) - 1)
        AS = act[idx]
    else:
        AS = np.ones(len(AT))
    resid = AT - np.array([bs.coarse_rec_of_b(b) for b in AB]) if len(AB) else np.zeros(0)

    def rec_of_b(B):
        """Between anchors at most :data:`STEADY_SPAN` beats apart: a steady tempo from one to the next; elsewhere
        the coarse map's shape moved by the anchors' residuals."""
        if not len(AB):
            return bs.coarse_rec_of_b(B)
        i = int(np.searchsorted(AB, B))
        if 0 < i < len(AB) and AB[i] - AB[i - 1] <= STEADY_SPAN + 1e-6:
            f = (B - AB[i - 1]) / (AB[i] - AB[i - 1])
            return float(AT[i - 1] + f * (AT[i] - AT[i - 1]))
        if i < len(AB) and abs(AB[i] - B) < 1e-9:
            return float(AT[i])
        return bs.coarse_rec_of_b(B) + float(np.interp(B, AB, resid))

    def conf_of_b(B, t):
        if t < 0.0 or t > rec_end:
            return 0.0, "outside"
        if not len(AB):
            return 0.2, "coarse"
        i = int(np.searchsorted(AB, B))
        cands = [k for k in (i - 1, i) if 0 <= k < len(AB)]
        k = min(cands, key=lambda q: abs(AB[q] - B))
        dist = abs(AB[k] - B)

        def qual(q):
            return (1.0 if AC[q] or not other else 0.75) * max(0.0, 1.0 - abs(AE[q]) / 0.5) * \
                (0.6 + 0.4 * min(1.0, float(AS[q]) / 0.3))
        if dist < 1e-3:
            return round(qual(k), 3), "detected"
        lo_k = i - 1 if i - 1 >= 0 else None
        hi_k = i if i < len(AB) else None
        if lo_k is not None and hi_k is not None and AB[hi_k] - AB[lo_k] <= 2.0 + 1e-6:
            return round(0.8 * min(qual(lo_k), qual(hi_k)), 3), "between"
        return round(0.4 * float(np.exp(-max(0.0, dist - 1.0) / 2.0)), 3), "coarse"

    bars = []
    for j, m in enumerate(ms):
        Bst = float(bs.b0[j])
        Ben = Bst + float(bs.n_rec[j])
        t0, t1 = rec_of_b(Bst), rec_of_b(Ben)
        pos_out = []
        for rt, st in (m.get("positions") or [[0, m["start"]]]):
            B = bs.b_of_position(j, rt, st)
            pos_out.append([int(rt), round(rec_of_b(B), 5)])
        beats = [round(rec_of_b(Bst + k), 5) for k in range(int(bs.n_rec[j]))]
        bc = [conf_of_b(Bst + k, rec_of_b(Bst + k))[0] for k in range(int(bs.n_rec[j]))]
        c0, src = conf_of_b(Bst, t0)
        bars.append({"index": j, "number": m["number"], "pass": m.get("pass", 1), "odd": bool(m.get("odd")),
                     "beats_score": int(bs.n_score[j]), "beats_recording": int(bs.n_rec[j]),
                     "beats_coarse": int(bs.n_coarse[j]), "loose": bool(bs.loose[j]),
                     "start": round(t0, 5), "end": round(t1, 5), "coarse_start": round(bs.r0[j], 5),
                     "conf": c0, "source": src, "beat_conf": bc, "beats": beats, "positions": pos_out})
    out = {"format": 1, "version": BARS_VERSION, "audio": os.path.abspath(args.audio), "duration": round(rec_end, 3),
           "song_end": song_end, "compound": compound, "primary": primary, "detectors": stats,
           "anchors": int(len(AB)), "anchors_confirmed": int(AC.sum()) if len(AC) else 0,
           "anchor_residual_beats_median": round(float(np.median(np.abs(AE))), 3) if len(AE) else None,
           "coarse_minus_detected_ms_median": round(float(-np.median(resid)) * 1000.0, 1) if len(resid) else None,
           "coarse_map": [[round(float(a), 4), round(float(b), 4)] for a, b in zip(S, R)],
           "bars": bars}
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
    p.add_argument("--offset", type=float, default=0.0,
                   help="the audio is played from this file time at the song's start (Note Waterfall's offset)")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("vocals")
    p.add_argument("--ref", required=True)
    p.add_argument("--audio", required=True, help="the vocals on the song's timeline (the delivered mp3)")
    p.add_argument("--active", help="JSON [[from, to], ...]: the song spans the recording plays")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_vocals)
    p = sub.add_parser("bars")
    p.add_argument("--ref", required=True, help="the song's measures as played, with their positions (rectempo)")
    p.add_argument("--align", required=True)
    p.add_argument("--audio", required=True)
    p.add_argument("--activations", help="madmom's beats and activations (tools/beat_activations.py)")
    p.add_argument("--coarse-only", action="store_true", help="no beat detection: the alignment's map alone")
    p.add_argument("--sigma-tempo", type=float, help=f"tempo change allowed per beat (default {SIGMA_TEMPO})")
    p.add_argument("--no-gap-beats", action="store_true",
                   help="a regular measure in an alignment gap keeps its own number of beats")
    p.add_argument("--estimate-odd", action="store_true",
                   help="a measure of another length counts the beats the coarse map gives it, not its time "
                        "signature's")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_bars)
    p = sub.add_parser("latency")
    p.add_argument("--out")
    p.set_defaults(fn=lambda a: write_json(a.out, measure_latency()))
    args = ap.parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
