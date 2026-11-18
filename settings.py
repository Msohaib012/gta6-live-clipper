"""Settings + favorites rotation (GTA 6 Live Clipper).

sohaib's workflow (2026-11-17 23:38 PKT): on the Styles page he marks
styles as FAVORITES, in an order he chooses. The auto-clip loop then
ROTATES through his favorites one by one — clip 1 uses favorite 1,
clip 2 uses favorite 2, and so on, cycling. Manual "Clip now" can still
pick one style explicitly.

settings.json (in the project folder) holds:
  favorites: ordered list of style ids (e.g. ["neon", "hype", "hormozi"])
  next_index: where the rotation currently is (persisted, survives restarts)
  gemini_key: optional FREE Gemini API key (for smarter AI picking;
              never hard-coded — the user pastes it in the dashboard
              Settings card; it is stored in config.json instead if that
              file exists, same pattern as transliterate.py)
  youtube_api_key: optional (YouTube live chat needs it — v2)

If no favorites are set, the loop uses the single default style ("neon").
"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE, "settings.json")
DEFAULT_STYLE = "neon"


def load_settings():
    try:
        d = json.load(open(SETTINGS_PATH))
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def save_settings(d):
    tmp = SETTINGS_PATH + ".tmp"
    json.dump(d, open(tmp, "w"), indent=1, ensure_ascii=False)
    os.replace(tmp, SETTINGS_PATH)
    return d


def get_favorites():
    """Ordered favorite style ids (empty = none set yet)."""
    favs = load_settings().get("favorites") or []
    return [str(f) for f in favs if isinstance(f, str)]


def set_favorites(favorites):
    """Replace the favorites list (dashboard Styles page). Resets rotation."""
    d = load_settings()
    d["favorites"] = [str(f) for f in favorites]
    d["next_index"] = 0
    return save_settings(d)


def next_favorite_style(default=DEFAULT_STYLE):
    """Next style in the rotation; advances + persists the index.
    Returns (style, position, total)."""
    d = load_settings()
    favs = [str(f) for f in (d.get("favorites") or []) if isinstance(f, str)]
    if not favs:
        return default, 0, 0
    idx = int(d.get("next_index") or 0) % len(favs)
    style = favs[idx]
    d["next_index"] = (idx + 1) % len(favs)
    save_settings(d)
    return style, idx + 1, len(favs)


def peek_next_favorite(default=DEFAULT_STYLE):
    """Which style the NEXT auto clip will use (without advancing)."""
    d = load_settings()
    favs = [str(f) for f in (d.get("favorites") or []) if isinstance(f, str)]
    if not favs:
        return default, 0, 0
    idx = int(d.get("next_index") or 0) % len(favs)
    return favs[idx], idx + 1, len(favs)


def get_positions():
    """sohaib's picks from the Positions page (persisted in settings.json):
    facecam_pos, caption_pos, ratio. Defaults = auto / auto / 9:16."""
    d = load_settings()
    return {"facecam_pos": d.get("facecam_pos", "auto"),
            "caption_pos": d.get("caption_pos", "auto"),
            "ratio": d.get("ratio", "9:16")}


def set_positions(facecam_pos=None, caption_pos=None, ratio=None):
    """Save position/ratio picks (validated against positions.py lists)."""
    from positions import CAPTION_POSITIONS, FACECAM_POSITIONS, RATIOS
    d = load_settings()
    if facecam_pos is not None and facecam_pos in FACECAM_POSITIONS:
        d["facecam_pos"] = facecam_pos
    if caption_pos is not None and caption_pos in CAPTION_POSITIONS:
        d["caption_pos"] = caption_pos
    if ratio is not None and ratio in RATIOS:
        d["ratio"] = ratio
    return save_settings(d)


def set_gemini_key(key):
    """Store the user's FREE Gemini key (Settings card). Kept in
    config.json (same file transliterate.py reads) when it exists,
    otherwise in settings.json. Never printed, never committed."""
    key = (key or "").strip()
    cfg_path = os.path.join(BASE, "config.json")
    try:
        cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
    except Exception:
        cfg = {}
    cfg["gemini_api_key"] = key
    json.dump(cfg, open(cfg_path, "w"), indent=1)
    return bool(key)


def get_gemini_key():
    """Read the key the same way transliterate.py does (config.json first)."""
    try:
        from transliterate import load_gemini_key
        return load_gemini_key()
    except Exception:
        return None
