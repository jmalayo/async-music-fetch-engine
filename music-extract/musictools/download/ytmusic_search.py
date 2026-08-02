from functools import lru_cache
from ytmusicapi import YTMusic

@lru_cache(maxsize=1)
def _client() -> YTMusic:
    return YTMusic()

def search(query: str, n: int = 10) -> list[dict]:

    try:
        results = _client().search(query, filter="songs", limit=n)

    except Exception:
        return []

    candidates = []

    for r in results:

        video_id = r.get("videoId")

        if not video_id:
            continue

        artists = ", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name"))

        candidates.append({
            "id": video_id,
            "title": r.get("title") or "",
            "channel": artists,
            "duration": r.get("duration_seconds"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "source": "ytmusic",
        })

    return candidates
