"""GTA 6 Live Clipper — main tool.

Watches live Twitch streams 24/7, catches the loud moments, and turns them
into vertical 720x1280 60fps Shorts with Glow Neon captions (or Hormozi Pop)
— while the stream is still live, so the Short is out BEFORE the streamer
publishes their own video. Finished clips go into an auto-post queue
(YouTube first; Instagram/TikTok are v2).

How it works (each step proven in spikes, 2026-11-17):
1. Capture: yt-dlp records live streams at 1080p60 in short segments
   (default 3 min). Twitch live max is 1080p60; capture runs ~1.05x
   real-time (not throttled). Channels are watched ROUND-ROBIN — one
   segment per channel per round — because a home PC (i5, no GPU) cannot
   capture + transcribe several 1080p60 streams in parallel. With 3
   channels at 3 min segments, each channel is checked every ~9 min.
2. Moment pick: faster-whisper tiny transcribes the segment (~15s per
   3 min) + audio-energy profile. The best 30-45s window is the loudest
   5s window with speech in it, with 8s of lead-in so the hook lands early.
3. Trigger: a clip renders when the loudest window is at/above the
   threshold (default -28 dBFS), or on demand (--clip-now / dashboard
   "Clip now" button), or after every segment in --once mode.
4. Render: frame-sequence extraction first (raw live captures have broken
   timestamps — never edit them directly), 608x1080 centre crop, lanczos
   upscale to 720x1280, Hormozi Pop / Glow Neon captions (PIL PNG track),
   H.264 CRF 18, preset veryfast. Moment -> Short in ~3-5 min.
5. Post queue: every finished clip is queued (queue.json) with a title
   from its loudest moment. A queue worker uploads to YouTube with
   spacing (default 30 min between posts) and a daily cap (default 10).
   YouTube needs a one-time OAuth setup (see README) — until then clips
   wait in the queue as "awaiting_setup", never lost.

Usage:
  # one channel, until Ctrl+C
  python clip_live.py https://www.twitch.tv/deansocool --out clips
  # 24/7 watch of a channel list
  python clip_live.py --channels channels.json --out clips
  # one segment, clip it, exit (test)
  python clip_live.py https://www.twitch.tv/deansocool --once --clip-now
  # watch + auto-post to YouTube
  python clip_live.py --channels channels.json --out clips --post

The same engine drives dashboard.py (local browser UI) — it imports
run_loop() and drives it from a thread. CLI and dashboard share the code.
"""
import argparse
import datetime
import json
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from capture import capture_segment, is_live, prune_segments  # noqa: E402
from render_short import render_short, FRAME_STYLES, DEFAULT_FRAME_STYLE  # noqa: E402
try:
    # Automatic per-clip A/V sync (PeakCut _align pattern, 2026-11-19):
    # measures the video-behind-audio offset from game subtitles vs
    # Whisper transcript, carries clock_fix forward per channel.
    from av_align import (measure_av_offset, load_clock_fix,
                          save_clock_fix, channel_key_for)  # noqa: E402
    _AV_ALIGN = True
except Exception:  # module missing -> fall back to 0.0, loop unaffected
    _AV_ALIGN = False
    measure_av_offset = load_clock_fix = save_clock_fix = None
    channel_key_for = lambda p: "unknown"
from transcribe import pick_moment, transcribe_segment, moment_window  # noqa: E402
from captions import STYLES  # noqa: E402
from settings import next_favorite_style  # noqa: E402
from chat import (ChatManager, chat_burst_windows, hype_burst_windows,
                  score_chat_windows)  # noqa: E402
try:
    import gemini_pick as _gemini_pick  # wording-based moment scoring
except Exception:  # module missing -> loudness-only picking, loop unaffected
    _gemini_pick = None
# Game detection (ClipKontrast games.py port) + stream titles for it.
import games  # noqa: E402
from stream_discovery import get_stream_title  # noqa: E402

try:
    from post_queue import enqueue_clip, start_queue_worker  # noqa: E402
except Exception:  # google-api client not installed -> posting disabled
    enqueue_clip = None
    start_queue_worker = None

try:
    import bridge_client as _bridge  # website requests + progress + publish
except Exception:  # bridge down -> loop still runs on its own channels
    _bridge = None

DEFAULT_STYLE = "neon"
DEFAULT_MODE = "game"
DEFAULT_LANGUAGE = "auto"   # auto-detect (Whisper); en | hi | ur to force
DEFAULT_CAPTION_MODE = "auto"  # auto | en | hi | hi-roman | ur | ur-roman
DEFAULT_ROMAN_MODE = "local"   # local | gemini (gemini needs key in config.json)
DEFAULT_SEGMENT_MINUTES = 3
DEFAULT_THRESHOLD_DB = -28.0
DEFAULT_CLIP_LEN = 40.0  # 2026-11-19 (sohaib): 40s is the MINIMUM clip
                         # length, NOT a fixed length — the moment's
                         # natural speech span decides the length, up to
                         # MAX_CLIP_LEN (60s). See transcribe.moment_window.
MAX_CLIP_LEN = 60.0
DEFAULT_PAD = 8.0
DEFAULT_KEEP_SEGMENTS = 4
DEFAULT_POST_SPACING_MIN = 30
DEFAULT_DAILY_CAP = 10

# --- auto-shorts quality gate (2026-11-19, sohaib's order) -------------
# Auto clips must be the BEST shots only (quality over quantity): Gemini
# judges the WORDING (funny / emotional / incident), not just loudness.
# Only a Gemini score >= GEMINI_MIN_SCORE renders an auto clip, and at
# most AUTO_CAP_PER_HOUR auto clips per hour across all channels.
# No Gemini key -> the gate is off and the old loudness trigger runs
# (logged once), so the loop never blocks on the key.
GEMINI_MIN_SCORE = 7.0
AUTO_CAP_PER_HOUR = 2
AUTO_CAP_WINDOW_S = 3600
AUTO_CAP_FILE = os.path.join(BASE, "auto_cap.json")
_GEMINI_NO_KEY_LOGGED = {"done": False}

# Live watch status, read by the dashboard: {channel: {...}}
WATCH_STATUS = {}
WATCH_LOCK = threading.Lock()

# --- manual marks ("Mark moment" button — ClipzLab idea) ---------------
# A mark is a wall-clock timestamp pressed by the user while watching.
# The watch loop turns the next mark inside the current segment into a
# forced clip centred on it. Stored in <out_dir>/marks.json.
MARKS_FILE = "marks.json"


def add_mark(out_dir, channel=None):
    """Record a manual mark (dashboard button / CLI). Returns the mark."""
    path = os.path.join(out_dir, MARKS_FILE)
    try:
        marks = json.load(open(path))
        if not isinstance(marks, list):
            marks = []
    except Exception:
        marks = []
    mark = {"channel": channel, "ts": time.time(),
            "at": datetime.datetime.now().strftime("%H:%M:%S")}
    marks.append(mark)
    json.dump(marks, open(path, "w"), indent=1)
    return mark


def take_marks(out_dir, channel=None):
    """Take (and clear) all marks for this channel — the loop consumes
    them once. Returns [wall-clock timestamps]."""
    path = os.path.join(out_dir, MARKS_FILE)
    try:
        marks = json.load(open(path))
        if not isinstance(marks, list):
            marks = []
    except Exception:
        marks = []
    keep, taken = [], []
    for m in marks:
        if channel is None or m.get("channel") in (None, channel):
            taken.append(m["ts"])
        else:
            keep.append(m)
    json.dump(keep, open(path, "w"), indent=1)
    return taken


