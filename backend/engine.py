"""The combined clipping engine: the connected AI (e.g. Gemini) and the built-in algorithm working together.

1. The built-in detector ranks every stretch of the video by loudness, reaction words, quotable lines and pace.
2. The AI reads the whole transcript and picks its own moments, using the detector's picks as leads.
3. Both sets go into one pool. The AI judges every candidate side by side with the detector's measurements,
   drops the weak ones, tightens the cuts and writes titles, hooks and captions.
4. Each kept clip gets a combined score: mostly the AI's judgement, plus the built-in score (tuned to your
   channel's real views on the Results page), plus a bonus when both engines found the same moment.
5. The cut is checked against the loudness: if the biggest reaction falls right at (or just after) the end,
   the clip is extended so the payoff isn't cut off.
6. Titles that only repeat a line from the speech are rewritten.

Without a connected AI, steps 1, 5 and 6 still run, with titles written by the built-in engine."""
import logging

import numpy as np

from . import ai, moments, providers

log = logging.getLogger("clipper")
AI_WEIGHT, LOCAL_WEIGHT, AGREE_BONUS = 0.65, 0.35, 6


def _overlap(a, b) -> float:
    """How much of the shorter of two clips the other covers (0..1)."""
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return max(0.0, inter) / max(1.0, min(a["end"] - a["start"], b["end"] - b["start"]))


def polish(m, z, segments, settings, total):
    """Makes sure the payoff is inside the cut: when the loudest second around the end lands in its last second
    or just after it, the end moves to a little after that peak (on a sentence end when there is one)."""
    s, e = float(m["start"]), float(m["end"])
    a, b = int(max(0, e - 6)), int(min(len(z), e + 4))
    if b - a < 3:
        return m
    peak = a + int(np.argmax(z[a:b]))
    if peak >= e - 1.0 and z[peak] >= 1.5:
        want = min(peak + 1.8, s + settings["max_len"], total or peak + 1.8)
        ends = [x["end"] for x in segments if want - 0.3 <= x["end"] <= want + 2.5 and x["end"] - s <= settings["max_len"]]
        new_end = round(min(ends, key=lambda t: abs(t - want)) + 0.3 if ends else want, 2)
        if new_end > e:
            m["end"] = min(new_end, total) if total else new_end
            m["transcript"] = moments._text_between(segments, s, m["end"])
    return m


def _local_view(m, z, segments, kind, rates):
    """The built-in engine's score and signals for any clip, whoever found it."""
    a, b = int(m["start"]), max(int(m["start"]) + 1, int(np.ceil(m["end"])))
    seg = z[a:min(len(z), b)]
    zp = float(np.percentile(seg, 95)) if len(seg) else 0.0
    text = moments._text_between(segments, m["start"], m["end"])
    sub = moments._subscores(zp, text, m["end"] - m["start"], rates[0], rates[1], kind)
    return moments.local_score(sub, kind), sub


