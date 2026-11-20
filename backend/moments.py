"""Local (no-AI-API) moment finder: loudness spikes + transcript cues, scored 0-100.

Streams and podcasts go viral for different reasons. A stream moment is usually a loud reaction; a podcast
moment is usually something said: a hot take, a surprising fact, a story with a payoff, a big laugh. So each
content type weighs the signals differently."""
import re

import numpy as np

HYPE = re.compile(
    r"\b(no way|oh my god|omg|what the|wtf|holy|let'?s go+|insane|crazy|clip (that|it)|bro+|dude|"
    r"are you kidding|i can'?t believe|actually|literally|oh no+|wait wait|what just|did you see|"
    r"he'?s dead|i'?m dead|clutch|hacks?|cheat\w*|banned|rage|scam\w*|fuck\w*|shit|damn|hell)\b", re.I)
LAUGH = re.compile(r"\b(a?ha(ha)+|lol|lmao+|hehe+)\b", re.I)
# Phrases that mark a quotable podcast moment: strong opinions, confessions, numbers, advice, pushback.
QUOTE = re.compile(
    r"\b(the truth is|truth|nobody (talks|tells|knows)|no one (talks|tells|knows)|here'?s the thing|"
    r"the (biggest|worst|best|number one|only) (mistake|thing|reason|advice|lesson|problem|secret)|"
    r"i'?ve never (told|said|talked)|first time i'?ve|unpopular opinion|hot take|controversial|"
    r"the reason (why|is)|secret|most people|everyone thinks|people don'?t (realize|understand)|"
    r"you (need|have) to|stop \w+ing|if you want|changed my life|i was (broke|homeless|depressed|lost)|"
    r"million|billion|\$\s?\d|\d+ ?(percent|%)|lied|lie|toxic|disagree|that'?s not true|you'?re wrong|"
    r"i regret|my biggest|cheated|fired|divorce|died|cancer|rich|money|prison|jail|scam\w*)\b", re.I)
MOODS = {
    "funny": re.compile(r"\b(a?ha(ha)+|lol|lmao+|funny|joke|hilarious)\b", re.I),
    "rage": re.compile(r"\b(fuck\w*|shit|hate|trash|garbage|stupid|rage|angry|pissed)\b", re.I),
    "tense": re.compile(r"\b(wait|careful|quiet|shh+|scared|oh no+|run|behind)\b", re.I),
    "hype": re.compile(r"\b(let'?s go+|insane|crazy|clutch|win|won|yes+|huge|massive)\b", re.I),
    "emotional": re.compile(r"\b(cried|cry|died|passed away|depressed|grateful|love (you|her|him)|miss (her|him))\b", re.I),
}
# Sub-score weights per content type; a podcast leans on what's said, a stream on how loud it gets.
WEIGHTS = {
    "irl": {"energy": 0.35, "emotion": 0.3, "quotable": 0.1, "pace": 0.15, "length": 0.1},
    "gaming": {"energy": 0.45, "emotion": 0.3, "quotable": 0.0, "pace": 0.15, "length": 0.1},
    "film": {"energy": 0.3, "emotion": 0.35, "quotable": 0.1, "pace": 0.15, "length": 0.1},
    "podcast": {"energy": 0.1, "emotion": 0.2, "quotable": 0.4, "pace": 0.2, "length": 0.1},
}
DEFAULT_WEIGHTS = {k: dict(v) for k, v in WEIGHTS.items()}


def load_learned():
    """Weights re-fitted from this channel's real views (see performance.py) replace the defaults per type."""
    import json
    from . import config
    for k, v in DEFAULT_WEIGHTS.items():
        WEIGHTS[k] = dict(v)
    f = config.DATA / "learned_weights.json"
    try:
        for k, v in (json.loads(f.read_text("utf-8")) if f.exists() else {}).items():
            if k in WEIGHTS and set(v) == set(WEIGHTS[k]):
                WEIGHTS[k] = v
    except Exception:
        pass


TARGET_LEN = {"podcast": 45}   # podcast moments need room for the setup; streams default to ~32s


def _text_between(segments, a, b):
    return " ".join(s["text"] for s in segments if s["end"] > a and s["start"] < b)


def snap(segments, start, end, min_len, max_len, total=None):
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
    start, end = max(0.0, start - 0.3), end + 0.4
    if total:
        end = min(end, total)
        start = max(0.0, min(start, end - min_len))
    return start, end


def _counts(text):
    return (len(HYPE.findall(text)) + 2 * len(LAUGH.findall(text)) + text.count("!"),
            len(QUOTE.findall(text)) + text.count("?") * 0.5)


