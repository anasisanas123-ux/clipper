"""Paths, settings, and the ffmpeg binary."""
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS = DATA / "jobs"
EXPORTS = DATA / "exports"
MUSIC_MINE = DATA / "music" / "mine"        # tracks the user drops in
MUSIC_LIB = DATA / "music" / "library"      # tracks downloaded by the app (with licence sidecars)
MODELS = DATA / "models"
BIN = DATA / "bin"
SETTINGS_FILE = DATA / "settings.json"

for _d in (JOBS, EXPORTS, MUSIC_MINE, MUSIC_LIB, MODELS, BIN):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    "scorer": "builtin",             # "builtin" (the local algorithm) or "ai" (a connected AI model)
    "ai_provider": "anthropic",      # one of providers.PROVIDERS
    "ai_keys": {},                   # provider -> API key (never sent to the UI)
    "ai_models": {"anthropic": "claude-opus-5-5"},   # provider -> chosen model id
    "custom_base_url": "",           # for the "other OpenAI-compatible" provider
    "whisper_model": "large-v3-turbo",
    "language": "",                  # "" = auto-detect
    "clip_count": 6,
    "min_len": 15,
    "max_len": 55,
    "default_layout": "bars",        # framing for new clips: bars, blur, auto, split or crop
    "default_zoom": 73,              # picture height as % of the screen for bars/blur (bigger = smaller bars)
    "caption_style": "meme",
    "highlight": "green",
    "progress_bar": True,
    "effects_preset": "edit",        # automatic effects style for new clips: none, subtle, edit, hype, cinematic
    "sfx_volume": 0.7,
    "sfx_source": "auto",            # sounds for automatic effects: auto (yours when you have some), mine, builtin
    "default_song": "",              # song for new clips: "", a track id, "auto:mood" or "auto:rotate"
    "default_template": "",          # (older versions) template for new videos; the setup window replaced it
    "last_setup": {},                # the choices made in the setup window for the last video, offered next time
    "encoder": "auto",               # video encoder: auto, nvenc, amf, qsv, mf (any GPU) or cpu
    "parallel_renders": 2,           # clips rendered at the same time (2 finishes a batch about a third sooner)
    "tighten": True,                 # cut silent pauses out of new clips (jump cuts)
    "cold_open": False,
    "live_min_score": 60,            # live streams: only moments scoring at least this become clips
    "live_auto_render": True,        # live streams: render new clips as soon as they're found              # start new clips with a 2-second teaser of the payoff
    "channel_name": "",              # shown at the top of "story card" Shorts
    "channel_handle": "",
    "channel_avatar": "",            # optional picture file for the story card header
    "music_volume": 0.18,
    "audd_api_key": "",              # optional: names the song playing in a clip
    "jamendo_client_id": "",         # optional: popularity-ranked free music
    "max_height": 1080,
    "schedule_times": "12:00, 18:00",   # daily upload slots, in this PC's local time
    "youtube_privacy": "private",       # visibility for "upload now" (scheduled uploads go public on time)
}

_lock = threading.Lock()


def _migrate(raw: dict) -> dict:
    """Older versions only knew Claude: carry its key, model and on/off switch over to the new settings."""
    if "scorer" in raw:
        return raw
    key = (raw.pop("anthropic_api_key", "") or "").strip()
    model = raw.pop("claude_model", "") or "claude-opus-5-5"
    on = raw.pop("use_claude", True)
    raw["ai_keys"] = {**raw.get("ai_keys", {}), **({"anthropic": key} if key else {})}
    raw["ai_models"] = {**raw.get("ai_models", {}), "anthropic": model}
    raw["ai_provider"] = "anthropic"
    raw["scorer"] = "ai" if (on and key) else "builtin"
    return raw


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULTS))   # deep copy, so the dict defaults are never shared
    if SETTINGS_FILE.exists():
        try:
            s.update(_migrate(json.loads(SETTINGS_FILE.read_text("utf-8"))))
        except Exception:
            pass
    return s


def save_settings(patch: dict) -> dict:
    with _lock:
        s = load_settings()
        for k, v in patch.items():
            if k not in DEFAULTS or v is None:
                continue
            d = DEFAULTS[k]
            if k == "last_setup":
                s[k] = dict(v)               # replaced whole, so choices turned off don't linger
            elif isinstance(d, dict):
                s[k] = {**s.get(k, {}), **{a: b for a, b in dict(v).items() if b is not None}}
            elif isinstance(d, bool):
                s[k] = bool(v)
            else:
                s[k] = type(d)(v)
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), "utf-8")
        return s


def ai_key(settings: dict, provider: str) -> str:
    key = (settings.get("ai_keys") or {}).get(provider, "")
    if not key and provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
    return key.strip()


def ai_model(settings: dict, provider: str) -> str:
    return ((settings.get("ai_models") or {}).get(provider) or "").strip()


_ffmpeg = None
_nvenc = None
gpu_note = ""   # set when an NVIDIA GPU is present but can't encode


