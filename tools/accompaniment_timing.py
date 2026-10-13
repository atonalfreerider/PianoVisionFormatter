#!/usr/bin/env python3
"""Timing and codec checks of a rendered accompaniment, for ``python3 -m pianovision accompaniment``.

Needs numpy, soundfile and librosa (not dependencies of the pianovision package): the command runs this file
with ``[accompaniment] timing_python`` from pianovision.toml, e.g. a conda environment's Python.

    accompaniment_timing.py --wav render.wav --onsets onsets.json [--mp3 out.mp3 --gain-db G] [--out result.json]

``onsets.json``: ``{"times": [seconds, ...]}``, the note-on times of the staves that sound in the render, on the
song's clock (the formatter's MIDI with the tempo map of every track's set_tempo events).

* **xcorr**: the render's onset-strength envelope against the onsets (a 10 ms Gaussian train), cross-correlated
  in 20 s windows every 10 s, lags within +-300 ms; + = the audio is later than the notes.  Windows with a
  correlation of at least 0.2 and 8 onsets count.  Reports the median, p5 and p95 of the lags and the drift (the
  slope of a straight line through them).  ``method_bias_ms`` is what the same analysis measures on clicks at
  the onset times (0 means the envelope adds no lag of its own).
* **matched**: every onset matched to the nearest detected onset within 50 ms; median difference.
* **mp3** (with ``--mp3``): where the mp3's audio lands against the WAV it was encoded from (times ``--gain-db``),
  decoded once as ffmpeg does by default (it honours a LAME gapless header when there is one), once without any
  gapless handling (``-flags2 +skip_manual``: every frame decoded and concatenated), and once from a frame in the
  middle of the file, as a player fed the frames from there on (what Note Waterfall serves a Sonos): that one
  should land at the frame's index times 1152 samples.
"""

import argparse
import json
import subprocess
import sys

import numpy as np
import soundfile as sf

SR = 22050
HOP = 64
NFFT = 1024


def envelope(y, sr):
    import librosa
    if y.ndim > 1:
        y = y.mean(axis=1)
    y = librosa.resample(np.ascontiguousarray(y, dtype=np.float32), orig_sr=sr, target_sr=SR)
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP, n_fft=NFFT, max_size=3)
    return env / (env.max() + 1e-12)


def train(times, n, fr, sigma_s=0.010):
    t = np.zeros(n)
    idx = np.round(np.asarray(times) * fr).astype(int)
    idx = idx[(idx >= 0) & (idx < n)]
    np.add.at(t, idx, 1.0)
    half = int(4 * sigma_s * fr)
    g = np.exp(-0.5 * (np.arange(-half, half + 1) / (sigma_s * fr)) ** 2)
    return np.convolve(t, g, "same")


def xcorr(env, tr, times, fr, win=20.0, hop=10.0, maxlag=0.3, min_corr=0.2, min_onsets=8):
    """Per window (start s, lag ms, corr, onsets): lag L maximises sum_n env[n + L] * tr[n] (audio later = +)."""
    W, H, M = int(win * fr), int(hop * fr), int(maxlag * fr)
    times = np.asarray(times)
    out = []
    for s in range(M, len(env) - W - M, H):
        b = tr[s:s + W] - tr[s:s + W].mean()
        k = int(((times >= s / fr) & (times < (s + W) / fr)).sum())
        if k < 2 or not b.any():
            continue
        cs = []
        for L in range(-M, M + 1):
            a = env[s + L:s + L + W]
            a = a - a.mean()
            cs.append(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
        cs = np.array(cs)
        i = int(np.argmax(cs))
        frac = 0.0
        if 0 < i < len(cs) - 1:                      # parabolic peak
            den = cs[i - 1] - 2 * cs[i] + cs[i + 1]
            frac = 0.5 * (cs[i - 1] - cs[i + 1]) / den if den else 0.0
        out.append((s / fr, (i - M + frac) / fr * 1000.0, float(cs[i]), k))
    good = [w for w in out if w[2] >= min_corr and w[3] >= min_onsets]
    return out, good


def summarize(windows, good):
    if not good:
        return {"windows": 0, "windows_total": len(windows), "median_ms": None, "p5_ms": None, "p95_ms": None,
                "drift_ms_per_min": None}
    t = np.array([w[0] + 10.0 for w in good])
    lag = np.array([w[1] for w in good])
    slope = float(np.polyfit(t, lag, 1)[0]) * 60 if len(good) > 2 else 0.0
    q = np.array_split(np.arange(len(good)), 4)
    return {"windows": len(good), "windows_total": len(windows), "median_ms": round(float(np.median(lag)), 1),
            "p5_ms": round(float(np.percentile(lag, 5)), 1), "p95_ms": round(float(np.percentile(lag, 95)), 1),
            "drift_ms_per_min": round(slope, 2),
            "quarters_median_ms": [round(float(np.median(lag[i])), 1) for i in q if len(i)],
            "worst": [{"t": round(w[0], 1), "lag_ms": round(w[1], 1), "corr": round(w[2], 2)}
                      for w in sorted(good, key=lambda w: -abs(w[1] - np.median(lag)))[:5]]}


def matched(env, times, fr, tol=0.05):
    import librosa
    on = librosa.onset.onset_detect(onset_envelope=env, sr=SR, hop_length=HOP, units="time", backtrack=False)
    times = np.unique(np.round(np.asarray(times), 4))
    d = []
    for t in times:
        j = np.searchsorted(on, t)
        c = [on[k] for k in (j - 1, j) if 0 <= k < len(on)]
        if c:
            best = min(c, key=lambda x: abs(x - t))
            if abs(best - t) <= tol:
                d.append(best - t)
    d = np.array(d) * 1000
    return {"onsets": int(len(times)), "matched": int(len(d)),
            "median_ms": round(float(np.median(d)), 1) if len(d) else None,
            "p10_ms": round(float(np.percentile(d, 10)), 1) if len(d) else None,
            "p90_ms": round(float(np.percentile(d, 90)), 1) if len(d) else None}


def decode(mp3, manual_skip):
    args = ["ffmpeg", "-v", "error"] + (["-flags2", "+skip_manual"] if manual_skip else []) + \
        ["-i", mp3, "-f", "f32le", "-ac", "1", "-ar", "44100", "-"]
    raw = subprocess.run(args, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def mp3_frames(data):
    """Byte offsets of the audio frames of an MPEG-1 Layer III file (an ID3v2 tag and a Xing/Info frame skipped)."""
    pos = 0
    if data[:3] == b"ID3":
        pos = 10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9])
    rates = [44100, 48000, 32000]
    kbps = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
    out = []
    while pos + 4 <= len(data):
        h = data[pos:pos + 4]
        if h[0] != 0xFF or (h[1] & 0xFE) != 0xFA:          # MPEG-1 Layer III sync
            pos += 1
            continue
        br, sr_i, pad = h[2] >> 4, (h[2] >> 2) & 3, (h[2] >> 1) & 1
        if br in (0, 15) or sr_i == 3:
            pos += 1
            continue
        length = 144000 * kbps[br] // rates[sr_i] + pad
        side = 17 if (h[3] >> 6) == 3 else 32
        if not out and data[pos + 4 + side:pos + 8 + side] in (b"Xing", b"Info"):
            pos += length
            continue
        out.append(pos)
        pos += length
    return out


