"""Build a 60fps per-frame crop trajectory (ffmpeg sendcmd) from V3 10Hz samples.

Modes:
- smooth (default, facecam): PCHIP interpolation (monotone cubic, no
  overshoot) of the 10Hz samples -> one `crop x` entry per output frame
  at 60Hz. This is the fix for the old 10Hz step function that made
  tracking look like 5 FPS.
- static (doc mode): crop is HELD CONSTANT within each shot and changes
  only at shot cuts (the ClipKaro method — "like a human editor would,
  with no jittery tracking"). Shot boundaries come from ffmpeg scene
  detection (scdet, threshold 0.4); the x0 inside a shot is the median of
  the tracker samples in that shot. This is the fix for documentary
  footage where the camera itself pans/drifts.

Sendcmd format: one line per frame, `t crop x <value>;`. Render with
`fps=60` BEFORE `sendcmd` in the filter chain so sendcmd sees 60Hz
frames: `fps=60,sendcmd=f=track_60.cmd,crop=608:1080:0:0,scale=...`

Adapted from the proven spike: ~/workspace/gta6-clip-research/build_track60_v3.py
"""
import argparse
import json
import subprocess

import numpy as np
from scipy.interpolate import PchipInterpolator

FPS_OUT = 60


def shot_boundaries(video_path, threshold=0.4):
    """Shot-cut times (seconds) from ffmpeg scene detection. Returns a
    sorted list starting with 0.0. Empty list on failure (caller falls back
    to tracker-jump detection)."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", video_path,
                        "-vf", f"select='gt(scene,{threshold})',showinfo",
                        "-f", "null", "-"],
                       capture_output=True, text=True)
    times = [0.0]
    for line in (r.stderr or "").splitlines():
        if "showinfo" in line and "pts_time:" in line:
            try:
                t = float(line.split("pts_time:")[1].split()[0])
                times.append(round(t, 3))
            except ValueError:
                pass
    return sorted(set(times))


def build_smooth(samples, crop_w, src_w):
    t = np.array([s[0] for s in samples], dtype=float)
    x = np.array([s[1] for s in samples], dtype=float)
    dur = t[-1] + 0.1
    pchip = PchipInterpolator(t, x, extrapolate=True)
    n_frames = int(np.ceil(dur * FPS_OUT))
    t60 = np.arange(n_frames) / FPS_OUT
    x60 = np.clip(pchip(t60), 0, src_w - crop_w)
    return t60, x60


def build_static(samples, crop_w, src_w, cuts):
    """Hold the median x0 within each shot; change only at cuts."""
    t = np.array([s[0] for s in samples], dtype=float)
    x = np.array([s[1] for s in samples], dtype=float)
    dur = t[-1] + 0.1
    n_frames = int(np.ceil(dur * FPS_OUT))
    t60 = np.arange(n_frames) / FPS_OUT
    bounds = list(cuts) + [dur]
    x60 = np.empty(n_frames)
    prev = float(x[0])
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        seg = x[(t >= a) & (t < b)]
        med = float(np.median(seg)) if len(seg) else prev
        med = max(0.0, min(src_w - crop_w, med))
        prev = med
        x60[(t60 >= a) & (t60 < b)] = med
    return t60, x60


def main():
    ap = argparse.ArgumentParser(description="Build 60Hz sendcmd crop track from V3 samples.")
    ap.add_argument("--src-json", required=True, help="track_v3_x0.json (t, x0) samples")
    ap.add_argument("--out-cmd", required=True, help="output sendcmd file")
    ap.add_argument("--out-json", default=None, help="optional output (t, x0) 60Hz JSON")
    ap.add_argument("--crop-w", type=int, default=608)
    ap.add_argument("--src-w", type=int, default=1920)
    ap.add_argument("--mode", default="smooth", choices=["smooth", "static"],
                    help="smooth=PCHIP (facecam), static=held per shot (doc)")
    ap.add_argument("--video", default=None,
                    help="video file for shot detection (static mode)")
    ap.add_argument("--scene-threshold", type=float, default=0.4)
    a = ap.parse_args()

    samples = json.load(open(a.src_json))
    print(f"samples={len(samples)} t range {samples[0][0]:.3f}..{samples[-1][0]:.3f}")
    if a.mode == "smooth":
        t60, x60 = build_smooth(samples, a.crop_w, a.src_w)
    else:
        cuts = [0.0]
        if a.video:
            cuts = shot_boundaries(a.video, a.scene_threshold)
            print(f"shot cuts: {len(cuts) - 1} detected ({cuts[:8]}...)")
        if len(cuts) < 2:  # no cuts found: fall back to tracker jumps > 120px
            t = np.array([s[0] for s in samples])
            x = np.array([s[1] for s in samples])
            jumps = t[1:][np.abs(np.diff(x)) > 120]
            cuts = sorted(set([0.0] + [round(float(j), 3) for j in jumps]))
            print(f"no scdet cuts; fell back to {len(cuts) - 1} tracker-jump cuts")
        t60, x60 = build_static(samples, a.crop_w, a.src_w, cuts)

    d = np.abs(np.diff(x60))
    print(f"60fps frames={len(t60)}")
    print(f"per-frame |dx|: max={d.max():.2f}px mean={d.mean():.2f}px "
          f"steps>2px: {(d > 2).sum()} steps>5px: {(d > 5).sum()}")
    print(f"x range {x60.min():.1f}..{x60.max():.1f}")
    with open(a.out_cmd, "w") as f:
        for ti, xi in zip(t60, x60):
            f.write(f"{ti:.6f} crop x {xi:.1f};\n")
    if a.out_json:
        json.dump([[round(float(ti), 6), round(float(xi), 1)]
                   for ti, xi in zip(t60, x60)], open(a.out_json, "w"))
    print(f"wrote {a.out_cmd} ({len(t60)} entries)")


if __name__ == "__main__":
    main()
