"""GTA 6 Live Clipper — local browser dashboard.

The tool runs on his PC; he uses it through a browser page on localhost
(same pattern as PeakCut). A public website cannot do this job: no server
can cheaply run 24/7 live capture + Whisper + rendering, and yt-dlp/ffmpeg
don't run in a browser. The UI is the professional Muse redesign (2026-11-18, dark theme, PeakCut red,
self-hosted Anton); the HTML structure and API routes are the contract, keep
their ids/paths stable (see README).

Pages (one page, sections):
- How it works (3 steps, always visible at the top).
- Find streams: SEARCH YouTube live (no key) + PASTE a link (YouTube /
  Twitch / Kick / any yt-dlp live URL) — validated live, saved to the
  watch list (channels.json).
- Watch setup: style (Favorites rotation = auto, or one style), crop mode,
  language, resolution, segment, trigger. Start/Stop/Clip now.
- Styles: EVERY caption style with a real animated preview video (CapCut-
  style). Star = favorite; order = rotation order. The auto-clip loop
  rotates through favorites one by one.
- VOD: paste an ALREADY-UPLOADED video URL (or a local file) -> best
  moments -> vertical Shorts -> gallery. (Shorts-making only — NOT a
  clip downloader; the YT Clip Tool stays a separate tool.)
- Gallery: finished clips with preview + Download (live + VOD).
- Auto-post queue (YouTube) + Settings (optional free Gemini key).

Run:  python dashboard.py
Then open http://127.0.0.1:5000 (auto picks the next free port if busy).
The dashboard calls the same engine as clip_live.py — CLI and dashboard
share the code; nothing is duplicated.
"""
import argparse
import datetime
import json
import os
import socket
import sys
import threading
import uuid
from collections import deque

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from clip_live import WATCH_STATUS, WATCH_LOCK, add_mark, run_loop, load_channels  # noqa: E402

try:
    from post_queue import load_queue, youtube_status  # noqa: E402
except Exception:
    load_queue = None

    def youtube_status():
        return "missing_secrets"

from captions import STYLE_META, STYLES  # noqa: E402
from positions import (CAPTION_META, CAPTION_POSITIONS, FACECAM_META,  # noqa: E402
                       FACECAM_POSITIONS, RATIO_META, RATIOS,
                       draw_layout_preview, ratio_params)
from settings import (get_favorites, get_positions, set_favorites,  # noqa: E402
                      set_positions, peek_next_favorite,
                      get_gemini_key, set_gemini_key)
from stream_discovery import search_youtube_live, resolve_url  # noqa: E402

CHANNELS_PATH = os.path.join(BASE, "channels.json")


def _load_channels_file():
    try:
        return load_channels(CHANNELS_PATH)
    except Exception:
        return []


def _save_channels_file(channels):
    tmp = CHANNELS_PATH + ".tmp"
    json.dump({"channels": channels}, open(tmp, "w"), indent=1)
    os.replace(tmp, CHANNELS_PATH)


