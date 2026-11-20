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

from . import (ai, audio, config, copyright, effects, engine, timeline, layout, moments, music, providers, render,
               sources, templates, transcribe, youtube)

log = logging.getLogger("clipper")
KINDS = ("irl", "gaming", "film", "podcast")

_jobs: dict = {}
_lock = threading.RLock()
_tasks: queue.Queue = queue.Queue()          # analysis and uploads, one at a time
_render_tasks: queue.Queue = queue.Queue()   # renders and previews, several at once
_sources: dict = {}     # job id -> (fetched_at, hi-res source), refreshed because the links expire
PAD = 3.0               # spare seconds kept on each side of a clip so trims don't need a re-download


def _dir(job_id):
    return config.JOBS / job_id


def _save(job):
    # Clips of one video render at the same time, so another thread may change the job while it is being
    # written out: retry the snapshot, and replace the file in one step so a reader never sees half of it.
    with _lock:
        for attempt in range(5):
            try:
                text = json.dumps(job, indent=1)
                break
            except RuntimeError:
                time.sleep(0.02)
        else:
            return
        f = _dir(job["id"]) / "job.json"
        if not f.parent.exists():
            return
        # Windows refuses to replace a file another program has open for a moment (antivirus, search indexing,
        # backup tools): retry briefly, then write it in place. Saving progress must never fail a render.
        tmp = f.with_suffix(".json.tmp")
        for attempt in range(6):
            try:
                tmp.write_text(text, "utf-8")
                tmp.replace(f)
                return
            except OSError:
                time.sleep(0.05 * (attempt + 1))
        try:
            f.write_text(text, "utf-8")
        except OSError as e:
            log.warning("[%s] couldn't save progress (%s); will try again on the next update", job["id"], e)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


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
        if job["status"] == "live":
            job["status"] = "ready"
            (job.get("live") or {}).update(recording=False, state="Stopped when the app closed")
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


def create(url: str, kind: str = "irl", topic: str = "", template=None) -> dict:
    """template: a template id, "" for the plain Settings defaults, or None for the default template."""
    if template is None:
        template = config.load_settings().get("default_template") or ""
    job = {"id": uuid.uuid4().hex[:10], "url": url.strip(),
           "kind": kind if kind in KINDS else "irl", "topic": topic.strip()[:80], "title": "", "streamer": "", "duration": None,
           "status": "analyzing", "stage": "Queued", "progress": 0.0, "created": time.time(),
           "error": None, "warnings": [], "ai": None, "engine": None, "clips": [],
           "template": template if templates.get(template) else ""}
    _dir(job["id"]).mkdir(parents=True)
    with _lock:
        _jobs[job["id"]] = job
    _save(job)
    _tasks.put(("analyze", job["id"], None))
    return job


def delete(job_id):
    if (_jobs.get(job_id) or {}).get("status") == "live":
        from . import live
        live.stop(job_id, wait=True)
    with _lock:
        job = _jobs.pop(job_id, None)
    if job:
        job["status"] = "deleted"
        shutil.rmtree(_dir(job_id), ignore_errors=True)


def queue_render(job_id, clip_id, patch=None, preview=False):
    job = _jobs[job_id]
    clip = next(c for c in job["clips"] if c["id"] == clip_id)
    with _lock:
        old_start = clip["start"]
        for k in (*templates.KEYS, "hook", "context", "title", "hashtags", "start", "end", "effects", "story", "comment"):
            if patch and k in patch and patch[k] is not None:
                clip[k] = patch[k]
        if clip.get("effects") and clip["start"] != old_start:
            # effect times are from the clip start: keep them on the same moment when the start moves
            shift = old_start - clip["start"]
            clip["effects"] = [dict(e, t=round(e["t"] + shift, 2)) for e in clip["effects"]
                               if 0 <= e["t"] + shift < clip["end"] - clip["start"]]
        if clip["end"] - clip["start"] < 5:
            clip["end"] = clip["start"] + 5
        if job.get("duration"):
            clip["end"] = min(clip["end"], job["duration"])
        clip["status"], clip["error"] = "queued", None
    _save(job)
    _render_tasks.put(("preview" if preview else "render", job_id, clip_id))
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
    # the video's template can set how many clips to find and how long they are
    settings = templates.find_settings(config.load_settings(), job.get("template", ""))
    d = _dir(job["id"])
    status = _status(job)
    _stage(job, "Reading video info")
    meta = sources.probe(job["url"])
    job.update(title=meta["title"], streamer=meta["streamer"], duration=meta.get("duration"),
               local=meta["local"], platform=meta["platform"], source_url=meta.get("webpage_url") or job["url"])
    if meta.get("is_live"):
        from . import live
        live.start(job)
        return
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
    found, job["ai"], notes = engine.find(job, segments, z, settings, n, short, lambda text: _stage(job, text))
    job["warnings"] += notes
    if not found:
        raise RuntimeError("No usable moments found (the video may be silent or too short).")

    for i, m in enumerate(found[: max(n * 2, n + 3)]):
        init_clip(job, m, f"c{i + 1}", settings)
        job["clips"].append(m)
    job["status"] = "ready"
    _stage(job, "Done", 1.0)
    log.info("[%s] %d moments found", job["id"], len(job["clips"]))
    for c in job["clips"][:n]:
        queue_render(job["id"], c["id"])


