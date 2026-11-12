"""Carpetas de descarga de cada usuario: las subcarpetas de MUSIC_DOWNLOADED_ROOT/<usuario>/.

En disco el nombre va normalizado (`slugify`: «Rock de los 80» → `rock_de_los_80`). El nombre que escribió el
usuario se guarda en un archivo `.owlo-carpeta.json` dentro de la carpeta, para mostrarlo tal cual. Una
carpeta sin ese archivo (creada antes, o por los endpoints anteriores) se muestra con su nombre de disco.
"""

import json
from pathlib import Path

from config.settings import ANON_DIR_NAME, MUSIC_DOWNLOADED_ROOT
from core import job_service

META_FILE = ".owlo-carpeta.json"
DEFAULT_FOLDER = "Canciones sueltas"
MAX_NAME = 80


class FolderExists(Exception):
    pass


class InvalidName(Exception):
    pass


def read_meta(path: Path) -> dict:
    """Datos guardados de la carpeta (.owlo-carpeta.json): su nombre visible y si es favorita."""
    try:
        data = json.loads((path / META_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_meta(path: Path, **changes) -> None:
    """Cambia datos de la carpeta sin perder los demás (renombrar no borra la favorita, ni al revés)."""
    path.mkdir(parents=True, exist_ok=True)
    data = {**read_meta(path), **changes}
    (path / META_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def is_favorite(path: Path) -> bool:
    return bool(read_meta(path).get("favorita"))


def _display_name(path: Path) -> str:
    try:
        return json.loads((path / META_FILE).read_text(encoding="utf-8"))["nombre"]
    except (OSError, ValueError, KeyError):
        return path.name.replace("_", " ")


def _count_songs(path: Path) -> int:
    return sum(1 for p in path.rglob("*.mp3") if not any(part.startswith(".") for part in p.relative_to(path).parts))


def remember_name(path: Path, nombre: str) -> None:
    """Guarda el nombre visible de la carpeta, si todavía no tiene uno."""
    meta = path / META_FILE
    if not meta.exists():
        meta.write_text(json.dumps({"nombre": nombre}, ensure_ascii=False), encoding="utf-8")


def user_root(username: str) -> Path:
    """Carpeta raíz de un usuario. Las descargas sin cuenta van aparte, en ANON_DIR_NAME."""
    return MUSIC_DOWNLOADED_ROOT / job_service.slugify(username)


def anon_root() -> Path:
    return MUSIC_DOWNLOADED_ROOT / ANON_DIR_NAME


def folder_payload(path: Path) -> dict:
    return {"nombre": _display_name(path), "slug": path.name, "canciones": _count_songs(path), "favorita": is_favorite(path)}


def list_folders(root: Path, query: str | None = None) -> list[dict]:
    if not root.exists():
        return []

    target = job_service.normalize_name(query or "")
    folders = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        payload = folder_payload(path)
        if target and target not in job_service.normalize_name(payload["nombre"]) and target not in path.name:
            continue
        folders.append(payload)

    # Las favoritas primero (son las que aparecen arriba al cambiar la carpeta de destino), después por nombre.
    return sorted(folders, key=lambda f: (not f["favorita"], job_service.normalize_name(f["nombre"])))


def create_folder(root: Path, nombre: str) -> dict:
    nombre = (nombre or "").strip()
    if not nombre:
        raise InvalidName("Escribe un nombre para la carpeta.")
    if len(nombre) > MAX_NAME:
        raise InvalidName(f"El nombre puede tener hasta {MAX_NAME} caracteres.")

    path = root / job_service.slugify(nombre)
    if path.exists():
        raise FolderExists(f"Ya existe una carpeta llamada «{_display_name(path)}».")

    path.mkdir(parents=True)
    remember_name(path, nombre)
    return folder_payload(path)
