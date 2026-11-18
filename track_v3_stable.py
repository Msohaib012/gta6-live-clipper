"""V3 stabilized face tracker — fixes the V2/60fps FLICKER.

Diagnosis (measured on V2 samples + frame at t=22s):
- t=21.5-22.7s: TWO faces in frame (two seated men). V2's "closest to prev
  centre" pick alternates between them (or between MP/Haar boxes of the
  same face) -> crop x0 bounces +-30px sample-to-sample (6 big sign
  reversals in 1.2s). That is the flicker sohaib sees.
- Plus broad micro-jitter: detector box centres wobble a few px every
  sample, and V2's weak smoothing (move half the clamped delta) passes
  that noise straight into the trajectory (residual RMS 5.0px vs 0.5s MA,
  16.4% of 60fps frames >3px off the smooth path).

Fixes (all in this file; V2 files untouched):
1. Face-ID persistence: candidates are matched to persistent face tracks
   (nearest centre within 120px). The tracker follows ONE track id.
2. Hysteresis: a challenger must beat the active track (distance to the
   predicted centre, minus size bonus, minus a 50px stickiness bonus for
   the incumbent) for 3 CONSECUTIVE samples before a switch. If the active
   face vanishes for 5 samples (0.5s), a free switch is allowed.
3. Two-rate EMA: small deltas (<40px, the noise regime) get alpha 0.25
   (heavy smoothing); large deltas (>=40px, real moves / shot cuts) get
   alpha 0.5 so catch-up stays fast. Raw delta still clamped to +-60px.
4. Deadband: the output crop-x does not move for changes under 8px.

Outputs:
  track_v3_x0.json        - (t, x0) 0.1s samples (stabilized)
  track_v3_candidates.json - per-sample raw candidates + chosen track id
  (60Hz sendcmd is built by build_track60_v3.py with system python/scipy)
"""
import argparse
import json
import os
import time

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

_ap = argparse.ArgumentParser(description="V3 stabilized face tracker (facecam/doc modes).")
_ap.add_argument("--src", required=True, help="input video (clean, timestamp-sane clip)")
_ap.add_argument("--out-json", required=True, help="output (t, x0) 0.1s samples JSON")
_ap.add_argument("--out-cand", required=True, help="output candidates/switch log JSON")
_ap.add_argument("--crop-w", type=int, default=608, help="vertical crop width (default 608 for 1080p game source)")
_ap.add_argument("--src-w", type=int, default=1920, help="source frame width (default 1920)")
_ap.add_argument("--model", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "models", "blaze_face_full_range.tflite"))
_args = _ap.parse_args()

SRC = _args.src
OUT_JSON = _args.out_json
OUT_CAND = _args.out_cand
CROP_W = _args.crop_w
SRC_W = _args.src_w
MODEL_PATH = _args.model
SAMPLE_EVERY = 3          # every 3rd frame = 0.1s at 30fps
MP_CONF = 0.4
MATCH_GATE = 120          # px: candidate-to-track matching radius
STICKY_BONUS = 50         # px-equivalent score bonus for the incumbent track
SWITCH_CONFIRM = 3        # consecutive wins a challenger needs to take over
ACTIVE_MISS_TOLERANCE = 5  # samples without the active face -> free switch
RAW_CLAMP = 60            # px clamp on raw jump before EMA
ALPHA_SMALL = 0.25        # EMA weight for small deltas (noise regime)
ALPHA_LARGE = 0.5         # EMA weight for large deltas (real moves / cuts)
ALPHA_CUTOFF = 40         # px: delta below this uses ALPHA_SMALL
DEADBAND = 8              # px: output x0 moves only if change >= this

base = vision.FaceDetectorOptions(
    base_options=mp_python.BaseOptions(model_asset_path=MODEL_PATH),
    min_detection_confidence=MP_CONF)
mp_det = vision.FaceDetector.create_from_options(base)
cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

cap = cv2.VideoCapture(SRC)
fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
print(f"frames={n_frames} fps={fps:.2f} duration={n_frames/fps:.2f}s")

# ---- face track state ----
tracks = {}               # id -> {cx, cy, w, last_seen}
next_id = 0
active_id = None
challenger_id = None
challenger_wins = 0
active_miss = 0

