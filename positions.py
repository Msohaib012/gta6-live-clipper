"""Positions + output ratios for the GTA 6 Live Clipper (FINAL build).

sohaib's order (2026-11-18 01:00 PKT): stop examples — give him a preview
list of EVERY subtitle position and EVERY facecam position, so he chooses
himself. Keep EVERY type. This module is the single source of truth for
all of them: the dashboard Positions page, the preview stills, and the
renderer all read from here.

FACECAM POSITIONS (gameplay mode — where the streamer's facecam goes):
  auto        auto-check per stream (layout.py — the default; picks the
              freest corner, avoids HUD + the streamer's own facecam)
  top-right   PeakCut-like corner (default when auto falls back)
  top-left    mirror corner
  mid-right   right side, vertically centred (below the top HUD)
  mid-left    left side, vertically centred
  top-band    full-width band across the top ~33% (Clipie-style: facecam
              band on top, gameplay below) — a LAYOUT, not just a corner
  off         no facecam overlay at all (plain game visuals)

CAPTION POSITIONS (where the subtitle strip sits — strip-top Y, 720-space):
  auto        layout.py picks the first free line (850, then 650, 450)
  bottom      y=850 strip-top -> text ~y950-1000 (the default — below the
              action centre, above the platforms' own UI at ~y1080)
  seam        y=430 strip-top -> text just BELOW the top-band facecam
              (Clipie-style seam placement; with corner facecams it sits
              mid-frame, so it is only recommended with top-band)
  top         y=16 strip-top -> text at the very top of the frame

OUTPUT RATIOS (ClipzLab idea — every ratio, not just 9:16):
  9:16  720x1280 / 1080x1920 — Shorts / Reels / TikTok (default)
  1:1   720x720 / 1080x1080 — square feeds
  4:5   720x900 / 1080x1350 — portrait feeds
  16:9  1280x720 / 1920x1080 — landscape (YouTube, X)

All coordinates are computed in OUTPUT space for the chosen ratio+res, so
the renderer and the preview generator agree exactly.
"""

import json
import os

# --- facecam positions -------------------------------------------------
# (name, x, y, w, h) in 720x1280 space; "band" is special-cased.
FACECAM_POSITIONS = ("auto", "top-right", "top-left", "mid-right",
                     "mid-left", "top-band", "off")
FACECAM_META = {
    "auto": {"name": "Auto (per stream)",
             "desc": "The tool checks this stream's own frames and picks the freest corner — avoids the HUD and the streamer's own facecam. Default.",
             "rect": None},
    "top-right": {"name": "Top-right corner",
                  "desc": "PeakCut-like corner — the streamer's face, bigger than PeakCut (40% of width).",
                  "rect": (416, 16, 288, 162)},
    "top-left": {"name": "Top-left corner",
                 "desc": "Mirror of top-right — for streams whose top-right has a HUD or logo.",
                 "rect": (16, 16, 288, 162)},
    "mid-right": {"name": "Mid-right",
                  "desc": "Right side, vertically centred — below the top HUD, above the caption band.",
                  "rect": (416, 420, 288, 162)},
    "mid-left": {"name": "Mid-left",
                 "desc": "Left side, vertically centred — same as mid-right, other side.",
                 "rect": (16, 420, 288, 162)},
    "top-band": {"name": "Top band (full width)",
                 "desc": "Clipie-style: the facecam stretches across the whole top ~33% as a band, gameplay fills below it.",
                 "rect": "band"},
    "off": {"name": "Off (no facecam)",
            "desc": "No facecam overlay at all — just the game, full frame.",
            "rect": None},
}

# --- caption positions -------------------------------------------------
# strip-top Y in 720x1280 space (the text sits ~100-200px into the strip).
CAPTION_POSITIONS = ("auto", "bottom", "seam", "top")
CAPTION_META = {
    "auto": {"name": "Auto (first free line)",
             "desc": "The tool checks the facecam + HUD and picks the first free line: bottom, then higher. Default.",
             "y": None},
    "bottom": {"name": "Bottom band (y~950-1000)",
               "desc": "Text at y~950-1000 — below the action centre, above the platforms' own UI. sohaib's verdict, the default.",
               "y": 850},
    "seam": {"name": "Seam (below the facecam band)",
             "desc": "Text just below the top-band facecam (Clipie-style seam). Best with the top-band facecam.",
             "y": 430},
    "top": {"name": "Top",
            "desc": "Text at the very top of the frame — for when the bottom is busy (HUD, subtitles in the game).",
            "y": 16},
}

