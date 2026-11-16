"""Optional AI pass: a connected model reads the whole transcript and picks, titles, and scores the moments."""
from . import moments, providers

SYSTEM = """You pick the moments from a long video (a livestream, podcast or similar) that will perform best as \
YouTube Shorts.

You get the video's transcript. Each line starts with its time in seconds from the start of the video. \
A line marked LOUD is where the audio jumped well above the video's normal level (shouting, laughing, a big \
in-game event). The transcript comes from speech recognition, so expect misheard words and missing punctuation, \
and remember you cannot see the video: prefer moments where the words and the loudness together make it clear \
something happened.

What makes a clip work as a Short:
- The first two seconds give a viewer who knows nothing about the stream a reason to stay.
- It stands alone: setup and payoff are both inside the clip, with no missing context.
- It carries a real reaction: surprise, laughter, rage, a clutch play, a confession, an argument, a fail.
- It ends right after the payoff instead of trailing off.

For each moment, choose start and end times that include the setup and stop just after the payoff, within the \
length limits given. Moments must not overlap.

Score honestly. `virality` is your estimate from 0 to 100 of how well the clip would do relative to typical \
clips of this kind: 50 is an ordinary clip, 70 is clearly good, 85 and above is rare and needs an obvious hook \
and payoff. A video with nothing special in it should get low scores across the board. The four sub-scores use \
the same scale. In `reasons`, give two or three short, specific reasons a viewer would watch or share it, and \
name any weakness that held the score down.

`title` is the upload title: under 70 characters, specific to what happens, no hashtags. `hook` is the on-screen \
text for the first three seconds: at most seven words that create curiosity without giving away the payoff. \
`hashtags` is five to eight lowercase tags without the # sign, most specific first: the game, film or topic, \
the streamer, then what happens in the clip. Leave out "shorts"; it is added automatically. \
Do not invent events the transcript does not support."""

KIND_NOTES = {
    "irl": "This is an IRL / just-chatting stream: the moments are in what people say and do.",
    "gaming": "This is a gameplay stream. Big in-game events can have little speech, so weigh LOUD marks heavily "
              "and treat short excited callouts as signs of a play worth clipping.",
    "film": "The streamer is reacting to or talking about a film or show. Pick the streamer's own reactions, "
            "jokes and opinions. Avoid stretches where the film's own audio plays with no commentary: those add "
            "nothing original and are what studios' automated matching claims.",
    "podcast": "This is a podcast or interview, so loudness matters little: the moments are in what is said. "
               "Look for a bold or controversial opinion, a surprising fact or number, a personal story with a "
               "clear payoff, a confession, a heated disagreement, a big laugh, or one sharp piece of advice. The "
               "clip must open on the line that makes a stranger stop scrolling (often a question or the start "
               "of the claim), not on small talk. Weak podcast clips are vague, need earlier context, or trail "
               "off without landing the point.",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sec": {"type": "number"},
                    "end_sec": {"type": "number"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "virality": {"type": "integer"},
                    "hook_strength": {"type": "integer"},
                    "emotion": {"type": "integer"},
                    "self_contained": {"type": "integer"},
                    "shareability": {"type": "integer"},
                    "mood": {"type": "string", "enum": ["hype", "funny", "tense", "chill", "emotional", "rage"]},
                    "reasons": {"type": "array", "items": {"type": "string"}},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start_sec", "end_sec", "title", "hook", "virality", "hook_strength", "emotion",
                             "self_contained", "shareability", "mood", "reasons", "hashtags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}


def _transcript_lines(segments, z):
    lines = []
    for s in segments:
        a, b = int(s["start"]), min(len(z), int(s["end"]) + 1)
        loud = len(z) and a < b and float(z[a:b].max()) >= 2.5
        lines.append(f"[{int(s['start'])}{' LOUD' if loud else ''}] {s['text']}")
    return "\n".join(lines)


def _num(v, default=50):
    try:
        return int(max(0, min(100, float(v))))
    except (TypeError, ValueError):
        return default


def _text(v, limit):
    return str(v or "").strip()[:limit]


def pick_moments(segments, z, meta, settings, limit, local_candidates, whole=False):
    """Returns (moments, info). Raises on failure so the caller can fall back to the built-in picks.
    whole=True scores an already-cut clip as one moment instead of searching inside it."""
    length = int(meta.get("duration") or len(z))
    by = f" by {meta['streamer']}" if meta.get("streamer") else ""
    head = (f"Video: \"{meta.get('title')}\"{by}. Length: {length} seconds.\n"
            f"{KIND_NOTES.get(meta.get('kind'), KIND_NOTES['irl'])}"
            f"{' Topic: ' + meta['topic'] + '.' if meta.get('topic') else ''}\n\n"
            f"<transcript>\n{_transcript_lines(segments, z)}\n</transcript>\n\n")
    if whole:
        ask = (f"This video is already cut to Short length. Return exactly one moment covering the whole video "
               f"(start 0, end {length}) and score, title and tag it as it stands.")
    else:
        hints = ", ".join(f"{int(c['start'])}-{int(c['end'])}" for c in local_candidates[:20])
        ask = (f"A simple detector flagged these second ranges as candidates; use them as leads, not as the "
               f"answer: {hints or 'none'}.\n\n"
               f"Return the {limit} best moments (fewer if the video doesn't have that many worth clipping), each "
               f"between {settings['min_len']} and {settings['max_len']} seconds long.")
    data, info = providers.ask_json(settings, SYSTEM, head + ask, SCHEMA)

    # Smaller models sometimes drop or mistype fields, so every field is read defensively.
    out = []
    for m in (data.get("moments") if isinstance(data, dict) else None) or []:
        if not isinstance(m, dict):
            continue
        try:
            start, end = float(m.get("start_sec")), float(m.get("end_sec"))
        except (TypeError, ValueError):
            continue
        if end <= start or start >= length:
            continue
        if whole:
            start, end = 0.0, float(length)
        else:
            start, end = moments.snap(segments, start, end, settings["min_len"], settings["max_len"], length)
        if any(start < o["end"] and end > o["start"] for o in out):
            continue
        text = moments._text_between(segments, start, end)
        title = _text(m.get("title"), 100) or moments._hook(text, meta.get("kind"))
        tags = m.get("hashtags") if isinstance(m.get("hashtags"), list) else []
        reasons = m.get("reasons") if isinstance(m.get("reasons"), list) else []
        mood = m.get("mood") if m.get("mood") in ("hype", "funny", "tense", "chill", "emotional", "rage") else "hype"
        out.append({
            "start": round(start, 2), "end": round(end, 2),
            "title": title, "hook": _text(m.get("hook"), 60) or title[:60],
            "score": _num(m.get("virality")),
            "breakdown": {"hook": _num(m.get("hook_strength")), "emotion": _num(m.get("emotion")),
                          "stands alone": _num(m.get("self_contained")), "shareable": _num(m.get("shareability"))},
            "reasons": [_text(r, 200) for r in reasons if r][:4], "mood": mood,
            "source": "ai", "scored_by": info["provider"],
            "hashtags": [t for t in (moments._tag(h) for h in tags) if t and t != "shorts"][:8],
            "transcript": text,
        })
        if whole:
            break
    out.sort(key=lambda m: -m["score"])
    return out, info
