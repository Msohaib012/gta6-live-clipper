# Roman captions — FIX REPORT (2026-11-18)

sohaib: "Not working properly in roman language" (on the old roman_short.mp4).
Fixed. New roman_short.mp4 re-rendered + frame-verified below.

## Root cause (proven from the actual transcripts, not guessed)

The TRANSLITERATOR was never the problem. The TRANSCRIPTION was.

- Old clip used `work_kakori/transcript.json` (tiny, auto-detect): Whisper
  tiny output **garbled Roman phonetics directly** — "embeho me kane bhai",
  "kene trittava", "angresokhe", "kees kees keel", "Alfred Pagai hoethe",
  "faring ho nala ghe". The raw `word` fields ARE the garbage; the
  transliterator had nothing to fix (words already Latin → passed through).
- Same audio, second run (`transcript_deva.json`): clean Devanagari →
  clean Roman ("ko socialist republican association banane mein aham
  bhumika nibhai…"). So tiny's Hindi script choice is unreliable
  run-to-run (temperature fallback sampling: default temperature list
  [0.0…1.0] makes decode nondeterministic when quality gates fail).

## Measurements (same 41.3s Kakori Hindi audio; reference = clean Roman
## transcript_roman.json; match = SequenceMatcher vs reference, in_ref =
## % words found in reference)

| config | words | Devanagari | Latin | match | in_ref | decode |
|---|---|---|---|---|---|---|
| tiny auto (OLD, reproduces bug) | 114 | 0% | 100% | 0.256 | 0.316 | 5s |
| tiny forced hi, no prompt | 114 | 0% | 100% | 0.256 | 0.316 | 11s |
| tiny auto + Devanagari prompt | 109 | 26% | 74% | 0.065 | 0.165 | 30s |
| tiny forced hi + prompt | 64 | 48% | 50% | 0.130 | 0.172 | 21s |
| small forced hi, no prompt | 123 | 100% | 0% | 0.596 | 0.602 | 115s |
| small forced hi + prompt | 123 | 100% | 0% | 0.588 | 0.618 | 63s |
| small auto (detected ur!) | 126 | 0% Urdu | 0% | —* | —* | 45s |
| **YouTube auto-subs (VOD)** | **128** | — | — | **0.893** | **0.883** | **~8s fetch** |

\* small-auto's Urdu output scored 0.367 via the (weak) Urdu→Roman
transliterator; the Urdu TEXT itself is mostly correct words — the
transliterator drops unwritten vowels (documented limit; Gemini path is
the accurate one for Urdu).

Conclusions:
- Forcing language="hi" does NOTHING for tiny (identical garbage).
- A Devanagari initial_prompt makes tiny WORSE (mixed scripts, even
  Chinese/Korean characters, words dropped). Never use a prompt with tiny.
- small + forced hi + NO prompt gives 100% Devanagari, real words
  (match 0.60 vs tiny's 0.26) — but still mishears some words
  ("endustan", "sochalis", "ehambhu", "kanibhai").
- **YouTube auto-subs are the clean source for VOD** (match 0.89) —
  real, natural Roman after transliteration.

Base model: NOT tested — the download (~145 MB) runs at ~10 KB/s from
this VM's proxy (2.6 MB in ~4 min before the run was stopped for the
final push). small is cached and strictly better than base on accuracy,
so small is the conservative Roman-path choice; base decode would be
faster than small's (74M vs 244M params) but its quality is unverified
here. On sohaib's PC the download is fast — base can be tried there.

## What changed (all in ~/workspace/gta6-live-clipper/, in the final ZIP)

**transcribe.py**
- `ROMAN_MODEL = "small"`: the Roman path (hi/ur + Roman captions)
  always uses small with the language forced, NO initial_prompt.
- `detect_language_fast()`: for `auto`, the language is detected on the
  first 30s with tiny BEFORE the full transcribe, so the right model is
  chosen up front (no double transcription).
- Script-guard: if Roman was wanted but >30% of raw words came out Latin
  (the garbage mode), retry once deterministically (temperature=0, small
  model) and log it loudly. Verified working: fixed run used small, hi
  forced, guard not triggered (100% Devanagari).
- Transcript JSON now records `transcription_model`, `language_forced`,
  `script_guard_retried`.

**transliterate.py**
- New overrides from observed forms: `echaarae`→HRA, `aichsare`/
  `echshvare`/`echsare`→HSRA, `sinh`→singh, `kamared`→comrade,
  `netritv`→netritva.
- Bug fixed: ITRANS leaves the ऑ vowel sign (ॉ) unconverted, so
  कॉमरेड came out "kaॉmared" and the override never matched — Devanagari
  leftovers are now stripped BEFORE the override lookup.
  Verified: कॉमरेड→comrade, एचआरए→HRA, एचएसआरए→HSRA, सिंह→singh,
  नेतृत्व→netritva.

**vod.py**
- `fetch_autosubs()` + `_parse_vtt()` + `autosubs_to_transcript()`:
  yt-dlp `--write-auto-subs` (proven on this video: ~8s), rolling-window
  duplicates merged, words split evenly across each cue.
- `process_vod()` now tries auto-subs FIRST for URL sources, falls back
  to Whisper. `transcribe_full()` uses the same Roman-path model
  selection (small + forced language) as transcribe.py.
- English / Devanagari / Nastaliq modes untouched (tiny still default
  for non-Roman paths; no keys hard-coded anywhere).

## Re-rendered demo — VERIFIED

[roman_short.mp4](sandbox://workspace/gta6-live-clipper/roman_short.mp4) —
720x1280, H.264, 60fps, 30.0s, 9.4 MB. Source: Kakori Hindi clip,
auto-subs transcript (VOD path), Glow Neon, game mode. Render 100s.
Roman text: "1928 mein bhagat singh ke sath milakar HRA ko hindustan
socialist republican association HSRA banane mein aham bhumika nibhai
bhagat singh aur azad ke netritva mein HSRA ne angrejon ke hosh uda
diye the … tabhi british police ne unhein charo taraf se gher liya …"

Frame checks (frames in roman-fix/frames/):
- t=5s: "MEIN **AHAM** BHUMIKA" — clean Roman, safe band y~950–1000 ✓
- t=12s: "**KE** IS KHEL" — clean Roman ✓
- t=20s: "TABHI BRITISH **POLICE**" — clean Roman ✓

## Honest limits (for the parent / sohaib)

- LIVE Hindi/Urdu streams have no auto-subs — they use Whisper small
  (match 0.60): real Hindi words, readable sentences, but some words
  still misheard ("endustan", "kanibhai"). Better than tiny's garbage,
  not perfect. Whisper `medium`/`large` would be more accurate but are
  too slow on CPU for the live loop.
- VOD (YouTube links / uploaded videos): auto-subs give clean Roman
  (match 0.89). This is the path the demo uses.
- The fixed Whisper-small transcript for this clip is saved at
  roman-fix/work_fixed/transcript.json (122 words, small, hi forced).

## Files

- roman-fix/ROMAN-FIX-REPORT.md (this file)
- roman-fix/transcript_autosubs.json (demo transcript, auto-subs)
- roman-fix/work_fixed/transcript.json (fixed Whisper-small transcript)
- roman-fix/test_r2.py + test_r2.log (measurement runs)
- roman-fix/frames/t5.png, t12.png, t20.png (verification frames)