# --- output ratios -----------------------------------------------------
RATIOS = ("9:16", "1:1", "4:5", "16:9")
RATIO_META = {
    "9:16": {"name": "9:16 vertical",
             "desc": "Shorts / Reels / TikTok. Default.",
             "size_720": (720, 1280), "size_1080": (1080, 1920)},
    "1:1": {"name": "1:1 square",
            "desc": "Square feeds (Instagram grid, X).",
            "size_720": (720, 720), "size_1080": (1080, 1080)},
    "4:5": {"name": "4:5 portrait",
            "desc": "Portrait feeds (Instagram, Facebook).",
            "size_720": (720, 900), "size_1080": (1080, 1350)},
    "16:9": {"name": "16:9 landscape",
             "desc": "Landscape (YouTube, X, Facebook).",
             "size_720": (1280, 720), "size_1080": (1920, 1080)},
}

# facecam size per ratio at 720 (width, height); scaled by res/720.
# 40% of width for portrait/square; 25% for landscape.
FACECAM_SIZE_720 = {
    "9:16": (288, 162),
    "1:1": (288, 162),
    "4:5": (288, 162),
    "16:9": (320, 180),
}


def ratio_params(ratio="9:16", res=720):
    """Everything the renderer needs for one ratio+res.

    Returns dict: out_w, out_h, crop (x, y, w, h in the 1920x1080 source),
    fc (w, h) facecam size, band_h (top-band height), strip_h (caption
    strip height), cap_y_default (strip-top Y for the bottom position).
    The source is 1920x1080; the crop is the largest window at the target
    aspect (9:16 keeps the proven 608x1080 centre crop = 1.18x upscale).
    """
    if ratio not in RATIOS:
        raise ValueError(f"ratio must be one of {RATIOS}, got {ratio!r}")
    if res not in (720, 1080):
        raise ValueError(f"res must be 720 or 1080, got {res!r}")
    out_w, out_h = RATIO_META[ratio][f"size_{res}"]
    s = res / 720.0  # resolution scale (1.0 at 720, 1.5 at 1080)
    # source crop at the target aspect, largest possible from 1920x1080
    ar = out_w / out_h
    if ar >= 16 / 9:
        crop_w, crop_h = 1920, 1080
    else:
        crop_h = 1080
        crop_w = int(1080 * ar)
        if ratio == "9:16":
            crop_w = 608 if res == 720 else 912  # the proven centre crop
    crop_x = (1920 - crop_w) // 2
    fc_w, fc_h = FACECAM_SIZE_720[ratio]
    fc_w, fc_h = int(fc_w * s), int(fc_h * s)
    fc_w, fc_h = (fc_w // 2) * 2, (fc_h // 2) * 2  # libx264 needs even dims
    return {
        "ratio": ratio, "res": res,
        "out_w": out_w, "out_h": out_h,
        "crop": (crop_x, 0, crop_w, crop_h),
        "fc_w": fc_w, "fc_h": fc_h,
        "band_h": int(out_h / 3),          # top-band facecam height (~33%)
        "strip_h": int(300 * out_h / 1280),  # caption strip height
        "cap_y_default": int(850 * out_h / 1280),
        "cap_y_seam": int(430 * out_h / 1280),
        "cap_y_top": int(16 * out_h / 1280),
    }


def game_crop(rp, band=False):
    """Source crop (x, y, w, h) in the 1920x1080 source for the game area.

    band=False: the full output frame is gameplay (the normal crop).
    band=True: only the area BELOW the top-band facecam is gameplay, so
    the crop is wider (e.g. 910x1080 for 720x1280 — more of the game
    stays visible instead of stretching the 608px crop).
    """
    if not band:
        cx, cy, cw, ch = rp["crop"]
        return (cx, cy, cw, ch)
    out_w, out_h = rp["out_w"], rp["out_h"]
    game_h = out_h - rp["band_h"]
    ar = out_w / game_h
    if ar >= 16 / 9:
        cw, ch = 1920, int(1920 / ar)
        return ((1920 - cw) // 2, (1080 - ch) // 2, cw, ch)
    ch = 1080
    cw = int(1080 * ar)
    return ((1920 - cw) // 2, 0, cw, ch)


def facecam_rect(position, rp, auto_layout=None):
    """Overlay rect (x, y, w, h) in OUTPUT coords for a facecam position.

    position: one of FACECAM_POSITIONS. auto_layout: the dict returned by
    layout.choose_layout() (used for "auto"). Returns:
      ("corner", (x, y, w, h)) — a corner/mid overlay,
      ("band", None)           — the full-width top band,
      (None, None)             — off / no facecam.
    """
    if position not in FACECAM_POSITIONS:
        raise ValueError(f"facecam position must be one of "
                         f"{FACECAM_POSITIONS}, got {position!r}")
    out_w, out_h = rp["out_w"], rp["out_h"]
    fc_w, fc_h = rp["fc_w"], rp["fc_h"]
    sx, sy = out_w / 720.0, out_h / 1280.0
    if position == "off":
        return None, None
    if position == "top-band":
        return "band", None
    if position == "auto":
        if auto_layout and auto_layout.get("overlay"):
            ov = auto_layout["overlay"]
            return "corner", (int(ov["x"] * sx), int(ov["y"] * sy),
                              int(ov["w"] * sx), int(ov["h"] * sy))
        position = "top-right"  # fallback when auto has nothing
    # fixed positions: rects are defined in 720x1280 space, scaled here
    rect = FACECAM_META[position]["rect"]
    x, y, w, h = rect
    # fixed rects are 288x162 at 720 — rescale to this ratio's facecam size
    # (keeps the same corner/margin, swaps in the right size)
    margin_x = int(16 * sx)
    margin_y = int(16 * sy)
    if "right" in position:
        x = out_w - fc_w - margin_x
    else:
        x = margin_x
    if position.startswith("mid"):
        y = int(420 * sy)
    else:
        y = margin_y
    return "corner", (x, y, fc_w, fc_h)


def caption_y(position, rp, auto_layout=None):
    """Strip-top Y (output coords) for a caption position."""
    if position not in CAPTION_POSITIONS:
        raise ValueError(f"caption position must be one of "
                         f"{CAPTION_POSITIONS}, got {position!r}")
    if position == "auto":
        if auto_layout and auto_layout.get("caption_y") is not None:
            return int(auto_layout["caption_y"] * rp["out_h"] / 1280)
        return rp["cap_y_default"]
    if position == "bottom":
        return rp["cap_y_default"]
    if position == "seam":
        return rp["cap_y_seam"]
    return rp["cap_y_top"]  # "top"


# --- annotated preview stills ------------------------------------------
# The dashboard Positions page shows one still per option, drawn on a
# REAL GTA frame (not a mockup): facecam box + caption band + crop
# boundaries + safe zones. Per-stream previews use the same drawing on
# that stream's own frames (draw_layout_preview).

def _draw_box(dr, rect, color, width=4, label=None, font=None):
    x, y, w, h = [int(v) for v in rect]
    for i in range(width):
        dr.rectangle([x + i, y + i, x + w - i, y + h - i], outline=color)
    if label and font is not None:
        tw = dr.textlength(label, font=font)
        ly = y - 26 if y >= 26 else y + 4  # label above the box, or inside it
        dr.rectangle([x, ly, x + tw + 12, ly + 24], fill=color)
        dr.text((x + 6, ly + 2), label, font=font, fill=(0, 0, 0, 255))


def _draw_band(img, dr, y, h, out_w, color, alpha=70, label=None, font=None):
    from PIL import Image
    ov = Image.new("RGBA", (out_w, h), color + (alpha,))
    img.paste(ov, (0, y), ov)
    if label and font is not None:
        dr.text((12, y + 6), label, font=font, fill=(255, 255, 255, 255))


def draw_layout_preview(frame, layout, out_w=720, out_h=1280,
                        facecam_img=None, caption_text="OH, HERE'S GEN"):
    """Draw the full layout on one real frame (PIL Image, output size).

    layout: dict with optional keys:
      facecam: ("corner", (x,y,w,h)) | ("band", None) | (None, None)
      caption_y: strip-top Y (output coords)
      crop: (x, y, w, h) in source coords (drawn as a boundary label)
      band_h: top-band height (for the band layout)
      corner: name of the chosen corner (for the label)
    facecam_img: PIL image pasted into the facecam box (a real facecam
    crop from the same stream when available).
    Returns the annotated PIL Image.
    """
    from PIL import Image, ImageDraw, ImageFont
    img = frame.convert("RGBA").resize((out_w, out_h))
    dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
        small = font

    # --- safe zones: the bottom ~300px (in 1280 space) belong to the
    # platforms' own UI — draw it hatched red so he can SEE it. The
    # label sits at the very bottom edge so it never collides with the
    # caption band.
    ui_top = int(out_h - 300 * out_h / 1280)
    ov = Image.new("RGBA", (out_w, out_h - ui_top), (255, 0, 0, 36))
    img.paste(ov, (0, ui_top), ov)
    dr = ImageDraw.Draw(img)
    dr.text((12, out_h - 26), "PLATFORM UI ZONE — captions + facecam stay out",
            font=small, fill=(255, 120, 120, 255))

    # --- facecam ---
    kind, rect = layout.get("facecam", (None, None))
    if kind == "band":
        bh = layout.get("band_h", out_h // 3)
        if facecam_img is not None:
            fc = facecam_img.convert("RGBA").resize((out_w, bh))
            img.paste(fc, (0, 0), fc)
            dr = ImageDraw.Draw(img)
        _draw_box(dr, (0, 0, out_w, bh), (0, 229, 255, 255), width=4,
                  label="FACECAM — TOP BAND", font=font)
    elif kind == "corner" and rect:
        if facecam_img is not None:
            fc = facecam_img.convert("RGBA").resize((rect[2], rect[3]))
            img.paste(fc, (rect[0], rect[1]), fc)
            dr = ImageDraw.Draw(img)
        _draw_box(dr, rect, (0, 229, 255, 255), width=4,
                  label="FACECAM — " + str(layout.get("corner", "")).upper(),
                  font=font)

    # --- caption band ---
    cy = layout.get("caption_y")
    if cy is not None:
        strip_h = layout.get("strip_h", int(300 * out_h / 1280))
        _draw_band(img, dr, cy, strip_h, out_w, (0, 229, 255), alpha=46,
                   label="CAPTIONS — " + str(layout.get("caption_name", "")).upper(),
                   font=small)
        # sample caption text, centred, in the band (neon style)
        try:
            cap_font = ImageFont.truetype(
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "fonts", "Montserrat-Black.ttf"),
                int(56 * out_h / 1280))
        except Exception:
            cap_font = font
        tw = dr.textlength(caption_text, font=cap_font)
        ty = cy + (strip_h - 56 * out_h / 1280) // 2
        dr.text(((out_w - tw) / 2 + 2, ty + 2), caption_text, font=cap_font,
                fill=(0, 0, 0, 255))
        dr.text(((out_w - tw) / 2, ty), caption_text, font=cap_font,
                fill=(255, 255, 255, 255))
    return img


def build_position_previews(base_frame, facecam_img, out_dir,
                            caption_text="OH, HERE'S GEN"):
    """One still per facecam position + per caption position + one
    combined 'auto' still, all drawn on a REAL GTA frame.

    base_frame: PIL Image of the gameplay frame (1920x1080 source).
    facecam_img: PIL Image of a real facecam crop (same stream).
    Returns {key: path} for every still written (+ index.json).
    """
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    out = {}
    # base: centre 608x1080 crop -> 720x1280 (same as the game render)
    base = base_frame.convert("RGBA").crop((656, 0, 656 + 608, 1080)).resize((720, 1280))
    rp = ratio_params("9:16", 720)
    # --- facecam positions (caption = bottom, so the facecam is the variable)
    for pos in FACECAM_POSITIONS:
        if pos == "auto":
            continue  # auto gets its own combined still below
        kind, rect = facecam_rect(pos, rp)
        layout = {"facecam": (kind, rect), "caption_y": rp["cap_y_default"],
                  "strip_h": rp["strip_h"], "caption_name": "bottom",
                  "corner": pos, "band_h": rp["band_h"]}
        img = draw_layout_preview(base, layout, facecam_img=facecam_img,
                                  caption_text=caption_text)
        p = os.path.join(out_dir, f"facecam-{pos}.jpg")
        img.convert("RGB").save(p, quality=88)
        out[f"facecam-{pos}"] = p
    # --- caption positions (facecam = top-right, so the caption is the variable)
    kind, rect = facecam_rect("top-right", rp)
    for pos in CAPTION_POSITIONS:
        if pos == "auto":
            continue
        cy = caption_y(pos, rp)
        layout = {"facecam": (kind, rect), "caption_y": cy,
                  "strip_h": rp["strip_h"], "caption_name": pos,
                  "corner": "top-right", "band_h": rp["band_h"]}
        img = draw_layout_preview(base, layout, facecam_img=facecam_img,
                                  caption_text=caption_text)
        p = os.path.join(out_dir, f"caption-{pos}.jpg")
        img.convert("RGB").save(p, quality=88)
        out[f"caption-{pos}"] = p
    # --- combined auto still (what the tool would pick on this stream)
    layout = {"facecam": ("corner", (416, 16, 288, 162)),
              "caption_y": rp["cap_y_default"], "strip_h": rp["strip_h"],
              "caption_name": "bottom (auto)", "corner": "top-right",
              "band_h": rp["band_h"]}
    img = draw_layout_preview(base, layout, facecam_img=facecam_img,
                              caption_text=caption_text)
    p = os.path.join(out_dir, "combined-auto.jpg")
    img.convert("RGB").save(p, quality=88)
    out["combined-auto"] = p
    json.dump({"positions": out,
               "facecam": {k: FACECAM_META[k] for k in FACECAM_POSITIONS},
               "captions": {k: CAPTION_META[k] for k in CAPTION_POSITIONS},
               "ratios": {k: RATIO_META[k] for k in RATIOS}},
              open(os.path.join(out_dir, "index.json"), "w"), indent=1)
    return out


if __name__ == "__main__":
    import sys
    print("facecam:", FACECAM_POSITIONS)
    print("captions:", CAPTION_POSITIONS)
    print("ratios:", RATIOS)
    for r in RATIOS:
        print(r, ratio_params(r, 720))
