"""Local HTTP API + UI host. Bound to 127.0.0.1 only."""
import argparse
import logging
import logging.handlers
import re
import subprocess
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import ai, config, effects, jobs, live, music, performance, providers, sources, templates, updater, youtube

app = FastAPI(title="Clipper")


def _public_settings():
    s = config.load_settings()
    keys = {p: config.ai_key(s, p) for p in providers.PROVIDERS}
    s.pop("ai_keys", None)
    # Keys never leave the engine; the UI only learns whether one is saved and its last 4 characters.
    s["key_hints"] = {p: ("…" + k[-4:]) for p, k in keys.items() if k}
    s["has_audd_key"] = bool(s.pop("audd_api_key"))
    ready, why = providers.readiness(s | {"ai_keys": keys})
    s["ai_ready"], s["ai_status"] = ready, why
    s["providers"] = {p: {k: v[k] for k in ("label", "needs_key", "key_url", "base")}
                      for p, v in providers.PROVIDERS.items()}
    config.video_encoder()
    s["gpu_note"] = config.gpu_note
    s["encoder_used"] = config.encoder_label()
    return s


@app.get("/api/settings")
def get_settings():
    return _public_settings()


@app.post("/api/settings")
def post_settings(patch: dict = Body(...)):
    # A blank key field means "keep what's saved", so the UI never has to hold the key.
    if patch.get("audd_api_key") == "":
        patch.pop("audd_api_key")
    key = (patch.pop("ai_key", None) or "").strip()
    provider = patch.get("ai_provider") or config.load_settings()["ai_provider"]
    if provider not in providers.PROVIDERS:
        raise HTTPException(400, "Unknown AI provider")
    if key:
        patch["ai_keys"] = {provider: key}
    for k in patch.pop("clear", []):
        if k == "audd_api_key":
            patch[k] = ""
        if k == "ai_key":
            patch["ai_keys"] = {provider: ""}
    if patch.get("ai_model") is not None:
        patch["ai_models"] = {provider: str(patch.pop("ai_model")).strip()}
    if patch.get("scorer") not in (None, "builtin", "ai"):
        raise HTTPException(400, "Unknown scoring mode")
    config.save_settings(patch)
    return _public_settings()


@app.post("/api/ai/models")
def ai_models(body: dict = Body(...)):
    """Lists the models a provider offers. Uses the key typed in the form if given, else the saved one."""
    provider = body.get("provider")
    if provider not in providers.PROVIDERS:
        raise HTTPException(400, "Unknown AI provider")
    s = config.load_settings()
    if provider == "custom" and body.get("base_url"):
        s["custom_base_url"] = body["base_url"]
    try:
        return {"models": providers.list_models(s, provider, (body.get("key") or "").strip() or None)}
    except providers.AIError as e:
        raise HTTPException(400, str(e))


@app.get("/api/jobs")
def list_jobs():
    return jobs.list_jobs()


@app.post("/api/jobs")
def create_job(body: dict = Body(...)):
    url = (body.get("url") or "").strip().strip('"')
    if not url:
        raise HTTPException(400, "Paste a Kick or YouTube link, or add a video file")
    if sources.is_local(url):
        if Path(url).suffix.lower() not in sources.VIDEO_EXT:
            raise HTTPException(400, "That file type isn't supported. Use an mp4, mkv, mov, webm or similar video.")
    elif not re.match(r"https?://", url):
        raise HTTPException(400, "That isn't a link or a video file on this PC")
    return jobs.create(url, body.get("kind") or "irl", body.get("topic") or "", body.get("template"))


@app.get("/api/templates")
def list_templates():
    return {"templates": templates.all_templates(), "default": config.load_settings().get("default_template", ""),
            "keys": list(templates.KEYS), "sound_roles": list(effects.SOUND_ROLES), "auto_songs": music.AUTO_SONG}


@app.post("/api/templates")
def save_template(body: dict = Body(...)):
    """Saves a look as a template (same name = update). make_default also makes it the default for new videos."""
    t = templates.save(body.get("name") or "", body.get("style") or {}, body.get("id"))
    if body.get("make_default"):
        config.save_settings({"default_template": t["id"]})
    return list_templates() | {"saved": t}


@app.post("/api/templates/default")
def default_template(body: dict = Body(...)):
    tid = body.get("id") or ""
    if tid and not templates.get(tid):
        raise HTTPException(404, "Template not found")
    config.save_settings({"default_template": tid})
    return list_templates()


@app.delete("/api/templates/{tid}")
def delete_template(tid: str):
    templates.delete(tid)
    return list_templates()


