"""Audio signals: loudness spikes over the whole stream, and a music-presence estimate for a clip."""
import wave

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d
from scipy.signal import stft


def wav_duration(path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def load_wav(path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def rms_curve(path) -> np.ndarray:
    """Loudness in dB for each second, read in chunks so multi-hour files stay cheap."""
    out = []
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        while True:
            buf = w.readframes(sr * 60)
            if not buf:
                break
            x = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
            n = len(x) // sr
            if n == 0:
                break
            sec = x[: n * sr].reshape(n, sr)
            out.append(20 * np.log10(np.sqrt((sec ** 2).mean(axis=1)) + 1e-4))
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


def excitement(rms_db: np.ndarray) -> np.ndarray:
    """How far each second sits above the stream's local normal loudness, in robust z-units."""
    if len(rms_db) == 0:
        return rms_db
    win = min(301, max(31, (len(rms_db) // 2) * 2 + 1))
    base = median_filter(rms_db, size=win, mode="nearest")
    spread = 1.4826 * median_filter(np.abs(rms_db - base), size=win, mode="nearest")
    z = (rms_db - base) / np.maximum(spread, 1.5)
    return uniform_filter1d(z, size=3, mode="nearest")


def _window_music(y: np.ndarray, sr: int) -> tuple:
    _, _, Z = stft(y, sr, nperseg=1024, noverlap=512)
    S = np.abs(Z)
    # Sustained tones (music) survive a median filter along time; clicks and consonants don't.
    H = median_filter(S, size=(1, 17))
    P = median_filter(S, size=(17, 1))
    h, p = float((H ** 2).sum()), float((P ** 2).sum())
    harm = h / (h + p + 1e-9)
    # A steady beat shows up as a peak in the autocorrelation of the onset envelope.
    flux = np.maximum(0, np.diff(np.log1p(10 * S), axis=1)).sum(axis=0)
    flux = flux - flux.mean()
    ac = np.correlate(flux, flux, "full")[len(flux) - 1:]
    fps = sr / 512
    lo, hi = int(0.3 * fps), int(1.2 * fps)
    beat = float(ac[lo:hi].max() / (ac[0] + 1e-9)) if len(ac) > hi else 0.0
    return harm, beat


def music_likelihood(path, sr=16000) -> dict:
    """Rough 0..1 estimate that music is playing in the clip. A heuristic, not a fingerprint match."""
    y = load_wav(path)
    win = sr * 8
    if len(y) < win:
        return {"score": 0.0, "harmonic": 0.0, "beat": 0.0}
    harms, beats = [], []
    for i in range(0, len(y) - win + 1, win // 2):
        seg = y[i:i + win]
        if np.sqrt((seg ** 2).mean()) < 0.003:
            continue
        h, b = _window_music(seg, sr)
        harms.append(h)
        beats.append(b)
    if not harms:
        return {"score": 0.0, "harmonic": 0.0, "beat": 0.0}
    harm, beat = float(np.median(harms)), float(np.percentile(beats, 75))
    h_n = np.clip((harm - HARM_LO) / (HARM_HI - HARM_LO), 0, 1)
    b_n = np.clip((beat - BEAT_LO) / (BEAT_HI - BEAT_LO), 0, 1)
    return {"score": round(float(0.2 * h_n + 0.8 * b_n), 3), "harmonic": round(harm, 3), "beat": round(beat, 3)}


# Calibration points (speech-only sits near the LO values, full music near the HI values).
# Measured on this app's test clips: a steady beat separates music from talk far better than tone does.
HARM_LO, HARM_HI = 0.40, 0.60
BEAT_LO, BEAT_HI = 0.28, 0.60
