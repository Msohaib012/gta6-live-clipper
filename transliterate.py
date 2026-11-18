"""Transliteration for multilingual captions (GTA 6 Live Clipper).

Problem (sohaib, 2026-11-17): PeakCut's live clip is the quality/caption/
facecam target, BUT it only detects English (its Parakeet model is
English-only) while the streamer speaks Roman Urdu / Hindi. Captions must
be in ROMAN (Latin) script when the speech is Hindi/Urdu (Hinglish), and
in each other language's own script otherwise.

Pipeline:
  Whisper (faster-whisper, auto-detect) transcribes Hindi as Devanagari
  and Urdu as Nastaliq. This module converts those words to Roman:
  - Devanagari -> Roman: indic-transliteration (ITRANS) + a Hinglish
    normalizer (long vowels to lower case, anusvara -> n, word-final
    schwa dropped) + a small overrides dict for the common words where
    the mechanical output is visibly off (ajad -> azad, skul -> school).
  - Urdu (Nastaliq) -> Roman: a rule-based letter map + the same
    overrides dict (local libs have no usable Urdu scheme on this stack:
    indic-transliteration has no Urdu scheme; aksharamukha 2.3's database
    has no Urdu->Latin entry — KeyError 'Urdu' — so it cannot do it
    either). Approximate but readable; the Gemini path below gives
    natural Roman Urdu when a key is set.
  - Gemini path (optional, key from config.json Settings — NEVER
    hard-coded, same pattern as the YT Clip Tool): natural Hinglish /
    Roman Urdu spelling. Used only when the user sets
    roman_mode="gemini" in config.json AND a key is present.

Honest limits (measured 2026-11-17, see BUILD-REPORT):
- Local Devanagari->Roman is ~good: proper nouns and common words come
  out right; internal schwa is NOT deleted (karana stays karana, natural
  is karna) except via the overrides dict; loanwords need overrides.
- Local Urdu->Roman is approximate (unwritten vowels are guessed);
  common words are fixed by overrides, rare words may read oddly.
- Gemini transliteration is natural (azad, karna, school, pakistan) but
  needs the user's own free Gemini key in Settings.
"""

import json
import os
import re

# ---------------------------------------------------------------------------
# script detection
# ---------------------------------------------------------------------------

_DEV_RE = re.compile(r"[\u0900-\u097F]")
_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


def script_of(text):
    """'devanagari' | 'urdu' (Arabic script) | 'latin'."""
    if _DEV_RE.search(text):
        return "devanagari"
    if _ARABIC_RE.search(text):
        return "urdu"
    return "latin"


# ---------------------------------------------------------------------------
# Devanagari -> Roman (indic-transliteration ITRANS + Hinglish normalize)
# ---------------------------------------------------------------------------

