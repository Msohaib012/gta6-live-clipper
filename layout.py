"""Auto layout for the PeakCut-style 'gameplay' mode (GTA 6 Live Clipper).

sohaib's orders (2026-11-17):
1. PeakCut-like layout: game fills the frame, facecam in a corner — BUT
   OUR facecam must be BIGGER than PeakCut's (~323x186 in a 1080x1920
   frame = 30% of width). Ours: 288x162 in 720x1280 = 40% of width, big
   enough to actually see the streamer's face, gameplay still visible.
2. Positions must be AUTO-CHECKED per stream/video — never one fixed
   corner blindly. Detect per stream: where the facecam actually is,
   where the HUD / busy areas are, and place the facecam overlay +
   captions in free zones that don't cover important content.
3. Captions: the default band is y=950-1000 (CAP_Y=850, text bottom edge
   ~y=1000-1050 — below the action centre, above the platform UI that
   starts ~y=1080; the old y=980-1160 hid behind the platform UI and
   the 23:11 fix y=800-899 covered the gameplay — both were rejected).
   The caption Y is adaptive — if the facecam or a game HUD occupies
   the default band, captions move to the nearest free line above it
   (650, then 450), once per clip (the facecam position is fixed per
   stream, so the free line is stable — the aman ClipKaro rule,
   applied per stream).

How detection works (all on the clip's own frames, nothing hard-coded):
- Facecam rectangle: Canny edges + contour rectangles, sampled across
  the clip. A rectangle is the facecam only if it is STILL — same
  position/size in >= 75% of samples (PeakCut's "never-moving straight
  border" idea) — and a MediaPipe face sits inside it.
- Fallback: no still rectangle -> the median MediaPipe face box,
  expanded 2.2x, is the facecam crop.
- No face at all -> no facecam to show; gameplay mode falls back to
  plain game mode (the caller logs this, it is not an error).
- HUD / busy zones: per-cell Canny edge density across samples. A zone
  that is always busy is HUD/static UI — the facecam overlay and the
  captions avoid it.
- Corner choice: candidates top-right (PeakCut-like), top-left,
  mid-right, mid-left. Bottom corners are excluded outright — the
  bottom ~300px belong to the platforms' own UI. Scored by: overlap
  with the caption band, overlap with the streamer's own facecam as it
  appears in the base crop (no double facecam), and HUD busyness.
  Top-right wins ties.
- Caption Y: candidates [700, 500, 300] (strip-top Y; the text itself
  sits ~100-200px into the strip). First line whose text band is free
  of the facecam overlay and of busy HUD cells wins; 700 is the default.

Needs the optional facecam deps (opencv + mediapipe,
requirements-facecam.txt). When they are missing, choose_layout()
returns the safe fallback (top-right, CAP_Y) and the caller renders
plain game mode instead of guessing.
"""

import json
import os

from positions import (CAPTION_POSITIONS, FACECAM_META, FACECAM_POSITIONS,  # noqa: E402
                       caption_y as _caption_y_for,
                       facecam_rect as _facecam_rect_for,
                       ratio_params)

OUT_W, OUT_H = 720, 1280
# Facecam overlay: 40% of output width (PeakCut: 30%). 16:9.
FC_W, FC_H = 288, 162
MARGIN = 16
CAP_Y_DEFAULT = 850
CAP_Y_CANDIDATES = (850, 650, 450)  # strip-top Y, in order of preference
STRIP_H = 300
TEXT_TOP, TEXT_BOTTOM = 100, 200    # text band inside the strip


