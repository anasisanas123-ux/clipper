"""Job store and the background worker that runs analysis and renders one task at a time."""
import json
import logging
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np

from . import ai, audio, config, copyright, layout, moments, music, providers, render, sources, transcribe, youtube

log = logging.getLogger("clipper")
KINDS = ("irl", "gaming", "film", "podcast")

_jobs: dict = {}
_lock = threading.RLock()
_tasks: queue.Queue = queue.Queue()
_sources: dict = {}     # job id -> (fetched_at, hi-res source), refreshed because the links expire
PAD = 3.0               # spare seconds kept on each side of a clip so trims don't need a re-download


def _dir(job_id):
    return config.JOBS / job_id


def _save(job):
    with _lock:
        (_dir(job["id"]) / "job.json").write_text(json.dumps(job, indent=1), "utf-8")


def load_all():
    for d in config.JOBS.iterdir():
        f = d / "job.json"
        if not f.exists():
            continue
        try:
            job = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        if (job.get("hd") or {}).get("status") == "downloading":
            job["hd"].update(status="error", error="Interrupted when the app closed")   # resumes on next render
        if job["status"] == "analyzing":
            job["status"], job["error"] = "error", "The app was closed while this video was being analysed."
        for c in job.get("clips", []):
            if c.get("youtube") and c["youtube"]["status"] in ("queued", "uploading"):
                c["youtube"].update(status="error", error="The app was closed before the upload finished.")
            if c["status"] in ("queued", "rendering"):
                c["status"] = "ready" if (d / f"{c['id']}.mp4").exists() else "idle"
        _jobs[job["id"]] = job


def list_jobs():
    with _lock:
        return sorted(({k: j[k] for k in ("id", "url", "title", "streamer", "status", "created", "duration")}
                       | {"clips": len(j.get("clips", []))} for j in _jobs.values()),
                      key=lambda j: -j["created"])


def get(job_id):
    return _jobs.get(job_id)


def create(url: str, kind: str = "irl", topic: str = "") -> dict:
    job = {"id": uuid.uuid4().hex[:10], "url": url.strip(),
           "kind": kind if kind in KINDS else "irl", "topic": topic.strip()[:80], "title": "", "streamer": "", "duration": None,
           "status": "analyzing", "stage": "Queued", "progress": 0.0, "created": time.time(),
           "error": None, "warnings": [], "ai": None, "engine": None, "clips": []}
    _dir(job["id"]).mkdir(parents=True)
    with _lock:
        _jobs[job["id"]] = job
    _save(job)
    _tasks.put(("analyze", job["id"], None))
    return job


def delete(job_id):
    with _lock:
        job = _jobs.pop(job_id, None)
    if job:
        job["status"] = "deleted"
        shutil.rmtree(_dir(job_id), ignore_errors=True)


def queue_render(job_id, clip_id, patch=None):
    job = _jobs[job_id]
    clip = next(c for c in job["clips"] if c["id"] == clip_id)
    with _lock:
        for k in ("layout", "caption_style", "highlight", "hook", "title", "hashtags", "song", "music_volume",
                  "progress_bar", "start", "end"):
            if patch and k in patch and patch[k] is not None:
                clip[k] = patch[k]
        if clip["end"] - clip["start"] < 5:
            clip["end"] = clip["start"] + 5
        if job.get("duration"):
            clip["end"] = min(clip["end"], job["duration"])
        clip["status"], clip["error"] = "queued", None
    _save(job)
    _tasks.put(("render", job_id, clip_id))
    return clip


def _stage(job, text, progress=0.0):
    if text != job.get("stage"):
        log.info("[%s] %s", job["id"], text)
    job["stage"], job["progress"] = text, round(float(progress), 3)
    job["updated"] = time.time()
    _save(job)


def _status(job):
    """Callback for long steps: updates the stage text and progress, saving at most every 2 seconds."""
    last = [0.0, ""]

    def cb(text, p):
        head = text.split(":")[0].split("(")[0]
        if head != last[1]:
            last[1] = head
            log.info("[%s] %s", job["id"], head.strip())
        job["stage"], job["progress"], job["updated"] = text, round(float(p), 3), time.time()
        if time.time() - last[0] > 2:
            last[0] = time.time()
            _save(job)
    return cb