def from_frame(mp3, ref, sr=44100):
    """Decode the mp3 from a frame boundary in its loudest stretch; lag of that decode against ref from the frame's
    nominal time (frame index * 1152), + = later."""
    with open(mp3, "rb") as f:
        data = f.read()
    frames = mp3_frames(data)
    if len(frames) < 100:
        return None
    hop = 1152
    loud = [float(np.sum(ref[k * hop:(k + 200) * hop] ** 2)) for k in range(0, len(frames) - 400, 200)]
    k = 200 * int(np.argmax(loud)) + 100 if loud else len(frames) // 2
    import tempfile, os
    fd, tmp = tempfile.mkstemp(suffix=".mp3")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data[frames[k]:])
        d = decode(tmp, True)
    finally:
        os.unlink(tmp)
    lag, corr = offset(ref[k * hop:], d, sr)
    return {"frame": k, "frame_time_s": round(k * hop / sr, 3), "samples": lag, "ms": round(lag / sr * 1000, 2),
            "corr": round(corr, 4), "frames": len(frames)}


def offset(ref, y, sr=44100, maxlag=6000):
    """Lag (samples) of y against ref, + = y later, over the loudest 20 s of ref."""
    n = min(len(ref), len(y))
    seg = int(20 * sr)
    if n < seg + 2 * maxlag:
        seg = max(sr, n - 2 * maxlag - 1)
    hop = sr
    best_s, best_e = maxlag, -1.0
    for s in range(maxlag, max(maxlag + 1, n - seg - maxlag), hop * 5):
        e = float(np.sum(ref[s:s + seg] ** 2))
        if e > best_e:
            best_s, best_e = s, e
    a = ref[best_s:best_s + seg]
    b = y[best_s - maxlag:best_s + seg + maxlag]
    m = len(a) + len(b)
    nfft = 1 << (m - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)[:2 * maxlag + 1]
    lag = int(np.argmax(c)) - maxlag
    return lag, float(c.max() / (np.linalg.norm(a) * np.linalg.norm(b[maxlag + lag:maxlag + lag + seg]) + 1e-12))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--onsets", required=True)
    ap.add_argument("--mp3")
    ap.add_argument("--gain-db", type=float, default=0.0)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    with open(args.onsets) as f:
        times = json.load(f)["times"]
    y, sr = sf.read(args.wav, dtype="float32", always_2d=True)
    fr = SR / HOP
    env = envelope(y, sr)
    tr = train(times, len(env), fr)
    windows, good = xcorr(env, tr, times, fr)
    res = {"xcorr": summarize(windows, good), "matched": matched(env, times, fr)}
    # the method's own lag: the same analysis of clicks at the onset times
    clicks = np.zeros(len(y), dtype=np.float32)
    for t in times:
        i = int(round(t * sr))
        if 0 <= i < len(clicks) - 32:
            clicks[i:i + 32] += np.hanning(32).astype(np.float32)
    cenv = envelope(clicks, sr)
    cw, cg = xcorr(cenv, train(times, len(cenv), fr), times, fr)
    res["method_bias_ms"] = summarize(cw, cg)["median_ms"]
    if args.mp3:
        ref = y.mean(axis=1) * (10 ** (args.gain_db / 20))
        out = {}
        for name, manual in (("ffmpeg_default", False), ("no_gapless_handling", True)):
            d = decode(args.mp3, manual)
            lag, corr = offset(ref, d, sr)
            out[name] = {"samples": lag, "ms": round(lag / sr * 1000, 2), "corr": round(corr, 4),
                         "decoded_samples": int(len(d)), "source_samples": int(len(ref))}
        ff = from_frame(args.mp3, ref, sr)
        if ff:
            out["from_a_frame"] = ff
        res["mp3"] = out
    text = json.dumps(res, indent=1)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
