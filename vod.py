"""VOD mode — make vertical Shorts from ALREADY-UPLOADED videos.

This is NOT a clip downloader (YT Clip Tool is a separate tool and stays
separate — see the boundary in BUILD-REPORT). VOD mode only makes
vertical Shorts: paste a video URL (YouTube etc.) or a local file ->
transcript -> pick the best moments -> render vertical Shorts with
captions -> gallery. The downloaded source file is DELETED after the
Shorts are rendered (it is working material, not a download).

Moment picking — 8-signal virality (FuseClip / OpenShorts method):
hooks, emotional peaks, opinion bombs, revelations, conflict, quotable
lines, story peaks, practical value.
- WITHOUT an AI key (default): the signals are approximated from what
  the machine can measure — loudness spikes, speech density, hype
  keywords ("go", "insane", "no way", "wow", "oh my god", ...),
  questions/exclamations (hooks), short punchy segments (quotable).
- WITH a free Gemini key (Settings, never hard-coded): two-pass picking
  (OpenShorts style) — 90s windows scored by Gemini, then the shortlist
  snapped to word boundaries. AI picks; the renderer is the same.
"""
import datetime
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from transcribe import pick_moment, transcribe_segment  # noqa: E402
from render_short import render_short  # noqa: E402
from settings import next_favorite_style  # noqa: E402

HYPE_WORDS = {
    "go", "go go", "lets go", "let's go", "insane", "no way", "wow",
    "oh my god", "omg", "crazy", "unbelievable", "look", "watch",
    "what", "wait", "bro", "damn", "holy", "yes", "come on",
    "chalo", "dekho", "kya", "yaar", "bhai", "are", "wah",
}
CHUNK_S = 600  # transcribe long videos in 10-min chunks


def gemini_generate(prompt, api_key, model="gemini-2.0-flash", timeout=60):
    """One generic Gemini text call (same plain-urllib pattern as
    transliterate.gemini_transliterate — no SDK). Returns text or None."""
    import urllib.request
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:"
           "generateContent?key=%s" % (model, api_key))
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        return d["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[vod:gemini] failed ({e})")
        return None