def _analyze(job):
    settings = config.load_settings()
    d = _dir(job["id"])
    status = _status(job)
    _stage(job, "Reading video info")
    meta = sources.probe(job["url"])
    job.update(title=meta["title"], streamer=meta["streamer"], duration=meta.get("duration"),
               local=meta["local"], platform=meta["platform"], source_url=meta.get("webpage_url") or job["url"])
    if sources.needs_full_download(job["platform"]):
        _start_hd_download(job, settings)

    if not meta["local"]:
        status("Downloading the audio", 0.0)
        low = sources.download_low(job["url"], d, lambda p: status(f"Downloading the audio ({p * 100:.0f}%)", p))
    else:
        low = Path(job["url"])
    wav = d / "audio.wav"
    _stage(job, "Preparing audio")
    sources.extract_wav(low, wav)
    if not meta["local"]:
        low.unlink(missing_ok=True)
    duration = audio.wav_duration(wav)
    job["duration"] = duration

    _stage(job, "Measuring loudness")
    z = audio.excitement(audio.rms_curve(wav))
    np.save(d / "excitement.npy", z)

    segments, job["engine"] = transcribe.transcribe_full(wav, settings, duration, status)
    (d / "transcript.json").write_text(json.dumps(segments), "utf-8")
    wav.unlink(missing_ok=True)

    n = int(settings["clip_count"])
    _stage(job, "Finding the best moments")
    short = duration <= settings["max_len"] + 10
    if short:
        # An already-cut clip: keep it whole and just score it.
        found = [moments.whole_clip(z, segments, duration, job["kind"])]
    else:
        found = moments.find_candidates(z, segments, settings, limit=max(12, n * 2), kind=job["kind"])
    ready, why = providers.readiness(settings)
    if settings.get("scorer") == "ai" and not ready:
        job["warnings"].append(f"Connected AI is selected but not set up ({why}), so the built-in algorithm "
                               f"scored this video. Finish the setup in Settings.")
    if ready and segments:
        name = providers.label(settings["ai_provider"])
        _stage(job, f"{name} is reading the transcript")
        try:
            picked, info = ai.pick_moments(segments, z, job, settings, 1 if short else n + 3, found, whole=short)
            if picked:
                found, job["ai"] = picked, info
            else:
                job["warnings"].append(f"{name} found no moments worth clipping; showing the built-in picks instead.")
        except Exception as e:
            log.exception("[%s] AI scoring failed", job["id"])
            job["warnings"].append(f"{name} step failed, so the built-in algorithm was used instead. "
                                   f"{providers.friendly(e, settings['ai_provider'])}")
    if not found:
        raise RuntimeError("No usable moments found (the video may be silent or too short).")

    for i, m in enumerate(found[: max(n * 2, n + 3)]):
        m.update(id=f"c{i + 1}", status="idle", error=None, version=0, layout="auto", layout_info=None,
                 caption_style=settings["caption_style"], highlight=settings["highlight"],
                 progress_bar=settings["progress_bar"], song="", music_volume=settings["music_volume"],
                 copyright=None, src_start=None, src_end=None, youtube=None)
        if not m.get("hashtags"):
            m["hashtags"] = moments.make_hashtags(job, m)
        job["clips"].append(m)
    job["status"] = "ready"
    _stage(job, "Done", 1.0)
    log.info("[%s] %d moments found", job["id"], len(job["clips"]))
    for c in job["clips"][:n]:
        queue_render(job["id"], c["id"])


_hd_threads: dict = {}


