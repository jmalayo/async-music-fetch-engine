import re
import unicodedata

from rapidfuzz import fuzz

_NEG_KEYWORDS = {
    "cover": -40, "live": -25, "remix": -30, "karaoke": -50,
    "instrumental": -35, "nightcore": -50, "sped up": -40,
    "slowed": -40, "8d": -40, "reverb": -30, "reaction": -60,
}

_POS_CHANNEL = {" - topic": 25, "vevo": 15}

_STOPWORDS = re.compile(r"\b(official|audio|video|music video|hd|4k|lyrics?)\b")

def _norm(s: str) -> str:

    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _STOPWORDS.sub(" ", s)

    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()

def score(candidate: dict, artist: str, title: str, duration_s: float) -> float:

    norm_title = _norm(title)
    norm_cand_title = _norm(candidate.get("title") or "")
    channel = (candidate.get("channel") or "").lower()

    s = 0.5 * fuzz.token_set_ratio(norm_title, norm_cand_title)
    s += 0.3 * fuzz.partial_ratio(_norm(artist), f"{norm_cand_title} {_norm(channel)}")

    duration = candidate.get("duration")
    if duration is None:
        s -= 5
    else:
        delta = abs(duration - duration_s)
        if delta <= 2:
            s += 30
        elif delta <= 5:
            s += 15
        elif delta <= 12:
            s += 0
        else:
            s -= min(60, (delta - 12) * 2)

    for keyword, weight in _NEG_KEYWORDS.items():
        if keyword in norm_cand_title and keyword not in norm_title:
            s += weight

    for keyword, weight in _POS_CHANNEL.items():
        if keyword in channel:
            s += weight

    return s

def rank(candidates: list[dict], artist: str, title: str, duration_s: float, top_n: int = 5) -> list[dict]:
    scored = [(score(c, artist, title, duration_s), c) for c in candidates]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    top = []
    for s, c in scored[:top_n]:
        c = dict(c)
        c["score"] = s
        top.append(c)

    return top
