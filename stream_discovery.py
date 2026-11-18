"""Stream discovery — search live streams + validate pasted links.

No API keys anywhere: everything goes through yt-dlp.
- Search: YouTube live search (yt-dlp ytsearch, no key). YouTube is the
  MAIN source (sohaib's order); Twitch + Kick work via paste-link resolve.
- Paste link: any yt-dlp-supported live URL (YouTube / Twitch / Kick /
  Facebook / etc.) — validated live before it joins the watch list.

Proven on this VM (2026-11-17): yt-dlp ytsearch returns results in ~5s
(the flat-playlist entries carry `live_status` — "is_live" for live
streams). Twitch resolve uses the same 1080p60-format check as
capture.is_live (Twitch live max is 1080p60).
"""
import json
import subprocess
import sys

from ytdlp_cmd import ytdlp_cmd

YDLP = ytdlp_cmd()


def detect_site(url):
    u = (url or "").lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "twitch.tv" in u:
        return "twitch"
    if "kick.com" in u:
        return "kick"
    return "other"


def search_youtube_live(query, limit=10, timeout=90):
    """Search YouTube for live streams. Returns a list of dicts:
    {title, channel, url, thumbnail, is_live, viewers, site}.
    Only LIVE streams are returned (live_status == 'is_live')."""
    if not query or not query.strip():
        return []
    q = query.strip()
    # ytsearch with "live" bias: append 'live' when the query has no live word
    if "live" not in q.lower():
        q = q + " live"
    cmd = YDLP + ["--dump-json", "--flat-playlist", "--no-download",
                  f"ytsearch{limit}:{q}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        live = e.get("live_status") == "is_live" or e.get("is_live") is True
        if not live:
            continue
        thumbs = e.get("thumbnails") or []
        thumb = thumbs[-1].get("url") if thumbs else None
        out.append({
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "url": e.get("webpage_url") or e.get("url") or "",
            "thumbnail": thumb,
            "is_live": True,
            "viewers": e.get("concurrent_view_count"),
            "site": "youtube",
        })
    return out


def search_youtube(query, limit=10, timeout=90):
    """Search YouTube for ANY videos (live + uploaded) — used by VOD mode.
    Returns {title, channel, url, thumbnail, is_live, duration, site}."""
    if not query or not query.strip():
        return []
    cmd = YDLP + ["--dump-json", "--flat-playlist", "--no-download",
                  f"ytsearch{limit}:{query.strip()}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        thumbs = e.get("thumbnails") or []
        thumb = thumbs[-1].get("url") if thumbs else None
        out.append({
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "url": e.get("webpage_url") or e.get("url") or "",
            "thumbnail": thumb,
            "is_live": e.get("live_status") == "is_live",
            "duration": e.get("duration"),
            "site": "youtube",
        })
    return out


def get_stream_title(url, timeout=30):
    """One yt-dlp --dump-json call for a channel/live URL. Returns
    (title, uploader) — the stream's real title, for game detection
    (games.py). Fails soft: ("", "") on any error, so the caller falls
    back to the channel slug / GTA defaults. Cached per channel per run
    by the caller — do NOT call it per segment."""
    url = (url or "").strip()
    if not url:
        return "", ""
    cmd = YDLP + ["--dump-json", "--no-download", "--no-playlist", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return "", ""
    if r.returncode != 0 or not r.stdout.strip():
        return "", ""
    try:
        e = json.loads(r.stdout.splitlines()[0])
    except Exception:
        return "", ""
    return (e.get("title") or "", e.get("uploader") or "")


def resolve_url(url, timeout=60):
    """Validate a pasted link. Returns a dict:
    {ok, url, site, title, channel, is_live, thumbnail, viewers, error}.
    is_live: True/False/None (None = could not determine — yt-dlp gave no
    live flag; Twitch uses the 1080p60-format check instead)."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "empty URL"}
    site = detect_site(url)
    # Twitch: the format check is the honest live test (see capture.is_live)
    if site == "twitch":
        try:
            from capture import is_live
        except Exception:
            is_live = None
        live = is_live(url, timeout=timeout) if is_live else None
        return {"ok": True, "url": url, "site": site,
                "title": url.split("/")[-1], "channel": url.split("/")[-1],
                "is_live": live, "thumbnail": None, "viewers": None,
                "error": None if live else "channel is not live right now"}
    cmd = YDLP + ["--dump-json", "--no-download", "--no-playlist", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "url": url, "site": site, "error": "timed out — try again"}
    if r.returncode != 0 or not r.stdout.strip():
        return {"ok": False, "url": url, "site": site,
                "error": "could not open this link (unsupported or removed)"}
    try:
        e = json.loads(r.stdout.splitlines()[0])
    except Exception:
        return {"ok": False, "url": url, "site": site, "error": "could not read this link"}
    live = e.get("is_live")
    if live is None:
        live = e.get("live_status") == "is_live"
    thumbs = e.get("thumbnails") or []
    thumb = thumbs[-1].get("url") if thumbs else None
    return {"ok": True, "url": e.get("webpage_url") or url, "site": site,
            "title": e.get("title") or "", "channel": e.get("uploader") or "",
            "is_live": live, "thumbnail": thumb,
            "viewers": e.get("concurrent_view_count"),
            "duration": e.get("duration"),
            "error": None if live else "this is not live right now"}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Search live streams / resolve a link.")
    ap.add_argument("--search", default=None, help="search YouTube live streams")
    ap.add_argument("--resolve", default=None, help="validate a pasted link")
    a = ap.parse_args()
    if a.search:
        for r in search_youtube_live(a.search):
            print(f"[LIVE] {r['title']} — {r['channel']} — {r['url']} "
                  f"(viewers: {r['viewers']})")
    if a.resolve:
        print(json.dumps(resolve_url(a.resolve), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
