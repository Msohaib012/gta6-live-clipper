# GTA 6 Live Clipper — FINAL (v2 + Roman fix + positions + all features)

Watches live streams 24/7, catches the best moments, and turns them into vertical **720x1280 60fps Shorts** (or 1080x1920) with professional animated captions — while the stream is still live, so the Short is out **before the streamer publishes their own video**. Finished clips land in a gallery (download them) and in an auto-post queue for YouTube.

Built 2026-11-17 for the GTA 6 launch (19 Nov 2026). Every step was spike-tested on real live streams before this tool was assembled — PeakCut, both ClipKaros, autoclip, OpenShorts, ClipShlip and FuseClip were all tested first; this tool takes the best of each (see BUILD-REPORT.md for the borrow list).

## What's new in FINAL (vs v2)

- **Roman captions FIXED** — Hindi/Urdu streams now render clean natural Roman words. Root cause was Whisper `tiny` mishearing Hindi; the fix is the SMALL model with language forced (no prompt) + a script guard + YouTube auto-subs for VOD. See "Roman captions" below.
- **Positions page (3b)** — every facecam position, every caption position and every output ratio, each with a real preview still (drawn on a real GTA frame). You pick; the renders use your pick. Facecam: auto, top-right, top-left, mid-right, mid-left, top-band, off. Captions: auto, bottom (y~950–1000, the safe band), seam, top. Ratios: 9:16, 1:1, 4:5, 16:9.
- **Per-stream layout preview** — for any stream/video, an annotated preview from ITS OWN frames: facecam box, caption band, crop boundaries and the platform-UI safe zone drawn on the actual frame.
- **"Mark moment" button + "clip that" voice trigger** — press Mark while watching (or the streamer says "clip that" on stream) and the loop clips a Short around that moment, beating the loudness trigger.
- **Approve / Reject in the gallery** — mark clips before posting.
- **Artifact bundle per clip** — every clip gets `transcript.json` + `script.md` + `analysis.json` (title, hook, hashtags) next to it.
- **Speed flags** — `--preset ultrafast|superfast|veryfast|medium`, `--encoder libx264|qsv` (QSV on his PC = the big win; see SPEED-REPORT.md).

## How it works — start to end

**START (you, 2 minutes):** add streams → language is auto-detected (Roman Urdu/Hindi captions by default) → pick styles on the Styles page → star your **favorites in order** → press **Start**.

**MIDDLE (automatic):** the watch loop tracks speech + live chat + loudness → finds the best moment → renders a vertical Short with the **next favorite style** (clip 1 → favorite 1, clip 2 → favorite 2, cycling). No AI key needed — picking works on loudness + chat burst + speech. A free Gemini key (Settings) makes the picking smarter.

**END (you):** clips land in the **Gallery** below the watch panel — watch them and press **Download**, or let the auto-post queue post them to YouTube.

**Your style selection reaches the tool ONLY through the dashboard Styles page** (favorites + order) — no chat round-trip needed per clip. The same flow is shown as a "How it works" strip at the top of the dashboard.

## Why a PC tool with a local dashboard (not a website)

- A public website cannot do this job: no server can cheaply run 24/7 live capture + Whisper transcription + rendering, and yt-dlp/ffmpeg don't run in a browser.
- So: the tool runs on **your PC**, and you use it through a browser page on **localhost** — same pattern as YT Clip Tool and PeakCut. Nothing is uploaded anywhere unless YouTube auto-post is switched on.
- **Hosting question, answered plainly:** a subdomain on a free/5GB shared cPanel host (e.g. "kohetur") **cannot** run this tool. Shared hosting gives you a web folder, not a 24/7 process — it cannot keep a Python process alive watching streams, cannot run ffmpeg + Whisper continuously, and cannot capture live video. This tool needs a real computer that stays on (your PC) — that is why it is a PC tool, not a website.

## Install (Windows — his PC)

