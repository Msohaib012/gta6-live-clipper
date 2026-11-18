"""Static-border facecam pre-check — ported from ClipKontrast
(anistroz/ClipKontrast, studio/facecam.py, MIT licence) into the GTA 6
Live Clipper, minus the AI (`ask`) path (our Gemini is text-only).

A gaming stream is a game with a small webcam picture in one corner.
The webcam is found by the one thing a webcam always has and a game
almost never has: a straight border that stays in the same place in
every frame, with a nearly-still picture inside it. This module finds
borders from gradient profiles, then validates candidate rectangles
by stillness — numpy + ffmpeg only, no model file, ~1-4s.

Role here: a FAST pre-check before layout.detect_facecam() (which needs
cv2 + the MediaPipe BlazeFace model). render_short.py calls this first
on the clip; on a high-confidence hit the MediaPipe pass is skipped
entirely. On a miss (borderless facecam, static game, no facecam) the
existing path runs unchanged — zero regression risk.

Differences from the ClipKontrast original:
- No `ask` path (no vision model here): _frame_jpeg / _asked / _seen /
  _overlap removed; detect() takes no callback.
- Aspect range tightened to 1.0-2.5 (our facecam range) instead of
  0.55-3.20, so a tall phone-shaped or banner-shaped panel cannot be
  accepted as a webcam.
- start/dur: the clip is a [start, end] slice of a longer capture, so
  frames are sampled across the SLICE, not the whole file.
- Only conf == "high" counts as a hit (a "low" box is probably a HUD
  panel); the caller treats anything else as no detection.
- Return contract matches layout.detect_facecam() so choose_layout()
  consumes it unchanged: {"rect", "method": "static-border",
  "face": None, "samples", "busy": None}.

Limits (from the original): needs a straight, static, high-contrast
border — fails on borderless/rounded facecams and on facecams that
move mid-clip; needs the game to move more than the webcam interior
(static menus/loading screens fail); a static HUD panel CAN pass the
stillness test (there is no face confirmation here — that is what the
MediaPipe fallback is for). One box only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

W, H = 320, 180            # the picture is looked at small: only the lines matter
MAX_FRAMES = 16            # capped from ClipKontrast's 24: measured 11.0s on a
                           # corrupt live capture at 24 frames (VM); 16 is ~7s
MIN_SIZE = 0.12            # the smallest a webcam may be, as a part of the width or height
MAX_SIZE = 0.60            # and the largest
ASPECT_MIN = 1.0           # tightened from ClipKontrast's 0.55 (our facecam range)
ASPECT_MAX = 2.0           # tightened from ClipKontrast's 3.20; facecams
                           # are ~16:9 (1.78) at widest — the PUBG HUD
                           # false positive measured 2026-11-18 was 2.49:1
STILL_FRAC = 0.35          # how much of the game's own movement a webcam picture may have
STILL_MIN = 6.0            # and the floor, for a game that itself is almost still
STILL_SHARE = 0.30         # at most this much of a webcam picture may be moving
EDGE_FLUSH = 0.02          # a side this close to the frame's own edge is the edge
EDGE = 2.2                 # how much brighter than the average line a border must be


def _frames(path: str | Path, count: int = MAX_FRAMES,
            start: float = 0.0, dur: float | None = None) -> np.ndarray:
    """The video (or the [start, start+dur] slice of it) as a few grey
    frames, spread through it."""
    try:
        d = dur if dur else _duration(path)
        rate = max(count / d, 0.02) if d > 0 else 0.2
        args = ["ffmpeg", "-v", "error"]
        if start:
            args += ["-ss", f"{start:.3f}"]
        if dur:
            args += ["-t", f"{dur:.3f}"]
        proc = subprocess.run(
            args + ["-i", str(path),
                    "-vf", f"fps={rate:.4f},scale={W}:{H},format=gray",
                    "-frames:v", str(count), "-f", "rawvideo", "-"],
            capture_output=True, timeout=180)
    except Exception:  # noqa: BLE001 - no ffmpeg or an unreadable file: no facecam, not a failure
        return np.zeros((0, H, W), dtype=np.uint8)
    raw = proc.stdout or b""
    size = W * H
    n = len(raw) // size
    if n < 4:
        return np.zeros((0, H, W), dtype=np.uint8)
    return np.frombuffer(raw[:n * size], dtype=np.uint8).reshape(n, H, W)


def _duration(path: str | Path) -> float:
    try:
        proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", str(path)],
                              capture_output=True, text=True, timeout=60)
        return float((proc.stdout or "").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _profiles(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(vertical lines, horizontal lines): how much changes at each column and each row.

    The median over the frames is used, so one busy moment cannot invent a line."""
    if len(frames) < 4:
        return np.zeros(W), np.zeros(H)
    f = frames.astype(np.int16)
    gx = np.median(np.abs(np.diff(f, axis=2)).sum(axis=1), axis=0)   # change across each column boundary
    gy = np.median(np.abs(np.diff(f, axis=1)).sum(axis=2), axis=0)   # change across each row boundary
    return gx, gy