def init_clip(job, m, cid, settings):
    """Fills in a newly found moment's editing settings from the video's template (or the Settings defaults)."""
    style = templates.style_for(settings, job.get("template", ""))
    _apply_look(m, style, int(cid.lstrip("c") or 1) - 1)
    m.update(id=cid, status="idle", error=None, version=0, layout_info=None,
             copyright=None, src_start=m.get("src_start"), src_end=m.get("src_end"), youtube=None,
             effects=None, showing=None, preview_version=0)
    if not m.get("hashtags"):
        m["hashtags"] = moments.make_hashtags(job, m)
    if not m.get("context"):
        m["context"] = m["hook"]   # the built-in algorithm can only quote the transcript
    m.setdefault("story", m.get("title") or "")
    m.setdefault("comment", "")
    return m


_hd_threads: dict = {}
_hd_lock = threading.Lock()


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
    with _hd_lock:          # two clips rendering at once must not both start the download
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
    full = _dir(job["id"]) / "source.mp4"
    if full.exists() and (job.get("hd") or {}).get("status") == "done":
        # downloaded whole by an older version: cutting from the file on disk is fastest
        return {"url": str(full), "audio_url": None, "headers": {}}
    cached = _sources.get(job["id"])
    if cached and time.time() - cached[0] < 900:
        return cached[1]
    src = sources.hires_source(job["url"], int(settings["max_height"]))
    _sources[job["id"]] = (time.time(), src)
    return src