# --- "clip that" voice trigger (ClipzLab idea) -------------------------
CLIP_THAT_PHRASES = (("clip", "that"), ("clip", "it"), ("clip", "this"))


def find_clip_that(transcript_path):
    """Timestamps where the streamer said 'clip that' / 'clip it' /
    'clip this' — the streamer marks their own moment with their voice.
    Returns [seconds] (start of the phrase)."""
    try:
        words = json.load(open(transcript_path)).get("words", [])
    except Exception:
        return []
    toks = [(w["start"], (w.get("display") or w["word"]).lower().strip(".,!?\"'"))
            for w in words]
    hits = []
    for i in range(len(toks) - 1):
        for a, b in CLIP_THAT_PHRASES:
            if toks[i][1] == a and toks[i + 1][1] == b:
                hits.append(toks[i][0])
    return hits


# --- per-clip artifact bundle (Clipie idea) ----------------------------
def write_artifact_bundle(clip_path, info, transcript_path):
    """transcript.json + script.md + analysis.json next to the clip —
    gallery metadata + ready titles/hashtags for posting. Returns the
    bundle dict (also written to <clip>.analysis.json)."""
    base = os.path.splitext(clip_path)[0]
    try:
        import shutil
        shutil.copy(transcript_path, base + ".transcript.json")
    except Exception:
        pass
    words, segs = [], []
    try:
        tr = json.load(open(transcript_path))
        words, segs = tr.get("words", []), tr.get("segments", [])
    except Exception:
        pass
    moment = info.get("moment", {}) or {}
    ms, me = moment.get("start", 0), moment.get("end", 0)
    in_moment = [w for w in words if ms <= w["start"] < me]
    text = " ".join((w.get("display") or w["word"]) for w in in_moment)
    # title: the shortest punchy segment in the moment, else first words
    title = ""
    for s in segs:
        if ms <= s["start"] and s["end"] <= me:
            st = s["text"].strip()
            if 3 <= len(st.split()) <= 10:
                title = st
                break
    if not title:
        title = " ".join(text.split()[:8]) or "GTA 6 Short"
    title = title[:80]
    hook = " ".join(text.split()[:12])
    # hashtags: the detected game's tags first (ClipKontrast games.py),
    # else the GTA fallback. YouTube tags in post_queue get the same
    # list without the '#' (see enqueue_clip).
    game_tags = info.get("game_tags") or []
    game_name = info.get("game_name")
    if game_tags:
        tags = ["#" + t for t in game_tags]
    else:
        tags = ["#GTA6", "#GTA", "#Gaming", "#Shorts"]
    if info.get("channel"):
        ch = str(info["channel"]).rstrip("/").split("/")[-1]
        if ch:
            tags.append("#" + "".join(c for c in ch if c.isalnum()))
    title_game = (game_name + " — " + title) if game_name else ("GTA 6 — " + title)
    bundle = {"clip": os.path.basename(clip_path),
              "title": title, "hook": hook,
              "moment": moment, "score": moment.get("score"),
              "signals": info.get("signals", []),
              "style": info.get("style"), "mode": info.get("mode"),
              "language": info.get("language"),
              "channel": info.get("channel"),
              "game": game_name,
              "words": len(in_moment),
              "hashtags": tags,
              "titles": [title,
                         title + " 😱" if len(title) < 70 else title,
                         title_game]}
    json.dump(bundle, open(base + ".analysis.json", "w"),
              ensure_ascii=False, indent=1)
    md = (f"# {title}\n\n**Hook:** {hook}\n\n"
          f"**Moment:** {ms:.1f}-{me:.1f}s · style {info.get('style')} · "
          f"{info.get('mode')} · {info.get('language')}\n\n"
          f"**Captions:**\n\n{text}\n\n"
          f"**Hashtags:** {' '.join(tags)}\n")
    open(base + ".script.md", "w", encoding="utf-8").write(md)
    return bundle

# --- favorites rotation + manual style override -------------------------
_CLIP_NOW_STYLE = {"style": None}   # set by the dashboard "Clip now" button
_CLIP_NOW_LOCK = threading.Lock()


def set_clip_now_style(style):
    """One manual clip uses this style instead of the rotation (then resets)."""
    with _CLIP_NOW_LOCK:
        _CLIP_NOW_STYLE["style"] = style


def _resolve_style(style):
    """Manual override first, then favorites rotation, else the given style."""
    with _CLIP_NOW_LOCK:
        override = _CLIP_NOW_STYLE["style"]
        _CLIP_NOW_STYLE["style"] = None
    if override:
        return override, "manual"
    if style in (None, "auto"):
        s, pos, total = next_favorite_style()
        return s, f"favorite {pos}/{total}"
    return style, "chosen"


# --- live chat: one tracker per Twitch channel (people, not messages) ---
CHAT = ChatManager()


def _adjust_for_chat(tp_path, start, end, tracker, seg_start_ts, seg_duration):
    """If the chat burst peaks outside the loudness-picked moment and there
    is real speech there, re-centre the clip on the burst. Returns
    (start, end, used_chat_burst, unique_chatters)."""
    if tracker is None:
        return start, end, False, 0
    bursts = chat_burst_windows(tracker, seg_start_ts, seg_duration)
    if not bursts:
        return start, end, False, 0
    top = bursts[0]  # most unique chatters (sorted desc)
    top_n = top["unique"]
    if top_n < 5:
        return start, end, False, top_n
    burst_c = (top["start"] + top["end"]) / 2  # segment-relative seconds
    # does the burst overlap the picked moment? then nothing to do
    if start - 5 <= burst_c <= end + 5:
        return start, end, False, top_n
    # speech at the burst? check transcript words near it
    try:
        words = json.load(open(tp_path)).get("words", [])
    except Exception:
        words = []
    near = [w for w in words if abs(w["start"] - burst_c) <= 15]
    if len(near) < 5:
        return start, end, False, top_n
    length = end - start
    ns = max(0.0, burst_c - length / 2)
    return ns, ns + length, True, top_n


def _set_status(channel, **kw):
    with WATCH_LOCK:
        st = WATCH_STATUS.setdefault(channel, {"channel": channel, "live": None,
                                               "last_check": None, "last_segment": None,
                                               "segments": 0, "clips": 0,
                                               "last_clip": None, "last_event": ""})
        st.update(kw)
        st["last_check"] = datetime.datetime.now().strftime("%H:%M:%S")


def _auto_cap_ok(cap=AUTO_CAP_PER_HOUR, window_s=AUTO_CAP_WINDOW_S):
    """True when fewer than `cap` auto clips were made in the last
    `window_s` seconds (timestamps in AUTO_CAP_FILE, pruned on read)."""
    try:
        ts = json.load(open(AUTO_CAP_FILE))
        if not isinstance(ts, list):
            ts = []
    except Exception:
        ts = []
    now = time.time()
    ts = [t for t in ts if now - float(t) < window_s]
    return len(ts) < cap


def _auto_cap_record():
    """Record one auto clip now (for the hourly cap)."""
    try:
        ts = json.load(open(AUTO_CAP_FILE))
        if not isinstance(ts, list):
            ts = []
    except Exception:
        ts = []
    now = time.time()
    ts = [t for t in ts if now - float(t) < AUTO_CAP_WINDOW_S]
    ts.append(now)
    try:
        json.dump(ts, open(AUTO_CAP_FILE, "w"))
    except Exception:
        pass


