import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

TARGET_LUFS = -14.0
TARGET_TP = -1.0

THRESH_SOFT = 2.0
THRESH_HARD = 6.0

REPORT_COLUMNS = [
    "filename",
    "lufs_original",
    "peak_original",
    "flag",
    "lufs_normalized",
    "peak_normalized",
    "action",
]

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def check_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        logger.error("ffmpeg no encontrado en PATH. Instalalo con: choco install ffmpeg")
        sys.exit(1)

def analyze(mp3: Path, target_lufs: float, target_tp: float) -> dict:
    af = f"loudnorm=I={target_lufs}:TP={target_tp}:LRA=11:print_format=json"
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(mp3), "-af", af, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    stderr = result.stderr
    start = stderr.rfind("{")
    end = stderr.rfind("}") + 1

    if start == -1 or end == 0:
        raise RuntimeError(f"ffmpeg no devolvio JSON:\n{stderr[-800:]}")

    return json.loads(stderr[start:end])

def normalize_file(src: Path, dst: Path, target_lufs: float, target_tp: float, measured: dict) -> None:
    af = (
        f"loudnorm=I={target_lufs}:TP={target_tp}:LRA=11"
        f":measured_I={measured['input_i']}"
        f":measured_LRA={measured['input_lra']}"
        f":measured_TP={measured['input_tp']}"
        f":measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}"
        f":linear=true:print_format=none"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-i", str(src),
        "-af", af,
        "-ar", "44100",
        "-b:a", "320k",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg normalize fallo (rc={result.returncode}):\n{result.stderr[-600:]}")

def get_flag(lufs: float, target: float) -> str:

    diff = lufs - target
    
    if diff <= -THRESH_HARD:
        return "MUY BAJO"

    if diff <= -THRESH_SOFT:
        return "bajo"

    if diff >= THRESH_HARD:
        return "MUY ALTO"

    if diff >= THRESH_SOFT:
        return "alto"

    return "OK"

def process_file(mp3: Path, output_dir: Path, target_lufs: float = TARGET_LUFS, target_tp: float = TARGET_TP) -> dict:
    try:
        measured = analyze(mp3, target_lufs, target_tp)

    except Exception as exc:
        logger.error(f"{mp3.name}: error de analisis: {exc}")

        return {
            "filename": mp3.name,
            "lufs_original": "ERROR",
            "peak_original": "ERROR",
            "flag": "ERROR",
            "lufs_normalized": "",
            "peak_normalized": "",
            "action": "ERROR",
        }

    raw_lufs = measured.get("input_i", "-inf")
    raw_tp = measured.get("input_tp", "-inf")

    if raw_lufs in ("-inf", "inf"):
        logger.warning(f"{mp3.name}: LUFS=-inf (silencio o pista vacia), SKIP")

        return {
            "filename": mp3.name,
            "lufs_original": "-inf",
            "peak_original": raw_tp,
            "flag": "SILENCIO",
            "lufs_normalized": "",
            "peak_normalized": "",
            "action": "SKIP",
        }

    lufs_orig = float(raw_lufs)
    peak_orig = float(raw_tp)
    flag = get_flag(lufs_orig, target_lufs)
    dst = output_dir / mp3.name

    if flag == "OK":
        action = "COPY"
        shutil.copy2(mp3, dst)

        lufs_norm = lufs_orig
        peak_norm = peak_orig

    else:
        action = "NORMALIZE"
        try:
            normalize_file(mp3, dst, target_lufs, target_tp, measured)

            verified = analyze(dst, target_lufs, target_tp)
            lufs_norm = float(verified.get("input_i", 0))
            peak_norm = float(verified.get("input_tp", 0))

        except Exception as exc:
            logger.error(f"{mp3.name}: error de normalizacion: {exc}")
            
            action = "ERROR"
            lufs_norm = None
            peak_norm = None

    logger.info(f"{mp3.name}: LUFS={lufs_orig:.2f} TP={peak_orig:.2f} flag={flag} action={action}")

    return {
        "filename": mp3.name,
        "lufs_original": f"{lufs_orig:.2f}",
        "peak_original": f"{peak_orig:.2f}",
        "flag": flag,
        "lufs_normalized": f"{lufs_norm:.2f}" if isinstance(lufs_norm, float) else "",
        "peak_normalized": f"{peak_norm:.2f}" if isinstance(peak_norm, float) else "",
        "action": action,
    }

def normalize_folder(input_dir: Path, output_dir: Path, target_lufs: float = TARGET_LUFS, target_tp: float = TARGET_TP) -> Path:

    check_ffmpeg()

    if not input_dir.exists():
        logger.error(f"Carpeta de entrada no existe: {input_dir}")
        sys.exit(1)

    mp3_files = sorted(input_dir.glob("*.mp3"))
    if not mp3_files:
        logger.error(f"No se encontraron MP3s en {input_dir}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"{len(mp3_files)} MP3s encontrados en {input_dir}")
    logger.info(f"Target: {target_lufs} LUFS | {target_tp} dBTP")

    total = len(mp3_files)
    rows = []

    for i, mp3 in enumerate(mp3_files, 1):
        logger.info(f"[{i}/{total}] {mp3.name}")
        rows.append(process_file(mp3, output_dir, target_lufs, target_tp))

    report = pd.DataFrame(rows, columns=REPORT_COLUMNS)
    report_path = output_dir / "normalization_report.csv"
    report.to_csv(report_path, sep=";", index=False, encoding="utf-8")

    counts = report["action"].value_counts()
    flag_counts = report["flag"].value_counts()

    logger.info(f"Reporte guardado: {report_path}")
    logger.info(f"Total archivos: {total}")
    logger.info(f"Normalizados: {counts.get('NORMALIZE', 0)}")
    logger.info(f"Copiados (OK): {counts.get('COPY', 0)}")
    logger.info(f"Errores: {counts.get('ERROR', 0)}")

    for flag in ("MUY BAJO", "bajo", "OK", "alto", "MUY ALTO"):
        logger.info(f"{flag}: {flag_counts.get(flag, 0)}")

    return report_path