def _start_hd_download(job, settings):
    """Downloads the full HD video in the background while the audio is analysed (YouTube only)."""
    out = _dir(job["id"]) / "source.mp4"
    if out.exists():
        job["hd"] = {"status": "done", "progress": 1.0, "error": None}
        return
    job["hd"] = {"status": "downloading", "progress": 0.0, "error": None}

    def run():
        try:
            sources.download_full(job["url"], out, int(settings["max_height"]),
                                  lambda p: job["hd"].update(progress=round(p, 3)))
            job["hd"].update(status="done", progress=1.0)
            log.info("[%s] HD video downloaded (%d MB)", job["id"], out.stat().st_size // 1_000_000)
        except Exception as e:
            log.exception("[%s] HD download failed", job["id"])
            job["hd"].update(status="error", error=_friendly(e))
        if job.get("status") != "deleted":
            _save(job)

    t = threading.Thread(target=run, daemon=True)
    _hd_threads[job["id"]] = t
    t.start()


def _wait_for_hd(job, step):
    """Blocks a render until the background HD download is finished, showing its progress."""
    hd = job.get("hd") or {}
    if hd.get("status") in (None, "error") and not (_dir(job["id"]) / "source.mp4").exists():
        # Not started (app restarted) or failed earlier: try again now.
        _start_hd_download(job, config.load_settings())
    t = _hd_threads.get(job["id"])
    while t and t.is_alive():
        step(f"Waiting for the HD video to download ({job['hd']['progress'] * 100:.0f}%)")
        t.join(timeout=2)
    if job["hd"]["status"] == "error":
        raise RuntimeError("Couldn't download the HD video: " + (job["hd"]["error"] or "unknown error"))


def _source(job, settings):
    if sources.needs_full_download(job.get("platform", "")):
        return {"url": str(_dir(job["id"]) / "source.mp4"), "audio_url": None, "headers": {}}
    cached = _sources.get(job["id"])
    if cached and time.time() - cached[0] < 900:
        return cached[1]
    src = sources.hires_source(job["url"], int(settings["max_height"]))
    _sources[job["id"]] = (time.time(), src)
    return src


def _render(job, clip):
    settings = config.load_settings()
    d = _dir(job["id"])
    cid = clip["id"]
    src = d / f"{cid}_src.mp4"
    words_file = d / f"{cid}_words.json"

    def step(text):
        clip["status"], clip["step"] = "rendering", text
        _save(job)

    covered = (src.exists() and clip["src_start"] is not None
               and clip["src_start"] <= clip["start"] and clip["end"] <= clip["src_end"])
    if not covered:
        if sources.needs_full_download(job.get("platform", "")):
            _wait_for_hd(job, step)
        step("Cutting the clip in full quality")
        s0 = max(0.0, clip["start"] - PAD)
        s1 = min(job["duration"] or clip["end"] + PAD, clip["end"] + PAD)
        try:
            sources.download_segment(_source(job, settings), s0, s1, src)
        except Exception:
            _sources.pop(job["id"], None)        # the link may have expired; fetch a new one once
            sources.download_segment(_source(job, settings), s0, s1, src)
        clip["src_start"], clip["src_end"] = s0, s1
        words_file.unlink(missing_ok=True)
        clip["layout_info"] = None

    if not clip.get("layout_info"):
        step("Looking for the facecam")
        clip["layout_info"] = layout.detect(src)
    chosen = clip["layout"] if clip["layout"] != "auto" else clip["layout_info"]["layout"]
    if clip["layout"] == "auto" and job.get("kind") == "podcast" and chosen == "split":
        # A small face in a podcast is a wide studio shot, not a webcam over gameplay: crop on the speaker.
        chosen = "crop"

    if not words_file.exists():
        step("Timing the captions")
        wav = d / f"{cid}_src.wav"
        sources.extract_wav(src, wav)
        words_file.write_text(json.dumps(transcribe.transcribe_words(wav, settings, lambda t, p: step(t))), "utf-8")
        wav.unlink(missing_ok=True)
    words = json.loads(words_file.read_text("utf-8"))

    step("Rendering")
    song_path, song_entry = music.resolve(clip.get("song") or "")
    trim_start = clip["start"] - clip["src_start"]
    trim_end = clip["end"] - clip["src_start"]
    spec = dict(clip, src=src.name, trim_start=trim_start, trim_end=trim_end, layout=chosen)
    tmp = d / f"{cid}.tmp.mp4"
    render.render(d, spec, words, tmp, song_path)
    tmp.replace(d / f"{cid}.mp4")

    step("Checking copyright risk")
    cut = d / f"{cid}_cut.wav"
    sources.extract_wav(src, cut, start=trim_start, dur=trim_end - trim_start)
    try:
        clip["copyright"] = copyright.assess(cut, song_entry, bool(job.get("local")), settings,
                                             job.get("kind", "irl"), job.get("topic", ""))
    finally:
        cut.unlink(missing_ok=True)
    clip.update(status="ready", step="", used_layout=chosen, version=clip.get("version", 0) + 1)
    _save(job)


def export(job_id, clip_id) -> str:
    """Copies the finished Short to the exports folder with a ready-to-paste description file."""
    job = _jobs[job_id]
    clip = next(c for c in job["clips"] if c["id"] == clip_id)
    src = _dir(job_id) / f"{clip_id}.mp4"
    if not src.exists():
        raise FileNotFoundError("Render the clip first")
    safe = lambda s: re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "").strip()[:80] or "clip"
    folder = config.EXPORTS / safe(job["streamer"])
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{safe(clip['title'])}.mp4"
    shutil.copyfile(src, out)
    out.with_suffix(".txt").write_text(f"{clip['title']}\n\n{description(job, clip)}", "utf-8")
    return str(out)


def hashtag_line(clip) -> str:
    tags = [t for t in clip.get("hashtags") or [] if t]
    return " ".join("#" + t for t in ["shorts", *tags])