@app.post("/api/jobs/{job_id}/apply-template")
def apply_template(job_id: str, body: dict = Body(...)):
    _job(job_id)
    try:
        return {"queued": jobs.apply_template(job_id, body.get("id") or "")}
    except ValueError as e:
        raise HTTPException(404, str(e))


def _job(job_id):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job(job_id)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    jobs.delete(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/clips/{clip_id}/render")
def render_clip(job_id: str, clip_id: str, patch: dict = Body(default={})):
    job = _job(job_id)
    if not any(c["id"] == clip_id for c in job["clips"]):
        raise HTTPException(404, "Clip not found")
    return jobs.queue_render(job_id, clip_id, patch, preview=bool(patch.pop("preview", False)))


@app.post("/api/jobs/{job_id}/clips/{clip_id}/effects/suggest")
def suggest_effects(job_id: str, clip_id: str, body: dict = Body(default={})):
    job = _job(job_id)
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not clip:
        raise HTTPException(404, "Clip not found")
    return jobs.suggest_effects(job, clip, body.get("preset") or "edit", start=body.get("start"), end=body.get("end"),
                                tighten=body.get("tighten"), cold_open=body.get("cold_open"), song=body.get("song"),
                                sfx_source=body.get("sfx_source"), sfx_fixed=body.get("sfx_fixed"))


def _clip(job, clip_id):
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not clip:
        raise HTTPException(404, "Clip not found")
    return clip


@app.post("/api/jobs/{job_id}/clips/{clip_id}/ideas")
def clip_ideas(job_id: str, clip_id: str):
    job = _job(job_id)
    clip = _clip(job, clip_id)
    settings = config.load_settings()
    ready, why = providers.readiness(settings)
    if not ready:
        raise HTTPException(400, f"Title ideas need a connected AI model ({why}). Set one up in Settings.")
    try:
        return ai.ideas(job, clip, settings)
    except Exception as e:
        raise HTTPException(502, providers.friendly(e, settings["ai_provider"]))


@app.post("/api/jobs/{job_id}/live/stop")
def live_stop(job_id: str):
    job = _job(job_id)
    if job.get("status") != "live":
        raise HTTPException(400, "This video isn't recording.")
    live.stop(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/live/resume")
def live_resume(job_id: str):
    job = _job(job_id)
    if not job.get("live_mode") or job.get("status") == "live":
        raise HTTPException(400, "This video isn't a stopped live recording.")
    try:
        src = sources.live_source(job.get("source_url") or job["url"], int(config.load_settings()["max_height"]))
    except Exception as e:
        raise HTTPException(400, f"Couldn't reach the stream: {e}")
    if not src:
        raise HTTPException(400, "The stream isn't live any more.")
    live.start(job)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/clips/{clip_id}/apply-style")
def apply_style(job_id: str, clip_id: str):
    job = _job(job_id)
    _clip(job, clip_id)
    return {"queued": jobs.apply_style(job_id, clip_id)}


@app.post("/api/jobs/{job_id}/render-all")
def render_all(job_id: str, body: dict = Body(default={})):
    _job(job_id)
    return {"queued": jobs.render_all(job_id, preview=bool(body.get("preview")))}


@app.post("/api/jobs/{job_id}/export-all")
def export_all(job_id: str):
    _job(job_id)
    paths = jobs.export_all(job_id)
    return {"count": len(paths), "folder": str(config.EXPORTS)}


SHARE = {"tiktok": "https://www.tiktok.com/tiktokstudio/upload",
         "instagram": "https://www.instagram.com/"}


@app.post("/api/jobs/{job_id}/clips/{clip_id}/share")
def share(job_id: str, clip_id: str, body: dict = Body(default={})):
    """Upload helper for TikTok and Instagram Reels: exports the file, shows it in Explorer, and returns the
    caption to copy and the upload page to open."""
    job = _job(job_id)
    clip = _clip(job, clip_id)
    platform = body.get("platform")
    if platform not in SHARE:
        raise HTTPException(400, "Unknown platform")
    try:
        path = jobs.export(job_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    subprocess.Popen(["explorer", "/select,", path])
    tags = " ".join("#" + t for t in (clip.get("hashtags") or [])[:5])
    caption = f"{clip['title']} {tags}".strip()
    return {"path": path, "caption": caption, "url": SHARE[platform]}


@app.get("/api/performance")
def get_performance():
    return performance.cached()


@app.post("/api/performance/refresh")
def refresh_performance():
    try:
        return performance.refresh([jobs.get(j["id"]) for j in jobs.list_jobs()])
    except Exception as e:
        raise HTTPException(400, f"Couldn't read your YouTube numbers: {e}")


@app.post("/api/performance/reset")
def reset_learning():
    performance.reset_learning()
    return {"ok": True}


@app.get("/api/update")
def check_update():
    return updater.check()


@app.post("/api/update")
def apply_update():
    try:
        return updater.apply()
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/effects")
def effects_catalog():
    return {"sounds": effects.sounds(), "visuals": effects.VISUALS, "emojis": effects.EMOJIS,
            "filters": {k: v[0] for k, v in effects.FILTERS.items()}, "presets": effects.PRESETS,
            "folder": str(effects.SFX_MINE)}


@app.get("/api/sfx/{kind}/{name}")
def sfx_file(kind: str, name: str):
    path, _ = effects.resolve_sound(f"{kind}/{name}")
    if not path:
        raise HTTPException(404)
    return FileResponse(path)


@app.post("/api/jobs/{job_id}/clips/{clip_id}/export")
def export_clip(job_id: str, clip_id: str):
    _job(job_id)
    try:
        path = jobs.export(job_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    subprocess.Popen(["explorer", "/select,", path])
    return {"path": path}


@app.post("/api/jobs/{job_id}/clips/{clip_id}/youtube")
def youtube_clip(job_id: str, clip_id: str, body: dict = Body(default={})):
    job = _job(job_id)
    if not any(c["id"] == clip_id for c in job["clips"]):
        raise HTTPException(404, "Clip not found")
    if not youtube.status()["connected"]:
        raise HTTPException(400, "Connect your YouTube channel in Settings first")
    try:
        return jobs.queue_upload(job_id, clip_id, body.get("mode", "next"), body.get("at"), body)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/jobs/{job_id}/youtube-all")
def youtube_all(job_id: str):
    _job(job_id)
    if not youtube.status()["connected"]:
        raise HTTPException(400, "Connect your YouTube channel in Settings first")
    return {"queued": jobs.schedule_all(job_id)}


@app.get("/api/youtube")
def youtube_status():
    return youtube.status()


@app.post("/api/youtube/client")
def youtube_client(body: dict = Body(...)):
    try:
        youtube.save_client(body.get("json") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return youtube.status()


@app.post("/api/youtube/connect")
def youtube_connect():
    try:
        youtube.connect()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return youtube.status()


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    youtube.disconnect()
    return youtube.status()


@app.get("/media/{job_id}/{clip_id}.mp4")
def media(job_id: str, clip_id: str, kind: str = "full"):
    job = _job(job_id)
    if not any(c["id"] == clip_id for c in job["clips"]):
        raise HTTPException(404)
    path = config.JOBS / job_id / (f"{clip_id}_preview.mp4" if kind == "preview" else f"{clip_id}.mp4")
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/music")
def music_library():
    return {"tracks": music.library(), "folder": str(config.MUSIC_MINE)}


@app.get("/api/music/search")
def music_search(mood: str = "hype", q: str = ""):
    return music.search(mood, q, config.load_settings())


@app.post("/api/music/download")
def music_download(body: dict = Body(...)):
    try:
        return music.download(body["track"], body.get("mood", ""))
    except Exception as e:
        raise HTTPException(400, f"Download failed: {e}")


@app.get("/api/music/trending")
def music_trending():
    try:
        return {"items": music.trending()}
    except Exception as e:
        return {"items": [], "error": str(e)}


@app.get("/api/music/file/{kind}/{name}")
def music_file(kind: str, name: str):
    path, _ = music.resolve(f"{kind}/{name}")
    if not path:
        raise HTTPException(404)
    return FileResponse(path)


@app.post("/api/open-folder")
def open_folder(body: dict = Body(...)):
    target = {"music": config.MUSIC_MINE, "exports": config.EXPORTS, "logs": config.DATA,
              "sfx": effects.SFX_MINE}.get(body.get("which"))
    if not target:
        raise HTTPException(400, "Unknown folder")
    subprocess.Popen(["explorer", str(target)])
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(config.ROOT / "ui"), html=True), name="ui")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8756)
    args = ap.parse_args()
    # data/clipper.log keeps what each job did and how long it took, for troubleshooting on other PCs.
    handler = logging.handlers.RotatingFileHandler(config.DATA / "clipper.log", maxBytes=2_000_000,
                                                   backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[handler, logging.StreamHandler()])
    for noisy in ("httpx", "httpcore", "faster_whisper", "googleapiclient", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("clipper").info("Clipper started")
    jobs.start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
