"""Templates: a saved look (framing, captions, effects, sounds, song, timeline options) that every new clip is
made with, so videos come out the same way instead of following the built-in defaults."""
import json
import threading
import time
import uuid

from . import config

FILE = config.DATA / "templates.json"
# Everything a template holds. "song" may be a track id, "" (no song), or "auto:mood" / "auto:rotate" to pick
# from your music per clip. "sfx_fixed" maps an effect role (intro, hit, big, transition, ending, emoji) to a
# sound you always want there; roles left out are chosen to fit the moment.
KEYS = ("layout", "zoom", "cam_pos", "split_game", "caption_style", "highlight", "progress_bar", "song",
        "music_volume", "effects_preset", "filter", "sfx_volume", "sfx_source", "sfx_fixed", "tighten",
        "cold_open", "track_face", "beat_sync")
_lock = threading.Lock()


def _read() -> list:
    try:
        data = json.loads(FILE.read_text("utf-8")) if FILE.exists() else []
        return [t for t in data if isinstance(t, dict) and t.get("id")]
    except Exception:
        return []


def _write(items):
    FILE.write_text(json.dumps(items, indent=1, ensure_ascii=False), "utf-8")


def all_templates() -> list:
    return _read()


def get(tid: str):
    return next((t for t in _read() if t["id"] == tid), None) if tid else None


def clean_style(style: dict) -> dict:
    out = {k: style[k] for k in KEYS if k in style and style[k] is not None}
    if not isinstance(out.get("sfx_fixed"), dict):
        out.pop("sfx_fixed", None)
    else:
        out["sfx_fixed"] = {k: str(v) for k, v in out["sfx_fixed"].items() if v}
    return out


def save(name: str, style: dict, tid: str = None) -> dict:
    name = (name or "").strip()[:60] or "My template"
    with _lock:
        items = _read()
        old = next((t for t in items if t["id"] == tid or (not tid and t["name"].lower() == name.lower())), None)
        if old:   # same name (or id): update it rather than making a duplicate
            old.update(name=name, style=clean_style(style), updated=time.time())
            t = old
        else:
            t = {"id": uuid.uuid4().hex[:8], "name": name, "style": clean_style(style), "updated": time.time()}
            items.append(t)
        _write(items)
    return t


def delete(tid: str) -> None:
    with _lock:
        _write([t for t in _read() if t["id"] != tid])
    if config.load_settings().get("default_template") == tid:
        config.save_settings({"default_template": ""})


def base_style(settings: dict) -> dict:
    """The look from Settings, used when no template is chosen (and under a template, for anything it leaves out)."""
    return {"layout": settings.get("default_layout") or "bars", "zoom": settings.get("default_zoom") or 73,
            "cam_pos": "auto", "split_game": "fill",
            "caption_style": settings["caption_style"], "highlight": settings["highlight"],
            "progress_bar": settings["progress_bar"], "song": settings.get("default_song", ""),
            "music_volume": settings["music_volume"], "effects_preset": settings.get("effects_preset") or "edit",
            "filter": None, "sfx_volume": settings.get("sfx_volume", 0.7),
            "sfx_source": settings.get("sfx_source", "auto"), "sfx_fixed": {},
            "tighten": settings.get("tighten", True), "cold_open": settings.get("cold_open", False),
            "track_face": True, "beat_sync": True}


def style_for(settings: dict, template_id=None) -> dict:
    """The look for a new clip: Settings' defaults, with the chosen (or default) template on top."""
    style = base_style(settings)
    t = get(template_id if template_id is not None else settings.get("default_template"))
    if t:
        style.update(clean_style(t.get("style") or {}))
    return style
