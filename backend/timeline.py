"""Edits to a clip's timeline before it is rendered: cutting dead air (jump cuts) and the cold open (the payoff
teased in the first seconds). Also the fine loudness envelope used to land effects exactly on hits, and song beat
detection for beat-synced effects.

Times here are seconds from the clip's start in the original video ("original time") unless they say otherwise.
A plan is a list of (a, b) original-time ranges played one after another; the first one is the teaser when the
plan has a cold open."""
import hashlib
import json
import subprocess
import wave
from pathlib import Path

import numpy as np

from . import config

ENV_RATE = 100           # envelope frames per second


# ---------------------------------------------------------------- envelope
def envelope(wav: Path) -> np.ndarray:
    """Loudness in dB every 10 ms."""
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    hop = sr // ENV_RATE
    n = len(x) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = x[: n * hop].reshape(n, hop)
    return (20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-5)).astype(np.float32)


def _slice(env, a, b):
    return env[max(0, int(a * ENV_RATE)): max(0, int(b * ENV_RATE))]


def onset_near(env, t, window=0.6):
    """The sharpest rise in loudness within `window` s of t (env is on the same time base as t)."""
    if env is None or not len(env):
        return t
    a = max(0, int((t - window) * ENV_RATE))
    b = min(len(env) - 1, int((t + window) * ENV_RATE))
    if b - a < 3:
        return t
    seg = np.convolve(env[a:b + 1], np.ones(3) / 3, mode="same")
    rise = np.diff(seg)
    i = int(np.argmax(rise))
    if rise[i] < 1.5:            # no real hit here, leave it where it was
        return t
    return (a + i + 1) / ENV_RATE


# ---------------------------------------------------------------- plan
def plan(words: list, env, dur: float, tighten: bool, cold_open: bool) -> dict:
    """words: [{start, end, text}] in original clip time. env: envelope in original clip time (or None).
    Returns {"segments": [(a, b)...], "teaser": bool, "cuts": n, "length": final length}."""
    keep = [(0.0, dur)]
    cuts = 0
    if tighten and words and env is not None and len(env):
        loud_ref = float(np.percentile(env, 60))
        quiet = lambda a, b: len(_slice(env, a, b)) == 0 or float(np.percentile(_slice(env, a, b), 90)) < loud_ref - 8
        gaps = []
        ws = sorted(words, key=lambda w: w["start"])
        if ws[0]["start"] > 0.7 and quiet(0, ws[0]["start"] - 0.15):
            gaps.append((0.0, ws[0]["start"] - 0.15))
        for cur, nxt in zip(ws, ws[1:]):
            a, b = cur["end"] + 0.12, nxt["start"] - 0.08
            if nxt["start"] - cur["end"] > 0.45 and b - a >= 0.25 and quiet(a, b):
                gaps.append((a, b))
        if dur - ws[-1]["end"] > 1.2 and quiet(ws[-1]["end"] + 0.5, dur):
            gaps.append((ws[-1]["end"] + 0.5, dur))
        keep, t = [], 0.0
        for a, b in gaps:
            if a > t + 0.05:
                keep.append((t, a))
            t = max(t, b)
        if dur > t + 0.05:
            keep.append((t, dur))
        cuts = len(gaps)
        if not keep:
            keep = [(0.0, dur)]

    teaser = None
    if cold_open and dur >= 12 and env is not None and len(env) > ENV_RATE * 4:
        sm = np.convolve(env, np.ones(20) / 20, mode="same")
        lo, hi = int(dur * 0.4 * ENV_RATE), int(max(dur * 0.4 + 1, dur - 0.3) * ENV_RATE)
        if hi > lo:
            p = (lo + int(np.argmax(sm[lo:hi]))) / ENV_RATE
            if p > 4:
                teaser = (max(0.0, p - 1.2), min(dur, p + 0.6))

    segs = ([teaser] if teaser else []) + keep
    return {"segments": [(round(a, 3), round(b, 3)) for a, b in segs], "teaser": bool(teaser), "cuts": cuts,
            "length": round(sum(b - a for a, b in segs), 3)}


def is_trivial(p: dict, dur: float) -> bool:
    return not p["teaser"] and len(p["segments"]) == 1 and p["segments"][0][0] < 0.01 and p["segments"][0][1] > dur - 0.01