def _subscores(z_peak, text, length, hype_per_sec=0.0, quote_per_sec=0.0, kind="irl"):
    words = len(text.split())
    rate = words / max(1.0, length)
    hype, quote = _counts(text)
    # Count only what's above this video's usual rate, so a creator who always swears doesn't max the score.
    usual = hype_per_sec * length
    hype = (hype - usual) / max(1.0, usual ** 0.5) + 1
    usual_q = quote_per_sec * length
    quote = (quote - usual_q) / max(1.0, usual_q ** 0.5) + 1
    target = TARGET_LEN.get(kind, 32)
    return {
        "energy": int(np.clip(30 + 23 * (z_peak - 1), 0, 100)),
        "emotion": int(np.clip(30 + 9 * hype, 0, 100)),
        "quotable": int(np.clip(25 + 15 * quote, 0, 100)),
        "pace": int(np.clip(100 - 45 * abs(rate - 3.0), 0, 100)) if words else 10,
        "length": int(np.clip(100 - 3.0 * max(0, abs(length - target) - 12), 30, 100)),
    }


def local_score(sub, kind="irl") -> int:
    w = WEIGHTS.get(kind, WEIGHTS["irl"])
    return int(round(sum(w[k] * sub[k] for k in w)))


def _shown(sub, kind):
    """Only the sub-scores that count for this content type are shown."""
    w = WEIGHTS.get(kind, WEIGHTS["irl"])
    return {k: v for k, v in sub.items() if w.get(k, 0) > 0}


def guess_mood(text, z_peak):
    counts = {m: len(r.findall(text)) for m, r in MOODS.items()}
    best = max(counts, key=counts.get)
    if counts[best] == 0:
        return "hype" if z_peak > 2.5 else "chill"
    return best