def _render(job, clip, preview=False):
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
    if not covered and job.get("live_mode") and src.exists() and clip["src_start"] is not None:
        # A live stream can't be fetched again: keep the cut inside what was recorded around the moment.
        clip["start"] = max(clip["start"], clip["src_start"])
        clip["end"] = min(clip["end"], clip["src_end"])
        covered = True
    if not covered:
        if job.get("live_mode"):
            raise RuntimeError("This part of the live stream wasn't kept, so the clip can't be cut again.")
        if sources.needs_full_download(job.get("platform", "")):
            _wait_for_hd(job, step)
        step("Cutting the clip in full quality")
        s0 = max(0.0, clip["start"] - PAD)
        s1 = min(job["duration"] or clip["end"] + PAD, clip["end"] + PAD)
        try:
            sources.download_segment(_source(job, settings), s0, s1, src)
        except Exception as first:
            _sources.pop(job["id"], None)        # the link may have expired; fetch a new one once
            try:
                sources.download_segment(_source(job, settings), s0, s1, src)
            except Exception:
                if job.get("local"):
                    raise
                # the site refused the direct cut (YouTube sometimes answers 403): fetch just this part
                log.warning("[%s] direct cut failed (%s); downloading just this part", job["id"], str(first)[-200:])
                step("Downloading this part of the video")
                sources.download_section(job["url"], s0, s1, src, int(settings["max_height"]))
        clip["src_start"], clip["src_end"] = s0, s1
        words_file.unlink(missing_ok=True)
        (d / f"{cid}_env.npy").unlink(missing_ok=True)
        clip["layout_info"] = None

    if not clip.get("layout_info"):
        step("Looking for the facecam")
        clip["layout_info"] = layout.detect(src)
        _remember_cam(job, clip["layout_info"])
    chosen = clip["layout"] if clip["layout"] != "auto" else clip["layout_info"]["layout"]
    if clip["layout"] == "auto" and job.get("kind") == "podcast" and chosen == "split":
        # A small face in a podcast is a wide studio shot, not a webcam over gameplay: crop on the speaker.
        chosen = "crop"
    if clip["layout"] == "auto" and chosen != "split" and job.get("kind") == "gaming" and (job.get("cam") or {}).get("box"):
        # The facecam wasn't seen in this clip, but it was in others from the same stream: it hasn't moved.
        chosen = "split"

    env_file = d / f"{cid}_env.npy"
    if not words_file.exists() or not env_file.exists():
        step("Timing the captions")
        wav = d / f"{cid}_src.wav"
        sources.extract_wav(src, wav)
        np.save(env_file, timeline.envelope(wav))
        if not words_file.exists():
            words_file.write_text(json.dumps(transcribe.transcribe_words(wav, settings, lambda t, p: step(t))), "utf-8")
        wav.unlink(missing_ok=True)
    words = json.loads(words_file.read_text("utf-8"))

    trim_start = clip["start"] - clip["src_start"]
    trim_end = clip["end"] - clip["src_start"]
    if (clip.get("song") or "").startswith("auto:"):
        clip["song"] = music.pick(clip["song"], clip.get("mood", ""), int(cid.lstrip("c") or 1) - 1)
    ctx = _clip_context(job, clip)
    song_path, song_entry = music.resolve(clip.get("song") or "")
    if clip.get("effects") is None:
        fx = suggest_effects(job, clip, clip.get("effects_preset") or settings.get("effects_preset"), words)
        if clip.get("filter"):
            fx.pop("filter")            # the user already picked one
        clip.update(fx)
    plan = ctx["plan"]
    final_len = plan["length"] if plan else trim_end - trim_start
    clip["effects"] = effects.clean(clip["effects"], final_len)

    fps = effects.video_fps(src)
    if plan and not timeline.is_trivial(plan, trim_end - trim_start):
        step("Cutting out the pauses" if not plan["teaser"] else "Building the cold open")
        edit = d / f"{cid}_edit.mkv"
        timeline.render_plan(src, trim_start, plan, edit, d, fps)
        r_src, r_start, r_end = edit, 0.0, plan["length"]
        r_words = timeline.remap_words(plan, ctx["words"])
    else:
        r_src, r_start, r_end, r_words = src, trim_start, trim_end, words
    lay = dict(clip["layout_info"])
    clip["layout_note"] = ""
    if chosen == "split":
        lay["cam"], clip["layout_note"] = _split_cam(job, clip, lay)
        lay["split_game"] = clip.get("split_game") or "fill"
    if (clip.get("track_face", True) and chosen in ("crop", "bars", "blur", "story")
            and lay.get("face_x") and lay.get("layout") != "split"):
        step("Following the face")
        try:
            lay["track"] = layout.track(r_src, r_start, r_end - r_start)
        except Exception:
            log.exception("[%s] face tracking failed", job["id"])

    step("Rendering a quick preview" if preview else "Rendering")
    spec = dict(clip, src=r_src.name, trim_start=r_start, trim_end=r_end, layout=chosen, layout_info=lay)
    tmp = d / f"{cid}.tmp.mp4"
    render.render(d, spec, r_words, tmp, song_path, preview=preview)
    clip["final_len"] = round(r_end - r_start, 2)
    clip["cuts"] = plan["cuts"] if plan else 0
    if preview:
        tmp.replace(d / f"{cid}_preview.mp4")
        clip.update(status="ready", step="", used_layout=chosen, showing="preview",
                    preview_version=clip.get("preview_version", 0) + 1)
        _save(job)
        return
    tmp.replace(d / f"{cid}.mp4")

    step("Checking copyright risk")
    cut = d / f"{cid}_cut.wav"
    sources.extract_wav(src, cut, start=trim_start, dur=trim_end - trim_start)
    try:
        clip["copyright"] = copyright.assess(cut, song_entry, bool(job.get("local")), settings,
                                             job.get("kind", "irl"), job.get("topic", ""))
    finally:
        cut.unlink(missing_ok=True)
    clip.update(status="ready", step="", used_layout=chosen, showing="full", version=clip.get("version", 0) + 1)
    _save(job)


