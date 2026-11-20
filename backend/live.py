"""Clipping a stream while it's live.

A recorder thread saves the stream in short pieces (CHUNK seconds each) with ffmpeg, reconnecting if it drops.
An analyser thread takes each finished piece: transcribes it, adds its loudness to the stream's running curve,
and looks for moments in the newest stretch (plus a little of the piece before, so moments that straddle two
pieces are found whole). Good moments become clips straight away: their footage is cut from the recorded
pieces at once, because a live stream can't be downloaded again later, and old pieces are then deleted so the
disk doesn't fill up over a long stream.

Times are seconds of recorded stream since recording started (a reconnect gap is skipped, not counted)."""
import json
import logging
import subprocess
import threading
import time

import numpy as np

from . import ai, audio, config, engine, jobs, moments, providers, sources, transcribe

log = logging.getLogger("clipper")
CHUNK = 120            # seconds per recorded piece
LOOKBACK = 75          # seconds of the previous piece included when looking for moments
TAIL = 12              # moments ending this close to the newest edge wait for the next piece
KEEP = 240             # seconds of already-analysed pieces kept on disk
PAD = jobs.PAD

_sessions: dict = {}


class Session:
    def __init__(self, job):
        self.job = job
        self.stop_flag = threading.Event()
        self.proc = None
        self.recorder_done = threading.Event()
        self.threads = []


def _d(job):
    return config.JOBS / job["id"]


def _state(job, text):
    job["live"]["state"] = text
    job["live"]["updated"] = time.time()
    jobs._save(job)


def start(job):
    """Begins (or resumes) recording a live job."""
    if job["id"] in _sessions:
        return
    (_d(job) / "live").mkdir(exist_ok=True)
    lv = job.setdefault("live", {})
    lv.update(recording=True, state="Connecting to the stream", error=None, started=lv.get("started") or time.time(),
              stopping=False)
    lv.setdefault("pieces", [])           # [{"n", "file", "start", "dur", "done"}]
    lv.setdefault("recorded", 0.0)
    lv.setdefault("analysed", 0.0)
    job.update(status="live", live_mode=True, stage="Live", progress=0.0, error=None)
    jobs._save(job)
    s = Session(job)
    _sessions[job["id"]] = s
    for target in (_record, _analyse):
        t = threading.Thread(target=target, args=(s,), daemon=True)
        s.threads.append(t)
        t.start()
    log.info("[%s] live recording started", job["id"])


def stop(job_id, wait=False):
    s = _sessions.get(job_id)
    if not s:
        return
    s.stop_flag.set()
    s.job["live"]["stopping"] = True
    _quit_ffmpeg(s)
    if wait:
        for t in s.threads:
            t.join(timeout=60)


def _quit_ffmpeg(s):
    p = s.proc
    if p and p.poll() is None:
        try:
            p.stdin.write(b"q")       # lets ffmpeg close the current piece cleanly
            p.stdin.flush()
        except Exception:
            pass
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


# ---------------------------------------------------------------- recording
def _record(s):
    job = s.job
    lv = job["live"]
    settings = config.load_settings()
    folder = _d(job) / "live"
    fails = 0
    try:
        while not s.stop_flag.is_set():
            try:
                src = sources.live_source(job.get("source_url") or job["url"], int(settings["max_height"]))
            except Exception as e:
                src, err = "error", e
            if src is None:
                _state(job, "The stream has ended")
                break
            if src == "error":
                fails += 1
                if fails >= 6:
                    lv["error"] = f"Couldn't reach the stream: {str(err)[-300:]}"
                    break
                _state(job, f"Couldn't reach the stream, retrying ({fails})")
                s.stop_flag.wait(10)
                continue
            first = max([p["n"] for p in lv["pieces"]], default=-1) + 1
            # existing files on disk from a previous run also count
            on_disk = [int(f.stem[1:]) for f in folder.glob("p*.ts") if f.stem[1:].isdigit()]
            first = max([first, *[n + 1 for n in on_disk]])
            net = [*sources.http_args(src["headers"]), "-reconnect", "1", "-reconnect_streamed", "1",
                   "-reconnect_delay_max", "10"]
            inputs = [*net, "-i", src["url"]]
            amap = ["-map", "0:a:0?"]
            if src.get("audio_url"):
                inputs += [*net, "-i", src["audio_url"]]
                amap = ["-map", "1:a:0"]
            cmd = [config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
                   "-map", "0:v:0", *amap, "-c", "copy",
                   "-f", "segment", "-segment_time", str(CHUNK), "-reset_timestamps", "1",
                   "-segment_start_number", str(first), str(folder / "p%06d.ts")]
            _state(job, "Recording")
            started = time.time()
            s.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                      creationflags=config.NO_WINDOW)
            # read errors until ffmpeg exits (stdin stays open so "q" can stop it cleanly)
            err_text = s.proc.stderr.read().decode("utf-8", "replace")
            s.proc.wait()
            if s.stop_flag.is_set():
                break
            if time.time() - started > 60:
                fails = 0
            fails += 1
            log.warning("[%s] live recorder stopped (%s), reconnecting", job["id"], err_text.strip()[-300:])
            _state(job, "The stream dropped, reconnecting")
            s.stop_flag.wait(5)
    except Exception as e:
        log.exception("[%s] live recorder failed", job["id"])
        lv["error"] = str(e)[-300:]
    finally:
        s.recorder_done.set()


