"""YouTube: connect the user's channel once, then upload Shorts with an optional scheduled publish time."""
import json
import threading
from datetime import datetime, timedelta, timezone

from . import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
CLIENT = config.DATA / "youtube_client.json"
TOKEN = config.DATA / "youtube_token.json"
CHANNEL = config.DATA / "youtube_channel.txt"
CATEGORY = {"gaming": "20", "film": "24", "irl": "22"}   # Gaming, Entertainment, People & Blogs
_state = {"connecting": False, "error": None}


def save_client(text: str) -> None:
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("That isn't valid JSON. Paste the whole contents of the downloaded client file.")
    if "installed" not in data:
        raise ValueError('This must be an OAuth client of type "Desktop app" (the file starts with {"installed": …}).')
    CLIENT.write_text(json.dumps(data), "utf-8")


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), "utf-8")
    return creds if creds.valid else None


def _service():
    from googleapiclient.discovery import build
    creds = _creds()
    if not creds:
        raise RuntimeError("YouTube isn't connected. Connect your channel in Settings.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def status() -> dict:
    return {"has_client": CLIENT.exists(), "connected": TOKEN.exists(),
            "channel": CHANNEL.read_text("utf-8") if CHANNEL.exists() else "",
            "connecting": _state["connecting"], "error": _state["error"]}


def connect() -> None:
    """Opens the Google sign-in page in the browser and waits for the user to approve."""
    if not CLIENT.exists():
        raise RuntimeError("Add your Google OAuth client first.")
    if _state["connecting"]:
        return

    def run():
        from google_auth_oauthlib.flow import InstalledAppFlow
        _state.update(connecting=True, error=None)
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300,
                                          authorization_prompt_message="")
            TOKEN.write_text(creds.to_json(), "utf-8")
            me = _service().channels().list(part="snippet", mine=True).execute()
            items = me.get("items") or []
            CHANNEL.write_text(items[0]["snippet"]["title"] if items else "(no channel on this account)", "utf-8")
        except Exception as e:
            _state["error"] = f"Sign-in didn't finish: {e}"[:300]
        finally:
            _state["connecting"] = False

    threading.Thread(target=run, daemon=True).start()


def disconnect() -> None:
    TOKEN.unlink(missing_ok=True)
    CHANNEL.unlink(missing_ok=True)


def to_utc(local_iso: str) -> str:
    """'2026-11-13T18:00' in this PC's timezone -> the UTC timestamp YouTube expects."""
    dt = datetime.fromisoformat(local_iso)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_slot(times: str, taken: set) -> str:
    """Earliest upcoming daily slot (local 'HH:MM, HH:MM' list) that no other upload is using."""
    slots = []
    for t in times.split(","):
        try:
            hh, mm = t.strip().split(":")
            slots.append((int(hh), int(mm)))
        except ValueError:
            continue
    slots = sorted(set(slots)) or [(18, 0)]
    now = datetime.now().astimezone()
    for day in range(0, 120):
        for hh, mm in slots:
            cand = (now + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            utc = cand.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if cand > now + timedelta(minutes=20) and utc not in taken:
                return utc
    raise RuntimeError("No free schedule slot in the next 120 days")


def upload(path, title, description, tags, kind, privacy, publish_at=None) -> str:
    from googleapiclient.http import MediaFileUpload
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        # YouTube publishes a scheduled video itself; it must be private until then.
        status.update(privacyStatus="private", publishAt=publish_at)
    body = {"snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:30],
                        "categoryId": CATEGORY.get(kind, "22")},
            "status": status}
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = _service().videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk(num_retries=4)
    return response["id"]


def stats(video_ids: list) -> dict:
    """Public numbers for uploaded videos: {id: {views, likes, comments, published, privacy, title}}."""
    out = {}
    svc = _service()
    for i in range(0, len(video_ids), 50):
        resp = svc.videos().list(part="statistics,snippet,status", id=",".join(video_ids[i:i + 50])).execute()
        for it in resp.get("items", []):
            st = it.get("statistics", {})
            out[it["id"]] = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                             "comments": int(st.get("commentCount", 0)),
                             "published": it["snippet"].get("publishedAt"),
                             "privacy": it.get("status", {}).get("privacyStatus"),
                             "title": it["snippet"].get("title")}
    return out
