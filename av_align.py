"""Automatic per-clip A/V sync measurement (PeakCut `_align()` pattern).

PROBLEM (proven by measurement, 2026-11-19): the captured YouTube-live
file has VIDEO behind AUDIO by a per-segment offset (RAJA measured
~2-3s; IShowSpeed perceived ~8s). The offset is baked in at capture
(yt-dlp muxes two independent DASH video-only/audio-only streams) and
VARIES per segment/channel, so no global value can fix it and manual
per-clip measurement is not scalable.

PATTERN (copied from PeakCut `studio/live.py` `_align()` + `clock_fix`):
for every clip, measure the offset automatically by comparing two
independent time references, take the MEDIAN, and carry the last
measured drift forward as the seed for the next clip.

OUR ADAPTATION (audio+video are muxed in one file by yt-dlp, so the
anchor is the game subtitle burned into the VIDEO frames vs the Whisper
transcript word times, which are AUDIO time):
  1. sample the subtitle region at 2 fps over the clip window,
  2. detect change points (a new subtitle line) by mean abs diff of
     the subtitle band between consecutive frames,
  3. OCR only the change-point frames (tesseract, cropped + 2x upscale,
     --psm 7) to read the subtitle line,
  4. match each subtitle line to the Whisper segment it belongs to
     (normalised token overlap, within +/-8s of the seeded guess),
  5. offset = MEDIAN(subtitle_appearance_time - segment_start_time).
     Positive = video late (subtitle appears after the voice speaks).
  6. carry the last measured value forward per channel as `clock_fix`
     (seed for the next clip; fallback when a clip has no subtitles,
     e.g. free-roam with no game dialogue).

The measured offset is passed as that clip's `av_offset` (render_short
advances the video window by it, so video+voice become equal). The
global DEFAULT_AV_OFFSET_S stays 0.0; the per-clip measured value
overrides it for that clip only. No manual flag, no guessed global.
"""

import json
import math
import os
import re
import statistics
import subprocess
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))

# --- verified parameters (measured 2026-11-19 on the RAJA segment) ---
# Subtitle band, as fractions of frame size (1920x1080 -> x600-1320,
# y930-1010, bottom-centre; the facecam does not cover it).
SUB_BAND = (0.3125, 0.8611, 0.6875, 0.9352)
SAMPLE_FPS = 2.0            # subtitle sampling rate (appearance +-0.25s)
CHANGE_THRESHOLD = 5.0      # mean abs diff: subtitle changes 7.7-11.0,
                            # stable frames 0-3.4
SEARCH_WINDOW = 8.0         # +/-8s around the seeded guess (PeakCut)
MIN_MATCHES = 3             # need >=3 matched lines, else fall back to seed
MIN_OVERLAP = 0.34          # token-overlap fraction to accept a match
OFFSET_CLAMP = (0.0, 12.0)  # sanity clamp on the measured offset (seconds)
CLOCK_FIX_FILE = os.path.join(BASE, "av_clock_fix.json")


