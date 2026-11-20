"""The video setup: every choice about how a video's clips are made (framing and facecam, captions, effects,
sounds, song, timeline options, how many clips and how long, and how the moments are picked). It is asked for
each new video before any work starts, filled in with the choices made last time.

Videos made with a template from an older version still use it; templates can no longer be made."""
import json

from . import config

OLD_TEMPLATES = config.DATA / "templates.json"
# A clip's look. "song" may be a track id, "" (no song), or "auto:mood" / "auto:rotate" to pick from your music
# per clip. "sfx_fixed" maps an effect role (intro, hit, big, transition, ending, emoji) to a sound you always
# want there; roles left out are chosen to fit the moment. "cam_box" is a facecam area marked by hand.
KEYS = ("layout", "zoom", "cam_pos", "cam_box", "split_game", "caption_style", "caption_size", "caption_position",
        "show_hook", "highlight", "progress_bar", "song", "music_volume", "effects_preset", "filter", "sfx_volume",
        "sfx_source", "sfx_fixed", "tighten", "cold_open", "track_face", "beat_sync")
# How the moments are found (not part of a clip's look).
FIND_KEYS = ("clip_count", "min_len", "max_len")
# About the video itself.
VIDEO_KEYS = ("kind", "topic", "scorer")
KINDS = ("irl", "gaming", "film", "podcast")
SCORERS = ("builtin", "ai", "chat")


def clean_style(style: dict) -> dict:
    out = {k: style[k] for k in KEYS + FIND_KEYS if k in style and style[k] not in (None, "")}
    for k, lo, hi in (("clip_count", 1, 30), ("min_len", 5, 120), ("max_len", 10, 180)):
        if k in out:
            try:
                out[k] = int(max(lo, min(hi, float(out[k]))))
            except (TypeError, ValueError):
                out.pop(k)
    if "min_len" in out and "max_len" in out and out["min_len"] >= out["max_len"]:
        out.pop("min_len"), out.pop("max_len")
    box = out.get("cam_box")
    if not (isinstance(box, dict) and all(isinstance(box.get(k), (int, float)) for k in ("x", "y", "w", "h"))):
        out.pop("cam_box", None)
    else:
        out["cam_box"] = {k: round(min(1.0, max(0.0, float(box[k]))), 4) for k in ("x", "y", "w", "h")}
    if out.get("cam_pos") == "custom" and "cam_box" not in out:
        out["cam_pos"] = "auto"
    if not isinstance(out.get("sfx_fixed"), dict):
        out.pop("sfx_fixed", None)
    else:
        out["sfx_fixed"] = {k: str(v) for k, v in out["sfx_fixed"].items() if v}
    return out


def clean_setup(setup: dict) -> dict:
    """A video's setup as sent from the app, checked."""
    setup = setup or {}
    out = clean_style(setup)
    out["kind"] = setup.get("kind") if setup.get("kind") in KINDS else "irl"
    out["topic"] = str(setup.get("topic") or "").strip()[:80]
    if setup.get("scorer") in SCORERS:
        out["scorer"] = setup["scorer"]
    return out


def base_style(settings: dict) -> dict:
    """Starting values for everything a clip's look holds."""
    return {"layout": settings.get("default_layout") or "bars", "zoom": settings.get("default_zoom") or 73,
            "cam_pos": "auto", "cam_box": None, "split_game": "fill",
            "caption_style": settings["caption_style"], "caption_size": "medium", "caption_position": "auto",
            "show_hook": True, "highlight": settings["highlight"],
            "progress_bar": settings["progress_bar"], "song": settings.get("default_song", ""),
            "music_volume": settings["music_volume"], "effects_preset": settings.get("effects_preset") or "edit",
            "filter": None, "sfx_volume": settings.get("sfx_volume", 0.7),
            "sfx_source": settings.get("sfx_source", "auto"), "sfx_fixed": {},
            "tighten": settings.get("tighten", True), "cold_open": settings.get("cold_open", False),
            "track_face": True, "beat_sync": True}


def defaults(settings: dict) -> dict:
    """What the setup window starts with: the choices made for the last video, or the starting values."""
    out = base_style(settings) | {k: settings[k] for k in FIND_KEYS}
    out |= {"kind": "irl", "topic": "", "scorer": settings.get("scorer", "builtin")}
    last = settings.get("last_setup") or {}
    out |= {k: v for k, v in clean_setup(last).items() if k != "topic"} if last else {}
    return out


def _old_template(template_id):
    try:
        items = json.loads(OLD_TEMPLATES.read_text("utf-8")) if template_id and OLD_TEMPLATES.exists() else []
    except Exception:
        return None
    return next((t for t in items if isinstance(t, dict) and t.get("id") == template_id), None)


def _chosen(job) -> dict:
    """The video's setup, or (for a video made by an older version) its template's."""
    if job.get("setup"):
        return job["setup"]
    t = _old_template(job.get("template"))
    return clean_style(t.get("style") or {}) if t else {}


def job_style(settings: dict, job: dict) -> dict:
    """The look for a new clip of this video."""
    style = base_style(settings)
    style.update({k: v for k, v in _chosen(job).items() if k in KEYS})
    return style


def job_settings(settings: dict, job: dict) -> dict:
    """Settings for finding this video's moments: its clip count, lengths and way of picking them."""
    chosen = _chosen(job)
    out = dict(settings, **{k: v for k, v in chosen.items() if k in FIND_KEYS})
    if chosen.get("scorer") in SCORERS:
        out["scorer"] = chosen["scorer"]
    if out["min_len"] >= out["max_len"]:
        out["min_len"], out["max_len"] = settings["min_len"], settings["max_len"]
    return out