def process_segment(seg_path, out_dir, style=DEFAULT_STYLE, mode=DEFAULT_MODE,
                    clip_len=DEFAULT_CLIP_LEN, pad=DEFAULT_PAD,
                    prefix="gta6_short", transcript_path=None, energy_path=None,
                    channel=None, language=DEFAULT_LANGUAGE,
                    caption_mode=DEFAULT_CAPTION_MODE,
                    roman_mode=DEFAULT_ROMAN_MODE, res=720, moment=None,
                    ratio="9:16", facecam_pos="auto", caption_pos="auto",
                    facecam_size="medium", facecam_band=50,
                    facecam_band_w=100, frame_style=DEFAULT_FRAME_STYLE,
                    trigger_name=None, game_name=None, game_tags=None):
    """Transcribe + pick + render ONE segment. Returns (clip_path, info) or
    (None, info) if the segment has no usable moment.
    Pass transcript_path/energy_path to reuse an earlier transcription
    (the live loop already transcribed it for the trigger check).
    Pass moment=(start, end, why) to render a pre-picked moment (e.g. after
    the chat-burst adjustment) instead of re-picking.
    style="auto" (or None) rotates through the dashboard favorites."""
    style, style_src = _resolve_style(style)
    seg_name = os.path.splitext(os.path.basename(seg_path))[0]
    work_dir = os.path.join(out_dir, "work_" + seg_name)
    os.makedirs(work_dir, exist_ok=True)
    t0 = time.time()
    if transcript_path is None or energy_path is None:
        tp, ep = transcribe_segment(seg_path, work_dir, language=language,
                                    caption_mode=caption_mode,
                                    roman_mode=roman_mode)
    else:
        tp, ep = transcript_path, energy_path
    if moment is None:
        start, end, why = pick_moment(tp, ep, clip_len, pad)
    else:
        start, end, why = moment
    info = {"segment": seg_path, "transcript": tp, "energy": ep,
            "moment": {"start": start, "end": end,
                       "words": why.get("words", 0),
                       "loud_db": (why.get("loud_window") or {}).get("db"),
                       "score": why.get("score")},
            "style": style, "style_source": style_src, "mode": mode,
            "channel": channel, "language": language,
            "caption_mode": caption_mode, "kind": "live",
            "ratio": ratio, "facecam_pos": facecam_pos,
            "facecam_size": facecam_size, "facecam_band": facecam_band,
            "facecam_band_w": facecam_band_w, "frame_style": frame_style,
            "caption_pos": caption_pos, "game_name": game_name,
            "game_tags": game_tags or [],
            "trigger": trigger_name or style_src}
    if why.get("words", 0) < 3 and (why.get("loud_window") or {}).get("db", -99) < -35:
        print(f"[clip] segment has no usable moment "
              f"(words={why.get('words', 0)}, loud={info['moment']['loud_db']} dB) — skipping")
        return None, info
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    clip_path = os.path.join(out_dir, f"{prefix}_{ts}.mp4")
    # --- AUTOMATIC A/V SYNC (2026-11-19, PeakCut _align pattern) ---
    # Measure this clip's video-behind-audio offset from the game
    # subtitles vs the Whisper transcript (median over matched lines),
    # seeded by the channel's carried-forward clock_fix. The measured
    # value is passed as this clip's av_offset only; the global default
    # stays 0.0 and no manual flag is ever used. Measurement failure
    # (no subtitles, OCR unavailable, <3 matches) falls back to the
    # seed and NEVER crashes the loop.
    av_offset_s = 0.0
    av_measured = False
    if _AV_ALIGN:
        try:
            ckey = channel_key_for(seg_path)
            seed = load_clock_fix(ckey, 0.0)
            off, n, det = measure_av_offset(seg_path, tp, start, end,
                                            seed=seed, verbose=False)
            av_offset_s = float(off)
            av_measured = n >= 3
            if av_measured:
                save_clock_fix(ckey, av_offset_s)
            print(f"[clip] av-align: channel={ckey} seed={seed:.2f}s "
                  f"measured={av_offset_s:.2f}s matches={n} "
                  f"{'SAVED clock_fix' if av_measured else 'fallback to seed'}")
        except Exception as e:
            print(f"[clip] av-align failed ({e}) — using av_offset 0.0")
            av_offset_s = 0.0
    info["av_offset_s"] = round(av_offset_s, 2)
    info["av_measured"] = av_measured
    render_short(seg_path, start, end, tp, style, mode, clip_path, work_dir,
                 res=res, ratio=ratio, facecam_pos=facecam_pos,
                 caption_pos=caption_pos, facecam_size=facecam_size,
                 facecam_band=facecam_band, facecam_band_w=facecam_band_w,
                 frame_style=frame_style, av_offset_s=av_offset_s)
    info["clip"] = clip_path
    info["elapsed_s"] = round(time.time() - t0, 1)
    # gallery sidecar: style / trigger / source, read by the dashboard
    sidecar = {"style": style, "style_source": style_src, "kind": "live",
               "frame_style": frame_style,
               "trigger": info.get("trigger", ""),
               "channel": channel or "",
               "moment": info["moment"],
               "review": None,
               "signals": [f"loud {info['moment']['loud_db']} dB",
                           f"{why['words']} words"]}
    if info.get("chat_burst"):
        sidecar["signals"].append(
            f"chat burst ({info['chat_unique']} people)")
    json.dump(sidecar, open(clip_path + ".info.json", "w"), indent=1)
    # per-clip artifact bundle: transcript + script + analysis
    try:
        bundle = write_artifact_bundle(clip_path, info, tp)
        info["artifact_bundle"] = bundle
    except Exception as e:
        print(f"[clip] artifact bundle failed ({e}) — clip still saved",
              flush=True)
    print(f"[clip] SAVED {clip_path} in {info['elapsed_s']}s "
          f"(moment {start:.1f}-{end:.1f}s, {why['words']} words, style={style})")
    return clip_path, info


def _chat_burst_trigger(tracker, seg_start_ts, segment_minutes, tp, clip_len):
    """Chat-burst TRIGGER (ClipKontrast _watch_spikes idea, adapted to
    our per-segment windows): the crowd itself is a moment. Returns
    (moment_start, unique, score) or None. Fires only on a real crowd
    (>= 5 unique people, burst_score >= 0.6) AND real speech at the
    burst (>= 5 transcript words within 15s) — a spam wave alone
    never clips. Windows are segment-relative (see chat_burst_windows)."""
    if tracker is None:
        return None
    try:
        score = tracker.burst_score(seg_start_ts,
                                    seg_start_ts + segment_minutes * 60)
        wins = chat_burst_windows(tracker, seg_start_ts,
                                  segment_minutes * 60)
        if score >= 0.6 and wins and wins[0]["unique"] >= 5:
            top = wins[0]
            burst_c = (top["start"] + top["end"]) / 2
            try:
                words = json.load(open(tp)).get("words", [])
            except Exception:
                words = []
            near = [w for w in words if abs(w["start"] - burst_c) <= 15]
            if len(near) >= 5:
                s0 = max(0.0, burst_c - clip_len / 2)
                return s0, top["unique"], score
    except Exception:
        pass
    return None


