"""Render one vertical Short from a capture — standalone step.

Pipeline (all steps proven on this VM, spikes 2026-11-17):
1. TIMESTAMP REGENERATION (LOAD-BEARING): raw HLS live captures have
   non-monotonic timestamps — NEVER edit the raw file. Extract the clip as
   an image sequence first (`fps=60` filter), then encode from images.
2. Audio is extracted separately, exactly clip-length (A/V sync verified
   by caption timing in the spikes).
3. Captions: Hormozi Pop / Glow Neon via captions.py (PIL PNG overlays ->
   one transparent caption-track video -> ONE ffmpeg overlay).
4. Crop: game mode = 608x1080 centre crop from 1920x1080 (1.18x upscale,
   the fix for the old 144p feel). facecam/doc modes use the V3 tracker
   + 60Hz sendcmd crop (see track_v3_stable.py / build_track60.py).
   gameplay mode (PeakCut-style, 2026-11-17): same centre-crop game
   base + the streamer's facecam as a 288x162 overlay (40% of width —
   BIGGER than PeakCut's 30%) in an auto-checked corner, captions at an
   auto-checked Y (see layout.py). Falls back to plain game mode when
   no facecam is found in the clip.
5. Encode: H.264 CRF 18, preset veryfast (~130s per 40s clip; `medium`
   at ~266s is too slow for the live loop), yuv420p, AAC 128k, +faststart.

FINAL build (2026-11-18): output RATIOS (9:16 default, 1:1, 4:5, 16:9 —
ClipzLab idea) and POSITION picks (sohaib chooses every facecam +
caption position on the Positions page — see positions.py). The
facecam-band layout (Clipie-style: full-width facecam band on top,
gameplay below) is --facecam-pos top-band.

Usage:
  python render_short.py --input capture.mp4 --start 90 --end 130 \
      --transcript transcript.json --style neon --mode game --out short.mp4
  python render_short.py --input capture.mp4 --start 0 --end 40 \
      --transcript transcript.json --style neon --mode gameplay --out short.mp4
  python render_short.py --input capture.mp4 --start 0 --end 40 \
      --transcript transcript.json --style hype --mode gameplay \
      --facecam-pos top-band --caption-pos seam --ratio 1:1 --out short.mp4
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from captions import STYLES, build_caption_track, CAP_Y  # noqa: E402
# CAP_Y lives in captions.py (single source of truth) — see the safe-zone
# comment there. Do not redefine it here.
from layout import choose_layout, detect_facecam, fallback_layout  # noqa: E402
# Static-border pre-check (ClipKontrast port): fast, no model — runs
# BEFORE detect_facecam() and skips the MediaPipe pass on a hit.
from layout_border import detect_facecam_border  # noqa: E402
from positions import (CAPTION_POSITIONS, FACECAM_POSITIONS, RATIOS,  # noqa: E402
                       game_crop, ratio_params)

FPS = 60
CROP_W, CROP_H = 608, 1080
SRC_W = 1920
OUT_W, OUT_H = 720, 1280
CRF = 18
PRESET = "veryfast"

# BLUR_OVERSCAN (2026-11-19, black-bars fix): the stage layout's blurred
# background is scaled BLUR_OVERSCAN px taller than the canvas on EACH
# side, blurred, then centre-cropped back to the canvas. Without this,
# stage renders show pure-black bands at the top (~12px, y0-12) and
# bottom (~64px, y1216-1280): gblur darkens the frame edges (it averages
# with black outside the frame) and the source itself is dark at its very
# top/bottom rows, so a blurred bg scaled to exactly the canvas fades to
# black at both edges. Overscan + centre-crop maps the visible edges to
# interior source rows (bottom maps to source y984-1032, mean ~44-50,
# instead of y1027-1080, mean ~1-14) and crops off the blur's darkened
# edge ring. 64px clears the ~42px blur edge zone (3*sigma, sigma=14)
# with margin. Applies to the stage and stage-nofacecam branches.
BLUR_OVERSCAN = 64

# Facecam FRAME designs for the stage layout (sohaib 2026-11-19: he asked
# for 5-6 designs in different colours/styles, selectable on the website
# alongside the existing layout categories). "gold" is the original
# (default) — a golden comet travelling around the rounded-rect frame,
# 4s per lap. Each design is a named, selectable frame_style:
# render_short(frame_style=...) / --frame-style / website request
# frame_style. Golden stays the default unless the user picks another.
# kind: comet = travelling blob with fading tail (gold/red/fire share the
# painter, colours + flicker differ) | line = a bright arc of uniform
# brightness sweeping around (no tail) | static = one still frame, no
# motion | pulse = the whole ring breathes in/out, nothing travels.
# frames: PNGs in one loop (240 = 4s/lap at 60fps; 120 = 2s breathe;
# 1 = still image looped). ring_alpha: base ring opacity (dim for the
# animated ones — the moving light is the star; bright for static, where
# the ring IS the design).
FRAME_STYLES = {
    "gold": {
        "label": "Golden Comet",
        "desc": "Golden light travelling around the frame — the original, calm 4s lap.",
        "kind": "comet", "frames": 240,
        "ring": (255, 200, 0), "ring_alpha": 70,
        "comet": (255, 190, 0), "core": (255, 240, 180),
    },
    "red": {
        "label": "Red Comet",
        "desc": "PeakCut-style hype red comet travelling around the frame, 4s lap.",
        "kind": "comet", "frames": 240,
        "ring": (255, 60, 60), "ring_alpha": 70,
        "comet": (255, 45, 45), "core": (255, 225, 210),
    },
    "line": {
        "label": "Cyan Line",
        "desc": "A bright cyan line sweeping around the frame — no comet blob, just the line, 4s lap.",
        "kind": "line", "frames": 240,
        "ring": (0, 220, 255), "ring_alpha": 60,
        "comet": (0, 220, 255), "core": (210, 248, 255),
    },
    "fire": {
        "label": "Fire Ember",
        "desc": "Orange fire ember with a strong flicker and a long tail travelling around the frame, 4s lap.",
        "kind": "fire", "frames": 240,
        "ring": (255, 140, 0), "ring_alpha": 70,
        "comet": (255, 110, 0), "core": (255, 225, 130),
    },
    "static": {
        "label": "Clean Static",
        "desc": "No motion at all — a clean thin white/silver border, the minimal look.",
        "kind": "static", "frames": 1,
        "ring": (232, 236, 240), "ring_alpha": 235,
        "comet": (232, 236, 240), "core": (255, 255, 255),
    },
    "pulse": {
        "label": "Violet Pulse",
        "desc": "A violet neon ring that breathes in and out — no travelling light, 2s cycle.",
        "kind": "pulse", "frames": 120,
        "ring": (170, 90, 255), "ring_alpha": 180,
        "comet": (170, 90, 255), "core": (225, 200, 255),
    },
    # --- NEW structurally-different styles (sohaib 2026-11-19 verdict:
    # the 6 colour frames only change the outline colour; the style is
    # the same box in all of them. These 5 change the SHAPE / structure,
    # not just the colour. The 6 colour frames stay exactly as they are.)
    "circle": {
        "label": "Circle",
        "desc": "Facecam cropped to a circle with a thin gold ring and a travelling light — no box at all, the round look.",
        "kind": "circle", "frames": 240,
        "ring": (255, 200, 0), "ring_alpha": 110,
        "comet": (255, 190, 0), "core": (255, 240, 180),
        # a circle needs a SQUARE facecam (180x180 at 720p, fits the
        # 188px top band); fc_y is the 720p top margin for it.
        "fc_size": (180, 180), "fc_y": 14,
    },
    "clean": {
        "label": "Clean (no frame)",
        "desc": "No border at all — just a soft drop shadow under the facecam, the minimal floating look.",
        "kind": "clean", "frames": 1,
        "ring": (0, 0, 0), "ring_alpha": 0,
        "comet": (0, 0, 0), "core": (255, 255, 255),
    },
    "bold": {
        "label": "Bold Chunky",
        "desc": "A much thicker chunky border (10px) instead of the thin line — the heavy frame look.",
        "kind": "static", "frames": 1,
        "ring": (255, 200, 0), "ring_alpha": 235,
        "comet": (255, 200, 0), "core": (255, 240, 180),
        "border": 10,  # override: chunky 10px border (standard is 4)
    },
    "corners": {
        "label": "Viewfinder Corners",
        "desc": "Viewfinder style — only the four corner brackets, no full border.",
        "kind": "corners", "frames": 1,
        "ring": (255, 200, 0), "ring_alpha": 235,
        "comet": (255, 200, 0), "core": (255, 240, 180),
    },
    "big": {
        "label": "Big Facecam",
        "desc": "A larger facecam (360x203) with a thin gold frame — the streamer front and centre.",
        "kind": "static", "frames": 1,
        "ring": (255, 200, 0), "ring_alpha": 200,
        "comet": (255, 200, 0), "core": (255, 240, 180),
        # larger facecam (360x203 at 720p); it floats over the top of
        # the game by ~29px — the game area is NOT moved.
        "fc_size": (360, 203),
    },
}
DEFAULT_FRAME_STYLE = "gold"

# Per-channel facecam source rect (1920x1080 source px) — used ONLY when
# both detectors miss the webcam. Measured from real frames 2026-11-19:
# IShowSpeed's webcam sits left-middle (0,445,410,255); Typical Gamer's
# left-middle higher up (16,370,364,250). Channels with no measured rect
# get no fallback (a wrong rect is worse than no facecam).
CHANNEL_FACECAM = {
    "qSyo5d-dVzA": (0, 445, 410, 255),    # IShowSpeed
    "3QjXNSf6keg": (16, 370, 364, 250),   # Typical Gamer (old stream id)
    "HzzzoomJ-Os": (13, 431, 430, 270),   # Typical Gamer (new stream id,
                                          # measured 2026-11-19 v3 fix)
    "OjGoc9yzFVM": (0, 388, 308, 300),   # RAJA PAKISTANI — mid-left
                                          # webcam, measured 2026-11-19
                                          # from frame edge detection
                                          # (right edge x308, top y388,
                                          # bottom y688).
}

# Twitch channels (keyed by lowercase channel name — Twitch segment files
# are named seg_<channel>__<ts>.mp4, no YouTube video id). Measured from
# real frames 2026-11-19 (v2 fix): caseoh_'s webcam is top-left.
CHANNEL_FACECAM_TWITCH = {
    "caseoh": (0, 130, 405, 310),   # caseoh_ — top-left webcam
}

# Per-channel A/V SYNC OFFSET (seconds) — LIP-SYNC FIX 2026-11-19.
# Root cause (measured, not guessed): the SOURCE live captures have the
# webcam VIDEO delayed relative to the AUDIO — a streamer-side sync issue
# (webcam/capture latency in OBS), NOT a render bug. The pipeline
# faithfully reproduces the source offset, so the lips lag the voice.
# DEFAULT_AV_OFFSET_S (2026-11-19, user-verified): +8.0s is the GLOBAL
# default A/V offset. The user confirmed +8.0s is PERFECT on IShowSpeed
# (+7s left 1s of lip lag, +8s matches exactly — voice and video match,
# captions match). File measurements show ~0s offset, but his playback
# consistently shows ~8s of lip lag, so the default is 8.0s for every
# video. A channel-specific entry in CHANNEL_AV_OFFSET_S still overrides
# this default, and an explicit --av-offset flag overrides everything.
DEFAULT_AV_OFFSET_S = 8.0

# Measured by word-locked mouth activity (mouth-motion peak vs Whisper
# word centre, 43-50 words, 4 independent measurements all ~0.42-0.43s):
#   Typical Gamer (HzzzoomJ-Os): video lags audio by ~0.43s
#   (v3 render +0.42s, test render +0.43s, JPEG frames +0.43s,
#    direct source extraction +0.43s — the source itself has it).
# Positive value = video lags audio by this many seconds. The fix shifts
# the VIDEO extraction window LATER by the offset (audio + captions stay
# on the original window, so they stay in sync with each other). Other
# channels default to 0.0 until measured — measure the same way before
# adding (word-locked mouth activity on a clean speech window).
CHANNEL_AV_OFFSET_S = {
    # Typical Gamer — RE-MEASURED 2026-11-19: the +0.43s here was a FALSE
    # POSITIVE from the word-locked frame-diff method (it peaks ~0.4s
    # after word centres on this facecam even when the lips are in sync
    # — coarticulation / the mouth keeps moving after words). Direct
    # visual check on the source frames proves offset ~0: the mouth is
    # PURSED for "do" (56.52-56.80) and OPEN for "that" (56.80-57.40)
    # exactly when the voice says them, and CLOSED during the 58.74-59.28
    # speech pause — all four frames contradict a +0.43s lag. The +0.43
    # shift made v4's lips LEAD the voice by 0.43s (user's report).
    # Offsets stay 0.0 until a channel is proven off by the same
    # direct mouth-open/closed-vs-speech check, not by frame-diff.
    "HzzzoomJ-Os": 0.0,   # Typical Gamer — measured ~0 (2026-11-19)
    "3QjXNSf6keg": 0.0,   # Typical Gamer (old stream id, same setup)
}


def av_offset_for(src):
    """A/V sync offset (seconds) for a source file, from the channel id
    in its filename (seg_watch?v=<id>_... or seg_<channel>__...).
    Returns DEFAULT_AV_OFFSET_S (8.0s) when the channel has no entry in
    CHANNEL_AV_OFFSET_S; a channel-specific entry overrides the default.
    An explicit --av-offset flag overrides this function entirely."""
    import re as _re
    m = _re.search(r"watch\?v=([A-Za-z0-9_-]{11})", src or "")
    if m and m.group(1) in CHANNEL_AV_OFFSET_S:
        return CHANNEL_AV_OFFSET_S[m.group(1)]
    m2 = _re.search(r"seg_([A-Za-z0-9_]+)__", src or "")
    if m2 and m2.group(1).lower() in CHANNEL_AV_OFFSET_S:
        return CHANNEL_AV_OFFSET_S[m2.group(1).lower()]
    return DEFAULT_AV_OFFSET_S


def detect_letterbox(frames_dir, src_h=1080, samples=8):
    """Detect cinematic letterbox bars (pure-black top/bottom margins) in
    the extracted source frames. GTA 6 cutscenes are letterboxed (~60px
    top + ~60px bottom of a 1920x1080 capture, ~2:1 content) and the
    stage filter faithfully reproduces those bars as black bands above
    and below the gameplay (sohaib's 2026-11-19 report on v4). Returns
    (y0, y1) content bounds in source coords, or (0, src_h) when no
    letterbox is found. Uses the median dark margin across sampled
    frames (robust to one dark frame); a <6.0 row-mean threshold
    separates a black/dark bar from visible game content (a true
    letterbox bar reads 0.0-1.0, a dark ceiling reads 1-5, visible game
    reads 10+ — so <6 catches both the letterbox AND the effectively-
    black ceiling that reads as a black bar to the viewer, while real
    game content stays). Fail-safe: any error -> (0, src_h)
    (no crop), so a bad detection can never break a render."""
    try:
        from PIL import Image
        import numpy as _np
    except Exception:
        return 0, src_h
    try:
        files = sorted(f for f in os.listdir(frames_dir)
                       if f.endswith(".jpg"))
    except Exception:
        return 0, src_h
    if not files:
        return 0, src_h
    n_samp = min(samples, len(files))
    idx = [int(i * (len(files) - 1) / max(n_samp - 1, 1))
           for i in range(n_samp)]
    tops, bots = [], []
    for i in idx:
        try:
            im = _np.asarray(
                Image.open(os.path.join(frames_dir, files[i])).convert("L"),
                dtype=_np.float32)
        except Exception:
            continue
        if im.shape[0] < src_h:
            continue
        rowmean = im.mean(axis=1)
        top = 0
        while top < src_h and rowmean[top] < 6.0:
            top += 1
        bot = 0
        while bot < src_h and rowmean[src_h - 1 - bot] < 6.0:
            bot += 1
        tops.append(top)
        bots.append(bot)
    if not tops:
        return 0, src_h
    # MAXIMUM across samples (not median/percentile): the dark ceiling
    # VARIES over a cutscene (60px at bright moments, ~230px at the
    # darkest), and the viewer watches from the start — any smaller
    # statistic under-crops the dark early part, leaving the black band
    # the user reported. The max guarantees the band is gone at every
    # sampled moment; the cost is cutting low-value ceiling/sky at
    # bright moments (the top of a 16:9 frame is the least important
    # part of a 9:16 vertical crop). The 200px cap + the half-frame
    # guard below stop a pathological dark scene from eating the frame.
    # The bottom bar is stable, so max barely changes it.
    top = int(max(tops))
    bot = int(max(bots))
    # ignore tiny margins (noise) and cap the crop so a pathological
    # dark scene can never eat the frame.
    top = top if top >= 8 else 0
    bot = bot if bot >= 8 else 0
    top = min(top, 200)
    bot = min(bot, 200)
    if top + bot >= src_h // 2:
        return 0, src_h
    return top, src_h - bot


def run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[render] {what} FAILED\n{r.stderr[-1500:]}", file=sys.stderr)
        raise RuntimeError(f"{what} failed")
    return r


def extract_frames(src, start, dur, frames_dir):
    # v3 fix (2026-11-19): do NOT use input `-ss` (before -i) on raw HLS
    # live captures — the fast keyframe seek duplicates the first frames
    # (measured on seg_watch?v=HzzzoomJ-Os_20261119-115254: input -ss
    # gave 3 identical frames at the head, mad 0.0, while decoding from
    # the start showed strong motion from frame 1). That duplicated head
    # is what made rendered clips play as a FROZEN picture for the first
    # 2-3s while the player time kept running (sohaib's report — and the
    # v2 audio fix already proved HLS timestamps are unreliable for
    # seeking). Output -ss (after -i) decodes sequentially and discards
    # up to `start`, so it is frame-accurate. Slower, but correct.
    os.makedirs(frames_dir, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error",
         "-i", src, "-ss", str(start), "-t", str(dur),
         "-vf", f"fps={FPS}", "-q:v", "2",
         os.path.join(frames_dir, "f_%04d.jpg")],
        "frame extraction")
    n = len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
    print(f"[render] extracted {n} frames @60fps (expected ~{int(dur * FPS)})")
    if n < dur * FPS * 0.8:
        raise RuntimeError(f"too few frames extracted ({n}) — bad capture?")
    return n


def extract_audio(src, start, dur, out_path):
    # v2 fix (2026-11-19, A/V SYNC): raw HLS live captures have
    # non-monotonic AUDIO timestamps — seeking the container (input -ss
    # or output -ss) lands ~12s early AND truncates the audio (measured
    # on seg_caseoh__20261119-110102: [27,39] gave 4s of audio from ~16s,
    # "That is the first..." instead of "Get him"). The video seek works
    # but the audio seek does not, so the voice drifted from the lips and
    # the captions. Fix: extract the FULL audio to a WAV first (sequential
    # decode, no seek — sample order is correct regardless of timestamps),
    # then seek within the clean WAV (WAV timestamps are monotonic, so
    # -ss is sample-accurate). This matches transcribe.py, which also
    # transcribes the full WAV — so the audio, the transcript, and the
    # captions all share one time base.
    tmp_wav = out_path + ".full.wav"
    run(["ffmpeg", "-y", "-v", "error",
         "-i", src, "-vn", "-ac", "1", "-ar", "16000", tmp_wav],
        "full audio extraction")
    run(["ffmpeg", "-y", "-v", "error",
         "-ss", str(start), "-i", tmp_wav, "-t", str(dur),
         "-c:a", "aac", "-b:a", "128k", out_path],
        "audio window extraction")
    try:
        os.remove(tmp_wav)
    except OSError:
        pass
    return out_path


def make_clean_clip(src, start, dur, out_path):
    """Re-encode a clean, timestamp-sane clip (needed as tracker input for
    facecam/doc modes — cv2 also chokes on broken live timestamps)."""
    run(["ffmpeg", "-y", "-v", "error",
         "-i", src, "-ss", str(start), "-t", str(dur),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-r", str(FPS), "-c:a", "aac", "-b:a", "128k", out_path],
        "clean clip for tracker")
    return out_path


def _video_codec_args(encoder, preset):
    """ffmpeg args for the video stream, by encoder.

    libx264 (default): software, preset from --preset (veryfast default).
    qsv: Intel Quick Sync — his PC (i5 8th gen + UHD 620). global_quality
    20 is QSV's ~CRF-18 ballpark. NOT testable on this VM (no Intel GPU:
    h264_qsv fails with 'Error creating a MFX session: -9'), so the
    caller falls back to libx264 automatically if the QSV run fails.
    See SPEED-REPORT.md for measured numbers.
    """
    if encoder == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast",
                "-global_quality", "20", "-g", "120"]
    if encoder != "libx264":
        raise ValueError(f"unknown encoder {encoder!r} (libx264|qsv)")
    return ["-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", preset, "-crf", str(CRF),
            "-g", "120"]  # keyframe every 2s at 60fps (2026-11-19 v3 fix:
                          # the default GOP 250 = ~4.2s made phone players
                          # resync slowly after a stall on a slow net)


def render_short(src, start, end, transcript_path, style="neon",
                 mode="game", out_path="short.mp4", work_dir=None,
                 fonts_dir=None, keep_work=False, script=None, res=720,
                 preset=PRESET, encoder="libx264", ratio="9:16",
                 facecam_pos="auto", caption_pos="auto",
                 facecam_size="medium", facecam_band=50, facecam_band_w=100,
                 frame_style="gold", av_offset_s=None):
    dur = end - start
    if dur <= 0:
        raise ValueError("end must be > start")
    if res not in (720, 1080):
        raise ValueError(f"res must be 720 or 1080, got {res!r}")
    if mode in ("facecam", "doc") and ratio != "9:16":
        # the tracker modes crop a fixed 608px window — ratios only
        # apply to game/gameplay (documented in FINAL-REPORT.md)
        raise ValueError(f"mode {mode!r} only supports ratio 9:16, got {ratio!r}")
    rp = ratio_params(ratio, res, facecam_size=facecam_size,
                      facecam_band=facecam_band, facecam_band_w=facecam_band_w)
    out_w, out_h = rp["out_w"], rp["out_h"]
    work_dir = work_dir or os.path.join(
        os.path.dirname(os.path.abspath(out_path)), "work_" + os.path.basename(out_path))
    os.makedirs(work_dir, exist_ok=True)
    fonts_dir = fonts_dir or os.path.join(BASE, "fonts")
    # caption script comes from the transcript (transcribe.py wrote it);
    # explicit arg wins. Default latin (old transcripts have no field).
    if script is None:
        try:
            script = json.load(open(transcript_path)).get("caption_script") or "latin"
        except Exception:
            script = "latin"
    t0 = time.time()
    # LIP-SYNC FIX (2026-11-19): the source video lags the audio by
    # av_offset_s (measured per channel, see CHANNEL_AV_OFFSET_S). The
    # VIDEO window is shifted LATER by the offset so the lips line up
    # with the voice; the AUDIO + CAPTIONS stay on the original window
    # [start, end] so they stay in sync with each other. (Delaying the
    # audio instead would also need the captions delayed and would leave
    # silence at the head; shifting the video is cleaner.)
    if av_offset_s is None:
        av_offset_s = av_offset_for(src)
    video_start = start + av_offset_s
    print(f"[render] {src} [{start:.1f}-{end:.1f}s] style={style} mode={mode} "
          f"script={script} ratio={ratio} res={res} "
          f"facecam={facecam_pos} captions={caption_pos} frame={frame_style} "
          f"av_offset={av_offset_s:.2f}s -> {out_path}")

    # SINGLE-CUT FIX (2026-11-19, sohaib's root-cause question: "do you
    # cut the facecam separately? Why don't you cut them TOGETHER so
    # they're cut at the same time and both have the same time?"). He is
    # right. The old flow cut video and audio in SEPARATE ffmpeg ops with
    # DIFFERENT methods — frames: output -ss on raw HLS (whose timestamps
    # are non-monotonic); audio: full decode to WAV then seek in WAV
    # (sample-accurate). Both claimed accuracy but used different time
    # bases on an unreliable container, so a small misalignment survived
    # and no av_offset could fix it (6 user reports across v3/v4/v5/Speed/
    # big/+100ms/+200ms — NEITHER offset version matched, because
    # av_offset only shifts the video window, it cannot fix a split time
    # base). Now: cut ONE clean clip covering the union window in a
    # single ffmpeg call (video + audio together, re-encoded to a
    # timestamp-sane MP4 — verified: clean clip audio matches the
    # transcript word-for-word), then extract frames AND audio FROM that
    # clean clip. Both share the clean clip's single time base, so the
    # facecam lips, the voice and the captions all come from one cut.
    # The union window covers both the audio window [start, end] and the
    # video window [video_start, video_start+dur] (they differ by
    # av_offset); the per-stream offsets inside the clean clip are
    # video_off / audio_off below. Output -ss is kept everywhere (never
    # input -ss on raw HLS — v3 fix), and the clean clip itself is cut
    # with output -ss (make_clean_clip already does that).
    clean_start = min(start, video_start)
    clean_end = max(end, video_start + dur)
    clean_dur = clean_end - clean_start
    video_off = video_start - clean_start  # video window offset in clean clip
    audio_off = start - clean_start        # audio window offset in clean clip
    clean_path = os.path.join(work_dir, "clean_base.mp4")
    make_clean_clip(src, clean_start, clean_dur, clean_path)
    print(f"[render] single-cut clean clip [{clean_start:.1f}-"
          f"{clean_end:.1f}s] video_off={video_off:.2f}s "
          f"audio_off={audio_off:.2f}s", flush=True)

    frames_dir = os.path.join(work_dir, "frames")
    extract_frames(clean_path, video_off, dur, frames_dir)
    audio_path = os.path.join(work_dir, "clip_audio.m4a")
    extract_audio(clean_path, audio_off, dur, audio_path)
    track_mov, _man = build_caption_track(transcript_path, start, end, style,
                                          work_dir, fonts_dir, script=script,
                                          out_size=(out_w, out_h))
    print(f"[render] captions done in {time.time() - t0:.0f}s", flush=True)

    cap_y = rp["cap_y_default"]
    facecam_path = None
    overlay = None
    layout_info = None
    is_band = False
    is_stage = False   # "stage" layout: compact facecam band + big game + caption zone
    is_stage_nofc = False  # "stage without facecam": stage look, no facecam
                           # (sohaib 2026-11-19: a streamer with no facecam
                           # still gets the stage layout, not plain game)
    stage_geom = {}
    stage_mask_path = None   # rounded-corner mask for the stage facecam
    stage_glow_pattern = None  # animated facecam-frame PNG sequence
    stage_glow_n = 240         # frames in one glow loop (gold default:
                               # one full lap = 4s at 60fps; slowed
                               # 2026-11-19 — sohaib said the 2s/lap
                               # (120 frames) was too fast). The actual
                               # count comes from the frame_style's spec
                               # (static = 1, pulse = 120).
    band_w = out_w      # band width (output px); full width by default
    band_pos = "top"    # band side: "top" or "bottom"
    band_vstack = False  # full-width band = vstack split; narrow = overlay
    if mode == "gameplay":
        # auto-check the facecam position + free zones on THIS clip's frames.
        # MediaPipe FIRST: it finds the real facecam (a face inside a
        # box). The static-border check (ClipKontrast, numpy+ffmpeg only,
        # no model) runs ONLY when MediaPipe finds nothing — as a
        # fallback for a bordered webcam whose face is turned away.
        # It must NOT run first: measured 2026-11-18, on the PUBG slice
        # [72,77] it "hit" a HUD panel (869,793,716,287, aspect 2.49)
        # while the real facecam is [0,460,440,240] — PUBG's HUD has
        # stronger bordered rectangles than its borderless webcam, so a
        # border-first order would put a HUD panel in the facecam slot.
        det = None
        try:
            det = detect_facecam(
                frames_dir, SRC_W, 1080,
                model_path=os.path.join(BASE, "models", "blaze_face_full_range.tflite"))
        except Exception as e:
            print(f"[render] MediaPipe facecam failed ({e}) — trying border",
                  flush=True)
            det = None
        if det is None:
            try:
                det = detect_facecam_border(src, SRC_W, 1080, start=start, dur=dur)
                if det is not None:
                    print(f"[render] border fallback hit: {det['rect']} "
                          f"(static-border, {det['samples']} frames)", flush=True)
            except Exception as e:
                print(f"[render] border fallback failed ({e})", flush=True)
                det = None
        if det is None:
            # Per-channel fallback rect (2026-11-19): the border detector
            # misses the facecam often (Speed 235918, TG 170256, auto
            # 001248/001913 all rendered with NO facecam even though the
            # streamer is on camera). Each channel's webcam sits in a
            # fixed spot in its own stream layout — measured from frames:
            # IShowSpeed (0,445,410,255) from the two border hits,
            # Typical Gamer (16,370,364,250) from a TG source frame,
            # caseoh_ (0,130,405,310) top-left (Twitch, 2026-11-19 v2 fix).
            # A wrong rect is worse than none, so channels with no
            # measured rect get no fallback (detection only).
            m = re.search(r"v=([A-Za-z0-9_-]{11})", os.path.basename(src))
            vid = m.group(1) if m else None
            fb = CHANNEL_FACECAM.get(vid) if vid else None
            if fb is None:
                # Twitch segments are named seg_<channel>__<ts>.mp4
                tm = re.search(r"seg_([A-Za-z0-9_]+)__", os.path.basename(src))
                if tm:
                    fb = CHANNEL_FACECAM_TWITCH.get(tm.group(1).lower())
                    vid = tm.group(1) if fb else None
            if fb:
                det = {"rect": fb, "method": "channel-fallback",
                       "face": None, "samples": 0, "busy": None}
                print(f"[render] channel fallback rect for {vid}: {fb}",
                      flush=True)
        try:
            layout_info = choose_layout(
                det, SRC_W, 1080, facecam_pos=facecam_pos,
                caption_pos=caption_pos, ratio=ratio, res=res,
                facecam_size=facecam_size, facecam_band=facecam_band,
                facecam_band_w=facecam_band_w) \
                if det else fallback_layout(ratio, res,
                                            facecam_size=facecam_size,
                                            facecam_band=facecam_band,
                                            facecam_band_w=facecam_band_w,
                                            facecam_pos=facecam_pos,
                                            caption_pos=caption_pos)
        except Exception as e:
            print(f"[render] layout detection failed ({e}) — using fallback", flush=True)
            layout_info = fallback_layout(ratio, res,
                                          facecam_size=facecam_size,
                                          facecam_band=facecam_band,
                                          facecam_band_w=facecam_band_w,
                                          facecam_pos=facecam_pos,
                                          caption_pos=caption_pos)
        print(f"[render] layout: {json.dumps({k: layout_info[k] for k in ('corner', 'caption_y', 'detection_method', 'facecam_src_rect') if k in layout_info})}", flush=True)
        cap_y = layout_info["caption_y"]
        # caption band (stage-top/centre/upper/lower, sohaib 2026-11-19):
        # a strip of BLURRED gameplay behind the caption text, so the
        # text stays readable even though it sits over the sharp game.
        # (y, h) in OUTPUT coords, or None (stage-bottom: the text
        # already sits in the bottom blurred zone; non-stage layouts:
        # no band — those filters have no blurred bg to crop from).
        cap_band = layout_info.get("caption_band")
        band_w = layout_info.get("band_w", out_w)
        band_pos = layout_info.get("band_pos", "top")
        if layout_info.get("stage"):
            # stage layout: small gold-framed facecam on top, gameplay
            # as large as possible in the middle, captions over blurred
            # gameplay at the bottom. The facecam is extracted at the
            # stage size with its own aspect (no full-width stretch —
            # that would blur it); the rounded mask + gold frame are
            # built from the stage geometry.
            is_stage = True
            stage_geom = layout_info["stage_geom"]
            if (layout_info.get("facecam_src_rect")
                    and facecam_pos != "stage-nofacecam"):
                # stage WITH facecam: extract it + build the frame assets.
                # facecam_pos="stage-nofacecam" is an EXPLICIT choice —
                # it skips the facecam even when a rect was detected.
                # Frame-style GEOMETRY override (circle/big change the
                # facecam size, bold changes the border): applied here so
                # the extraction, the mask/frame assets AND the overlay
                # positions below all use the same overridden geometry.
                # (A copy — the layout's geometry dict is not mutated.)
                sg = dict(stage_geom)
                fc_rect, frame_rect, use_border = _frame_style_geom(
                    sg, frame_style, facecam_pos, out_w, out_h)
                sg["fc"] = fc_rect
                sg["frame"] = frame_rect
                sg["border"] = use_border
                stage_geom = sg
                facecam_path = _extract_facecam(
                    frames_dir, layout_info["facecam_src_rect"], work_dir,
                    sg["fc"][2], sg["fc"][3],
                    target_aspect=sg["fc"][2] / sg["fc"][3])
                stage_mask_path, stage_glow_pattern, stage_glow_n = \
                    _stage_frame_assets(
                        sg["fc"][2], sg["fc"][3], sg["border"], sg["radius"],
                        sg["glow_pad"], work_dir, frame_style=frame_style)
            else:
                # No facecam: do NOT fall back to plain game mode — render
                # the stage look WITHOUT the facecam (sohaib 2026-11-19):
                # the game fills the full frame height (the freed top
                # band goes to gameplay) and captions sit over the same
                # blurred-gameplay zone at the bottom as the stage layout.
                # No empty space at the top, no facecam frame, no mask.
                # (is_stage=False so the facecam filter branch does not
                # run; stage_geom is kept — the no-facecam branch needs
                # its game_bottom / blur_sigma / caption zone.)
                is_stage = False
                is_stage_nofc = True
                print("[render] stage chosen but no facecam — rendering "
                      "stage without facecam (full-frame game + blurred "
                      "caption zone)", flush=True)
        elif layout_info.get("band"):
            is_band = True
            if layout_info.get("facecam_src_rect"):
                facecam_path = _extract_facecam(
                    frames_dir, layout_info["facecam_src_rect"], work_dir,
                    band_w, rp["band_h"],
                    target_aspect=band_w / rp["band_h"])
            else:
                print(f"[render] {band_pos}-band chosen but no facecam "
                      f"found — falling back to plain game mode", flush=True)
                mode = "game"
                is_band = False
        elif layout_info.get("facecam_src_rect"):
            overlay = layout_info["overlay"]
            facecam_path = _extract_facecam(
                frames_dir, layout_info["facecam_src_rect"], work_dir,
                overlay["w"], overlay["h"])
        else:
            print("[render] no facecam found in this clip — falling back to "
                  "plain game mode (no overlay)", flush=True)
            mode = "game"

    if mode in ("game", "gameplay"):
        # Full-width band = vstack split: the game is cropped to the area
        # beside the band. Narrower band = the game stays full-frame and
        # the band is overlaid on it (centred) — no black bars on the sides.
        band_vstack = is_band and band_w >= out_w
        if is_stage or is_stage_nofc:
            # stage (with or without facecam): the game crop + scale +
            # pad are built in the stage filter branches below
            # (pad-based); nothing to set here.
            crop_filt = ""
            base_scale = ""
        elif band_vstack:
            # band layout: the game area is beside the facecam band, so the
            # source crop is wider (see positions.game_crop)
            cx, cy, cw, ch = game_crop(rp, band=True)
            crop_filt = f"crop={cw}:{ch}:{cx}:{cy}"
            game_h = out_h - rp["band_h"]
            base_scale = f"scale={out_w}:{game_h}:flags=lanczos"
        else:
            cx, cy, cw, ch = game_crop(rp, band=False)
            crop_filt = f"crop={cw}:{ch}:{cx}:{cy}"
            base_scale = f"scale={out_w}:{out_h}:flags=lanczos"
        pre = ""
    elif mode in ("facecam", "doc"):
        # tracker input uses the same shifted video window as the frames
        # (lip-sync fix) so the tracked facecam aligns with the render.
        track_cmd = _tracking_cmd(src, video_start, dur, work_dir, mode)
        crop_filt = f"crop={CROP_W}:{CROP_H}:0:0"
        base_scale = f"scale={out_w}:{out_h}:flags=lanczos"
        pre = f"fps={FPS},sendcmd=f={track_cmd},"
    else:
        raise ValueError(f"unknown mode {mode!r} (game|gameplay|facecam|doc)")

    # Bottom strip crop: GTA 6 bakes its own dialogue subtitles into the
    # bottom of the game frame — they are pixels, not a caption track, so
    # they can only be removed by cropping. MEASURED (clip 170256, 720p):
    # the game subtitle sits at ~y1150-1205 (strips y1080-1280 @ 20s/28s/35s;
    # y1190-1280 is clean), NOT y1216-1280 as first guessed — a 90px crop
    # leaves the subtitle visible. MEASURED (clips 170256/171803):
    # subtitle top varies y1095-1145, so cutting 200px (to y1080) clears
    # it with margin; our captions end ~y1030, so 50px clearance remains. Our burned-in captions end at ~y1050 and the facecam
    # is top-left, so both stay safe (80px clearance). Output 720x1130
    # at 720p (crop scales with out_h, so 1080p -> 1080x1695).
    # Side effect: the frame is no longer 9:16 (720/1130 = 0.64) — the
    # website's .gal-video uses object-fit:cover at 9:16, so ~7% gets
    # cropped off EACH SIDE on the raw <video> path (captions are centred
    # with margins, so they survive; very wide words could touch the edge).
    # Drive's iframe player fits instead, so no crop there. Alternative if
    # that side-crop ever bites: pad back to 720x1280 with a black bar.
    # NO bottom crop any more (2026-11-19, sohaib's call): the 200px crop
    # cut the GTA subtitles but left a black bar at the bottom of the
    # 9:16 frame and he said it looks weird — "neeche black out na ho".
    # The game subtitles stay visible now; the frame stays proper 9:16.
    bottom_crop = ""

    # Colour: the caption overlay forces the chain into RGB, and the auto
    # RGB->YUV conversion lands on full-range + BT.601 (yuvj420p, bt470bg)
    # — players then show washed-out/wrong colours, the "not HD" feel.
    # Force standard limited-range BT.709 at the end of the chain.
    # NOTE: no leading comma — the filter is appended directly after
    # [vpre] (with bottom_crop gone, a leading comma made an empty
    # filter and ffmpeg failed with "No such filter: ''").
    # setsar=1 (2026-11-19, v2 fix): the stage chain's blurred-background
    # scale (980x1080 -> 720x1280) made ffmpeg preserve the source DAR by
    # setting SAR 392:243, so the output displayed as DAR 49:54 (~square)
    # instead of 9:16 on phones. Forcing SAR 1:1 at the end guarantees
    # the output is a true 9:16 full-screen frame.
    color_fix = ("scale=out_color_matrix=bt709:out_range=tv,"
                 "format=yuv420p,setsar=1")

    if is_stage or is_stage_nofc:
        # LETTERBOX FIX (2026-11-19, sohaib's v4 black-bars report): GTA 6
        # cutscenes are letterboxed in the SOURCE (~60px black top +
        # ~60px black bottom of the 1920x1080 capture). The stage crop
        # used the full 1080 height, so those bars were faithfully
        # reproduced as black bands above (y 192-320) and below (y
        # 1216-1280) the gameplay. Detect the content bounds and crop
        # the GAME to the content only — the facecam is extracted
        # separately from the full frames, so it is unaffected. When
        # there is no letterbox (y0=0, y1=1080) the crop is byte-identical
        # to before, so non-letterboxed clips (e.g. v3) are unchanged.
        lb_y0, lb_y1 = detect_letterbox(frames_dir, src_h=1080)
        if lb_y0 or lb_y1 < 1080:
            content_h = lb_y1 - lb_y0
            game_h_lb = stage_geom["game_h"]
            crop_w_lb = int(out_w * content_h / game_h_lb)
            crop_w_lb = (crop_w_lb // 2) * 2
            crop_x_lb = (SRC_W - crop_w_lb) // 2
            stage_geom["crop"] = (crop_x_lb, lb_y0, crop_w_lb, content_h)
            print(f"[render] letterbox detected: content y {lb_y0}-"
                  f"{lb_y1} ({content_h}px) — game crop now "
                  f"{stage_geom['crop']}", flush=True)

    if is_stage:
        # stage: sharp game in the middle (full height, side-crop only)
        # over a heavily blurred copy of the same game frame (fills the
        # whole canvas — no black bars, captions sit over blurred game);
        # small facecam on top with rounded corners + ANIMATED golden
        # light travelling around the frame (v2 fix — the reference Reel's
        # frame is a moving golden fire/light, not a static border);
        # captions over the blurred zone at the bottom.
        # [0]=frames [1]=caption track [2]=audio [3]=facecam video
        # [4]=rounded mask (looped) [5]=gold glow PNG sequence.
        sg = stage_geom
        cx, cy, cw, ch = sg["crop"]
        fx, fy = sg["fc"][0], sg["fc"][1]
        frx, fry = sg["frame"][0], sg["frame"][1]
        # caption band (stage-top/centre/upper/lower): a strip of the
        # BLURRED full-frame bg behind the caption text, so the text
        # stays readable over the sharp game. Split [vblur] so the band
        # can be cropped from it; stage-bottom needs none (its text
        # already sits in the bottom blurred zone).
        if cap_band:
            band_y, band_h_cap = cap_band
            blur_src = (f"gblur=sigma={sg['blur_sigma']},"
                        f"crop={out_w}:{out_h}:0:{BLUR_OVERSCAN},"
                        f"split=2[vblur][vblur2]")
            band_filt = (f"[vblur2]crop={out_w}:{band_h_cap}:0:{band_y}"
                         f"[cband];"
                         f"[v1][cband]overlay=0:{band_y}[v1b];")
            cap_src = "[v1b]"
        else:
            blur_src = (f"gblur=sigma={sg['blur_sigma']},"
                        f"crop={out_w}:{out_h}:0:{BLUR_OVERSCAN}[vblur]")
            band_filt = ""
            cap_src = "[v1]"
        filt = (f"[0:v]{pre}crop={cw}:{ch}:{cx}:{cy},split=2[gc1][gc2];"
                f"[gc1]scale={out_w}:{sg['game_h']}:flags=lanczos,"
                f"unsharp=5:5:0.8:5:5:0.0[vsharp];"
                f"[gc2]scale={out_w}:{out_h + 2 * BLUR_OVERSCAN}:flags=lanczos,"
                f"{blur_src};"
                f"[vblur][vsharp]overlay=0:{sg['band_h']}[v0];"
                f"[3:v]format=rgba[fc0];"
                f"[4:v]format=rgba[mk];[fc0][mk]alphamerge[fcr];"
                f"[5:v]format=rgba,loop=loop=-1:size={stage_glow_n}[glow];"
                f"[v0][glow]overlay={frx}:{fry}[v1a];"
                f"[v1a][fcr]overlay={fx}:{fy}[v1];"
                f"{band_filt}"
                f"[1:v]format=rgba[cap];{cap_src}[cap]overlay=0:{cap_y}[vpre];"
                f"[vpre]{bottom_crop}{color_fix}[v]")
        inputs = (["ffmpeg", "-y", "-v", "error",
                   "-framerate", str(FPS), "-i",
                   os.path.join(frames_dir, "f_%04d.jpg"),
                   "-i", track_mov, "-i", audio_path, "-i", facecam_path,
                   "-loop", "1", "-framerate", str(FPS), "-i",
                   stage_mask_path,
                   "-framerate", str(FPS), "-i",
                   stage_glow_pattern])
        audio_idx = 2
    elif is_stage_nofc:
        # stage WITHOUT facecam: sharp game fills the FULL frame height
        # (y 0..game_bottom — the freed top band goes to gameplay) over
        # a heavily blurred copy of the same game frame (fills the whole
        # canvas — no black bars, no empty top band); captions over the
        # blurred zone at the bottom — the SAME caption zone, blur and
        # styling as the stage layout. No facecam, no frame, no mask.
        # [0]=frames [1]=caption track [2]=audio.
        sg = stage_geom
        game_h_nofc = sg["game_bottom"]  # 980 at 720p: game y 0..980
        # use the stage crop (letterbox-corrected by the adjustment
        # above — identical to the old 1080-based crop when there is
        # no letterbox, so no-facecam renders are unchanged there).
        cx2, cy2, cw2, ch2 = sg["crop"]
        # caption band (stage-top/centre/upper/lower): same blurred
        # strip behind the caption text as the stage branch (see above).
        if cap_band:
            band_y, band_h_cap = cap_band
            blur_src = (f"gblur=sigma={sg['blur_sigma']},"
                        f"crop={out_w}:{out_h}:0:{BLUR_OVERSCAN},"
                        f"split=2[vblur][vblur2]")
            band_filt = (f"[vblur2]crop={out_w}:{band_h_cap}:0:{band_y}"
                         f"[cband];"
                         f"[v0][cband]overlay=0:{band_y}[v0b];")
            cap_src = "[v0b]"
        else:
            blur_src = (f"gblur=sigma={sg['blur_sigma']},"
                        f"crop={out_w}:{out_h}:0:{BLUR_OVERSCAN}[vblur]")
            band_filt = ""
            cap_src = "[v0]"
        filt = (f"[0:v]{pre}crop={cw2}:{ch2}:{cx2}:{cy2},split=2[gc1][gc2];"
                f"[gc1]scale={out_w}:{game_h_nofc}:flags=lanczos,"
                f"unsharp=5:5:0.8:5:5:0.0[vsharp];"
                f"[gc2]scale={out_w}:{out_h + 2 * BLUR_OVERSCAN}:flags=lanczos,"
                f"{blur_src};"
                f"[vblur][vsharp]overlay=0:0[v0];"
                f"{band_filt}"
                f"[1:v]format=rgba[cap];{cap_src}[cap]overlay=0:{cap_y}[vpre];"
                f"[vpre]{bottom_crop}{color_fix}[v]")
        inputs = (["ffmpeg", "-y", "-v", "error",
                   "-framerate", str(FPS), "-i",
                   os.path.join(frames_dir, "f_%04d.jpg"),
                   "-i", track_mov, "-i", audio_path])
        audio_idx = 2
    elif is_band:
        # [0]=frames [1]=caption track [2]=audio [3]=facecam band video
        if band_vstack:
            # full-width band: split-screen — facecam + game stacked
            stack = ("[fc][v0]vstack[v1]" if band_pos == "top"
                     else "[v0][fc]vstack[v1]")
            filt = (f"[0:v]{pre}{crop_filt},{base_scale},"
                    f"unsharp=5:5:0.8:5:5:0.0[v0];"
                    f"[3:v]drawbox=x=0:y=0:w=iw:h=ih:color=0x00E5FF:t=3[fc];"
                    f"{stack};"
                    f"[1:v]format=rgba[cap];[v1][cap]overlay=0:{cap_y}[vpre];"
                    f"[vpre]{bottom_crop}{color_fix}[v]")
        else:
            # narrower band: the game stays full-frame, the band is
            # overlaid on it (centred, top or bottom) — no black bars
            band_x = (out_w - band_w) // 2
            band_y = 0 if band_pos == "top" else out_h - rp["band_h"]
            filt = (f"[0:v]{pre}{crop_filt},{base_scale},"
                    f"unsharp=5:5:0.8:5:5:0.0[v0];"
                    f"[3:v]drawbox=x=0:y=0:w=iw:h=ih:color=0x00E5FF:t=3[fc];"
                    f"[v0][fc]overlay={band_x}:{band_y}[v1];"
                    f"[1:v]format=rgba[cap];[v1][cap]overlay=0:{cap_y}[vpre];"
                    f"[vpre]{bottom_crop}{color_fix}[v]")
        inputs = (["ffmpeg", "-y", "-v", "error",
                   "-framerate", str(FPS), "-i",
                   os.path.join(frames_dir, "f_%04d.jpg"),
                   "-i", track_mov, "-i", audio_path, "-i", facecam_path])
        audio_idx = 2
    elif facecam_path is not None:
        # [0]=frames [1]=caption track [2]=audio [3]=facecam video
        filt = (f"[0:v]{pre}{crop_filt},{base_scale},"
                f"unsharp=5:5:0.8:5:5:0.0[v0];"
                f"[3:v]drawbox=x=0:y=0:w=iw:h=ih:color=0x00E5FF:t=3[fc];"
                f"[v0][fc]overlay={overlay['x']}:{overlay['y']}[v1];"
                f"[1:v]format=rgba[cap];[v1][cap]overlay=0:{cap_y}[vpre];"
                f"[vpre]{bottom_crop}{color_fix}[v]")
        inputs = (["ffmpeg", "-y", "-v", "error",
                   "-framerate", str(FPS), "-i",
                   os.path.join(frames_dir, "f_%04d.jpg"),
                   "-i", track_mov, "-i", audio_path, "-i", facecam_path])
        audio_idx = 2
    else:
        filt = (f"[0:v]{pre}{crop_filt},{base_scale},"
                f"unsharp=5:5:0.8:5:5:0.0[v0];"
                f"[1:v]format=rgba[cap];[v0][cap]overlay=0:{cap_y}[vpre];"
                f"[vpre]{bottom_crop}{color_fix}[v]")
        inputs = (["ffmpeg", "-y", "-v", "error",
                   "-framerate", str(FPS), "-i",
                   os.path.join(frames_dir, "f_%04d.jpg"),
                   "-i", track_mov, "-i", audio_path])
        audio_idx = 2
    cmd = (inputs + ["-filter_complex", filt, "-map", "[v]", "-map", f"{audio_idx}:a"]
           + _video_codec_args(encoder, preset) +
           ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-color_range", "tv", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709",
            "-t", str(dur), out_path])
    t1 = time.time()
    try:
        run(cmd, "final encode")
    except RuntimeError:
        if encoder == "qsv":
            # QSV not available / failed (e.g. no Intel GPU) — fall back
            # to software so the render still completes.
            print("[render] QSV encode failed — falling back to libx264",
                  flush=True)
            fb = (inputs + ["-filter_complex", filt, "-map", "[v]",
                    "-map", f"{audio_idx}:a"]
                  + _video_codec_args("libx264", preset) +
                  ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-color_range", "tv", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709",
                   "-t", str(dur), out_path])
            run(fb, "final encode (libx264 fallback)")
        else:
            raise
    print(f"[render] encode done in {time.time() - t1:.0f}s "
          f"(total {time.time() - t0:.0f}s) -> {out_path} "
          f"({os.path.getsize(out_path) / 1e6:.1f} MB)")
    if not keep_work:
        import shutil
        shutil.rmtree(frames_dir, ignore_errors=True)
    return out_path


def _rounded_rect_perimeter(fw, fh, r, step=3.0):
    """Sample points (x, y) clockwise around a rounded rect [0,fw]x[0,fh]
    with corner radius r, roughly `step` px apart. Used to drive the
    travelling golden light around the stage facecam frame."""
    import math
    pts = []

    def edge(x0, y0, x1, y1):
        import math as _m
        L = _m.hypot(x1 - x0, y1 - y0)
        n = max(1, int(L / step))
        for i in range(n):
            t = i / n
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))

    def arc(cx, cy, a0, a1):
        # step so arc length per point is ~step px
        da = math.degrees(step / max(r, 1.0))
        a = a0
        while (a <= a1 if a1 > a0 else a >= a1):
            rad = math.radians(a)
            pts.append((cx + r * math.cos(rad), cy + r * math.sin(rad)))
            a += da if a1 > a0 else -da

    edge(r, 0, fw - r, 0)            # top
    arc(fw - r, r, -90, 0)           # top-right corner
    edge(fw, r, fw, fh - r)          # right
    arc(fw - r, fh - r, 0, 90)       # bottom-right corner
    edge(fw - r, fh, r, fh)          # bottom
    arc(r, fh - r, 90, 180)          # bottom-left corner
    edge(0, fh - r, 0, r)            # left
    arc(r, r, 180, 270)              # top-left corner
    return pts


def _frame_style_geom(sg, frame_style, facecam_pos, out_w, out_h):
    """Apply a frame style's GEOMETRY overrides to a stage geometry dict.
    Returns (fc_rect, frame_rect, border) — the facecam rect (x,y,w,h),
    the frame rect (x,y,w,h), and the border thickness to build the
    frame assets with. Styles without overrides return the standard
    geometry unchanged. The GAME area is NOT moved: a bigger facecam
    (big) simply floats over the top of the game by ~29px — the
    facecam is an overlay, and the overlap reads as intentional. The
    anchor follows the facecam position (centre stays centred, left
    stays left, right stays right, side stays vertically centred)."""
    spec = FRAME_STYLES.get(frame_style) or FRAME_STYLES[DEFAULT_FRAME_STYLE]
    s = out_h / 1280.0
    fc_x, fc_y, fc_w, fc_h = sg["fc"]
    border = sg["border"]
    glow_pad = sg["glow_pad"]
    top_margin = sg["top_margin"]
    if "fc_size" in spec:
        w720, h720 = spec["fc_size"]
        fc_w = max(2, int(w720 * s))
        fc_h = max(2, int(h720 * s))
        fc_w, fc_h = (fc_w // 2) * 2, (fc_h // 2) * 2
        if facecam_pos == "stage-topleft":
            fc_x = top_margin
        elif facecam_pos == "stage-topright":
            fc_x = out_w - fc_w - top_margin
        elif facecam_pos == "stage-side":
            fc_x = top_margin
        else:  # "stage" (top-centre) and anything else: centred
            fc_x = (out_w - fc_w) // 2
        if facecam_pos == "stage-side":
            fc_y = (out_h - fc_h) // 2
        elif "fc_y" in spec:
            fc_y = int(spec["fc_y"] * s)
        else:
            fc_y = top_margin
    if "border" in spec:
        border = max(3, int(spec["border"] * s))
    ext = border + glow_pad
    # keep the frame fully on-canvas: a thick border (bold) or a low
    # top margin (circle) can push the frame rect off the top/left
    # edge, clipping the ring — shift the facecam in until it fits.
    if fc_y < ext:
        fc_y = ext
    if fc_x < ext:
        fc_x = ext
    frame_rect = (fc_x - ext, fc_y - ext, fc_w + 2 * ext, fc_h + 2 * ext)
    return (fc_x, fc_y, fc_w, fc_h), frame_rect, border


def _paint_frame(kind, f, n, geom, spec):
    """Paint ONE frame of the stage facecam frame sequence (RGBA).
    kind: comet | line | fire | static | pulse | circle | clean |
    corners (see FRAME_STYLES).
    geom: {"fw","fh","ring","base","perim","n_per","trail","core_r",
           "border","glow_pad"} — shared geometry from _stage_frame_assets.
    spec: the FRAME_STYLES entry (colours).
    - comet: travelling blob with a fading tail + white-hot core + soft
      halo (the original golden logic, colours from spec).
    - fire: same travelling idea, but a longer tail, a bigger blob and a
      strong double-sine flicker — reads as flame, not light.
    - line: a bright arc of FIXED length and UNIFORM brightness sweeping
      around — no tail fade, no blob — reads as a rotating line.
    - static: just the base ring (the design is the ring itself); the
      caller only ever asks for frame 0 of 1.
    - pulse: the whole ring breathes — alpha oscillates 0.35..1 over the
      loop, with a soft halo that breathes with it; nothing travels.
    - circle: like comet (travelling blob + tail), but the perimeter in
      geom is a CIRCLE — the facecam is round, so the light travels
      around the round frame. (Mask/ring are circular too, built by
      _stage_frame_assets.)
    - clean: NO ring at all — just a soft drop shadow under the facecam
      (a dark blurred rounded rect, offset down-right, drawn under the
      facecam by the filter order). The caller only asks for frame 0.
    - corners: viewfinder style — only the four corner brackets, no
      full ring. The caller only asks for frame 0."""
    from PIL import Image, ImageDraw, ImageFilter
    import math
    fw, fh = geom["fw"], geom["fh"]
    base = geom["base"]
    if kind == "static":
        return base
    if kind == "clean":
        # drop shadow only: a dark rounded rect, offset down-right by
        # ~6px and blurred, sitting under the facecam (the filter
        # overlays the glow BEFORE the facecam, so the shadow peeks out
        # behind its bottom-right edge). No ring, no border.
        ext = geom["border"] + geom["glow_pad"]
        sh = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        off = max(4, geom["glow_pad"] // 2)
        sd.rounded_rectangle(
            [ext + off, ext + off,
             fw - 1 - (ext - off), fh - 1 - (ext - off)],
            radius=geom.get("radius", 18), fill=(0, 0, 0, 110))
        return sh.filter(ImageFilter.GaussianBlur(radius=6))
    if kind == "corners":
        # viewfinder: four L-shaped brackets hugging the facecam corners,
        # no full ring. Bracket length ~44px, thickness = border + 2.
        ext = geom["border"] + geom["glow_pad"]
        bw = max(4, geom["border"] + 2)
        bl = 44
        img = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        col = spec["ring"] + (spec["ring_alpha"],)
        x0, y0 = ext, ext
        x1, y1 = fw - 1 - ext, fh - 1 - ext
        # each bracket = two bars forming an L
        for (cx, cy, sx, sy) in [(x0, y0, 1, 1), (x1, y0, -1, 1),
                                 (x0, y1, 1, -1), (x1, y1, -1, -1)]:
            # horizontal bar
            hx0 = min(cx, cx + sx * bl)
            hx1 = max(cx, cx + sx * bl)
            d.rectangle([hx0, cy - bw // 2 if sy > 0 else cy - bw // 2,
                         hx1, cy + bw // 2], fill=col)
            # vertical bar
            vy0 = min(cy, cy + sy * bl)
            vy1 = max(cy, cy + sy * bl)
            d.rectangle([cx - bw // 2, vy0, cx + bw // 2, vy1], fill=col)
        halo = img.filter(ImageFilter.GaussianBlur(
            radius=max(3, geom["glow_pad"] // 2)))
        return Image.alpha_composite(halo, img)
    if kind == "pulse":
        # breathe: 0.35..1 over one loop (120 frames = 2s at 60fps)
        ph = 0.5 + 0.5 * math.sin(2 * math.pi * f / n)
        a = 0.35 + 0.65 * ph
        alpha = base.split()[3].point(lambda v: int(v * a))
        frame = Image.merge("RGBA", base.split()[:3] + (alpha,))
        halo = frame.filter(ImageFilter.GaussianBlur(
            radius=max(3, geom["glow_pad"] // 2)))
        return Image.alpha_composite(halo, frame)
    perim, n_per = geom["perim"], geom["n_per"]
    head = int((f / n) * n_per) % n_per
    if kind == "line":
        arc_len = max(10, int(n_per * 0.25))  # 25% of the perimeter
        lw = max(3, geom["border"] + 1)
        line_img = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        ld = ImageDraw.Draw(line_img)
        for k in range(arc_len):
            idx = (head - k) % n_per
            px, py = perim[idx]
            ld.ellipse([px - lw, py - lw, px + lw, py + lw],
                       fill=spec["comet"] + (235,))
        hx, hy = perim[head]
        ld.ellipse([hx - lw - 2, hy - lw - 2, hx + lw + 2, hy + lw + 2],
                   fill=spec["core"] + (255,))
        halo = line_img.filter(ImageFilter.GaussianBlur(
            radius=max(3, geom["glow_pad"] // 2)))
        frame = Image.alpha_composite(base, halo)
        return Image.alpha_composite(frame, line_img)
    # comet / fire: travelling blob with fading tail
    if kind == "fire":
        trail = max(8, int(n_per * 0.30))  # longer tail than comet
        core_r = geom["core_r"] + 2
        flick = 0.75 + 0.25 * math.sin(f * 0.9) * math.sin(f * 0.37)
    else:
        trail = geom["trail"]
        core_r = geom["core_r"]
        flick = 0.88 + 0.12 * math.sin(f * 0.55)
    comet = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
    cd = ImageDraw.Draw(comet)
    for k in range(trail, -1, -1):
        idx = (head - k) % n_per
        px, py = perim[idx]
        a = int(240 * flick * (1.0 - k / (trail + 1)) ** 1.3)
        rr = max(2, int(core_r * (0.6 + 0.4 * (1.0 - k / (trail + 1)))))
        cd.ellipse([px - rr, py - rr, px + rr, py + rr],
                   fill=spec["comet"] + (a,))
    hx, hy = perim[head]
    cd.ellipse([hx - core_r, hy - core_r, hx + core_r, hy + core_r],
               fill=spec["core"] + (255,))
    halo = comet.filter(ImageFilter.GaussianBlur(
        radius=max(3, geom["glow_pad"] // 2)))
    frame = Image.alpha_composite(base, halo)
    return Image.alpha_composite(frame, comet)


def _stage_frame_assets(fc_w, fc_h, border, radius, glow_pad, work_dir,
                        glow_frames=None, frame_style="gold"):
    """Rounded-corner mask + facecam FRAME for the stage layout
    (sohaib 2026-11-19, viralclipping010 reference: the frame is NOT a
    static yellow border — it is a golden FIRE / golden LIGHT that
    CONTINUOUSLY MOVES around the frame). frame_style picks one of
    FRAME_STYLES (default "gold"); unknown values fall back to gold.
    glow_frames defaults to the style's own loop length (240 = 4s/lap
    for the travelling designs, 120 = 2s breathe for pulse, 1 = still
    for static).

    mask.png: fc_w x fc_h, white rounded rect on transparent — used by
    alphamerge to round the facecam video's corners.
    stage_glow_%03d.png: a looping PNG sequence (glow_frames frames,
    one full lap around the frame per loop for the travelling kinds) —
    a dim base ring in the style's colour + the style's moving element
    (comet / line / fire) or the breathing ring (pulse) or just the
    ring (static). "Halki si fire / golden light", not a garish thick
    border. Returns (mask_path, glow_pattern, glow_frames) where
    glow_pattern is the ffmpeg image-sequence path
    (e.g. .../stage_glow_%03d.png)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    import math
    spec = FRAME_STYLES.get(frame_style) or FRAME_STYLES[DEFAULT_FRAME_STYLE]
    if glow_frames is None:
        glow_frames = spec["frames"]
    ext = border + glow_pad
    fw, fh = fc_w + 2 * ext, fc_h + 2 * ext
    is_circle = (spec["kind"] == "circle")
    # --- mask: rounded rect, white inside (circle: a true circle) ---
    mask = Image.new("RGBA", (fc_w, fc_h), (0, 0, 0, 0))
    if is_circle:
        ImageDraw.Draw(mask).ellipse(
            [0, 0, fc_w - 1, fc_h - 1], fill=(255, 255, 255, 255))
    else:
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, fc_w - 1, fc_h - 1], radius=radius,
            fill=(255, 255, 255, 255))
    mask_path = os.path.join(work_dir, "stage_mask.png")
    mask.save(mask_path)
    # --- ring mask: THIN gold ring (border px) hugging the facecam edge,
    # NOT a thick solid band — the reference is a subtle golden LIGHT, so
    # the base ring stays dim and the travelling comet is the star.
    # Facecam sits at [ext,ext] inside the frame image; the ring is the
    # band from the facecam edge outward by `border`.
    # (circle: the ring is the band between two concentric circles.)
    if is_circle:
        cx, cy = fw / 2.0, fh / 2.0
        r_in = fc_w / 2.0
        r_out = r_in + border
        outer = Image.new("L", (fw, fh), 0)
        ImageDraw.Draw(outer).ellipse(
            [cx - r_out, cy - r_out, cx + r_out, cy + r_out], fill=255)
        inner = Image.new("L", (fw, fh), 0)
        ImageDraw.Draw(inner).ellipse(
            [cx - r_in, cy - r_in, cx + r_in, cy + r_in], fill=255)
        ring = ImageChops.subtract(outer, inner)
    else:
        outer = Image.new("L", (fw, fh), 0)
        ImageDraw.Draw(outer).rounded_rectangle(
            [ext - border, ext - border,
             fw - 1 - (ext - border), fh - 1 - (ext - border)],
            radius=radius + border, fill=255)
        inner = Image.new("L", (fw, fh), 0)
        ImageDraw.Draw(inner).rounded_rectangle(
            [ext, ext, fw - 1 - ext, fh - 1 - ext], radius=radius, fill=255)
        ring = ImageChops.subtract(outer, inner)
    # base ring in the style's colour (dim for the animated kinds — the
    # moving light is the star; bright for static, where the ring IS
    # the design)
    base = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
    base.paste(Image.new("RGBA", (fw, fh),
                         spec["ring"] + (spec["ring_alpha"],)), (0, 0), ring)
    # perimeter points (clockwise) along the ring's centreline, for the
    # travelling kinds: the centreline rect is inset by (ext - border/2)
    # from the frame edge, radius radius + border/2.
    # (circle: points on the circle of radius fc/2 + border/2.)
    if is_circle:
        cx, cy = fw / 2.0, fh / 2.0
        r_mid = fc_w / 2.0 + border / 2.0
        n_per = max(24, int(2 * math.pi * r_mid / 3.0))
        perim = [(cx + r_mid * math.cos(2 * math.pi * i / n_per),
                  cy + r_mid * math.sin(2 * math.pi * i / n_per))
                 for i in range(n_per)]
    else:
        _cl = ext - border // 2
        perim = _rounded_rect_perimeter(
            fw - 2 * _cl, fh - 2 * _cl, radius + border // 2, step=3.0)
        perim = [(x + _cl, y + _cl) for (x, y) in perim]
        n_per = len(perim)
    trail = max(8, int(n_per * 0.22))   # comet arc ~22% of the perimeter
    core_r = max(4, border + 3)         # comet blob radius (bigger = clearer)
    geom = {"fw": fw, "fh": fh, "ring": ring, "base": base,
            "perim": perim, "n_per": n_per, "trail": trail,
            "core_r": core_r, "border": border, "glow_pad": glow_pad,
            "radius": radius}
    glow_pattern = os.path.join(work_dir, "stage_glow_%03d.png")
    for f in range(glow_frames):
        frame = _paint_frame(spec["kind"], f, glow_frames, geom, spec)
        frame.save(glow_pattern % (f + 1))
    return mask_path, glow_pattern, glow_frames


def _extract_facecam(frames_dir, src_rect, work_dir, fc_w, fc_h,
                     target_aspect=None):
    """Crop the facecam region out of the extracted frame sequence and
    encode it as a small video (same fps/duration as the base).
    target_aspect: when given, trim the source rect (centred) to that
    aspect first — used by the top-band layout (band is wider than 16:9).
    """
    x, y, w, h = [int(v) for v in src_rect]
    if target_aspect:
        ar = w / max(h, 1)
        if ar > target_aspect:  # too wide -> trim width
            nw = int(h * target_aspect)
            x += (w - nw) // 2
            w = nw
        elif ar < target_aspect:  # too tall -> trim height
            nh = int(w / target_aspect)
            y += (h - nh) // 2
            h = nh
    # libx264 + yuv420p needs EVEN dimensions: 162*1.5=243 (odd) broke the
    # 1080p facecam encode. Round down to even.
    fc_w, fc_h = (fc_w // 2) * 2, (fc_h // 2) * 2
    out_path = os.path.join(work_dir, "facecam.mp4")
    run(["ffmpeg", "-y", "-v", "error",
         "-framerate", str(FPS), "-i", os.path.join(frames_dir, "f_%04d.jpg"),
         "-vf", f"crop={w}:{h}:{x}:{y},scale={fc_w}:{fc_h}:flags=lanczos",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-pix_fmt", "yuv420p", out_path],
        "facecam extraction")
    return out_path


def _tracking_cmd(src, start, dur, work_dir, mode):
    """facecam/doc: clean clip -> V3 tracker -> 60Hz sendcmd file.
    Requires the optional facecam deps (mediapipe, opencv, scipy)."""
    clean = os.path.join(work_dir, "clean_clip.mp4")
    make_clean_clip(src, start, dur, clean)
    x0_json = os.path.join(work_dir, "track_x0.json")
    cand_json = os.path.join(work_dir, "track_candidates.json")
    run([sys.executable, os.path.join(BASE, "track_v3_stable.py"),
         "--src", clean, "--out-json", x0_json, "--out-cand", cand_json,
         "--crop-w", str(CROP_W), "--src-w", str(SRC_W),
         "--model", os.path.join(BASE, "models", "blaze_face_full_range.tflite")],
        "face tracking")
    cmd_path = os.path.join(work_dir, "track_60.cmd")
    args = [sys.executable, os.path.join(BASE, "build_track60.py"),
            "--src-json", x0_json, "--out-cmd", cmd_path,
            "--crop-w", str(CROP_W), "--src-w", str(SRC_W)]
    if mode == "doc":
        args += ["--mode", "static", "--video", clean]
    run(args, "60Hz track build")
    return cmd_path


def main():
    ap = argparse.ArgumentParser(description="Render one vertical Short from a capture.")
    ap.add_argument("--input", required=True, help="captured video file")
    ap.add_argument("--start", type=float, required=True, help="clip start (seconds, source time)")
    ap.add_argument("--end", type=float, required=True, help="clip end (seconds, source time)")
    ap.add_argument("--transcript", required=True, help="transcript JSON with word timestamps")
    ap.add_argument("--style", default="neon", choices=list(STYLES),
                    help="caption style (default: neon — sohaib's pick; hype = PeakCut's red pop)")
    ap.add_argument("--mode", default="game", choices=["game", "gameplay", "facecam", "doc"],
                    help="crop mode: game=centre (default), gameplay=game + facecam corner (PeakCut-style), facecam=V3 tracker, doc=static per shot")
    ap.add_argument("--res", type=int, default=720, choices=[720, 1080],
                    help="output resolution: 720 (720x1280 60fps, default — fast) | 1080 (1080x1920 60fps, quality)")
    ap.add_argument("--ratio", default="9:16", choices=list(RATIOS),
                    help="output ratio: 9:16 (default, Shorts) | 1:1 | 4:5 | 16:9 (landscape)")
    ap.add_argument("--facecam-pos", default="auto", choices=list(FACECAM_POSITIONS),
                    help="facecam position (gameplay mode): auto (default, per-stream check) | top-right | top-left | mid-right | mid-left | top-band | off")
    ap.add_argument("--frame-style", default="gold", choices=list(FRAME_STYLES),
                    help="stage-layout facecam frame design (default: gold — the original golden comet; red = PeakCut hype red comet; line = cyan line sweep; fire = orange fire ember; static = clean still border; pulse = violet breathing ring)")
    ap.add_argument("--caption-pos", default="auto", choices=list(CAPTION_POSITIONS),
                    help="caption position: auto (default, first free line) | bottom (y~950-1000) | seam (below the facecam band) | top")
    ap.add_argument("--preset", default=PRESET,
                    choices=["ultrafast", "superfast", "veryfast", "medium"],
                    help="libx264 preset (default: veryfast — see SPEED-REPORT.md; "
                         "ultrafast is ~33%% faster but 2.1x the file size; "
                         "medium is 2.8x slower for ~no gain)")
    ap.add_argument("--encoder", default="libx264", choices=["libx264", "qsv"],
                    help="video encoder: libx264 (default, software) | qsv "
                         "(Intel Quick Sync — his PC's UHD 620; falls back to "
                         "libx264 automatically if QSV is unavailable)")
    ap.add_argument("--script", default=None,
                    help="caption script override (default: read from transcript JSON: latin | devanagari | urdu)")
    ap.add_argument("--av-offset", type=float, default=None,
                    help="A/V sync offset in seconds (video lags audio by this "
                         "much; the video window is shifted later to match the "
                         "voice). Default: auto from CHANNEL_AV_OFFSET_S by "
                         "channel id, else 0. Use 0 to disable the correction.")
    ap.add_argument("--out", required=True, help="output mp4 path")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--keep-work", action="store_true")
    a = ap.parse_args()
    render_short(a.input, a.start, a.end, a.transcript, a.style, a.mode,
                 a.out, a.work_dir, keep_work=a.keep_work, script=a.script,
                 res=a.res, preset=a.preset, encoder=a.encoder,
                 ratio=a.ratio, facecam_pos=a.facecam_pos,
                 caption_pos=a.caption_pos, frame_style=a.frame_style,
                 av_offset_s=a.av_offset)


if __name__ == "__main__":
    main()