# ---------------------------------------------------------------- analysing
def _finished_pieces(s):
    """Pieces whose file is complete: every one except the newest while ffmpeg is still writing."""
    folder = _d(s.job) / "live"
    files = sorted(folder.glob("p*.ts"))
    if not s.recorder_done.is_set():
        files = files[:-1]
    known = {p["n"] for p in s.job["live"]["pieces"]}
    return [f for f in files if int(f.stem[1:]) not in known and f.stat().st_size > 0]


def _analyse(s):
    job = s.job
    lv = job["live"]
    d = _d(job)
    try:
        while True:
            ready = _finished_pieces(s)
            for f in ready:
                _process_piece(s, f)
            if not ready:
                if s.recorder_done.is_set():
                    break
                s.stop_flag.wait(3)
                if s.stop_flag.is_set() and not s.recorder_done.wait(15):
                    _quit_ffmpeg(s)
    except Exception as e:
        log.exception("[%s] live analysis failed", job["id"])
        lv["error"] = str(e)[-400:]
    finally:
        # final pass over the newest stretch now nothing more is coming
        try:
            _find(s, final=True)
        except Exception:
            log.exception("[%s] final live pass failed", job["id"])
        lv.update(recording=False, stopping=False)
        if not lv.get("error"):
            lv["state"] = "Stopped" if s.stop_flag.is_set() else "The stream has ended"
        job["status"] = "ready"
        job["duration"] = lv["recorded"]
        _sessions.pop(job["id"], None)
        jobs._save(job)
        log.info("[%s] live recording finished: %.0f s recorded, %d clips", job["id"], lv["recorded"], len(job["clips"]))


def _process_piece(s, f):
    job, lv, d = s.job, s.job["live"], _d(s.job)
    settings = config.load_settings()
    n = int(f.stem[1:])
    wav = d / "live" / f"{f.stem}.wav"
    sources.extract_wav(f, wav)
    dur = audio.wav_duration(wav)
    if dur < 1:
        wav.unlink(missing_ok=True)
        f.unlink(missing_ok=True)
        return
    start = lv["recorded"]
    _state(job, f"Listening to {_clock(start)}–{_clock(start + dur)}")

    # loudness: append this piece's per-second curve and recompute the whole stream's excitement
    rms_file = d / "rms.npy"
    rms = np.load(rms_file) if rms_file.exists() else np.zeros(0, dtype=np.float32)
    base = int(round(start))           # keep second i of the curve at second i of the stream over long streams
    if len(rms) < base:
        rms = np.concatenate([rms, np.full(base - len(rms), rms[-1] if len(rms) else -60.0, dtype=np.float32)])
    rms = np.concatenate([rms[:base], audio.rms_curve(wav)]).astype(np.float32)
    np.save(rms_file, rms)
    np.save(d / "excitement.npy", audio.excitement(rms))

    segs, info = transcribe.transcribe_full(wav, settings, dur, lambda t, p: None)
    job["engine"] = info
    tr_file = d / "transcript.json"
    transcript = json.loads(tr_file.read_text("utf-8")) if tr_file.exists() else []
    transcript += [{**x, "start": round(x["start"] + start, 2), "end": round(x["end"] + start, 2)} for x in segs]
    tr_file.write_text(json.dumps(transcript), "utf-8")
    wav.unlink(missing_ok=True)

    lv["pieces"].append({"n": n, "file": f.name, "start": round(start, 3), "dur": round(dur, 3)})
    lv["recorded"] = round(start + dur, 3)
    job["duration"] = lv["recorded"]
    _find(s)
    lv["analysed"] = lv["recorded"]
    _cleanup(s)
    _state(job, "Recording" if not s.recorder_done.is_set() else "Finishing")