# Mechanical ITRANS output -> natural Hinglish, for the words where the
# mechanical form is visibly wrong. Extend freely; keys are the normalized
# form, values the natural spelling.
ROMAN_OVERRIDES = {
    "ajad": "azad",
    "skul": "school",
    "karana": "karna",
    "bolana": "bolna",
    "dekhana": "dekhna",
    "sunana": "sunna",
    "milana": "milna",
    "khelana": "khelna",
    "chalana": "chalna",
    "karane": "karne",
    "bolane": "bolne",
    "dekhane": "dekhne",
    "sunane": "sunne",
    "milane": "milne",
    "jane": "jaane",
    "ane": "aane",
    "khane": "khana",
    "pine": "peene",
    "pakstan": "pakistan",
    "mjhe": "mujhe",
    "mire": "mere",
    "tmhara": "tumhara",
    "nhin": "nahin",
    "min": "mein",
    "kia": "kya",
    "krna": "karna",
    "pina": "peena",
    "jina": "jeena",
    "men": "mein",        # में — "men" reads as the English word
    "gae": "gaye",         # गए — natural Hinglish "gaye"
    # English loanwords — natural Roman spelling, not phonetic ITRANS
    "soshalist": "socialist",
    "ripablikan": "republican",
    "esosieshan": "association",
    "pulis": "police",
    "phayaring": "firing",
    "alphred": "alfred",
    "pharavari": "farvari",
    "echaesaarae": "HSRA",  # एचएसआरए — acronym, not a word
    "echaarae": "HRA",      # एचआरए — acronym, not a word
    "sinh": "singh",        # सिंह — natural Hinglish "singh", not "sinh"
    "kamared": "comrade",   # कॉमरेड — English loanword
    "aichsare": "HSRA",     # small-model mishearing of एचएसआरए
    "echshvare": "HSRA",    # small-model mishearing of एचएसआरए
    "echsare": "HSRA",      # small-model mishearing of एचएसआरए
    "netritv": "netritva",  # नेतृत्व — natural Hinglish keeps the final 'a'
    # natural Hinglish spellings (ITRANS phonetics read oddly)
    "unaki": "unki",
    "apane": "apne",
    "tin": "teen",
    "nind": "neend",
    "donon": "dono",
    "charon": "charo",
    "taraph": "taraf",
    "akhirakar": "aakhirkar",
    "unhen": "unhein",
    "valon": "walon",
    "die": "diye",         # दिए — ITRANS drops the 'y'
    "chupi": "chhupi",     # छुपी — ITRANS drops the 'h'
    "naheen": "nahin",     # नहीं — step-4 long-vowel fold gives "naheen"
    # ClipKontrast-fallback spellings (the ck engine spells these
    # loanwords differently from ITRANS — both keys stay, each path
    # fires its own): skool/komred/soshlist/phaayring/faravri/azaad/
    # pakistaan are what _ck_dev_word produces.
    "skool": "school",
    "komred": "comrade",
    "soshlist": "socialist",
    "phaayring": "firing",
    "faravri": "farvari",
    "azaad": "azad",
    "pakistaan": "pakistan",
}

# Urdu-only overrides (applied only when the source word is Nastaliq, so
# "he" -> "hai" never touches an English or Hindi word).
URDU_OVERRIDES = {
    "mjhe": "mujhe",
    "mire": "mere",
    "tmhara": "tumhara",
    "tmhari": "tumhari",
    "nhin": "nahin",
    "he": "hai",
    "pakstan": "pakistan",
    "dost": "dost",
    "kya": "kya",
    "min": "mein",
    "kia": "kya",
    "krna": "karna",
    "mlna": "milna",
    "sanna": "sunna",
    "kha": "kaha",
    "dikhna": "dekhna",
    "hndostan": "hindustan",
    "bnane": "banane",
    "nbhai": "nibhai",
}


_VOWEL_CHARS = set("aeiouAEIOU")


def _drop_medial_schwa(w):
    """Drop a medial short 'a' per ClipKontrast's right-to-left rule
    (roman-string form, applied BEFORE the A/I/U fold so long vowels
    are still visible as capitals).

    Only a lowercase short 'a' is a candidate (long 'A' is a real
    vowel); the word's first vowel never drops; the 'a' drops only
    when the previous letter is a consonant, the next is a consonant,
    and the letter after that is a vowel. karana -> karna, samajhana
    -> samajhna — but kahan stays kahan (the final-drop above already
    removed its last vowel, so the 'a' after 'h' sees no vowel after
    the 'n') and hamara stays hamara (its vowels are long). Right to
    left, so a drop on the right does not license a wrong drop on
    the left (samajhana: the 'a' after 'h' drops, the 'a' after 'j'
    then sees a consonant and stays)."""
    chars = list(w)
    first_vowel = next((i for i, c in enumerate(chars)
                        if c in _VOWEL_CHARS), None)
    if first_vowel is None:
        return w
    for i in range(len(chars) - 1, first_vowel, -1):
        if chars[i] != "a":
            continue
        prev = chars[i - 1] if i > 0 else ""
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        after = chars[i + 2] if i + 2 < len(chars) else ""
        if (prev.isalpha() and prev not in _VOWEL_CHARS
                and nxt.isalpha() and nxt not in _VOWEL_CHARS
                and after in _VOWEL_CHARS):
            chars[i] = ""
    return "".join(chars)


