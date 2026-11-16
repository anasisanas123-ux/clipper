"""Getting video in: Kick and YouTube links (via yt-dlp) and video files already on this PC."""
import os
import shutil
import subprocess
from pathlib import Path

from . import config

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v", ".ts"}


def is_local(url: str) -> bool:
    return os.path.isfile(url)


def _ydl(extra=None):
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "ffmpeg_location": config.ffmpeg(),
        "retries": 10, "fragment_retries": 20, "socket_timeout": 30, "fixup": "never",
    }
    # YouTube needs a JavaScript runtime to unlock all formats; Node is there whenever the app was set up from source.
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def _platform(info: dict) -> str:
    key = (info.get("extractor_key") or info.get("extractor") or "").lower()
    if key.startswith("kick"):
        return "Kick"
    if key.startswith("youtube"):
        return "YouTube"
    return info.get("extractor_key") or "the web"


def probe_local(path: str) -> dict:
    """Duration of a local file, read from ffmpeg's header dump."""
    p = subprocess.run([config.ffmpeg(), "-hide_banner", "-i", path], capture_output=True, text=True,
                       errors="replace", creationflags=config.NO_WINDOW)
    import re
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    if not m:
        raise RuntimeError("That file isn't a video Clipper can read.")
    if "Video:" not in p.stderr:
        raise RuntimeError("That file has no video track.")
    h, mi, s = m.groups()
    return {"title": Path(path).stem, "streamer": "", "duration": int(h) * 3600 + int(mi) * 60 + float(s),
            "local": True, "platform": "local file"}


def probe(url: str) -> dict:
    """Title, channel, duration and platform for a link or file."""
    if is_local(url):
        return probe_local(url)
    with _ydl({"skip_download": True, "noplaylist": True}) as y:
        info = y.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        raise RuntimeError("That's a playlist or channel page. Paste the link to one video.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise RuntimeError("That link is live or hasn't started. Paste the link once the stream has ended.")
    return {
        "title": info.get("title") or "Video",
        "streamer": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "unknown",
        "duration": info.get("duration"),
        "local": False,
        "platform": _platform(info),
        "webpage_url": info.get("webpage_url") or url,
    }


def hires_source(url: str, max_height: int) -> dict:
    """Direct media URL(s) + headers for ffmpeg, fetched fresh because the links expire.
    YouTube serves HD as separate video and audio streams, so this can return two URLs."""
    if is_local(url):
        return {"url": url, "audio_url": None, "headers": {}}
    fmt = (f"bestvideo[height<={max_height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
           f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best")
    with _ydl({"skip_download": True, "noplaylist": True, "format": fmt}) as y:
        info = y.extract_info(url, download=False)
    parts = info.get("requested_formats")
    if parts and len(parts) == 2:
        v = next(p for p in parts if p.get("vcodec") not in (None, "none"))
        a = next(p for p in parts if p is not v)
        return {"url": v["url"], "audio_url": a["url"], "headers": v.get("http_headers") or {}}
    return {"url": info["url"], "audio_url": None, "headers": info.get("http_headers") or {}}


def download_low(url: str, out_dir: Path, progress) -> Path:
    """Smallest version of the whole video; only its audio is used for analysis."""
    if is_local(url):
        return Path(url)

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(d.get("downloaded_bytes", 0) / total)
            elif d.get("fragment_count"):
                progress(d.get("fragment_index", 0) / d["fragment_count"])

    with _ydl({"format": "worstaudio[abr>=32]/worstaudio/bestaudio/worst", "noplaylist": True,
               "outtmpl": str(out_dir / "low.%(ext)s"), "concurrent_fragment_downloads": 8,
               "progress_hooks": [hook]}) as y:
        info = y.extract_info(url, download=True)
        return Path(y.prepare_filename(info))


def needs_full_download(platform: str) -> bool:
    """YouTube refuses ffmpeg's direct requests for sections of HD video (403 Forbidden), so YouTube videos
    are downloaded whole by yt-dlp, which fetches them in chunks it accepts. Kick serves HLS, which cuts fine."""
    return platform == "YouTube"


def download_full(url: str, out: Path, max_height: int, progress) -> Path:
    """Whole video in HD, merged to one mp4."""
    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(d.get("downloaded_bytes", 0) / total)

    fmt = (f"bestvideo[height<={max_height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
           f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best")
    tmpl = str(out.with_suffix("")) + ".%(ext)s"
    with _ydl({"format": fmt, "noplaylist": True, "outtmpl": tmpl, "merge_output_format": "mp4",
               "concurrent_fragment_downloads": 4, "progress_hooks": [hook]}) as y:
        info = y.extract_info(url, download=True)
        got = Path(y.prepare_filename(info)).with_suffix(".mp4")
    if got != out:
        got.replace(out)
    return out


def extract_wav(src: Path, wav: Path, start=None, dur=None) -> None:
    args = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", src]
    if dur is not None:
        args += ["-t", f"{dur:.3f}"]
    config.run_ffmpeg([*args, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], timeout=7200)


def _http_args(headers: dict) -> list:
    args = []
    ua = headers.get("User-Agent")
    if ua:
        args += ["-user_agent", ua]
    rest = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k != "User-Agent")
    if rest:
        args += ["-headers", rest]
    return args


def download_segment(source: dict, start: float, end: float, out: Path) -> None:
    """Frame-exact cut of [start, end] re-encoded at high quality."""
    remote = not is_local(source["url"])
    http = _http_args(source.get("headers") or {}) if remote else []
    seek = ["-ss", f"{max(0, start):.3f}"]
    args = [*http, *seek, "-i", source["url"]]
    amap = ["-map", "0:a:0?"]
    if source.get("audio_url"):
        args += [*http, *seek, "-i", source["audio_url"]]
        amap = ["-map", "1:a:0"]
    args += ["-t", f"{end - start:.3f}", "-map", "0:v:0", *amap,
             "-c:v", *config.video_encoder(), *config.quality_args(18), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", out]
    config.run_ffmpeg(args, timeout=1800)