def _remember_cam(job, info):
    """Keeps the most confidently found facecam of the whole video. A webcam overlay stays in one place for the
    stream, so a clip where the face wasn't visible can use the spot found in another clip."""
    cam, conf = info.get("cam"), info.get("cam_conf") or 0
    if not cam or info.get("layout") != "split" or conf < 0.3:
        return
    old = job.get("cam") or {}
    if conf > old.get("conf", 0):
        job["cam"] = {"box": cam, "conf": conf, "width": info["width"], "height": info["height"]}


def _split_cam(job, clip, lay):
    """The facecam box for the 'Facecam top, gameplay bottom' framing, and a note when it had to be guessed.
    Never falls back to a different framing: the clip always comes out split."""
    W, H = lay["width"], lay["height"]
    pos = clip.get("cam_pos") or "auto"
    if pos == "custom" or (pos == "auto" and job.get("cam_box")):
        # marked by hand on this clip, or on another clip of the same video
        box = layout.marked_box(W, H, (clip.get("cam_box") if pos == "custom" else None) or job.get("cam_box") or {})
        if box:
            return box, ""
    if pos in layout.CORNERS:
        return layout.corner_box(W, H, pos), ""
    if lay.get("cam") and (lay.get("cam_conf") or 0) >= 0.3 and lay.get("layout") == "split":
        return lay["cam"], ""
    remembered = job.get("cam") or {}
    if remembered.get("box") and remembered.get("width") == W and remembered.get("height") == H:
        return remembered["box"], ""
    if lay.get("cam"):
        return lay["cam"], ""
    return (layout.corner_box(W, H, "tl"),
            "No facecam found, so the top-left corner is used. Pick the facecam's corner under Framing.")


def frame_jpeg(job, clip_id=None) -> bytes:
    """One frame of the video (JPEG, 960 wide) to mark the facecam on: from the clip's own cut when it has been
    rendered, else from the video itself (the file on disk, or a single frame fetched from the stream)."""
    d = _dir(job["id"])
    clips = job.get("clips") or []
    clip = next((c for c in clips if c["id"] == clip_id), None) or (clips[0] if clips else None)
    candidates = [c for c in ([clip] if clip else []) + clips if c and (d / f"{c['id']}_src.mp4").exists()]
    if candidates:
        c = candidates[0]
        src = {"url": str(d / f"{c['id']}_src.mp4"), "audio_url": None, "headers": {}}
        at = max(0.0, ((c["start"] + c["end"]) / 2) - (c.get("src_start") or 0))
    else:
        at = ((clip["start"] + clip["end"]) / 2) if clip else (job.get("duration") or 60) / 2
        if job.get("local"):
            src = {"url": job["url"], "audio_url": None, "headers": {}}
        else:
            src = _source(job, config.load_settings())
    out = d / f"_frame_{uuid.uuid4().hex[:6]}.jpg"
    http = sources.http_args(src.get("headers") or {}) if not sources.is_local(src["url"]) else []
    try:
        config.run_ffmpeg([*http, "-ss", f"{at:.2f}", "-i", src["url"], "-frames:v", "1", "-vf", "scale=960:-2",
                           "-q:v", "3", str(out)], timeout=90)
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


def set_cam(job_id, box: dict, clip_id=None, everywhere=True) -> int:
    """Uses a facecam area marked by hand (shares of the frame) on one clip or every clip in the video, and
    re-renders the ones already rendered with the facecam framing. Returns how many were queued."""
    job = _jobs[job_id]
    clean = templates.clean_style({"cam_pos": "custom", "cam_box": box})
    if clean.get("cam_pos") != "custom":
        raise ValueError("Draw a box around the facecam first")
    count = 0
    for c in job["clips"]:
        if not everywhere and c["id"] != clip_id:
            continue
        with _lock:
            c["cam_pos"], c["cam_box"] = "custom", clean["cam_box"]
        split = c.get("layout") == "split" or (c.get("layout") == "auto" and c.get("used_layout") == "split")
        rendered = c.get("version", 0) > 0 or c.get("preview_version", 0) > 0
        # the clip being edited is re-rendered by the user along with their other changes
        if c["id"] != clip_id and split and rendered and c["status"] not in ("queued", "rendering"):
            queue_render(job_id, c["id"], preview=c.get("showing") == "preview")
            count += 1
    with _lock:
        job["cam_box"] = clean["cam_box"]     # remembered for clips found later (live streams)
    _save(job)
    return count


