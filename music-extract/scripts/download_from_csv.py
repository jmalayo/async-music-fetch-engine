import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from musictools.download.core import SpotifyCredentials, default_js_runtime, download

MUSIC_EXTRACT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = MUSIC_EXTRACT_DIR / "data"
ENV_PATH = MUSIC_EXTRACT_DIR / ".env"
COOKIES_FILE = DATA_DIR / "cookies" / "cookies-files" / "cookies.txt"
CACHE_PATH = DATA_DIR / "blocked_cache.json"
PLAYLISTS_FETCHED_DIR = MUSIC_EXTRACT_DIR.parent / "spotify-api" / "playlists-fetched"

FORMAT = "mp3"
BITRATE = "192k"
THREADS = 3


def main() -> None:
    parser = argparse.ArgumentParser(description="Descarga una playlist a partir de su CSV de spotify_url.")
    parser.add_argument("category", help="Nombre de la carpeta/categoria (ej: rock_english)")
    parser.add_argument("--csv", type=Path, default=None, help="Ruta al CSV (default: playlists-fetched/<category>.csv)")
    parser.add_argument("--threads", type=int, default=THREADS)
    args = parser.parse_args()

    load_dotenv(dotenv_path=ENV_PATH)
    spotify_creds = SpotifyCredentials(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    )

    csv_path = args.csv or (PLAYLISTS_FETCHED_DIR / f"{args.category}.csv")
    links = pd.read_csv(csv_path, sep=";")["spotify_url"].tolist()

    if not links:
        sys.exit(f"[FATAL] {csv_path} no tiene enlaces en la columna spotify_url.")

    out_dir = DATA_DIR / "music-downloaded" / args.category
    out_dir.mkdir(parents=True, exist_ok=True)

    js_runtime = default_js_runtime()
    proxy = os.getenv("PROXY")

    print("=" * 60)
    print(f"  Descargando : {len(links)} enlaces")
    print(f"  Formato     : {FORMAT} @ {BITRATE}")
    print(f"  Threads     : {args.threads}")
    print(f"  Salida      : {out_dir}")
    print("=" * 60 + "\n")

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futures = {
            pool.submit(
                download,
                url,
                out_dir=out_dir,
                cookies_file=COOKIES_FILE,
                cache_path=CACHE_PATH,
                js_runtime=js_runtime,
                fmt=FORMAT,
                bitrate=BITRATE,
                spotify_creds=spotify_creds,
                proxy=proxy,
            ): url
            for url in links
        }

        for future in as_completed(futures):
            result = future.result()
            url, ok, msg = result["url"], result["ok"], result["mensaje"]

            if not ok:
                label = "[FAIL]"
            elif msg.startswith("[SKIP]"):
                label = "[SKIP]"
            else:
                label = "[OK]  "

            short = url if len(url) <= 65 else url[:62] + "..."
            print(f"  {label}  {short}")

            if not ok or label == "[SKIP]":
                for line in msg.splitlines()[-3:]:
                    if line.strip():
                        print(f"          {line.strip()}")

    print(f"\n  Archivos en: {out_dir}")


if __name__ == "__main__":
    main()