def _hype_burst_trigger(tracker, seg_start_ts, segment_minutes, tp, clip_len,
                        gemini_score=None):
    """HYPE-burst TRIGGER (2026-11-19, sohaib's order): the chat suddenly
    SPIKED — many comments arriving at once — and that moment is a Short.
    Primary scorer is score_chat_windows (adapted from GTA-Auto-Clipper):
    distinct lines per user + reaction vocabulary + median/MAD robust
    signal, with the peak 5s bucket giving the exact hype moment for
    re-centring. Falls back to the ratio-based hype_burst_windows when
    the reference scorer has no text data. Returns
    (moment_start, unique, score, strength) or None, where score is the
    reference signal (0-4) or the ratio for the fallback. Fires when
    EITHER:
    - Gemini scored decently (>= 5) AND a hype burst exists (burst +
      wording together = a moment worth a Short), OR
    - the burst alone is very strong (signal >= 3.0, or ratio >= 5x,
      or unique >= 20).
    Requires real speech at the burst (>= 5 transcript words within
    15s) — a chat spike with nobody talking never clips.
    Windows are segment-relative."""
    if tracker is None:
        return None
    try:
        # Primary: reference-style scorer (distinct lines + reactions +
        # median/MAD). peak_t is the exact hype moment (5s bucket).
        scored = score_chat_windows(tracker, seg_start_ts,
                                    segment_minutes * 60)
        top = None
        score = 0.0
        strength = 0.0
        burst_c = None
        unique = 0
        if (scored and scored[0]["signal"] >= 2.5
                and scored[0]["burst"] >= 2.0):
            top = scored[0]
            score = top["signal"]
            strength = min(1.0, score / 4.0)
            burst_c = top["peak_t"]
            unique = top["chatters"]
        else:
            # Fallback: ratio-based spike (no text data, or no strong
            # reference signal).
            wins = hype_burst_windows(tracker, seg_start_ts,
                                      segment_minutes * 60)
            if not wins:
                return None
            top = wins[0]
            score = top["ratio"]
            strength = top["strength"]
            burst_c = (top["start"] + top["end"]) / 2
            unique = top["unique"]
        is_ref = top is not None and "signal" in top
        if is_ref:
            # reference signal (0-4): >= 3.0 alone is very strong
            strong_alone = score >= 3.0
        else:
            # ratio fallback: >= 5x spike or >= 20 people alone is strong
            strong_alone = score >= 5.0 or unique >= 20
        decent_gemini = gemini_score is not None and gemini_score >= 5.0
        if not (strong_alone or decent_gemini):
            return None
        try:
            words = json.load(open(tp)).get("words", [])
        except Exception:
            words = []
        near = [w for w in words if abs(w["start"] - burst_c) <= 15]
        if len(near) < 5:
            return None
        s0 = max(0.0, burst_c - clip_len / 2)
        return s0, unique, score, strength
    except Exception:
        pass
    return None