def _shorten_long_vowels(w):
    """'aa'->'a', 'ee'->'i', 'oo'->'u' when the next consonant carries a
    vowel or at word end (ClipKontrast's rule): baat, kaam, aaj and
    teen keep the double; hamara, pata, ki and tu go single."""
    out = []
    i = 0
    short = {"aa": "a", "ee": "i", "oo": "u"}
    while i < len(w):
        two = w[i:i + 2]
        if two in short:
            j = i + 2
            nxt = w[j] if j < len(w) else ""
            after = w[j + 1] if j + 1 < len(w) else ""
            # shorten at word end, or when the following consonant
            # carries a vowel (after is a real vowel letter — note
            # "" in "aeiou" is True in Python, so test it explicitly).
            if nxt == "" or (after != "" and after in "aeiou"):
                out.append(short[two])
                i += 2
                continue
            out.append(two)
            i += 2
            continue
        out.append(w[i])
        i += 1
    return "".join(out)


def _hinglish_normalize(itrans_word):
    """ITRANS -> natural Hinglish spelling (lowercase, no tech capitals)."""
    w = itrans_word
    # two-letter sequences first (Th/Dh/Sh/Ng/JN are single sounds)
    for src, dst in (("Th", "th"), ("Dh", "dh"), ("Sh", "sh"), ("Ch", "ch"),
                     ("Ng", "ng"), ("JN", "gy"), ("RRi", "ri"), ("RR", "ri"),
                     (".D", "D"), (".d", "d"), (".T", "T"), (".t", "t")):
        w = w.replace(src, dst)
    # a word-final LONG 'A' (आ) is a real vowel — keep it (kaha, jana).
    # Only a word-final SHORT 'a' (अ, the inherent schwa) is dropped.
    ends_long_a = w.endswith("A")
    w = w.replace("M", "n").replace("~", "n")   # anusvara / chandrabindu -> n
    # word-final schwa: pulisa -> pulis, milane -> milne, parka -> park
    if not ends_long_a and len(w) > 2 and w.endswith("a"):
        w = w[:-1]
    # medial schwa (see _drop_medial_schwa) — before the fold below,
    # so long vowels are still capitals and visible to the rule.
    w = _drop_medial_schwa(w)
    # long vowels doubled first (A->aa, I->ee, U->oo), then shortened
    # where people type them single (see _shorten_long_vowels).
    w = (w.replace("A", "aa").replace("I", "ee").replace("U", "oo")
          .replace("R", "ri").replace("L", "l")
          .replace("T", "t").replace("D", "d").replace("N", "n")
          .replace("G", "gh").replace("K", "q").replace("Z", "z")
          .replace("F", "f").replace("X", "ksh").replace("Y", "y"))
    return _shorten_long_vowels(w)


def devanagari_to_roman(text):
    """Devanagari string -> Roman (Latin) string, word by word.

    Common words come from the ClipKontrast table first (correct schwa
    + long vowels, no library needed); the rest go through
    indic-transliteration (ITRANS) when installed, else the vendored
    ClipKontrast engine (_ck_dev_word, stdlib only) — so a machine
    without indic-transliteration NO LONGER falls back to Devanagari
    captions (measured 2026-11-18: the lib is installed nowhere on
    the build VM). ROMAN_OVERRIDES still win last (loanwords, Whisper
    mishearings, acronyms)."""
    text = text.replace("।", ".").replace("॥", ".")
    try:
        from indic_transliteration import sanscript
        from indic_transliteration.sanscript import transliterate
        have_itrans = True
    except ImportError:
        have_itrans = False
    out = []
    for tok in re.split(r"(\s+)", text):
        if not tok.strip():
            out.append(tok)
            continue
        m = re.match(r"^([^\w\u0900-\u097F]*)([\w\u0900-\u097F]+)([^\w\u0900-\u097F]*)$",
                     tok, re.UNICODE)
        if not m:
            out.append(tok)
            continue
        pre, core, post = m.groups()
        if _DEV_RE.search(core):
            if core in _CK_DEV_WORDS:
                rom = _CK_DEV_WORDS[core]
            elif have_itrans:
                it = transliterate(core, sanscript.DEVANAGARI, sanscript.ITRANS)
                rom = _hinglish_normalize(it)
                # defensive: no Devanagari glyph may survive into a Roman caption
                # (ITRANS leaves the ऑ vowel sign ॉ unconverted in कॉमरेड) —
                # strip leftovers BEFORE the override lookup, or keys never match.
                rom = re.sub(r"[\u0900-\u097F]", "", rom)
            else:
                rom = _ck_dev_word(core)
            rom = ROMAN_OVERRIDES.get(rom, rom)
            out.append(pre + rom + post)
        else:
            out.append(tok)
    return "".join(out)


