"""Generate the Positions-page preview stills (FINAL build).

One still per facecam position + per caption position + one combined
'auto' still, all drawn on a REAL GTA frame with a REAL facecam crop
pasted in — not mockups. Run once; the stills ship in the ZIP under
static/positions/ and the dashboard Positions page serves them.

Sources (this VM):
- GTA frame: ~/workspace/gta6-clip-research/gtav_capture.mp4 @ 12s
- Facecam crop: ~/workspace/gta6-live-clipper/pubg_capture2.mp4 — the
  Twitch Halifax stream's facecam, source rect [0, 460, 440, 240]
  (measured by layout.py on the PUBG demo clip).

Usage: python preview_positions.py
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from positions import build_position_previews  # noqa: E402

GTA_SRC = os.path.expanduser("~/workspace/gta6-clip-research/gtav_capture.mp4")
PUBG_SRC = os.path.join(BASE, "pubg_capture2.mp4")
OUT_DIR = os.path.join(BASE, "static", "positions")
TMP = "/tmp/pos_preview"


def _extract(src, t, out_path, crop=None):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # -ss AFTER -i (accurate seek): raw HLS captures have broken
    # timestamps, so fast pre-input seeking can land on a bad GOP.
    vf = ["-i", src, "-ss", str(t), "-frames:v", "1"]
    if crop:
        x, y, w, h = crop
        vf += ["-vf", f"crop={w}:{h}:{x}:{y}"]
    vf += [out_path]
    r = subprocess.run(["ffmpeg", "-y", "-v", "error"] + vf,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"frame extract failed: {r.stderr[-500:]}")
    return out_path


def main():
    from PIL import Image
    os.makedirs(TMP, exist_ok=True)
    base_path = _extract(GTA_SRC, 12, os.path.join(TMP, "gta_frame.jpg"))
    fc_path = _extract(PUBG_SRC, 90, os.path.join(TMP, "facecam.jpg"),
                       crop=(0, 460, 440, 240))
    base = Image.open(base_path)
    fc = Image.open(fc_path)
    print(f"[positions] base frame {base.size}, facecam crop {fc.size}")
    out = build_position_previews(base, fc, OUT_DIR)
    for k, p in sorted(out.items()):
        print(f"[positions] {k}: {p} ({os.path.getsize(p)//1024} KB)")
    print(f"[positions] done — {len(out)} stills in {OUT_DIR}")


if __name__ == "__main__":
    main()
