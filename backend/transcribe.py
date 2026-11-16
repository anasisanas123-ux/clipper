"""Speech-to-text with faster-whisper: a fast pass over the whole video, and word-level timing per clip.

With an NVIDIA GPU the chosen model runs on it. Without one, a lighter model runs on the CPU for the
long full-video pass (the big model would take hours there), and a mid-size one times the clip captions."""
import logging
import os
import site
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger("clipper")
_models: dict = {}
_lock = threading.Lock()
_gpu = None

# Approximate download sizes, for showing progress while a model downloads.
SIZES_MB = {"tiny": 75, "base": 145, "small": 485, "medium": 1530, "large-v3-turbo": 1620, "large-v3": 3090}
CPU_FULL_PASS = "base"
CPU_WORDS = "small"


def _add_cuda_dlls():
    """ctranslate2 needs cuBLAS/cuDNN DLLs; the nvidia-* pip packages ship them outside PATH."""
    for sp in site.getsitepackages():
        for d in Path(sp).glob("nvidia/*/bin"):
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def has_gpu() -> bool:
    """True when an NVIDIA GPU is usable. Checked once, by running a tiny model on it."""
    global _gpu
    if _gpu is None:
        _add_cuda_dlls()
        try:
            import ctranslate2
            _gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            _gpu = False
        log.info("NVIDIA GPU available: %s", _gpu)
    return _gpu


def _download(name: str, status=None) -> str:
    """Fetch the model files (resuming after stalls), reporting MB downloaded so far."""
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    from faster_whisper import download_model
    try:
        return download_model(name, cache_dir=str(config.MODELS), local_files_only=True)
    except Exception:
        pass   # not downloaded yet

    total = SIZES_MB.get(name, 500)
    done = threading.Event()

    def watch():
        folder = next(config.MODELS.glob(f"models--*faster-whisper-{name}"), None)
        while not done.wait(1.5):
            folder = folder or next(config.MODELS.glob(f"models--*faster-whisper-{name}"), None)
            if folder and status:
                mb = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) / 1e6
                status(f"Downloading the speech model, one time only ({mb:.0f} of ~{total} MB)", min(0.99, mb / total))

    if status:
        status(f"Downloading the speech model, one time only (~{total} MB)", 0.0)
        threading.Thread(target=watch, daemon=True).start()
    last = None
    try:
        for _ in range(8):
            try:
                return download_model(name, cache_dir=str(config.MODELS))
            except Exception as e:
                last = e
                log.warning("Model download attempt failed: %s", e)
                time.sleep(3)
    finally:
        done.set()
    raise RuntimeError(f"Could not download the speech model '{name}'. Check your internet connection. ({last})")


def _load(name: str, status=None):
    """Load (once) a model by name onto the GPU when there is one, otherwise the CPU."""
    with _lock:
        if name in _models:
            return _models[name]
        from faster_whisper import WhisperModel
        path = _download(name, status)
        if status:
            status("Loading the speech model", 0.0)
        if has_gpu():
            try:
                m = WhisperModel(path, device="cuda", compute_type="float16")
                import numpy as np
                list(m.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])   # proves the CUDA libs work
                _models[name] = (m, "cuda")
                return _models[name]
            except Exception as e:
                log.warning("GPU load failed, using the CPU instead: %s", e)
                global _gpu
                _gpu = False
        m = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=max(1, (os.cpu_count() or 4) - 1))
        _models[name] = (m, "cpu")
        return _models[name]


def full_pass_model(settings) -> str:
    return (settings.get("whisper_model") or "large-v3-turbo") if has_gpu() else CPU_FULL_PASS


def words_model(settings) -> str:
    return (settings.get("whisper_model") or "large-v3-turbo") if has_gpu() else CPU_WORDS


def transcribe_full(wav, settings, duration, status) -> tuple:
    """Segment-level transcript of the entire video. Returns (segments, info for the job)."""
    name = full_pass_model(settings)
    model, device = _load(name, status)
    lang = settings.get("language") or None
    where = "GPU" if device == "cuda" else "CPU (no NVIDIA GPU found, so this is the slow part)"
    status(f"Transcribing speech on the {where}", 0.0)
    from faster_whisper import BatchedInferencePipeline
    gpu = device == "cuda"
    # The batched pipeline returns ~30-second chunks. They're split into sentences so clips can start and end
    # cleanly: by real word timings on the GPU (free there), by text position on the CPU (timings double CPU time).
    segments, _ = BatchedInferencePipeline(model=model).transcribe(
        str(wav), language=lang, beam_size=1, batch_size=8 if gpu else 4, word_timestamps=gpu)

    out, started, last = [], time.time(), 0.0
    for s in segments:
        out.extend(_split_words(s.words) if s.words else _split_text(s.start, s.end, s.text))
        if duration and time.time() - last > 2:
            last = time.time()
            frac = min(1.0, s.end / duration)
            elapsed = time.time() - started
            eta = ""
            if frac > 0.02 and elapsed > 10:
                left = elapsed / frac * (1 - frac)
                eta = f", about {_fmt(left)} left" if left >= 60 else ", almost done"
            status(f"Transcribing speech on the {where}: {_clock(s.end)} of {_clock(duration)}{eta}", frac)
    speed = (duration or 0) / max(1.0, time.time() - started)
    log.info("Transcribed %.0fs of audio in %.0fs on %s with %s (%.1fx realtime)",
             duration or 0, time.time() - started, device, name, speed)
    return out, {"whisper": name, "device": device, "speed": round(speed, 1)}


def transcribe_words(wav, settings, status=None) -> list:
    """Word-level timestamps for one clip, used to time the captions."""
    model, _ = _load(words_model(settings), status)
    if status:
        status("Timing the captions", 0.0)
    segments, _ = model.transcribe(str(wav), language=settings.get("language") or None, beam_size=5,
                                   vad_filter=True, word_timestamps=True, condition_on_previous_text=False)
    words = []
    for s in segments:
        for w in s.words or []:
            t = w.word.strip()
            if t:
                words.append({"start": round(w.start, 3), "end": round(w.end, 3), "text": t})
    return words


MAX_SENTENCE = 12.0   # seconds; longer run-on speech is still split so cut points stay precise


def _split_words(words) -> list:
    """Groups timed words into sentences, breaking on end punctuation, long pauses, or 12 seconds."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        text = w.word.strip()
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = nxt is not None and nxt.start - w.end > 1.0
        if text[-1:] in ".!?" or pause or nxt is None or w.end - cur[0].start >= MAX_SENTENCE:
            line = "".join(x.word for x in cur).strip()
            if line:
                out.append({"start": round(cur[0].start, 2), "end": round(w.end, 2), "text": line})
            cur = []
    return out


def _split_text(start: float, end: float, text: str) -> list:
    """Splits a chunk into sentences, estimating each one's time from its position in the text."""
    import re
    text = text.strip()
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    if not parts:
        return []
    total = sum(len(p) for p in parts)
    out, t = [], start
    for p in parts:
        dur = (end - start) * len(p) / total
        out.append({"start": round(t, 2), "end": round(t + dur, 2), "text": p.strip()})
        t += dur
    return out


def _clock(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _fmt(sec: float) -> str:
    m = int(sec // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"
