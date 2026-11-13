"""Local HTTP API + UI host. Bound to 127.0.0.1 only."""
import argparse
import subprocess

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, jobs, music, youtube

app = FastAPI(title="Clipper")


def _public_settings():
    s = config.load_settings()
    key = config.api_key(s)
    s["anthropic_api_key"] = ""
    s["has_api_key"] = bool(key)
    s["key_hint"] = ("…" + key[-4:]) if key else ""
    s["has_audd_key"] = bool(s.pop("audd_api_key"))
    s["claude_active"] = config.claude_enabled(config.load_settings())
    return s


@app.get("/api/settings")
def get_settings():
    return _public_settings()


@app.post("/api/settings")
def post_settings(patch: dict = Body(...)):
    # Blank secret fields mean "keep what's saved", so the UI never has to hold the key.
    for secret in ("anthropic_api_key", "audd_api_key"):
        if patch.get(secret) == "":
            patch.pop(secret)
    for k in patch.pop("clear", []):
        if k in ("anthropic_api_key", "audd_api_key"):
            patch[k] = ""
    config.save_settings(patch)
    return _public_settings()


@app.get("/api/jobs")
def list_jobs():
    return jobs.list_jobs()


@app.post("/api/jobs")
def create_job(body: dict = Body(...)):
    url = (body.get("url") or "").strip().strip('"')
    if not url:
        raise HTTPException(400, "Paste a Kick VOD link first")
    return jobs.create(url, body.get("kind") or "irl", body.get("topic") or "")


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
    return jobs.queue_render(job_id, clip_id, patch)


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
def media(job_id: str, clip_id: str):
    job = _job(job_id)
    if not any(c["id"] == clip_id for c in job["clips"]):
        raise HTTPException(404)
    path = config.JOBS / job_id / f"{clip_id}.mp4"
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
    target = {"music": config.MUSIC_MINE, "exports": config.EXPORTS}.get(body.get("which"))
    if not target:
        raise HTTPException(400, "Unknown folder")
    subprocess.Popen(["explorer", str(target)])
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(config.ROOT / "ui"), html=True), name="ui")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8756)
    args = ap.parse_args()
    jobs.start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
