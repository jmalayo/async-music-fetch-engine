from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import auth, folders, job_service
from infrastructure.persistence.database import get_db
from infrastructure.persistence.models import Job, Track, User

router = APIRouter(prefix="/folders", tags=["Web"])


class CreateFolderRequest(BaseModel):
    nombre: str


@router.get("")
def list_folders(q: str | None = None, db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Carpetas del usuario, con su nombre visible y cuántas canciones tienen. `q` filtra por nombre.

    Las canciones se cuentan en la base (las de la biblioteca), no en disco: pueden estar solo en la nube."""
    rows = (db.query(Job.carpeta).join(Track, Track.job_id == Job.job_id)
            .filter(Job.user_id == user.id, Track.status.in_(("successful", "incorrect")), Track.eliminada.is_(False),
                    Track.ya_descargada.is_(False), Track.archivo.isnot(None)).all())
    counts: dict[str, int] = {}
    for (carpeta,) in rows:
        slug = job_service.slugify(carpeta or folders.DEFAULT_FOLDER)
        counts[slug] = counts.get(slug, 0) + 1
    carpetas = [{**f, "canciones": counts.get(f["slug"], 0)} for f in folders.list_folders(folders.user_root(user.username), q)]
    return {"por_defecto": folders.DEFAULT_FOLDER, "carpetas": carpetas}


@router.post("", status_code=201)
def create_folder(body: CreateFolderRequest, user: User = Depends(auth.require_user)):
    try:
        return folders.create_folder(folders.user_root(user.username), body.nombre)
    except folders.InvalidName as e:
        raise HTTPException(422, str(e))
    except folders.FolderExists as e:
        raise HTTPException(409, str(e))