1. Install **Python 3.10+** (python.org — tick "Add Python to PATH" during setup).
2. Install **ffmpeg**: `winget install Gyan.FFmpeg` in PowerShell, then close and reopen the terminal.
3. Unzip this folder somewhere short, e.g. `C:\gta6clipper` (deep paths break things on Windows — PeakCut has the same rule, its speech model breaks past 260 chars).
4. `cd C:\gta6clipper` then `python -m pip install -r requirements.txt` (downloads yt-dlp, faster-whisper, Flask, the YouTube API client — a few minutes, once).
5. Facecam / documentary modes only (optional): `python -m pip install -r requirements-facecam.txt`.

Linux is the same minus step 2 (`sudo apt install ffmpeg`).

## Quick start

### Dashboard (recommended)

```
python dashboard.py
```

Open http://127.0.0.1:5000 — it auto-picks the next free port if 5000 is busy.

1. **Find streams** — search YouTube live (no key needed) and click to add, or paste a live link (YouTube / Twitch / Kick / any yt-dlp live URL). The tool checks it is live before adding it. The watch list is saved in `channels.json` and survives restarts.
2. **Watch setup** — style (**★ Favorites rotation** = auto, or one style), crop mode (**Game** default, **Gameplay** = game + facecam corner), **language** (Auto = detect), **captions** (Auto = Roman for Hindi/Urdu), **resolution** (720p default, 1080p quality), segment + trigger. Press **Start watching**. The page shows watch status per channel (live?, segments, clips, chat unique/min), finished clips, and the YouTube post queue. **Clip now** forces the next segment of every channel to render immediately (with the style picked in the dropdown, or the next favorite if rotation is on).
3. **Styles page** — every caption style with a **real animated preview video** (rendered by the tool from a real GTA V frame — CapCut-style). Star styles to mark **favorites**; ▲▼ reorders them. The auto-clip loop rotates through favorites in that order. **Save favorites** to apply.
4. **VOD** — paste any already-uploaded video URL (YouTube etc.) or a local file path → the tool transcribes it, picks the best moments (loudness + speech, or Gemini two-pass if a key is set), renders vertical Shorts into the gallery. Manual start–end window supported. **VOD is Shorts-making only — NOT a clip downloader** (the YT Clip Tool stays a separate tool for downloading clips).
5. **Gallery** — finished clips (live + VOD) with preview + Download, each labelled with its style, trigger (loudness / chat burst / manual mark / "clip that") and channel. **✓ Approve / ✕ Reject** marks clips before posting. Each clip also gets an artifact bundle next to it: `transcript.json` + `script.md` + `analysis.json` (hook, titles, hashtags).
6. **Settings** — optional free Gemini API key (smarter picking + transliteration fallback). Stored in `config.json` on your PC, never shown back, never hard-coded.

## Roman captions (fixed 2026-11-18)

Hindi/Urdu speech → captions in **Roman script** (not Devanagari/Nastaliq), as ordered. The first Roman render garbled words — root cause was Whisper `tiny` mishearing Hindi (it output garbled Roman phonetics), not the transliterator. The fix:

- **Live:** the Roman path (hi/ur) uses the SMALL model with language FORCED and NO prompt, plus a script guard — if more than 30% of the words come out Latin when Roman was wanted, it retries once at temperature=0.
- **VOD:** the tool first tries **YouTube auto-subs** (Hindi auto-subs are clean) and transliterates them locally; Whisper is the fallback.
- Honest limit: live Roman is ~60% intelligible on proper nouns (names, places) — the words are right, the odd name may be misspelled. VOD Roman (auto-subs) is clean.

## Access from anywhere — Cloudflare Tunnel (free)

The dashboard runs on your PC (`http://127.0.0.1:5000`). To reach it from your **phone, anywhere**, without port forwarding:

