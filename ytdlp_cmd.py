"""Frozen-exe compatibility helpers (PyInstaller onedir).

Two things break when this project runs as a Windows .exe instead of a
source checkout, and both are fixed here in one place:

1. yt-dlp: source runs invoke it as [sys.executable, "-m", "yt_dlp", ...]
   (python is real). In the frozen exe sys.executable IS the .exe itself —
   it has no "-m" support. So the exe ships the official standalone
   yt-dlp.exe (downloaded in the GitHub Actions workflow from the yt-dlp
   release and --add-binary'd into the bundle) and frozen runs call that
   instead. Same CLI, same flags, same behaviour.
2. ffmpeg / ffprobe: every module calls them bare ("ffmpeg", "ffprobe").
   PyInstaller puts the --add-binary files in _internal (sys._MEIPASS),
   which is NOT on PATH, so Windows cannot resolve them. When frozen,
   prepend sys._MEIPASS to PATH at startup so the bare calls find them.
"""
import os
import sys


def ytdlp_cmd():
    """Command prefix for yt-dlp: [exe, -m, yt_dlp] in source runs,
    [bundled yt-dlp.exe] in the frozen exe."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return [os.path.join(sys._MEIPASS, "yt-dlp.exe"),
                "--no-check-certificates"]
    return [sys.executable, "-m", "yt_dlp", "--no-check-certificates"]


def ensure_frozen_binaries_on_path():
    """Frozen exe only: put the bundle dir (_internal, where ffmpeg.exe /
    ffprobe.exe / yt-dlp.exe live) on PATH. No-op in source runs."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        os.environ["PATH"] = (sys._MEIPASS + os.pathsep +
                              os.environ.get("PATH", ""))
