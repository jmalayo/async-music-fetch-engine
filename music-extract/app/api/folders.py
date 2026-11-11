from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import auth, folders
from infrastructure.persistence.models import User

router = APIRouter(prefix="/folders", tags=["Web"])


class CreateFolderRequest(BaseModel):
    nombre: str


@router.get("")
def list_folders(q: str | None = None, user: User = Depends(auth.require_user)):
    """Carpetas del usuario, con su nombre visible y cuántas canciones tienen. `q` filtra por nombre."""
    return {"por_defecto": folders.DEFAULT_FOLDER, "carpetas": folders.list_folders(folders.user_root(user.username), q)}


@router.post("", status_code=201)
def create_folder(body: CreateFolderRequest, user: User = Depends(auth.require_user)):
    try:
        return folders.create_folder(folders.user_root(user.username), body.nombre)
    except folders.InvalidName as e:
        raise HTTPException(422, str(e))
    except folders.FolderExists as e:
        raise HTTPException(409, str(e))
