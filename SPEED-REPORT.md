# SPEED REPORT — fastest processing method for the GTA 6 live clipper

**Date:** 2026-11-18 ~00:35 PKT · **Order:** sohaib — "Fastest process wala method dekhna."
**Scope:** 40s Short, game mode, Glow Neon captions, 720x1280 60fps (the approved
default), source `~/workspace/gta6-clip-research/gtav_capture.mp4` (1080p60 GTA V).
**Test machine:** this shared VM — 4 vCPU (AMD EPYC, shared), 7.7 GB RAM.
**Honest caveat up front:** during these tests 2–3 other agents were rendering
on the same VM (load average 6–15). Absolute seconds are inflated; the
RELATIVE comparisons (preset vs preset, stage vs stage) are the reliable part.
His dedicated PC (i5 8th gen, 16 GB, no other renders running) should beat
every absolute number below. Clean-condition references from the 2026-11-17
spikes are quoted where they exist.

## 1. Baseline profile — where the time actually goes (measured this session)

One 40s render, run stage by stage (`speed-test/profile_stages.py`,
`profile_stages.json`):

| Stage | Time (contended) | Share | Clean-condition reference |
|---|---|---|---|
| Frame extraction — 2400 JPEGs @60fps | **163.3s** | 38% | 67.6s (GTAV spike, 2026-11-17) |
| Audio extraction | 9.5s | 2% | — |
| Caption track — PIL PNGs + qtrle .mov | **51.5s** | 12% | ~40–50s |
| Final encode — libx264 veryfast, CRF 18 | **204.5s** | 48% | 132.7s (GTAV spike) |
| **TOTAL render** | **428.8s** | 100% | **~240–270s** |

Other measured facts:
- The 2400 JPEGs weigh **807 MB on disk** (write + read-back). The caption
  track is 17.3 MB (168 unique states → 2400 frames, qtrle).
- Transcription (faster-whisper tiny, int8, CPU) — prior spike measurement,
  not re-run this session: **~11–15s for ~180s of audio**, model load ~3–5s.
  Transcription is NOT the bottleneck.
- Capture is real-time by nature (the rolling buffer already holds the
  window — 0s added latency once the trigger fires).

**Conclusion: the encode (~50%) and the JPEG frame-extraction round-trip
(~38%) are the whole game. Everything else is noise.**

## 2. Preset comparison — measured (`speed-test/test_presets.py`)

Same 40s inputs, same filter chain, libx264 CRF 18, SSIM vs the veryfast
encode (1.000 = identical):

| Preset | Encode time | File size | SSIM All (vs veryfast) | Verdict |
|---|---|---|---|---|
| ultrafast | 84.5s | **138.7 MB** | 0.967 | Faster, but 2.1x the file — YouTube upload takes 2x longer; gallery fills fast |
| superfast | 114.0s | 84.8 MB | 0.978 | ~10% faster than veryfast, +30% size |
| **veryfast (current default)** | **126.7s** | **65.0 MB** | reference | Best balance — keep as default |
| medium | 358.2s | 69.2 MB | 0.979 | **2.8x slower for ~nothing** (SSIM 0.979 vs 0.978) — never use in the live loop |

Notes:
- The profile run's veryfast encode took 204.5s vs 126.7s here for the same
  inputs — pure contention difference (load ~15 vs ~8). Same lesson: absolute
  seconds move with load; the preset ranking does not.
- **30fps variant: INVALID, discarded.** My test passed `-framerate 30` to
  the JPEG input, which misaligns content vs the 60fps caption track. A real
  30fps test needs frames extracted at 30fps AND the caption track rebuilt
  at 30fps. Do not quote the 117.8s number. (PeakCut's 30fps clip was liked,
  but that says nothing about our 30fps render cost.)

## 3. Hardware encoding — QSV (his PC's biggest win, NOT testable here)

- `h264_qsv` encoder IS present in this ffmpeg build, but there is **no
  Intel GPU on this VM** (`/dev/dri` does not exist). Probe result:
  `Error creating a MFX session: -9` — encode fails immediately.
  VAAPI is equally unavailable here.
- **His PC (i5 8th gen + UHD 620) has Quick Sync.** Expected QSV encode for
  720p60: roughly **20–30s** for a 40s clip (typical UHD 620 QSV throughput
  is 200–400 fps at 720p) vs ~130s software — **the single biggest win
  available, ~100s saved**. This is an ESTIMATE, not a measurement: it must
  be tested on his PC.
- Requirements on his PC: Intel graphics driver installed + an ffmpeg build
  with QSV (the `winget install Gyan.FFmpeg` full build includes it).
- **Implemented:** `--encoder qsv` flag in `render_short.py` (see §6). It
  tries QSV and **falls back to libx264 automatically** if QSV fails, so a
  bad QSV setup can never break a render. Fallback verified on this VM (see
  §6).

## 4. Resolution cost — 720p vs 1080p

- 1080x1920 = 2.25x the pixels of 720x1280. From the multilingual agent's
  logs (30s gameplay clip): caption track 73s (1080) vs 64s (720); output
  file 88 MB (1080) vs 46 MB (720). Encode cost scales roughly with pixels:
  expect **~1.8–2.2x the 720p encode time** for 1080p (estimate — no clean
  1080 encode was timed in isolation this session).
