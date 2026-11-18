"""Auto-post queue for GTA 6 Live Clipper.

Finished clips go into queue.json (in the output dir) with a title taken
from the clip's loudest moment. A background worker uploads them to
YouTube with spacing (default 30 min between posts) and a daily cap
(default 10/day) — so the channel is fed steadily through launch week
without flooding it.

YouTube (v1, implemented):
- Upload via the YouTube Data API v3 (google-api-python-client).
- One-time setup (his step, needs his Google account — see README):
  1. Google Cloud Console -> create a project -> enable "YouTube Data API v3"
     -> create OAuth client ID (Desktop app) -> download client_secrets.json
     into this folder.
  2. Run `python post_queue.py --auth` once: a browser opens, he signs in
     with the YouTube channel's Google account, consent is stored in
     token.json (never committed, never shared).
  3. From then on the worker uploads automatically. Until the setup is
     done, clips wait in the queue as "awaiting_setup" — never lost, never
     failed.
- The worker NEVER starts an interactive auth flow by itself (it runs
  unattended 24/7); it only uploads when token.json already exists.

Instagram / TikTok (v2, NOT implemented — and not faked):
- Both need app approvals + review before automated posting works
  (Meta's Instagram Graph API needs an approved app + a Business/Creator
  account; TikTok's Content Posting API needs an approved app too).
  post_to_instagram() / post_to_tiktok() raise NotImplementedError with
  that explanation. The queue marks entries per-platform, so v2 can add
  them without changing the queue format.

NOTE: YouTube OAuth was implemented from the API spec and CANNOT be
tested end-to-end on this VM (no Google account / OAuth client here).
Status: implemented, needs his OAuth client on first run.
"""
import datetime
import json
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILENAME = "queue.json"
CLIENT_SECRETS = os.path.join(BASE, "client_secrets.json")
TOKEN_FILE = os.path.join(BASE, "token.json")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
DEFAULT_TAGS = ["GTA 6", "GTA VI", "GTA 6 live", "GTA 6 clips",
                "Grand Theft Auto 6", "GTA 6 gameplay"]
_q_lock = threading.Lock()


def _queue_path(out_dir):
    return os.path.join(out_dir, QUEUE_FILENAME)


def load_queue(out_dir):
    p = _queue_path(out_dir)
    if not os.path.exists(p):
        return []
    try:
        return json.load(open(p))
    except Exception:
        return []


def save_queue(out_dir, entries):
    with _q_lock:
        tmp = _queue_path(out_dir) + ".tmp"
        json.dump(entries, open(tmp, "w"), indent=1)
        os.replace(tmp, _queue_path(out_dir))


def make_title(info, channel, game_name=None):
    """Title from the clip's loudest moment: first spoken line after the
    moment start, capped at 90 chars (YouTube limit is 100). The title
    names the game when the clip is about it (ClipKontrast GAME_RULES);
    without a detected game it stays "GTA 6 LIVE:"."""
    hook = ""
    try:
        tr = json.load(open(info["transcript"]))
        start = info["moment"]["start"]
        for seg in tr["segments"]:
            if seg["end"] > start:
                hook = seg["text"].strip().strip(".,!?\"' ")
                break
    except Exception:
        pass
    prefix = f"{game_name} LIVE: " if game_name else "GTA 6 LIVE: "
    if hook:
        hook = hook[:60].strip()
        title = prefix + hook
    else:
        title = prefix + "wild moment from the stream"
    return title[:95]


def enqueue_clip(out_dir, clip_path, channel, info, game_name=None,
                 game_tags=None):
    entries = load_queue(out_dir)
    title = make_title(info, channel, game_name)
    # tags: the detected game's tags first, then the GTA defaults
    # (deduped, YouTube format = plain words, no '#'; total under the
    # 500-char tag limit).
    tags = []
    for t in list(game_tags or []) + DEFAULT_TAGS:
        if t and t not in tags:
            tags.append(t)
    total = 0
    kept = []
    for t in tags:
        if total + len(t) + 1 > 480:
            break
        kept.append(t)
        total += len(t) + 1
    entry = {
        "id": os.path.splitext(os.path.basename(clip_path))[0],
        "clip": os.path.abspath(clip_path),
        "channel": channel,
        "title": title,
        "description": (f"{title}\n\nClipped LIVE from {channel} during GTA 6 "
                        f"launch week — before the streamer's own upload.\n"
                        f"Moment: {info['moment']['start']:.0f}s into the segment."),
        "tags": kept,
        "status": "queued",  # queued | posting | posted | failed | awaiting_setup
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "posted_at": None,
        "youtube_id": None,
        "attempts": 0,
        "error": None,
    }
    entries.append(entry)
    save_queue(out_dir, entries)
    print(f"[post] queued: {entry['title']} ({entry['id']})", flush=True)
    return entry


def youtube_status():
    """Returns 'ready' | 'needs_auth' | 'missing_secrets'."""
    if not os.path.exists(CLIENT_SECRETS):
        return "missing_secrets"
    if not os.path.exists(TOKEN_FILE):
        return "needs_auth"
    return "ready"


def get_youtube_service():
    """Build an authenticated YouTube service from token.json (refreshes
    the token when expired). Raises if not set up."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError("YouTube not authorised yet — run: python post_queue.py --auth")
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, YOUTUBE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            json.dump(json.loads(creds.to_json()), open(TOKEN_FILE, "w"))
        else:
            raise RuntimeError("YouTube token invalid — run: python post_queue.py --auth")
    return build("youtube", "v3", credentials=creds)


def run_auth_flow():
    """One-time interactive OAuth (opens a browser). Run by the user, never
    by the worker."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not os.path.exists(CLIENT_SECRETS):
        print("Missing client_secrets.json in this folder.", file=sys.stderr)
        print("Create an OAuth client (Desktop app) in Google Cloud Console,",
              file=sys.stderr)
        print("enable YouTube Data API v3, download the JSON as client_secrets.json.",
              file=sys.stderr)
        raise SystemExit(1)
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, YOUTUBE_SCOPES)
    creds = flow.run_local_server(port=0)
    json.dump(json.loads(creds.to_json()), open(TOKEN_FILE, "w"))
    print(f"Authorised — token saved to {TOKEN_FILE} (keep it private, never commit it).")