# ---------------------------------------------------------------------------
# Urdu (Nastaliq) -> Roman, rule-based (no usable local lib on this stack)
# ---------------------------------------------------------------------------

def _urdu_word_to_roman(core):
    # ClipKontrast's engine (vendored below): a 95-word table for the
    # words whose unwritten vowels no rule can guess, then letter rules
    # with the correct a-insertion (after the first letter, or before
    # the consonant at i==2 when w[1] is do-chashmi he: ghar, not gahr).
    # Measured 2026-11-18: ours was wrong on ~30/57 common words, ck
    # right on all but one (gaye, fixed in the table below).
    rom = _ck_urdu_word(core)
    rom = URDU_OVERRIDES.get(rom, ROMAN_OVERRIDES.get(rom, rom))
    return rom


def urdu_to_roman(text):
    """Nastaliq string -> Roman (Latin) string, word by word. Approximate:
    unwritten short vowels are guessed; common words are fixed by the
    overrides dict above. Gemini (with the user's key) is the accurate
    path — see gemini_transliterate()."""
    text = (text.replace("،", ",").replace("۔", ".")
                .replace("؟", "?").replace("؛", ";"))
    out = []
    for tok in re.split(r"(\s+)", text):
        if not tok.strip():
            out.append(tok)
            continue
        m = re.match(r"^([^\w\u0600-\u06FF]*)([\w\u0600-\u06FF]+)([^\w\u0600-\u06FF]*)$",
                     tok, re.UNICODE)
        if not m:
            out.append(tok)
            continue
        pre, core, post = m.groups()
        if _ARABIC_RE.search(core):
            out.append(pre + _urdu_word_to_roman(core) + post)
        else:
            out.append(tok)
    return "".join(out)


# ---------------------------------------------------------------------------
# ClipKontrast roman engine (vendored, stdlib only)
# ---------------------------------------------------------------------------
# From anistroz/ClipKontrast studio/roman.py (MIT licence), renamed with
# a _ck_ prefix. Used as (a) the word-table + fallback for Devanagari
# when indic-transliteration is missing, (b) the Urdu engine, and
# (c) the romanizer for scripts the caption fonts cannot draw
# (Cyrillic, Greek, Korean). Two corrections vs upstream, measured
# 2026-11-18: "मैं" -> "mein" (not "main"), "नहीं" -> "nahin" (not
# "nahi"), "میں" -> "mein" (not "main"), and "گئے" -> "gaye"
# (upstream gives the wrong "geee").

_CK_DEV_WORDS = {"में": "mein", "मैं": "mein", "हैं": "hain", "है": "hai", "नहीं": "nahin", "नही": "nahi", "हूँ": "hoon",
                 "हूं": "hoon", "क्या": "kya", "क्यों": "kyun", "क्यूँ": "kyun", "यह": "yeh", "वह": "woh", "और": "aur",
                 "भी": "bhi", "ही": "hi", "की": "ki", "जी": "ji", "तो": "to", "हाँ": "haan", "हां": "haan", "यहाँ": "yahan",
                 "यहां": "yahan", "वहाँ": "wahan", "वहां": "wahan", "कहाँ": "kahan", "कहां": "kahan", "थी": "thi",
                 "रही": "rahi", "कभी": "kabhi", "अभी": "abhi", "सभी": "sabhi", "भाई": "bhai", "मुझे": "mujhe",
                 "तुम": "tum", "हम": "hum", "आप": "aap", "बहुत": "bahut", "अच्छा": "accha", "ठीक": "theek", "लिए": "liye"}
_CK_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai",
              "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_CK_SIGNS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o",
             "ौ": "au", "ॉ": "o", "ॅ": "e"}
_CK_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
            "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh",
            "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v",
            "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
            "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "फ़": "f", "ड़": "r", "ढ़": "rh", "य़": "y"}
