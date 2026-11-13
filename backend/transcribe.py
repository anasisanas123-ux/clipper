"""Speech-to-text with faster-whisper: a fast pass over the whole stream, and word-level timing per clip."""
import os
import site
import threading
import time
from pathlib import Path

from . import config

_model = None
_device = None
_lock = threading.Lock()


def _add_cuda_dlls():
    """ctranslate2 needs cuBLAS/cuDNN DLLs; the nvidia-* pip packages ship them outside PATH."""
    for sp in site.getsitepackages():
        for d in Path(sp).glob("nvidia/*/bin"):
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def _download(name: str) -> str:
    """Fetch the model files, resuming after stalls (the big model is about 1.6 GB)."""
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from faster_whisper import download_model
    last = None
    for attempt in range(6):
        try:
            return download_model(name, cache_dir=str(config.MODELS))
        except Exception as e:
            last = e
            time.sleep(3)
    raise RuntimeError(f"Could not download the speech model '{name}'. Check your internet connection. ({last})")


def get_model(settings):
    global _model, _device
    with _lock:
        name = settings.get("whisper_model") or "large-v3-turbo"
        if _model is not None and _model[0] == name:
            return _model[1]
        _add_cuda_dlls()
        from faster_whisper import WhisperModel
        path = _download(name)
        try:
            m = WhisperModel(path, device="cuda", compute_type="float16")
            # Loading succeeds even when the CUDA libraries are broken; a tiny run proves they work.
            import numpy as np
            list(m.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])
            _device = "cuda"
        except Exception:
            small = name if name in ("tiny", "base", "small") else "small"
            m = WhisperModel(_download(small), device="cpu", compute_type="int8")
            _device = "cpu"
        _model = (name, m)
        return m


def device() -> str:
    return _device or "?"


def transcribe_full(wav, settings, duration, progress) -> list:
    """Segment-level transcript of the entire stream."""
    model = get_model(settings)
    lang = settings.get("language") or None
    kwargs = dict(language=lang, beam_size=1, vad_filter=True, condition_on_previous_text=False)
    segments = None
    if _device == "cuda":
        try:
            from faster_whisper import BatchedInferencePipeline
            segments, _ = BatchedInferencePipeline(model=model).transcribe(
                str(wav), language=lang, beam_size=1, batch_size=8)
        except Exception:
            segments = None
    if segments is None:
        segments, _ = model.transcribe(str(wav), **kwargs)
    out = []
    for s in segments:
        text = s.text.strip()
        if text:
            out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
        if duration:
            progress(min(1.0, s.end / duration))
    return out


def transcribe_words(wav, settings) -> list:
    """Word-level timestamps for one clip, used to time the captions."""
    model = get_model(settings)
    segments, _ = model.transcribe(str(wav), language=settings.get("language") or None, beam_size=5,
                                   vad_filter=True, word_timestamps=True, condition_on_previous_text=False)
    words = []
    for s in segments:
        for w in s.words or []:
            t = w.word.strip()
            if t:
                words.append({"start": round(w.start, 3), "end": round(w.end, 3), "text": t})
    return words