1. Install `cloudflared` on your PC: `winget install Cloudflare.cloudflared` (PowerShell).
2. `cloudflared tunnel login` — opens a browser; sign in with any Cloudflare account (free, no card).
3. `cloudflared tunnel create gta6-clipper` — note the tunnel ID it prints.
4. `cloudflared tunnel route dns gta6-clipper clips.arenaflix.co.uk` — uses a subdomain of a domain you already own; no new domain to buy.
5. Create `C:\gta6clipper\cloudflared-config.yml`:
   ```yaml
   tunnel: <the tunnel ID from step 3>
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel-ID>.json
   ingress:
     - hostname: clips.arenaflix.co.uk
       service: http://127.0.0.1:5000
     - service: http_status:404
   ```
6. Start the tool, then `cloudflared tunnel --config C:\gta6clipper\cloudflared-config.yml run gta6-clipper`. Now `https://clips.arenaflix.co.uk` opens the dashboard on your phone from anywhere.

Rules: your **PC must be on and the tool running** — the tunnel only forwards, it does not run the engine. Backup route: **Tailscale** (already set up) — open the dashboard at your PC's Tailscale IP:5000. The only way it runs with the PC off is a VPS (monthly cost — not recommended). The dashboard is mobile-friendly for the phone view.

## Positions, ratios & layouts

- **Facecam positions:** auto (per-stream detection picks the freest corner), top-right, top-left, mid-right, mid-left, top-band (full-width band on top, Clipie-style — gameplay below), off.
- **Caption positions:** auto (first free line, caption-Y overlap check), bottom (y~950–1000 — the safe band: below the action centre, above the platform UI that covers the bottom ~300px), seam (right below the facecam band), top.
- **Ratios:** 9:16 (Shorts default), 1:1, 4:5, 16:9. Facecam/gameplay modes are 9:16 only — ratios with facecam are rejected with a clear error; use Game mode for the other ratios.
- Every position has a preview still in the Positions section of the dashboard, drawn on a real GTA frame (source: `static/positions/`). The **per-stream layout preview** (same section) draws the chosen layout on the stream's own frames.
- CLI: `--facecam-pos top-band --caption-pos seam --ratio 1:1` on `clip_live.py`, `vod.py` and `render_short.py`.

### CLI

```bash
# 24/7 watch of a channel list (channels.json), clips saved to clips/
python clip_live.py --channels channels.json --out clips

# one channel, until Ctrl+C
python clip_live.py https://www.twitch.tv/deansocool --out clips

# test: one segment, clip it, exit
python clip_live.py https://www.twitch.tv/deansocool --once --clip-now --style neon

# favorites rotation from the CLI (same as dashboard "auto")
python clip_live.py --channels channels.json --out clips --style auto

# Hindi/Urdu stream: Roman captions, gameplay layout, 1080p
python clip_live.py https://www.twitch.tv/summit1g --once --clip-now \
    --caption-mode auto --mode gameplay --style hype --res 1080

# watch + auto-post to YouTube (needs the one-time OAuth setup below)
python clip_live.py --channels channels.json --out clips --post
```

### Re-render a moment without re-capturing

```
python render_short.py --input capture.mp4 --start 90 --end 130 \
    --transcript transcript.json --style neon --mode game --out short.mp4
```

### VOD from the CLI

```
python vod.py --source "https://www.youtube.com/watch?v=..." --count 3 --out clips
python vod.py --source C:\Videos\stream.mp4 --manual 120-160 --style karaoke --out clips
```

## Caption styles — 25, all with animated previews

The Styles page lists every style with a real looping preview render (4s, from a real GTA V frame). Favorites rotate one by one in the auto-clip loop.

