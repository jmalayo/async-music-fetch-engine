"""Limpieza de la biblioteca: borrar canciones del disco y renombrar o borrar carpetas.

Borrar no es en cascada: la fila de la canción queda (con `eliminada` y sin archivo) para que Recientes conserve
el historial; solo desaparece de la biblioteca y deja de contar en la cuota.
"""

import json
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import settings
from core import folders, job_service, nube
from core.job_service import _lock, _now
from core.web_jobs import Conflict, NotFound, tema_files
from infrastructure.persistence.models import Job, Track, User
from musictools.download.audio import safe_name

BUSY = ("pending", "in_progress", "cancelling")
# Lo que muestra la biblioteca (lo mismo que api/library.LISTED): lo demás no se marca como eliminado.
LISTED = ("successful", "incorrect", "failed", "skipped", "waiting_auth")


def _folder_tracks(db: Session, user: User, slug: str) -> list[Track]:
    rows = db.query(Track, Job).join(Job, Job.job_id == Track.job_id).filter(Job.user_id == user.id).all()
    return [t for t, j in rows if job_service.slugify(j.carpeta or folders.DEFAULT_FOLDER) == slug]


def _inside_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(settings.MUSIC_DOWNLOADED_ROOT.resolve())
        return True
    except ValueError:
        return False


def _remove_files(db: Session, track: Track) -> int:
    """Borra del disco el archivo de la canción (y su carpeta de temas, si se cortó). Devuelve los bytes liberados.

    Si otra canción activa apunta al mismo archivo (se volvió a bajar), el archivo se deja.
    """
    freed = track.bytes or 0
    if track.archivo:
        shared = db.query(Track).filter(Track.archivo == track.archivo, Track.track_id != track.track_id,
                                        Track.eliminada.is_(False)).first()
        path = Path(track.archivo)
        path = path if path.is_absolute() else settings.MUSIC_DOWNLOADED_ROOT / path
        own_temas = tema_files(track)
        if own_temas:
            # Solo los temas de esta descarga: la carpeta del set puede tener temas de otras.
            for tema in own_temas.values():
                if _inside_root(tema) and tema.is_file():
                    tema.unlink()
            folder = next(iter(own_temas.values())).parent
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
            if not shared and _inside_root(path) and path.is_file():
                path.unlink()
        elif not shared and _inside_root(path):
            targets = [path]
            # Con el audio completo guardado, los temas quedan en una carpeta al lado con el título de la canción.
            if path.is_file():
                targets.append(path.parent / safe_name(track.title or path.stem))
            for target in targets:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
    track.archivo = None
    track.bytes = None
    track.nube = None
    track.eliminada = True
    track.updated_at = _now()
    return freed


def delete_tracks(db: Session, user: User, track_ids: list[int]) -> dict:
    with _lock:
        tracks = (db.query(Track).join(Job, Job.job_id == Track.job_id)
                  .filter(Job.user_id == user.id, Track.track_id.in_(track_ids or [-1]), Track.status.in_(LISTED), Track.eliminada.is_(False)).all())
        if not tracks:
            raise NotFound("No hay canciones para borrar.")
        if any(t.status in BUSY for t in tracks):
            raise Conflict("Hay canciones que se están descargando: espera a que terminen o cancélalas.")
        bases = [t.nube for t in tracks if t.nube]
        freed = sum(_remove_files(db, t) for t in tracks)
        db.commit()
    nube.tracks_removed(user.id, bases)
    return {"eliminadas": len(tracks), "liberado_bytes": freed}


def rename_folder(db: Session, user: User, slug: str, nombre: str) -> dict:
    nombre = (nombre or "").strip()
    if not nombre:
        raise folders.InvalidName("Escribe un nombre para la carpeta.")
    if len(nombre) > folders.MAX_NAME:
        raise folders.InvalidName(f"El nombre puede tener hasta {folders.MAX_NAME} caracteres.")

    root = folders.user_root(user.username)
    old_path, new_slug = root / slug, job_service.slugify(nombre)
    new_path = root / new_slug

    with _lock:
        tracks = _folder_tracks(db, user, slug)
        if not tracks and not old_path.is_dir():
            raise NotFound("No existe esa carpeta.")
        if any(t.status in BUSY for t in tracks):
            raise Conflict("Hay canciones de esta carpeta descargándose: espera a que terminen.")
        if new_slug != slug and (new_path.exists() or _folder_tracks(db, user, new_slug)):
            raise folders.FolderExists(f"Ya existe una carpeta llamada «{nombre}».")

        if new_slug != slug and old_path.is_dir():
            old_path.rename(new_path)
        new_path.mkdir(parents=True, exist_ok=True)
        folders.write_meta(new_path, nombre=nombre)  # conserva la favorita

        old_prefix = f"{root.name}/{slug}/"
        jobs = {t.job_id for t in tracks}
        for track in tracks:
            if track.archivo and track.archivo.startswith(old_prefix):
                track.archivo = f"{root.name}/{new_slug}/{track.archivo[len(old_prefix):]}"
        # También los trabajos sin canciones listadas (en vista previa) que apuntan a esta carpeta.
        for job in db.query(Job).filter(Job.user_id == user.id).all():
            if job.job_id in jobs or job_service.slugify(job.carpeta or folders.DEFAULT_FOLDER) == slug:
                job.carpeta = nombre
                job.updated_at = _now()
        db.commit()

    nube.library_changed(user.id)
    return {"nombre": nombre, "slug": new_slug}


