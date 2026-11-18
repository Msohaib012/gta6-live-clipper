#!/usr/bin/env python3
"""Package gta6-live-clipper-final.zip — source-only, explicit file list.
Excludes: venvs, models cache, captures, work dirs, clips, test renders,
old ZIPs, __pycache__, static/previews/_work, static/layout_previews."""
import hashlib
import json
import os
import zipfile

SRC = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SRC, "gta6-live-clipper-final.zip")

FILES = [
    # code
    "clip_live.py", "capture.py", "transcribe.py", "transliterate.py",
    "render_short.py", "captions.py", "layout.py", "layout_border.py",
    "positions.py", "ytdlp_cmd.py",
    "preview_positions.py", "preview_styles.py", "track_v3_stable.py",
    "build_track60.py", "post_queue.py", "stream_discovery.py", "chat.py",
    "games.py", "settings.py", "vod.py", "dashboard.py",
    # dashboard + docs
    "dashboard.html", "README.md", "BUILD-REPORT.md", "SPEED-REPORT.md",
    "FINAL-REPORT.md",
    # config
    "requirements.txt", "requirements-facecam.txt", "channels.json",
    "settings.json",
    # model (facecam detection — 1.1 MB; without it detection falls back)
    "models/blaze_face_full_range.tflite",
    # roman fix evidence
    "roman-fix/ROMAN-FIX-REPORT.md", "roman-fix/transcript_autosubs.json",
]
DIRS = [
    "fonts",            # all .ttf
    "assets",           # sample_bg.jpg
    "static/previews",  # 25 style mp4s + index.json (NOT _work)
    "static/positions", # 10 position stills + index.json
    "static/fonts",     # Anton (dashboard display font, self-hosted)
]


def collect():
    out = []
    for f in FILES:
        p = os.path.join(SRC, f)
        assert os.path.isfile(p), f"MISSING: {f}"
        out.append(f)
    for d in DIRS:
        dp = os.path.join(SRC, d)
        assert os.path.isdir(dp), f"MISSING DIR: {d}"
        for root, dirs, names in os.walk(dp):
            dirs[:] = [x for x in dirs if x not in ("_work", "__pycache__")]
            for n in sorted(names):
                if n.endswith(".pyc"):
                    continue
                full = os.path.join(root, n)
                out.append(os.path.relpath(full, SRC))
    return sorted(set(out))


def main():
    files = collect()
    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in files:
            if f == "FINAL-REPORT.md":
                # A file cannot contain its own container's hash: the copy
                # of this report inside the ZIP states where the real
                # SHA-256 lives (the copy next to the ZIP); the project-dir
                # copy states the real hash of the shipped ZIP.
                import re
                text = open(os.path.join(SRC, f), encoding="utf-8").read()
                text = re.sub(
                    r"\*\*ZIP:\*\* `~/workspace/gta6-live-clipper/gta6-live-clipper-final\.zip` — [^\n]*",
                    "**ZIP:** `gta6-live-clipper-final.zip` — file count, size and SHA-256 are in the copy of this report next to the ZIP (a file cannot contain its own container's hash).",
                    text, count=1)
                text = re.sub(
                    r"- \*\*Size:\*\* [^\n]*\n- \*\*SHA-256:\*\* `[0-9a-f]+`",
                    "- **Size / SHA-256:** see the copy of this report next to the ZIP.",
                    text, count=1)
                z.writestr(f, text)
            else:
                z.write(os.path.join(SRC, f), f)
    h = hashlib.sha256(open(OUT, "rb").read()).hexdigest()
    print(f"ZIP: {OUT}")
    print(f"files: {len(files)}")
    print(f"bytes: {os.path.getsize(OUT)}")
    print(f"sha256: {h}")
    print("--- files ---")
    for f in files:
        print(" ", f)


if __name__ == "__main__":
    main()