| Style | Look | Source |
|---|---|---|
| **Glow Neon** (default) | Montserrat Black, active word cyan #00E5FF with neon glow | our captions-v2 spike (sohaib: "captions bakiyo se achi") |
| **Hormozi Pop** | Anton ALL-CAPS, active word pops 80→112→100% + turns yellow #FFE600 | our captions-v2 spike (sohaib ❤️ 2026-11-17) |
| **Hype** | Anton ALL-CAPS, active word pops + turns RED #FF2626 | PeakCut "hype" (sohaib: "PeakCut ki hi achi lagi — text color, font etc") |
| **Karaoke** | Accent sweeps left→right through the active word | aman ClipKaro karaoke (`\kf` sweep, PNG version) |
| **Pop** | Per-word scale bounce | autoclip pop |
| **Fade** | Fade-in + slide-up | FuseClip V2 fade |
| **Box** | Opaque yellow highlighter box behind the spoken word | autoclip boxed / aman ClipKaro box |
| **Pulse** | Word scales to 122% on the beat | FuseClip V2 pulse |
| **Boxed** | Dark box + blue active word | autoclip boxed |
| **Cinematic** | Lowercase, plate behind text, desaturated grade feel | autoclip cinematic |
| **Classic** | Thin outline, clean white | autoclip classic |
| **Minimal** | Fade + accent keywords only | aman ClipKaro minimal |
| **Bold** | 1.15x scale + thick outline | aman ClipKaro bold |
| **Comic** | Bangers font, active word tilted −4° | aman ClipKaro comic |
| **Typewriter** | Letter-by-letter reveal | remotion-captioneer typewriter |
| **Pill** | White rounded rect behind the caption | remotion-captioneer pill / OpenShorts pill |
| **Bounce** | Active word bounces with a spring + turns cyan | remotion-captioneer bounce (Beasty/Mozi) |
| **Rawani** | Glowing teal underline extends beneath words as speech continues | **FuseClip Rawani** (extracted from the 0.75.1 binary) |
| **Lafz** | One bold outlined word at a time, popping in | **FuseClip Lafz** (single-word display mode) |
| **Aks** | Oversized words; spoken word inverted (black on white) | **FuseClip Aks** |
| **Lakeer** | Crisp blue underline on the active word only | **FuseClip Lakeer** |
| **Ali Abdaal** | White rounded card; gray words turn black as spoken | **FuseClip Ali Abdaal** (white-karaoke) |
| **Shamani** | Glowing yellow, gentle floating wave, pop-and-fade entrance | **FuseClip Shamani** |
| **Saaf** | Clean bold white; each spoken word slides up into place | **FuseClip Saaf** |
| **Lehar** | Violet pill fills left-to-right as words are spoken | **FuseClip Lehar** (filled-tube) |

FuseClip's 18 real presets were extracted from its closed 0.75.1 release (never run, asar inspected — see `~/workspace/fuseclip-styles/FUSECLIP-STYLES.md` in the research notes). Corrections from that extraction: there is **no "typewriter" in FuseClip** (that word is from the sibling Fuse Caption Studio's marketing) and **no "neon"/"hormozi" named styles** — the closest real things are the glow presets (Mumbai, Shamani, Karachi). Our typewriter stays, credited to remotion-captioneer.

**Chunking rule (all styles):** max 3 words per chunk (autoclip pagination math, 2-line guarantee) — except Lafz, which is 1 word at a time by design. **Three missing-subtitle bugs found in testing are fixed in this build:** words straddling the clip end, zero-duration words, and captions blanking between words.

**Caption band (2026-11-17, corrected twice by sohaib):** burned captions sit at **y~950–1000** on a 720x1280 frame (`CAP_Y = 850` in `captions.py`, single source of truth) — below the action centre, above the platforms' own UI (username + caption text starts ~y=1080). The first attempt (y=980–1160) hid behind the platform UI; the second (y=800–899, middle of the frame) covered the gameplay — both rejected. The band is adaptive: if the facecam or a game HUD occupies it, captions move to the nearest free line above it (650, then 450) — see `layout.py`.

## How it works (engine)

