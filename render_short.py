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


def run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[render] {what} FAILED\n{r.stderr[-1500:]}", file=sys.stderr)
        raise RuntimeError(f"{what} failed")
    return r


def extract_frames(src, start, dur, frames_dir):
    os.makedirs(frames_dir, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error",
         "-ss", str(start), "-i", src, "-t", str(dur),
         "-vf", f"fps={FPS}", "-q:v", "2",
         os.path.join(frames_dir, "f_%04d.jpg")],
        "frame extraction")
    n = len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
    print(f"[render] extracted {n} frames @60fps (expected ~{int(dur * FPS)})")
    if n < dur * FPS * 0.8:
        raise RuntimeError(f"too few frames extracted ({n}) — bad capture?")
    return n


def extract_audio(src, start, dur, out_path):
    run(["ffmpeg", "-y", "-v", "error",
         "-ss", str(start), "-i", src, "-t", str(dur),
         "-vn", "-c:a", "aac", "-b:a", "128k", out_path],
        "audio extraction")
    return out_path


def make_clean_clip(src, start, dur, out_path):
    """Re-encode a clean, timestamp-sane clip (needed as tracker input for
    facecam/doc modes — cv2 also chokes on broken live timestamps)."""
    run(["ffmpeg", "-y", "-v", "error",
         "-ss", str(start), "-i", src, "-t", str(dur),
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
                "-global_quality", "20"]
    if encoder != "libx264":
        raise ValueError(f"unknown encoder {encoder!r} (libx264|qsv)")
    return ["-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", preset, "-crf", str(CRF)]


def render_short(src, start, end, transcript_path, style="neon",
                 mode="game", out_path="short.mp4", work_dir=None,
                 fonts_dir=None, keep_work=False, script=None, res=720,
                 preset=PRESET, encoder="libx264", ratio="9:16",
                 facecam_pos="auto", caption_pos="auto"):
    dur = end - start
    if dur <= 0:
        raise ValueError("end must be > start")
    if res not in (720, 1080):
        raise ValueError(f"res must be 720 or 1080, got {res!r}")
    if mode in ("facecam", "doc") and ratio != "9:16":
        # the tracker modes crop a fixed 608px window — ratios only
        # apply to game/gameplay (documented in FINAL-REPORT.md)
        raise ValueError(f"mode {mode!r} only supports ratio 9:16, got {ratio!r}")
    rp = ratio_params(ratio, res)
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
    print(f"[render] {src} [{start:.1f}-{end:.1f}s] style={style} mode={mode} "
          f"script={script} ratio={ratio} res={res} "
          f"facecam={facecam_pos} captions={caption_pos} -> {out_path}")

    frames_dir = os.path.join(work_dir, "frames")
    extract_frames(src, start, dur, frames_dir)
    audio_path = os.path.join(work_dir, "clip_audio.m4a")
    extract_audio(src, start, dur, audio_path)
    track_mov, _man = build_caption_track(transcript_path, start, end, style,
                                          work_dir, fonts_dir, script=script,
                                          out_size=(out_w, out_h))
    print(f"[render] captions done in {time.time() - t0:.0f}s", flush=True)

    cap_y = rp["cap_y_default"]
    facecam_path = None
    overlay = None
    layout_info = None
    is_band = False
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
        try:
            layout_info = choose_layout(
                det, SRC_W, 1080, facecam_pos=facecam_pos,
                caption_pos=caption_pos, ratio=ratio, res=res) \
                if det else fallback_layout(ratio, res)
        except Exception as e:
            print(f"[render] layout detection failed ({e}) — using fallback", flush=True)
            layout_info = fallback_layout(ratio, res)
        print(f"[render] layout: {json.dumps({k: layout_info[k] for k in ('corner', 'caption_y', 'detection_method', 'facecam_src_rect') if k in layout_info})}", flush=True)
        cap_y = layout_info["caption_y"]
        if layout_info.get("band"):
            is_band = True
            if layout_info.get("facecam_src_rect"):
                facecam_path = _extract_facecam(
                    frames_dir, layout_info["facecam_src_rect"], work_dir,
                    out_w, rp["band_h"], target_aspect=out_w / rp["band_h"])
            else:
                print("[render] top-band chosen but no facecam found — "
                      "falling back to plain game mode", flush=True)
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
        if is_band:
            # band layout: the game area is BELOW the facecam band, so the
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
        track_cmd = _tracking_cmd(src, start, dur, work_dir, mode)
        crop_filt = f"crop={CROP_W}:{CROP_H}:0:0"
        base_scale = f"scale={out_w}:{out_h}:flags=lanczos"
        pre = f"fps={FPS},sendcmd=f={track_cmd},"
    else:
        raise ValueError(f"unknown mode {mode!r} (game|gameplay|facecam|doc)")

    if is_band:
        # [0]=frames [1]=caption track [2]=audio [3]=facecam band video
        filt = (f"[0:v]{pre}{crop_filt},{base_scale},"
                f"unsharp=5:5:0.8:5:5:0.0[v0];"
                f"[3:v]drawbox=x=0:y=0:w=iw:h=ih:color=0x00E5FF:t=3[fc];"
                f"[fc][v0]vstack[v1];"
                f"[1:v]format=rgba[cap];[v1][cap]overlay=0:{cap_y}[v]")
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
                f"[1:v]format=rgba[cap];[v1][cap]overlay=0:{cap_y}[v]")
        inputs = (["ffmpeg", "-y", "-v", "error",
                   "-framerate", str(FPS), "-i",
                   os.path.join(frames_dir, "f_%04d.jpg"),
                   "-i", track_mov, "-i", audio_path, "-i", facecam_path])
        audio_idx = 2
    else:
        filt = (f"[0:v]{pre}{crop_filt},{base_scale},"
                f"unsharp=5:5:0.8:5:5:0.0[v0];"
                f"[1:v]format=rgba[cap];[v0][cap]overlay=0:{cap_y}[v]")
        inputs = (["ffmpeg", "-y", "-v", "error",
                   "-framerate", str(FPS), "-i",
                   os.path.join(frames_dir, "f_%04d.jpg"),
                   "-i", track_mov, "-i", audio_path])
        audio_idx = 2
    cmd = (inputs + ["-filter_complex", filt, "-map", "[v]", "-map", f"{audio_idx}:a"]
           + _video_codec_args(encoder, preset) +
           ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
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
    ap.add_argument("--out", required=True, help="output mp4 path")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--keep-work", action="store_true")
    a = ap.parse_args()
    render_short(a.input, a.start, a.end, a.transcript, a.style, a.mode,
                 a.out, a.work_dir, keep_work=a.keep_work, script=a.script,
                 res=a.res, preset=a.preset, encoder=a.encoder,
                 ratio=a.ratio, facecam_pos=a.facecam_pos,
                 caption_pos=a.caption_pos)


if __name__ == "__main__":
    main()
