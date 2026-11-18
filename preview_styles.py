"""Style previews — real animated preview renders for the Styles page.

CapCut-style: every caption style gets a short LOOPING preview video the
user watches and picks from. These are REAL renders (the same
build_caption_track the live loop uses), not mockups — a fixed sample
sentence over a real GTA V frame, 4 seconds, 30fps.

Run:  python preview_styles.py [--rebuild] [--styles neon,hype]
Output: static/previews/<style>.mp4 + static/previews/index.json
The dashboard serves them at /static/previews/<style>.mp4.
"""
import argparse
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from captions import build_caption_track, CAP_Y  # noqa: E402

PREVIEW_DIR = os.path.join(BASE, "static", "previews")
ASSETS_DIR = os.path.join(BASE, "assets")
SAMPLE_DUR = 4.0
SAMPLE_FPS = 30

# Sample sentence (2 chunks, 3 words max each — the real chunking rule):
# "WATCH THIS PLAY" (0.2-1.7s) + "THAT WAS INSANE" (2.0-3.5s)
SAMPLE_WORDS = [
    {"start": 0.20, "end": 0.65, "word": "WATCH"},
    {"start": 0.70, "end": 1.10, "word": "THIS"},
    {"start": 1.15, "end": 1.70, "word": "PLAY"},
    {"start": 2.00, "end": 2.45, "word": "THAT"},
    {"start": 2.50, "end": 2.90, "word": "WAS"},
    {"start": 2.95, "end": 3.50, "word": "INSANE"},
]


def _sample_transcript(work_dir):
    p = os.path.join(work_dir, "sample_transcript.json")
    json.dump({"segments": [{"start": 0.2, "end": 1.7, "text": "WATCH THIS PLAY"},
                            {"start": 2.0, "end": 3.5, "text": "THAT WAS INSANE"}],
               "words": SAMPLE_WORDS, "duration": SAMPLE_DUR,
               "detected_language": "en", "caption_script": "latin"},
              open(p, "w"), indent=1)
    return p


def _sample_bg():
    """One real GTA V frame (720x1280) as the preview background.
    Source: ~/workspace/gta6-clip-research/gtav_capture.mp4 (our own test
    capture). Falls back to a dark gradient if the capture is missing."""
    bg = os.path.join(ASSETS_DIR, "sample_bg.jpg")
    if os.path.exists(bg):
        return bg
    os.makedirs(ASSETS_DIR, exist_ok=True)
    src = os.path.expanduser("~/workspace/gta6-clip-research/gtav_capture.mp4")
    if os.path.exists(src):
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "5", "-i", src,
                            "-frames:v", "1",
                            "-vf", "crop=608:1080:656:0,scale=720:1280:flags=lanczos",
                            bg], capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(bg):
            return bg
    # fallback: solid dark frame (PIL)
    from PIL import Image
    Image.new("RGB", (720, 1280), (12, 18, 26)).save(bg)
    return bg


def build_preview(style, work_dir, fonts_dir=None):
    """Render one style's preview mp4. Returns the output path."""
    fonts_dir = fonts_dir or os.path.join(BASE, "fonts")
    os.makedirs(work_dir, exist_ok=True)
    tp = _sample_transcript(work_dir)
    track_mov, _man = build_caption_track(tp, 0.0, SAMPLE_DUR, style,
                                          work_dir, fonts_dir, fps=SAMPLE_FPS)
    bg = _sample_bg()
    out = os.path.join(PREVIEW_DIR, f"{style}.mp4")
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    r = subprocess.run(["ffmpeg", "-y", "-v", "error",
                        "-loop", "1", "-t", str(SAMPLE_DUR), "-i", bg,
                        "-i", track_mov,
                        "-filter_complex",
                        f"[1:v]format=rgba[cap];[0:v][cap]overlay=0:{CAP_Y}[v]",
                        "-map", "[v]", "-r", str(SAMPLE_FPS),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-preset", "veryfast", "-crf", "20",
                        "-movflags", "+faststart", out],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"preview encode failed for {style}:\n{r.stderr[-800:]}")
    return out


def style_meta():
    """Style list + descriptions for the dashboard (single source of truth
    lives in captions.STYLE_META; fall back to plain names)."""
    try:
        from captions import STYLE_META
        return STYLE_META
    except Exception:
        from captions import STYLES
        return {s: {"name": s, "desc": "", "source": ""} for s in STYLES}


def build_all(styles=None, rebuild=False):
    work_dir = os.path.join(BASE, "static", "previews", "_work")
    os.makedirs(work_dir, exist_ok=True)
    meta = style_meta()
    todo = styles or list(meta)
    index = []
    for s in todo:
        if s not in meta:
            print(f"[preview] unknown style {s!r} — skipping")
            continue
        out = os.path.join(PREVIEW_DIR, f"{s}.mp4")
        if os.path.exists(out) and not rebuild:
            print(f"[preview] {s} exists — skipping (use --rebuild to redo)")
        else:
            out = build_preview(s, os.path.join(work_dir, s))
            print(f"[preview] {s}: {out} ({os.path.getsize(out) / 1e3:.0f} KB)")
        m = dict(meta[s])
        m.update({"id": s, "preview": f"/static/previews/{s}.mp4"})
        index.append(m)
    json.dump(index, open(os.path.join(PREVIEW_DIR, "index.json"), "w"), indent=1)
    print(f"[preview] index.json: {len(index)} styles")
    return index


def main():
    ap = argparse.ArgumentParser(description="Build animated style previews.")
    ap.add_argument("--styles", default=None, help="comma-separated style ids (default: all)")
    ap.add_argument("--rebuild", action="store_true", help="rebuild even if the preview exists")
    a = ap.parse_args()
    styles = [s.strip() for s in a.styles.split(",")] if a.styles else None
    build_all(styles, rebuild=a.rebuild)


if __name__ == "__main__":
    main()
