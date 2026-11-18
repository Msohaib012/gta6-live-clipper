# GTA 6 Live Clipper — Build Report

**Built:** 2026-11-17 (evening) · **For:** GTA 6 launch, 19 Nov 2026
**Location:** `~/workspace/gta6-live-clipper/`
**Status: BUILT + TESTED END-TO-END on a live stream.** One real clip was produced with the tool itself (see Example below). Two things are implemented-but-untestable here and are marked as such below.

## What it is

A PC tool with a **local browser dashboard** (not a website — a public site cannot run 24/7 live capture + Whisper + rendering, and yt-dlp/ffmpeg don't run in a browser). It watches live Twitch streams 24/7, auto-clips the loud moments into vertical 720x1280 60fps Shorts with professional captions, and queues them for YouTube auto-post — so the Short is out **before the streamer publishes their own video**.

## Files

| File | What it is | Provenance |
|---|---|---|
| `clip_live.py` | Main tool: 24/7 watch loop (round-robin channels), triggers, queue hookup | New, built on the spike scripts |
| `dashboard.py` + `dashboard.html` | Local browser UI (Flask, http://127.0.0.1:5000) | New, YT Clip Tool pattern |
| `capture.py` | yt-dlp 1080p60 segment capture, `.part` recovery, live check, process-tree kill | Spikes: LIVE-QUALITY-FIX.md + GTAV-QUALITY.md |
| `transcribe.py` | faster-whisper tiny + audio-energy profile + moment pick | Spikes: gtav_transcribe.py (wave-module PyAV bypass kept) |
| `render_short.py` | Standalone render: capture + window → finished Short | Spikes: render_v2.py + hq/gtav_render.py |
| `captions.py` | Glow Neon + Hormozi Pop caption-track builder (PIL PNG) | Spike: build_captions_v2.py, parameterized |
| `track_v3_stable.py` | V3 stabilized face tracker (facecam/doc modes) | Spike: track_v3_stable.py, paths parameterized |
| `build_track60.py` | 10Hz samples → 60Hz sendcmd track (smooth PCHIP / static per shot) | Spike: build_track60_v3.py, parameterized + static mode added |
| `post_queue.py` | Auto-post queue + YouTube upload (OAuth) | New (YouTube API); Instagram/TikTok are explicit v2 stubs |
| `channels.json` | Example channel list (edit with the GTA 6 streamers) | New |
| `fonts/` | Anton + Montserrat Black (OFL, bundled) | Spike: fonts/ |
| `models/` | BlazeFace full-range model (face tracker) | Spike: fuseclip-spike/ |
| `example_short.mp4` | Example Short rendered with this tool (Glow Neon) | Live test, 2026-11-17 |
| `requirements.txt` / `requirements-facecam.txt` | Core deps / facecam+doc deps | New |
| `README.md` | Install (Windows first), quick start, YouTube setup, limits | New |

## How each part maps to the proven components

- **Live capture (1080p60, ~1.05x real-time, Twitch not throttled)** — `capture.py`, format `1080p60__source_`. Verified again in the live test: 121s recorded, 124.8 MB.
- **Timestamp fix (LOAD-BEARING)** — `render_short.py` extracts the clip as a 60fps image sequence first; the raw live capture is never edited directly. The live test extracted 2400/2400 frames.
- **Moment pick (faster-whisper tiny + audio energy)** — `transcribe.py`. Live test: 121.7s audio → 26 segments, 249 words; loudest window −28.6 dB.
- **Render (608x1080 centre crop → lanczos → unsharp 0.8 → H.264 CRF 18, `veryfast`, 60fps)** — `render_short.py`. Live test output verified: 720x1280, H.264+AAC, 60fps, 40.0s.
- **Tracking** — game default is centre crop (correct for gameplay). `track_v3_stable.py` (face-ID persistence + hysteresis + two-rate EMA + deadband) is the facecam mode; `build_track60.py --mode static` is the documentary mode (crop held per shot, changes only at cuts).
- **Captions** — `captions.py`, both styles with a `--style` flag, **default Glow Neon** (sohaib: "Glow Neon's captions are better than the others", 22:39 PKT; Hormozi Pop was his earlier ❤️). All 3 missing-subtitle fixes are in: (1) words overlapping the clip window are included + clamped, (2) every word ≥0.08s active time, (3) caption HOLDS until the next word starts. Caption-track architecture (unique PNGs → 60fps transparent video → ONE ffmpeg overlay). Windows-safe: PNGs are **copied**, not symlinked. **Caption safe zone (sohaib's order, 2026-11-17 23:11 PKT):** burned captions sit in the middle band of the 720x1280 frame — caption bottom edge above **y=950** (`CAP_Y = 700` in `captions.py`, single source of truth, imported by `render_short.py`). The bottom ~300px are reserved for the platforms' own UI (username + caption bottom-left, like/comment/share buttons bottom-right on TikTok / Shorts / Reels). The old position (`CAP_Y = 980`) put the caption bottom edge at ~y=1160, hidden behind the platform overlay; the raised position also clears the game's own bottom HUD.
- **Rolling buffer** — segments of N minutes (default 3) in `buffer/`, oldest pruned (keep 4). Channels watched **round-robin** (one segment per channel per round) — a home PC can't capture + transcribe several 1080p60 streams in parallel.
- **24/7 watch + auto-post loop (PeakCut parity)** — `clip_live.py` watches a channel list unattended; `post_queue.py` queues every clip with a title from its loudest moment and uploads to YouTube with 30-min spacing + 10/day cap. Instagram/TikTok are documented v2 stubs (they need app approvals; nothing is faked).

## Example Short (rendered with this tool, live)

**File:** [example_short.mp4](sandbox://workspace/gta6-live-clipper/example_short.mp4) — 40s, 720x1280, H.264+AAC, 60fps, 65.0 MB

- **Source:** deansocool live on Twitch (GTA V, NoPixel SWAT RP), 1080p60, captured 2026-11-17 17:50 PKT with the tool itself (`clip_live.py --once --clip-now --segment-minutes 2 --style neon`).
- **Moment picked:** 0.0–40.0s (loudest 5s window at 5.0s, −28.6 dB, 96 words) — "We're out, we're going to check."
- **Captions:** Glow Neon (default). Frame-verified: cyan caption pixels present at t=5s (11,483 px) and t=35s (10,373 px); t=20s has none — confirmed against the transcript as a genuine speech gap (18.52–21.92s), not a bug.
- **Timings (this VM, heavily contended — see note):** capture 121s · transcribe 64.6s · captions 170s · encode 272s · render total 442s · wall 17:50:11→18:00:50 (10m39s).
- **Latency note (honest):** the clean spike numbers were transcribe ~15s + render ~132s (veryfast) ≈ 4 min moment→Short. This run was ~8.5 min after capture because the VM was shared with two other agents rendering at the same time (plus an orphaned capture ffmpeg, see Bugs). On his dedicated PC, expect ~4–5 min moment→Short.

### Re-render — caption safe zone (2026-11-17 ~23:18 PKT)

The example was re-rendered with the safe-zone fix (`CAP_Y = 700`, see Captions above). The original deansocool capture segment from 17:50 PKT no longer exists on this VM (buffer pruned; only `test_run/gta6_short_20261117-175325.mp4`, byte-identical to the old example, survives), and a fresh live capture was attempted twice at 23:13–23:14 PKT but failed both times — deansocool was live (1080p60 listed) yet the HLS segment fetch died in ffmpeg (exit 183, then 251: TLS I/O error / stream ends prematurely — the same transient Twitch/proxy flakiness documented below). So the re-render uses the **same streamer, same game, same 0.0–40.0s moment window** from the earlier proven capture `~/workspace/gta6-clip-research/gtav_capture.mp4` (deansocool, GTA V NoPixel, 1080p60, 98.1s) + `gtav_transcript.json` (168 words in 0–40s), rendered through the tool's own `render_short.py` (`--style neon --mode game`). Render took 233s (frames 2400/2400, captions 93s, encode 140s). ffprobe-verified: H.264, 720x1280, 60fps, 40.0s, 65.0 MB.

**Frame verification (cyan-pixel row measurement):** old position put the caption at y=1100–1199 (bottom edge ~1160 — inside the platform UI zone). New position: t=5s caption at y=800–899 (bottom edge ~881), t=36s caption at y=800–899 (bottom edge ~881) — **both above y=950, clear of the platform UI band and the game's bottom HUD**. t=35s is blank — verified against the transcript as a genuine speech gap (last word ends 32.52s, next starts 35.74s), not a bug. Frame captures in `verify/safezone/` (`new_t5.jpg`, `new_t35.jpg`, `new_t36.jpg`).

## Tests run (all on this VM, 2026-11-17 evening)

| Test | Result |
|---|---|
| All modules import + compile (venv python) | ✅ PASS |
| Live end-to-end: deansocool, `--once --clip-now`, Glow Neon | ✅ PASS — clip saved + verified (see Example) |
| Caption visibility (cyan-pixel frame check at t=5/20/35s) | ✅ PASS — captions show during speech, blank during real silence |
| Queue: enqueue → process without YouTube setup | ✅ PASS — title from loudest moment; entry marked `awaiting_setup`, not lost |
| Dashboard: GET / (200), GET /api/status (correct JSON incl. clip list + `youtube: missing_secrets`), POST /api/start with empty channels (400 + error) | ✅ PASS |
| `post_queue.py --status` | ✅ PASS — reports `missing_secrets` |
| Capture kill fix: no orphan ffmpeg after failed captures | ✅ PASS — `ps` shows no leftover ffmpeg (see Bugs) |
| Dashboard Start with a live channel (full loop via the UI) | ⚠️ NOT run directly — the dashboard calls the same `run_loop()` that the CLI test proved; starting it would have begun a second live capture while Twitch was flapping (see Bugs) |
| Caption safe-zone re-render (2026-11-17 ~23:18 PKT) | ✅ PASS — `CAP_Y = 700`; example re-rendered (H.264, 720x1280, 60fps, 40.0s); caption bottom edge ~y=881 at t=5s and t=36s (was ~y=1160); t=35s blank = genuine speech gap (32.52–35.74s) |

## Bugs found during testing (fixed)

1. **Orphaned ffmpeg capture (FOUND + FIXED).** yt-dlp spawns ffmpeg as its HLS downloader. My first version killed only the yt-dlp parent (`proc.kill()`) — the ffmpeg child survived and kept recording: it wrote **485 MB over 8.5 min** after the parent died. Fix in `capture.py`: new process group on start (`start_new_session` / `CREATE_NEW_PROCESS_GROUP`) + kill the whole tree (`os.killpg` on Linux, `taskkill /F /T` on Windows). Verified: no orphan ffmpeg remains after captures end.
2. **Transient Twitch/proxy flakiness (NOT a tool bug, documented).** At 18:01–18:08 PKT, three capture attempts failed at the m3u8/segment fetch ("Input/output error", "IncompleteRead", code 183/251) while `yt-dlp -F` confirmed the channel was still live — the same channel had recorded fine at 17:50. The loop handles this by design (capture failure → retry next segment). A possible future hardening: `--hls-prefer-native` (yt-dlp's native HLS downloader retries fragments and spawns no ffmpeg child) — **untested, not switched**; it needs its own verification run before it replaces the proven ffmpeg path.

## Calibration finding

**Default trigger threshold is now −28 dB (was −25).** deansocool's loudest 5s window was −28.6 dB — a talk-heavy RP stream — so −25 would have missed the moment entirely (the test clip only rendered because of `--clip-now`). −28 catches both this stream and louder ones (GTAV spike: −24.8 dB). Updated in `clip_live.py`, `dashboard.html`, and `README.md`.

## NOT done / known limits

- **YouTube OAuth — implemented, NOT tested end-to-end.** It needs his Google account + OAuth client (`python post_queue.py --auth` on first run). The queue logic around it IS tested (enqueue → awaiting_setup). No upload has ever been attempted from here.
- **Facecam / doc modes — included, NOT tested end-to-end on this VM.** The tracker needs mediapipe + opencv (not installed here); `track_v3_stable.py` was proven on the Kakori clip in the spike, and the code here is the same logic with parameterized paths. Game mode (the default) is the tested path.
- **Dashboard Start with a live channel** — same `run_loop()` as the CLI test, but the button path itself wasn't clicked with a live channel (Twitch was flapping at the time).
- **Instagram / TikTok auto-post — v2, not implemented.** Needs app approvals; the stubs raise `NotImplementedError` with the explanation. Nothing is faked.
- **Render speed on his PC is unmeasured** — the VM numbers above are contended; his i5/16GB/no-GPU PC should be similar or a bit slower than the clean spike numbers (~4–5 min moment→Short).
- **No 1440p/4K live quality exists on Twitch** — 1080p60 is the ceiling. Old games (GTA III) look old regardless of pipeline.
- **A clip needs speech** — a 40s window with no talking is skipped. The centre crop cuts the streamer's facecam off (gameplay only, by design).
- **Clips other people's streams** — only clip streams you have permission to use, or streamers who welcome clipping.

---

## Multilingual captions + gameplay layout + Hype + 1080p (2026-11-17 ~23:20–23:45 PKT)

Ordered after sohaib watched the PeakCut live clip: PeakCut is the best so far (quality + captions + fps), BUT it only detects English while the streams speak Roman Urdu/Hindi. The tool now matches PeakCut's look AND speaks Roman Urdu/Hindi.

### What changed

- **`transliterate.py` (NEW).** Devanagari/Nastaliq → Roman, local first: indic-transliteration (ITRANS, with anusvara→n, visarga dropped) + a Hinglish normalizer (long vowels restored: ii→i, aa→a — "kaa" is how English writes "ka"; short "a" restored at word ends; `shch`→`sh`, `chh`→`ch`) + an overrides dict for common words (azad, karna, school, mujhe, mere, peena, jeena, mein, pulis). Gemini path (`transliterate_words_gemini`, `roman_mode="gemini"`) uses the user's key from `config.json` — never hard-coded; if no key, falls back to local and says so. Urdu path is rule-based + overrides (indic-transliteration has no Urdu scheme; aksharamukha 2.3 checked — NO Urdu→Latin support, `KeyError 'Urdu'`).
- **`transcribe.py`.** faster-whisper **auto-detects** the language (reports `detected_language` + `language_probability`; Kakori: `hi` at 0.83). New knobs: `language="auto|en|hi|ur|roman-urdu"`, `caption_mode="auto|hi-roman|hi-deva|ur-roman|ur-nastaliq|en"`, `roman_mode="local|gemini"`. `roman-urdu` = force Hindi/Urdu input regardless of what Whisper thinks the language is (needed: Hindi/Urdu are acoustically identical and Whisper flips between them). `apply_caption_mode()` maps Whisper words → `{display, script}`; the renderer's font/script selection reads `display` (fallback: raw word).
- **`captions.py`.** New style **`hype`**: Anton ALL-CAPS, active word pops 80→112→100% and turns RED `#FF2626` (PeakCut's "CHOPPER UH" look). New `scale` param — the caption track renders natively at the output resolution (720 or 1080). Font selection: latin → Anton/Montserrat Black; devanagari → Noto Sans Devanagari Bold; urdu → Noto Nastaliq Urdu; PIL + libraqm (the Devanagari-fix test from the FuseClip spike). **Caption band corrected (his verdict 23:31 PKT — "Ye nahi achi lagi" on the CAP_Y=700 re-render):** `CAP_Y = 850` — text at y~955–1045, bottom edge ~y=1000–1050, below the action centre, above the platform UI (~y=1080). The 700 band (y=800–899) put captions mid-frame over the police car / running cop; the old 980–1160 band hid behind the platform UI. Both rejected — the answer is the band between.
- **`layout.py` (NEW).** Facecam auto-detection (still-border detection on sampled frames + MediaPipe face confirmation, falls back to the face box, then to plain game mode) + `choose_layout()`: facecam **288x162 (40% of width — BIGGER than PeakCut's ~323x186 at 1080 = 30%)**, corner picked per clip (top-right preferred; bottom corners excluded — platform UI zone), captions at an auto-checked Y (candidates 850/650/450, first band free of facecam/HUD wins). `detect_facecam` samples 8 frames over the clip — once per clip, not per frame.
- **`render_short.py`.** `--style` gains `hype`; `--res 1080` (1080x1920 60fps, quality option — PeakCut's clip was 1080-wide and sohaib liked it); 720x1280 60fps stays the default. Everything scales by 1.5x: crop 608→912 (same 1.18x upscale), caption track rendered natively at 1080, overlay Y + facecam rect scaled.
- **`clip_live.py` + dashboard.** `--style hype`, `--res 720|1080` threaded through `run_loop → _watch_one_segment → process_segment → render_short`; dashboard style picker has all three styles + a resolution picker + a language picker (global default; per-channel language in `channels.json`).

### Test renders (this VM, 2026-11-17 ~23:45 PKT)

| Test | File | Result |
|---|---|---|
| Roman captions (Kakori Hindi → Roman, clean Devanagari source), neon, game, 720, 0–30s | `test_roman_captions.mp4` | ✅ PASS — 9.2 MB, 720x1280 60fps, 30.0s; captions 89s + encode 119s = **209s total**. Frame-checked: "AHAM BHUMIKA NIBHAI" — readable Roman, cyan active word, band y~955–1045. (First run 293s used the pre-fix transcript with "men"/"gae" — re-rendered fixed.) |
| Devanagari captions, same clip, 0–15s | `test_devanagari_captions.mp4` | ✅ PASS — 5.2 MB, 15s; captions 49s + encode 80s = **130s total**. Frame-checked: "अहम भूमिका निभाई।" — conjuncts shaped correctly (PIL + libraqm). |
| Nastaliq captions (Urdu words, font test), 0–10s | `test_nastaliq_captions.mp4` | ✅ PASS — 4.0 MB, 10s; **38s total**. Frame-checked: "نہیں میں پاکستان" — proper Nastaliq slant + joining. |
| Gameplay + Hype, GTA V, 720, 0–30s | `test_gameplay_hype_720.mp4` | ✅ PASS — 46.2 MB, 720x1280 60fps, 30.0s; captions 64s + encode 91s = **179s total**. Frame-checked t=5s + t=20s: facecam top-left 288x162 (clearly visible, bigger than PeakCut's 30%), "THEM." red Anton ALL-CAPS, caption band y~950–1000 (over the police-car roof in this shot — the car fills the lower half — but legible, car fully recognizable), HUD bottom-right untouched, t=20s blank = genuine speech gap. |
| Gameplay + Hype, same clip, 1080 | `test_gameplay_hype_1080.mp4` | ✅ PASS on retry — 88.1 MB, **1080x1920 60fps**, 30.0s; captions (scale 1.5) + encode 234s = **378s total**. Frame-checked t=5s: facecam 432x242, "THEM." red, caption band y~1430–1570. **First attempt FAILED** (78s): facecam 432x243 (odd height) broke libx264/yuv420p — fixed by rounding facecam dims to even in `_extract_facecam`. **1080 vs 720 cost on this VM: 378s vs 179s ≈ 2.1x for the same 30s clip.** |
| **Demo clips (2026-11-18 00:08 PKT order)** | `roman_short.mp4` + `pubg_short.mp4` | See "Demo clips" section below. |

(Run logs: `test_renders.log`, `test_renders2.log`. Frame-checks in `/tmp/cap_*.png`, `/tmp/gp720_*.png`, `/tmp/gp1080_*.png`.)

### Demo clips (2026-11-18 00:08 PKT — sohaib's order, final verification)

1. **`roman_short.mp4`** — Kakori Hindi → Roman. Source: `work_kakori1080.mp4` (41.3s Kakori clip), transcribed live with faster-whisper tiny (auto-detect: **hi, 0.83**), transliterated local, Glow Neon, game mode, 720x1280 60fps, 30s, 9.3 MB, render 301s. Frame-verified t=5/12/20s: captions ARE Roman (Latin script — "ME KANE BHAI.", "KEES KEES KEEL", "ALFRED PAGAI HOETHE"), neon cyan active word, band y~950–1000, never mid-frame. **Honest quality note: the WORDS are partly garbled — that is Whisper tiny's Hindi transcription (~60% intelligible: "embeho", "trittava", "kees keel"), NOT the transliteration.** Whisper tiny writes Hindi as Roman phonetics itself, so the transliterator had no Devanagari to convert (display == word). Proof the transliteration is fine: the same clip from the clean Devanagari transcript (`test_roman_captions.mp4`) reads "ko socialist republican association banane mein aham bhumika nibhai bhagat aur azad ke netritv mein HSRA ne..." — clean natural Roman. Fix path for launch night: a better transcript source (YouTube auto-subs = clean Devanagari) or the Whisper `small` model (but `small` is 10x slower and hallucinates — not usable in the live loop).
2. **`pubg_short.mp4`** — PUBG with facecam. Source: Twitch **Halifax** (PUBG streamer, "PUBG IS BEST GAME IN WORLD"), live-captured 1080p30 on this VM (Twitch not throttled; YouTube live FAILED — two streams tried, both died on the audio HLS edge with I/O errors, and the 4 search-result streams were already over). First 75s capture: 25s Twitch ad break + quiet looting, only 4 words ("Thank you for watching." — ad audio) — unusable for a captioned demo, so a second 150s capture (107.5 MB, ~141s) was taken. Transcribed: **en, 0.77, 220 words**; picked moment 72–112s (94 words, loudest window 80–85s, −17.5 dB). Rendered `--mode gameplay --style hype`, 720x1280 60fps, 40.0s, 17.3 MB. Frame-verified t=8s + t=25s: facecam auto-detected (still-border, src rect [0,460,440,240]) → overlaid **top-right 288x162**, clearly visible (Halifax, beard + glasses + headphones — bigger than PeakCut's 30%); captions "OH, HERE'S **GEN**" Hype red pop in the safe band y~950–1000, never overlapping the facecam or the matchmaking bar; t=25s blank = genuine speech gap. **Honest notes:** the 72–112s window is the PUBG LOBBY/matchmaking screen, not in-match combat (the moment picker follows speech, and Halifax was talking in the lobby); the facecam sits over part of the top menu bar text ("PLAYGROUND" partially hidden) — cosmetic, not gameplay action. Source stream was 1080p30 (not 60) — output is 60fps with duplicated frames.

### Honest accuracy numbers

- **Whisper auto-detect on Hinglish: detection RIGHT, transcription ROUGH.** Kakori, `tiny`: language `hi` at 0.83 (correct). Transcription: ~60–70% intelligible — "banaane", "bhai", "Bhagat Singh", "Azad" OK; "embeho", "tritava", "angresokhe" garbage. It Romanizes Hindi phonetically instead of writing Devanagari. `small`: language `ur` at 0.65 (wrong-ish — Hindi/Urdu are acoustically identical), output error-prone Nastaliq + hallucinated Chinese characters, 100s for 41s audio — **unusable in the live loop**. So `tiny` stays the live model; the transliteration path is proven correct on clean Devanagari/Nastaliq input (e.g. YouTube auto-subs transcripts).
- **Local transliteration on clean Devanagari: GOOD.** "chandrashekhar azad ne kaha, british pulis se milne school jana hai" — vs Gemini's "Chandrashekhar Azad ne kaha, British police se milne school jana hai". Differences are capitalization and "pulis"/"police" — readable Roman, not Devanagari, not English. Local Urdu→Roman is approximate (unwritten vowels guessed; overrides fix common words). Gemini is the natural path when the user sets their key.
- **What this means on launch night:** Roman captions will be readable and correct when Whisper's words are right; when Whisper garbles a word, the caption garbles with it. That is a Whisper limit, not a transliteration limit — no tool fixes it without a bigger model or YouTube's auto-subs as the transcript source.

---

## v2 — Consolidated final build (2026-11-17 23:40 → 2026-11-18 00:15 PKT)

**Built to sohaib's full spec (23:37 PKT) + styles/favorites orders (23:38 PKT) + YT Clip Tool separation boundary (23:39 PKT). Packaged 2026-11-18 ~00:15 PKT on his "jaldi final kar ke zip file do" order — features frozen, ZIP first.**

### What v2 added (all in the ZIP)

- **Stream discovery (dashboard):** SEARCH YouTube live (yt-dlp ytsearch, no key — proven: "GTA" → 2 live streams, "gaming" → 3, "Fortnite" → 3) + PASTE LINK (YouTube/Twitch/Kick/any yt-dlp live URL — validated live before adding). Watch list persists in `channels.json`.
- **Caption styles: 25, every one with a REAL animated preview** (`static/previews/<style>.mp4`, 4s, rendered by the tool from a real GTA V frame — CapCut-style). Sources: our Hormozi Pop + Glow Neon + karaoke/pop/fade/box/pulse (V2 spike), PeakCut hype (Anton RED pop — his benchmark), autoclip pop/boxed/cinematic/classic, aman ClipKaro karaoke/box/minimal/bold/comic, remotion-captioneer typewriter/pill/bounce, **8 FuseClip styles extracted from the closed 0.75.1 binary** (Rawani, Lafz, Aks, Lakeer, Ali Abdaal, Shamani, Saaf, Lehar). All 25 caption tracks build clean (verified) and all 25 preview mp4s ffprobe-verified at 4.03s.
- **Favorites rotation:** star styles on the Styles page, order with ▲▼, Save. The auto-clip loop rotates through favorites one by one (clip 1 → fav 1, clip 2 → fav 2, cycling) — verified end-to-end: two consecutive `process_segment(style="auto")` calls rendered **neon (favorite 1/3)** then **hype (favorite 2/3)**, both H.264 720x1280 60fps 15s, with `.info.json` sidecars. Manual "Clip now" can override one clip with an explicit style.
- **Live chat tracking (Twitch IRC):** unique chatters per minute (PeakCut's rule — people, not messages). If the crowd peaks outside the loudness-picked moment and real speech is there (≥5 unique chatters), the clip re-centres on the burst. YouTube live chat is NOT tracked (needs a Google API key — loudness + transcript still work).
- **VOD mode:** URL or local file → transcript (10-min chunks) → best moments via the **8-signal virality scorer** (no AI) or **Gemini two-pass picking** when a key is set → same vertical render + captions + gallery. Verified: local GTA V file → auto-picked moment 7–22s (signals: dense-speech, hype:go,look, hook, story-peak) → H.264 720x1280 60fps 15s + sidecar. Source deleted after rendering; URLs download only the picked ranges. **VOD is Shorts-making only — NOT a clip downloader** (YT Clip Tool stays separate: no code, UI, repo or release shared).
- **Gallery enrichment:** every clip carries a `.info.json` sidecar (style, trigger, channel, signals, moment) shown in the dashboard gallery with preview + Download.
- **Dashboard:** How-it-works strip (Start → automatic → gallery), search/paste/watch-list, watch setup (style/mode/language/captions/res/segment/trigger/post), Styles page (25 previews + favorites + reorder + rebuild), VOD card, Settings (optional free Gemini key — never shown back), queue + events. Verified via Flask test client: GET / 200 (all sections present), /api/status, /api/styles (25 styles, 25 previews exist), /api/channels, /api/favorites, /api/search, /static/previews/neon.mp4 200.
- **Hosting answer (in README):** a subdomain on a free/5GB shared cPanel host **cannot** run this tool — shared hosting gives a web folder, not a 24/7 process; it cannot run ffmpeg + Whisper continuously or capture live video. The tool needs a real computer that stays on (his PC).

### FuseClip extraction (child agent, ~7 min, binary never run)

FuseClip 0.75.1 (202 MB installer, SHA-256 `c8c97da0…a8fd6d`): NSIS → Electron asar (8,699 files) → preset array `Zi` in the Vite renderer bundle. **Exactly 18 real presets** (Mumbai, Numaya, Josh, Aks, Rawani, Headline, Rang, Noor, Lehar, Orbit, Bayan, Lakeer, Uchhaal, Ali Abdaal, Karachi, Lafz, Shamani, Saaf). Full report: `~/workspace/fuseclip-styles/FUSECLIP-STYLES.md`. **Corrections:** no "typewriter" in FuseClip 0.75.1 (that word is from the sibling Fuse Caption Studio's marketing); no "neon"/"hormozi" named styles (closest: glow presets + Mumbai's Anton yellow gradient). 8 presets became our styles; the rest map to existing ones (Mumbai→hormozi+glow, Josh→box, Ali Abdaal→karaoke, Rang→comic/bounce, Lehar→pill/glow, Saaf→bold).

### OpenShorts spike (completed 2026-11-17 ~23:55 PKT)

**RUNS end-to-end as a VOD clipper — but wrong for GTA and too slow for live.** Full `main.py` render: 1080x1920 H.264 60fps 40s in **634s (15.8x real-time)** — ~5x slower than our pipeline. Auto-picked the GENERAL layout (gameplay only in the middle ~50% of the frame, blurred bars top/bottom) because faces appear in only 2 of 10 frames — the wrong look for GTA. Captions are ASS/libass (Devanagari-shaping risk; our PNG overlays are the proven fix) and violate the safe zone (bottom edge ~1057 vs the y~950–1000 band). Gemini two-pass picking NOT tested (no key). **Borrowed:** faster-whisper language detection (already in our transcribe), two-pass picking shape for VOD (in vod.py), the named-styles menu concept. **Not borrowed:** ASS rendering, face-count layout for games, 1080 delivery floor, the full app as the live tool. Full report: `~/workspace/openshorts-spike/OPENSHORTS-SPIKE.md`.

### v2 test results (this VM, 2026-11-17 evening → 2026-11-18 00:15 PKT)

| Test | Result |
|---|---|
| All 15 modules import clean (track_v3_stable.py is CLI-only — subprocess-invoked, see note) | ✅ PASS |
| 25 caption styles build a caption track (sample transcript, 30fps) | ✅ PASS — 25/25, incl. lafz single-word chunking (6 chunks from 6 words) |
| 25 animated style previews build + ffprobe | ✅ PASS — 25/25 mp4s, 4.03s each |
| render_short CLI, karaoke (new FuseClip-inspired style), 0–15s | ✅ PASS — H.264 720x1280 60fps 15s |
| process_segment style=auto (favorites rotation) ×2 | ✅ PASS — neon (1/3) then hype (2/3), both 720x1280 60fps 15s, sidecars correct |
| VOD: local GTA V file, 8-signal picking, count=1 | ✅ PASS — moment 7–22s auto-picked, H.264 720x1280 60fps 15s + sidecar |
| Dashboard (Flask test client): /, /api/status, /api/styles, /api/channels, /api/favorites, /api/search, /static/previews | ✅ PASS — all 200; 25 styles, 25 previews |
| YouTube live search (yt-dlp ytsearch, no key) | ✅ PASS — "GTA" 2, "gaming" 3, "Fortnite" 3 live results |
| Gameplay mode 1080p (multilingual agent's render) | ✅ PASS — H.264 1080x1920 60fps 30s (even-dim facecam fix worked: 432x242) |
| Gameplay mode 720p | ✅ PASS — H.264 720x1280 60fps 30s |
| Roman captions (Kakori Hindi → Roman), neon, 720 | ✅ PASS — test_roman_captions.mp4 (multilingual agent) |
| Devanagari + Nastaliq caption renders | ✅ PASS — test_devanagari_captions.mp4 / test_nastaliq_captions.mp4 |
| Live watch loop via dashboard Start (full loop, live channel) | ⚠️ NOT run directly — same run_loop() as the CLI test; starting it would have begun a second live capture while renders were running |
| YouTube OAuth end-to-end | ⚠️ NOT tested — needs his Google account + OAuth client on first run |
| Live chat burst re-centring (real Twitch IRC) | ⚠️ NOT tested live — code path exists; Twitch IRC is connection-reset from this VM (datacenter egress). Test on his PC. |

### Bugs found during v2 testing (fixed)

1. **preview_styles.py work-dir bug (FOUND + FIXED).** `build_preview` wrote the sample transcript into a per-style work dir that didn't exist (`FileNotFoundError`). Fix: `os.makedirs(work_dir, exist_ok=True)` before writing. All 25 previews then built.
2. **captions.py docstring SyntaxWarning (FIXED).** `\kf` in the karaoke docstring → changed to "kf-sweep".
3. **Gameplay 1080p facecam encode (FOUND + FIXED by the multilingual agent).** 162×1.5=243 is odd; libx264 + yuv420p needs even dimensions. Fix in `_extract_facecam`: round fc_w/fc_h down to even (432x242). Verified by the 1080p render above.
4. **/tmp full during testing (TEST HARNESS, not a tool bug).** My first E2E script wrote renders to /tmp (512 MB tmpfs) and hit `No space left`. Re-ran with the workspace out dir — passed. Lesson: never render to /tmp.

### What is IN the ZIP

`gta6-live-clipper-v2.zip` — source only: all `.py` files + `dashboard.html` + `fonts/` (Anton, Montserrat Black, Noto Sans Devanagari Bold, Noto Nastaliq Urdu, Bangers) + `README.md` + `BUILD-REPORT.md` + `requirements.txt` + `requirements-facecam.txt` + `channels.json` (example) + `settings.json` (favorites default: neon, hype, hormozi) + `static/previews/` (25 mp4s + index.json) + `assets/sample_bg.jpg` (preview background). **Excluded:** venv, models/ (see below), captures, clips/, work dirs, `__pycache__`, `.part` files, test renders (`example_short.mp4`, `test_*.mp4`, `work_kakori1080.mp4`), test scripts/logs (`run_tests*.sh`, `test_*.log`), `static/previews/_work/`.

**models/ excluded by design (ZIP is source-only):** facecam/doc modes need `models/blaze_face_full_range.tflite` (1.1 MB) — download it from https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/1/blaze_face_full_range.tflite (verified HTTP 200, 2026-11-18) into `models/`. Game/gameplay modes do not need it. `track_v3_stable.py` is CLI-only (module-level argparse) — it is invoked as a subprocess by `render_short.py`, never imported; that is by design, not a defect.

### v2.1 — NOT in the ZIP (ordered, not built yet)

- **Per-stream/video FULL LAYOUT PREVIEW (ordered 2026-11-18 00:08 PKT):** dashboard shows, for every stream/video, a full preview from that stream's own frames — facecam box, caption band, crop boundaries, safe zones drawn on the actual frame — before clips are made. Implementation: sample a few frames on add (or a "Preview layout" button), run layout.py detection, render 2–3 annotated preview frames, cache per stream/video until inputs change. Cheap (frames only, no full render).
- **Demo clips (layout agent's job, in progress):** one Short with ROMAN captions (Hindi → Roman) + one PUBG clip with facecam, positions set — `roman_short.mp4` + `pubg_short.mp4` in this folder. Not part of the ZIP (they are renders, not source).
- **Kick capture:** resolver exists (yt-dlp generic) but live capture is UNTESTED. YouTube + Twitch are proven.
- **YouTube live chat:** needs a Google API key — not implemented; loudness + transcript + Twitch chat carry the picking.
- **Instagram / TikTok auto-post:** v2 stubs (need app approvals).

### What still needs his keys / steps (on first run)

1. **Nothing is required to run.** No key needed: picking works on loudness + chat + speech; Roman captions work locally; the dashboard works out of the box.
2. **Optional free Gemini key** (Settings → paste key, or `config.json`): smarter two-pass AI picking + natural Hinglish/Roman Urdu transliteration fallback. Without it, local transliteration + no-AI picking run instead. **PeakCut's rule still applies to PeakCut itself (no key = no auto clips there) — this tool never errors without a key.**
3. **YouTube auto-post (optional):** Google Cloud Console → YouTube Data API v3 → OAuth client (Desktop app) → `client_secrets.json` in this folder → `python post_queue.py --auth` once. Until then clips wait in the queue as `awaiting_setup`.
4. **Facecam/doc modes (optional):** `pip install -r requirements-facecam.txt` + the BlazeFace model file above.
5. **Test the live loop on his PC with a real stream before 19 Nov** (residential connection — Twitch IRC + capture were flaky/blocked from this VM's datacenter egress).