smooth_cx = SRC_W // 2
smooth_cy = 360
prev_x0 = max(0, min(SRC_W - CROP_W, smooth_cx - CROP_W // 2))
x0_series = []
cand_log = []
switch_log = []
stats = {"mp": 0, "haar": 0, "none": 0, "sampled": 0, "switches": 0}
t0 = time.time()
i = 0
while True:
    ret, frame = cap.read()
    if not ret:
        break
    if i % SAMPLE_EVERY != 0:
        i += 1
        continue
    i += 1
    t = (i - 1) / fps
    stats["sampled"] += 1
    cands = []  # (cx, cy, w, score, src)
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    res = mp_det.detect(img)
    for d in res.detections:
        bb = d.bounding_box
        cands.append((bb.origin_x + bb.width // 2,
                      bb.origin_y + bb.height // 2,
                      bb.width, float(d.categories[0].score), "mp"))
    if not cands:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1,
                                         minNeighbors=5, minSize=(40, 40))
        for (x, y, w, h) in faces:
            cands.append((x + w // 2, y + h // 2, w, 0.5, "haar"))
    for c in cands:
        stats[c[4]] += 1

    # --- match candidates to persistent tracks (nearest centre, gated) ---
    matched = {}  # track_id -> cand index
    cand_track = [-1] * len(cands)
    used_tracks = set()
    for ci, c in enumerate(cands):
        best_id, best_d = None, MATCH_GATE
        for tid, tr in tracks.items():
            if tid in used_tracks:
                continue
            d = abs(c[0] - tr["cx"]) + abs(c[1] - tr["cy"]) * 0.5
            if d < best_d:
                best_id, best_d = tid, d
        if best_id is not None:
            matched[best_id] = ci
            cand_track[ci] = best_id
            used_tracks.add(best_id)
    for tid, ci in matched.items():
        c = cands[ci]
        tracks[tid].update(cx=c[0], cy=c[1], w=c[2], last_seen=stats["sampled"])
    for ci, c in enumerate(cands):
        if cand_track[ci] == -1:
            tracks[next_id] = {"cx": c[0], "cy": c[1], "w": c[2],
                               "last_seen": stats["sampled"]}
            cand_track[ci] = next_id
            next_id += 1
    # prune tracks unseen for > 30 samples (3s)
    tracks = {tid: tr for tid, tr in tracks.items()
              if stats["sampled"] - tr["last_seen"] <= 30}

    # --- choose the active track (with hysteresis) ---
    def track_score(tid):
        """Lower is better: distance to predicted centre minus size bonus.
        The incumbent gets an extra stickiness bonus."""
        tr = tracks[tid]
        dist = abs(tr["cx"] - smooth_cx) + abs(tr["cy"] - smooth_cy) * 0.5
        bonus = 0.3 * tr["w"] + (STICKY_BONUS if tid == active_id else 0)
        return dist - bonus

    if active_id is None and tracks:
        active_id = min(tracks, key=track_score)
        print(f"t={t:.1f} initial active face id={active_id}")

    raw = None  # (cx, cy) target for this sample
    if active_id is not None and active_id in matched:
        active_miss = 0
        challenger_id, challenger_wins = None, 0
        tr = tracks[active_id]
        raw = (tr["cx"], tr["cy"])
    elif tracks:
        # active face not seen this sample: look for a challenger that was
        # actually DETECTED this sample (stale tracks don't count wins)
        live = [tid for tid in matched]
        if live:
            best = min(live, key=track_score)
            if best == challenger_id:
                challenger_wins += 1
            else:
                challenger_id, challenger_wins = best, 1
            active_miss += 1
            if challenger_wins >= SWITCH_CONFIRM or active_miss >= ACTIVE_MISS_TOLERANCE:
                old = active_id
                active_id = best
                challenger_id, challenger_wins = None, 0
                active_miss = 0
                stats["switches"] += 1
                switch_log.append((round(t, 2), old, best))
                print(f"t={t:.1f} SWITCH face {old} -> {best}")
                tr = tracks[active_id]
                raw = (tr["cx"], tr["cy"])
        else:
            active_miss += 1
    else:
        stats["none"] += 1

    # --- smooth the target (two-rate EMA + deadband on output) ---
    if raw is not None:
        dx = max(-RAW_CLAMP, min(RAW_CLAMP, raw[0] - smooth_cx))
        alpha = ALPHA_SMALL if abs(dx) < ALPHA_CUTOFF else ALPHA_LARGE
        smooth_cx = smooth_cx + alpha * dx
        dy = max(-RAW_CLAMP, min(RAW_CLAMP, raw[1] - smooth_cy))
        smooth_cy = smooth_cy + (ALPHA_SMALL if abs(dy) < ALPHA_CUTOFF else ALPHA_LARGE) * dy
    x0 = max(0, min(SRC_W - CROP_W, int(round(smooth_cx)) - CROP_W // 2))
    if abs(x0 - prev_x0) < DEADBAND:
        x0 = prev_x0
    prev_x0 = x0
    x0_series.append((round(t, 3), x0))
    cand_log.append({
        "t": round(t, 3),
        "cands": [[int(c[0]), int(c[1]), int(c[2]), round(float(c[3]), 2), c[4], int(cand_track[ci])]
                  for ci, c in enumerate(cands)],
        "active": int(active_id) if active_id is not None else None,
        "raw": [int(raw[0]), int(raw[1])] if raw else None,
        "smooth_cx": round(float(smooth_cx), 1),
        "x0": int(x0),
    })

cap.release()
print(f"tracking time={time.time()-t0:.1f}s stats={stats}")
print(f"switches: {switch_log}")
print(f"x0 range={min(x for _, x in x0_series)}..{max(x for _, x in x0_series)} "
      f"mean={sum(x for _, x in x0_series)/len(x0_series):.0f}")
json.dump(x0_series, open(OUT_JSON, "w"))
json.dump({"switches": switch_log, "samples": cand_log}, open(OUT_CAND, "w"))
print(f"wrote {OUT_JSON} ({len(x0_series)} samples) and {OUT_CAND}")
