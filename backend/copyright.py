"""Copyright risk report for a clip. An estimate to guide the user, not a legal clearance."""
import httpx

from . import audio, config

LEVELS = ["low", "medium", "high"]


def identify_song(wav_path, api_token: str):
    """Optional AudD lookup: names the commercial track playing in the clip, if any."""
    mp3 = wav_path.with_suffix(".id.mp3")
    config.run_ffmpeg(["-i", wav_path, "-t", "20", "-b:a", "96k", mp3])
    try:
        with open(mp3, "rb") as f:
            r = httpx.post("https://api.audd.io/", data={"api_token": api_token}, files={"file": f}, timeout=40)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(str(body.get("error", {}).get("error_message") or body)[:200])
        res = body.get("result")
        return {"artist": res.get("artist"), "title": res.get("title")} if res else None
    finally:
        mp3.unlink(missing_ok=True)


def assess(wav_path, song_entry, is_local_file: bool, settings: dict, kind: str = "irl", topic: str = "") -> dict:
    items = []
    if kind == "film":
        items.append({"level": "high", "label": "Film or TV footage is likely on screen",
                      "detail": f"Studios match {topic or 'film'} footage and audio automatically, even in short "
                                f"clips, and can block the Short or issue a strike. Clips where the streamer's "
                                f"own face and commentary carry the moment are far safer than the footage itself."})
    elif kind == "gaming":
        items.append({"level": "low", "label": "Gameplay footage",
                      "detail": "Publishers generally allow gameplay clips, but in-game radio and licensed "
                                "soundtrack songs can still be claimed."})

    # 1. Music already playing in the video's own audio (the usual cause of Content ID claims).
    m = audio.music_likelihood(wav_path)
    matched, id_error = None, None
    if settings.get("audd_api_key"):
        try:
            matched = identify_song(wav_path, settings["audd_api_key"])
        except Exception as e:
            id_error = str(e)
    if matched:
        items.append({"level": "high", "label": "Copyrighted song identified in the video's own audio",
                      "detail": f"\"{matched['title']}\" by {matched['artist']}. Expect a Content ID claim; "
                                f"mute or replace that section."})
    elif m["score"] >= 0.6:
        items.append({"level": "high", "label": "Music is very likely playing in the video's own audio",
                      "detail": "Steady beat and sustained tones detected. If it's a commercial song, YouTube "
                                "can claim or block the Short."})
    elif m["score"] >= 0.3:
        items.append({"level": "medium", "label": "Possible background music in the video's own audio",
                      "detail": "Some musical signal detected (could also be game music or sound effects). "
                                "Listen before uploading."})
    else:
        items.append({"level": "low", "label": "No clear music detected in the video's own audio",
                      "detail": "Mostly speech and game sound."})
    if id_error:
        items.append({"level": "low", "label": "Song lookup failed", "detail": id_error})

    # 2. The song the user added on top.
    if song_entry:
        if song_entry["source"] == "library":
            lic = song_entry.get("license", "")
            needs_credit = "CC0" not in lic and "PDM" not in lic
            items.append({"level": "low", "label": f"Added song is free to use ({lic})",
                          "detail": ("Paste this credit in the description: " + song_entry.get("attribution", ""))
                          if needs_credit else "No credit required.",
                          "attribution": song_entry.get("attribution", "") if needs_credit else ""})
        else:
            items.append({"level": "medium", "label": "Added song comes from your own folder",
                          "detail": "The app can't verify its licence. Safe only if you have the rights to it."})

    # 3. Whose footage it is.
    if is_local_file:
        items.append({"level": "medium", "label": "Ownership of this file is unknown",
                      "detail": "Fine if you recorded it yourself. If it was downloaded from someone else's channel "
                                "or stream, re-uploading without their permission can get the Short removed or a "
                                "strike."})
    else:
        items.append({"level": "medium", "label": "Creator's permission not verified",
                      "detail": "The footage belongs to the streamer or channel. Re-uploading without their "
                                "permission can get the Short removed or a strike, even with no music in it."})

    overall = max((i["level"] for i in items), key=LEVELS.index)
    return {"overall": overall, "items": items, "music_score": m["score"],
            "note": "Risk estimate only. The app can't see YouTube's Content ID database or detect copyrighted "
                    "video shown on screen (TV, films, other creators' content)."}
