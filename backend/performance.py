"""How uploaded Shorts actually did, compared with Clipper's viral-chance score, and learning from it.

Views come from YouTube for every clip uploaded through Clipper. Once a content type has enough settled
uploads (older than three days), the built-in algorithm's weights for that type are re-fitted toward what got
views on this channel, blended half-and-half with the defaults so a few lucky videos can't swing it."""
import json
import math
from datetime import datetime, timezone

import numpy as np

from . import config, moments, youtube

CACHE = config.DATA / "performance.json"
LEARNED = config.DATA / "learned_weights.json"
MIN_SAMPLES = 8
SIGNALS = ("energy", "emotion", "quotable", "pace", "length")


def _age_days(iso):
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 86400)
    except Exception:
        return None


def _signals(job, clip):
    if clip.get("signals"):
        return clip["signals"]
    # older clips: recompute the built-in sub-scores from the saved loudness and transcript
    d = config.JOBS / job["id"]
    try:
        z = np.load(d / "excitement.npy")
        a, b = int(clip["start"]), max(int(clip["start"]) + 1, int(clip["end"]))
        zp = float(np.percentile(z[a:b], 95)) if b <= len(z) else 0.0
        return moments._subscores(zp, clip.get("transcript", ""), clip["end"] - clip["start"], kind=job.get("kind", "irl"))
    except Exception:
        return None


def _spearman(x, y):
    if len(x) < 4:
        return None
    rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def refresh(all_jobs: list) -> dict:
    rows = []
    for job in all_jobs:
        for c in job.get("clips", []):
            yt = c.get("youtube") or {}
            if yt.get("video_id"):
                rows.append({"job": job["id"], "job_title": job.get("title", ""), "kind": job.get("kind", "irl"),
                             "clip": c["id"], "title": c.get("title", ""), "score": c.get("score"),
                             "scored_by": c.get("scored_by") or ("built-in" if c.get("source") == "local" else c.get("source")),
                             "video_id": yt["video_id"], "signals": _signals(job, c)})
    numbers = youtube.stats([r["video_id"] for r in rows]) if rows else {}
    for r in rows:
        n = numbers.get(r["video_id"])
        if n:
            r.update(n)
            r["age_days"] = _age_days(n.get("published"))
            r["views_per_day"] = round(r["views"] / max(1.0, r["age_days"] or 1.0), 1)
        else:
            r["missing"] = True
    report = {"fetched": datetime.now(timezone.utc).isoformat(), "rows": rows, "kinds": {}}
    learned = json.loads(LEARNED.read_text("utf-8")) if LEARNED.exists() else {}
    for kind in moments.WEIGHTS:
        settled = [r for r in rows if r["kind"] == kind and not r.get("missing") and (r.get("age_days") or 0) >= 3
                   and r.get("privacy") == "public"]
        info = {"uploads": len([r for r in rows if r["kind"] == kind]), "settled": len(settled)}
        if len(settled) >= 4:
            info["score_vs_views"] = _spearman([r["score"] or 0 for r in settled], [math.log1p(r["views"]) for r in settled])
        if len(settled) >= MIN_SAMPLES:
            fit = _fit([r for r in settled if r.get("signals")], kind)
            if fit:
                learned[kind] = fit
                info["learned"] = fit
        report["kinds"][kind] = info
    LEARNED.write_text(json.dumps(learned, indent=1), "utf-8")
    CACHE.write_text(json.dumps(report, indent=1), "utf-8")
    moments.load_learned()
    return report


def _fit(rows, kind):
    if len(rows) < MIN_SAMPLES:
        return None
    X = np.array([[r["signals"].get(k, 50) / 100 for k in SIGNALS] for r in rows], dtype=float)
    y = np.array([math.log1p(r["views"]) for r in rows], dtype=float)
    X = (X - X.mean(0)) / (X.std(0) + 1e-6)
    y = (y - y.mean()) / (y.std() + 1e-6)
    coef = np.linalg.solve(X.T @ X + 2.0 * np.eye(len(SIGNALS)), X.T @ y)   # ridge: stays calm with few videos
    pos = np.clip(coef, 0, None)
    if pos.sum() <= 0:
        return None
    base = moments.DEFAULT_WEIGHTS[kind]
    fitted = pos / pos.sum()
    w = {k: round(0.5 * base[k] + 0.5 * float(f), 3) for k, f in zip(SIGNALS, fitted)}
    total = sum(w.values())
    return {k: round(v / total, 3) for k, v in w.items()}


def cached() -> dict:
    return json.loads(CACHE.read_text("utf-8")) if CACHE.exists() else {"rows": [], "kinds": {}, "fetched": None}


def reset_learning() -> None:
    LEARNED.unlink(missing_ok=True)
    moments.load_learned()
