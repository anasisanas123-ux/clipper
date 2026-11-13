"""Fetching Kick VODs with yt-dlp: metadata, a low-quality full pass for analysis, and exact hi-res segments."""
import os
from pathlib import Path

from . import config


def is_local(url: str) -> bool:
    return os.path.isfile(url)


def _ydl(extra=None):
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "ffmpeg_location": str(Path(config.ffmpeg()).parent),
        "retries": 5, "fragment_retries": 10, "fixup": "never",
    }
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def probe(url: str) -> dict:
    """Title, streamer, duration, and the best stream URL at or under the height cap."""
    if is_local(url):
        return {"title": Path(url).stem, "streamer": "local file", "duration": None, "local": True}
    with _ydl({"skip_download": True}) as y:
        info = y.extract_info(url, download=False)
    if info.get("is_live"):
        raise RuntimeError("That link is a live stream. Paste the link to a finished stream (VOD) instead.")
    return {
        "title": info.get("title") or "Kick stream",
        "streamer": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "unknown",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "local": False,
    }


def hires_source(url: str, max_height: int) -> dict:
    """Direct media URL + headers for ffmpeg, picked fresh because Kick's links expire."""
    if is_local(url):
        return {"url": url, "headers": {}}
    with _ydl({"skip_download": True, "format": f"best[height<={max_height}]/best"}) as y:
        info = y.extract_info(url, download=False)
    return {"url": info["url"], "headers": info.get("http_headers") or {}}


def download_low(url: str, out_dir: Path, progress) -> Path:
    """Smallest variant of the whole VOD; only its audio is used."""
    if is_local(url):
        return Path(url)

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(d.get("downloaded_bytes", 0) / total)
            elif d.get("fragment_count"):
                progress(d.get("fragment_index", 0) / d["fragment_count"])

    with _ydl({"format": "worstaudio/worst", "outtmpl": str(out_dir / "low.%(ext)s"),
               "concurrent_fragment_downloads": 8, "progress_hooks": [hook]}) as y:
        info = y.extract_info(url, download=True)
        return Path(y.prepare_filename(info))


def extract_wav(src: Path, wav: Path, start=None, dur=None) -> None:
    args = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", src]
    if dur is not None:
        args += ["-t", f"{dur:.3f}"]
    config.run_ffmpeg([*args, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])


def download_segment(source: dict, start: float, end: float, out: Path) -> None:
    """Frame-exact cut of [start, end] re-encoded at high quality."""
    args = []
    headers = source.get("headers") or {}
    if headers:
        ua = headers.get("User-Agent")
        if ua:
            args += ["-user_agent", ua]
        rest = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k != "User-Agent")
        if rest:
            args += ["-headers", rest]
    args += ["-ss", f"{max(0, start):.3f}", "-i", source["url"], "-t", f"{end - start:.3f}",
             "-map", "0:v:0", "-map", "0:a:0?",
             "-c:v", *config.video_encoder(), *config.quality_args(18), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", out]
    config.run_ffmpeg(args, timeout=900)
