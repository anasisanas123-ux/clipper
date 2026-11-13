"""Local (no-AI-API) moment finder: loudness spikes + transcript cues, scored 0-100."""
import re

import numpy as np

HYPE = re.compile(
    r"\b(no way|oh my god|omg|what the|wtf|holy|let'?s go+|insane|crazy|clip (that|it)|bro+|dude|"
    r"are you kidding|i can'?t believe|actually|literally|oh no+|wait wait|what just|did you see|"
    r"he'?s dead|i'?m dead|clutch|hacks?|cheat\w*|banned|rage|scam\w*|fuck\w*|shit|damn|hell)\b", re.I)
LAUGH = re.compile(r"\b(a?ha(ha)+|lol|lmao+|hehe+)\b", re.I)
MOODS = {
    "funny": re.compile(r"\b(a?ha(ha)+|lol|lmao+|funny|joke|hilarious)\b", re.I),
    "rage": re.compile(r"\b(fuck\w*|shit|hate|trash|garbage|stupid|rage|angry|pissed)\b", re.I),
    "tense": re.compile(r"\b(wait|careful|quiet|shh+|scared|oh no+|run|behind)\b", re.I),
    "hype": re.compile(r"\b(let'?s go+|insane|crazy|clutch|win|won|yes+|huge|massive)\b", re.I),
}


def _text_between(segments, a, b):
    return " ".join(s["text"] for s in segments if s["end"] > a and s["start"] < b)


def snap(segments, start, end, min_len, max_len):
    """Move the cut points onto speech boundaries so clips don't start or end mid-sentence."""
    starts = [s["start"] for s in segments if start - 6 <= s["start"] <= start + 3]
    if starts:
        start = min(starts, key=lambda t: abs(t - start))
    ends = [s["end"] for s in segments if end - 3 <= s["end"] <= end + 6]
    if ends:
        end = min(ends, key=lambda t: abs(t - end))
    if end - start < min_len:
        end = start + min_len
    if end - start > max_len:
        end = start + max_len
    return max(0.0, start - 0.3), end + 0.4


def _subscores(z_peak, text, length, hype_per_sec=0.0):
    words = len(text.split())
    rate = words / max(1.0, length)
    hype = len(HYPE.findall(text)) + 2 * len(LAUGH.findall(text)) + text.count("!")
    # Count only reactions beyond this streamer's usual rate, so constant swearing doesn't max the score.
    usual = hype_per_sec * length
    hype = (hype - usual) / max(1.0, usual ** 0.5) + 1
    return {
        "energy": int(np.clip(30 + 23 * (z_peak - 1), 0, 100)),
        "emotion": int(np.clip(30 + 9 * hype, 0, 100)),
        "pace": int(np.clip(100 - 45 * abs(rate - 3.0), 0, 100)) if words else 10,
        "length": int(np.clip(100 - 3.0 * max(0, abs(length - 32) - 10), 30, 100)),
    }


def local_score(sub) -> int:
    return int(round(0.4 * sub["energy"] + 0.3 * sub["emotion"] + 0.2 * sub["pace"] + 0.1 * sub["length"]))


def guess_mood(text, z_peak):
    counts = {m: len(r.findall(text)) for m, r in MOODS.items()}
    best = max(counts, key=counts.get)
    if counts[best] == 0:
        return "hype" if z_peak > 2.5 else "chill"
    return best


def _hook(text):
    """Punchiest short sentence in the clip, as a stand-in title when Claude is off."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if 2 <= len(p.split()) <= 8]
    if not parts:
        return " ".join(text.split()[:6])
    return max(parts, key=lambda p: len(HYPE.findall(p)) * 2 + p.count("!") + p.count("?"))


def _tag(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def make_hashtags(job, clip) -> list:
    """Hashtags built from the topic, streamer, content type and mood (used when Claude is off)."""
    tags = [_tag(t) for t in (job.get("topic") or "").split(",")]
    kind = job.get("kind") or "irl"
    tags += {"gaming": ["gaming", "gamingclips"], "film": ["movie", "reaction"], "irl": ["irl", "streamer"]}[kind]
    if not job.get("local"):
        tags += [_tag(job.get("streamer")), "kick", "kickclips"]
    tags += {"funny": ["funny"], "rage": ["rage"], "hype": ["hype"], "tense": ["intense"],
             "emotional": ["emotional"], "chill": []}.get(clip.get("mood"), [])
    out = []
    for t in tags:
        if t and t != "shorts" and t not in out:
            out.append(t)
    return out[:8]


def find_candidates(z, segments, settings, limit, kind="irl"):
    """Rank windows of the stream by loudness spike + transcript cues; keep the best non-overlapping ones."""
    n = len(z)
    if n == 0:
        return []
    min_len, max_len = settings["min_len"], settings["max_len"]
    target = int(np.clip(32, min_len, max_len))
    words_at = np.zeros(n)
    hype_at = np.zeros(n)
    for s in segments:
        i = min(n - 1, int(s["start"]))
        words_at[i] += len(s["text"].split())
        hype_at[i] += len(HYPE.findall(s["text"])) + 2 * len(LAUGH.findall(s["text"])) + s["text"].count("!")
    hype_per_sec = float(hype_at.sum()) / n
    scored = []
    for t in range(0, max(1, n - 10), 5):
        w = slice(t, min(n, t + target))
        zp = float(np.percentile(z[w], 95))
        words = float(words_at[w].sum())
        if words < 8 and kind != "gaming":   # outside gameplay, a wordless stretch rarely makes a good Short
            zp *= 0.5
        scored.append((zp + 0.35 * float(hype_at[w].sum()) + 0.01 * words, t, zp))
    scored.sort(reverse=True)
    picked = []
    for _, t, zp in scored:
        if all(abs(t - p[0]) >= target + 20 for p in picked):
            picked.append((t, zp))
        if len(picked) >= limit:
            break
    out = []
    for t, zp in picked:
        # Lead in to the spike: the setup usually sits before the loudest second.
        peak = t + int(np.argmax(z[t:min(n, t + target)]))
        start, end = snap(segments, max(0, peak - target * 0.6), peak + target * 0.4, min_len, max_len)
        text = _text_between(segments, start, end)
        sub = _subscores(zp, text, end - start, hype_per_sec)
        hook = _hook(text)
        reasons = []
        if sub["energy"] >= 60:
            reasons.append("Loudness spikes well above the stream's normal level")
        if sub["emotion"] >= 50:
            reasons.append("Strong reaction words or laughter in the speech")
        if sub["pace"] >= 70:
            reasons.append("Fast, continuous talking keeps attention")
        out.append({
            "start": round(start, 2), "end": round(end, 2), "title": hook[:80], "hook": hook[:60],
            "score": local_score(sub), "breakdown": sub, "reasons": reasons or ["Mild audio peak"],
            "mood": guess_mood(text, zp), "source": "local", "transcript": text,
        })
    out.sort(key=lambda m: -m["score"])
    return out
