"""Entrega de archivos al equipo del usuario: una canción suelta (MP3) o varias en un ZIP.

Cada entrega suma 1 a `descargas` de cada canción incluida. El ZIP se arma en un archivo temporal sin comprimir
(los MP3 ya vienen comprimidos) y se borra después de enviarlo.
"""

import os
import re
import tempfile
import zipfile
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from config.settings import MUSIC_DOWNLOADED_ROOT
from infrastructure.persistence.models import Track
from musictools.download.audio import safe_name

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _safe(name: str) -> str:
    return _UNSAFE.sub(" ", name or "").strip(" .")[:120] or "owlo"


def _path(track: Track) -> Path | None:
    if not track.archivo:
        return None
    path = Path(track.archivo)
    path = path if path.is_absolute() else MUSIC_DOWNLOADED_ROOT / path
    # Nunca fuera de la carpeta de descargas.
    try:
        path.resolve().relative_to(MUSIC_DOWNLOADED_ROOT.resolve())
    except ValueError:
        return None
    return path if path.exists() else None


def parts_dir(track: Track, path: Path) -> Path | None:
    """Carpeta con los temas cortados de un mix. Si se guardó solo por partes, `archivo` ya es esa carpeta;
    si se guardó también entero, está al lado del MP3 con el título de la canción."""
    if path.is_dir():
        return path
    candidate = path.parent / safe_name(track.title or path.stem)
    return candidate if candidate.is_dir() else None


def tema_file(track: Track, tema: int) -> Path:
    """El MP3 de un tema del tracklist. `tema` es su índice en el tracklist completo."""
    from core.web_jobs import _loads, tema_files

    own = tema_files(track)
    if own:
        path = own.get(tema)
        if path is None or not path.is_file():
            raise HTTPException(404, "Ese tema no está guardado.")
        return path

    # Descargas de antes: los archivos se numeraban solo con los temas cortados (sin los excluidos).
    path = _path(track)
    parts = parts_dir(track, path) if path else None
    excluded = set((_loads(track.tracklist, {}) or {}).get("excluidos") or [])
    if parts is None or tema < 0 or tema in excluded:
        raise HTTPException(404, "Ese tema no está guardado.")
    position = sum(1 for i in range(tema) if i not in excluded)
    files = sorted(p for p in parts.glob("*.mp3") if not p.name.startswith("."))
    if position >= len(files):
        raise HTTPException(404, "Ese tema no está guardado.")
    return files[position]


def _cut(track: Track) -> bool:
    from core.web_jobs import saved_modes

    return saved_modes(track)["partes"]


def serve_tracks(db: Session, tracks: list[Track], *, name: str, force_zip: bool = False, modo: str | None = None):
    """Un mix cortado por tracklist se entrega por partes (un ZIP con sus temas, no el audio completo), porque eso
    es lo que pidió el usuario; `modo="entero"` entrega el MP3 completo, si se guardó."""
    found = [(t, p) for t in tracks if (p := _path(t)) is not None]
    if modo != "entero":
        found = [(t, (parts_dir(t, p) or p) if _cut(t) else p) for t, p in found]
        if len(found) == 1 and found[0][1].is_dir():
            force_zip = True
    if modo == "partes" and len(found) == 1:
        parts = parts_dir(*found[0])
        if parts is None:
            raise HTTPException(404, "Esta canción no se guardó por partes.")
        found, force_zip = [(found[0][0], parts)], True
    elif modo == "entero" and len(found) == 1 and not found[0][1].is_file():
        raise HTTPException(404, "Esta canción no se guardó entera.")
    if not found:
        raise HTTPException(404, "El archivo ya no está disponible (las descargas sin cuenta se borran a las 24 h).")

    for track, _ in found:
        track.descargas = (track.descargas or 0) + 1
    db.commit()

    # Una sola canción con su MP3: se entrega tal cual.
    if not force_zip and len(found) == 1 and found[0][1].is_file():
        path = found[0][1]
        return FileResponse(path, media_type="audio/mpeg", filename=path.name)

    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    from core.web_jobs import tema_files

    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as archive:
        used: set[str] = set()
        for track, path in found:
            own = [p for _, p in sorted(tema_files(track).items()) if p.is_file()] if modo != "entero" else []
            if own:
                # Un tracklist: solo sus temas (la carpeta del set puede tener temas de otras descargas).
                pairs = [(p, f"{p.parent.name}/{p.name}") for p in own]
            elif path.is_file():
                pairs = [(path, path.name)]
            else:
                pairs = [(p, f"{path.name}/{p.relative_to(path)}") for p in sorted(path.rglob("*")) if p.is_file() and not p.name.startswith(".")]
            for entry, arcname in pairs:
                base, n = arcname, 2
                while arcname in used:
                    stem, dot, ext = base.rpartition(".")
                    arcname = f"{stem} ({n}).{ext}" if dot else f"{base} ({n})"
                    n += 1
                used.add(arcname)
                archive.write(entry, arcname)

    return FileResponse(tmp, media_type="application/zip", filename=f"{_safe(name)}.zip", background=BackgroundTask(os.remove, tmp))