_CK_NUKTA = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "फ": "f", "ड": "r", "ढ": "rh", "य": "y"}
_CK_VIRAMA, _CK_NUKTA_SIGN = "्", "़"
_CK_NASAL = {"ं": "n", "ँ": "n"}
_CK_OTHER = {"ः": "h", "।": ".", "॥": ".", "ॐ": "om", **{chr(0x0966 + i): str(i) for i in range(10)}}


def _ck_dev_word(w: str) -> str:
    if w in _CK_DEV_WORDS:
        return _CK_DEV_WORDS[w]
    # One unit per letter: [roman consonant or vowel, its vowel or None (None = a consonant with no vowel), is_cons]
    units, i = [], 0
    while i < len(w):
        ch = w[i]
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if ch in _CK_CONS:
            c = _CK_CONS[ch]
            if nxt == _CK_NUKTA_SIGN:
                c = _CK_NUKTA.get(ch, c)
                i += 1
                nxt = w[i + 1] if i + 1 < len(w) else ""
            if nxt == _CK_VIRAMA:
                units.append([c, None, True, False])
                i += 2
                continue
            if nxt in _CK_SIGNS:
                units.append([c, _CK_SIGNS[nxt], True, False])
                i += 2
                continue
            units.append([c, "a", True, True])        # the vowel every Hindi letter carries unless told otherwise
        elif ch in _CK_VOWELS:
            units.append(["", _CK_VOWELS[ch], False, False])
        elif ch in _CK_NASAL:
            if units:
                units[-1][1] = (units[-1][1] or "") + "n"
                units[-1][3] = False
                units[-1].append("nasal")
            else:
                units.append(["n", None, True, False])
        elif ch in _CK_OTHER:
            units.append([_CK_OTHER[ch], None, False, False])
        elif ch == _CK_NUKTA_SIGN or ch == _CK_VIRAMA:
            pass
        else:
            units.append([ch, None, False, False])
        i += 1
    letters = [u for u in units if u[2] or u[1]]
    # Hindi drops the carried "a" at the end of a word, and between a vowel and another vowel-carrying letter
    # (कहना -> kahna, not kahana). Looked at right to left, as speakers do.
    if len(letters) > 1 and letters[-1][3]:
        letters[-1][1] = None
    for k in range(len(letters) - 2, 0, -1):
        u, before, after = letters[k], letters[k - 1], letters[k + 1]
        if u[3] and before[1] and after[2] and after[1] and "nasal" not in before:
            u[1] = None
    # Long vowels the way people type them: doubled only before a consonant that ends a syllable (baat, kaam,
    # aaj), single elsewhere (hamara, pata, ki, tu).
    short = {"aa": "a", "ee": "i", "oo": "u"}
    for k, u in enumerate(letters):
        if u[1] in short:
            after = letters[k + 1] if k + 1 < len(letters) else None
            if after is None or after[1]:
                u[1] = short[u[1]]
    return "".join(u[0] + (u[1] or "") for u in units)


_CK_UR_WORDS = {"ہے": "hai", "ہیں": "hain", "ہوں": "hoon", "ہو": "ho", "ہی": "hi", "یہ": "ye", "وہ": "wo", "کیا": "kya",
                "کیوں": "kyun", "نہیں": "nahi", "نہ": "na", "میں": "mein", "میرا": "mera", "میری": "meri", "میرے": "mere",
                "تو": "to", "کہ": "ke", "کی": "ki", "کا": "ka", "کے": "ke", "کو": "ko", "سے": "se", "پر": "par",
                "اور": "aur", "بھی": "bhi", "تھا": "tha", "تھی": "thi", "تھے": "the", "گیا": "gaya", "گئی": "gayi", "گئے": "gaye",
                "رہا": "raha", "رہی": "rahi", "رہے": "rahe", "بہت": "bahut", "اچھا": "achha", "یار": "yaar", "بھائی": "bhai",
                "آپ": "aap", "ہم": "hum", "تم": "tum", "مجھے": "mujhe", "تمہیں": "tumhein", "اب": "ab", "کر": "kar",
                "کرو": "karo", "کرنا": "karna", "دیکھو": "dekho", "چلو": "chalo", "ابھی": "abhi", "کبھی": "kabhi",
                "جی": "ji", "ہاں": "haan", "نا": "na", "اللہ": "Allah", "ٹھیک": "theek", "پتا": "pata", "پتہ": "pata",
                "لیکن": "lekin", "پھر": "phir", "سب": "sab", "کچھ": "kuch", "کون": "kaun", "کہاں": "kahan", "یہاں": "yahan",
                "وہاں": "wahan", "ایک": "ek", "دو": "do", "اپنا": "apna", "اپنی": "apni", "لوگ": "log", "بات": "baat",
                "کام": "kaam", "دن": "din", "آج": "aaj", "کل": "kal", "بس": "bas", "شکریہ": "shukriya", "واہ": "wah",
                "گھر": "ghar", "ہوا": "hua", "ہوئی": "hui", "ہوئے": "hue", "اس": "us", "ان": "un", "جو": "jo", "جب": "jab",
                "تب": "tab", "مگر": "magar", "پاکستان": "Pakistan", "زندگی": "zindagi", "پیسے": "paise", "روپے": "rupay"}
