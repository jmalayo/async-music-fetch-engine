import json
from pathlib import Path

def load(cache_path: Path) -> set[str]:

    if not cache_path.exists():
        return set()

    try:
        return set(json.loads(cache_path.read_text(encoding="utf-8")))

    except (json.JSONDecodeError, OSError):
        return set()

def add(video_id: str, cache_path: Path) -> None:

    if not video_id:
        return

    ids = load(cache_path)

    if video_id in ids:
        return

    ids.add(video_id)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")