def _norm(text):
    """Lowercase alphanumeric-only (PeakCut key())."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _keys(text):
    """Set of normalised words (len>=3) in a text."""
    return {_norm(w) for w in re.split(r"\s+", text or "") if len(_norm(w)) >= 3}


def _keys_match(k1, k2):
    """Keys match if equal, or one contains the other (len>=3).

    Handles Whisper-vs-subtitle spelling drift, e.g. subtitle 'uh-huh'
    (-> 'uhhuh') vs transcript '-huh.' (-> 'huh'): 'huh' is a substring
    of 'uhhuh', so they match.
    """
    if k1 == k2:
        return True
    if len(k1) >= 3 and len(k2) >= 3:
        return k1 in k2 or k2 in k1
    return False


def channel_key_for(seg_path):
    """Channel/video key for a segment file, same ids as av_offset_for."""
    m = re.search(r"watch\?v=([A-Za-z0-9_-]{11})", seg_path or "")
    if m:
        return m.group(1)
    m2 = re.search(r"seg_([A-Za-z0-9_]+)__", seg_path or "")
    if m2:
        return m2.group(1).lower()
    return os.path.splitext(os.path.basename(seg_path or "unknown"))[0]


def load_clock_fix(key, default=0.0):
    """Last measured A/V offset for a channel (the seed / fallback)."""
    try:
        data = json.load(open(CLOCK_FIX_FILE))
        return float(data.get(key, default))
    except Exception:
        return default


def save_clock_fix(key, value):
    """Persist a newly measured offset for a channel."""
    try:
        data = {}
        if os.path.exists(CLOCK_FIX_FILE):
            data = json.load(open(CLOCK_FIX_FILE))
        data[key] = round(float(value), 2)
        json.dump(data, open(CLOCK_FIX_FILE, "w"), indent=1)
    except Exception as e:
        print(f"[av-align] could not save clock_fix ({e})")


def _run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed: {(r.stderr or r.stdout or '')[:300]}")
    return r


def _sample_frames(seg_path, start, end, tmpdir):
    """Extract 2-fps JPEGs of the segment over [start, end].

    Returns [(time, path)] with time in segment seconds.
    """
    dur = end - start
    if dur <= 0:
        return []
    out_pat = os.path.join(tmpdir, "f_%04d.jpg")
    _run(["ffmpeg", "-y", "-v", "error",
          "-ss", str(start), "-i", seg_path, "-t", str(dur),
          "-vf", f"fps={SAMPLE_FPS}", "-q:v", "4", out_pat],
         "frame sampling")
    files = sorted(f for f in os.listdir(tmpdir) if f.startswith("f_") and f.endswith(".jpg"))
    return [(start + i / SAMPLE_FPS, os.path.join(tmpdir, f)) for i, f in enumerate(files)]


def _band_array(frame_path):
    """Subtitle band of a frame as a float grayscale numpy array."""
    import numpy as np
    from PIL import Image
    im = Image.open(frame_path).convert("L")
    w, h = im.size
    x0, y0, x1, y1 = SUB_BAND
    band = im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    return np.array(band, dtype=np.float32)


def _ocr_band(frame_path):
    """OCR the subtitle band of one frame. Returns cleaned text or ''.

    Crop + 2x upscale is REQUIRED (without it tesseract misreads badly);
    --psm 7 (single text line). ~0.6s per call (measured).
    """
    from PIL import Image
    im = Image.open(frame_path).convert("L")
    w, h = im.size
    x0, y0, x1, y1 = SUB_BAND
    band = im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    up = band.resize((band.width * 2, band.height * 2), Image.LANCZOS)
    tmp = frame_path + ".ocr.png"
    up.save(tmp)
    try:
        r = subprocess.run(["tesseract", tmp, "stdout", "--psm", "7"],
                           capture_output=True, text=True, timeout=60)
        text = " ".join((r.stdout or "").split())
        # drop obvious OCR junk lines (single stray chars)
        words = [w for w in text.split() if len(_norm(w)) >= 2]
        return " ".join(words)
    except Exception:
        return ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _segment_lines(transcript):
    """Whisper segments overlapping the window, as (start, keys, text)."""
    segs = []
    for s in transcript.get("segments", []):
        text = (s.get("text") or "").strip()
        keys = _keys(text)
        if keys:
            segs.append((float(s["start"]), float(s["end"]), keys, text))
    return segs


def measure_av_offset(seg_path, transcript_path, start, end, seed=0.0,
                      verbose=True):
    """Measure how far the VIDEO is behind the AUDIO (seconds) for the
    clip window [start, end] of a segment.

    Returns (offset, n_matches, details). offset is the median of
    (subtitle_appearance_time - voice_time) over matched subtitle lines;
    positive = video late. Falls back to `seed` when fewer than
    MIN_MATCHES lines could be matched (e.g. no game dialogue in the
    window). `seed` is the carried-forward clock_fix for the channel.
    """
    def log(msg):
        if verbose:
            print(f"[av-align] {msg}", flush=True)

    transcript = json.load(open(transcript_path))
    words = transcript.get("words", [])
    segs = _segment_lines(transcript)
    if not words or not segs:
        log("no transcript words/segments — falling back to seed "
            f"{seed:.2f}s")
        return seed, 0, {"reason": "no-transcript"}

    tmpdir = tempfile.mkdtemp(prefix="avalign_")
    try:
        frames = _sample_frames(seg_path, start, end, tmpdir)
        if len(frames) < 4:
            log(f"only {len(frames)} frames sampled — falling back to seed "
                f"{seed:.2f}s")
            return seed, 0, {"reason": "too-few-frames", "frames": len(frames)}

        # --- change detection on the subtitle band ---
        # A new subtitle line appears as a jump in the band's mean abs
        # diff vs the previous frame. The appearance time is the
        # midpoint of the sampling interval (frame time - 0.25s at 2fps).
        change_points = []  # (time, frame_path)
        prev = None
        for t, path in frames:
            arr = _band_array(path)
            if prev is not None:
                import numpy as np
                diff = float(np.abs(arr - prev).mean())
                if diff > CHANGE_THRESHOLD:
                    change_points.append((t - 0.5 / SAMPLE_FPS, path))
            prev = arr
        log(f"{len(frames)} frames sampled, {len(change_points)} change "
            f"points (threshold {CHANGE_THRESHOLD})")
        if not change_points:
            log("no subtitle changes in window — falling back to seed "
                f"{seed:.2f}s")
            return seed, 0, {"reason": "no-subtitle-changes"}

        # --- group close change points (a subtitle fades in over
        # ~0.5s, so its change points arrive in pairs ~0.5s apart).
        # The appearance time is the FIRST change in the group (when
        # the subtitle starts appearing); the OCR reads the LAST frame
        # in the group (when it is fully visible and most readable).
        # Using the readable frame's time would overstate the offset
        # by the fade-in duration (~0.5-1s).
        groups = []  # [(first_time, last_path)]
        for t, path in change_points:
            if groups and t - groups[-1][0] <= 1.0:
                groups[-1] = (groups[-1][0], path)
            else:
                groups.append((t, path))
        log(f"{len(groups)} fade-in groups (from {len(change_points)} "
            f"change points)")

        # --- OCR the change-point frames, dedupe by text ---
        events = []  # (appearance_time, text, keys)
        last_keys = set()
        for t, path in groups:
            text = _ocr_band(path)
            keys = _keys(text)
            if len(keys) < 2:
                continue  # disappearance / noise / unreadable
            # same line re-OCR'd on a consecutive change (fade-in): skip
            if keys and last_keys and len(keys & last_keys) / max(1, len(keys | last_keys)) >= 0.8:
                continue
            events.append((t, text, keys))
            last_keys = keys
        log(f"{len(events)} subtitle events read: "
            + "; ".join(f"{t:.1f}s {txt[:40]!r}" for t, txt, _ in events[:8]))
        if not events:
            log("no readable subtitles — falling back to seed "
                f"{seed:.2f}s")
            return seed, 0, {"reason": "no-readable-subtitles"}

        # --- match each subtitle line to a Whisper segment ---
        # The segment whose words overlap the subtitle's words, within
        # +/-SEARCH_WINDOW of the seeded guess (T_sub - seed).
        diffs = []
        matched = []
        for t_sub, text, keys in events:
            guess = t_sub - seed
            best = None  # (overlap, -|start-guess|, start, seg_text)
            for s_start, s_end, s_keys, s_text in segs:
                if not (guess - SEARCH_WINDOW <= s_start <= guess + SEARCH_WINDOW):
                    continue
                # token overlap: fraction of subtitle keys found in segment
                hit = sum(1 for k in keys if any(_keys_match(k, sk) for sk in s_keys))
                overlap = hit / max(1, len(keys))
                if overlap < MIN_OVERLAP:
                    continue
                cand = (overlap, -abs(s_start - guess), s_start, s_text)
                if best is None or cand[:2] > best[:2]:
                    best = cand
            if best is not None:
                diff = t_sub - best[2]
                diffs.append(diff)
                matched.append({"t_sub": round(t_sub, 2),
                                "voice_start": round(best[2], 2),
                                "diff": round(diff, 2),
                                "overlap": round(best[0], 2),
                                "subtitle": text[:60],
                                "segment": best[3][:60]})
        log(f"{len(matched)} matched lines, diffs: "
            + ", ".join(f"{m['diff']:.2f}s" for m in matched))
        if len(diffs) < MIN_MATCHES:
            log(f"only {len(diffs)} matches (<{MIN_MATCHES}) — falling "
                f"back to seed {seed:.2f}s")
            return seed, len(diffs), {"reason": "too-few-matches",
                                      "matched": matched, "diffs": diffs}
        offset = float(statistics.median(diffs))
        lo, hi = OFFSET_CLAMP
        offset = max(lo, min(hi, offset))
        log(f"measured offset = {offset:.2f}s (median of {len(diffs)}, "
            f"seed was {seed:.2f}s)")
        return offset, len(diffs), {"matched": matched, "diffs": diffs}
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