def to_final(p: dict, t: float):
    """Original clip time -> final time (skipping the teaser), or None if t was cut out."""
    segs = p["segments"]
    off = 0.0
    start = 1 if p["teaser"] else 0
    if p["teaser"]:
        off = segs[0][1] - segs[0][0]
    for a, b in segs[start:]:
        if a <= t < b:
            return off + (t - a)
        off += b - a
    return None


def remap_words(p: dict, words: list) -> list:
    """Words in original clip time -> words in final time (teaser words appear twice)."""
    out, off = [], 0.0
    for a, b in p["segments"]:
        for w in words:
            if a <= w["start"] < b:
                out.append({**w, "start": round(off + w["start"] - a, 3), "end": round(off + min(w["end"], b) - a, 3)})
        off += b - a
    return out


def render_plan(src: Path, src_offset: float, p: dict, out: Path, cwd: Path, fps: float = 30) -> None:
    """Writes the re-timed clip. src_offset: where the clip starts inside src."""
    segs = p["segments"]
    inputs, fc = [], []
    parts = []
    main = segs[1:] if p["teaser"] else segs
    if p["teaser"]:
        a, b = segs[0]
        inputs += ["-ss", f"{src_offset + a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src)]
        fc.append(f"[0:v]setpts=PTS-STARTPTS,fps={fps:g}[tv];[0:a]asetpts=PTS-STARTPTS,aresample=48000[ta]")
        parts.append("[tv][ta]")
    k = len(inputs) // 6
    lo, hi = main[0][0], main[-1][1]
    inputs += ["-ss", f"{src_offset + lo:.3f}", "-t", f"{hi - lo:.3f}", "-i", str(src)]
    sel = "+".join(f"between(t,{a - lo:.3f},{b - lo:.3f})" for a, b in main)
    fc.append(f"[{k}:v]fps={fps:g},select='{sel}',setpts=N/({fps:g})/TB[mv];"
              f"[{k}:a]aresample=48000,asetnsamples=n=480,aselect='{sel}',asetpts=N/SR/TB[ma]")
    parts.append("[mv][ma]")
    if len(parts) > 1:
        fc.append("".join(parts) + "concat=n=2:v=1:a=1[v][a]")
        vmap, amap = "[v]", "[a]"
    else:
        vmap, amap = "[mv]", "[ma]"
    config.run_ffmpeg([*inputs, "-filter_complex", ";".join(fc), "-map", vmap, "-map", amap,
                       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-c:a", "pcm_s16le", str(out)],
                      cwd=str(cwd), timeout=900)


# ---------------------------------------------------------------- song beats
BEATS = config.DATA / "music" / ".beats"
BEATS.mkdir(parents=True, exist_ok=True)


def song_beats(path: Path, limit=240.0) -> list:
    """Beat times (s) for the first `limit` seconds of a song, cached."""
    key = hashlib.md5(f"{path}|{path.stat().st_mtime}".encode()).hexdigest()
    cache = BEATS / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text("utf-8"))
    sr = 11025
    p = subprocess.run([config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-t", str(limit), "-i", str(path),
                        "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True,
                       creationflags=config.NO_WINDOW, timeout=120)
    x = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    hop = sr // ENV_RATE
    n = len(x) // hop
    beats = []
    if n > ENV_RATE * 4:
        e = np.log1p(100 * np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1)))
        flux = np.maximum(0, np.diff(e, prepend=e[0]))
        flux -= flux.mean()
        ac = np.correlate(flux, flux, mode="full")[n - 1:]
        lags = np.arange(int(ENV_RATE * 60 / 180), int(ENV_RATE * 60 / 70))
        period = int(lags[np.argmax(ac[lags])])
        phase = int(np.argmax([flux[ph::period].sum() for ph in range(period)]))
        beats = [round((ph) / ENV_RATE, 3) for ph in range(phase, n, period)]
    cache.write_text(json.dumps(beats), "utf-8")
    return beats


def snap_to_beat(t: float, beats: list, window=0.22) -> float:
    if not beats:
        return t
    i = int(np.searchsorted(beats, t))
    near = [b for b in beats[max(0, i - 1): i + 1] if abs(b - t) <= window]
    return min(near, key=lambda b: abs(b - t)) if near else t