def ffmpeg() -> str:
    """Path to an ffmpeg.exe literally named ffmpeg (yt-dlp needs that name)."""
    global _ffmpeg
    if _ffmpeg:
        return _ffmpeg
    local = BIN / "ffmpeg.exe"
    if not local.exists():
        found = shutil.which("ffmpeg")
        if found:
            _ffmpeg = found
            return found
        import imageio_ffmpeg
        shutil.copyfile(imageio_ffmpeg.get_ffmpeg_exe(), local)
    _ffmpeg = str(local)
    # yt-dlp's check for section downloads only looks on PATH, not at the ffmpeg_location it is given
    if str(BIN) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(BIN) + os.pathsep + os.environ.get("PATH", "")
    return _ffmpeg


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_ffmpeg(args: list, cwd=None, timeout=3600) -> None:
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace",
                       timeout=timeout, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + (p.stderr or "").strip()[-1500:])


# Hardware video encoders tried in order; the first one that works on this PC is used for every render.
ENCODERS = {
    "nvenc": ("h264_nvenc", ["-preset", "p5", "-rc", "vbr", "-b:v", "0"], lambda q: ["-cq", str(q)], "NVIDIA GPU"),
    "amf": ("h264_amf", ["-quality", "balanced", "-rc", "cqp"],
            lambda q: ["-qp_i", str(q), "-qp_p", str(q + 2), "-qp_b", str(q + 4)], "AMD GPU"),
    "qsv": ("h264_qsv", ["-preset", "medium"], lambda q: ["-global_quality", str(q + 2)], "Intel graphics"),
    # Windows' own hardware encoder layer: works with any GPU that has a video encoder (any maker), so it
    # catches the cases above miss, e.g. a driver without the AMD or Intel runtime. hw_encoding refuses the
    # software fallback, so it only counts as working when a GPU really does the encoding.
    "mf": ("h264_mf", ["-hw_encoding", "1", "-rate_control", "quality"],
           lambda q: ["-quality", str(int(max(1, min(100, 100 - (q - 10) * 2.5))))], "GPU (Windows Media Foundation)"),
}
ENCODER_CHOICES = {"auto": "Automatic (fastest that works)", "nvenc": "NVIDIA GPU", "amf": "AMD GPU",
                   "qsv": "Intel graphics", "mf": "Any GPU (Windows Media Foundation)", "cpu": "CPU (slowest)"}
_encoder_for = None    # the "encoder" setting the cached choice was made for


def _try_encoder(key) -> str:
    """'' when the encoder works on this PC, otherwise ffmpeg's error."""
    name, opts, quality, _ = ENCODERS[key]
    try:
        run_ffmpeg(["-f", "lavfi", "-i", "color=c=gray:s=1080x1920:d=0.3", "-c:v", name, *opts, *quality(21),
                    "-pix_fmt", "nv12" if key == "mf" else "yuv420p", "-f", "null", "-"], timeout=30)
        return ""
    except Exception as e:
        return str(e)


def video_encoder() -> list:
    """The encoder chosen in Settings, or the fastest that works here: NVIDIA, AMD or Intel hardware, then any
    GPU through Windows Media Foundation, otherwise x264 on the CPU. A chosen encoder that doesn't work on this
    PC falls back to the automatic choice."""
    global _nvenc, gpu_note, _encoder_for
    want = load_settings().get("encoder") or "auto"
    if want != _encoder_for:
        _nvenc, _encoder_for, gpu_note = None, want, ""
    if _nvenc is None:
        _nvenc = ""
        if want == "cpu":
            return ["libx264", "-preset", "veryfast"]
        if want in ENCODERS and not _try_encoder(want):
            _nvenc = want
        for key in ([] if _nvenc else ENCODERS):
            err = _try_encoder(key)
            if not err:
                _nvenc = key
                break
            if key == "nvenc":
                import re as _re
                m = _re.search(r"minimum required Nvidia driver for nvenc is ([\d.]+)", err)
                if m:
                    gpu_note = (f"Your NVIDIA graphics driver is too old for fast GPU video encoding (it needs version "
                                f"{m.group(1)} or newer), so videos are encoded on the CPU, which is several times "
                                f"slower. Update the driver with the NVIDIA app or from nvidia.com, then restart Clipper.")
    if _nvenc:
        name, opts, _, _ = ENCODERS[_nvenc]
        return [name, *opts]
    return ["libx264", "-preset", "veryfast"]


def encoder_label() -> str:
    video_encoder()
    return ENCODERS[_nvenc][3] if _nvenc else "CPU"


def quality_args(q: int) -> list:
    video_encoder()
    return ENCODERS[_nvenc][2](q) if _nvenc else ["-crf", str(q)]


def pix_fmt() -> str:
    """Pixel format for the chosen encoder: Windows Media Foundation hardware encoders only take NV12 (the same
    4:2:0 picture as yuv420p, laid out differently)."""
    video_encoder()
    return "nv12" if _nvenc == "mf" else "yuv420p"