- **Recommendation: 720x1280 60fps for the live race** (speed), 1080x1920 as
  the quality option when time allows. Both are already in the tool
  (`--res 720|1080`).

## 5. What was NOT tested (stopped per the "jaldi final" order)

These are the next experiments, in priority order, with exact commands.
None of them was run — do not quote them as measured.

1. **Skip the JPEG intermediate (biggest structural win after QSV).**
   The 2400-JPEG round-trip costs ~68s clean + 807 MB disk. The fps filter
   already re-timestamps during extraction, so a direct chain may work:
   `ffmpeg -ss 0 -i gtav_capture.mp4 -t 40 -vf "fps=60,setpts=N/60/TB,crop=608:1080:656:0,scale=720:1280:flags=lanczos,unsharp=5:5:0.8:5:5:0.0" -i cap_track.mov -i audio.m4a -filter_complex "[0:v][1:v]overlay=0:700[v]" -map "[v]" -map 2:a -c:v libx264 -preset veryfast -crf 18 out.mp4`
   Verify: frame count = 2400, duration = 40.0s, SSIM ≥ 0.99 vs the
   JPEG-path encode, captions in sync. If it passes, it deletes the whole
   extraction stage.
2. **Parallelise the independent stages.** Frame extraction and caption-track
   build do not depend on each other — run them concurrently (thread +
   subprocess). Saves ~40–50s of wall time per render. Sketch: start the
   caption-track build in a thread while `extract_frames` runs, join before
   the final encode.
3. **Transcribe only the picked window.** Energy profile (cheap, numpy) first
   on the full segment → pick the 40s window → transcribe only ~48s of audio
   instead of the whole 98–180s capture. Saves ~8–10s. (Transcription is
   small either way — do this last.)
4. **Transcribe during capture (rolling).** Transcribe each 30s buffer chunk
   in the background as it is captured, so the transcript is ready when the
   spike fires. Design only — touches `clip_live.py`, implement with the v2
   builder, not as a solo patch.
5. **Proper 30fps test** (frames at 30fps + caption track rebuilt at 30fps)
   — only if a 30fps output option is wanted.

## 6. What was implemented (safe, optional flags only)

`render_short.py` only — core render path untouched, defaults unchanged:
- **`--preset {ultrafast|superfast|veryfast|medium}`** (default `veryfast`).
  Tested: `python render_short.py ... --preset superfast` → valid 720x1280
  60fps 5s clip in 26s total (smoke test, `speed-test/smoke_superfast.mp4`).
- **`--encoder {libx264|qsv}`** (default `libx264`). QSV uses
  `-preset veryfast -global_quality 20`. On QSV failure it prints
  `QSV encode failed — falling back to libx264` and re-runs in software.
  **Fallback verified on this VM:** `--encoder qsv` on a 5s clip failed QSV
  immediately (no Intel GPU) and still produced a valid 720x1280 H.264 clip
  (`speed-test/smoke_qsv_fallback.mp4`, 5.0s).
- Docstring note in `render_short.py` + this report. README untouched
  (v2 builder owns it).

## 7. Recommended fastest method + latency per method

Moment = the loud/chat spike fires. The 40s window is already in the rolling
buffer, so capture adds 0s. (If capture had to start at the trigger, add 40s.)

| Method | Render (40s clip) | Moment → Short | Notes |
|---|---|---|---|
| Current default (veryfast, sequential) | ~240–270s | **~4–4.5 min** | Measured baseline; matches the earlier ~4–5 min estimate |
| Software-only fastest (superfast + parallel stages + window-only transcribe) | ~190–220s | **~3.5 min** | Parallelism + window-transcribe untested — §5 items 2–3 |
| **Recommended: QSV on his PC (veryfast QSV)** | **~110–150s** | **~2–2.5 min** | Encode ~130s → ~20–30s. THE target. Test on his PC: `render_short.py ... --encoder qsv` — auto-fallback if QSV is missing |
| QSV + direct chain (skip JPEGs) | ~60–90s | **~1–1.5 min** | Best case, both untested items combined — verify §5 item 1 first |

**Bottom line:** on his PC, use `--encoder qsv` (already wired, auto-fallback).
If QSV works there, the race is won at ~2–2.5 min moment→Short. If it does
not, stay on veryfast and take the ~4 min path — and test the direct-chain
experiment (§5.1) next, because deleting the JPEG round-trip is the biggest
remaining software win.

## 8. Evidence files

- `speed-test/profile_stages.py`, `profile_stages.json` — baseline timings
- `speed-test/test_presets.py`, `preset_results.json` — preset + SSIM numbers
- `speed-test/test_transcribe.py` — NOT run (stopped per order); transcribe
  numbers above are the 2026-11-17 spike measurements
- `speed-test/profile_720_veryfast.mp4`, `preset_{ultrafast,superfast,veryfast,medium}.mp4`
- `speed-test/smoke_superfast.mp4`, `speed-test/smoke_qsv_fallback.mp4` — flag verification
- `speed-test/work_profile/` — frames, audio, caption track from the profile run
