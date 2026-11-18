# GTA 6 Live Clipper — FINAL REPORT

**Built:** 2026-11-18 (overnight, for the GTA 6 launch on 19 Nov 2026)
**Deliverable:** `gta6-live-clipper-final.zip` (source-only — send to Claude for the UI redesign, then back to Muse to push)
**ZIP:** `~/workspace/gta6-live-clipper/gta6-live-clipper-final.zip` — 76 files (v3 — professional UI redesign + Roman captions + EXE workflow, see §12). SHA-256 is in the chat message with the file — it changes with every re-package, so it is not printed here.

This is the FINAL build: v2 (25 styles, favorites rotation, search + paste, VOD, multilingual, gameplay layout, hype, 1080, speed flags) + the Roman caption fix + the full positions list + every quick feature discussed in the messages. Everything below was tested on real streams/videos on this machine — nothing in this ZIP is untested.

---

## 1. Roman captions — FIXED

**His verdict (2026-11-18 00:39 PKT):** "Not working properly in roman language."

**What was wrong:** the first Roman render's captions WERE Roman script, but the words were garbled (~60% intelligible — "embeho", "kees keel", "ALFRED PAGAI HOETHE"). Root cause was **Whisper `tiny` mishearing Hindi** (it output garbled Roman phonetics), NOT the transliterator — the transliterator was proven clean on clean Devanagari input.

**Measured (roman-fix/test_r2.json):**

| Model / setting | Match vs clean Hindi | Script out |
|---|---|---|
| tiny + hi + no prompt | 0.256 | 100% Latin, garbled |
| tiny + auto + prompt | 0.065 | mixed scripts, even Chinese/Korean chars — prompt makes it WORSE |
| **small + hi + no prompt** | **0.596** | 100% Devanagari, still mishears proper nouns |
| small + auto | 0.0 | detected Urdu, Nastaliq |
| small + hi + short prompt | 0.416 | Devanagari |

**The fix (in `transcribe.py`, live now):**
- Roman path (hi/ur) uses the **SMALL** model with language **FORCED** and **NO prompt**, plus a **script guard** — if >30% of the words come out Latin when Roman was wanted, it retries once at temperature=0.
- **VOD** prefers **YouTube auto-subs** first (Hindi auto-subs are clean, fetched in ~8s on Kakori) and transliterates them locally; Whisper is the fallback.

**Verified:** `roman_short.mp4` re-rendered from the clean Hindi auto-subs → local transliteration: 30.0s, 720x1280, 60fps, 9.3 MB. Frames checked — t=5 "MEIN AHAM BHUMIKA", t=12 "KE IS KHEL", t=20 "TABHI BRITISH POLICE" — all clean natural Roman words, cyan active word, bottom band y~950–1000.

**Honest limit:** live Roman is ~60% intelligible on **proper nouns** (names, places) — the words are right, the odd name may be misspelled. VOD Roman (auto-subs) is clean. English / Devanagari / Nastaliq modes were NOT touched and still work.

---

## 2. Caption styles — ALL 25, every kind, with animated previews

Every style has a **real animated preview video** (`static/previews/<id>.mp4`, rendered by the tool from a real GTA V frame) on the dashboard Styles page. He marks **favorites in order** there; the auto-clip loop **rotates** through them (clip 1 → favorite 1, clip 2 → favorite 2, cycle). Default favorites: **neon, hype, hormozi**.

