import argparse
from pathlib import Path

from musictools.normalize.loudness import TARGET_LUFS, TARGET_TP, normalize_folder

MUSIC_EXTRACT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = MUSIC_EXTRACT_DIR / "data"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normaliza MP3s con LUFS + peak normalization via ffmpeg loudnorm."
    )

    parser.add_argument("playlist", help="Nombre de la carpeta de playlist (ej: rock_english)")
    parser.add_argument("--target-lufs", type=float, default=TARGET_LUFS)
    parser.add_argument("--target-tp", type=float, default=TARGET_TP)
    args = parser.parse_args()

    input_dir = DATA_DIR / "music-downloaded" / args.playlist
    output_dir = DATA_DIR / "music-normalized" / args.playlist

    normalize_folder(input_dir, output_dir, args.target_lufs, args.target_tp)


if __name__ == "__main__":
    main()