_CK_UR_CONS = {"ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "ch", "ح": "h", "خ": "kh", "د": "d",
               "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s", "ض": "z",
               "ط": "t", "ظ": "z", "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "ك": "k", "گ": "g", "ل": "l", "م": "m",
               "ن": "n", "ہ": "h", "ه": "h", "ة": "a", "ھ": "h"}
_CK_UR_MARKS = {"َ": "a", "ِ": "i", "ُ": "u", "ّ": "", "ْ": "", "ً": "an", "ٰ": "a"}
_CK_UR_DIGITS = {**{chr(0x06F0 + i): str(i) for i in range(10)}, **{chr(0x0660 + i): str(i) for i in range(10)}}


def _ck_urdu_word(w: str) -> str:
    if w in _CK_UR_WORDS:
        return _CK_UR_WORDS[w]
    out, prev_cons = [], False
    for i, ch in enumerate(w):
        first, last = i == 0, i == len(w) - 1
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if ch in _CK_UR_MARKS:
            out.append(_CK_UR_MARKS[ch])
            prev_cons = False
            continue
        if ch in _CK_UR_DIGITS:
            out.append(_CK_UR_DIGITS[ch])
            prev_cons = False
            continue
        if ch == "ھ":                     # do chashmi he: the "h" of bh, ph, kh, chh...
            out.append("h")
            continue
        if ch in "اآ":
            out.append("aa" if ch == "آ" else "a")
            prev_cons = False
            continue
        if ch in "وؤ":
            if first or not prev_cons:
                out.append("w" if nxt not in "اآ" else "w")
                prev_cons = True
            else:
                out.append("o")
                prev_cons = False
            continue
        if ch in "یيئى":
            if first:
                out.append("y")
                prev_cons = True
            else:
                out.append("i" if last else "ee" if prev_cons and nxt not in "اآوؤ" else "y")
                prev_cons = out[-1] == "y"
            continue
        if ch == "ے":
            out.append("e")
            prev_cons = False
            continue
        if ch in "ںن" and ch == "ں":
            out.append("n")
            continue
        if ch in "عء":
            continue
        c = _CK_UR_CONS.get(ch)
        if c is None:
            out.append(ch)
            prev_cons = False
            continue
        if ch in "ہه" and last and prev_cons:
            out.append("a")               # a final ہ after a consonant is a vowel: کہ, بچہ -> bacha
            prev_cons = False
            continue
        if prev_cons and i == 1 or (prev_cons and i == 2 and w[1] == "ھ"):
            out.append("a")               # the short "a" Urdu does not write after a word's first letter: گھر -> ghar
        out.append(c)
        prev_cons = True
    s = "".join(out)
    if re.fullmatch(r"[a-z]{2,}", s) and not re.search(r"[aeiou]", s):
        s = s[:-1] + "a" + s[-1]          # no vowel at all: give it the one Urdu leaves out
    return s


# ---------------------------------------------------------------- Russian-style (Cyrillic), Greek and Korean letters
_CK_MORE_SCRIPTS = re.compile(r"[\u0370-\u03FF\u0400-\u04FF\uAC00-\uD7A3]")
_CK_CYR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y",
           "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
           "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
           "я": "ya", "є": "ye", "і": "i", "ї": "yi", "ґ": "g", "ў": "u", "ђ": "dj", "ј": "j", "љ": "lj", "њ": "nj",
           "ћ": "c", "џ": "dz", "ә": "a", "ғ": "g", "қ": "q", "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h"}
