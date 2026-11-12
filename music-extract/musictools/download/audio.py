"""Conversión a MP3 y corte por tracklist con ffmpeg, informando el % al reporter de progreso.

yt-dlp convierte con su postprocesador `FFmpegExtractAudio`, que no informa avance. Aquí ffmpeg corre con
`-progress pipe:1`: cada línea `out_time_us=` dice cuántos segundos de audio lleva, y con la duración del
video sale el porcentaje. Si el usuario cancela, o si ffmpeg pasa `stall_timeout_s` sin avanzar, el proceso
se mata.
"""

import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from . import progress
from .progress import TrackCancelled

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


class FfmpegError(RuntimeError):
    pass


class StalledError(RuntimeError):
    """ffmpeg dejó de avanzar."""


def probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        return float(out) if out else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _run_with_progress(cmd: list[str], on_seconds, stall_timeout_s: float) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines: queue.Queue = queue.Queue()

    def pump() -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    last_advance = time.monotonic()
    last_seconds = -1.0

    try:
        while True:
            progress.check_cancelled()

            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                if time.monotonic() - last_advance > stall_timeout_s:
                    raise StalledError(f"ffmpeg no avanzó en {stall_timeout_s:.0f} s")
                continue

            if line is None:
                break

            key, _, value = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and value.isdigit():
                # Las dos claves vienen en microsegundos (out_time_ms es un nombre histórico de ffmpeg).
                seconds = int(value) / 1_000_000
                if seconds > last_seconds:
                    last_seconds = seconds
                    last_advance = time.monotonic()
                    on_seconds(seconds)

        code = proc.wait()
        if code != 0:
            raise FfmpegError((proc.stderr.read() or "").strip()[-300:] or f"ffmpeg terminó con código {code}")

    except (TrackCancelled, StalledError):
        proc.kill()
        proc.wait()
        raise


def convert_to_mp3(src: Path, dst: Path, *, bitrate: str = "192k", duration_s: float | None = None, stall_timeout_s: float = 60) -> Path:
    reporter = progress.current()
    total = duration_s or probe_duration(src)
    started = time.monotonic()

    def on_seconds(seconds: float) -> None:
        if reporter is None:
            return
        pct = min(100.0, seconds / total * 100) if total else None
        elapsed = time.monotonic() - started
        # Tiempo restante a la velocidad que lleva ffmpeg hasta ahora.
        eta = round((total - seconds) * elapsed / seconds) if total and seconds > 0 else None
        reporter.update(
            fase="convirtiendo",
            porcentaje=round(pct, 1) if pct is not None else None,
            segundos=round(seconds),
            segundos_total=round(total) if total else None,
            eta_s=eta,
        )

    if reporter is not None:
        reporter.update(fase="convirtiendo", porcentaje=0.0, segundos=0, segundos_total=round(total) if total else None, eta_s=None)

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src), "-vn", "-codec:a", "libmp3lame", "-b:a", bitrate,
        "-progress", "pipe:1", "-nostats", str(dst),
    ]
    try:
        _run_with_progress(cmd, on_seconds, stall_timeout_s)
    except BaseException:
        dst.unlink(missing_ok=True)
        raise

    return dst


# Audio que guarda owlo: MP3 (convertido) o el original de YouTube/TikTok sin volver a codificar.
AUDIO_EXTS = (".mp3", ".opus", ".m4a", ".ogg")
_ORIGINAL_EXT = {"opus": ".opus", "aac": ".m4a", "mp3": ".mp3", "vorbis": ".ogg"}
MEDIA_TYPES = {".mp3": "audio/mpeg", ".opus": "audio/ogg", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}


def media_type(path) -> str:
    return MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def keep_original(src: Path) -> Path:
    """Formato original: el audio tal cual vino (Opus → .opus, AAC → .m4a), solo cambiando el contenedor, sin
    volver a codificar (no pierde calidad y es casi instantáneo)."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                            "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    ext = _ORIGINAL_EXT.get(probe.stdout.strip().lower())
    if ext is None:
        raise FfmpegError(f"Códec de audio no reconocido: {probe.stdout.strip() or probe.stderr.strip()[-120:]}")
    if src.suffix.lower() == ext:
        return src
    dst = src.with_suffix(ext)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "copy", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FfmpegError(f"No se pudo guardar el audio original: {result.stderr.strip()[-200:]}")
    src.unlink(missing_ok=True)
    return dst


def safe_name(text: str, max_len: int = 120) -> str:
    cleaned = _UNSAFE.sub(" ", text or "").strip(" .")
    return re.sub(r"\s+", " ", cleaned)[:max_len] or "sin titulo"


def cut_chapters(src: Path, chapters: list[dict], dest_dir: Path) -> list[Path]:
    """Corta `src` en un MP3 por capítulo, sin volver a codificar (`-c copy`).

    `chapters`: [{"inicio_s": 0, "fin_s": 222, "titulo": "Intro"}, …]. Si falta `fin_s`, el tema dura
    hasta el inicio del siguiente (o hasta el final del archivo).
    """
    reporter = progress.current()
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = len(chapters)
    outputs: list[Path] = []

    for i, chapter in enumerate(chapters):
        progress.check_cancelled()
        if reporter is not None:
            reporter.update(fase="cortando", porcentaje=round(i / total * 100, 1), temas_guardados=i, temas=total, eta_s=None)

        start = float(chapter.get("inicio_s") or 0)
        end = chapter.get("fin_s")
        if end is None and i + 1 < total:
            end = chapters[i + 1].get("inicio_s")

        out = dest_dir / f"{i + 1:02d} - {safe_name(chapter.get('titulo') or f'Tema {i + 1}')}{src.suffix.lower()}"
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{start:.3f}"]
        if end is not None:
            cmd += ["-to", f"{float(end):.3f}"]
        cmd += ["-i", str(src), "-c", "copy", "-map_metadata", "-1", "-metadata", f"title={chapter.get('titulo') or ''}", "-metadata", f"track={i + 1}/{total}", str(out)]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise FfmpegError(f"No se pudo cortar el tema {i + 1}: {result.stderr.strip()[-200:]}")
        outputs.append(out)

    if reporter is not None:
        reporter.update(fase="cortando", porcentaje=100.0, temas_guardados=total, temas=total, eta_s=0)

    return outputs
