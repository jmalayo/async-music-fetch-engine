"""Biblioteca del usuario: sus carpetas, lo que contiene cada una y cuánto almacenamiento usa.

Una canción entra en una de cuatro categorías (los filtros rápidos de la web):
- descargada: bajó bien a la primera;
- recuperada: bajó después de fallar, o con una URL cambiada (la sugerencia que eligió el usuario);
- dudosa: bajó, pero con score bajo (puede no ser la canción pedida);
- fallida: no bajó (fallida, omitida o esperando cookies).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import auth, folders, job_service, library_admin, web_jobs
from infrastructure.external import downloader_service
from infrastructure.persistence.database import get_db
from infrastructure.persistence.models import Job, Track, User
from api import files

router = APIRouter(prefix="/library", tags=["Biblioteca"])

LISTED = ("successful", "incorrect", "failed", "skipped", "waiting_auth")


def category(track: Track) -> str:
    if track.status in ("failed", "skipped", "waiting_auth"):
        return "fallida"
    if track.status == "incorrect":
        return "dudosa"
    return "recuperada" if track.recuperada else "descargada"


def _user_tracks(db: Session, user: User):
    return (
        db.query(Track, Job)
        .join(Job, Job.job_id == Track.job_id)
        .filter(Job.user_id == user.id, Track.status.in_(LISTED), Track.eliminada.is_(False), Track.ya_descargada.is_(False))
        .order_by(Track.updated_at.desc())
        .all()
    )


@router.get("")
def library(db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Carpetas con sus contadores por categoría, y el almacenamiento usado contra la cuota."""
    by_folder: dict[str, dict] = {}

    # Carpetas en disco (también las vacías, recién creadas).
    for folder in folders.list_folders(folders.user_root(user.username)):
        by_folder[folder["slug"]] = {"nombre": folder["nombre"], "slug": folder["slug"], "canciones": 0, "bytes": 0,
                                     "descargada": 0, "recuperada": 0, "dudosa": 0, "fallida": 0, "actualizada": None}

    for track, job in _user_tracks(db, user):
        name = job.carpeta or folders.DEFAULT_FOLDER
        slug = job_service.slugify(name)
        entry = by_folder.setdefault(slug, {"nombre": name, "slug": slug, "canciones": 0, "bytes": 0,
                                            "descargada": 0, "recuperada": 0, "dudosa": 0, "fallida": 0, "actualizada": None})
        entry[category(track)] += 1
        entry["canciones"] += 1
        entry["bytes"] += track.bytes or 0
        entry["actualizada"] = max(filter(None, [entry["actualizada"], track.updated_at]), default=None)

    return {
        "usado_bytes": web_jobs.used_bytes(db, user.id),
        "cuota_bytes": user.quota_bytes,
        "carpetas": sorted(by_folder.values(), key=lambda f: f["actualizada"] or "", reverse=True),
    }


@router.get("/folders/{slug}")
def folder(slug: str, db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Las canciones de una carpeta, con su categoría, lo que se pidió, lo que se bajó y el score."""
    rows = [(t, j) for t, j in _user_tracks(db, user) if job_service.slugify(j.carpeta or folders.DEFAULT_FOLDER) == slug]
    path = folders.user_root(user.username) / slug
    if not rows and not path.exists():
        raise HTTPException(404, "No existe esa carpeta.")

    name = rows[0][1].carpeta if rows else folders.folder_payload(path)["nombre"]
    tracks = []
    for track, job in rows:
        payload = web_jobs.track_payload(track, downloader_service.progress_of(track.track_id))
        payload["categoria"] = category(track)
        payload["trabajo"] = {"job_id": job.job_id, "nombre": job.playlist_name or job.carpeta}
        tracks.append(payload)

    return {"nombre": name, "slug": slug, "tracks": tracks}


@router.get("/folders/{slug}/zip")
def folder_zip(slug: str, db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    tracks = [t for t, j in _user_tracks(db, user)
              if job_service.slugify(j.carpeta or folders.DEFAULT_FOLDER) == slug and t.archivo and t.status in ("successful", "incorrect")]
    if not tracks:
        raise HTTPException(404, "Esta carpeta no tiene canciones para bajar.")
    return files.serve_tracks(db, tracks, name=slug, force_zip=True)


class DeleteTracksRequest(BaseModel):
    track_ids: list[int]


class RenameFolderRequest(BaseModel):
    nombre: str


def _admin(fn):
    try:
        return fn()
    except web_jobs.NotFound as e:
        raise HTTPException(404, str(e))
    except (web_jobs.Conflict, folders.FolderExists) as e:
        raise HTTPException(409, str(e))
    except folders.InvalidName as e:
        raise HTTPException(422, str(e))


@router.post("/tracks/delete")
def delete_tracks(body: DeleteTracksRequest, db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Borra del disco las canciones elegidas. Siguen en Recientes (no es en cascada), pero ya no ocupan espacio."""
    return _admin(lambda: library_admin.delete_tracks(db, user, body.track_ids))


@router.patch("/folders/{slug}")
def rename_folder(slug: str, body: RenameFolderRequest, db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    return _admin(lambda: library_admin.rename_folder(db, user, slug, body.nombre))


@router.delete("/folders/{slug}")
def delete_folder(slug: str, db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Borra la carpeta y sus canciones del disco. Las canciones siguen en Recientes."""
    return _admin(lambda: library_admin.delete_folder(db, user, slug))
