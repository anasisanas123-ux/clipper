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
    "caption_style": "pop",
    "highlight": "yellow",
    "progress_bar": True,
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
            if isinstance(d, dict):
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
    return _ffmpeg


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_ffmpeg(args: list, cwd=None, timeout=3600) -> None:
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace",
                       timeout=timeout, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + (p.stderr or "").strip()[-1500:])


def video_encoder() -> list:
    """NVENC when the GPU supports it, otherwise x264."""
    global _nvenc
    if _nvenc is None:
        try:
            run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc",
                        "-f", "null", "-"], timeout=30)
            _nvenc = True
        except Exception:
            _nvenc = False
    return ["h264_nvenc", "-preset", "p5", "-rc", "vbr", "-b:v", "0"] if _nvenc else ["libx264", "-preset", "veryfast"]


def quality_args(q: int) -> list:
    video_encoder()
    return ["-cq", str(q)] if _nvenc else ["-crf", str(q)]