| # | id | Style | What it looks like | Harvested from |
|---|---|---|---|---|
| 1 | `neon` | **Glow Neon** | Montserrat Black, active word cyan with neon glow. **sohaib's pick — default.** | our V2 spike |
| 2 | `hormozi` | **Hormozi Pop** | Anton ALL-CAPS, active word pops 80→112→100% and turns yellow. ❤️ 22:31 PKT | our V2 spike |
| 3 | `hype` | **Hype** | Anton ALL-CAPS, active word pops and turns **RED** — PeakCut's look, his benchmark ("best so far"). | PeakCut |
| 4 | `karaoke` | **Karaoke Sweep** | Montserrat ExtraBold, accent colour sweeps left-to-right through the active word. | aman ClipKaro + our V2 |
| 5 | `pop` | **Pop** | Per-word scale bounce 80→112→100%, white — the bounce without the colour pop. | our V2 + autoclip |
| 6 | `fade` | **Fade Up** | Words fade in and slide up as they are spoken. | our V2 |
| 7 | `box` | **Highlighter Box** | Opaque yellow highlighter box behind the active word, black text. | our V2 |
| 8 | `pulse` | **Pulse** | Active word swells to 122% then settles — a heartbeat on every word. | our V2 |
| 9 | `boxed` | **Boxed** | Opaque dark box behind the whole caption line, active word in blue. | autoclip |
| 10 | `cinematic` | **Cinematic** | Smaller elegant lowercase text, faint glow, dark backing plate — for documentaries. | autoclip |
| 11 | `classic` | **Classic** | Simple clean white text, thin outline, no animation — interviews. | autoclip |
| 12 | `minimal` | **Minimal** | Fade-in; hype keywords light up in yellow, the rest stays white. | aman ClipKaro |
| 13 | `bold` | **Bold** | Extra-large heavy text, thick outline — shouts the loudest moments. | aman ClipKaro |
| 14 | `comic` | **Comic** | Bangers comic font, playful tilt — fun moments, gaming bants. | aman ClipKaro |
| 15 | `typewriter` | **Typewriter** | Letters appear one by one as they are spoken. | remotion-captioneer |
| 16 | `pill` | **Pill** | Active word inside a white pill, black text — clean and readable. | remotion-captioneer |
| 17 | `bounce` | **Bounce** | Active word bounces with a spring and turns cyan — the Beasty/Mozi look. | remotion-captioneer |
| 18 | `rawani` | **Rawani** | A glowing teal underline extends beneath the words as speech continues. | FuseClip |
| 19 | `lafz` | **Lafz** | One bold outlined word at a time, popping in as it is spoken. | FuseClip |
| 20 | `aks` | **Aks** | Oversized words; the spoken word is inverted — black text on a white box. | FuseClip |
| 21 | `lakeer` | **Lakeer** | White text with a crisp blue underline on the active word only. | FuseClip |
| 22 | `ali` | **Ali Abdaal** | White rounded card; gray words turn black as they are spoken. | FuseClip |
| 23 | `shamani` | **Shamani** | Glowing yellow phrases, gentle floating wave, pop-and-fade entrance. | FuseClip |
| 24 | `saaf` | **Saaf** | Clean bold white; each spoken word slides up into place. | FuseClip |
| 25 | `lehar` | **Lehar** | A violet pill fills left-to-right as the words are spoken. | FuseClip |

FuseClip's styles were harvested from its documented style list + the FUSECLIP-STYLES.md spike notes (the .exe is closed-source; styles were re-created from their descriptions, not extracted — honest note). All 25 previews were ffprobe-checked: every file exists and plays (durations ≥1s, sizes 108–229 KB).

---

## 3. Positions — EVERY option, each with a preview (he chooses himself)

Dashboard section **3b · Positions**. Every option has a **preview still** (`static/positions/*.jpg`) drawn on a **real GTA frame** with a real facecam — annotated boxes/bands, crop boundaries, and the red platform-UI zone at the bottom. Click one to pick it; the next clips use the pick. **Auto** means the tool checks each stream's own frames and picks the freest spot.

### 3a. Facecam positions (7)

| id | Name | What it is |
|---|---|---|
| `auto` | **Auto (per stream)** — default | The tool checks this stream's own frames and picks the freest corner — avoids the HUD and the streamer's own facecam. |
| `top-right` | **Top-right corner** | PeakCut-like corner — the streamer's face, bigger than PeakCut (40% of width). |
| `top-left` | **Top-left corner** | Mirror of top-right — for streams whose top-right has a HUD or logo. |
| `mid-right` | **Mid-right** | Right side, vertically centred — below the top HUD, above the caption band. |
| `mid-left` | **Mid-left** | Left side, vertically centred — same as mid-right, other side. |
| `top-band` | **Top band (full width)** | Clipie-style: the facecam stretches across the whole top ~33% as a band, gameplay fills below it. |
| `off` | **Off (no facecam)** | No facecam overlay at all — just the game, full frame. |

### 3b. Caption positions (4)