def description(job, clip) -> str:
    """Upload description: hashtags, credit to the streamer, and the music credit when one is owed."""
    credit = next((i.get("attribution") for i in (clip.get("copyright") or {}).get("items", [])
                   if i.get("attribution")), "")
    desc = hashtag_line(clip) + "\n"
    if not job.get("local"):
        desc += f"\nClipped from {job['streamer']} on {job.get('platform') or 'Kick'}: {job.get('source_url') or job['url']}\n"
    if credit:
        desc += f"\nMusic: {credit}\n"
    return desc


def _taken_slots() -> set:
    return {c["youtube"]["publish_at"] for j in _jobs.values() for c in j.get("clips", [])
            if c.get("youtube") and c["youtube"].get("publish_at") and c["youtube"]["status"] != "error"}


def queue_upload(job_id, clip_id, mode="next", at=None, patch=None):
    """mode: 'next' = next free daily slot, 'at' = a chosen local time, 'now' = upload without scheduling."""
    job = _jobs[job_id]
    clip = next(c for c in job["clips"] if c["id"] == clip_id)
    if not (_dir(job_id) / f"{clip_id}.mp4").exists():
        raise ValueError("Render the clip first")
    if clip.get("youtube") and clip["youtube"]["status"] in ("queued", "uploading", "scheduled", "uploaded"):
        raise ValueError("This clip is already on YouTube or on its way there")
    settings = config.load_settings()
    with _lock:
        for k in ("title", "hashtags"):
            if patch and patch.get(k) is not None:
                clip[k] = patch[k]
        publish_at = None
        if mode == "next":
            publish_at = youtube.next_slot(settings["schedule_times"], _taken_slots())
        elif mode == "at":
            if not at:
                raise ValueError("Pick a date and time first")
            publish_at = youtube.to_utc(at)
        clip["youtube"] = {"status": "queued", "publish_at": publish_at, "video_id": None, "error": None}
    _save(job)
    _tasks.put(("upload", job_id, clip_id))
    return clip


def schedule_all(job_id) -> int:
    """Queue every rendered clip that isn't on YouTube yet, best score first, one per free slot."""
    job = _jobs[job_id]
    count = 0
    for clip in sorted(job["clips"], key=lambda c: -c["score"]):
        done = clip.get("youtube") and clip["youtube"]["status"] != "error"
        if clip["status"] == "ready" and not done:
            queue_upload(job_id, clip["id"], "next")
            count += 1
    return count


def _upload(job, clip):
    settings = config.load_settings()
    yt = clip["youtube"]
    yt["status"] = "uploading"
    _save(job)
    try:
        yt["video_id"] = youtube.upload(
            _dir(job["id"]) / f"{clip['id']}.mp4", clip["title"], description(job, clip),
            ["shorts", *(clip.get("hashtags") or [])], job.get("kind", "irl"),
            settings["youtube_privacy"], yt["publish_at"])
        yt["status"] = "scheduled" if yt["publish_at"] else "uploaded"
    except Exception as e:
        log.exception("Task failed")
        yt["status"], yt["error"] = "error", str(e)[-500:]
    _save(job)


def _worker():
    while True:
        kind, job_id, clip_id = _tasks.get()
        job = _jobs.get(job_id)
        if not job:
            continue
        try:
            if kind == "analyze":
                _analyze(job)
            elif kind == "upload":
                _upload(job, next(c for c in job["clips"] if c["id"] == clip_id))
            else:
                clip = next(c for c in job["clips"] if c["id"] == clip_id)
                try:
                    _render(job, clip)
                except Exception as e:
                    log.exception("Task failed")
                    clip["status"], clip["error"] = "error", str(e)[-600:]
                    _save(job)
        except Exception as e:
            log.exception("Task failed")
            log.error("[%s] %s failed: %s", job_id, kind, e)
            if job["status"] != "deleted":
                job["status"], job["error"] = "error", _friendly(e)
                _save(job)


def _friendly(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    if "sign in to confirm" in low:
        return ("YouTube blocked the download as automated traffic. Try again later, or download the video "
                "yourself and add it as a file.")
    if "private video" in low or "members-only" in low or "join this channel" in low:
        return "That video is private or members-only, so Clipper can't download it."
    if "unsupported url" in low:
        return "Clipper doesn't recognise that link. Paste a Kick VOD or YouTube video link, or add a video file."
    return text[-800:]


def start():
    load_all()
    threading.Thread(target=_worker, daemon=True).start()