def _free_port(start=5000):
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def create_app(out_dir="clips"):
    from flask import Flask, jsonify, request, send_file, send_from_directory

    app = Flask(__name__)
    state = {
        "thread": None,
        "stop_event": None,
        "force_event": threading.Event(),
        "out_dir": os.path.abspath(out_dir),
        "channels": [],
        "events": deque(maxlen=50),
        "started_at": None,
        "vod_jobs": {},
        "vod_lock": threading.Lock(),
    }
    os.makedirs(state["out_dir"], exist_ok=True)

    def _log(msg):
        state["events"].appendleft(
            {"t": datetime.datetime.now().strftime("%H:%M:%S"), "msg": msg})

    def _clips():
        d = state["out_dir"]
        out = []
        for f in sorted(os.listdir(d), key=lambda x: os.path.getmtime(os.path.join(d, x)),
                        reverse=True):
            if (f.startswith("gta6_short_") or f.startswith("vod_short_")) and f.endswith(".mp4"):
                p = os.path.join(d, f)
                info = {}
                try:
                    info = json.load(open(p + ".info.json"))
                except Exception:
                    pass
                out.append({"name": f,
                            "size_mb": round(os.path.getsize(p) / 1e6, 1),
                            "created": datetime.datetime.fromtimestamp(
                                os.path.getmtime(p)).strftime("%d %b %H:%M"),
                            "style": info.get("style", ""),
                            "kind": info.get("kind", "live"),
                            "trigger": info.get("trigger", ""),
                            "channel": info.get("channel", ""),
                            "review": info.get("review"),
                            "signals": info.get("signals", [])})
        return out

    def _running():
        return state["thread"] is not None and state["thread"].is_alive()

    @app.route("/")
    def index():
        with open(os.path.join(BASE, "dashboard.html"), encoding="utf-8") as f:
            return f.read()

    @app.route("/static/previews/<name>")
    def preview_file(name):
        if "/" in name or "\\" in name or ".." in name:
            return jsonify({"error": "bad name"}), 400
        d = os.path.join(BASE, "static", "previews")
        if not os.path.isfile(os.path.join(d, name)):
            return jsonify({"error": "not found"}), 404
        return send_from_directory(d, name)

    @app.route("/static/positions/<name>")
    def position_file(name):
        if "/" in name or "\\" in name or ".." in name:
            return jsonify({"error": "bad name"}), 400
        d = os.path.join(BASE, "static", "positions")
        if not os.path.isfile(os.path.join(d, name)):
            return jsonify({"error": "not found"}), 404
        return send_from_directory(d, name)

    @app.route("/static/layout_previews/<name>")
    def layout_preview_file(name):
        if "/" in name or "\\" in name or ".." in name:
            return jsonify({"error": "bad name"}), 400
        d = os.path.join(BASE, "static", "layout_previews")
        if not os.path.isfile(os.path.join(d, name)):
            return jsonify({"error": "not found"}), 404
        return send_from_directory(d, name)

    @app.route("/api/status")
    def status():
        with WATCH_LOCK:
            channels = [dict(v) for v in WATCH_STATUS.values()]
        queue = load_queue(state["out_dir"]) if load_queue else []
        favs = get_favorites()
        nxt, pos, total = peek_next_favorite()
        with state["vod_lock"]:
            vod_jobs = [dict(v) for v in state["vod_jobs"].values()]
        return jsonify({
            "running": _running(),
            "channels": channels,
            "started_at": state["started_at"],
            "clips": _clips(),
            "queue": queue,
            "youtube": youtube_status(),
            "events": list(state["events"])[:20],
            "out_dir": state["out_dir"],
            "favorites": favs,
            "next_favorite": {"style": nxt, "position": pos, "total": total},
            "gemini_configured": bool(get_gemini_key()),
            "vod_jobs": vod_jobs,
        })

    # ---- stream discovery: search + paste link + watch list ----
    @app.route("/api/search")
    def search():
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"ok": False, "error": "empty query"}), 400
        try:
            results = search_youtube_live(q, limit=10)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "results": results})

    @app.route("/api/resolve", methods=["POST"])
    def resolve():
        data = request.get_json(force=True) or {}
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "empty URL"}), 400
        try:
            r = resolve_url(url)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify(r)

    @app.route("/api/channels")
    def channels():
        return jsonify({"ok": True, "channels": _load_channels_file()})

    @app.route("/api/channels", methods=["POST"])
    def channels_add():
        data = request.get_json(force=True) or {}
        action = data.get("action", "add")
        url = (data.get("url") or "").strip()
        chs = _load_channels_file()

        def _urls(lst):
            return [c if isinstance(c, str) else (c.get("url") or c.get("channel"))
                    for c in lst]
        if action == "set":
            new = data.get("channels") or []
            new = [c.strip() for c in new if str(c).strip()]
            _save_channels_file(new)
            return jsonify({"ok": True, "channels": new})
        if not url:
            return jsonify({"ok": False, "error": "empty URL"}), 400
        if action == "add":
            if url not in _urls(chs):
                chs.append(url)
                _save_channels_file(chs)
            return jsonify({"ok": True, "channels": chs})
        if action == "remove":
            chs = [c for c in chs if (c if isinstance(c, str) else
                                      (c.get("url") or c.get("channel"))) != url]
            _save_channels_file(chs)
            return jsonify({"ok": True, "channels": chs})
        return jsonify({"ok": False, "error": "unknown action"}), 400

    # ---- styles + favorites ----
    @app.route("/api/styles")
    def styles():
        favs = get_favorites()
        nxt, pos, total = peek_next_favorite()
        out = []
        for sid in STYLES:
            m = dict(STYLE_META.get(sid, {"name": sid, "desc": "", "source": ""}))
            m.update({"id": sid,
                      "preview": f"/static/previews/{sid}.mp4",
                      "preview_exists": os.path.isfile(
                          os.path.join(BASE, "static", "previews", f"{sid}.mp4")),
                      "favorite": sid in favs,
                      "fav_order": (favs.index(sid) + 1) if sid in favs else 0})
            out.append(m)
        return jsonify({"ok": True, "styles": out, "favorites": favs,
                        "next_favorite": {"style": nxt, "position": pos, "total": total}})

    @app.route("/api/favorites", methods=["POST"])
    def favorites():
        data = request.get_json(force=True) or {}
        favs = [str(f) for f in (data.get("favorites") or []) if str(f) in STYLES]
        set_favorites(favs)
        return jsonify({"ok": True, "favorites": favs})

    @app.route("/api/previews/rebuild", methods=["POST"])
    def previews_rebuild():
        def _run():
            try:
                from preview_styles import build_all
                build_all(rebuild=True)
                _log("style previews rebuilt")
            except Exception as e:
                _log(f"preview rebuild failed: {e}")
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "msg": "rebuilding previews in the background"})

    # ---- positions (facecam + captions + ratios) ----
    @app.route("/api/positions")
    def positions():
        cur = get_positions()
        pos_dir = os.path.join(BASE, "static", "positions")
        return jsonify({
            "ok": True,
            "current": cur,
            "facecam": [{"id": k, "name": v["name"], "desc": v["desc"],
                         "preview": f"/static/positions/facecam-{k}.jpg",
                         "preview_exists": os.path.isfile(
                             os.path.join(pos_dir, f"facecam-{k}.jpg"))}
                        for k, v in FACECAM_META.items() if k != "auto"],
            "captions": [{"id": k, "name": v["name"], "desc": v["desc"],
                          "preview": f"/static/positions/caption-{k}.jpg",
                          "preview_exists": os.path.isfile(
                              os.path.join(pos_dir, f"caption-{k}.jpg"))}
                         for k, v in CAPTION_META.items() if k != "auto"],
            "ratios": [{"id": k, "name": v["name"], "desc": v["desc"]}
                       for k, v in RATIO_META.items()],
            "combined_preview": "/static/positions/combined-auto.jpg",
        })

    @app.route("/api/positions", methods=["POST"])
    def positions_save():
        data = request.get_json(force=True) or {}
        set_positions(data.get("facecam_pos"), data.get("caption_pos"),
                      data.get("ratio"))
        _log(f"positions saved: facecam={data.get('facecam_pos')} "
             f"captions={data.get('caption_pos')} ratio={data.get('ratio')}")
        return jsonify({"ok": True, "current": get_positions()})

    @app.route("/api/layout_preview", methods=["POST"])
    def layout_preview():
        """Per-stream FULL layout preview (his 00:08 order): an annotated
        still from THIS stream/video's own frames — facecam box, caption
        band, crop boundaries, safe zones drawn on the actual frame.
        source = a local video file path, or a channel URL (uses the
        latest capture in clips/buffer for that channel)."""
        data = request.get_json(force=True) or {}
        source = (data.get("source") or "").strip()
        if not source:
            return jsonify({"ok": False, "error": "give a video file or channel URL"}), 400
        try:
            from PIL import Image
            import hashlib
            import subprocess
            video = None
            if os.path.isfile(source):
                video = source
            else:
                # channel URL: latest buffer segment for it
                slug = source.rstrip("/").split("/")[-1].lower()
                buf = os.path.join(state["out_dir"], "buffer")
                cands = [os.path.join(buf, f) for f in os.listdir(buf)
                         if f.startswith("seg_" + slug) and f.endswith(".mp4")] \
                    if os.path.isdir(buf) else []
                if cands:
                    video = max(cands, key=os.path.getmtime)
            if not video:
                return jsonify({"ok": False,
                                "error": "no capture yet for this stream — "
                                         "start the watch first, then preview"}), 404
            # frame at 30% of the duration
            dur = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", video],
                capture_output=True, text=True).stdout.strip() or 0)
            t = max(1.0, dur * 0.3)
            tmp = os.path.join(BASE, "static", "layout_previews")
            os.makedirs(tmp, exist_ok=True)
            key = hashlib.sha256(
                f"{video}|{os.path.getmtime(video)}".encode()).hexdigest()[:12]
            frame_path = os.path.join(tmp, f"frame_{key}.jpg")
            if not os.path.isfile(frame_path):
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video,
                                "-ss", str(t), "-frames:v", "1", frame_path],
                               check=True)
            frame = Image.open(frame_path)
            # detect the facecam on this stream's own frames
            cur = get_positions()
            rp = ratio_params(cur.get("ratio", "9:16"), 720)
            # detection works on a frames dir — extract 8 sample frames
            frames_dir = os.path.join(tmp, f"frames_{key}")
            if not os.path.isdir(frames_dir) or len(os.listdir(frames_dir)) < 4:
                os.makedirs(frames_dir, exist_ok=True)
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video,
                                "-vf", "fps=1/15", "-frames:v", "8",
                                os.path.join(frames_dir, "f_%04d.jpg")],
                               check=True)
            layout_info = None
            try:
                from layout import choose_layout, detect_facecam, fallback_layout
                det = detect_facecam(
                    frames_dir, 1920, 1080,
                    model_path=os.path.join(
                        BASE, "models", "blaze_face_full_range.tflite"))
                layout_info = choose_layout(
                    det, 1920, 1080,
                    facecam_pos=cur.get("facecam_pos", "auto"),
                    caption_pos=cur.get("caption_pos", "auto"),
                    ratio=cur.get("ratio", "9:16"), res=720) \
                    if det else fallback_layout(cur.get("ratio", "9:16"), 720)
            except Exception as e:
                from layout import fallback_layout
                layout_info = fallback_layout(cur.get("ratio", "9:16"), 720)
                layout_info["note"] = f"detection failed ({e}) — fallback shown"
            # draw the annotated preview on the actual frame
            kind = "band" if layout_info.get("band") else (
                "corner" if layout_info.get("overlay") else None)
            rect = None
            if kind == "corner":
                ov = layout_info["overlay"]
                rect = (ov["x"], ov["y"], ov["w"], ov["h"])
            layout_draw = {"facecam": (kind, rect),
                           "caption_y": layout_info["caption_y"],
                           "strip_h": rp["strip_h"],
                           "caption_name": cur.get("caption_pos", "auto"),
                           "corner": layout_info.get("corner", ""),
                           "band_h": rp["band_h"]}
            img = draw_layout_preview(frame, layout_draw)
            out_path = os.path.join(tmp, f"layout_{key}.jpg")
            img.convert("RGB").save(out_path, quality=88)
            return jsonify({"ok": True,
                            "preview": f"/static/layout_previews/layout_{key}.jpg",
                            "corner": layout_info.get("corner"),
                            "caption_y": layout_info.get("caption_y"),
                            "detection": layout_info.get("detection_method"),
                            "note": layout_info.get("note", "")})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:300]}), 500

    # ---- manual mark + gallery review ----
    @app.route("/api/mark", methods=["POST"])
    def mark():
        data = request.get_json(force=True, silent=True) or {}
        channel = (data.get("channel") or "").strip() or None
        m = add_mark(state["out_dir"], channel)
        _log(f"marked a moment at {m['at']}" +
             (f" for {channel}" if channel else ""))
        return jsonify({"ok": True, "mark": m})

    @app.route("/api/gallery/<name>/review", methods=["POST"])
    def gallery_review(name):
        if "/" in name or "\\" in name or ".." in name or not name.endswith(".mp4"):
            return jsonify({"error": "bad name"}), 400
        data = request.get_json(force=True, silent=True) or {}
        status = (data.get("status") or "").strip().lower()
        if status not in ("approved", "rejected", "none"):
            return jsonify({"error": "status must be approved|rejected|none"}), 400
        info_path = os.path.join(state["out_dir"], name + ".info.json")
        if not os.path.isfile(info_path):
            return jsonify({"error": "clip not found"}), 404
        info = json.load(open(info_path))
        info["review"] = None if status == "none" else status
        json.dump(info, open(info_path, "w"), indent=1)
        return jsonify({"ok": True, "review": info["review"]})

    # ---- settings (Gemini key) ----
    @app.route("/api/settings/gemini", methods=["POST"])
    def settings_gemini():
        data = request.get_json(force=True) or {}
        key = (data.get("key") or "").strip()
        ok = set_gemini_key(key)
        _log("Gemini key saved" if ok else "Gemini key cleared")
        return jsonify({"ok": True, "configured": ok})

    # ---- watch loop ----
    @app.route("/api/start", methods=["POST"])
    def start():
        if _running():
            return jsonify({"ok": False, "error": "already running"}), 400
        data = request.get_json(force=True) or {}
        raw = data.get("channels", "")
        if isinstance(raw, str):
            channels = [c.strip() for c in raw.replace(",", "\n").splitlines()
                        if c.strip()]
        else:
            channels = [str(c).strip() for c in raw if str(c).strip()]
        if not channels:
            return jsonify({"ok": False, "error": "give at least one stream URL"}), 400
        # persist the watch list so it survives restarts
        _save_channels_file(channels)
        style = data.get("style", "auto")
        mode = data.get("mode", "game")
        res = int(data.get("res", 720))
        ratio = str(data.get("ratio", "9:16"))
        facecam_pos = str(data.get("facecam_pos", "auto"))
        caption_pos = str(data.get("caption_pos", "auto"))
        set_positions(facecam_pos, caption_pos, ratio)
        language = data.get("language", "auto")
        caption_mode = data.get("caption_mode", "auto")
        roman_mode = data.get("roman_mode", "local")
        segment_minutes = int(data.get("segment_minutes", 3))
        threshold_db = float(data.get("threshold_db", -28))
        post = bool(data.get("post", False))
        state["channels"] = channels
        state["stop_event"] = threading.Event()
        state["force_event"] = threading.Event()
        state["started_at"] = datetime.datetime.now().strftime("%d %b %H:%M")
        state["events"].clear()
        _log(f"watch started: {len(channels)} channel(s), style={style}, post={'on' if post else 'off'}")

        def _on_clip(clip_path, info):
            _log(f"CLIP SAVED: {os.path.basename(clip_path)} "
                 f"(moment {info['moment']['start']:.0f}-{info['moment']['end']:.0f}s, "
                 f"style={info.get('style')})")

        def _on_event(msg):
            _log(msg)

        t = threading.Thread(
            target=run_loop,
            kwargs={"channels": channels, "out_dir": state["out_dir"],
                    "style": style, "mode": mode,
                    "language": language, "caption_mode": caption_mode,
                    "roman_mode": roman_mode, "res": res,
                    "ratio": ratio, "facecam_pos": facecam_pos,
                    "caption_pos": caption_pos,
                    "segment_minutes": segment_minutes,
                    "threshold_db": threshold_db,
                    "stop_event": state["stop_event"],
                    "on_clip": _on_clip, "on_event": _on_event,
                    "post": post, "force_event": state["force_event"]},
            daemon=True)
        state["thread"] = t
        t.start()
        return jsonify({"ok": True, "channels": channels})

    @app.route("/api/stop", methods=["POST"])
    def stop():
        if state["stop_event"] is not None:
            state["stop_event"].set()
        _log("stop requested — finishing the current segment, then stopping")
        return jsonify({"ok": True})

    @app.route("/api/clip-now", methods=["POST"])
    def clip_now():
        if not _running():
            return jsonify({"ok": False, "error": "watch is not running"}), 400
        data = request.get_json(force=True, silent=True) or {}
        style = (data.get("style") or "").strip()
        if style and style in STYLES:
            # explicit style for the next manual clip (overrides rotation once)
            from clip_live import set_clip_now_style
            set_clip_now_style(style)
        state["force_event"].set()
        _log("Clip now pressed — the next segment of every channel will render")
        return jsonify({"ok": True})

    # ---- VOD mode ----
    @app.route("/api/vod", methods=["POST"])
    def vod_start():
        data = request.get_json(force=True) or {}
        source = (data.get("source") or "").strip()
        if not source:
            return jsonify({"ok": False, "error": "give a video URL or a local file path"}), 400
        job_id = uuid.uuid4().hex[:8]
        job = {"id": job_id, "source": source, "status": "running",
               "started": datetime.datetime.now().strftime("%H:%M:%S"),
               "clips": [], "error": None}
        with state["vod_lock"]:
            state["vod_jobs"][job_id] = job

        def _run():
            try:
                from vod import process_vod
                manual = None
                if data.get("manual"):
                    s, e = str(data["manual"]).split("-")
                    manual = (float(s), float(e))
                clips = process_vod(
                    source, state["out_dir"],
                    style=(data.get("style") or None),
                    mode=data.get("mode", "game"),
                    res=int(data.get("res", 720)),
                    ratio=str(data.get("ratio", "9:16")),
                    facecam_pos=str(data.get("facecam_pos", "auto")),
                    caption_pos=str(data.get("caption_pos", "auto")),
                    clip_len=float(data.get("clip_len", 40)),
                    count=int(data.get("count", 3)),
                    manual=manual,
                    language=data.get("language", "auto"),
                    caption_mode=data.get("caption_mode", "auto"),
                    roman_mode=data.get("roman_mode", "local"),
                    api_key=get_gemini_key())
                with state["vod_lock"]:
                    job["status"] = "done"
                    job["clips"] = [os.path.basename(c) for c in clips]
                _log(f"VOD done: {len(clips)} clip(s) from {source[:60]}")
            except Exception as e:
                with state["vod_lock"]:
                    job["status"] = "failed"
                    job["error"] = str(e)[:300]
                _log(f"VOD failed: {e}")

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id})

    @app.route("/api/vod")
    def vod_list():
        with state["vod_lock"]:
            jobs = [dict(v) for v in state["vod_jobs"].values()]
        return jsonify({"ok": True, "jobs": jobs})

    @app.route("/clips/<name>")
    def download_clip(name):
        # basename guard: only files directly in the out dir, .mp4 only
        if "/" in name or "\\" in name or ".." in name or not name.endswith(".mp4"):
            return jsonify({"error": "bad name"}), 400
        p = os.path.join(state["out_dir"], name)
        if not os.path.isfile(p):
            return jsonify({"error": "not found"}), 404
        return send_file(p, mimetype="video/mp4", as_attachment=False)

    return app


def main():
    # Frozen exe: put the bundle dir (ffmpeg/ffprobe/yt-dlp.exe) on PATH
    from ytdlp_cmd import ensure_frozen_binaries_on_path
    ensure_frozen_binaries_on_path()
    ap = argparse.ArgumentParser(description="GTA 6 Live Clipper dashboard (localhost).")
    ap.add_argument("--out", default="clips", help="output dir for finished Shorts")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    port = a.port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((a.host, port))
        except OSError:
            port = _free_port(a.port)
    print(f"GTA 6 Live Clipper dashboard: http://{a.host}:{port}")
    print("Press Ctrl+C to stop. (The watch loop keeps running only while this is open.)")
    try:  # double-click launch (exe): open the dashboard in the default browser
        import webbrowser
        threading.Timer(1.0, webbrowser.open,
                        args=(f"http://{a.host}:{port}",)).start()
    except Exception:
        pass
    create_app(a.out).run(host=a.host, port=port, threaded=True)


if __name__ == "__main__":
    main()