def _find(s, final=False):
    """Looks for moments in the newest stretch and turns the good ones into clips."""
    job, lv, d = s.job, s.job["live"], _d(s.job)
    if not lv["pieces"]:
        return
    settings = jobs.templates.job_settings(config.load_settings(), job)
    if settings.get("scorer") == "chat":
        settings["scorer"] = "builtin"      # a live stream can't wait for a pasted answer
    end = lv["recorded"]
    w0 = max(0.0, end - CHUNK - LOOKBACK)
    z_all = np.load(d / "excitement.npy")
    transcript = json.loads((d / "transcript.json").read_text("utf-8"))
    zw = z_all[int(w0): int(np.ceil(end))]
    segs = [{**x, "start": x["start"] - w0, "end": x["end"] - w0} for x in transcript if x["end"] > w0]
    if len(zw) < settings["min_len"]:
        return
    kind = job.get("kind", "irl")
    local = found = moments.find_candidates(zw, segs, settings, limit=4, kind=kind)
    ready, _ = providers.readiness(settings)
    used_ai = False
    if ready and segs:
        try:
            picked, info = ai.pick_moments(segs, zw, dict(job, duration=len(zw)), settings, 2, found)
            if picked:
                found, job["ai"], used_ai = picked, info, True
        except Exception as e:
            log.warning("[%s] live AI scoring failed: %s", job["id"], e)
            note = f"{providers.label(settings['ai_provider'])} failed on a live piece, so the built-in algorithm scored it."
            if note not in job["warnings"]:
                job["warnings"].append(note)
    # the same combined scoring and title check as for whole videos (without the extra judging call)
    found = engine.fuse(found, local, zw, segs, settings, kind, providers.label(settings["ai_provider"]))
    engine.fix_titles(found, job, settings, used_ai)
    limit_end = end - (0 if final else TAIL)
    floor = int(settings.get("live_min_score", 60))
    for m in sorted(found, key=lambda m: -m["score"]):
        a, b = m["start"] + w0, m["end"] + w0
        if b > limit_end or m["score"] < floor:
            continue
        if any(a < c["end"] and b > c["start"] for c in job["clips"]):
            continue
        m.update(start=round(a, 2), end=round(b, 2))
        _make_clip(s, m, settings)


def _make_clip(s, m, settings):
    job, lv, d = s.job, s.job["live"], _d(s.job)
    cid = f"c{len(job['clips']) + 1}"
    while any(c["id"] == cid for c in job["clips"]):
        cid = f"c{int(cid[1:]) + 1}"
    s0 = max(0.0, m["start"] - PAD)
    s1 = min(lv["recorded"], m["end"] + PAD)
    _cut(lv["pieces"], d, s0, s1, d / f"{cid}_src.mp4")
    m.update(src_start=s0, src_end=s1)
    jobs.init_clip(job, m, cid, settings)
    m["found_live_at"] = time.time()
    with jobs._lock:
        job["clips"].append(m)
    jobs._save(job)
    log.info("[%s] live moment %s at %s (score %s)", job["id"], cid, _clock(m["start"]), m["score"])
    if settings.get("live_auto_render", True):
        jobs.queue_render(job["id"], cid)


def _cut(pieces, d, s0, s1, out):
    """Frame-exact cut of recorded stream time [s0, s1] from the pieces on disk."""
    use = [p for p in pieces if p["start"] < s1 and p["start"] + p["dur"] > s0 and (d / "live" / p["file"]).exists()]
    if not use:
        raise RuntimeError("That part of the stream is no longer on disk.")
    lst = d / "live" / f"cut_{out.stem}.txt"
    lst.write_text("".join(f"file '{p['file']}'\n" for p in use), "utf-8")
    off = s0 - use[0]["start"]
    try:
        config.run_ffmpeg(["-f", "concat", "-safe", "0", "-i", lst.name, "-ss", f"{max(0, off):.3f}",
                           "-t", f"{s1 - s0:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
                           "-c:v", *config.video_encoder(), *config.quality_args(18), "-pix_fmt", config.pix_fmt(),
                           "-c:a", "aac", "-b:a", "192k", str(out)], cwd=str(d / "live"), timeout=900)
    finally:
        lst.unlink(missing_ok=True)


def _cleanup(s):
    """Deletes analysed pieces older than KEEP seconds; every clip's footage was already cut out of them."""
    lv, d = s.job["live"], _d(s.job)
    edge = lv["recorded"] - KEEP
    for p in lv["pieces"]:
        if p["start"] + p["dur"] < edge:
            (d / "live" / p["file"]).unlink(missing_ok=True)


def _clock(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"
