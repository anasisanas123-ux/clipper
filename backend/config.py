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
    "use_claude": True,              # only takes effect when an API key is available
    "anthropic_api_key": "",
    "claude_model": "claude-opus-5-5",
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


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:
            pass
    return s


def save_settings(patch: dict) -> dict:
    with _lock:
        s = load_settings()
        for k, v in patch.items():
            if k in DEFAULTS and v is not None:
                s[k] = type(DEFAULTS[k])(v) if not isinstance(DEFAULTS[k], bool) else bool(v)
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), "utf-8")
        return s


def api_key(settings: dict) -> str:
    return (settings.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def claude_enabled(settings: dict) -> bool:
    return bool(settings.get("use_claude")) and bool(api_key(settings))


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