1. **Capture** — yt-dlp records each live stream at **1080p60** in short segments (default 3 min). Twitch live max is 1080p60 (no 1440p/4K live). Capture runs ~1.05x real-time. Channels are watched **round-robin** (one segment per channel per round) because a home PC can't capture + transcribe several 1080p60 streams in parallel. With 3 channels at 3-min segments, each channel is checked every ~9 min.
2. **Moment pick (no AI by default)** — faster-whisper `tiny` transcribes the segment (~15s per 3 min) + an audio-energy profile. The best 30–45s window is the loudest 5s window that has speech in it, with 8s of lead-in so the hook lands in the first seconds. **Live chat (Twitch IRC, people not messages — PeakCut's rule, bot storms don't fake a crowd)** is tracked per channel; if the crowd peaks somewhere else and there is real speech there, the clip re-centres on the burst (needs ≥5 unique chatters). **YouTube live chat is NOT tracked** (needs a Google API key — loudness + transcript still work). With a free Gemini key (Settings), two-pass AI picking runs instead (score windows → shortlist → word-snapped boundaries, the OpenShorts shape) — but the no-AI path is always the fallback; **a key is never required**.
3. **Trigger** — a clip renders when the loudest window is at/above −28 dBFS, or on demand (Clip now / `--clip-now`), or when a chat burst fires.
4. **Render** — the clip is extracted as a **60fps image sequence first** (raw live captures have broken timestamps — never edit them directly), then 608x1080 centre crop → lanczos upscale to 720x1280 → captions → H.264 CRF 18, preset `veryfast`. **Moment → Short in ~3–5 min.**
5. **Captions** — PIL PNG overlays composited as ONE transparent caption-track video (not libass ASS, so Devanagari/Roman Urdu render correctly — the ASS path misshaped Devanagari in testing). 25 styles (above), all PNG-based.
6. **Languages** — faster-whisper **auto-detects** the spoken language (Hindi detected at 0.83 on the Kakori clip). **Hindi/Urdu speech gets ROMAN captions by default** (sohaib's order — PeakCut only ever did English while the streamers speak Roman Urdu/Hindi). Whisper writes Hindi as Devanagari / Urdu as Nastaliq; `transliterate.py` converts the words to Roman with a local library first (indic-transliteration + a Hinglish normalizer + an overrides dict for common words: azad, karna, school, mujhe, mein...). A Gemini path (natural Hinglish/Roman Urdu) is available when the user puts their free Gemini key in `config.json` (`roman_mode="gemini"`, key never hard-coded). Other languages caption in their own script (Devanagari via Noto Sans Devanagari Bold, Nastaliq via Noto Nastaliq Urdu, both shaped with PIL + libraqm). Per-channel language in `channels.json` (`{"url": ..., "language": "hi", "caption_mode": "hi-roman", "mode": "gameplay"}`); dashboard language picker = global default. **Honest limit:** Whisper `tiny`'s Hindi is rough — it Romanizes Hindi phonetically and garbles words ("embeho", "tritava"); detection is right, transcription is ~60–70% intelligible. `small` is no better (it wrote Urdu Nastaliq with errors + hallucinated Chinese characters) and 10x slower (100s for 41s audio — unusable in the live loop). So `tiny` stays the live model; the transliteration path is proven correct on clean Devanagari/Nastaliq input (e.g. YouTube auto-subs transcripts).
7. **Post queue** — every clip is queued (`queue.json`) with a title from its loudest moment. A worker uploads to YouTube with spacing (30 min) and a daily cap (10). See below.

## Stream discovery — what is proven vs untested

| Site | Search | Paste link + live check | Live capture |
|---|---|---|---|
| YouTube | **PROVEN 2026-11-17** (yt-dlp ytsearch, live search returned real streams) | **PROVEN** (resolve + `is_live`) | **PROVEN** (PeakCut spike: real YouTube-live GTA V RP, 167s, buffer filled, 30s clip rendered) |
| Twitch | n/a (search is YouTube-only) | **PROVEN** (resolve + `is_live` 1080p60 check) | **PROVEN** (multiple 1080p60 captures, 1.01–1.09x real-time; ClipKaro's live buffer stalled on this VM's broken HLS timestamps — our capture re-encodes from frames, so it is not affected) |
| Kick | n/a | **UNTESTED** (yt-dlp generic resolver — should work, not verified) | **UNTESTED** |
| Other live sites | n/a | via yt-dlp generic resolver where possible — **UNTESTED** | **UNTESTED** |

## YouTube auto-post — one-time setup (his step)

1. Google Cloud Console → create a project → enable **YouTube Data API v3** → Credentials → create **OAuth client ID (Desktop app)** → download the JSON as `client_secrets.json` into this folder.
2. Run `python post_queue.py --auth` once: a browser opens, sign in with the YouTube channel's Google account, allow. The consent is stored in `token.json` — **keep it private, never commit it, never share it**.
3. From then on the worker uploads automatically. Until setup is done, clips wait in the queue as `awaiting_setup` — never lost, never failed. Check status any time: `python post_queue.py --status`.

**Instagram / TikTok auto-post is v2, not in this build** — both need app approvals + review before automated posting works (Meta's Instagram Graph API, TikTok's Content Posting API). Nothing is faked for them; the queue format already carries per-platform status so v2 can add them.

## Crop modes + resolution

- **game** (default) — centre crop. Correct for gameplay: the action is centred, the camera is stable.
- **gameplay** — PeakCut-style: same centre-crop game base + the streamer's facecam as a **288x162 overlay (40% of width — BIGGER than PeakCut's 30%)** in an auto-checked corner (top-right preferred; bottom corners excluded — platform UI zone). The facecam rectangle is detected per clip (still-border detection + MediaPipe face confirmation, falls back to the face box, falls back to plain game mode when no facecam exists). Captions sit at the auto-checked Y (see above). Needs `requirements-facecam.txt`.
- **facecam** — V3 stabilized tracker (face-ID persistence + hysteresis + two-rate EMA + deadband; sign reversals 50→3 measured) + PCHIP 60Hz interpolation. For podcasts / facecam streams. Needs `requirements-facecam.txt`.
- **doc** — static crop per shot (crop held constant inside a shot, changes only at cuts — the ClipKaro method). For documentaries, where the camera itself pans/drifts. Needs `requirements-facecam.txt`.