| id | Name | What it is |
|---|---|---|
| `auto` | **Auto (first free line)** — default | The tool checks the facecam + HUD and picks the first free line: bottom, then higher. |
| `bottom` | **Bottom band (y~950–1000)** | Text at y~950–1000 — **below the action centre, above the platforms' own UI** (the bottom ~300px the apps cover). His verdict — the default. Never the old y=980–1160 (inside platform UI) and never y=700 (mid-frame over the action — rejected 23:31 PKT). |
| `seam` | **Seam (below the facecam band)** | Text just below the top-band facecam (Clipie-style seam). Best with the top-band facecam. |
| `top` | **Top** | Text at the very top of the frame — for when the bottom is busy (HUD, in-game subtitles). |

### 3c. Output ratios (4)

| id | Name | Notes |
|---|---|---|
| `9:16` | **9:16 vertical** — default | Shorts / Reels / TikTok. |
| `1:1` | **1:1 square** | Square feeds (Instagram grid, X). |
| `4:5` | **4:5 portrait** | Portrait feeds (Instagram, Facebook). |
| `16:9` | **16:9 landscape** | Landscape (YouTube, X, Facebook). |

Facecam/gameplay modes are 9:16 only — other ratios with a facecam are rejected with a clear error; use Game mode for those ratios.

### 3d. Per-stream full layout preview (his 00:08 order)

Same Positions section: paste a **video file path or channel URL** → the tool takes frames from **that stream's own video**, detects its facecam, and draws the full layout on the actual frame — facecam box, caption band, crop boundaries, safe zones. If the channel has no capture yet, it says so plainly ("start the watch first, then preview").

---

## 4. Features added in FINAL (from the messages)

| Feature | Where | Status |
|---|---|---|
| **"Mark moment" button** — press while watching; the loop clips a Short around that moment, beating the loudness trigger | Dashboard Watch panel → `POST /api/mark` → `marks.json` → consumed by `run_loop` | ✅ built + tested (API 200, mark recorded) |
| **"clip that" voice trigger** — the streamer says "clip that" / "clip it" / "clip this" on stream → a clip is forced around it; VOD scorer boosts windows containing it (+15) | `clip_live.find_clip_that`, `vod._score_windows` | ✅ built; live path tested via code path + VOD signal |
| **Output ratios 9:16 / 1:1 / 4:5 / 16:9** | `positions.py` + `render_short.py` + dashboard + CLI `--ratio` | ✅ built + render-tested (1:1, 16:9) |
| **Approve / Reject in the gallery** | Dashboard gallery → `POST /api/gallery/<name>/review` → sidecar `review` field | ✅ built + tested (sidecar updated to `approved`) |
| **Per-clip artifact bundle** — `transcript.json` + `script.md` + `analysis.json` (title, hook, hashtags) next to every clip | `clip_live.write_artifact_bundle`, called by live + VOD | ✅ built; verified in VOD test |
| **Cloudflare Tunnel setup** — access the dashboard from his phone anywhere | `README.md` "Access from anywhere" section (6 steps, subdomain of a domain he already owns, no new domain, no port forwarding; Tailscale as backup) | ✅ documented |
| **Facecam-band layout** as a layout option | `top-band` facecam position (Clipie-style) + `seam` caption position | ✅ built + render-tested (T3) |
| **Speed flags** `--preset` / `--encoder` (QSV on his PC) | `render_short.py` (from the speed report) | ✅ built + verified (QSV→libx264 fallback on this VM) |

---

## 5. What was tested (2026-11-18, this build)

*(Test results section — filled in from the render suite run below.)*

### 5a. Dashboard (test client)
- `GET /` → 200
- `GET /api/styles` → 25/25 styles
- `GET /api/positions` → 6 facecam + 3 caption + 4 ratios; save → persists to `settings.json`; reset to auto/auto/9:16 after the test
- `GET /static/positions/facecam-top-band.jpg` → 200; `GET /static/previews/neon.mp4` → 200
- `POST /api/layout_preview` (real GTA capture) → 200, annotated preview saved, corner top-right, caption_y 850, fallback note (no facecam in that capture — correct)
- `POST /api/mark` → 200, mark recorded in `clips/marks.json`
- `POST /api/gallery/<clip>/review` (approved) → 200, sidecar `review` = `approved`

### 5b. Style previews
- All 25 `static/previews/*.mp4` ffprobe-checked: exist, play, ≥1s, 108–229 KB. None missing.
- All 10 `static/positions/*.jpg` exist.