def delete_folder(db: Session, user: User, slug: str) -> dict:
    path = folders.user_root(user.username) / slug
    with _lock:
        tracks = _folder_tracks(db, user, slug)
        if not tracks and not path.is_dir():
            raise NotFound("No existe esa carpeta.")
        if any(t.status in BUSY for t in tracks):
            raise Conflict("Hay canciones de esta carpeta descargándose: espera a que terminen.")
        removed = [t for t in tracks if t.status in LISTED and not t.eliminada]
        bases = [t.nube for t in removed if t.nube]
        freed = sum(_remove_files(db, t) for t in removed)
        if path.is_dir() and _inside_root(path):
            shutil.rmtree(path)
        db.commit()
    nube.tracks_removed(user.id, bases)
    return {"eliminadas": len(tracks), "liberado_bytes": freed}


def _unique(path: Path) -> Path:
    """Si ya existe algo con ese nombre en la carpeta destino, agrega « (2)», « (3)»…"""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}") if path.suffix else path.with_name(f"{path.name} ({n})")
        if not candidate.exists():
            return candidate
        n += 1


def move_tracks(db: Session, user: User, track_ids: list[int], nombre: str) -> dict:
    """Mueve canciones de la biblioteca a otra carpeta (la crea si no existe): mueve sus archivos en disco (el MP3 y,
    si es un tracklist, sus temas) y las pasa a un trabajo «movidas» de esa carpeta, así Biblioteca, Recientes y
    «ya descargada» las ven en su carpeta nueva. El trabajo original no se toca (pierde esas canciones)."""
    from core.web_jobs import _loads

    nombre = (nombre or "").strip()
    if not nombre:
        raise folders.InvalidName("Elige la carpeta de destino.")
    if len(nombre) > folders.MAX_NAME:
        raise folders.InvalidName(f"El nombre puede tener hasta {folders.MAX_NAME} caracteres.")
    root = folders.user_root(user.username)
    slug = job_service.slugify(nombre)
    target = root / slug

    with _lock:
        rows = (db.query(Track, Job).join(Job, Job.job_id == Track.job_id)
                .filter(Job.user_id == user.id, Track.track_id.in_(track_ids or [-1]), Track.status.in_(("successful", "incorrect")),
                        Track.eliminada.is_(False), Track.ya_descargada.is_(False)).all())
        rows = [(t, j) for t, j in rows if job_service.slugify(j.carpeta or folders.DEFAULT_FOLDER) != slug]
        if not rows:
            raise NotFound("No hay canciones para mover (o ya están en esa carpeta).")

        if not (target / folders.META_FILE).exists():
            folders.write_meta(target, nombre=nombre)
        now = _now()
        moved_job = Job(job_id=job_service.new_job_id(), type="movidas", amount=len(rows), status="completed",
                        origen=rows[0][1].origen, carpeta=nombre, user_id=user.id, elapsed_time_s=0.0,
                        created_at=now, updated_at=now)
        db.add(moved_job)
        db.flush()

        for track, job in rows:
            old_folder = root / job_service.slugify(job.carpeta or folders.DEFAULT_FOLDER)
            relocated: dict[Path, Path] = {}

            def move(path: Path) -> Path:
                """Mueve un archivo (o carpeta) de la carpeta vieja a la nueva, respetando su ruta dentro de ella."""
                try:
                    rel = path.relative_to(old_folder)
                except ValueError:
                    return path
                for src, dst in relocated.items():  # ya se movió con su carpeta (temas dentro de la del set)
                    if path == src or src in path.parents:
                        return dst / path.relative_to(src)
                dest = _unique(target / rel)
                if path.exists() and _inside_root(path):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(dest))
                relocated[path] = dest
                return dest

            if track.archivo:
                main = Path(track.archivo)
                main = main if main.is_absolute() else settings.MUSIC_DOWNLOADED_ROOT / main
                track.archivo = str(move(main).relative_to(settings.MUSIC_DOWNLOADED_ROOT))
            options = _loads(track.tracklist, {}) or {}
            if options.get("archivos"):
                options["archivos"] = {
                    k: str(move(settings.MUSIC_DOWNLOADED_ROOT / v).relative_to(settings.MUSIC_DOWNLOADED_ROOT)) for k, v in options["archivos"].items()
                }
                track.tracklist = json.dumps(options)
            # Carpetas de temas que quedaron vacías en la carpeta vieja.
            for src in relocated:
                parent = src.parent
                if parent != old_folder and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            track.job_id = moved_job.job_id
            track.updated_at = now
        db.commit()

    nube.library_changed(user.id)
    return {"movidas": len(rows), "carpeta": {"nombre": nombre, "slug": slug}}


def set_favorite(db: Session, user: User, slug: str, favorita: bool) -> dict:
    """Marca o desmarca una carpeta como favorita (aparece primero al cambiar la carpeta de destino)."""
    path = folders.user_root(user.username) / slug
    if not path.is_dir() and not _folder_tracks(db, user, slug):
        raise NotFound("No existe esa carpeta.")
    folders.write_meta(path, favorita=bool(favorita))
    nube.library_changed(user.id)
    return {"slug": slug, "favorita": bool(favorita)}