**Resolution:** `--res 720` (720x1280 60fps, default — fast) or `--res 1080` (1080x1920 60fps, quality — PeakCut's clip was 1080-wide and sohaib liked it). 1080 renders natively (captions drawn at 1080, crop 912x1080, same 1.18x upscale) — not an upscale of the 720 render. Costs more render time (measured on this VM — see BUILD-REPORT).

## VOD mode

Paste a video URL or local file → transcript (faster-whisper, 10-min chunks) → best moments picked by the **8-signal virality scorer** (loudness + speech density + hype keywords + hook + quotable + story-peak, no AI) or **Gemini two-pass picking** when a key is set → same vertical render + captions + gallery. Source file is deleted after rendering (only the picked ranges are ever downloaded — never the whole video when a URL is given: yt-dlp `--download-sections`). Manual start–end window supported. **VOD is Shorts-making only — NOT a clip downloader.**

## Limits (honest)

- **Per-stream layout preview is v2.1, not in this build** (ordered 2026-11-18 00:08 PKT): the dashboard will show, for every stream/video, a full preview from that stream's own frames — facecam box, caption band, crop boundaries, safe zones drawn on the actual frame — before clips are made. In v2, the layout is auto-checked per clip at render time (layout.py), but there is no per-stream preview card yet.

- Twitch live max is **1080p60** — there is no 1440p/4K live quality to capture.
- Old games look old: GTA III's 2001 textures are the ceiling, no pipeline fixes that. GTA V measured ~3.4x sharper than GTA III with the same pipeline; GTA VI will look better still.
- The centre crop cuts the streamer's facecam off (bottom corner). Gameplay only, by design (use `gameplay` mode to keep it).
- A clip needs speech: a 40s window with no talking produces no usable moment and is skipped.
- Whisper `tiny` is fast but not perfect — a misheard word in a caption is possible.
- YouTube OAuth cannot be tested end-to-end on a dev machine — it needs his Google account on first run (`python post_queue.py --auth`).
- **PeakCut rule (still true):** no AI key = no AI picking. The no-AI path (loudness + chat + speech) always works; the Gemini two-pass path needs his free key in Settings.
- ClipKaro's live buffer stalled on this VM (broken HLS timestamps) — our capture path re-encodes from frames, so it is not affected, but **test the live loop on his PC with a real stream before 19 Nov**.
- This tool clips **other people's live streams**. Only clip streams you have permission to use, or streams whose owners welcome clipping — same rule PeakCut states in its README.

## Files

| File | What it is |
|---|---|
| `clip_live.py` | Main tool: 24/7 watch loop (round-robin channels), triggers, favorites rotation, chat-burst re-centring |
| `dashboard.py` + `dashboard.html` | Local browser UI (Flask, http://127.0.0.1:5000) — search, paste link, Styles page, VOD, gallery, settings. **Placeholder UI — Claude will redesign.** |
| `capture.py` | yt-dlp 1080p60 segment capture + `.part` recovery + live check |
| `transcribe.py` | faster-whisper tiny + auto-detect + audio-energy profile + moment pick |
| `transliterate.py` | Devanagari/Nastaliq → Roman (local) + Gemini path (user's key) |
| `render_short.py` | Standalone render: capture + window → finished Short |
| `captions.py` | **25 caption styles** (PIL PNG track builder) — neon/hormozi/hype + autoclip/aman/remotion + 8 FuseClip styles |
| `layout.py` | Facecam auto-detection + corner/caption-Y auto-check (gameplay mode) |
| `track_v3_stable.py` | V3 stabilized face tracker (facecam/doc modes) |
| `build_track60.py` | 10Hz samples → 60Hz sendcmd crop track (smooth PCHIP / static per shot) |
| `post_queue.py` | Auto-post queue + YouTube upload (OAuth) |
| `stream_discovery.py` | YouTube live search (yt-dlp ytsearch, no key) + paste-link resolver |
| `chat.py` | Twitch IRC chat tracker — unique chatters per minute, burst windows |
| `settings.py` | Favorites + rotation state + Gemini key (config.json) |
| `vod.py` | VOD mode: URL/file → transcript → 8-signal / Gemini picking → Shorts |
| `preview_styles.py` | Builds the animated style previews (static/previews/*.mp4) |
| `static/previews/` | 25 animated style previews + index.json (served by the dashboard) |
| `assets/sample_bg.jpg` | Real GTA V frame used as the preview background |
| `channels.json` | Example channel list (edit with the GTA 6 streamers) |
| `fonts/` | Anton + Montserrat Black + Noto Sans Devanagari Bold + Noto Nastaliq Urdu + Bangers (OFL, bundled) |
| `models/` | BlazeFace full-range model (face tracker) — **NOT in the ZIP** (source-only). For facecam/doc modes, download `blaze_face_full_range.tflite` into `models/` from https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/1/blaze_face_full_range.tflite (1.1 MB, free). Game/gameplay modes do not need it. |
| `example_short.mp4` | Example Short rendered with this tool (Glow Neon) |
| `requirements.txt` | Core deps · `requirements-facecam.txt` — facecam/doc deps |

## Troubleshooting

- **"channel is not live"** — the streamer is offline; the watcher retries every round. Check the URL in a browser.
- **yt-dlp errors on a channel** — update it: `python -m pip install -U yt-dlp`. Twitch changes things often.
- **No clips from a segment** — the segment had no loud moment with speech (quiet gameplay). Lower `--threshold-db` (e.g. −32) or press **Clip now**. (Calibrated 2026-11-17: deansocool's loudest 5s window was −28.6 dB, so the default is −28 — a quieter talk-heavy RP stream still triggers, while −25 missed it.)
- **Render is slow** — `veryfast` is already the live preset. Full 40s render takes ~2 min on this VM; his PC (i5, no GPU) will be similar or a bit slower. Don't use `medium`.
- **Dashboard port busy** — it auto-picks the next free port and prints it.
- **Style previews missing** — run `python preview_styles.py --rebuild` (needs the GTA V frame in `assets/` or it falls back to a dark frame).
- **"AI error: The AI is not set up"** — that is PeakCut's message, not this tool's. This tool never errors without a key: the no-AI path always runs. Add a free Gemini key in Settings only if you want smarter picking.
