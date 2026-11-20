"""Songs: the user's own folder, a downloadable free-to-use library, and a 'trending now' chart for tips."""
import json
import re
import time

import httpx

from . import config

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"}
UA = {"User-Agent": "shorts-clipper/1.0"}
MOOD_QUERIES = {
    "hype": "energetic", "funny": "funny", "tense": "suspense",
    "chill": "lofi", "emotional": "emotional piano", "rage": "aggressive",
}
# Licences that allow use in monetised, edited videos.
OK_LICENSES = {"cc0", "pdm", "by"}   # share-alike (BY-SA) is left out: it could force the whole video under the same licence
_trending_cache = {"at": 0, "items": []}


def library() -> list:
    items = []
    for f in sorted(config.MUSIC_MINE.iterdir()):
        if f.suffix.lower() in AUDIO_EXT:
            items.append({"id": f"mine/{f.name}", "title": f.stem, "artist": "", "source": "mine",
                          "license": "your own file", "attribution": ""})
    for f in sorted(config.MUSIC_LIB.iterdir()):
        if f.suffix.lower() in AUDIO_EXT:
            meta = {}
            side = f.with_suffix(".json")
            if side.exists():
                meta = json.loads(side.read_text("utf-8"))
            items.append({"id": f"library/{f.name}", "title": meta.get("title", f.stem),
                          "artist": meta.get("artist", ""), "source": "library", "mood": meta.get("mood", ""),
                          "license": meta.get("license", "unknown"), "license_url": meta.get("license_url", ""),
                          "attribution": meta.get("attribution", "")})
    return items


def resolve(track_id: str):
    """Track id -> (path, library entry), refusing anything outside the music folders."""
    if not track_id:
        return None, None
    kind, _, name = track_id.partition("/")
    base = {"mine": config.MUSIC_MINE, "library": config.MUSIC_LIB}.get(kind)
    if not base or "/" in name or "\\" in name or ".." in name:
        return None, None
    path = base / name
    if not path.is_file():
        return None, None
    return path, next((t for t in library() if t["id"] == track_id), None)


AUTO_SONG = {"auto:mood": "Auto: a song matching each clip's mood", "auto:rotate": "Auto: rotate through my songs"}


def pick(mode: str, mood: str = "", index: int = 0) -> str:
    """A concrete track id for a clip. `mode` is a track id (kept if it still exists), "" (no song), or
    "auto:mood" / "auto:rotate", which choose from your songs and the downloaded library."""
    if not mode:
        return ""
    tracks = library()
    if not mode.startswith("auto:"):
        return mode if any(t["id"] == mode for t in tracks) else ""
    if not tracks:
        return ""
    if mode == "auto:mood":
        matching = [t for t in tracks if mood and t.get("mood") == mood]
        if matching:
            tracks = matching
    return tracks[index % len(tracks)]["id"]


def _openverse(query: str) -> list:
    r = httpx.get("https://api.openverse.org/v1/audio/", headers=UA, timeout=20, params={
        "q": query, "category": "music", "license": "by,cc0,pdm", "page_size": 20})
    r.raise_for_status()
    out = []
    for t in r.json().get("results", []):
        lic = (t.get("license") or "").lower()
        secs = (t.get("duration") or 0) / 1000
        if lic not in OK_LICENSES or not t.get("url") or secs < 20:
            continue
        out.append({
            "provider": "openverse", "key": t["id"], "title": t.get("title") or "Untitled",
            "artist": t.get("creator") or "", "duration": int(secs), "url": t["url"],
            "license": f"CC {lic.upper()} {t.get('license_version') or ''}".strip(),
            "license_url": t.get("license_url") or "", "page": t.get("foreign_landing_url") or "",
            "attribution": t.get("attribution") or "",
        })
    return out