def _peaks(profile: np.ndarray, limit: int) -> list[tuple[int, float]]:
    """The lines that stand out: (where, how much)."""
    if len(profile) < 3:
        return []
    med = float(np.median(profile)) or 1.0
    out = []
    for i in range(1, len(profile) - 1):
        if profile[i] >= profile[i - 1] and profile[i] > profile[i + 1] and profile[i] > EDGE * med:
            out.append((i, float(profile[i] / med)))
    return sorted(out, key=lambda p: -p[1])[:limit]


def _size(path: str | Path) -> tuple[int, int]:
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=width,height", "-of", "default=nw=1", str(path)],
                          capture_output=True, text=True, timeout=60)
    w = h = 0
    for line in (proc.stdout or "").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        try:
            if k.strip() == "width":
                w = int(v.strip())
            elif k.strip() == "height":
                h = int(v.strip())
        except ValueError:
            continue
    return w, h


def _stillness(frames: np.ndarray) -> np.ndarray:
    """How much each pixel changes over the frames.

    A webcam picture barely changes: a person sits in a room and shifts a little. The game changes every
    frame. This is the one thing that tells a webcam apart from the game's own panels, which have straight
    borders too and are drawn over the game with the game showing through them."""
    return frames.astype(np.float32).std(axis=0)


def _plausible(x0, y0, x1, y1, still, light, game, share=STILL_SHARE):
    """Could this rectangle be a webcam picture? Judged on its shape, its stillness and its light.

    Returns how much it moves, or None."""
    bw, bh = (x1 - x0) / W, (y1 - y0) / H
    if not (MIN_SIZE <= bw <= MAX_SIZE and MIN_SIZE <= bh <= MAX_SIZE):
        return None
    aspect = (bw * W) / max(1e-6, bh * H)
    if not (ASPECT_MIN <= aspect <= ASPECT_MAX):
        return None
    a, b, c, d = int(x0), int(y0), int(x1), int(y1)
    inside = still[b:d, a:c]
    if not inside.size:
        return None
    cut = max(STILL_MIN, STILL_FRAC * game)
    moving = float(np.median(inside))
    if moving > cut:
        return None                    # it changes with the game: it is the game
    if float((inside > cut).mean()) > share:
        return None                    # a good part of it moves: the game is showing through
    if float(light[b:d, a:c].mean()) < 0.30 * float(light.mean()):
        return None                    # a black hole, not a picture
    return moving


def _local(vpeaks, hpeaks, still, light):
    """Every rectangle the strong straight lines can make, that still looks like a camera.

    Returns them best first: the one whose weakest border is the strongest, and of those the bigger."""
    game = float(np.median(still))
    xs = sorted({0.0, float(W)} | {float(x) for x, _ in vpeaks[:10]})
    ys = sorted({0.0, float(H)} | {float(y) for y, _ in hpeaks[:10]})
    vs = {float(x): s for x, s in vpeaks}
    hs = {float(y): s for y, s in hpeaks}
    out = []
    for i, x0 in enumerate(xs):
        wx = [vs[v] for v in (x0,) if v in vs]
        for x1 in xs[i + 1:]:
            wscore = wx + [vs[v] for v in (x1,) if v in vs]
            for j, y0 in enumerate(ys):
                hy0 = [hs[v] for v in (y0,) if v in hs]
                for y1 in ys[j + 1:]:
                    score = wscore + hy0 + [hs[v] for v in (y1,) if v in hs]
                    if len(score) < 2:
                        continue       # a box needs two borders of its own, or it is just the frame
                    moving = _plausible(x0, y0, x1, y1, still, light, game)
                    if moving is None:
                        continue
                    key = (min(score), (x1 - x0) * (y1 - y0))
                    out.append({"key": key, "x": x0, "y": y0, "x1": x1, "y1": y1,
                                "score": round(min(score), 2), "moving": moving})
    return sorted(out, key=lambda c: c["key"], reverse=True)


def _full_frame(path: str | Path, start: float = 0.0,
                dur: float | None = None) -> np.ndarray | None:
    """One grey frame at full size, from the middle of the (slice of the) video."""
    w, h = _size(path)
    if not w or not h:
        return None
    mid = start + (_duration(path) / 2.0 if dur is None else dur / 2.0)
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, mid):.1f}", "-i", str(path),
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, timeout=120).stdout or b""
    except Exception:  # noqa: BLE001
        return None
    if len(raw) < w * h:
        return None
    return np.frombuffer(raw[:w * h], dtype=np.uint8).reshape(h, w).astype(np.float32)