def _apply_look(clip, style, index=0):
    """Puts a template's (or the defaults') look on a clip. An automatic song choice becomes a concrete track."""
    for k in templates.KEYS:
        if k in style:
            clip[k] = json.loads(json.dumps(style[k]))
    clip["song"] = music.pick(style.get("song") or "", clip.get("mood", ""), index)


def apply_template(job_id, template_id) -> int:
    """Gives every clip in the video a template's look and re-renders the ones already rendered or queued."""
    job = _jobs[job_id]
    t = templates.get(template_id)
    if not t:
        raise ValueError("That template no longer exists")
    style = templates.style_for(config.load_settings(), template_id)
    count = 0
    with _lock:
        job["template"] = template_id
    for i, c in enumerate(job["clips"]):
        if c["status"] in ("queued", "rendering"):
            continue
        with _lock:
            _apply_look(c, style, i)
            c["effects"] = None          # placed again on this clip's own moments, with the template's sounds
        if c.get("version", 0) > 0 or c.get("preview_version", 0) > 0 or c["status"] == "error":
            queue_render(job_id, c["id"])
            count += 1
    _save(job)
    return count


def _clip_context(job, clip, start=None, end=None, tighten=None, cold_open=None, song=None):
    """Everything about a clip's timing that rendering and effect suggestions share: its words in clip time,
    the fine loudness envelope, the timeline edit (dead air / cold open) and the song's beats.
    Pieces that need a first render (words, envelope) are None until then."""
    d = _dir(job["id"])
    cid = clip["id"]
    start = clip["start"] if start is None else float(start)
    end = clip["end"] if end is None else float(end)
    dur = end - start
    out = {"words": None, "env": None, "plan": None, "beats": None, "offset": None}
    wf, ef = d / f"{cid}_words.json", d / f"{cid}_env.npy"
    if wf.exists() and ef.exists() and clip.get("src_start") is not None and clip["src_start"] <= start:
        off = start - clip["src_start"]
        src_words = json.loads(wf.read_text("utf-8"))
        env = np.load(ef)
        out["offset"] = off
        out["src_words"] = src_words
        out["words"] = [{**w, "start": w["start"] - off, "end": w["end"] - off} for w in src_words
                        if w["end"] > off and w["start"] < off + dur]
        out["env"] = env[int(off * timeline.ENV_RATE): int((off + dur) * timeline.ENV_RATE)]
        out["plan"] = timeline.plan(out["words"], out["env"], dur,
                                    clip.get("tighten", True) if tighten is None else bool(tighten),
                                    clip.get("cold_open", False) if cold_open is None else bool(cold_open))
    song_path, _ = music.resolve((clip.get("song") or "") if song is None else song)
    if song_path and clip.get("beat_sync", True):
        try:
            out["beats"] = timeline.song_beats(Path(song_path))
        except Exception:
            log.exception("beat detection failed")
    return out


def _excitement(job):
    f = _dir(job["id"]) / "excitement.npy"
    return np.load(f) if f.exists() else None


def suggest_effects(job, clip, preset, words=None, start=None, end=None, tighten=None, cold_open=None, song=None,
                    sfx_source=None, sfx_fixed=None) -> dict:
    """Automatic effects for a clip (optionally for a draft start/end/options the user hasn't rendered yet),
    timed on the finished Short. Sounds come from your own folder first unless the clip says otherwise."""
    c = dict(clip, start=clip["start"] if start is None else float(start), end=clip["end"] if end is None else float(end))
    ctx = _clip_context(job, clip, start, end, tighten, cold_open, song)
    snd = dict(sound_source=sfx_source or clip.get("sfx_source") or config.load_settings().get("sfx_source", "auto"),
               sound_fixed=clip.get("sfx_fixed") if sfx_fixed is None else sfx_fixed)
    if ctx["words"] is not None:
        return effects.suggest(preset, c, _excitement(job), ctx["src_words"], ctx["offset"],
                               env=ctx["env"], plan=ctx["plan"], beats=ctx["beats"], **snd)
    # no word timings yet: use the transcript's sentence timings, which are on the video's timeline
    words = [{"start": seg["start"], "text": seg["text"]} for seg in moments_segments(job)
             if c["start"] <= seg["start"] < c["end"]]
    return effects.suggest(preset, c, _excitement(job), words, c["start"], beats=ctx["beats"], **snd)


