"""Progreso y cancelación de una descarga en curso.

Cada canción se descarga en un hilo del pool. Ese hilo registra un `Reporter` con `using(reporter)`, y las
funciones de descarga (yt-dlp, ffmpeg, corte por tracklist) lo leen con `current()` sin que haga falta
pasarlo por todos los parámetros.

- Progreso: el reporter guarda el último estado (`fase`, `porcentaje`, bytes, segundos…). La API lo lee en
  memoria; no se escribe en la base en cada actualización.
- Cancelación: `reporter.cancel()` marca la canción; el siguiente chequeo lanza `TrackCancelled`.
  Hereda de BaseException para que los `except Exception` de la cadena de descarga no la traguen.
"""

import threading
import time
from contextlib import contextmanager


class TrackCancelled(BaseException):
    """El usuario canceló la canción."""


class Reporter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict | None = None
        # Contexto que no pisa el hook de yt-dlp: en un tracklist, qué tema se baja y cómo va cada uno.
        self._context: dict = {}
        self._cancelled = threading.Event()
        self.last_change = time.monotonic()

    def update(self, **state) -> None:
        with self._lock:
            self._state = state
            self.last_change = time.monotonic()

    def set_context(self, **context) -> None:
        with self._lock:
            self._context.update(context)
            self.last_change = time.monotonic()

    def snapshot(self) -> dict | None:
        with self._lock:
            if not self._state and not self._context:
                return None
            return {**(self._state or {}), **self._context}

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check(self) -> None:
        if self._cancelled.is_set():
            raise TrackCancelled()

    # Hook de yt-dlp (`progress_hooks`): se llama muchas veces por segundo mientras baja.
    def ytdlp_hook(self, d: dict) -> None:
        self.check()

        if d.get("status") != "downloading":
            return

        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        eta = d.get("eta")

        self.update(
            fase="descargando",
            porcentaje=round(done / total * 100, 1) if total else None,
            bytes=done,
            bytes_total=total,
            velocidad_bps=round(d["speed"]) if d.get("speed") else None,
            eta_s=round(eta) if eta is not None else None,
        )


_local = threading.local()


def current() -> Reporter | None:
    return getattr(_local, "reporter", None)


def check_cancelled() -> None:
    reporter = current()
    if reporter is not None:
        reporter.check()


@contextmanager
def using(reporter: Reporter):
    previous = current()
    _local.reporter = reporter
    try:
        yield reporter
    finally:
        _local.reporter = previous