_CK_GRK = {"α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th", "ι": "i", "κ": "k", "λ": "l",
           "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "φ": "f",
           "χ": "ch", "ψ": "ps", "ω": "o"}
_CK_K_FIRST = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_CK_K_VOWEL = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu",
               "eu", "ui", "i"]
_CK_K_LAST = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p", "t", "t", "ng",
              "t", "t", "k", "t", "p", "t"]
_CK_DEV_RE = re.compile(r"[ऀ-ॿ]")
_CK_OTHER_SCRIPT = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")   # Urdu / Arabic letters


def ck_needs_roman(text: str) -> bool:
    """Is this in a script that captions should show in Roman letters instead (Hindi, Urdu, Arabic, Russian-style,
    Greek or Korean letters)?"""
    text = text or ""
    return bool(_CK_DEV_RE.search(text) or _CK_OTHER_SCRIPT.search(text) or _CK_MORE_SCRIPTS.search(text))


def ck_drawable(text: str) -> bool:
    """Can the caption fonts draw this? They have Latin letters (with accents), digits and punctuation only."""
    return not any(ch.isalpha() and not (ord(ch) < 0x250 or 0x1E00 <= ord(ch) <= 0x1EFF) for ch in text or "")


def _ck_letters(run: str) -> str:
    import unicodedata
    out = []
    for ch in unicodedata.normalize("NFD", run):
        if unicodedata.combining(ch):
            continue                                   # Greek accents
        low = ch.lower()
        r = _CK_CYR.get(low, _CK_GRK.get(low))
        if r is None:
            out.append(ch)
        else:
            out.append(r.capitalize() if ch != low and r else r)
    return "".join(out)


