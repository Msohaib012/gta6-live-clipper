"""Live capture helpers — yt-dlp Twitch 1080p60, rolling segments.

Proven on this VM (spikes 2026-11-17):
- Twitch live max = 1080p60 (`1080p60__source_`, ~7-9 Mbps). No 1440p/4K live.
- Capture runs at 1.01-1.09x real-time — Twitch is NOT proxy-throttled.
- yt-dlp must be killed after the segment length (it records forever on a
  live stream); the leftover `.part` file IS playable — rename it to .mp4.
  (The 2026-11-17 GTAV spike recovered an 81 MB .part as a full 98s capture.)
- `--no-check-certificates` is needed on this VM (proxy MITM self-signed
  cert). On a home PC it is harmless; keep it so the same command works
  everywhere.
- ffprobe's fps metadata LIES on live captures (says 30fps when the frames
  are really 60fps). Verify by frame count, never trust it. Downstream code
  never trusts container timestamps either (see render_short.py).

Cross-platform kill: subprocess + a threading.Timer (works on Windows and
Linux — no `timeout` command needed).
"""
import glob
import os
import subprocess
import sys
import threading
import time

from ytdlp_cmd import ytdlp_cmd

FORMAT = "1080p60__source_"  # Twitch source quality (kept as default alias)

# Site-aware format selectors (fix 2026-11-18: the Twitch-only
# "1080p60__source_" id does not exist on YouTube, so is_live() and
# capture_segment() failed every YouTube live stream with
# "Requested format is not available").
TWITCH_FORMAT = "1080p60__source_"
GENERIC_FORMAT = "bestvideo[height<=1080][fps<=60]+bestaudio/best[height<=1080]/best"


def format_for(channel_url):
    """yt-dlp -f selector for this site: Twitch keeps its source format,
    everything else (YouTube / Kick / generic) uses best <=1080p."""
    if "twitch.tv" in (channel_url or ""):
        return TWITCH_FORMAT
    return GENERIC_FORMAT


def is_live(channel_url, timeout=45):
    """True if the channel is live right now.

    Twitch: the source format id exists only while live, so the format
    probe is the liveness check. YouTube/generic: the format exists for
    ANY video (live or not), so the format probe is meaningless there —
    use the metadata is_live field instead. The metadata probe hits
    YouTube's bot check intermittently on this VM, so retry it up to
    3 times; True if any attempt says live.
    """
    url = channel_url or ""
    if "twitch.tv" in url:
        try:
            r = subprocess.run(
                ytdlp_cmd() + ["-f", TWITCH_FORMAT, "--no-download", url],
                capture_output=True, text=True, timeout=timeout)
            return r.returncode == 0
        except Exception:
            return False
    for _ in range(3):
        try:
            r = subprocess.run(
                ytdlp_cmd() + ["--no-download", "--print", "%(is_live)s", url],
                capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and r.stdout.strip().lower() == "true":
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _kill_tree(proc):
    """Kill yt-dlp AND the ffmpeg child it spawns for the HLS download.
    Killing only the parent orphans the ffmpeg child, which then records
    forever (found in testing 2026-11-17: an orphaned ffmpeg wrote 485 MB
    after the parent was killed). Linux: new process group + killpg.
    Windows: CREATE_NEW_PROCESS_GROUP + taskkill /T (kills the tree)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def capture_segment(channel_url, out_path, seconds, log_path=None):
    """Record `seconds` of a live stream to out_path.

    Returns the playable file path (out_path, or the recovered .part).
    Raises RuntimeError if nothing usable was recorded.
    """
    part_path = out_path + ".part"
    for p in (out_path, part_path):
        if os.path.exists(p):
            os.remove(p)
    cmd = ytdlp_cmd() + ["-f", format_for(channel_url), "-o", out_path, channel_url]
    t0 = time.time()
    # Retry loop: YouTube's bot check fires intermittently on this VM even
    # while the stream is live (2026-11-18: same URL passed once, failed
    # minutes later) — a failed extraction is usually transient, so retry
    # up to 3 times before giving up the round.
    final = None
    for attempt in range(1, 4):
        for p in (out_path, part_path):
            if os.path.exists(p):
                os.remove(p)
        logf = open(log_path, "w") if log_path else subprocess.DEVNULL
        print(f"[capture] recording {seconds}s -> {out_path} (attempt {attempt}/3)",
              flush=True)
        popen_kw = {}
        if os.name == "nt":
            popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kw["start_new_session"] = True
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, **popen_kw)
        timer = threading.Timer(seconds, _kill_tree, args=(proc,))
        timer.start()
        try:
            proc.wait()
        finally:
            timer.cancel()
            if log_path:
                logf.close()
        # yt-dlp writes .part while recording; on kill it may not rename it.
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            final = out_path
            break
        elif os.path.exists(part_path) and os.path.getsize(part_path) > 0:
            os.rename(part_path, out_path)
            final = out_path
            print(f"[capture] recovered .part -> {out_path}", flush=True)
            break
        print(f"[capture] attempt {attempt} recorded nothing, retrying in 10s",
              flush=True)
        time.sleep(10)
    if final is None:
        raise RuntimeError(f"nothing recorded for {channel_url} after 3 attempts "
                           f"(see {log_path or 'yt-dlp output'})")
    dur = media_duration(final)
    print(f"[capture] done in {time.time() - t0:.0f}s, file "
          f"{os.path.getsize(final) / 1e6:.1f} MB, duration ~{dur:.0f}s", flush=True)
    return final


def media_duration(path):
    """Best-effort duration via ffprobe (container metadata may lie about fps,
    but duration is usable). Returns 0.0 on failure."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of",
                            "default=noprint_wrappers=1:nokey=1", path],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def frame_count(path, seconds=10.0):
    """Count decoded frames in the first `seconds` — the honest fps check
    (ffprobe fps lies on live captures). Returns (frames, fps)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-t", str(seconds),
                        "-i", path, "-map", "0:v", "-f", "null", "-"],
                       capture_output=True, text=True)
    # ffmpeg's progress goes to stderr; count via framecrc instead (reliable)
    r = subprocess.run(["ffmpeg", "-v", "error", "-t", str(seconds),
                        "-i", path, "-map", "0:v", "-f", "framecrc", "-"],
                       capture_output=True, text=True)
    n = r.stdout.count("frame,")
    return n, (n / seconds if seconds else 0.0)


def prune_segments(buffer_dir, keep=4):
    """Keep the newest `keep` segment files, delete older ones."""
    files = sorted(glob.glob(os.path.join(buffer_dir, "seg_*.mp4")),
                   key=os.path.getmtime)
    for f in files[:-keep]:
        try:
            os.remove(f)
            print(f"[capture] pruned old segment {os.path.basename(f)}")
        except OSError:
            pass


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Record one live segment (test helper).")
    ap.add_argument("channel", help="Twitch channel URL, e.g. https://www.twitch.tv/deansocool")
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    print("live:", is_live(a.channel))
    p = capture_segment(a.channel, a.out, a.seconds)
    print("recorded:", p, "frames/10s:", frame_count(p))


if __name__ == "__main__":
    main()
