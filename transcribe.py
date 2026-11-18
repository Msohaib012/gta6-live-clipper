"""Transcribe a captured stream segment + audio-energy profile (moment picking).

Proven on this VM (spikes 2026-11-17):
- faster-whisper tiny, CPU int8: ~180s audio in ~11-15s.
- Audio is decoded via the `wave` module (PyAV version mismatch bypass —
  faster-whisper's decode path broke on this VM's PyAV; feeding raw PCM
  floats works around it). Keep this: do NOT switch to model.transcribe(path).
- Energy: RMS per 5s window (dBFS) -> loudest windows = hype moments.

Multilingual (2026-11-17): language AUTO-DETECT by default (Whisper
reports info.language + probability — stored in the transcript JSON).
Hindi/Urdu speech gets ROMAN captions by default (sohaib's order — the
streamer speaks Roman Urdu/Hindi, PeakCut only ever did English):
Whisper writes Hindi as Devanagari / Urdu as Nastaliq, and
transliterate.py converts the words to Roman (local lib first; Gemini
with the user's key from config.json when roman_mode="gemini").
Other languages caption in their own script. See transliterate.py.

Roman fix (2026-11-18, measured — see roman-fix/ROMAN-FIX-REPORT.md):
tiny is UNRELIABLE for Hindi: with auto-detect it outputs garbled ROMAN
phonetics directly ("embeho me kane bhai", "kees keel" — match 0.26 vs
a clean reference), forcing language="hi" changes NOTHING (identical
garbage), and a Devanagari initial_prompt makes it WORSE (mixed scripts,
Chinese/Korean chars, match 0.07). So the Roman path uses the SMALL
model with the language FORCED and NO prompt: small+hi gives 100%
Devanagari words (match 0.60). A script-guard catches the garbage mode:
if Roman was wanted but >30% of raw words came out Latin, the run is
retried once (deterministic temperature=0) and loudly logged.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import wave

import numpy as np

from transliterate import apply_caption_script, load_gemini_key, script_of  # noqa: E402

MODEL = "tiny"
ROMAN_MODEL = "small"  # hi/ur Roman path — tiny emits garbled Roman (measured)
LANG = "auto"  # auto-detect (Whisper); 'en' | 'hi' | 'ur' to force
CAPTION_MODE = "auto"  # auto | en | hi | hi-roman | ur | ur-roman (roman-urdu)
ROMAN_MODE = "local"   # local | gemini (gemini needs the user's key in config.json)
WIN = 5.0  # energy window seconds
ROMAN_LATIN_LIMIT = 0.30  # script-guard: >30% Latin words when Roman was wanted = garbage mode


def roman_wanted(language, caption_mode):
    """True when the captions for this language/mode must come out Roman."""
    mode = (caption_mode or "auto").lower().replace("_", "-")
    if mode in ("hi-roman", "ur-roman", "roman-urdu"):
        return True
    if mode == "auto" and (language or "").lower() in ("hi", "ur"):
        return True
    return False


def choose_model(language, caption_mode):
    """(model_name, forced_language) for the transcribe call.
    Roman path (hi/ur): small + forced language, no prompt. Else tiny."""
    lang = (language or "auto").lower()
    if lang in ("hi", "ur") and roman_wanted(lang, caption_mode):
        return ROMAN_MODEL, lang
    if lang in ("hi", "ur"):
        return MODEL, lang  # native-script hi/ur: tiny is fine, language forced
    return MODEL, None  # auto / en: tiny, auto-detect


def detect_language_fast(pcm, sample_rate=16000, probe_seconds=30):
    """Detect the spoken language on the first `probe_seconds` of audio
    with tiny (fast, ~2-4s). Returns (lang, prob)."""
    from faster_whisper import WhisperModel
    probe = pcm[:int(probe_seconds * sample_rate)]
    model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    lang, prob, _all = model.detect_language(probe)
    return lang, float(prob)


def _latin_fraction(words):
    if not words:
        return 0.0
    n_lat = sum(1 for w in words if script_of(w["word"]) == "latin")
    return n_lat / len(words)


def _transcribe_once(model, pcm, lang_arg, temperature=None):
    """One faster-whisper pass. Returns (segments, info, words, segs)."""
    kwargs = {"language": lang_arg, "word_timestamps": True}
    if temperature is not None:
        kwargs["temperature"] = temperature
    segments, info = model.transcribe(pcm, **kwargs)
    words, segs = [], []
    for s in segments:
        segs.append({"start": round(s.start, 2), "end": round(s.end, 2),
                     "text": s.text.strip()})
        for wd in (s.words or []):
            words.append({"start": round(wd.start, 2), "end": round(wd.end, 2),
                          "word": wd.word})
    return segments, info, words, segs


def transcribe_segment(capture_path, work_dir, model_name=MODEL, language=LANG,
                       caption_mode=CAPTION_MODE, roman_mode=ROMAN_MODE,
                       gemini_key=None):
    """Transcribe + energy-profile one captured file. Returns (transcript, energy) paths.

    Roman path (2026-11-18 fix): when the speech is Hindi/Urdu and Roman
    captions are wanted, the SMALL model is used with the language forced
    (tiny emits garbled Roman phonetics; forcing language + NO prompt is
    what makes small output clean Devanagari/Nastaliq). For 'auto' the
    language is detected on the first 30s first, so the right model is
    chosen BEFORE the full transcribe — no double transcription.
    A script-guard retries once (deterministic) if Roman was wanted but
    Whisper still emitted Latin directly.
    """
    os.makedirs(work_dir, exist_ok=True)
    wav_path = os.path.join(work_dir, "audio.wav")
    transcript_path = os.path.join(work_dir, "transcript.json")
    energy_path = os.path.join(work_dir, "energy.json")
    t0 = time.time()

    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", capture_path,
                    "-vn", "-ac", "1", "-ar", "16000", wav_path], check=True)
    print(f"[transcribe] wav extracted in {time.time() - t0:.1f}s", flush=True)

    from faster_whisper import WhisperModel
    w = wave.open(wav_path, "rb")
    n = w.getnframes()
    pcm = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    dur = n / 16000
    print(f"[transcribe] audio {dur:.1f}s loaded", flush=True)

    # --- decide model + forced language BEFORE the full transcribe ---
    lang_req = (language or "auto").lower()
    if lang_req == "auto":
        det_lang, det_prob = detect_language_fast(pcm)
        print(f"[transcribe] fast detect: {det_lang} prob {det_prob:.2f}", flush=True)
        eff_lang = det_lang
    else:
        eff_lang = lang_req
    want_roman = roman_wanted(eff_lang, caption_mode)
    if want_roman and model_name == MODEL:
        # Roman path always upgrades to the small model (tiny garbles Hindi)
        use_model, lang_arg = ROMAN_MODEL, eff_lang
    elif lang_req != "auto":
        use_model, lang_arg = model_name, lang_req
    else:
        use_model, lang_arg = model_name, None
    print(f"[transcribe] model={use_model} language={lang_arg or 'auto'} "
          f"roman={want_roman}", flush=True)

    model = WhisperModel(use_model, device="cpu", compute_type="int8")
    print(f"[transcribe] model loaded in {time.time() - t0:.1f}s", flush=True)
    _segs, info, words, segs = _transcribe_once(model, pcm, lang_arg)
    detected = info.language
    det_prob = round(float(info.language_probability), 2)
    print(f"[transcribe] detected language: {detected} prob {det_prob:.2f}", flush=True)

    # --- script-guard: Roman wanted but Whisper wrote Latin directly ---
    guard_retried = False
    if want_roman and _latin_fraction(words) > ROMAN_LATIN_LIMIT:
        print(f"[transcribe] SCRIPT GUARD: {100 * _latin_fraction(words):.0f}% Latin "
              f"words but Roman captions wanted — Whisper emitted Roman "
              f"phonetics directly. Retrying once (temperature=0, "
              f"model={ROMAN_MODEL})", flush=True)
        if use_model != ROMAN_MODEL:
            model = WhisperModel(ROMAN_MODEL, device="cpu", compute_type="int8")
            use_model = ROMAN_MODEL
        _s2, info2, words2, segs2 = _transcribe_once(
            model, pcm, detected if detected in ("hi", "ur") else lang_arg,
            temperature=0.0)
        if _latin_fraction(words2) < _latin_fraction(words):
            words, segs, info = words2, segs2, info2
            detected = info.language
            det_prob = round(float(info.language_probability), 2)
            print("[transcribe] script guard: retry is cleaner, keeping it", flush=True)
        else:
            print("[transcribe] script guard: retry no better, keeping first pass",
                  flush=True)
        guard_retried = True

    # caption script: Roman for Hindi/Urdu by default (sohaib's order),
    # own script otherwise. Adds a 'display' field per word.
    if roman_mode == "gemini" and gemini_key is None:
        gemini_key = load_gemini_key()
        if gemini_key is None:
            print("[transcribe] roman_mode=gemini but no key in config.json — "
                  "using local transliteration", flush=True)
            roman_mode = "local"
    words, cap_script, translit_method = apply_caption_script(
        words, detected, caption_mode, roman_mode, gemini_key)
    print(f"[transcribe] caption script: {cap_script} (translit: {translit_method})",
          flush=True)
    json.dump({"segments": segs, "words": words, "duration": dur,
               "detected_language": detected, "language_probability": det_prob,
               "caption_script": cap_script, "translit_method": translit_method,
               "transcription_model": use_model,
               "language_forced": lang_arg,
               "script_guard_retried": guard_retried},
              open(transcript_path, "w"), ensure_ascii=False, indent=1)
    print(f"[transcribe] {len(segs)} segments, {len(words)} words "
          f"in {time.time() - t0:.1f}s total", flush=True)

    # audio energy: RMS per 5s window (dBFS), loudest first
    nwin = int(dur // WIN)
    prof = []
    for i in range(nwin):
        s0 = int(i * WIN * 16000)
        s1 = int(min((i + 1) * WIN, dur) * 16000)
        chunk = pcm[s0:s1]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        db = 20 * np.log10(rms + 1e-9)
        prof.append({"start": round(i * WIN, 1),
                     "end": round(min((i + 1) * WIN, dur), 1), "db": round(db, 1)})
    prof.sort(key=lambda p: -p["db"])
    json.dump(prof, open(energy_path, "w"), indent=1)
    print("[transcribe] top energy windows:",
          [(p["start"], p["db"]) for p in prof[:5]], flush=True)
    return transcript_path, energy_path


def pick_moment(transcript_path, energy_path, clip_len=40.0, pad=8.0,
                min_words=5):
    """Pick the best clip window [start, end] inside one segment.

    Score: loudest 5s energy window, then centre the clip on it with `pad`
    seconds of lead-in (so the hook lands in the first ~3s). Prefers windows
    that actually contain speech (words >= min_words); falls back to the
    loudest window regardless.
    Returns (start, end, reason_dict).
    """
    tr = json.load(open(transcript_path))
    prof = json.load(open(energy_path))
    dur = tr["duration"]
    words = tr["words"]

    def words_in(a, b):
        return [w for w in words if w["start"] >= a and w["end"] <= b]

    best = None
    for p in prof:
        # clip centred so the loud window starts ~pad seconds in
        start = max(0.0, p["start"] - pad)
        end = min(dur, start + clip_len)
        start = max(0.0, end - clip_len)  # keep full length when clamped
        nw = len(words_in(start, end))
        cand = {"start": round(start, 2), "end": round(end, 2),
                "loud_window": p, "words": nw,
                "score": p["db"] + (10 if nw >= min_words else 0)}
        if best is None or cand["score"] > best["score"]:
            best = cand
        if nw >= min_words and p["db"] >= -30:
            break  # good enough: loud + speech — take the first such window
    if best is None:  # empty transcript / no energy at all
        best = {"start": 0.0, "end": min(clip_len, dur), "loud_window": None,
                "words": 0, "score": -999}
    return best["start"], best["end"], best


def main():
    ap = argparse.ArgumentParser(description="Transcribe a capture + energy profile.")
    ap.add_argument("--input", required=True, help="captured video file")
    ap.add_argument("--work-dir", required=True, help="dir for wav/transcript/energy JSON")
    ap.add_argument("--model", default=MODEL,
                    help="faster-whisper model for non-Roman paths (default: tiny). "
                         "The Roman path (hi/ur) always upgrades to 'small' — "
                         "tiny emits garbled Roman phonetics (measured 2026-11-18).")
    ap.add_argument("--lang", default=LANG,
                    help="transcription language: auto (detect, default) | en | hi | ur")
    ap.add_argument("--caption-mode", default=CAPTION_MODE,
                    help="caption script: auto (Roman for Hindi/Urdu, default) | en | hi | hi-roman | ur | ur-roman")
    ap.add_argument("--roman-mode", default=ROMAN_MODE,
                    help="transliteration engine: local (default) | gemini (needs key in config.json)")
    ap.add_argument("--clip-len", type=float, default=40.0)
    ap.add_argument("--pad", type=float, default=8.0)
    a = ap.parse_args()
    tp, ep = transcribe_segment(a.input, a.work_dir, a.model, a.lang,
                                a.caption_mode, a.roman_mode)
    s, e, why = pick_moment(tp, ep, a.clip_len, a.pad)
    print(f"picked moment: {s:.2f}-{e:.2f}s ({why['words']} words, "
          f"loud window {why['loud_window']})")
    print(f"transcript: {tp}\nenergy: {ep}")


if __name__ == "__main__":
    main()