def _watch_one_segment(channel, out_dir, style, mode, segment_minutes,
                       threshold_db, clip_len, pad, keep_segments,
                       force_clip, on_clip, on_event, queue,
                       language=DEFAULT_LANGUAGE,
                       caption_mode=DEFAULT_CAPTION_MODE,
                       roman_mode=DEFAULT_ROMAN_MODE, res=720,
                       ratio="9:16", facecam_pos="auto", caption_pos="auto",
                       facecam_size="medium", facecam_band=50,
                       facecam_band_w=100, frame_style=DEFAULT_FRAME_STYLE,
                       mark_times=None, game_name=None, game_tags=None,
                       on_stage=None, auto_publish=False):
    """One capture+decide+maybe-render cycle for ONE channel.
    Returns the clip path made, or None. Updates WATCH_STATUS.
    on_stage(stage, progress) — optional progress hook (website requests):
    called at capturing (5) / transcribing (45) / rendering (65).
    auto_publish=True (round-robin auto path only) publishes a made clip
    to the Gallery via the bridge, exactly like a manual request clip.
    The manual request path keeps auto_publish=False — it publishes
    through its own on_clip and must never double-publish."""
    def ev(msg):
        print(msg, flush=True)
        if on_event:
            try:
                on_event(msg)
            except Exception:
                pass

    buffer_dir = os.path.join(out_dir, "buffer")
    os.makedirs(buffer_dir, exist_ok=True)
    live = is_live(channel)
    _set_status(channel, live=live)
    if not live:
        ev(f"[live] {channel} is not live right now — skipping this round")
        return None
    if on_stage:
        try: on_stage("capturing", 5)
        except Exception: pass
    # live chat (Twitch IRC / YouTube yt-dlp, people not messages) —
    # started once per channel; ChatManager returns None for unsupported
    tracker = CHAT.track(channel)
    seg_start_ts = time.time()
    seg_path = os.path.join(
        buffer_dir,
        f"seg_{channel.split('/')[-1]}_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.mp4")
    try:
        capture_segment(channel, seg_path, segment_minutes * 60,
                        log_path=os.path.join(buffer_dir, "capture.log"))
    except RuntimeError as e:
        ev(f"[live] capture failed for {channel}: {e}")
        return None
    _set_status(channel, last_segment=os.path.basename(seg_path))
    if on_stage:
        try: on_stage("transcribing", 45)
        except Exception: pass
    with WATCH_LOCK:
        WATCH_STATUS[channel]["segments"] += 1
    work_dir = os.path.join(out_dir, "work_" +
                            os.path.splitext(os.path.basename(seg_path))[0])
    try:
        tp, ep = transcribe_segment(seg_path, work_dir, language=language,
                                    caption_mode=caption_mode,
                                    roman_mode=roman_mode)
        _s, _e, why = pick_moment(tp, ep, clip_len, pad)
        loud = (why["loud_window"] or {}).get("db", -99)
        trigger = force_clip or loud >= threshold_db
        # manual mark / "clip that" beat loudness: a mark pressed while
        # watching, or the streamer saying "clip that", forces a clip
        # centred on that moment (ClipzLab ideas).
        trigger_name = None
        moment = (_s, _e, why)
        if mark_times:
            for mt in mark_times:
                off = mt - seg_start_ts
                if 0 <= off < segment_minutes * 60:
                    # variable length (40s floor, 60s cap) around the mark
                    s0, e0 = moment_window(tp, off, min_len=clip_len)
                    moment = (s0, min(segment_minutes * 60.0, e0), why)
                    trigger = True
                    trigger_name = "manual mark"
                    ev(f"[live] {channel}: manual mark at {off:.0f}s -> CLIP")
                    break
        if trigger_name is None:
            for ct in find_clip_that(tp):
                if 0 <= ct < segment_minutes * 60:
                    # variable length (40s floor, 60s cap) around "clip that"
                    s0, e0 = moment_window(tp, ct, min_len=clip_len)
                    moment = (s0, min(segment_minutes * 60.0, e0), why)
                    trigger = True
                    trigger_name = "clip that"
                    ev(f"[live] {channel}: streamer said 'clip that' "
                       f"at {ct:.0f}s -> CLIP")
                    break
        # Gemini wording gate (auto path only — never on force_clip,
        # manual marks or "clip that", which are user-initiated): when a
        # key is configured, the WORDING decides. Score >= GEMINI_MIN_SCORE
        # renders (moment re-centred on Gemini's window); below it the
        # loudness trigger is overridden to skip — a loud-but-boring
        # segment must NOT become a clip. No key / API failure -> the
        # loudness trigger stands (logged once) so the loop never blocks.
        # Segments with < 10 words have nothing to judge, so Gemini is
        # skipped there and loudness stands (a loud pure-game moment can
        # still clip; the hourly cap still applies).
        gemini = None
        if not force_clip and trigger_name is None and _gemini_pick is not None \
                and why.get("words", 0) >= 10:
            # No key needed here — score_moment calls Gemini through the
            # already-connected custom.gemini connector helper. If the
            # helper fails it returns None and the loudness trigger stands.
            gemini = _gemini_pick.score_moment(tp, ep, clip_len, pad)
            if gemini is not None:
                    gs = float(gemini.get("score") or 0)
                    gcat = gemini.get("category") or "moment"
                    if gs >= GEMINI_MIN_SCORE:
                        trigger = True
                        trigger_name = f"gemini: {gcat}"
                        gs0, ge0 = gemini.get("start"), gemini.get("end")
                        if gs0 is not None and ge0 is not None and ge0 > gs0:
                            dur = float(segment_minutes * 60)
                            c = (float(gs0) + float(ge0)) / 2
                            # variable length (40s floor, 60s cap) around
                            # Gemini's moment centre
                            s0, e0 = moment_window(tp, c, min_len=clip_len)
                            e0 = min(dur, e0)
                            moment = (s0, e0, why)
                        ev(f"[live] {channel}: gemini {gs:.0f}/10 ({gcat}) "
                           f"— {gemini.get('reason') or ''}")
                    else:
                        trigger = False
                        ev(f"[live] {channel}: gemini {gs:.0f}/10 "
                           f"({gcat}) — below {GEMINI_MIN_SCORE:.0f}, "
                           f"loudness trigger off (loud was {loud} dB)")
        # Gemini score for the hype-burst trigger below (None = no Gemini
        # verdict this segment; the burst can still fire on its own when
        # it is very strong).
        gemini_score = float(gemini.get("score") or 0) if gemini else None
        # chat-burst TRIGGER (see _chat_burst_trigger): the crowd itself
        # is a moment. Only fires when loudness did NOT trigger and no
        # manual mark / "clip that" won.
        chat_burst = False
        chat_unique = 0
        if tracker is not None and trigger_name is None and not trigger:
            got = _chat_burst_trigger(tracker, seg_start_ts,
                                      segment_minutes, tp, clip_len)
            if got is not None:
                s0, chat_unique, _score = got
                # variable length (40s floor, 60s cap) around the burst
                # centre (the trigger's s0 was burst_c - clip_len/2)
                s0, e0 = moment_window(tp, s0 + clip_len / 2,
                                       min_len=clip_len)
                moment = (s0, min(segment_minutes * 60.0, e0), why)
                trigger = True
                trigger_name = "chat burst"
                chat_burst = True
                ev(f"[live] {channel}: chat burst ({chat_unique} people, "
                   f"score {_score:.2f}) at {s0 + clip_len / 2:.0f}s -> CLIP")
        # HYPE-burst TRIGGER (2026-11-19): a sudden chat SPIKE. Fires
        # when Gemini scored decently (>= 5) OR the burst alone is very
        # strong — even when Gemini vetoed a loud-but-boring segment or
        # loudness never triggered. Only when no other trigger won.
        hype_burst = False
        hype_ratio = 0.0
        if tracker is not None and trigger_name is None and not trigger:
            got = _hype_burst_trigger(tracker, seg_start_ts,
                                      segment_minutes, tp, clip_len,
                                      gemini_score)
            if got is not None:
                s0, chat_unique, hype_score_v, _strength = got
                # variable length (40s floor, 60s cap) around the burst
                # centre (the trigger's s0 was burst_c - clip_len/2)
                s0, e0 = moment_window(tp, s0 + clip_len / 2,
                                       min_len=clip_len)
                moment = (s0, min(segment_minutes * 60.0, e0), why)
                trigger = True
                trigger_name = "hype burst"
                chat_burst = True
                hype_burst = True
                hype_ratio = hype_score_v
                ev(f"[live] {channel}: HYPE burst ({chat_unique} people, "
                   f"score {hype_score_v:.1f}) at {s0 + clip_len / 2:.0f}s "
                   f"-> CLIP")
        # hourly auto cap: at most AUTO_CAP_PER_HOUR auto clips per hour
        # across all channels (quality over quantity — 1-2/hour is fine).
        # Manual marks, "clip that", force-clip and website requests are
        # user-initiated and are never capped.
        is_auto = bool(trigger) and not force_clip and trigger_name not in (
            "manual mark", "clip that")
        if is_auto and not _auto_cap_ok():
            trigger = False
            ev(f"[live] {channel}: auto cap reached "
               f"({AUTO_CAP_PER_HOUR}/hour) — skipping this moment")
        ev(f"[live] {channel}: loudest {loud} dB, words={why['words']} -> "
           f"{'CLIP' if trigger else 'skip'}")
        if not trigger:
            return None
        # chat-burst adjustment: if the crowd peaked somewhere else and
        # there is real speech there, re-centre the clip on the burst
        # (but never override a manual mark, a "clip that", or a chat
        # burst that already centred the moment)
        if tracker is not None and trigger_name is None:
            try:
                chat_unique = tracker.unique_chatters(
                    seg_start_ts, seg_start_ts + segment_minutes * 60)
                ns, ne, used, top_n = _adjust_for_chat(
                    tp, _s, _e, tracker, seg_start_ts, segment_minutes * 60)
                if used:
                    moment = (ns, ne, why)
                    chat_burst = True
                    chat_unique = top_n
                    ev(f"[live] {channel}: chat burst ({top_n} people) "
                       f"-> clip re-centred to {ns:.1f}-{ne:.1f}s")
            except Exception as e:
                ev(f"[live] {channel}: chat check failed ({e}) — loudness only")
            _set_status(channel, chat_unique=chat_unique)
        if on_stage:
            try: on_stage("rendering", 65)
            except Exception: pass
        clip_path, info = process_segment(
            seg_path, out_dir, style, mode, clip_len, pad,
            transcript_path=tp, energy_path=ep, channel=channel,
            language=language, caption_mode=caption_mode,
            roman_mode=roman_mode, res=res, moment=moment,
            ratio=ratio, facecam_pos=facecam_pos, caption_pos=caption_pos,
            facecam_size=facecam_size, facecam_band=facecam_band,
            facecam_band_w=facecam_band_w, frame_style=frame_style,
            trigger_name=trigger_name, game_name=game_name,
            game_tags=game_tags)
        if chat_burst:
            info["chat_burst"] = True
            info["chat_unique"] = chat_unique
            info["trigger"] = trigger_name or "chat burst"
            if hype_burst:
                info["hype_burst"] = True
                info["hype_ratio"] = hype_ratio
            try:
                sc = json.load(open(clip_path + ".info.json"))
                sc["trigger"] = trigger_name or "chat burst"
                sig = (f"hype burst ({chat_unique} people, "
                       f"score {hype_ratio:.1f})" if hype_burst else
                       f"chat burst ({chat_unique} people)")
                sc["signals"].append(sig)
                json.dump(sc, open(clip_path + ".info.json", "w"), indent=1)
            except Exception:
                pass
        if clip_path:
            with WATCH_LOCK:
                WATCH_STATUS[channel]["clips"] += 1
            _set_status(channel, last_clip=os.path.basename(clip_path))
            if gemini is not None:
                info["gemini"] = gemini
                try:
                    sc = json.load(open(clip_path + ".info.json"))
                    sc["signals"].append(
                        f"gemini {float(gemini.get('score') or 0):.0f}/10 "
                        f"{gemini.get('category') or ''}")
                    json.dump(sc, open(clip_path + ".info.json", "w"), indent=1)
                except Exception:
                    pass
            if is_auto:
                _auto_cap_record()
            if queue is not None and enqueue_clip is not None:
                enqueue_clip(out_dir, clip_path, channel, info,
                             game_name=game_name, game_tags=game_tags)
            if on_clip:
                on_clip(clip_path, info)
            # auto-publish: auto clips go to the Gallery (Pages upload +
            # bridge /clips) exactly like manual request clips. Runs ONLY
            # on the round-robin auto path (auto_publish=True); manual
            # requests already publish via their own on_clip.
            if auto_publish and _bridge is not None:
                try:
                    url = _bridge.publish_clip(
                        clip_path, {"channel": channel,
                                    "style": info.get("style")}, info)
                    if url:
                        ev(f"[auto] published to Gallery: {url}")
                    else:
                        ev("[auto] publish FAILED — clip kept locally at "
                           f"{clip_path}")
                except Exception as e:
                    ev(f"[auto] publish error ({e}) — clip kept locally")
            return clip_path
    except Exception as e:
        ev(f"[live] segment processing failed for {channel}: {e}")
    finally:
        prune_segments(buffer_dir, keep_segments)
    return None