def _best(profile: np.ndarray, approx: float, span: float) -> float:
    """The strongest change near a guessed line."""
    lo, hi = max(0, int(approx - span)), min(len(profile), int(approx + span) + 1)
    seg = profile[lo:hi]
    return float(lo + seg.argmax()) if len(seg) else float(approx)


def _snap(path: str | Path, box, w: int, h: int, span: float = 0.0,
          start: float = 0.0, dur: float | None = None):
    """A box on the small grid, put onto the borders that are really in the picture."""
    frame = _full_frame(path, start=start, dur=dur)
    if frame is None:
        return None
    fh, fw = frame.shape
    col = np.abs(np.diff(frame, axis=1)).sum(axis=0)
    row = np.abs(np.diff(frame, axis=0)).sum(axis=1)
    spread = max(4.0, span * max(fw, fh), 1.5 * max(fw / W, fh / H))

    def line(profile, approx, edge, extent):
        if abs(approx - edge) <= EDGE_FLUSH * extent:
            return float(edge)
        return _best(profile, approx, spread)

    x0, y0, x1, y1 = box
    return (line(col, x0 * fw / W, 0, fw), line(row, y0 * fh / H, 0, fh),
            line(col, x1 * fw / W, fw, fw), line(row, y1 * fh / H, fh, fh))


def _corner(x0, y0, x1, y1, w, h) -> str:
    """What part of the frame the box is on: its corner, its edge, or the middle."""
    near = EDGE_FLUSH
    left, right = x0 <= w * near, x1 >= w * (1 - near)
    top, bottom = y0 <= h * near, y1 >= h * (1 - near)
    if left and top:
        return "top-left"
    if right and top:
        return "top-right"
    if left and bottom:
        return "bottom-left"
    if right and bottom:
        return "bottom-right"
    if left or right:
        return "right" if right else "left"
    if top or bottom:
        return "bottom" if bottom else "top"
    return "middle"


def _finish(path: str | Path, found: dict, w: int, h: int, still: np.ndarray,
            start: float = 0.0, dur: float | None = None) -> dict | None:
    """The box in source pixels, with how sure it is that this is really a camera."""
    snapped = _snap(path, (found["x"], found["y"], found["x1"], found["y1"]), w, h,
                    start=start, dur=dur)
    if not snapped:
        return None
    x0, y0, x1, y1 = snapped
    if x1 - x0 < w * MIN_SIZE or y1 - y0 < h * MIN_SIZE:
        return None
    inside = still[int(found["y"]):int(found["y1"]), int(found["x"]):int(found["x1"])]
    game = float(np.median(still))
    moving = float(np.median(inside)) if inside.size else 99.0
    # sure of it when the picture inside hardly moves while the game around it is racing
    conf = "high" if moving <= max(3.0, 0.25 * game) else "low"
    return {"x": int(round(x0)), "y": int(round(y0)), "w": int(round(x1 - x0)), "h": int(round(y1 - y0)),
            "corner": _corner(x0, y0, x1, y1, w, h), "score": found.get("score", 0.0), "conf": conf}


def detect_facecam_border(video_path, src_w=None, src_h=None,
                          start=0.0, dur=None):
    """The facecam's box in source pixels, or None.

    video_path: the capture file; start/dur select the clip slice inside
    it (the live loop renders [start, end] of a longer capture).
    Returns the layout.detect_facecam() contract so choose_layout()
    consumes it unchanged — {"rect", "method": "static-border",
    "face": None, "samples", "busy": None} — or None. Only a
    high-confidence box counts; anything else falls through to the
    MediaPipe path in the caller.
    """
    frames = _frames(video_path, start=start, dur=dur)
    if len(frames) < 4:
        return None
    w, h = (src_w, src_h) if (src_w and src_h) else _size(video_path)
    if not w or not h:
        return None
    still = _stillness(frames)
    light = frames.astype(np.float32).mean(axis=0)
    vx, hy = _profiles(frames)
    vpeaks, hpeaks = _peaks(vx, 40), _peaks(hy, 40)
    found = _local(vpeaks, hpeaks, still, light) if (vpeaks and hpeaks) else []
    if not found:
        return None
    res = _finish(video_path, found[0], w, h, still, start=start, dur=dur)
    if res is None or res.get("conf") != "high":
        return None
    return {"rect": (res["x"], res["y"], res["w"], res["h"]),
            "method": "static-border", "face": None,
            "samples": len(frames), "busy": None}