def fetch_autosubs(url, work_dir, lang_hint=None):
    """Fetch YouTube auto-captions for a URL with yt-dlp (proven 2026-11-16
    on the Kakori video: Hindi auto-subs in ~8s, 1,464 segments).
    Returns (cues, lang) where cues = [{start, end, text}] (rolling-window
    duplicates merged), or (None, None) when no subtitles exist.
    Auto-subs are usually far cleaner than local Whisper for Hindi/Urdu,
    so VOD prefers them when available. Local files have no subs."""
    import glob
    import re as _re
    langs = []
    if (lang_hint or "").lower() in ("hi", "ur", "roman-urdu", "hi-roman", "ur-roman"):
        langs = ["hi.*", "ur.*", "en.*"]
    elif (lang_hint or "").lower() == "en":
        langs = ["en.*", "hi.*"]
    else:
        langs = ["hi.*", "ur.*", "en.*"]
    out_tmpl = os.path.join(work_dir, "subs.%(ext)s")
    cmd = [sys.executable, "-m", "yt_dlp", "--no-check-certificates",
           "--write-auto-subs", "--write-subs",
           "--sub-lang", ",".join(langs), "--sub-format", "vtt",
           "--skip-download", "-o", out_tmpl, url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(f"[vod:subs] yt-dlp subs failed: {r.stderr[-300:]}", flush=True)
        return None, None
    # prefer hi, then ur, then en
    for want in ("hi", "ur", "en"):
        for f in sorted(glob.glob(os.path.join(work_dir, "subs.*.vtt"))):
            if f".{want}" in f or f.endswith(f".{want}.vtt"):
                cues = _parse_vtt(f)
                if cues:
                    print(f"[vod:subs] got {len(cues)} cues ({want}) from {f}",
                          flush=True)
                    return cues, want
    print("[vod:subs] no usable subtitles found", flush=True)
    return None, None


def _parse_vtt(path):
    """Parse a VTT file into [{start, end, text}] with rolling-window
    duplicates merged (YouTube auto-subs repeat each line across cues)."""
    import re as _re
    ts_re = _re.compile(r"(\d+):(\d+):(\d+)\.(\d+)\s*-->\s*(\d+):(\d+):(\d+)\.(\d+)")

    def _ts(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
    cues, cur = [], None
    for line in open(path, encoding="utf-8", errors="ignore"):
        line = line.strip()
        m = ts_re.match(line)
        if m:
            if cur:
                cues.append(cur)
            cur = {"start": _ts(*m.groups()[:4]), "end": _ts(*m.groups()[4:]),
                   "text": ""}
        elif cur is not None and line and not line.startswith(("WEBVTT", "NOTE")):
            txt = _re.sub(r"<[^>]+>", "", line).strip()
            if txt:
                cur["text"] = (cur["text"] + " " + txt).strip() if cur["text"] else txt
    if cur:
        cues.append(cur)
    # merge: drop a cue whose text is fully contained in the previous cue's text
    merged = []
    for c in cues:
        if not c["text"]:
            continue
        if merged and c["text"] in merged[-1]["text"]:
            merged[-1]["end"] = max(merged[-1]["end"], c["end"])
            continue
        if merged and merged[-1]["text"] in c["text"]:
            merged[-1] = c
            continue
        merged.append(c)
    return merged


def autosubs_to_transcript(cues, duration, caption_mode="auto",
                           roman_mode="local", detected_lang="hi"):
    """Auto-sub cues -> transcript JSON dict (same shape as Whisper output).
    Words are split evenly across each cue's span (auto-subs have no
    word timestamps); the caption chunker only needs word order + rough
    timing, which this gives."""
    from transliterate import apply_caption_script
    words, segs = [], []
    for c in cues:
        toks = c["text"].split()
        if not toks:
            continue
        span = max(0.01, c["end"] - c["start"])
        per = span / len(toks)
        segs.append({"start": round(c["start"], 2), "end": round(c["end"], 2),
                     "text": c["text"]})
        for i, tok in enumerate(toks):
            ws = c["start"] + i * per
            words.append({"start": round(ws, 2), "end": round(ws + per, 2),
                          "word": tok})
    words, cap_script, method = apply_caption_script(
        words, detected_lang, caption_mode, roman_mode, None)
    return {"segments": segs, "words": words, "duration": duration,
            "detected_language": detected_lang, "language_probability": 1.0,
            "caption_script": cap_script, "translit_method": method,
            "transcription_model": "youtube-autosubs", "language_forced": None,
            "script_guard_retried": False}


def download_video(url, work_dir, timeout=1800):
    """Download an uploaded video for Shorts-making (best 1080p, else best).
    Returns the local file path. The caller DELETES it after rendering."""
    out_tmpl = os.path.join(work_dir, "vod_source.%(ext)s")
    cmd = [sys.executable, "-m", "yt_dlp", "--no-check-certificates",
           "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
           "-o", out_tmpl, "--no-playlist", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"download failed: {r.stderr[-500:]}")
    for f in os.listdir(work_dir):
        if f.startswith("vod_source."):
            return os.path.join(work_dir, f)
    raise RuntimeError("download produced no file")


def _write_energy(path, out_path, win=5.0):
    """Audio-energy profile (5s windows, same shape as transcribe.py)."""
    import wave
    import numpy as np
    wav = os.path.join(os.path.dirname(out_path), "energy_tmp.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path,
                    "-vn", "-ac", "1", "-ar", "16000", wav], check=True)
    w = wave.open(wav, "rb")
    n = w.getnframes()
    pcm = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    dur = n / 16000
    prof = []
    nwin = int(dur // win)
    for i in range(nwin):
        s0 = int(i * win * 16000)
        s1 = int(min((i + 1) * win, dur) * 16000)
        c = pcm[s0:s1]
        rms = float(np.sqrt(np.mean(c ** 2))) if len(c) else 0.0
        db = 20 * float(np.log10(rms + 1e-9))
        prof.append({"start": round(i * win, 1),
                     "end": round(min((i + 1) * win, dur), 1), "db": round(db, 1)})
    prof.sort(key=lambda p: -p["db"])
    json.dump(prof, open(out_path, "w"), indent=1)
    try:
        os.remove(wav)
    except OSError:
        pass
    return out_path


def transcribe_full(path, work_dir, language="auto", caption_mode="auto",
                    roman_mode="local"):
    """Transcribe a whole (possibly long) video: 10-min chunks, merged
    into one transcript JSON with word timestamps on the full timeline.
    Roman path (2026-11-18 fix): Hindi/Urdu + Roman captions uses the
    SMALL model with the language forced (tiny emits garbled Roman
    phonetics); the language is detected on the first chunk first."""
    import wave
    import numpy as np
    from faster_whisper import WhisperModel
    from transcribe import detect_language_fast, roman_wanted, ROMAN_MODEL

    wav_path = os.path.join(work_dir, "vod_audio.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path,
                    "-vn", "-ac", "1", "-ar", "16000", wav_path], check=True)
    w = wave.open(wav_path, "rb")
    n = w.getnframes()
    pcm = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    dur = n / 16000
    print(f"[vod] audio {dur:.0f}s, transcribing in {CHUNK_S}s chunks", flush=True)

    from transliterate import apply_caption_script, load_gemini_key
    gemini_key = load_gemini_key() if roman_mode == "gemini" else None
    # detect the language on the first 30s, then pick the model once
    lang_req = (language or "auto").lower()
    if lang_req == "auto":
        det0, _p0 = detect_language_fast(pcm)
        eff_lang = det0
    else:
        eff_lang = lang_req
    use_model = ROMAN_MODEL if roman_wanted(eff_lang, caption_mode) else "tiny"
    if use_model == ROMAN_MODEL:
        lang_arg = eff_lang  # Roman path: always force the language
    elif lang_req != "auto":
        lang_arg = lang_req
    else:
        lang_arg = None
    print(f"[vod] model={use_model} language={lang_arg or 'auto'}", flush=True)
    model = WhisperModel(use_model, device="cpu", compute_type="int8")
    all_words, all_segs, detected = [], [], None
    t = 0.0
    while t < dur:
        chunk = pcm[int(t * 16000):int(min(t + CHUNK_S, dur) * 16000)]
        segs, info = model.transcribe(chunk, language=lang_arg,
                                      word_timestamps=True)
        if detected is None:
            detected = info.language
        for s in segs:
            all_segs.append({"start": round(s.start + t, 2),
                             "end": round(s.end + t, 2),
                             "text": s.text.strip()})
            for wd in (s.words or []):
                all_words.append({"start": round(wd.start + t, 2),
                                  "end": round(wd.end + t, 2),
                                  "word": wd.word})
        t += CHUNK_S
        print(f"[vod] transcribed to {min(t, dur):.0f}s / {dur:.0f}s", flush=True)
    words, cap_script, method = apply_caption_script(
        all_words, detected or "en", caption_mode, roman_mode, gemini_key)
    tp = os.path.join(work_dir, "vod_transcript.json")
    json.dump({"segments": all_segs, "words": words, "duration": dur,
               "detected_language": detected, "caption_script": cap_script,
               "translit_method": method,
               "transcription_model": use_model},
              open(tp, "w"), ensure_ascii=False, indent=1)
    # energy profile (5s windows, same shape as transcribe.py)
    ep = os.path.join(work_dir, "vod_energy.json")
    _write_energy(path, ep)
    print(f"[vod] {len(all_segs)} segments, {len(all_words)} words, "
          f"lang={detected}, script={cap_script}", flush=True)
    return tp, ep


def _score_windows(tp, ep, clip_len=40.0, pad=8.0):
    """No-AI 8-signal approximation. Scores every 5s energy window as a
    clip centre and returns ranked candidates:
    [{start, end, score, signals:[...], loud_db, words}].
    Signals measured: loudness, speech density, hype keywords, hook
    (question/exclamation in the first 3s), quotable (short punchy
    segment), story peak (loud + dense + keyword together)."""
    tr = json.load(open(tp))
    prof = json.load(open(ep))
    dur = tr["duration"]
    words = tr["words"]

    def words_in(a, b):
        return [w for w in words if w["start"] >= a and w["end"] <= b]

    def text_in(a, b):
        return " ".join(w.get("display") or w["word"]
                        for w in words_in(a, b)).lower()

    cands = []
    for p in prof:
        start = max(0.0, p["start"] - pad)
        end = min(dur, start + clip_len)
        start = max(0.0, end - clip_len)
        ws = words_in(start, end)
        nw = len(ws)
        if nw < 5:
            continue
        txt = text_in(start, end)
        signals = []
        score = p["db"]  # loudness base (dBFS, negative)
        # "clip that": the streamer marked this moment with their voice
        # (ClipzLab idea) — the strongest single signal we have.
        if ("clip that" in txt or "clip it" in txt
                or "clip this" in txt):
            signals.append("clip-that")
            score += 15
        if nw >= 20:
            signals.append("dense-speech")
            score += 6
        hits = [k for k in HYPE_WORDS if k in txt]
        if hits:
            signals.append("hype:" + ",".join(sorted(hits)[:3]))
            score += 4 * min(len(hits), 3)
        hook = text_in(start, start + 3)
        if "?" in hook or "!" in hook:
            signals.append("hook")
            score += 5
        # quotable: a short punchy segment (<= 8 words, ends with ! or ?)
        for s in tr["segments"]:
            if start <= s["start"] and s["end"] <= end:
                stxt = s["text"].strip()
                if 3 <= len(stxt.split()) <= 8 and stxt[-1:] in "!?":
                    signals.append("quotable")
                    score += 4
                    break
        if len(signals) >= 3:
            signals.append("story-peak")
            score += 5
        cands.append({"start": round(start, 2), "end": round(end, 2),
                      "score": round(score, 1), "signals": signals,
                      "loud_db": p["db"], "words": nw})
    cands.sort(key=lambda c: -c["score"])
    return cands


def pick_moments_ai(tp, ep, api_key, clip_len=40.0, pad=8.0, top_n=3):
    """Two-pass picking (OpenShorts style) with a free Gemini key.
    Pass 1: the transcript is chunked into 90s windows; Gemini scores
    each for virality. Pass 2: the top windows are snapped to word
    boundaries (via _score_windows' loudest window inside them).
    Returns the same candidate shape as _score_windows, or None on
    any failure (caller falls back to the no-AI scorer)."""
    tr = json.load(open(tp))
    dur = tr["duration"]
    segs = tr["segments"]
    # pass 1: 90s windows with text
    wins = []
    t = 0.0
    while t < dur:
        txt = " ".join(s["text"] for s in segs
                       if s["start"] >= t and s["start"] < t + 90)
        if txt.strip():
            wins.append((t, min(t + 90, dur), txt))
        t += 90
    if not wins:
        return None
    listing = "\n".join(f"[{i}] {a:.0f}-{b:.0f}s: {txt[:400]}"
                        for i, (a, b, txt) in enumerate(wins))
    prompt = ("You are picking viral Shorts moments from a video transcript. "
              "Score each 90-second window 0-100 for virality (hooks, emotional "
              "peaks, opinion bombs, revelations, conflict, quotable lines, "
              "story peaks, practical value). Return ONLY a JSON list like "
              '[{"window": 2, "score": 92, "reason": "short reason"}] — the '
              "top 5 windows, best first, no other text.\n\n" + listing)
    out = gemini_generate(prompt, api_key)
    if not out:
        return None
    try:
        import re
        m = re.search(r"\[.*\]", out, re.S)
        picks = json.loads(m.group(0)) if m else json.loads(out)
    except Exception:
        return None
    # pass 2: snap each picked window to the best no-AI candidate inside it
    base = _score_windows(tp, ep, clip_len, pad)
    results = []
    for p in picks[:top_n]:
        wi = p.get("window")
        if wi is None or wi >= len(wins):
            continue
        a, b, _ = wins[wi]
        inside = [c for c in base if a <= c["start"] < b]
        if inside:
            c = inside[0]
            c = dict(c)
            c["signals"] = c["signals"] + ["ai:" + str(p.get("reason", ""))[:40]]
            c["score"] = round(c["score"] + 10, 1)
            results.append(c)
    return results or None


def pick_moments(tp, ep, clip_len=40.0, pad=8.0, count=3, api_key=None):
    """Top `count` non-overlapping moments. AI (Gemini) when a key is
    given, else the no-AI scorer. Always returns at least the single
    best moment (pick_moment fallback)."""
    cands = None
    if api_key:
        cands = pick_moments_ai(tp, ep, api_key, clip_len, pad, top_n=count)
        if cands:
            print(f"[vod] AI picking: {len(cands)} moment(s)", flush=True)
    if not cands:
        cands = _score_windows(tp, ep, clip_len, pad)
    picked = []
    for c in cands:
        if all(c["end"] <= p["start"] or c["start"] >= p["end"] for p in picked):
            picked.append(c)
        if len(picked) >= count:
            break
    if not picked:  # last resort: loudest window, whatever it holds
        s, e, why = pick_moment(tp, ep, clip_len, pad)
        picked = [{"start": s, "end": e, "score": why["score"],
                   "signals": ["loudest"], "loud_db": (why["loud_window"] or {}).get("db"),
                   "words": why["words"]}]
    return picked


def process_vod(source, out_dir, style=None, mode="game", res=720,
                clip_len=40.0, count=3, manual=None,
                language="auto", caption_mode="auto", roman_mode="local",
                api_key=None, keep_source=False,
                ratio="9:16", facecam_pos="auto", caption_pos="auto"):
    """One VOD job. source = URL or local file path.
    manual = (start, end) to render exactly that window instead of picking.
    Returns list of clip paths made. Writes <clip>.info.json per clip."""
    os.makedirs(out_dir, exist_ok=True)
    work_dir = os.path.join(out_dir, "work_vod_" +
                            datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(work_dir, exist_ok=True)
    downloaded = None
    try:
        if os.path.isfile(source):
            src = source
            print(f"[vod] local file: {src}", flush=True)
        else:
            print(f"[vod] downloading: {source}", flush=True)
            src = download_video(source, work_dir)
            downloaded = src
        # YouTube auto-subs first for URL sources (cleaner than local
        # Whisper for Hindi/Urdu — proven on the Kakori video). Falls
        # back to Whisper when no subs exist.
        tp = ep = None
        if not os.path.isfile(source):
            cues, sub_lang = fetch_autosubs(source, work_dir, language)
            if cues:
                dur = float(subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "format=duration", "-of", "csv=p=0", src],
                    capture_output=True, text=True).stdout.strip() or 0)
                tr = autosubs_to_transcript(
                    cues, dur, caption_mode, roman_mode,
                    detected_lang=sub_lang or "hi")
                tp = os.path.join(work_dir, "vod_transcript.json")
                json.dump(tr, open(tp, "w"), ensure_ascii=False, indent=1)
                # energy profile (same shape as transcribe.py)
                ep = os.path.join(work_dir, "vod_energy.json")
                _write_energy(src, ep)
                print(f"[vod] transcript from YouTube auto-subs "
                      f"({len(tr['words'])} words, lang={sub_lang})", flush=True)
        if tp is None:
            tp, ep = transcribe_full(src, work_dir, language, caption_mode, roman_mode)
        if manual:
            moments = [{"start": float(manual[0]), "end": float(manual[1]),
                        "score": 0, "signals": ["manual"],
                        "loud_db": None, "words": 0}]
        else:
            moments = pick_moments(tp, ep, clip_len, 8.0, count, api_key)
        clips = []
        for i, m in enumerate(moments):
            st = style
            if st is None:
                st, pos, total = next_favorite_style()
                print(f"[vod] clip {i + 1}: favorite style {pos}/{total} = {st}", flush=True)
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            clip_path = os.path.join(out_dir, f"vod_short_{ts}_{i + 1}.mp4")
            wdir = os.path.join(work_dir, f"render_{i + 1}")
            render_short(src, m["start"], m["end"], tp, st, mode, clip_path,
                         wdir, res=res, ratio=ratio,
                         facecam_pos=facecam_pos, caption_pos=caption_pos)
            info = {"source": source, "kind": "vod",
                    "moment": {"start": m["start"], "end": m["end"]},
                    "style": st, "mode": mode, "res": res,
                    "ratio": ratio, "facecam_pos": facecam_pos,
                    "caption_pos": caption_pos,
                    "signals": m.get("signals", []), "score": m.get("score"),
                    "language": json.load(open(tp)).get("detected_language"),
                    "review": None}
            json.dump(info, open(clip_path + ".info.json", "w"), indent=1)
            # per-clip artifact bundle: transcript + script + analysis
            try:
                from clip_live import write_artifact_bundle
                write_artifact_bundle(clip_path, info, tp)
            except Exception as e:
                print(f"[vod] artifact bundle failed ({e}) — clip still saved",
                      flush=True)
            clips.append(clip_path)
            print(f"[vod] SAVED {clip_path}", flush=True)
        return clips
    finally:
        if downloaded and not keep_source:
            try:
                os.remove(downloaded)
                print("[vod] source file deleted (working material only)", flush=True)
            except OSError:
                pass