def _channel_settings(ch, defaults):
    """A channel entry is a URL string, or a dict with per-channel
    overrides: {"url": ..., "language": "hi", "caption_mode": "hi-roman",
    "mode": "gameplay", "style": "neon", "game": "GTA 6"}. Dict values win
    over the global defaults (dashboard language picker = global default).
    "game" pins the game for this channel (games.detect resolves it);
    without it the game is detected from the stream title."""
    if isinstance(ch, dict):
        url = ch.get("url") or ch.get("channel")
        out = dict(defaults)
        for k in ("language", "caption_mode", "roman_mode", "mode", "style",
                  "res", "ratio", "facecam_pos", "caption_pos", "game",
                  "frame_style"):
            if ch.get(k):
                out[k] = ch[k]
        return url, out
    return ch, dict(defaults)


def _valid_choice(value, allowed, default):
    return value if value in allowed else default


def _process_vod_request(req, out_dir, ev):
    """Process ONE website VOD request: a PUBLISHED video -> transcript ->
    best moments -> vertical Shorts, published to the Gallery like a live
    clip. The source video is working material only (process_vod deletes
    it after rendering). Returns the list of clip paths, or None on
    failure (request marked failed)."""
    req_id = req.get("id") or ""
    source = req.get("stream_url") or ""
    favorites = [str(f) for f in (req.get("favorites") or []) if f]
    style = str(req.get("style") or "")
    if style in (None, "", "auto"):
        style = favorites[0] if favorites else DEFAULT_STYLE
    style = _valid_choice(style, list(STYLES) + ["auto"], DEFAULT_STYLE)
    facecam_pos = _valid_choice(
        str(req.get("facecam_pos") or "auto"),
        ["auto", "top-right", "top-left", "mid-right", "mid-left",
         "bottom-left", "bottom-right", "top-band", "bottom-band",
         "stage", "stage-nofacecam", "stage-topleft",
         "stage-topright", "stage-side", "off"],
        "auto")
    caption_pos = _valid_choice(
        str(req.get("caption_pos") or "auto"),
        ["auto", "bottom", "seam", "top", "stage-bottom",
         "stage-top", "stage-centre", "stage-upper", "stage-lower"], "auto")
    ratio = _valid_choice(str(req.get("ratio") or "9:16"),
                          ["9:16", "1:1", "4:5", "16:9"], "9:16")
    try:
        res = int(req.get("resolution") or 720)
    except Exception:
        res = 720
    res = res if res in (720, 1080) else 720
    mode = _valid_choice(str(req.get("mode") or "gameplay"),
                         ["game", "gameplay", "facecam", "doc"], "gameplay")
    language = str(req.get("language") or "auto")
    caption_mode = str(req.get("caption_mode") or "auto")
    try:
        count = int(req.get("count") or 3)
    except Exception:
        count = 3
    count = count if 1 <= count <= 5 else 3
    # video title/channel for the Gallery card (best effort)
    if not req.get("channel"):
        try:
            _t, uploader = get_stream_title(source)
            if uploader:
                req["channel"] = uploader
        except Exception:
            pass
    ev(f"[vod] {req_id[:8]} {source}: style={style} mode={mode} "
       f"count={count} ratio={ratio} res={res} lang={language}")
    if _bridge is not None:
        _bridge.post_req_update(req_id, status="processing",
                                stage="transcribing", progress=10)
    vod_err = ""
    try:
        from vod import process_vod
        clips = process_vod(
            source, out_dir, style=style, mode=mode, res=res,
            clip_len=40.0, count=count, language=language,
            caption_mode=caption_mode, roman_mode=DEFAULT_ROMAN_MODE,
            api_key=None, ratio=ratio, facecam_pos=facecam_pos,
            caption_pos=caption_pos)
    except Exception as e:
        ev(f"[vod] {req_id[:8]} failed: {e}")
        clips = []
        vod_err = str(e)[:200]
    if not clips:
        if _bridge is not None:
            _bridge.post_req_update(req_id, status="failed",
                                    stage="failed",
                                    error=vod_err or "no moments found in this video")
            _bridge.mark_inbox(req_id, "failed")
        ev(f"[vod] {req_id[:8]} failed — no clips made")
        return None
    published = 0
    for clip_path in clips:
        info = {}
        try:
            with open(clip_path + ".info.json") as f:
                info = json.load(f)
        except Exception:
            pass
        if _bridge is not None:
            url = _bridge.publish_clip(clip_path, req, info)
            if url:
                published += 1
                ev(f"[vod] {req_id[:8]} ready: {url}")
            else:
                ev(f"[vod] {req_id[:8]} clip saved locally but publish "
                   f"FAILED — file kept at {clip_path}")
    if _bridge is not None:
        if published:
            _bridge.mark_inbox(req_id, "done")
        else:
            _bridge.post_req_update(
                req_id, stage="rendering", progress=95,
                error="upload failed — clips are on the VM, retry pending")
            _bridge.mark_inbox(req_id, "failed")
            return None
    return clips