def _sample_frame_paths(frames_dir, n=8):
    files = sorted(f for f in os.listdir(frames_dir) if f.endswith(".jpg"))
    if not files:
        return []
    if len(files) <= n:
        return [os.path.join(frames_dir, f) for f in files]
    step = len(files) / n
    return [os.path.join(frames_dir, files[int(i * step)]) for i in range(n)]


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def detect_facecam(frames_dir, src_w=1920, src_h=1080, model_path=None):
    """Find the streamer's facecam rectangle in source coords.

    Returns {"rect": (x, y, w, h), "method": "still-border"|"face-box",
    "face": (cx, cy, w, h) | None, "samples": n} or None when nothing
    usable was found (caller falls back to plain game mode).
    """
    try:
        import cv2
    except ImportError:
        print("[layout] opencv not installed — facecam detection unavailable")
        return None
    paths = _sample_frame_paths(frames_dir)
    if not paths:
        return None

    # --- MediaPipe face boxes (BlazeFace full-range, same model as the tracker)
    face_boxes = []  # per-sample (cx, cy, w, h) in source coords
    mp_det = None
    if model_path and os.path.exists(model_path):
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
            opts = vision.FaceDetectorOptions(
                base_options=mp_python.BaseOptions(model_asset_path=model_path),
                min_detection_confidence=0.4)
            mp_det = vision.FaceDetector.create_from_options(opts)
        except Exception as e:
            print(f"[layout] mediapipe unavailable ({e}) — border-only detection")

    rect_votes = {}  # quantised rect -> count
    busy = None      # edge-density grid, accumulated
    for p in paths:
        frame = cv2.imread(p)
        if frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        # busy grid (12 rows x 20 cols) for HUD detection
        gh, gw = 12, 20
        cell = edges.reshape(gh, gray.shape[0] // gh, gw, gray.shape[1] // gw)
        dens = (cell > 0).mean(axis=(1, 3))
        busy = dens if busy is None else busy + dens
        # still-border rectangles
        cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            area = cv2.contourArea(c)
            if area < 0.01 * src_w * src_h or area > 0.30 * src_w * src_h:
                continue
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            if len(approx) != 4:
                continue
            x, y, w, h = cv2.boundingRect(approx)
            if not (1.0 <= w / max(h, 1) <= 2.5):
                continue
            key = (round(x / 20) * 20, round(y / 20) * 20,
                   round(w / 20) * 20, round(h / 20) * 20)
            rect_votes[key] = rect_votes.get(key, 0) + 1
        # face boxes
        if mp_det is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            try:
                res = mp_det.detect(img)
            except Exception:
                res = None
            if res:
                for d in res.detections:
                    bb = d.bounding_box
                    face_boxes.append((bb.origin_x + bb.width // 2,
                                       bb.origin_y + bb.height // 2,
                                       bb.width, bb.height))
    n = len(paths)
    busy = (busy / max(n, 1)) if busy is not None else None
    # close the detector now — otherwise its __del__ fires at interpreter
    # shutdown and prints "Exception ignored ... 'NoneType' object is not
    # callable" noise into the logs (harmless, but confusing).
    if mp_det is not None:
        try:
            mp_det.close()
        except Exception:
            pass

    # median face box (stable across the clip)
    face = None
    if face_boxes:
        import statistics
        face = (int(statistics.median([b[0] for b in face_boxes])),
                int(statistics.median([b[1] for b in face_boxes])),
                int(statistics.median([b[2] for b in face_boxes])),
                int(statistics.median([b[3] for b in face_boxes])))

    # still rectangles: present in >= 75% of samples, containing the face
    still = [(r, c) for r, c in rect_votes.items() if c >= max(2, int(0.75 * n))]
    still.sort(key=lambda rc: -rc[1])
    for rect, _count in still:
        x, y, w, h = rect
        if face and x <= face[0] <= x + w and y <= face[1] <= y + h:
            return {"rect": rect, "method": "still-border", "face": face,
                    "samples": n, "busy": busy.tolist() if busy is not None else None}
    if still:
        # a still rectangle with no face inside is probably a HUD panel,
        # not a webcam — do NOT use it. Fall through to the face box.
        pass
    if face:
        cx, cy, fw, fh = face
        w = int(fw * 2.2)
        h = int(w * 9 / 16)
        x = max(0, min(src_w - w, cx - w // 2))
        y = max(0, min(src_h - h, cy - h // 2))
        return {"rect": (x, y, w, h), "method": "face-box", "face": face,
                "samples": n, "busy": busy.tolist() if busy is not None else None}
    return None


def choose_layout(detection, src_w=1920, src_h=1080, cap_y_default=CAP_Y_DEFAULT,
                  facecam_pos="auto", caption_pos="auto", ratio="9:16", res=720):
    """Pick the overlay corner + caption Y for the gameplay layout.

    detection: result of detect_facecam() (or None).
    facecam_pos / caption_pos: sohaib's picks from the Positions page
    ("auto" = the old auto-check behaviour; any other value OVERRIDES
    the auto choice — he chooses, the tool obeys). ratio/res scale the
    result to the output size.
    Returns a dict with overlay rect (OUTPUT coords), caption Y (output
    coords), the facecam source rect, and the reasons — everything the
    renderer and the report need. For the top-band layout the dict has
    "band": True and "band_h" instead of an overlay rect.
    """
    rp = ratio_params(ratio, res)
    sx, sy = rp["out_w"] / 720.0, rp["out_h"] / 1280.0
    src_rect = detection["rect"] if detection else None

    def _scale_rect_720(rect):
        x, y, w, h = rect
        return (int(x * sx), int(y * sy), int(w * sx), int(h * sy))

    # --- top-band layout (Clipie-style): facecam across the whole top,
    # gameplay below it. Caption defaults to the seam (just under the band).
    if facecam_pos == "top-band":
        cap = caption_pos if caption_pos != "auto" else "seam"
        return {"mode": "gameplay-band",
                "facecam_src_rect": src_rect,
                "detection_method": detection["method"] if detection else None,
                "overlay": None, "band": True, "band_h": rp["band_h"],
                "corner": "top-band",
                "caption_y": _caption_y_for(cap, rp),
                "caption_y_default": rp["cap_y_default"],
                "corner_scores": [],
                "facecam_bigger_than_peakcut": True,
                "note": ("top-band: facecam spans the full width on top, "
                         "gameplay below — Clipie-style layout")}
    # --- facecam off: no overlay at all, captions at the chosen position.
    if facecam_pos == "off":
        cap = caption_pos if caption_pos != "auto" else "bottom"
        return {"mode": "gameplay",
                "facecam_src_rect": None,
                "detection_method": detection["method"] if detection else None,
                "overlay": None, "band": False,
                "corner": "off",
                "caption_y": _caption_y_for(cap, rp),
                "caption_y_default": rp["cap_y_default"],
                "corner_scores": [],
                "facecam_bigger_than_peakcut": True,
                "note": "facecam off — plain game visuals"}
    # candidates: (name, x, y) — bottom corners excluded (platform UI zone)
    cands = [("top-right", OUT_W - FC_W - MARGIN, MARGIN),
             ("top-left", MARGIN, MARGIN),
             ("mid-right", OUT_W - FC_W - MARGIN, 420),
             ("mid-left", MARGIN, 420)]
    # where does the streamer's own facecam appear inside the base crop?
    # base crop = centre 608px of the source (x 656..1264) -> output 720 wide.
    CROP_W = 608
    crop_x0 = (src_w - CROP_W) // 2
    busy = None
    if detection:
        busy = detection.get("busy")

    def rect_overlap(a, b):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        ox = max(0, min(ax + aw, bx + bw) - max(ax, bx))
        oy = max(0, min(ay + ah, by + bh) - max(ay, by))
        return ox * oy

    def busy_score(rect):
        # mean edge density of the grid cells the rect covers (0..1)
        if busy is None:
            return 0.0
        gh, gw = len(busy), len(busy[0])
        x, y, w, h = rect
        # map output rect back to source coords for the grid
        sx = int(x / OUT_W * src_w)
        sy = int(y / OUT_H * src_h)
        sw = int(w / OUT_W * src_w)
        sh = int(h / OUT_H * src_h)
        cells = []
        for r in range(gh):
            for c in range(gw):
                cx0, cy0 = c * src_w // gw, r * src_h // gh
                cx1, cy1 = (c + 1) * src_w // gw, (r + 1) * src_h // gh
                if rect_overlap((sx, sy, sw, sh), (cx0, cy0, cx1 - cx0, cy1 - cy0)) > 0:
                    cells.append(busy[r][c])
        return sum(cells) / len(cells) if cells else 0.0

    # projected facecam rect in output coords (only the part inside the crop)
    # NOTE: do NOT reuse sx/sy here — those are the 720->output scale
    # factors defined above; the src rect's x/y would clobber them
    # (a facecam at x=0 made the forced-corner branch divide by zero).
    proj = None
    if src_rect:
        rx, ry, rw, rh = src_rect
        vx0 = max(rx, crop_x0)
        vx1 = min(rx + rw, crop_x0 + CROP_W)
        if vx1 > vx0:
            proj = ((vx0 - crop_x0) / CROP_W * OUT_W,
                    ry / src_h * OUT_H,
                    (vx1 - vx0) / CROP_W * OUT_W,
                    rh / src_h * OUT_H)

    scored = []
    for name, ox, oy in cands:
        rect = (ox, oy, FC_W, FC_H)
        score = 0.0
        # 1. overlapping the streamer's own facecam in the base crop = bad
        if proj:
            ov = rect_overlap(rect, proj) / (FC_W * FC_H)
            score += ov * 100
        # 2. busy HUD zone = bad
        score += busy_score(rect) * 50
        # 3. top-right preferred on ties (PeakCut-like)
        score += {"top-right": 0, "top-left": 1, "mid-right": 2, "mid-left": 3}[name]
        scored.append((score, name, rect))
    scored.sort(key=lambda s: s[0])
    _score, corner, overlay = scored[0]
    # sohaib's pick overrides the auto choice (Positions page)
    if facecam_pos in ("top-right", "top-left", "mid-right", "mid-left"):
        _kind, fixed = _facecam_rect_for(facecam_pos, rp)
        # _facecam_rect_for returns OUTPUT coords; convert back to 720-space
        # for the caption-Y overlap check below.
        overlay = (int(fixed[0] / sx), int(fixed[1] / sy),
                   int(fixed[2] / sx), int(fixed[3] / sy))
        corner = facecam_pos

    # caption Y: sohaib's pick wins; otherwise first free line (text band
    # must clear overlay + HUD)
    if caption_pos in ("bottom", "seam", "top"):
        cap_y = _caption_y_for(caption_pos, rp)  # already output coords
        cap_y_720 = int(cap_y / sy)
    else:
        cap_y = cap_y_default
        for cand_y in CAP_Y_CANDIDATES:
            band = (0, cand_y + TEXT_TOP, OUT_W, TEXT_BOTTOM - TEXT_TOP)
            if rect_overlap(band, overlay) > 0:
                continue
            if busy_score(band) > 0.35:
                continue
            cap_y = cand_y
            break
        else:
            cap_y = CAP_Y_CANDIDATES[-1]  # 300 — highest free line we have
        cap_y_720 = cap_y
        cap_y = int(cap_y * sy)

    ov_out = _scale_rect_720(overlay)
    return {"mode": "gameplay",
            "facecam_src_rect": src_rect,
            "detection_method": detection["method"] if detection else None,
            "overlay": {"x": ov_out[0], "y": ov_out[1], "w": ov_out[2], "h": ov_out[3]},
            "band": False,
            "corner": corner,
            "caption_y": cap_y,
            "caption_y_default": int(cap_y_default * sy),
            "corner_scores": [(s[1], round(s[0], 1)) for s in scored],
            "facecam_bigger_than_peakcut": True,
            "ratio": ratio, "res": res,
            "note": ("overlay 288x162 = 40% of 720 width; PeakCut's was "
                     "323x186 in 1080x1920 = 30% of width")}


def fallback_layout(ratio="9:16", res=720):
    """No detection deps / no facecam found: safe defaults, no guessing.
    Overlay + caption Y are in OUTPUT coords for the ratio/res."""
    rp = ratio_params(ratio, res)
    _kind, rect = _facecam_rect_for("top-right", rp)
    return {"mode": "gameplay",
            "facecam_src_rect": None,
            "detection_method": None,
            "overlay": {"x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3]},
            "band": False,
            "corner": "top-right",
            "caption_y": rp["cap_y_default"],
            "caption_y_default": rp["cap_y_default"],
            "corner_scores": [],
            "facecam_bigger_than_peakcut": True,
            "ratio": ratio, "res": res,
            "note": "fallback — detection unavailable or no facecam found"}