def main():
    import argparse
    ap = argparse.ArgumentParser(description="VOD mode: Shorts from an uploaded video or local file.")
    ap.add_argument("source", help="video URL or local file path")
    ap.add_argument("--out", default="clips")
    ap.add_argument("--style", default=None, help="caption style (default: favorites rotation)")
    ap.add_argument("--mode", default="game", choices=["game", "gameplay", "facecam", "doc"])
    ap.add_argument("--res", type=int, default=720, choices=[720, 1080])
    ap.add_argument("--ratio", default="9:16",
                    choices=["9:16", "1:1", "4:5", "16:9"])
    ap.add_argument("--facecam-pos", default="auto",
                    choices=["auto", "top-right", "top-left", "mid-right",
                             "mid-left", "top-band", "off"])
    ap.add_argument("--caption-pos", default="auto",
                    choices=["auto", "bottom", "seam", "top"])
    ap.add_argument("--count", type=int, default=3, help="how many moments to clip")
    ap.add_argument("--clip-len", type=float, default=40.0)
    ap.add_argument("--manual", default=None, help="exact window: START-END seconds, e.g. 90-130")
    ap.add_argument("--lang", default="auto")
    ap.add_argument("--caption-mode", default="auto")
    a = ap.parse_args()
    manual = None
    if a.manual:
        s, e = a.manual.split("-")
        manual = (float(s), float(e))
    from settings import get_gemini_key
    clips = process_vod(a.source, a.out, a.style, a.mode, a.res,
                        a.clip_len, a.count, manual, a.lang,
                        a.caption_mode, api_key=get_gemini_key(),
                        ratio=a.ratio, facecam_pos=a.facecam_pos,
                        caption_pos=a.caption_pos)
    print("clips:", clips)


if __name__ == "__main__":
    main()
