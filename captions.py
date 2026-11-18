"""Captions for GTA 6 Live Clipper — Hormozi Pop + Glow Neon.

PIL PNG overlays -> single transparent caption-track video -> ONE ffmpeg
overlay. NOT libass ASS (Devanagari-unsafe).

Styles (sohaib's picks — Glow Neon is the default, Hormozi Pop second):
- Glow Neon (default): Montserrat Black, ALL CAPS, active word cyan #00E5FF
  with neon glow (blurred copy behind), 6px outline, inactive words dim white.
- Hormozi Pop: Anton, ALL CAPS, max 3 words/line, 8px black outline,
  active word pops 80% -> 112% -> 100% and turns yellow #FFE600.
- Hype (PeakCut's look, sohaib's benchmark 2026-11-17 — "PeakCut ki hi
  achi lagi"): Anton, ALL CAPS, same pop as Hormozi, active word turns
  RED #FF2626 instead of yellow ("CHOPPER UH" look).

Missing-subtitles fixes (diagnosed on live_short_hq.mp4, 2026-11-17):
1. Words straddling the clip end were dropped -> speech with NO captions.
   Fix: include any word overlapping the window, clamp to window.
2. Zero-duration words produced empty overlays -> never highlighted.
   Fix: cursor normalisation guarantees every word >= 0.08s active time.
3. Inter-word gaps blanked the caption -> flicker.
   Fix: caption state HOLDS until the next word starts; blank only during
   real silence between chunks.

Adapted from the proven spike: ~/workspace/gta6-clip-research/build_captions_v2.py
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys

FPS = 60
W, H = 720, 1280
STRIP_W, STRIP_H = 720, 300
# CAPTION BAND (sohaib's orders, 2026-11-17):
# - 23:11 PKT: captions must NOT sit in the bottom ~300px — TikTok /
#   YouTube Shorts / Instagram Reels cover that with their own UI
#   (username + caption bottom-left, like/comment/share bottom-right).
#   The old position (text y=980-1160) hid behind that UI.
# - 23:31 PKT ("Ye nahi achi lagi"): CAP_Y=700 was WRONG the other way —
#   text at y=800-899 sits in the MIDDLE of the frame, on top of the main
#   action (the police car at t=5s, the running cop at t=36s in the
#   gtav_capture frames). Captions must NOT cover the gameplay.
# - The answer is the band BETWEEN: text at ~y=950-1000, bottom edge
#   ~y=1000-1050 — below the action centre, above the platform UI
#   (username/caption text starts ~y=1080 on a 1280 frame).
# The caption strip is 300px tall with the text vertically centred, so
# the text block lands at ~CAP_Y + 105..CAP_Y + 195. CAP_Y = 850 puts
# the text at ~y=955-1045. layout.py adapts this per shot (moves up to
# 650, then 450) only when the default band is occupied by the facecam
# or a HUD. render_short.py imports this value — single source of truth,
# do not duplicate it.
CAP_Y = 850
MAXW = 660  # max caption line width px
YELLOW = (255, 230, 0, 255)
RED = (255, 38, 38, 255)
CYAN = (0, 229, 255, 255)
WHITE = (255, 255, 255, 255)
DIM = (215, 215, 215, 255)
BLACK = (0, 0, 0, 255)

STYLES = ("neon", "hormozi", "hype", "karaoke", "pop", "fade", "box",
          "pulse", "boxed", "cinematic", "classic", "minimal", "bold",
          "comic", "typewriter", "pill", "bounce",
          "rawani", "lafz", "aks", "lakeer", "ali", "shamani", "saaf", "lehar")

# Style catalogue (single source of truth — the dashboard Styles page and
# preview_styles.py read this). "source" = where the style was borrowed
# from (spike-tested repos, our own V2 renders, or PeakCut's clip).
# Animation classes: "pop" (3-stage scale, dt<0.04/0.08), "anim"
# (continuous — dt quantized to 1/30s buckets), "static" (no time
# dependence — one state per chunk/active word).
STYLE_META = {
    "neon": {"name": "Glow Neon", "desc": "Montserrat Black, active word cyan with neon glow. sohaib's pick — default.",
             "source": "our V2 spike (2026-11-17)", "anim": "static"},
    "hormozi": {"name": "Hormozi Pop", "desc": "Anton ALL-CAPS, active word pops 80→112→100% and turns yellow.",
                "source": "our V2 spike (2026-11-17)", "anim": "pop"},
    "hype": {"name": "Hype", "desc": "Anton ALL-CAPS, active word pops and turns RED — PeakCut's 'CHOPPER UH' look, sohaib's benchmark.",
             "source": "PeakCut (haziqahmedqasmi/PeakCut)", "anim": "pop"},
    "karaoke": {"name": "Karaoke Sweep", "desc": "Montserrat ExtraBold, accent colour sweeps left-to-right through the active word.",
                "source": "aman ClipKaro karaoke (\\kf sweep) + our V2 karaoke", "anim": "anim"},
    "pop": {"name": "Pop", "desc": "Per-word scale bounce 80→112→100%, white — the bounce without the colour pop.",
            "source": "our V2 pop + autoclip pop", "anim": "pop"},
    "fade": {"name": "Fade Up", "desc": "Words fade in and slide up as they are spoken.",
            "source": "our V2 fade", "anim": "anim"},
    "box": {"name": "Highlighter Box", "desc": "Opaque yellow highlighter box behind the active word, black text.",
            "source": "our V2 box", "anim": "static"},
    "pulse": {"name": "Pulse", "desc": "Active word swells to 122% then settles — a heartbeat on every word.",
              "source": "our V2 pulse", "anim": "anim"},
    "boxed": {"name": "Boxed", "desc": "Opaque dark box behind the whole caption line, active word in blue.",
              "source": "autoclip boxed", "anim": "static"},
    "cinematic": {"name": "Cinematic", "desc": "Smaller elegant lowercase text, faint glow, dark backing plate — for documentaries.",
                  "source": "autoclip cinematic", "anim": "static"},
    "classic": {"name": "Classic", "desc": "Simple clean white text, thin outline, no animation — interviews.",
                "source": "autoclip classic", "anim": "static"},
    "minimal": {"name": "Minimal", "desc": "Fade-in; hype keywords light up in yellow, the rest stays white.",
                "source": "aman ClipKaro minimal", "anim": "anim"},
    "bold": {"name": "Bold", "desc": "Extra-large heavy text, thick outline — shouts the loudest moments.",
             "source": "aman ClipKaro bold", "anim": "static"},
    "comic": {"name": "Comic", "desc": "Bangers comic font, playful tilt — fun moments, gaming bants.",
              "source": "aman ClipKaro comic", "anim": "static"},
    "typewriter": {"name": "Typewriter", "desc": "Letters appear one by one as they are spoken.",
                   "source": "remotion-captioneer typewriter", "anim": "anim"},
    "pill": {"name": "Pill", "desc": "Active word inside a white pill, black text — clean and readable.",
             "source": "remotion-captioneer pill", "anim": "static"},
    "bounce": {"name": "Bounce", "desc": "Active word bounces with a spring and turns cyan — the Beasty/Mozi look.",
              "source": "remotion-captioneer bounce", "anim": "anim"},
    # --- FuseClip 0.75.1 presets, extracted from the closed binary
    # (~/workspace/fuseclip-styles/FUSECLIP-STYLES.md, 2026-11-17) ---
    "rawani": {"name": "Rawani", "desc": "A glowing teal underline extends beneath the words as speech continues.",
               "source": "FuseClip Rawani (underline-continuous)", "anim": "anim"},
    "lafz": {"name": "Lafz", "desc": "One bold outlined word at a time, popping in as it is spoken.",
             "source": "FuseClip Lafz (single-word)", "anim": "anim"},
    "aks": {"name": "Aks", "desc": "Oversized words; the spoken word is inverted — black text on a white box.",
            "source": "FuseClip Aks", "anim": "static"},
    "lakeer": {"name": "Lakeer", "desc": "White text with a crisp blue underline on the active word only.",
               "source": "FuseClip Lakeer (underline)", "anim": "static"},
    "ali": {"name": "Ali Abdaal", "desc": "White rounded card; gray words turn black as they are spoken.",
            "source": "FuseClip Ali Abdaal (white-karaoke)", "anim": "static"},
    "shamani": {"name": "Shamani", "desc": "Glowing yellow phrases, gentle floating wave, pop-and-fade entrance.",
                "source": "FuseClip Shamani", "anim": "anim"},
    "saaf": {"name": "Saaf", "desc": "Clean bold white; each spoken word slides up into place.",
             "source": "FuseClip Saaf (bold)", "anim": "anim"},
    "lehar": {"name": "Lehar", "desc": "A violet pill fills left-to-right as the words are spoken.",
             "source": "FuseClip Lehar (filled-tube)", "anim": "static"},
}
POP_STYLES = ("hormozi", "hype", "pop")
CONTINUOUS_STYLES = ("karaoke", "fade", "pulse", "minimal", "typewriter",
                     "bounce", "rawani", "lafz", "shamani", "saaf")
BLUE = (77, 166, 255, 255)
GREEN = (0, 230, 118, 255)


def _load_words(transcript_path, clip_start, clip_end):
    from PIL import Image  # noqa: F401  (kept for parity; not used here)
    d = json.load(open(transcript_path))
    # 'display' is the caption text (transliterated to Roman when the
    # speech is Hindi/Urdu — see transcribe.py / transliterate.py).
    # Falls back to the raw Whisper word when absent (old transcripts).
    raw = [dict(w, word=(w.get("display") or w["word"]).strip())
           for w in d["words"] if (w.get("display") or w["word"]).strip()]
    # FIX 1: include words overlapping the window (not only fully inside), clamp.
    words = []
    for w in raw:
        if w["start"] < clip_end and w["end"] > clip_start:
            words.append({"start": max(w["start"], clip_start),
                          "end": min(w["end"], clip_end), "word": w["word"]})
    words.sort(key=lambda w: w["start"])
    # FIX 2: cursor normalisation — every word gets >= 0.08s active time.
    cursor = clip_start
    norm = []
    for w in words:
        s = max(w["start"], cursor)
        e = max(w["end"], s + 0.08)
        e = min(e, clip_end)
        norm.append({"start": s, "end": e, "word": w["word"]})
        cursor = e
    return norm


def _chunk_words(words, clip_start, clip_end, max_words=3):
    # max_words per chunk; break on gap > 0.5s or sentence-ending punctuation.
    # Lafz (FuseClip single-word display) uses max_words=1.
    chunks = []
    cur = []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            if gap > 0.5 or len(cur) >= max_words or cur[-1]["word"][-1] in ".?!":
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    # chunk visible window: first word start -> last word end + 0.4s hold,
    # capped at next chunk start (FIX 3: caption HOLDS across inter-word gaps).
    for i, c in enumerate(chunks):
        nxt = chunks[i + 1][0]["start"] if i + 1 < len(chunks) else clip_end
        c_vis_end = min(c[-1]["end"] + 0.4, nxt, clip_end)
        for w in c:
            w["chunk"] = i
            w["vis_start"] = c[0]["start"]
            w["vis_end"] = c_vis_end
    return chunks


def _active_at(chunks, t):
    """Return (chunk_idx, active_word_idx, dt_since_word_start) or None."""
    for i, c in enumerate(chunks):
        if c[0]["start"] - 1e-6 <= t < c[-1]["vis_end"]:
            act = 0
            for j, w in enumerate(c):
                if w["start"] <= t:
                    act = j
            return i, act, t - c[act]["start"]
    return None


def _fit_font(load, path, text, start_size, maxw, min_size=44):
    """Largest font size (<= start_size) where text fits maxw."""
    for sz in range(start_size, min_size - 1, -2):
        f = load(path, sz)
        if f.getlength(text) <= maxw:
            return f, sz
    return load(path, min_size), min_size


# Caption scripts (multilingual, 2026-11-17): Latin (English / Roman
# Urdu-Hindi) uses Anton + Montserrat Black as before. Devanagari and
# Nastaliq need their own fonts (Anton/Montserrat have no such glyphs)
# AND libraqm shaping — PIL + Layout.RAQM (the Devanagari fix proven in
# ~/workspace/fuseclip-spike/DEVANAGARI-FIX-TEST.md). Bundled in fonts/.
SCRIPT_FONTS = {
    "latin": ("Anton-Regular.ttf", "Montserrat-Black.ttf"),
    "devanagari": ("NotoSansDevanagari-Bold.ttf", "NotoSansDevanagari-Bold.ttf"),
    "urdu": ("NotoNastaliqUrdu-Regular.ttf", "NotoNastaliqUrdu-Regular.ttf"),
}
# Nastaliq glyphs are tall — start smaller so the line fits the 300px strip.
SCRIPT_START_SIZES = {"latin": (76, 68), "devanagari": (68, 60), "urdu": (60, 54)}


def build_caption_track(transcript_path, clip_start, clip_end, style,
                        out_dir, fonts_dir, fps=FPS, script="latin",
                        scale=1.0, out_size=None):
    """Build the transparent caption-track video for one style.

    Returns (track_mov_path, manifest_dict). Re-runnable: rebuilds from scratch.
    script: 'latin' (English / Roman Urdu-Hindi — default), 'devanagari',
    'urdu'. Non-Latin scripts use their own font + libraqm shaping and are
    NOT uppercased (upper() is a no-op on them, but skipping it is safer).
    The transcript's 'caption_script' field (written by transcribe.py)
    decides this — render_short.py passes it through.
    scale: 1.0 = 720x1280 output (strip 720x300); 1.5 = 1080x1920
    (strip 1080x450, fonts/strokes/blur scaled) — native 1080 text, not
    an upscale of the 720 track.
    out_size: (out_w, out_h) for non-9:16 ratios — the strip is built at
    the output width, fonts scaled by output HEIGHT (so text stays the
    same fraction of the frame), line width scaled by output width.
    When given, it replaces `scale` (scale is kept for backward compat).
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}, got {style!r}")
    if script not in SCRIPT_FONTS:
        raise ValueError(f"script must be one of {tuple(SCRIPT_FONTS)}, got {script!r}")
    f_hormozi, f_neon = SCRIPT_FONTS[script]
    anton = os.path.join(fonts_dir, f_hormozi)
    monts = os.path.join(fonts_dir, f_neon)
    for p in (anton, monts):
        if not os.path.exists(p):
            raise FileNotFoundError(f"missing font: {p}")
    # libraqm shaping for Devanagari / Nastaliq (proven fix); Latin uses default.
    layout_engine = None
    if script in ("devanagari", "urdu"):
        layout_engine = ImageFont.Layout.RAQM

    def load(path, size):
        if layout_engine is not None:
            return ImageFont.truetype(path, size, layout_engine=layout_engine)
        return ImageFont.truetype(path, size)

    def disp(s):
        return s.upper() if script == "latin" else s

    size_h, size_n = SCRIPT_START_SIZES[script]
    # scale everything for the output size. fscale = font/blur/stroke
    # scale (by output HEIGHT — text stays the same fraction of the
    # frame); the strip is the full output width, its height 300px at
    # 1280 tall. scale=1.0 -> 720x1280, scale=1.5 -> 1080x1920, same as
    # before; out_size=(out_w, out_h) replaces scale for other ratios.
    if out_size is None:
        out_size = (int(720 * scale), int(1280 * scale))
    fscale = out_size[1] / 1280.0
    strip_w, strip_h = int(out_size[0]), int(STRIP_H * fscale)
    maxw = int(MAXW * out_size[0] / 720.0)
    size_h, size_n = int(size_h * fscale), int(size_n * fscale)
    sw_pop, sw_neon = max(4, int(8 * fscale)), max(4, int(6 * fscale))
    blur_r = max(4, int(8 * fscale))

    words = _load_words(transcript_path, clip_start, clip_end)
    chunks = _chunk_words(words, clip_start, clip_end,
                          max_words=1 if style == "lafz" else 3)
    print(f"[captions:{style}] words in clip: {len(words)}, chunks: {len(chunks)}, "
          f"script: {script}, out_size: {out_size}")

    def draw_chunk_pop(chunk, active_idx, stage, accent=YELLOW):
        """Pop animation (Hormozi Pop = yellow accent, Hype = red accent —
        PeakCut's 'CHOPPER UH' look, sohaib's benchmark 2026-11-17).
        stage 0: active word 80%, stage 1: 112%, stage 2: 100% (accent)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, anton, text, size_h, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        sizes = []
        for j, w in enumerate(chunk):
            if j == active_idx:
                sc = [0.80, 1.12, 1.00][stage]
                sizes.append((load(anton, max(20, int(base_sz * sc))), sc))
            else:
                sizes.append((base_font, 1.0))
        widths = [sizes[j][0].getlength(disp(w["word"])) for j, w in enumerate(chunk)]
        space_w = base_font.getlength(" ")
        total = sum(widths) + space_w * (len(chunk) - 1)
        x = (strip_w - total) / 2
        max_h = max(f.size for f, _ in sizes)
        y = (strip_h - max_h) / 2
        for j, w in enumerate(chunk):
            f = sizes[j][0]
            col = accent if j == active_idx else WHITE
            yj = y + (max_h - f.size) / 2
            dr.text((x, yj), disp(w["word"]), font=f, fill=col,
                    stroke_width=sw_pop, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_neon(chunk, active_idx):
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths = [base_font.getlength(disp(w["word"])) for w in chunk]
        space_w = base_font.getlength(" ")
        total = sum(widths) + space_w * (len(chunk) - 1)
        x0 = (strip_w - total) / 2
        y = (strip_h - base_sz) / 2
        glow = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        x = x0
        for j, w in enumerate(chunk):
            if j == active_idx:
                gd.text((x, y), disp(w["word"]), font=base_font, fill=CYAN,
                        stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        glow = glow.filter(ImageFilter.GaussianBlur(blur_r))
        img = Image.alpha_composite(img, glow)
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            col = CYAN if j == active_idx else DIM
            dr.text((x, y), disp(w["word"]), font=base_font, fill=col,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def _layout(chunk, font, text_fn=None):
        """x positions for the words of one chunk, centred in the strip."""
        tf = text_fn or disp
        widths = [font.getlength(tf(w["word"])) for w in chunk]
        space_w = font.getlength(" ")
        total = sum(widths) + space_w * (len(chunk) - 1)
        return widths, space_w, (strip_w - total) / 2

    def draw_chunk_karaoke(chunk, active_idx, dt):
        """Accent sweeps left->right through the active word (aman kf-sweep)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            dr.text((x, y), disp(w["word"]), font=base_font, fill=WHITE,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        w = chunk[active_idx]
        dur = max(w["end"] - w["start"], 0.05)
        frac = min(1.0, max(0.0, dt / dur))
        if frac > 0:
            acc = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
            ad = ImageDraw.Draw(acc)
            ax = x0 + sum(widths[:active_idx]) + space_w * active_idx
            ad.text((ax, y), disp(w["word"]), font=base_font, fill=CYAN,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            mask = Image.new("L", (strip_w, strip_h), 0)
            ImageDraw.Draw(mask).rectangle(
                [ax, 0, ax + widths[active_idx] * frac, strip_h], fill=255)
            img = Image.composite(acc, img, mask)
        return img

    def draw_chunk_fade(chunk, active_idx, dt):
        """Words fade in and slide up as they are spoken (V2 fade)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y0 = (strip_h - base_sz) / 2
        for j, w in enumerate(chunk):
            if j > active_idx:
                continue
            a, yj = 1.0, y0
            if j == active_idx:
                a = min(1.0, max(0.0, dt / 0.15))
                yj = y0 + (1 - a) * 20
            layer = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            x = x0 + sum(widths[:j]) + space_w * j
            ld.text((x, yj), disp(w["word"]), font=base_font, fill=WHITE,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            if a < 1.0:
                layer.putalpha(layer.split()[3].point(lambda p: int(p * a)))
            img = Image.alpha_composite(img, layer)
        return img

    def draw_chunk_box(chunk, active_idx, dt):
        """Opaque yellow highlighter box behind the active word (V2 box)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        pad = 12
        ax = x0 + sum(widths[:active_idx]) + space_w * active_idx
        dr = ImageDraw.Draw(img)
        dr.rectangle([ax - pad, y - pad,
                      ax + widths[active_idx] + pad, y + base_sz + pad],
                     fill=YELLOW)
        x = x0
        for j, w in enumerate(chunk):
            col = BLACK if j == active_idx else WHITE
            sw = 0 if j == active_idx else sw_neon
            dr.text((x, y), disp(w["word"]), font=base_font, fill=col,
                    stroke_width=sw, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_pulse(chunk, active_idx, dt):
        """Active word swells to 122% then settles (V2 pulse)."""
        import math
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        sc = 1.0 + 0.22 * math.exp(-dt * 2.0)
        big = load(monts, max(20, int(base_sz * sc)))
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        widths_b = list(widths)
        widths_b[active_idx] = big.getlength(disp(chunk[active_idx]["word"]))
        total = sum(widths_b) + space_w * (len(chunk) - 1)
        x = (strip_w - total) / 2
        dr = ImageDraw.Draw(img)
        for j, w in enumerate(chunk):
            f = big if j == active_idx else base_font
            yj = (strip_h - f.size) / 2
            dr.text((x, yj), disp(w["word"]), font=f, fill=WHITE,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths_b[j] + space_w
        return img

    def draw_chunk_boxed(chunk, active_idx, dt):
        """Opaque dark box behind the whole line, active word blue (autoclip)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        pad = 16
        total = sum(widths) + space_w * (len(chunk) - 1)
        dr.rectangle([x0 - pad, y - pad, x0 + total + pad, y + base_sz + pad],
                     fill=(0, 0, 0, 200))
        x = x0
        for j, w in enumerate(chunk):
            col = BLUE if j == active_idx else WHITE
            dr.text((x, y), disp(w["word"]), font=base_font, fill=col,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_cinematic(chunk, active_idx, dt):
        """Smaller lowercase text, faint glow, dark backing plate (autoclip).
        Least readable on bright gameplay — the plate keeps it legible."""
        low = (lambda s: s.lower()) if script == "latin" else (lambda s: s)
        text = " ".join(low(w["word"]) for w in chunk)
        small = int(size_n * 0.75)
        base_font, base_sz = _fit_font(load, monts, text, small, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font, low)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        pad = 14
        total = sum(widths) + space_w * (len(chunk) - 1)
        dr.rectangle([x0 - pad, y - pad, x0 + total + pad, y + base_sz + pad],
                     fill=(0, 0, 0, 140))
        glow = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        ax = x0 + sum(widths[:active_idx]) + space_w * active_idx
        gd.text((ax, y), low(chunk[active_idx]["word"]), font=base_font,
                fill=(255, 255, 255, 120))
        glow = glow.filter(ImageFilter.GaussianBlur(blur_r))
        img = Image.alpha_composite(img, glow)
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            col = WHITE if j == active_idx else DIM
            dr.text((x, y), low(w["word"]), font=base_font, fill=col)
            x += widths[j] + space_w
        return img

    def draw_chunk_classic(chunk, active_idx, dt):
        """Simple clean white text, thin outline, no animation (autoclip)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            dr.text((x, y), disp(w["word"]), font=base_font, fill=WHITE,
                    stroke_width=2, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    _HYPE_KEYS = {"go", "insane", "wow", "crazy", "look", "watch", "wait",
                  "bro", "damn", "omg", "chalo", "dekho", "kya", "yaar",
                  "bhai", "wah"}

    def draw_chunk_minimal(chunk, active_idx, dt):
        """Fade-in; hype keywords light up yellow (aman minimal)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        t_chunk = dt + (chunk[active_idx]["start"] - chunk[0]["start"])
        a = min(1.0, max(0.0, t_chunk / 0.2))
        layer = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        x = x0
        for j, w in enumerate(chunk):
            wd = disp(w["word"])
            col = YELLOW if w["word"].lower().strip(".,!?") in _HYPE_KEYS else WHITE
            ld.text((x, y), wd, font=base_font, fill=col,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        if a < 1.0:
            layer.putalpha(layer.split()[3].point(lambda p: int(p * a)))
        img = Image.alpha_composite(img, layer)
        return img

    def draw_chunk_bold(chunk, active_idx, dt):
        """Extra-large heavy text, thick outline (aman bold)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        big_sz = int(size_h * 1.15)
        base_font, base_sz = _fit_font(load, anton, text, big_sz, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            col = YELLOW if j == active_idx else WHITE
            dr.text((x, y), disp(w["word"]), font=base_font, fill=col,
                    stroke_width=max(6, int(10 * fscale)), stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_comic(chunk, active_idx, dt):
        """Bangers comic font, active word tilted (aman comic)."""
        comic_path = os.path.join(fonts_dir, "Bangers-Regular.ttf")
        cfont = comic_path if (script == "latin" and os.path.exists(comic_path)) else anton
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, cfont, text, size_h, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            wd = disp(w["word"])
            if j == active_idx:
                wl = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
                ImageDraw.Draw(wl).text((x, y), wd, font=base_font, fill=YELLOW,
                                        stroke_width=sw_pop, stroke_fill=BLACK)
                wl = wl.rotate(-4, resample=Image.BICUBIC,
                               center=(x + widths[j] / 2, y + base_sz / 2))
                img = Image.alpha_composite(img, wl)
            else:
                dr.text((x, y), wd, font=base_font, fill=WHITE,
                        stroke_width=sw_pop, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_typewriter(chunk, active_idx, dt):
        """Letters appear one by one as spoken (remotion typewriter)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            if j > active_idx:
                break
            wd = disp(w["word"])
            if j == active_idx:
                n = int(math.ceil(len(wd) * min(1.0, max(0.0, dt / 0.4))))
                wd = wd[:max(1, n)]
            dr.text((x, y), wd, font=base_font, fill=WHITE,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_pill(chunk, active_idx, dt):
        """Active word inside a white pill, black text (remotion pill)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        ax = x0 + sum(widths[:active_idx]) + space_w * active_idx
        pad_x, pad_y = 14, 8
        dr.rounded_rectangle([ax - pad_x, y - pad_y,
                              ax + widths[active_idx] + pad_x,
                              y + base_sz + pad_y],
                             radius=(base_sz // 2 + pad_y), fill=WHITE)
        x = x0
        for j, w in enumerate(chunk):
            col = BLACK if j == active_idx else WHITE
            sw = 0 if j == active_idx else sw_neon
            dr.text((x, y), disp(w["word"]), font=base_font, fill=col,
                    stroke_width=sw, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_bounce(chunk, active_idx, dt):
        """Active word bounces with a spring, turns cyan (Beasty/Mozi)."""
        import math
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            yj, col = y, WHITE
            if j == active_idx:
                yj = y - abs(math.sin(dt * 12.0)) * 12 * math.exp(-dt * 2.5)
                col = CYAN
            dr.text((x, yj), disp(w["word"]), font=base_font, fill=col,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_rawani(chunk, active_idx, dt):
        """Glowing teal underline extends beneath words as speech continues."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            dr.text((x, y), disp(w["word"]), font=base_font, fill=WHITE,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        w = chunk[active_idx]
        dur = max(w["end"] - w["start"], 0.05)
        frac = min(1.0, max(0.0, dt / dur))
        ux0 = x0
        ux1 = x0 + sum(widths[:active_idx]) + space_w * active_idx + widths[active_idx] * frac
        uy = y + base_sz + 10
        glow = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        ImageDraw.Draw(glow).line([ux0, uy, ux1, uy], fill=(45, 212, 191, 255), width=6)
        glow = glow.filter(ImageFilter.GaussianBlur(4))
        img = Image.alpha_composite(img, glow)
        ImageDraw.Draw(img).line([ux0, uy, ux1, uy], fill=(45, 212, 191, 255), width=4)
        return img

    def draw_chunk_lafz(chunk, active_idx, dt):
        """One bold outlined word at a time, popping in (chunks are 1 word)."""
        text = disp(chunk[0]["word"])
        base_font, base_sz = _fit_font(load, monts, text, int(size_n * 1.2), maxw)
        sc = [0.80, 1.12, 1.00][0 if dt < 0.04 else (1 if dt < 0.08 else 2)]
        f = load(monts, max(20, int(base_sz * sc))) if sc != 1.0 else base_font
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        wdt = f.getlength(text)
        x = (strip_w - wdt) / 2
        y = (strip_h - f.size) / 2
        ImageDraw.Draw(img).text((x, y), text, font=f, fill=WHITE,
                                 stroke_width=sw_neon, stroke_fill=BLACK)
        return img

    def draw_chunk_aks(chunk, active_idx, dt):
        """Oversized words; the spoken word is inverted (black on white)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        big_sz = int(size_h * 1.1)
        base_font, base_sz = _fit_font(load, monts, text, big_sz, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        ax = x0 + sum(widths[:active_idx]) + space_w * active_idx
        pad = 10
        dr.rectangle([ax - pad, y - pad,
                      ax + widths[active_idx] + pad, y + base_sz + pad],
                     fill=WHITE)
        x = x0
        for j, w in enumerate(chunk):
            col = BLACK if j == active_idx else WHITE
            sw = 0 if j == active_idx else sw_neon
            dr.text((x, y), disp(w["word"]), font=base_font, fill=col,
                    stroke_width=sw, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_lakeer(chunk, active_idx, dt):
        """Crisp blue underline on the active word only."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            dr.text((x, y), disp(w["word"]), font=base_font, fill=WHITE,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        ax = x0 + sum(widths[:active_idx]) + space_w * active_idx
        uy = y + base_sz + 8
        dr.line([ax, uy, ax + widths[active_idx], uy], fill=(37, 99, 235, 255), width=5)
        return img

    def draw_chunk_ali(chunk, active_idx, dt):
        """White rounded card; gray words turn black as spoken (Ali Abdaal)."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        pad_x, pad_y = 18, 12
        total = sum(widths) + space_w * (len(chunk) - 1)
        dr.rounded_rectangle([x0 - pad_x, y - pad_y,
                              x0 + total + pad_x, y + base_sz + pad_y],
                             radius=24, fill=WHITE)
        x = x0
        for j, w in enumerate(chunk):
            col = (0, 0, 0, 255) if j <= active_idx else (191, 191, 191, 255)
            dr.text((x, y), disp(w["word"]), font=base_font, fill=col)
            x += widths[j] + space_w
        return img

    def draw_chunk_shamani(chunk, active_idx, dt):
        """Glowing yellow, gentle floating wave, pop-and-fade entrance."""
        import math
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y0 = (strip_h - base_sz) / 2
        float_y = math.sin(dt * 3.0) * 4
        a = min(1.0, max(0.0, dt / 0.2))
        layer = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        ax = x0 + sum(widths[:active_idx]) + space_w * active_idx
        ImageDraw.Draw(layer).text(
            (ax, y0 + float_y), disp(chunk[active_idx]["word"]), font=base_font,
            fill=(255, 230, 0, 255), stroke_width=sw_neon, stroke_fill=BLACK)
        layer = layer.filter(ImageFilter.GaussianBlur(blur_r))
        img = Image.alpha_composite(img, layer)
        ld = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            col = (255, 230, 0, 255) if j == active_idx else WHITE
            ld.text((x, y0 + float_y), disp(w["word"]), font=base_font, fill=col,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        if a < 1.0:
            img.putalpha(img.split()[3].point(lambda p: int(p * a)))
        return img

    def draw_chunk_saaf(chunk, active_idx, dt):
        """Clean bold white; each spoken word slides up into place."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y0 = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            yj = y0
            if j == active_idx:
                k = min(1.0, max(0.0, dt / 0.15))
                yj = y0 + (1 - k) * 20
            dr.text((x, yj), disp(w["word"]), font=base_font, fill=WHITE,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    def draw_chunk_lehar(chunk, active_idx, dt):
        """Violet pill fills left-to-right as the words are spoken."""
        text = " ".join(disp(w["word"]) for w in chunk)
        base_font, base_sz = _fit_font(load, monts, text, size_n, maxw)
        img = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        widths, space_w, x0 = _layout(chunk, base_font)
        y = (strip_h - base_sz) / 2
        dr = ImageDraw.Draw(img)
        pad_x, pad_y = 16, 10
        total = sum(widths) + space_w * (len(chunk) - 1)
        pill = [x0 - pad_x, y - pad_y, x0 + total + pad_x, y + base_sz + pad_y]
        dr.rounded_rectangle(pill, radius=(base_sz // 2 + pad_y),
                             fill=(124, 58, 237, 90))
        fill_right = (x0 + sum(widths[:active_idx + 1])
                      + space_w * active_idx + pad_x)
        fill_layer = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
        ImageDraw.Draw(fill_layer).rounded_rectangle(
            pill, radius=(base_sz // 2 + pad_y), fill=(124, 58, 237, 255))
        mask = Image.new("L", (strip_w, strip_h), 0)
        ImageDraw.Draw(mask).rectangle([0, 0, fill_right, strip_h], fill=255)
        img = Image.composite(fill_layer, img, mask)
        dr = ImageDraw.Draw(img)
        x = x0
        for j, w in enumerate(chunk):
            dr.text((x, y), disp(w["word"]), font=base_font, fill=WHITE,
                    stroke_width=sw_neon, stroke_fill=BLACK)
            x += widths[j] + space_w
        return img

    pop_stages = style in POP_STYLES
    continuous = style in CONTINUOUS_STYLES
    if style == "hormozi":
        draw_fn = lambda c, a, s: draw_chunk_pop(c, a, s, YELLOW)
    elif style == "hype":
        draw_fn = lambda c, a, s: draw_chunk_pop(c, a, s, RED)
    elif style == "pop":
        draw_fn = lambda c, a, s: draw_chunk_pop(c, a, s, WHITE)
    elif style == "karaoke":
        draw_fn = draw_chunk_karaoke
    elif style == "fade":
        draw_fn = draw_chunk_fade
    elif style == "box":
        draw_fn = draw_chunk_box
    elif style == "pulse":
        draw_fn = draw_chunk_pulse
    elif style == "boxed":
        draw_fn = draw_chunk_boxed
    elif style == "cinematic":
        draw_fn = draw_chunk_cinematic
    elif style == "classic":
        draw_fn = draw_chunk_classic
    elif style == "minimal":
        draw_fn = draw_chunk_minimal
    elif style == "bold":
        draw_fn = draw_chunk_bold
    elif style == "comic":
        draw_fn = draw_chunk_comic
    elif style == "typewriter":
        draw_fn = draw_chunk_typewriter
    elif style == "pill":
        draw_fn = draw_chunk_pill
    elif style == "bounce":
        draw_fn = draw_chunk_bounce
    elif style == "rawani":
        draw_fn = draw_chunk_rawani
    elif style == "lafz":
        draw_fn = draw_chunk_lafz
    elif style == "aks":
        draw_fn = draw_chunk_aks
    elif style == "lakeer":
        draw_fn = draw_chunk_lakeer
    elif style == "ali":
        draw_fn = draw_chunk_ali
    elif style == "shamani":
        draw_fn = draw_chunk_shamani
    elif style == "saaf":
        draw_fn = draw_chunk_saaf
    elif style == "lehar":
        draw_fn = draw_chunk_lehar
    else:
        draw_fn = lambda c, a, dt: draw_chunk_neon(c, a)

    style_dir = os.path.join(out_dir, f"cap_{style}")
    frames_dir = os.path.join(style_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    blank = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
    blank_path = os.path.join(style_dir, "blank.png")
    blank.save(blank_path)

    nframes = int((clip_end - clip_start) * fps)
    cache = {}
    n_unique = 0
    for f in range(nframes):
        t = clip_start + f / fps
        st = _active_at(chunks, t)
        if st is None:
            key = "blank"
        else:
            ci, ai, dt = st
            if pop_stages:
                # 3-stage pop (dt < 0.04 / < 0.08 / after) — same as before
                stage = 0 if dt < 0.04 else (1 if dt < 0.08 else 2)
                key = f"c{ci}_a{ai}_s{stage}"
            elif continuous:
                # continuous animation — dt quantized to 1/30s buckets
                key = f"c{ci}_a{ai}_t{int(dt * 30)}"
            else:
                # static style — one state per chunk/active word
                key = f"c{ci}_a{ai}"
        if key not in cache:
            if key == "blank":
                cache[key] = blank_path
            else:
                parts = key.split("_")
                ci, ai = int(parts[0][1:]), int(parts[1][1:])
                if pop_stages:
                    img = draw_fn(chunks[ci], ai, int(parts[2][1:]))
                elif continuous:
                    img = draw_fn(chunks[ci], ai, int(parts[2][1:]) / 30.0)
                else:
                    img = draw_fn(chunks[ci], ai, 0.0)
                p = os.path.join(style_dir, f"{key}.png")
                img.save(p)
                cache[key] = p
                n_unique += 1
        # copy (not symlink) — Windows-safe, no privilege needed
        shutil.copyfile(cache[key], os.path.join(frames_dir, f"f_{f:04d}.png"))

    mov = os.path.join(style_dir, "cap_track.mov")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", str(fps),
                        "-i", os.path.join(frames_dir, "f_%04d.png"),
                        "-c:v", "qtrle", "-pix_fmt", "argb", mov],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-1000:], file=sys.stderr)
        raise RuntimeError(f"caption track encode failed ({style})")
    print(f"[captions:{style}] {n_unique} unique states, {nframes} frames, "
          f"track={mov} ({os.path.getsize(mov) / 1e6:.1f} MB)")
    manifest = {"clip": [clip_start, clip_end],
                "style": style,
                "script": script,
                "scale": fscale,
                "out_size": list(out_size),
                "words": words,
                "chunks": [[w["word"] for w in c] for c in chunks],
                "track": mov}
    return mov, manifest


def main():
    ap = argparse.ArgumentParser(description="Build a caption-track video (Hormozi Pop / Glow Neon).")
    ap.add_argument("--transcript", required=True, help="transcript JSON with word timestamps")
    ap.add_argument("--start", type=float, required=True, help="clip start (seconds, source time)")
    ap.add_argument("--end", type=float, required=True, help="clip end (seconds, source time)")
    ap.add_argument("--style", default="neon", choices=list(STYLES) + ["both"],
                    help="caption style (default: neon — sohaib's pick)")
    ap.add_argument("--script", default="latin", choices=list(SCRIPT_FONTS),
                    help="caption script (default: latin; devanagari / urdu use their own font + raqm)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="1.0 = 720x1280 output, 1.5 = 1080x1920 (native 1080 text)")
    ap.add_argument("--out-dir", required=True, help="working dir for caption PNGs + track")
    ap.add_argument("--fonts-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts"))
    a = ap.parse_args()
    styles = STYLES if a.style == "both" else (a.style,)
    for s in styles:
        mov, man = build_caption_track(a.transcript, a.start, a.end, s, a.out_dir,
                                       a.fonts_dir, script=a.script, scale=a.scale)
        json.dump(man, open(os.path.join(a.out_dir, f"cap_{s}_manifest.json"), "w"), indent=1)
        print(f"track: {mov}")


if __name__ == "__main__":
    main()