def moments_segments(job):
    f = _dir(job["id"]) / "transcript.json"
    try:
        return json.loads(f.read_text("utf-8")) if f.exists() else []
    except Exception:
        return []


def export(job_id, clip_id) -> str:
    """Copies the finished Short to the exports folder with a ready-to-paste description file."""
    job = _jobs[job_id]
    clip = next(c for c in job["clips"] if c["id"] == clip_id)
    src = _dir(job_id) / f"{clip_id}.mp4"
    if not src.exists():
        raise FileNotFoundError("Render the clip first")
    if clip.get("showing") == "preview":
        raise FileNotFoundError("Your latest changes are only in the quick preview. Render it in full quality first.")
    safe = lambda s: re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "").strip()[:80] or "clip"
    folder = config.EXPORTS / safe(job["streamer"])
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{safe(clip['title'])}.mp4"
    shutil.copyfile(src, out)
    out.with_suffix(".txt").write_text(f"{clip['title']}\n\n{description(job, clip)}", "utf-8")
    return str(out)


STYLE_KEYS = templates.KEYS


def apply_style(job_id, clip_id) -> int:
    """Copies one clip's look (framing, captions, effects style, filter, song, timeline options) to every other
    clip in the video and re-renders them. Each clip gets its own effects placed on its own loud moments."""
    job = _jobs[job_id]
    model = next(c for c in job["clips"] if c["id"] == clip_id)
    count = 0
    for c in job["clips"]:
        if c["id"] == clip_id or c["status"] in ("queued", "rendering"):
            continue
        with _lock:
            for k in STYLE_KEYS:
                if k in model:
                    c[k] = model[k]
            c["effects"] = None
        queue_render(job_id, c["id"])
        count += 1
    return count


def render_all(job_id, preview=False) -> int:
    job = _jobs[job_id]
    count = 0
    for c in job["clips"]:
        if c["status"] not in ("queued", "rendering"):
            queue_render(job_id, c["id"], preview=preview)
            count += 1
    return count


def export_all(job_id) -> list:
    job = _jobs[job_id]
    out = []
    for c in job["clips"]:
        if c["status"] == "ready" and c.get("showing") != "preview" and (_dir(job_id) / f"{c['id']}.mp4").exists():
            out.append(export(job_id, c["id"]))
    return out


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


MAX_RENDER_WORKERS = 3
_render_gate = threading.Condition()
_rendering: set = set()        # (job id, clip id) being rendered right now


def _parallel_limit() -> int:
    try:
        return max(1, min(MAX_RENDER_WORKERS, int(config.load_settings().get("parallel_renders", 2))))
    except (TypeError, ValueError):
        return 2


def _render_worker():
    """Renders run on their own workers, several at once (Settings: clips rendered at the same time). A single
    render leaves most CPU cores and the GPU encoder idle, so two at once finish a batch about a third sooner.
    Speech recognition inside a render is still one at a time (transcribe.py holds a lock)."""
    while True:
        kind, job_id, clip_id = _render_tasks.get()
        key = (job_id, clip_id)
        with _render_gate:
            # wait for a free slot, and never render the same clip twice at once
            while len(_rendering) >= _parallel_limit() or key in _rendering:
                _render_gate.wait(timeout=1.0)
            _rendering.add(key)
        try:
            job = _jobs.get(job_id)
            clip = next((c for c in job["clips"] if c["id"] == clip_id), None) if job else None
            if clip:
                try:
                    _render(job, clip, preview=(kind == "preview"))
                except Exception as e:
                    log.exception("Task failed")
                    clip["status"], clip["error"] = "error", str(e)[-600:]
                    if job.get("status") != "deleted":
                        _save(job)
        finally:
            with _render_gate:
                _rendering.discard(key)
                _render_gate.notify_all()


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
        return "Clipper doesn't recognise that link. Paste a Kick, YouTube or Twitch link, or add a video file."
    return text[-800:]


def start():
    load_all()
    threading.Thread(target=_worker, daemon=True).start()
    for _ in range(MAX_RENDER_WORKERS):
        threading.Thread(target=_render_worker, daemon=True).start()