def _jamendo(query: str, client_id: str) -> list:
    """Jamendo can rank by this week's popularity, which is the closest thing to 'trending' for free music."""
    r = httpx.get("https://api.jamendo.com/v3.0/tracks/", headers=UA, timeout=20, params={
        "client_id": client_id, "format": "json", "limit": 30, "order": "popularity_week",
        "fuzzytags": query.replace(" ", "+"), "include": "licenses", "audioformat": "mp32"})
    r.raise_for_status()
    out = []
    for t in r.json().get("results", []):
        cc = t.get("license_ccurl") or ""
        m = re.search(r"/licenses/([a-z-]+)/", cc)
        lic = m.group(1) if m else ""
        if lic not in OK_LICENSES or not t.get("audiodownload_allowed", True) or not t.get("audio"):
            continue
        out.append({
            "provider": "jamendo", "key": str(t["id"]), "title": t.get("name") or "Untitled",
            "artist": t.get("artist_name") or "", "duration": int(t.get("duration") or 0),
            "url": t.get("audiodownload") or t["audio"], "license": f"CC {lic.upper()}",
            "license_url": cc, "page": t.get("shareurl") or "",
            "attribution": f"\"{t.get('name')}\" by {t.get('artist_name')} (Jamendo), licensed {cc}",
        })
    return out


def search(mood: str, query: str, settings: dict) -> dict:
    q = (query or "").strip() or MOOD_QUERIES.get(mood, "energetic")
    results, errors = [], []
    cid = settings.get("jamendo_client_id")
    if cid:
        try:
            results += _jamendo(q, cid)
        except Exception as e:
            errors.append(f"Jamendo: {e}")
    try:
        results += _openverse(q)
    except Exception as e:
        errors.append(f"Openverse: {e}")
    return {"query": q, "results": results, "errors": errors}


def download(track: dict, mood: str = "") -> dict:
    url = track["url"]
    if not url.startswith("https://"):
        raise ValueError("Refusing a non-https download link")
    safe = re.sub(r"[^\w\- ]", "", f"{track.get('artist', '')} - {track.get('title', '')}").strip()[:80] or "track"
    ext = ".mp3"
    m = re.search(r"\.(mp3|ogg|wav|m4a|flac)(?:$|\?)", url, re.I)
    if m:
        ext = "." + m.group(1).lower()
    dest = config.MUSIC_LIB / f"{safe}{ext}"
    with httpx.stream("GET", url, headers=UA, timeout=60, follow_redirects=True) as r:
        r.raise_for_status()
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(65536):
                size += len(chunk)
                if size > 60 * 1024 * 1024:
                    raise ValueError("Track is larger than 60 MB")
                f.write(chunk)
    dest.with_suffix(".json").write_text(json.dumps({
        "title": track.get("title"), "artist": track.get("artist"), "license": track.get("license"),
        "license_url": track.get("license_url"), "attribution": track.get("attribution"),
        "page": track.get("page"), "provider": track.get("provider"), "mood": mood,
    }, indent=2), "utf-8")
    return {"id": f"library/{dest.name}"}


def trending() -> list:
    """Current most-played chart. These are commercial songs: shown as suggestions to add inside the
    YouTube app at upload (where they're licensed), never downloaded."""
    if time.time() - _trending_cache["at"] < 6 * 3600 and _trending_cache["items"]:
        return _trending_cache["items"]
    try:
        r = httpx.get("https://rss.marketingtools.apple.com/api/v2/us/music/most-played/25/songs.json",
                      headers=UA, timeout=20, follow_redirects=True)
        r.raise_for_status()
        items = [{"title": x.get("name"), "artist": x.get("artistName")}
                 for x in r.json().get("feed", {}).get("results", [])]
    except Exception:
        # The newer feed is sometimes down; the older iTunes chart is a dependable stand-in.
        r = httpx.get("https://itunes.apple.com/us/rss/topsongs/limit=25/json", headers=UA, timeout=20,
                      follow_redirects=True)
        r.raise_for_status()
        items = [{"title": x["im:name"]["label"], "artist": x["im:artist"]["label"]}
                 for x in r.json().get("feed", {}).get("entry", [])]
    _trending_cache.update(at=time.time(), items=items)
    return items