def _process_website_request(req, out_dir, segment_minutes, threshold_db,
                            clip_len, pad, keep_segments, ev,
                            game_info=None):
    """Process ONE website clip request: capture its channel with the
    request's OWN settings (style, facecam_pos, caption_pos, ratio,
    resolution, language, caption_mode, mode) — never the loop's CLI
    defaults — force a clip from the best moment, publish it to the
    website Gallery (direct video) and mark the request ready.
    Returns the clip path, or None on failure (request marked failed)."""
    # VOD jobs (kind "vod", from the website's Published Video box) go
    # to process_vod instead of live capture.
    if str(req.get("kind") or "live") == "vod":
        return _process_vod_request(req, out_dir, ev)
    req_id = req.get("id") or ""
    channel = req.get("stream_url") or ""
    favorites = [str(f) for f in (req.get("favorites") or []) if f]
    style = str(req.get("style") or "")
    if style in (None, "", "auto"):
        style = favorites[0] if favorites else DEFAULT_STYLE
    style = _valid_choice(style, list(STYLES) + ["auto"], DEFAULT_STYLE)
    facecam_pos = _valid_choice(
        str(req.get("facecam_pos") or "auto"),
        ["auto", "top-right", "top-left", "mid-right", "mid-left",
         "bottom-left", "bottom-right", "top-band", "bottom-band",
         "stage", "stage-nofacecam", "stage-topleft",
         "stage-topright", "stage-side", "off"],
        "auto")
    facecam_size = _valid_choice(
        str(req.get("facecam_size") or "medium"),
        ["small", "medium", "large"], "medium")
    try:
        facecam_band = int(req.get("facecam_band") or 50)
    except Exception:
        facecam_band = 50
    facecam_band = facecam_band if facecam_band in (30, 50) else 50
    try:
        facecam_band_w = int(req.get("band_width") or 100)
    except Exception:
        facecam_band_w = 100
    facecam_band_w = facecam_band_w if facecam_band_w in (100, 75, 50) else 100
    caption_pos = _valid_choice(
        str(req.get("caption_pos") or "auto"),
        ["auto", "bottom", "seam", "top", "stage-bottom",
         "stage-top", "stage-centre", "stage-upper", "stage-lower"], "auto")
    frame_style = _valid_choice(
        str(req.get("frame_style") or "gold"),
        list(FRAME_STYLES), "gold")
    ratio = _valid_choice(str(req.get("ratio") or "9:16"),
                          ["9:16", "1:1", "4:5", "16:9"], "9:16")
    try:
        res = int(req.get("resolution") or 720)
    except Exception:
        res = 720
    res = res if res in (720, 1080) else 720
    mode = _valid_choice(str(req.get("mode") or "gameplay"),
                         ["game", "gameplay", "facecam", "doc"], "gameplay")
    language = str(req.get("language") or "auto")
    caption_mode = str(req.get("caption_mode") or "auto")
    gname, gtags = (game_info or {}).get(channel, (None, []))
    ev(f"[req] {req_id[:8]} {req.get('channel') or channel}: "
       f"style={style} facecam={facecam_pos} size={facecam_size} "
       f"band={facecam_band} captions={caption_pos} frame={frame_style} "
       f"ratio={ratio} res={res} lang={language} mode={mode}")
    if _bridge is not None:
        _bridge.post_req_update(req_id, status="processing",
                                stage="capturing", progress=5)

    def on_stage(stage, pct):
        if _bridge is not None:
            _bridge.post_req_update(req_id, stage=stage, progress=pct)

    def on_clip(clip_path, info):
        # publish: Pages upload + gallery post + request ready
        if _bridge is not None:
            url = _bridge.publish_clip(clip_path, req, info)
            if url:
                ev(f"[req] {req_id[:8]} ready: {url}")
            else:
                ev(f"[req] {req_id[:8]} clip saved locally but publish "
                   f"FAILED — file kept at {clip_path}")
                _bridge.post_req_update(
                    req_id, stage="rendering", progress=95,
                    error="upload failed — clip is on the VM, retry pending")
                _bridge.mark_inbox(req_id, "failed")

    try:
        clip_path = _watch_one_segment(
            channel, out_dir, style, mode, segment_minutes,
            threshold_db, clip_len, pad, keep_segments,
            True, on_clip, ev, None,
            language=language, caption_mode=caption_mode,
            roman_mode=DEFAULT_ROMAN_MODE, res=res,
            ratio=ratio, facecam_pos=facecam_pos, caption_pos=caption_pos,
            facecam_size=facecam_size, facecam_band=facecam_band,
            facecam_band_w=facecam_band_w, frame_style=frame_style,
            mark_times=None, game_name=gname, game_tags=gtags,
            on_stage=on_stage)
    except Exception as e:
        ev(f"[req] {req_id[:8]} failed: {e}")
        clip_path = None
    if not clip_path:
        if _bridge is not None:
            _bridge.post_req_update(req_id, status="failed",
                                    stage="failed",
                                    error="stream offline or no moment")
            _bridge.mark_inbox(req_id, "failed")
        ev(f"[req] {req_id[:8]} failed — no clip made")
        return None
    if _bridge is not None:
        _bridge.mark_inbox(req_id, "done")
    return clip_path


def run_loop(channels, out_dir, style=DEFAULT_STYLE, mode=DEFAULT_MODE,
             segment_minutes=DEFAULT_SEGMENT_MINUTES,
             watch_minutes=0, threshold_db=DEFAULT_THRESHOLD_DB,
             clip_len=DEFAULT_CLIP_LEN, pad=DEFAULT_PAD,
             keep_segments=DEFAULT_KEEP_SEGMENTS,
             stop_event=None, on_clip=None, on_event=None, force_clip=False,
             once=False, post=False, post_spacing_min=DEFAULT_POST_SPACING_MIN,
             daily_cap=DEFAULT_DAILY_CAP, force_event=None,
             language=DEFAULT_LANGUAGE, caption_mode=DEFAULT_CAPTION_MODE,
             roman_mode=DEFAULT_ROMAN_MODE, res=720,
             ratio="9:16", facecam_pos="auto", caption_pos="auto",
             frame_style=DEFAULT_FRAME_STYLE):
    """24/7 watch loop over a channel list (round-robin: one segment per
    channel per round). `channels` is a list of Twitch URLs; a single
    channel is just a one-element list. Runs until stop_event is set,
    watch_minutes elapse, or (once=True) one round is done.
    Returns list of clip paths made."""
    if isinstance(channels, str):
        channels = [channels]
    stop_event = stop_event or threading.Event()
    os.makedirs(out_dir, exist_ok=True)
    for ch in channels:
        _set_status(ch, live=None, last_event="watch starting")
    queue = None
    if post:
        if start_queue_worker is None:
            print("[post] WARNING: google-api client not installed — clips will "
                  "be queued but NOT uploaded. pip install -r requirements.txt",
                  flush=True)
        queue = {"spacing_min": post_spacing_min, "daily_cap": daily_cap}
        if start_queue_worker is not None:
            start_queue_worker(out_dir, stop_event, post_spacing_min, daily_cap)
            print(f"[post] queue worker started (spacing {post_spacing_min}min, "
                  f"daily cap {daily_cap})", flush=True)
    clips = []
    t_start = time.time()
    # crash recovery: a website request left 'processing' when the loop
    # died goes back to 'pending' so it is retried on this start.
    if _bridge is not None:
        _bridge.reset_stuck_processing()
    defaults = {"style": style, "mode": mode, "language": language,
                "caption_mode": caption_mode, "roman_mode": roman_mode,
                "res": res, "ratio": ratio, "facecam_pos": facecam_pos,
                "caption_pos": caption_pos, "frame_style": frame_style}

    def ev(msg):
        print(msg, flush=True)
        if on_event:
            try:
                on_event(msg)
            except Exception:
                pass

    ev(f"[live] watching {len(channels)} channel(s) {channels} | style={style} "
       f"mode={mode} language={language} caption_mode={caption_mode} "
       f"segment={segment_minutes}min threshold={threshold_db}dB "
       f"out={out_dir} post={'on' if post else 'off'}")
    # live-chat trackers (Twitch IRC + YouTube yt-dlp) start once per
    # channel, shared across rounds; the ChatManager picks the right
    # tracker per URL (None for anything else).
    for ch in channels:
        url = (ch.get("url") or ch.get("channel")) if isinstance(ch, dict) else ch
        CHAT.track(url)
    # game detection, once per channel (ClipKontrast games.py): a pinned
    # "game" override in channels.json wins; else the stream title is
    # fetched once (~2-5s per channel) and detected from it. Without a
    # title the game stays unknown and the GTA defaults apply everywhere.
    game_info = {}
    for ch in channels:
        url = (ch.get("url") or ch.get("channel")) if isinstance(ch, dict) else ch
        _u, cfg0 = _channel_settings(ch, defaults)
        override = cfg0.get("game")
        title, uploader = "", ""
        if not override:
            try:
                title, uploader = get_stream_title(url)
            except Exception as e:
                ev(f"[live] {url}: could not fetch stream title ({e}) — GTA defaults")
        gname, gtags = games.detect(override, title, uploader, url.split("/")[-1])
        game_info[url] = (gname, gtags)
        if gname:
            ev(f"[live] {url}: game detected — {gname} (tags: {', '.join(gtags)})")
    # force_event lets the dashboard's "Clip now" button force the next
    # segment of every channel to render a clip.
    force = force_event if force_event is not None else threading.Event()
    if force_clip:
        force.set()
    try:
        while not stop_event.is_set():
            if watch_minutes and (time.time() - t_start) > watch_minutes * 60:
                ev("[live] watch time reached — stopping")
                break
            # website requests first: sequential queue, one at a time,
            # each with its own settings. The round-robin over the
            # watched channels only runs while the queue is empty.
            if _bridge is not None:
                req = _bridge.take_oldest_pending()
                if req:
                    _process_website_request(
                        req, out_dir, segment_minutes, threshold_db,
                        clip_len, pad, keep_segments, ev,
                        game_info=game_info)
                    continue
            for ch in channels:
                if stop_event.is_set():
                    break
                url, cfg = _channel_settings(ch, defaults)
                do_force = force.is_set()
                if do_force:
                    force.clear()
                marks = take_marks(out_dir, url)
                gname, gtags = game_info.get(url, (None, []))
                c = _watch_one_segment(url, out_dir, cfg["style"], cfg["mode"],
                                       segment_minutes,
                                       threshold_db, clip_len, pad, keep_segments,
                                       do_force, on_clip, on_event, queue,
                                       language=cfg["language"],
                                       caption_mode=cfg["caption_mode"],
                                       roman_mode=cfg["roman_mode"],
                                       res=cfg["res"],
                                       ratio=cfg["ratio"],
                                       facecam_pos=cfg["facecam_pos"],
                                       caption_pos=cfg["caption_pos"],
                                       frame_style=cfg.get("frame_style", DEFAULT_FRAME_STYLE),
                                       mark_times=marks or None,
                                       game_name=gname, game_tags=gtags,
                                       auto_publish=True)
                if c:
                    clips.append(c)
            if once:
                break
            # small breather between rounds so a short offline check doesn't spin
            stop_event.wait(5)
    finally:
        CHAT.stop_all()
    ev(f"[live] stopped — {len(clips)} clip(s) made")
    return clips