def upload_to_youtube(service, entry):
    """Resumable upload of one queued clip. Returns the YouTube video id."""
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": entry["title"],
            "description": entry["description"],
            "tags": entry["tags"],
            "categoryId": "20",  # Gaming
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(entry["clip"], chunksize=-1, resumable=True)
    req = service.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _status, resp = req.next_chunk()
    return resp["id"]


def _posted_today(entries):
    today = datetime.date.today().isoformat()
    return sum(1 for e in entries
               if e["status"] == "posted" and (e.get("posted_at") or "").startswith(today))


def _last_posted_at(entries):
    times = [e["posted_at"] for e in entries
             if e["status"] == "posted" and e.get("posted_at")]
    return max(times) if times else None


def process_queue_once(out_dir, spacing_min=30, daily_cap=10):
    """Try to post ONE queued clip (oldest first). Returns a short report
    string of what happened (or why nothing happened)."""
    entries = load_queue(out_dir)
    pending = [e for e in entries if e["status"] in ("queued", "failed")]
    if not pending:
        return "queue empty"
    if youtube_status() != "ready":
        for e in pending:
            e["status"] = "awaiting_setup"
        save_queue(out_dir, entries)
        return (f"YouTube not connected ({youtube_status()}) — "
                f"{len(pending)} clip(s) waiting in queue (not lost)")
    if _posted_today(entries) >= daily_cap:
        return f"daily cap reached ({daily_cap}) — waiting for tomorrow"
    last = _last_posted_at(entries)
    if last:
        dt = (datetime.datetime.now() -
              datetime.datetime.fromisoformat(last)).total_seconds() / 60
        if dt < spacing_min:
            return (f"spacing: last post {dt:.0f} min ago, "
                    f"next in {spacing_min - dt:.0f} min")
    entry = pending[0]
    entry["status"] = "posting"
    entry["attempts"] += 1
    save_queue(out_dir, entries)
    try:
        service = get_youtube_service()
        vid = upload_to_youtube(service, entry)
        entry["status"] = "posted"
        entry["youtube_id"] = vid
        entry["posted_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        entry["error"] = None
        save_queue(out_dir, entries)
        print(f"[post] POSTED {entry['title']} -> https://youtu.be/{vid}", flush=True)
        return f"posted {entry['id']} -> https://youtu.be/{vid}"
    except Exception as e:
        entry["status"] = "failed"
        entry["error"] = str(e)[:300]
        save_queue(out_dir, entries)
        print(f"[post] FAILED {entry['id']}: {e}", flush=True)
        return f"failed {entry['id']}: {e}"


def queue_worker(out_dir, stop_event, spacing_min=30, daily_cap=10):
    """Background loop: try one post per minute while the watcher runs."""
    while not stop_event.is_set():
        try:
            process_queue_once(out_dir, spacing_min, daily_cap)
        except Exception as e:
            print(f"[post] worker error: {e}", flush=True)
        stop_event.wait(60)


def start_queue_worker(out_dir, stop_event, spacing_min=30, daily_cap=10):
    t = threading.Thread(target=queue_worker,
                         args=(out_dir, stop_event, spacing_min, daily_cap),
                         daemon=True)
    t.start()
    return t


# --- v2 platforms: documented, NOT faked ---------------------------------
def post_to_instagram(entry):
    """v2 — NOT implemented. Instagram automated posting needs a Meta app
    approved for the Instagram Graph API + a Business/Creator account;
    there is no key-only route. Raises until that is set up."""
    raise NotImplementedError(
        "Instagram posting is v2: it needs a Meta app approved for the "
        "Instagram Graph API (instagram_business_content_publish) and a "
        "Business/Creator account. No code path is faked for it.")


def post_to_tiktok(entry):
    """v2 — NOT implemented. TikTok automated posting needs an app approved
    for the Content Posting API (video.upload + video.publish scopes)."""
    raise NotImplementedError(
        "TikTok posting is v2: it needs a TikTok app approved for the "
        "Content Posting API. No code path is faked for it.")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Auto-post queue: YouTube upload worker.")
    ap.add_argument("--out", default="clips", help="output dir holding queue.json")
    ap.add_argument("--auth", action="store_true",
                    help="one-time YouTube OAuth (opens a browser)")
    ap.add_argument("--status", action="store_true", help="show YouTube + queue status")
    ap.add_argument("--process-once", action="store_true", help="try posting one queued clip now")
    ap.add_argument("--spacing-min", type=int, default=30)
    ap.add_argument("--daily-cap", type=int, default=10)
    a = ap.parse_args()
    if a.auth:
        run_auth_flow()
        return
    if a.status:
        print("YouTube:", youtube_status())
        for e in load_queue(a.out):
            print(f"  {e['id']}: {e['status']} — {e['title']}"
                  + (f" -> https://youtu.be/{e['youtube_id']}" if e.get("youtube_id") else "")
                  + (f" (err: {e['error']})" if e.get("error") else ""))
        return
    if a.process_once:
        print(process_queue_once(a.out, a.spacing_min, a.daily_cap))
        return
    ap.error("give --auth, --status or --process-once")


if __name__ == "__main__":
    main()