def _hook(text, kind="irl"):
    """Punchiest short sentence in the clip, as a stand-in title when Claude is off."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if 3 <= len(p.split()) <= 10]
    if not parts:
        return " ".join(text.split()[:7])
    if kind == "podcast":
        return max(parts, key=lambda p: len(QUOTE.findall(p)) * 3 + p.count("?") + len(LAUGH.findall(p)))
    return max(parts, key=lambda p: len(HYPE.findall(p)) * 2 + p.count("!") + p.count("?"))


_WORD = re.compile(r"[a-z0-9']+")


def quotes_speech(title: str, text: str) -> bool:
    """True when a title is just a line from the speech: it shares a run of four words with the transcript, or a
    short title appears in it word for word."""
    t = _WORD.findall((title or "").lower())
    s = _WORD.findall((text or "").lower())
    if len(t) < 3 or not s:
        return False
    n = 4 if len(t) >= 4 else 3
    grams = {tuple(s[i:i + n]) for i in range(len(s) - n + 1)}
    return any(tuple(t[i:i + n]) in grams for i in range(len(t) - n + 1))


# Titles the built-in engine writes from what kind of moment it is, instead of quoting the speech.
TITLES = {
    "funny": ["{who} couldn't keep a straight face after this", "Chat lost it after this moment",
              "{who} was not ready for this", "This had {who} crying laughing"],
    "rage": ["{who} completely lost it here", "This is the moment {who} snapped",
             "{who} was so done with this", "Nobody could calm {who} down after this"],
    "tense": ["This got way too intense", "{who} knew something was wrong", "Wait for what happens next",
              "The tension in this moment was unreal"],
    "hype": ["{who} went absolutely crazy here", "Nobody expected this to happen{topic}",
             "This is why people watch {who}", "{who} pulled this off live"],
    "emotional": ["{who} got real for a second", "This moment hit different", "{who} opened up about this",
                  "Nobody expected {who} to say this"],
    "chill": ["{who} explained this perfectly", "This changed how I see it", "{who} said what everyone was thinking",
              "Wait until you hear this"],
}
QUOTE_TITLES = ["{who} said what nobody wants to hear", "{who}'s take on this is controversial",
                "{who} explains the truth about this", "This is the part nobody talks about"]
MONEY = re.compile(r"(\$\s?[\d,.]+\s?(k|m|million|thousand)?|\b\d[\d,.]*\s?(k|million|billion|thousand|grand|dollars)\b)", re.I)


def make_title(text, mood, kind, who="", topic="", seed=0) -> str:
    """A descriptive title for a moment (built-in engine), built from its mood, the creator and the topic."""
    who = (who or "").strip() or ("He" if kind != "podcast" else "This guy")
    pool = QUOTE_TITLES if kind == "podcast" and QUOTE.search(text or "") else TITLES.get(mood, TITLES["hype"])
    money = MONEY.search(text or "")
    if money and kind != "podcast":
        pool = [f"{{who}} and the {money.group(0).strip()} moment", *pool]
    # patterns start with a capital or the creator's name, which keeps its own spelling (xQc, not XQc)
    return pool[seed % len(pool)].format(who=who, topic=f" in {topic}" if topic and kind == "gaming" else "")


def video_rates(segments, n):
    """Reaction words and quotable cues per second across the whole video, to judge a clip against its video."""
    hype = quote = 0.0
    for s in segments:
        h, q = _counts(s["text"])
        hype, quote = hype + h, quote + q
    return hype / max(1, n), quote / max(1, n)


def _reasons(sub, kind):
    out = []
    if sub["energy"] >= 60:
        out.append("Loudness spikes well above the video's normal level")
    if sub["emotion"] >= 50:
        out.append("Strong reaction words or laughter in the speech")
    if sub["quotable"] >= 55 and WEIGHTS[kind]["quotable"] > 0:
        out.append("Quotable line: a strong opinion, number, confession or bit of advice")
    if sub["pace"] >= 70:
        out.append("Fast, continuous talking keeps attention")
    if not out:
        out.append("No strong signal; a mild peak compared with the rest of the video")
    return out


def _tag(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def make_hashtags(job, clip) -> list:
    """Hashtags built from the topic, creator, content type and mood (used when Claude is off)."""
    tags = [_tag(t) for t in (job.get("topic") or "").split(",")]
    kind = job.get("kind") or "irl"
    tags += {"gaming": ["gaming", "gamingclips"], "film": ["movie", "reaction"], "irl": ["irl", "streamer"],
             "podcast": ["podcast", "podcastclips"]}.get(kind, [])
    if not job.get("local"):
        tags.append(_tag(job.get("streamer")))
        if job.get("platform") == "Kick":
            tags += ["kick", "kickclips"]
    tags += {"funny": ["funny"], "rage": ["rage"], "hype": ["hype"], "tense": ["intense"],
             "emotional": ["emotional"], "chill": []}.get(clip.get("mood"), [])
    out = []
    for t in tags:
        if t and t != "shorts" and t not in out:
            out.append(t)
    return out[:8]


def _clip(start, end, zp, segments, kind, hype_ps=0.0, quote_ps=0.0):
    text = _text_between(segments, start, end)
    sub = _subscores(zp, text, end - start, hype_ps, quote_ps, kind)
    hook = _hook(text, kind)
    return {
        "start": round(start, 2), "end": round(end, 2), "title": hook[:80], "hook": hook[:60],
        "score": local_score(sub, kind), "breakdown": _shown(sub, kind), "reasons": _reasons(sub, kind),
        "mood": guess_mood(text, zp), "source": "local", "transcript": text, "signals": sub,
    }


def whole_clip(z, segments, duration, kind="irl"):
    """Scores an already-cut video as a single clip (for files that are already Short-length)."""
    zp = float(np.percentile(z, 95)) if len(z) else 0.0
    return _clip(0.0, round(duration, 2), zp, segments, kind)


def find_candidates(z, segments, settings, limit, kind="irl"):
    """Rank windows of the video by loudness + transcript cues; keep the best non-overlapping ones."""
    n = len(z)
    if n == 0:
        return []
    min_len, max_len = settings["min_len"], settings["max_len"]
    target = int(np.clip(TARGET_LEN.get(kind, 32), min_len, max_len))
    w = WEIGHTS.get(kind, WEIGHTS["irl"])
    words_at, hype_at, quote_at = np.zeros(n), np.zeros(n), np.zeros(n)
    for s in segments:
        i = min(n - 1, int(s["start"]))
        words_at[i] += len(s["text"].split())
        hype_at[i], quote_at[i] = np.add((hype_at[i], quote_at[i]), _counts(s["text"]))
    hype_ps, quote_ps = float(hype_at.sum()) / n, float(quote_at.sum()) / n
    scored = []
    for t in range(0, max(1, n - 10), 5):
        win = slice(t, min(n, t + target))
        zp = float(np.percentile(z[win], 95))
        words = float(words_at[win].sum())
        if words < 8 and kind != "gaming":   # outside gameplay, a wordless stretch rarely makes a good Short
            zp *= 0.5
        rank = (w["energy"] * 2.5 * zp + 0.35 * float(hype_at[win].sum())
                + w["quotable"] * 2.0 * float(quote_at[win].sum()) + 0.01 * words)
        scored.append((rank, t, zp))
    scored.sort(reverse=True)
    picked = []
    for _, t, zp in scored:
        if all(abs(t - p[0]) >= target + 20 for p in picked):
            picked.append((t, zp))
        if len(picked) >= limit:
            break
    out = []
    for t, zp in picked:
        if kind == "podcast":
            # Podcast windows already start where the talking starts; keep them as found.
            start, end = snap(segments, t, t + target, min_len, max_len, n)
        else:
            # Lead in to the spike: the setup usually sits before the loudest second.
            peak = t + int(np.argmax(z[t:min(n, t + target)]))
            start, end = snap(segments, max(0, peak - target * 0.6), peak + target * 0.4, min_len, max_len, n)
        out.append(_clip(start, end, zp, segments, kind, hype_ps, quote_ps))
    out.sort(key=lambda m: -m["score"])
    return out


load_learned()