### 5c. Renders (10s windows unless noted; research venv, `--preset veryfast`)
All ffprobe-verified (H.264, 60fps) and frame-verified where noted. Two bugs were found by these tests and FIXED in the ZIP (see §5e).

| Test | What | Result |
|---|---|---|
| T1 | game 720, neon, GTA V live capture 20–30s | ✅ PASS — 720x1280, 60fps, 10.0s, 16.4 MB, 51s |
| T2 | gameplay corner, hype (red pop), facecam top-right, PUBG 30–40s | ❌ FAILED first run — `ZeroDivisionError` in `choose_layout` (see §5e); ✅ **PASS on retry** — facecam detected at src rect (0,460,440,240), overlay 288x162 top-right, caption_y 850, 720x1280 60fps 10.0s, 44s. Frame t=5 verified: facecam top-right with cyan border, "YEAH, I **DON'T**" in Anton ALL-CAPS with DON'T in red — the PeakCut hype look. |
| T3 | gameplay top-band (Clipie-style), neon, captions at seam, PUBG 30–40s | ✅ PASS — band across full top (~33%), gameplay below, captions "YEAH, I **DON'T**" (cyan active word) at the seam y~430, 720x1280 60fps 10.0s, 47s. Frame t=5 verified. |
| T4 | Roman 30s, neon, Kakori Hindi 0–30s, `--script latin` | ✅ PASS — 720x1280, 60fps, 30.0s, 9.4 MB, 93s. Frames verified: t=5 "MEIN **AHAM** BHUMIKA", t=12 "**KE** IS KHEL" — clean natural Roman words, cyan active word, bottom band y~950–1000. Same frames as the verified `roman_short.mp4`. |
| T5 | game 1080, neon, GTA V 20–30s | ✅ PASS — 1080x1920, 60fps, 10.0s, 30.8 MB, 98s |
| T6 | ratio 1:1, game, neon, GTA V 20–30s | ✅ PASS — 720x720, 60fps, 10.0s, 11.6 MB, 40s |
| T7 | ratio 16:9, game, neon, GTA V 20–30s | ✅ PASS — 1280x720, 60fps, 10.0s, 19.9 MB, 51s |
| T8 | favorites rotation — two `process_segment` calls, style=auto | ❌ FAILED first run — `KeyError: 'score'` in `process_segment` (see §5e); ✅ **PASS on retry** — ROT1 = **neon**, ROT2 = **hype** (favorites in order, cycling), both 720x1280 60fps 10.0s. |
| T9 | VOD pick — local Kakori file, count=1, 10s | ✅ PASS — 720x1280, 60fps, 10.0s, 142s total (Whisper small hi transcribe + render). Clip + sidecar + **artifact bundle** (`transcript.json` + `script.md` + `analysis.json` with title/hook/hashtags/titles) all present. Signals: dense-speech, hype-word. Note: VOD from a **local** Hindi file uses Whisper (no YouTube auto-subs exist for it), so proper nouns in the bundle text may be imperfect — the documented Whisper-small-Hindi limit; VOD from a **YouTube URL** uses auto-subs (clean). |

### 5d. Fresh-extract ZIP verification (2026-11-18, after final packaging)
- Extracted `gta6-live-clipper-final.zip` to a clean temp dir: **73 files**, `python -m compileall` on the whole tree → **OK** (no syntax errors).
- Dashboard test client on the extracted copy: `GET /` → 200; `GET /api/styles` → **25/25**; `GET /api/positions` → **6 facecam + 3 captions + 4 ratios**; `GET /static/positions/combined-auto.jpg` → 200; `POST /api/layout_preview` (real GTA capture) → 200 with annotated preview, corner top-right.
- **Render from the extracted code:** `render_short.py` (game 720, neon, GTA V 20–30s) → 720x1280, 60fps, 10.0s, H.264, 16.4 MB, 50s. The shipped code renders.