def _ck_korean(run: str) -> str:
    out = []
    for ch in run:
        n = ord(ch) - 0xAC00
        out.append(_CK_K_FIRST[n // 588] + _CK_K_VOWEL[n % 588 // 28] + _CK_K_LAST[n % 28] if 0 <= n < 11172 else ch)
    return "".join(out)


def _ck_to_roman(text: str) -> str:
    """Every Hindi and Urdu word in the text spelled in Roman letters, plus Cyrillic/Greek/Korean.
    Anything else is left exactly as it is."""
    import unicodedata
    text = text or ""
    if _CK_DEV_RE.search(text):
        text = re.sub(r"[ऀ-ॿ]+", lambda m: _ck_dev_word(m.group(0)), text)
    if _CK_OTHER_SCRIPT.search(text):
        text = text.replace("،", ",").replace("؟", "?").replace("۔", ".")
        text = re.sub(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+", lambda m: _ck_urdu_word(m.group(0)), text)
    if _CK_MORE_SCRIPTS.search(text):
        text = "".join(unicodedata.normalize("NFD", ch) if "\u0370" <= ch <= "\u04FF" or "\u1F00" <= ch <= "\u1FFF" else ch for ch in text)
        text = re.sub(r"[\uAC00-\uD7A3]+", lambda m: _ck_korean(m.group(0)), text)
        text = unicodedata.normalize("NFC", re.sub(r"[\u0370-\u03FF\u0400-\u04FF\u0300-\u036F]+", lambda m: _ck_letters(m.group(0)), text))
    return text


# ---------------------------------------------------------------------------
# Gemini path (optional — user's key from config.json, never hard-coded)
# ---------------------------------------------------------------------------

def load_gemini_key(config_path=None):
    """Read the user's Gemini key from config.json (YT Clip Tool pattern:
    Settings stores it there). Returns None when absent — the caller must
    then use the local path and say so."""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "config.json")
    try:
        d = json.load(open(config_path))
        key = (d.get("gemini_api_key") or "").strip()
        return key or None
    except Exception:
        return None


def gemini_transliterate(text, api_key, src="Hindi/Urdu", model="gemini-2.0-flash",
                         timeout=30):
    """Transliterate with Gemini (natural Hinglish / Roman Urdu spelling).
    Returns the Roman text, or None on any failure (caller falls back to
    the local path). Only called when the user configured a key."""
    import urllib.request
    prompt = ("Transliterate this %s text into Roman (Latin) script, using "
              "natural Hinglish / Roman Urdu spelling as people write it on "
              "social media (e.g. azad, karna, mujhe, pakistan). Return ONLY "
              "the transliterated text, no explanation, no quotes.\n\n%s"
              % (src, text))
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
        print(f"[transliterate:gemini] failed ({e}) — falling back to local")
        return None


# ---------------------------------------------------------------------------
# main entry: words of a transcript -> display words + script
# ---------------------------------------------------------------------------

def to_roman_word(word, src_script, roman_mode="local", gemini_key=None):
    """One word -> Roman. roman_mode: 'local' (default) or 'gemini'."""
    if src_script == "devanagari":
        if roman_mode == "gemini" and gemini_key:
            return None  # sentence-level Gemini handles whole chunks; see below
        return devanagari_to_roman(word)
    if src_script == "urdu":
        if roman_mode == "gemini" and gemini_key:
            return None
        return urdu_to_roman(word)
    return word


def apply_caption_script(words, detected_lang, caption_mode="auto",
                         roman_mode="local", gemini_key=None):
    """Decide the caption script for a transcript and add a 'display' field
    to each word (the text the caption renderer shows).

    caption_mode:
      auto       — Hindi/Urdu speech -> Roman captions (sohaib's order);
                   English/other -> that language's own script.
      en         — English, Latin as-is.
      hi         — Hindi, Devanagari.
      hi-roman   — Hindi, Roman.
      ur         — Urdu, Nastaliq.
      ur-roman   — Urdu, Roman (also 'roman-urdu').
    Returns (words, script, method) where script is 'latin' | 'devanagari'
    | 'urdu' and method is 'local' | 'gemini' | 'native' | 'none'.
    """
    mode = (caption_mode or "auto").lower().replace("_", "-")
    if mode in ("ur-roman", "roman-urdu"):
        want, src = "roman", "ur"
    elif mode == "hi-roman":
        want, src = "roman", "hi"
    elif mode == "hi":
        want, src = "native", "hi"
    elif mode == "ur":
        want, src = "native", "ur"
    elif mode == "en":
        want, src = "latin", "en"
    else:  # auto
        if detected_lang in ("hi", "ur"):
            want, src = "roman", detected_lang
        elif detected_lang == "en":
            want, src = "latin", "en"
        else:
            want, src = "native", detected_lang

    if want == "latin":
        for w in words:
            w["display"] = w["word"]
        return words, "latin", "none"

    if want == "native":
        script = "devanagari" if src == "hi" else ("urdu" if src == "ur" else "latin")
        for w in words:
            # the caption fonts are Latin-only: a word they cannot draw
            # (Devanagari, Urdu, Cyrillic, Greek, Korean) is romanized
            # instead of rendering as tofu boxes.
            if not ck_drawable(w["word"]):
                w["display"] = _ck_to_roman(w["word"])
            else:
                w["display"] = w["word"]
        return words, script, "native"

    # want == "roman": transliterate word by word (local) or chunk by chunk (gemini)
    script_of_words = "devanagari" if src == "hi" else "urdu"
    if roman_mode == "gemini" and gemini_key:
        # Gemini works best on sentences, not single words: transliterate
        # each segment's text, then re-split onto the word timestamps.
        # Simpler robust approach: transliterate the joined word stream
        # in chunks of <= 40 words, then map back positionally.
        method = "gemini"
        flat = [w["word"] for w in words]
        roman = []
        ok = True
        for i in range(0, len(flat), 40):
            chunk = " ".join(flat[i:i + 40])
            res = gemini_transliterate(chunk, gemini_key,
                                       src="Hindi" if src == "hi" else "Urdu")
            if res is None:
                ok = False
                break
            roman.extend(res.split())
        if ok and len(roman) == len(flat):
            for w, r in zip(words, roman):
                w["display"] = r
            return words, "latin", "gemini"
        print("[transliterate] gemini word-count mismatch — falling back to local")

    method = "local"
    fn = devanagari_to_roman if src == "hi" else urdu_to_roman
    for w in words:
        r = fn(w["word"])
        w["display"] = r if r else w["word"]
    return words, "latin", method