def load_channels(path):
    """channels.json: {"channels": ["https://www.twitch.tv/...", ...]} or a
    plain list. Entries may be URL strings or dicts with per-channel
    overrides (see _channel_settings). Returns the list."""
    d = json.load(open(path))
    if isinstance(d, dict):
        return d.get("channels", [])
    return d


def main():
    # Frozen exe: put the bundle dir (ffmpeg/ffprobe/yt-dlp.exe) on PATH
    from ytdlp_cmd import ensure_frozen_binaries_on_path
    ensure_frozen_binaries_on_path()
    ap = argparse.ArgumentParser(description="GTA 6 Live Clipper — clip live Twitch streams into Shorts, 24/7.")
    ap.add_argument("channel", nargs="?", help="Twitch channel URL, e.g. https://www.twitch.tv/deansocool")
    ap.add_argument("--channels", default=None, help="channels.json file with a channel list")
    ap.add_argument("--out", default="clips", help="output dir for finished Shorts")
    ap.add_argument("--style", default=DEFAULT_STYLE, choices=list(STYLES) + ["auto"],
                    help="caption style (default: neon; auto = rotate through dashboard favorites)")
    ap.add_argument("--mode", default=DEFAULT_MODE, choices=["game", "gameplay", "facecam", "doc"],
                    help="crop mode (default: game = centre crop; gameplay = game + facecam corner)")
    ap.add_argument("--lang", default=DEFAULT_LANGUAGE,
                    help="transcription language: auto (detect, default) | en | hi | ur")
    ap.add_argument("--caption-mode", default=DEFAULT_CAPTION_MODE,
                    help="caption script: auto (Roman for Hindi/Urdu, default) | en | hi | hi-roman | ur | ur-roman")
    ap.add_argument("--roman-mode", default=DEFAULT_ROMAN_MODE,
                    help="transliteration engine: local (default) | gemini (needs key in config.json)")
    ap.add_argument("--res", type=int, default=720, choices=[720, 1080],
                    help="output resolution: 720 (default, fast) | 1080 (quality)")
    ap.add_argument("--ratio", default="9:16",
                    choices=["9:16", "1:1", "4:5", "16:9"],
                    help="output ratio: 9:16 (default, Shorts) | 1:1 | 4:5 | 16:9")
    ap.add_argument("--facecam-pos", default="auto",
                    choices=["auto", "top-right", "top-left", "mid-right",
                             "mid-left", "top-band", "off", "stage",
                             "stage-nofacecam", "stage-topleft",
                             "stage-topright", "stage-side"],
                    help="facecam position (gameplay mode): auto (default) | corner | top-band | off | stage (facecam top-centre + big game) | stage-nofacecam (stage look, no facecam) | stage-topleft / stage-topright (stage, facecam top-left/right) | stage-side (stage, facecam mid-left, full-frame game)")
    ap.add_argument("--caption-pos", default="auto",
                    choices=["auto", "bottom", "seam", "top", "stage-bottom",
                    "stage-top", "stage-centre", "stage-upper", "stage-lower"],
                    help="caption position: auto (default) | bottom | seam | top | stage-bottom (stage, bottom blurred zone) | stage-top / stage-centre / stage-upper / stage-lower (stage, blurred strip at that height)")
    ap.add_argument("--frame-style", default="gold", choices=list(FRAME_STYLES),
                    help="stage-layout facecam frame design (default: gold — the original golden comet; red = PeakCut hype red comet; line = cyan line sweep; fire = orange fire ember; static = clean still border; pulse = violet breathing ring)")
    ap.add_argument("--segment-minutes", type=int, default=DEFAULT_SEGMENT_MINUTES)
    ap.add_argument("--watch-minutes", type=int, default=0,
                    help="stop after N minutes (0 = run until Ctrl+C)")
    ap.add_argument("--threshold-db", type=float, default=DEFAULT_THRESHOLD_DB,
                    help="loudest 5s window at/above this dB triggers a clip")
    ap.add_argument("--clip-len", type=float, default=DEFAULT_CLIP_LEN)
    ap.add_argument("--pad", type=float, default=DEFAULT_PAD,
                    help="seconds of lead-in before the loud moment")
    ap.add_argument("--keep-segments", type=int, default=DEFAULT_KEEP_SEGMENTS)
    ap.add_argument("--clip-now", action="store_true",
                    help="clip the best moment from every segment (ignore threshold)")
    ap.add_argument("--once", action="store_true",
                    help="one round over the channels, then exit")
    ap.add_argument("--post", action="store_true",
                    help="enable the auto-post queue (YouTube; needs OAuth setup, see README)")
    ap.add_argument("--post-spacing-min", type=int, default=DEFAULT_POST_SPACING_MIN)
    ap.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    a = ap.parse_args()
    if a.channels:
        channels = load_channels(a.channels)
    elif a.channel:
        channels = [a.channel]
    else:
        ap.error("give a channel URL or --channels channels.json")
    if not channels:
        ap.error("no channels given")
    try:
        run_loop(channels, a.out, a.style, a.mode, a.segment_minutes,
                 a.watch_minutes, a.threshold_db, a.clip_len, a.pad,
                 a.keep_segments, force_clip=a.clip_now, once=a.once,
                 post=a.post, post_spacing_min=a.post_spacing_min,
                 daily_cap=a.daily_cap, language=a.lang,
                 caption_mode=a.caption_mode, roman_mode=a.roman_mode,
                 res=a.res, ratio=a.ratio, facecam_pos=a.facecam_pos,
                 caption_pos=a.caption_pos, frame_style=a.frame_style)
    except KeyboardInterrupt:
        print("\nstopped by user")


if __name__ == "__main__":
    main()