def fuse(found, local, z, segments, settings, kind, name="the AI"):
    """Combined score for AI-found clips; built-in clips keep their own. Overlaps are removed, best first."""
    rates = moments.video_rates(segments, len(z))
    strong = sorted(local, key=lambda m: -m["score"])[: max(3, len(local) // 2)]
    out = []
    for m in found:
        polish(m, z, segments, settings, len(z))
        lscore, sub = _local_view(m, z, segments, kind, rates)
        m["signals"] = sub
        if m.get("source") != "ai":
            out.append(m)
            continue
        agree = max((_overlap(m, l) for l in strong), default=0.0)
        a = m["score"]
        m["score"] = int(np.clip(round(AI_WEIGHT * a + LOCAL_WEIGHT * lscore + AGREE_BONUS * agree), 0, 99))
        m["engine"] = {"ai": a, "builtin": lscore, "agreement": round(agree, 2)}
        m["breakdown"] = {**(m.get("breakdown") or {}), "loudness": sub["energy"]}
        if agree >= 0.5:
            m["reasons"] = [f"Both {name} and the built-in detector picked this moment", *(m.get("reasons") or [])][:4]
        out.append(m)
    out.sort(key=lambda m: -m["score"])
    kept = []
    for m in out:
        if all(_overlap(m, k) < 0.35 for k in kept):
            kept.append(m)
    return kept


def fix_titles(clips, job, settings, use_ai):
    """Rewrites titles that only repeat what was said: by the AI when one is connected, else by the built-in
    engine. Built-in clips always get a written title rather than a quote."""
    who = job.get("streamer") if not job.get("local") else ""
    bad = [c for c in clips if c.get("source") != "ai" or moments.quotes_speech(c["title"], c.get("transcript", ""))]
    if not bad:
        return
    new = [""] * len(bad)
    if use_ai and any(c.get("source") == "ai" for c in bad):
        try:
            new = ai.retitle(bad, job, settings)
        except Exception:
            log.exception("[%s] retitling failed", job["id"])
    for i, (c, t) in enumerate(zip(bad, new)):
        if not t or moments.quotes_speech(t, c.get("transcript", "")):
            t = moments.make_title(c.get("transcript", ""), c.get("mood"), job.get("kind"), who, job.get("topic", ""), i)
        if c.get("source") != "ai":
            c["hook"] = c.get("hook") or t[:60]
        c["title"] = t


def find(job, segments, z, settings, n, short, stage):
    """Returns (clips best first, AI usage info or None, notes for the user)."""
    notes, kind = [], job.get("kind", "irl")
    duration = float(job.get("duration") or len(z))
    if short:
        local = [moments.whole_clip(z, segments, duration, kind)]
    else:
        local = moments.find_candidates(z, segments, settings, limit=max(16, n * 3), kind=kind)
    ready, why = providers.readiness(settings)
    if settings.get("scorer") == "ai" and not ready:
        notes.append(f"Connected AI is selected but not set up ({why}), so only the built-in algorithm scored this "
                     f"video. Finish the setup in Settings.")
    if not (ready and segments):
        clips = fuse(local, local, z, segments, settings, kind)
        fix_titles(clips, job, settings, False)
        return clips, None, notes

    name = providers.label(settings["ai_provider"])
    info, picked = None, []
    stage(f"{name} is reading the transcript")
    try:
        picked, info = ai.pick_moments(segments, z, job, settings, 1 if short else n + 3, local, whole=short)
    except Exception as e:
        log.exception("[%s] AI moment picking failed", job["id"])
        notes.append(f"{name}'s first pass failed ({providers.friendly(e, settings['ai_provider'])}); it judged the "
                     f"built-in picks instead.")
    if short:
        clips = fuse(picked or local, local, z, segments, settings, kind, name)
        fix_titles(clips, job, settings, bool(picked))
        return clips, info, notes

    for p in picked:
        p["agreed"] = any(_overlap(p, l) >= 0.5 for l in local)
    extra = [l for l in local if all(_overlap(l, p) < 0.4 for p in picked)][: max(4, min(12, n))]
    pool = picked + extra
    stage(f"{name} and the built-in engine are comparing {len(pool)} moments")
    judged = None
    try:
        judged, info2 = ai.judge(pool, segments, z, job, settings)
        info = _add_usage(info, info2)
        if not judged:
            notes.append(f"{name} rejected every candidate in its final check, so its first picks are shown.")
    except Exception as e:
        log.exception("[%s] AI judging failed", job["id"])
        if picked:
            notes.append(f"{name}'s final check failed ({providers.friendly(e, settings['ai_provider'])}), so its "
                         f"first picks were combined with the built-in scores.")
        else:
            notes.append(f"{name} failed ({providers.friendly(e, settings['ai_provider'])}), so the built-in "
                         f"algorithm was used.")
    if judged:
        for j in judged:
            src = pool[j.pop("candidate")]
            j["found_by"] = "both" if src.get("agreed") else ("ai" if src.get("source") == "ai" else "builtin")
        clips = judged
    else:
        clips = picked or local
    clips = fuse(clips, local, z, segments, settings, kind, name)
    stage("Checking the titles")
    fix_titles(clips, job, settings, bool(judged or picked))
    return clips, info, notes


def _add_usage(a, b):
    if not a:
        return b
    out = dict(a)
    for k in ("input_tokens", "output_tokens", "cost_usd"):
        if a.get(k) is not None and b.get(k) is not None:
            out[k] = round(a[k] + b[k], 3) if k == "cost_usd" else a[k] + b[k]
    return out
