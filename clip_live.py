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
from render_short import render_short  # noqa: E402
from transcribe import pick_moment, transcribe_segment  # noqa: E402
from captions import STYLES  # noqa: E402
from settings import next_favorite_style  # noqa: E402
from chat import ChatManager, chat_burst_windows  # noqa: E402
# Game detection (ClipKontrast games.py port) + stream titles for it.
import games  # noqa: E402
from stream_discovery import get_stream_title  # noqa: E402

try:
    from post_queue import enqueue_clip, start_queue_worker  # noqa: E402
except Exception:  # google-api client not installed -> posting disabled
    enqueue_clip = None
    start_queue_worker = None

DEFAULT_STYLE = "neon"
DEFAULT_MODE = "game"
DEFAULT_LANGUAGE = "auto"   # auto-detect (Whisper); en | hi | ur to force
DEFAULT_CAPTION_MODE = "auto"  # auto | en | hi | hi-roman | ur | ur-roman
DEFAULT_ROMAN_MODE = "local"   # local | gemini (gemini needs key in config.json)
DEFAULT_SEGMENT_MINUTES = 3
DEFAULT_THRESHOLD_DB = -28.0
DEFAULT_CLIP_LEN = 40.0
DEFAULT_PAD = 8.0
DEFAULT_KEEP_SEGMENTS = 4
DEFAULT_POST_SPACING_MIN = 30
DEFAULT_DAILY_CAP = 10

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


def process_segment(seg_path, out_dir, style=DEFAULT_STYLE, mode=DEFAULT_MODE,
                    clip_len=DEFAULT_CLIP_LEN, pad=DEFAULT_PAD,
                    prefix="gta6_short", transcript_path=None, energy_path=None,
                    channel=None, language=DEFAULT_LANGUAGE,
                    caption_mode=DEFAULT_CAPTION_MODE,
                    roman_mode=DEFAULT_ROMAN_MODE, res=720, moment=None,
                    ratio="9:16", facecam_pos="auto", caption_pos="auto",
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
            "caption_pos": caption_pos, "game_name": game_name,
            "game_tags": game_tags or [],
            "trigger": trigger_name or style_src}
    if why.get("words", 0) < 3 and (why.get("loud_window") or {}).get("db", -99) < -35:
        print(f"[clip] segment has no usable moment "
              f"(words={why.get('words', 0)}, loud={info['moment']['loud_db']} dB) — skipping")
        return None, info
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    clip_path = os.path.join(out_dir, f"{prefix}_{ts}.mp4")
    render_short(seg_path, start, end, tp, style, mode, clip_path, work_dir,
                 res=res, ratio=ratio, facecam_pos=facecam_pos,
                 caption_pos=caption_pos)
    info["clip"] = clip_path
    info["elapsed_s"] = round(time.time() - t0, 1)
    # gallery sidecar: style / trigger / source, read by the dashboard
    sidecar = {"style": style, "style_source": style_src, "kind": "live",
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


def _watch_one_segment(channel, out_dir, style, mode, segment_minutes,
                       threshold_db, clip_len, pad, keep_segments,
                       force_clip, on_clip, on_event, queue,
                       language=DEFAULT_LANGUAGE,
                       caption_mode=DEFAULT_CAPTION_MODE,
                       roman_mode=DEFAULT_ROMAN_MODE, res=720,
                       ratio="9:16", facecam_pos="auto", caption_pos="auto",
                       mark_times=None, game_name=None, game_tags=None):
    """One capture+decide+maybe-render cycle for ONE channel.
    Returns the clip path made, or None. Updates WATCH_STATUS."""
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
    # live chat (Twitch IRC, people not messages) — started once per channel
    tracker = CHAT.track(channel) if "twitch.tv" in channel.lower() else None
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
                    s0 = max(0.0, off - pad)
                    moment = (s0, min(segment_minutes * 60.0, s0 + clip_len), why)
                    trigger = True
                    trigger_name = "manual mark"
                    ev(f"[live] {channel}: manual mark at {off:.0f}s -> CLIP")
                    break
        if trigger_name is None:
            for ct in find_clip_that(tp):
                if 0 <= ct < segment_minutes * 60:
                    s0 = max(0.0, ct - pad)
                    moment = (s0, min(segment_minutes * 60.0, s0 + clip_len), why)
                    trigger = True
                    trigger_name = "clip that"
                    ev(f"[live] {channel}: streamer said 'clip that' "
                       f"at {ct:.0f}s -> CLIP")
                    break
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
                moment = (s0, min(segment_minutes * 60.0, s0 + clip_len), why)
                trigger = True
                trigger_name = "chat burst"
                chat_burst = True
                ev(f"[live] {channel}: chat burst ({chat_unique} people, "
                   f"score {_score:.2f}) at {s0 + clip_len / 2:.0f}s -> CLIP")
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
        clip_path, info = process_segment(
            seg_path, out_dir, style, mode, clip_len, pad,
            transcript_path=tp, energy_path=ep, channel=channel,
            language=language, caption_mode=caption_mode,
            roman_mode=roman_mode, res=res, moment=moment,
            ratio=ratio, facecam_pos=facecam_pos, caption_pos=caption_pos,
            trigger_name=trigger_name, game_name=game_name,
            game_tags=game_tags)
        if chat_burst:
            info["chat_burst"] = True
            info["chat_unique"] = chat_unique
            info["trigger"] = "chat burst"
            try:
                sc = json.load(open(clip_path + ".info.json"))
                sc["trigger"] = "chat burst"
                sc["signals"].append(f"chat burst ({chat_unique} people)")
                json.dump(sc, open(clip_path + ".info.json", "w"), indent=1)
            except Exception:
                pass
        if clip_path:
            with WATCH_LOCK:
                WATCH_STATUS[channel]["clips"] += 1
            _set_status(channel, last_clip=os.path.basename(clip_path))
            if queue is not None and enqueue_clip is not None:
                enqueue_clip(out_dir, clip_path, channel, info,
                             game_name=game_name, game_tags=game_tags)
            if on_clip:
                on_clip(clip_path, info)
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
                  "res", "ratio", "facecam_pos", "caption_pos", "game"):
            if ch.get(k):
                out[k] = ch[k]
        return url, out
    return ch, dict(defaults)


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
             ratio="9:16", facecam_pos="auto", caption_pos="auto"):
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
    defaults = {"style": style, "mode": mode, "language": language,
                "caption_mode": caption_mode, "roman_mode": roman_mode,
                "res": res, "ratio": ratio, "facecam_pos": facecam_pos,
                "caption_pos": caption_pos}

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
    # live-chat trackers (Twitch IRC) start once per channel, shared across
    # rounds; YouTube live chat needs an API key, so it is not tracked.
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
                                       mark_times=marks or None,
                                       game_name=gname, game_tags=gtags)
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
                             "mid-left", "top-band", "off"],
                    help="facecam position (gameplay mode): auto (default) | corner | top-band | off")
    ap.add_argument("--caption-pos", default="auto",
                    choices=["auto", "bottom", "seam", "top"],
                    help="caption position: auto (default) | bottom | seam | top")
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
                 caption_pos=a.caption_pos)
    except KeyboardInterrupt:
        print("\nstopped by user")


if __name__ == "__main__":
    main()
