"""Optional Claude pass: reads the whole transcript and picks, titles, and scores the moments."""
import json

import anthropic

from . import config, moments

# USD per million tokens (input, output), for the cost shown after a run.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50), "claude-fable-5-2": (8.0, 40.0),
}

SYSTEM = """You pick the moments from a livestream that will perform best as YouTube Shorts.

You get the stream's transcript. Each line starts with its time in seconds from the start of the stream. \
A line marked LOUD is where the audio jumped well above the stream's normal level (shouting, laughing, a big \
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
stream clips: 50 is an ordinary clip, 70 is clearly good, 85 and above is rare and needs an obvious hook and \
payoff. A stream with nothing special in it should get low scores across the board. The four sub-scores use \
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


def _clamp(v):
    return int(max(0, min(100, v)))


def pick_moments(segments, z, meta, settings, limit, local_candidates):
    """Returns (moments, info). Raises on API failure so the caller can fall back to the local picks."""
    client = anthropic.Anthropic(api_key=config.api_key(settings))
    model = settings.get("claude_model") or "claude-opus-5-5"
    hints = ", ".join(f"{int(c['start'])}-{int(c['end'])}" for c in local_candidates[:20])
    user = (
        f"Stream: \"{meta.get('title')}\" by {meta.get('streamer')}. Length: {int(len(z))} seconds.\n"
        f"{KIND_NOTES.get(meta.get('kind'), KIND_NOTES['irl'])}"
        f"{' Topic: ' + meta['topic'] + '.' if meta.get('topic') else ''}\n\n"
        f"<transcript>\n{_transcript_lines(segments, z)}\n</transcript>\n\n"
        f"A loudness-based detector flagged these second ranges as candidates; use them as leads, not as the "
        f"answer: {hints or 'none'}.\n\n"
        f"Return the {limit} best moments (fewer if the stream doesn't have that many worth clipping), each "
        f"between {settings['min_len']} and {settings['max_len']} seconds long."
    )
    kwargs = dict(
        model=model, max_tokens=16000, system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    try:
        # Lets the API hand the request to another model if a safety classifier declines it.
        with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                         **kwargs) as stream:
            resp = stream.get_final_message()
    except (TypeError, anthropic.BadRequestError):
        with client.messages.stream(**kwargs) as stream:
            resp = stream.get_final_message()

    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyse this transcript")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off")
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))

    out = []
    for m in data.get("moments", []):
        start, end = float(m["start_sec"]), float(m["end_sec"])
        if end <= start or start >= len(z):
            continue
        start, end = moments.snap(segments, start, end, settings["min_len"], settings["max_len"])
        if any(start < o["end"] and end > o["start"] for o in out):
            continue
        out.append({
            "start": round(start, 2), "end": round(end, 2),
            "title": m["title"].strip()[:100], "hook": m["hook"].strip()[:60],
            "score": _clamp(m["virality"]),
            "breakdown": {"hook": _clamp(m["hook_strength"]), "emotion": _clamp(m["emotion"]),
                          "stands alone": _clamp(m["self_contained"]), "shareable": _clamp(m["shareability"])},
            "reasons": [r.strip() for r in m["reasons"]][:4], "mood": m["mood"], "source": "claude",
            "hashtags": [t for t in (moments._tag(h) for h in m["hashtags"]) if t and t != "shorts"][:8],
            "transcript": moments._text_between(segments, start, end),
        })
    out.sort(key=lambda m: -m["score"])
    pin, pout = PRICES.get(model, (0, 0))
    cost = (resp.usage.input_tokens * pin + resp.usage.output_tokens * pout) / 1e6
    return out, {"model": resp.model, "input_tokens": resp.usage.input_tokens,
                 "output_tokens": resp.usage.output_tokens, "cost_usd": round(cost, 3)}


def friendly_error(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "The Anthropic API key was rejected. Check it in Settings."
    if isinstance(e, anthropic.RateLimitError):
        return "Claude's rate limit was hit."
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude API error {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Could not reach the Claude API (check your internet connection)."
    return str(e)