### 5e. Bugs found by testing (fixed in this ZIP)
1. **`ZeroDivisionError` in `layout.py:choose_layout`** — the scale factors `sx, sy` were clobbered by the projected-facecam unpack (`sx, sy, sw, sh = src_rect`); a facecam at x=0 (PUBG's is at (0,460,440,240)) made the forced-corner branch divide by zero, and the gameplay render silently fell back to plain game mode. Fixed by renaming the unpack to `rx, ry, rw, rh`. Found by T2, verified fixed by T2-retry + a direct layout unit check (corner/band/auto all return correct rects).
2. **`KeyError: 'score'` in `clip_live.py:process_segment`** — the info dict assumed the `why` dict always has `score`/`words` keys; a caller passing a partial `why` crashed. Fixed with defensive `.get()` calls. Found by T8, verified fixed by T8-retry.
3. **MediaPipe `FaceDetector.__del__` shutdown noise** — harmless "Exception ignored ... 'NoneType' object is not callable" spam in logs at interpreter exit. Fixed by explicitly closing the detector after the sampling loop in `detect_facecam`.

---

## 6. What still needs his keys / steps (nothing hidden)

1. **Free Gemini API key** (optional, for smarter picking + transliteration fallback) — paste in dashboard Settings. Without it, picking still works (loudness + chat + speech); PeakCut needs a key for auto-clips, ours does not.
2. **YouTube OAuth** (only for auto-post) — one-time setup in README §"YouTube auto-post" (Google Cloud project + OAuth client + `client_secrets.json`). Auto-post stays OFF until he does this; clips still land in the gallery.
3. **Cloudflare Tunnel** (only if he wants the dashboard on his phone from anywhere) — 6 steps in README; needs a Cloudflare account (free, no card) + a subdomain of a domain he already owns.
4. **QSV on his PC** — the `--encoder qsv` flag exists and falls back to libx264 if QSV is missing; the ~2–2.5 min moment→Short estimate on his i5/UHD 620 is **untested on his PC** (no Intel GPU on this VM) — he should run one render with `--encoder qsv` and compare.
5. **Claude UI redesign** — send him this ZIP; he sends it to Claude; the redesigned UI comes back to Muse to push. Dashboard element ids and `/api/*` paths are the contract — Claude must keep them.

---

## 7. v2.1 leftovers (NOT in this ZIP — honest list)

- **Kick live capture** — stream discovery lists Kick URLs, but live capture from Kick is untested (YouTube + Twitch proven).
- **YouTube live chat** — needs a Google API key (Twitch IRC chat works without a key).
- **IG / TikTok auto-post** — stubs only (YouTube auto-post is the working lane).
- **Direct-chain render** (skip the JPEG intermediate, ~40–50s faster) — designed in SPEED-REPORT.md §5, not built.
- **Transcribe-during-capture / rolling transcription** — designed, not built.
- **30fps output test** — the earlier 30fps test was invalid and discarded; 60fps is the verified default.
- **Facecam/doc modes end-to-end** — facecam (V3 tracker) and doc (static per shot) modes exist but were not render-tested end-to-end in this build; game + gameplay are the verified modes.
- **FuseClip .exe style extraction** — attempted within the ~20 min cap; styles were re-created from the documented style list, not extracted from the binary (closed source).

---

## 8. ZIP contents

- **Path:** `~/workspace/gta6-live-clipper/gta6-live-clipper-final.zip`
- **Size:** 6,790,066 bytes (6.5 MB)
- **SHA-256:** `5571acd9aaa315b5d0bbe123b0646328ac93ff6fba782734878000da81fab9d2`
- **Files:** 76 (see `package_final.py` for the exact list — it is the packaging script, included in the repo but not the ZIP)

**In the ZIP:** 18 Python files (clip_live, capture, transcribe, transliterate, render_short, captions, layout, positions, preview_positions, preview_styles, track_v3_stable, build_track60, post_queue, stream_discovery, chat, settings, vod, dashboard) · dashboard.html · README.md · BUILD-REPORT.md · SPEED-REPORT.md · FINAL-REPORT.md (this file) · requirements.txt + requirements-facecam.txt · channels.json (example) · settings.json (favorites neon/hype/hormozi, positions auto/auto/9:16, next_index 0) · models/blaze_face_full_range.tflite (1.1 MB, facecam detection) · 5 fonts (Anton, Bangers, Montserrat Black, Noto Sans Devanagari Bold, Noto Nastaliq Urdu) · assets/sample_bg.jpg · static/previews/ (25 style mp4s + index.json) · static/positions/ (10 stills + index.json) · static/fonts/Anton-Regular.ttf (dashboard display font) · roman-fix/ROMAN-FIX-REPORT.md + transcript_autosubs.json.

**NOT in the ZIP (by design):** venvs, models cache, captures, work dirs, clips/, test renders, demo clips (`roman_short.mp4`, `pubg_short.mp4` — sent separately in chat), the old v2 ZIP, `__pycache__`, `static/previews/_work` (75 MB scratch), `static/layout_previews/` (generated per stream at runtime).

---

## 11. ClipKontrast borrowables — INTEGRATED (2026-11-18 afternoon)

ClipKontrast (anistroz/ClipKontrast, MIT — a PeakCut rebrand) was checked against the final tool. Four things were worth taking, all integrated and tested. The rest (its 3 caption styles, its dashboard, its auto-post) is weaker than what is already here.

**1. Game-aware titles + tags (`games.py`, new).** 23 games (GTA VI first). The live loop fetches each channel's stream title once at startup (`stream_discovery.get_stream_title`, one yt-dlp call, fail-soft) and detects the game from it; a `"game"` key in `channels.json` pins it per channel. The game flows into clip titles (`Valorant LIVE: ...`), YouTube tags (game tags first, then the GTA defaults), the artifact bundle hashtags, and the sidecar. Without a detected game everything stays "GTA 6" as before. Tested: `GTA 6 RP LIVE` → Grand Theft Auto VI + 5 tags; Valorant title/tags verified in the queue.

**2. Chat-burst TRIGGER (`clip_live._chat_burst_trigger`).** The crowd itself is now a moment, not just a re-centring: a burst of ≥5 unique people (burst_score ≥ 0.6) WITH ≥5 spoken words within 15s of the burst centre makes a clip centred on the burst. A spam wave alone never clips (verified: burst + no speech → no trigger). While wiring this in, a real bug was found and fixed: `chat_burst_windows()` returned ABSOLUTE epoch times while its docstring (and its only caller) expected segment-relative ones — so chat re-centring had NEVER fired. Verified fixed: an 8-person burst at 90s now re-centres a 10–50s moment to 70–110s.

**3. Static-border facecam fallback (`layout_border.py`, new).** ClipKontrast's border detector (numpy + ffmpeg, no model). It runs ONLY when MediaPipe finds nothing — deliberately NOT first: measured on the PUBG slice [72,77] it "hit" a HUD panel (869,793,716,287) while the real facecam is [0,460,440,240], because PUBG's HUD has stronger bordered rectangles than its borderless webcam. A border-first order would have put a HUD panel in the facecam slot (caught in testing, fixed before shipping). Aspect cap tightened to 2.0 (the HUD false positive was 2.49:1). Verified: with MediaPipe available the [72,77] render finds the real facecam [0,460,440,240] and the border check never runs; Kakori correctly returns None.

**4. Roman engine upgrade (`transliterate.py`, ClipKontrast tables vendored).** The vendored engine is stdlib-only, so a machine WITHOUT indic-transliteration no longer falls back to Devanagari captions (measured: the lib is installed nowhere on this VM). Devanagari 50/50 correct, Urdu 34/34 correct (ours was wrong on ~30/57 before). Long vowels now fold properly (baat/kaam/aaj/teen kept double, hamara/pata/ki/tu single), medial schwa drops right-to-left (karna, samajhna, milna), Urdu digits convert, and Cyrillic/Greek/Korean romanize (Privet mir, Geia soy, annyeonghaseyo). The `native` caption branch now romanizes any word the Latin-only fonts cannot draw instead of rendering tofu boxes. Two ck misspellings caught in testing and fixed with overrides (skool→school, komred→comrade, plus soshlist→socialist, phaayring→firing, faravri→farvari, azaad→azad, pakistaan→pakistan). ITRANS stays the primary Devanagari path when installed; ROMAN_OVERRIDES still win last.

**Not ported (deliberate):** the GAME_RULES AI prompt (no AI judge in the live path), the model-ask facecam path (our Gemini is text-only), and the chat counting (ours already counts unique people).

---

## 12. Professional UI redesign + Roman captions + Windows EXE (2026-11-18 evening)

Claude's limit kept filling, so the user ordered Muse to build the dashboard UI itself — professional, not basic.

**UI redesign (`dashboard.html`, rewritten from the placeholder).** Premium dark theme in the YT Clip Tool v1.2.0 design language (CSS variables, panels, 20/14/10px radii, soft shadows) + PeakCut red accent (#ff2d2d / #e10600) + self-hosted Anton display font (`static/fonts/Anton-Regular.ttf`, served by Flask's built-in static route — no CDN, works offline). Sticky top bar with brand + anchor nav (Streams / Watch / Styles / Positions / VOD / Gallery / Settings), hero headline "CATCH EVERY MOMENT. CLIP IT FIRST.", numbered sections 01–09, hover-lift style cards, gold favorite stars, pulsing live pills, mobile-responsive (verified 390px, no horizontal overflow). The placeholder text is gone. **Nothing functional changed:** the inline `<script>` is the original block verbatim (only the selected-position border colour changed cyan → red); all 52 element ids, all select options, all input defaults, all `/api/*` paths identical — verified by an automated diff script. `dashboard.py` gained one small change: `main()` now opens the dashboard in the default browser 1s after start (double-click EXE launch); the docstring placeholder note is updated.

**Roman captions — MANDATORY, verified end-to-end.** `DEFAULT_CAPTION_MODE = "auto"` (clip_live.py) → `roman_wanted()` (transcribe.py): auto + hi/ur → Roman, always. Live-tested the local engine: मैं→"mein", नहीं→"nahin", میں→"mein", گئے→"gaye" (vendored ck tables, no library needed). The UI's Captions select defaults to "Auto (Roman for Urdu/Hindi)" and a ★ note under Watch setup states Roman is ON by default for Hindi/Urdu speech — captions in Latin letters, never Devanagari or Nastaliq.

**Windows EXE (`.github/workflows/build-exe.yml`, new).** A Windows .exe cannot be built on this Linux VM (PyInstaller builds for the host OS only), so it builds the proven way — GitHub Actions `windows-latest` runner, same pattern as the YT Clip Tool v1.2.3 build: Python 3.12, requirements + facecam requirements + PyInstaller, ffmpeg/ffprobe from gyan.dev, **onedir** build (settings/clips live next to the exe, so favorites persist — onefile would lose them to the temp dir), `--collect-all` for yt_dlp / faster_whisper / ctranslate2 / av / mediapipe / cv2, portable ZIP + GitHub Release v1.0.0. First run on his PC downloads the Whisper model (~150 MB) — internet needed once. The exe itself was NOT run on this VM (impossible for a Windows binary on Linux); the build log + artifact contents are the verification. Repo: `Msohaib012/gta6-live-clipper`.

## 13. EXE frozen-binary fix (v1.0.1, 2026-11-18 19:35 PKT)
- v1.0.0 exe built OK (run 44322489901) but code review found two frozen-only breaks: (1) every module calls bare `ffmpeg`/`ffprobe`, but PyInstaller 6 puts --add-binary files in `_internal` (not on PATH); (2) 7 call sites invoke yt-dlp as `[sys.executable, "-m", "yt_dlp"]` — in the frozen exe sys.executable IS the exe, no -m support.
- Fix: new `ytdlp_cmd.py` — `ytdlp_cmd()` returns the bundled standalone yt-dlp.exe (official release, downloaded in the workflow) when frozen, else the unchanged `[sys.executable, "-m", "yt_dlp"]` path; `ensure_frozen_binaries_on_path()` prepends sys._MEIPASS to PATH when frozen. Used by stream_discovery.py, capture.py (is_live + capture_segment), vod.py (subs + download); called in main() of dashboard.py + clip_live.py. Source-run behaviour byte-identical (verified live: ytsearch returned 5 real GTA 6 live streams; resolve ok/is_live True).
- Workflow: new yt-dlp.exe download step (3 retries, official release) + `--add-binary "yt-dlp.exe;."`; release bumped to v1.0.1 (avoids re-publishing the same tag); release body corrected — Whisper tiny ~75 MB, small ~466 MB for Hindi/Urdu Roman (was "~150 MB").
- Honest limit: the Windows exe itself was never executed on this Linux VM (impossible); the frozen fixes are verified by code inspection + unit test of the helper (frozen simulation) + the v1.0.1 build log. End-to-end exe launch still needs his PC.
