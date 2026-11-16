"""Carpetas de descarga de cada usuario: las subcarpetas de settings.MUSIC_DOWNLOADED_ROOT/<usuario>/.

En disco el nombre va normalizado (`slugify`: «Rock de los 80» → `rock_de_los_80`). Los datos de cada carpeta (el
nombre que escribió el usuario, si es favorita, dónde vive su música y cómo se sincroniza) se guardan en la base
(tabla `carpetas`): el disco de descargas es temporal y se pierde al recrear el contenedor. Un `.owlo-carpeta.json`
de antes se importa a la base la primera vez que se lee. Una carpeta sin datos se muestra con su nombre de disco.
"""

import json
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import settings
from core import job_service
from infrastructure.persistence.database import SessionLocal
from infrastructure.persistence.models import Carpeta

META_FILE = ".owlo-carpeta.json"
DEFAULT_FOLDER = "Canciones sueltas"
# Descargas sin cuenta: se borran apenas se entregan (y lo que quede, al arrancar el motor).
ANON_DIR = "_sin-cuenta"
MAX_NAME = 80

# Dónde vive la música de la carpeta: en la nube (R2), en el dispositivo (local) o en los dos.
ALMACENAMIENTOS = ("nube", "local", "ambos")
# Cómo llega al dispositivo una carpeta local o «ambos»: sola al abrir la app (auto) o con un botón (manual).
SINCRONIZACIONES = ("auto", "manual")
DEFAULTS = {"favorita": False, "almacenamiento": "nube", "sincronizacion": "manual"}
FIELDS = ("nombre", "favorita", "almacenamiento", "sincronizacion")


class FolderExists(Exception):
    pass


class InvalidName(Exception):
    pass


@contextmanager
def _session(db: Session | None):
    """La sesión de quien llama (si tiene una abierta con cambios, otra sesión que escriba quedaría bloqueada por
    SQLite) o una propia. Devuelve (sesión, propia)."""
    if db is not None:
        yield db, False
    else:
        with SessionLocal() as own:
            yield own, True


def _key(path: Path) -> tuple[str, str]:
    """(dueño, slug) de una carpeta: su carpeta raíz de usuario y su nombre en disco."""
    return path.parent.name, path.name


def _legacy_meta(path: Path) -> dict:
    try:
        data = json.loads((path / META_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_meta(path: Path, db: Session | None = None) -> dict:
    """Datos guardados de la carpeta. Sin fila en la base, importa el `.owlo-carpeta.json` de antes (si hay)."""
    dueno, slug = _key(path)
    with _session(db) as (session, _):
        row = session.query(Carpeta).filter_by(dueno=dueno, slug=slug).one_or_none()
        if row is not None:
            return {field: getattr(row, field) for field in FIELDS}
    legacy = _legacy_meta(path)
    if legacy.get("nombre"):
        write_meta(path, db=db, nombre=legacy["nombre"], favorita=bool(legacy.get("favorita")))
        return {**DEFAULTS, "nombre": legacy["nombre"], "favorita": bool(legacy.get("favorita"))}
    return {}


def write_meta(path: Path, db: Session | None = None, **changes) -> None:
    """Cambia datos de la carpeta sin perder los demás (renombrar no borra la favorita, ni al revés). Con la sesión
    de quien llama, solo la deja lista (flush): confirma quien llama."""
    path.mkdir(parents=True, exist_ok=True)
    dueno, slug = _key(path)
    with _session(db) as (session, own):
        row = session.query(Carpeta).filter_by(dueno=dueno, slug=slug).one_or_none()
        if row is None:
            legacy = _legacy_meta(path)
            row = Carpeta(dueno=dueno, slug=slug, nombre=legacy.get("nombre") or path.name.replace("_", " "),
                          favorita=bool(legacy.get("favorita")), almacenamiento=DEFAULTS["almacenamiento"],
                          sincronizacion=DEFAULTS["sincronizacion"])
            session.add(row)
        for field, value in changes.items():
            if field in FIELDS:
                setattr(row, field, value)
        session.commit() if own else session.flush()


def move_meta(old: Path, new: Path, nombre: str, db: Session | None = None) -> None:
    """Al renombrar: los datos pasan a la carpeta nueva con su nombre nuevo (la favorita y el resto se conservan)."""
    data = read_meta(old, db)
    if old != new:
        forget(old, db)
    write_meta(new, db=db, **{**DEFAULTS, **data, "nombre": nombre})


def forget(path: Path, db: Session | None = None) -> None:
    """Borra los datos de una carpeta (al borrarla)."""
    dueno, slug = _key(path)
    _named.discard((dueno, slug))
    with _session(db) as (session, own):
        session.query(Carpeta).filter_by(dueno=dueno, slug=slug).delete()
        session.commit() if own else session.flush()


def is_favorite(path: Path) -> bool:
    return bool(read_meta(path).get("favorita"))


def _display_name(path: Path) -> str:
    return read_meta(path).get("nombre") or path.name.replace("_", " ")


def _count_songs(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for p in path.rglob("*.mp3") if not any(part.startswith(".") for part in p.relative_to(path).parts))


# Carpetas que ya tienen su nombre guardado: cada descarga llama a remember_name y, así, solo la primera de cada
# carpeta toca la base (los hilos de descarga no compiten por escribir en SQLite).
_named: set[tuple[str, str]] = set()


def remember_name(path: Path, nombre: str, db: Session | None = None) -> None:
    """Guarda el nombre visible de la carpeta, si todavía no tiene uno."""
    if _key(path) in _named:
        return
    if not read_meta(path, db).get("nombre"):
        write_meta(path, db=db, nombre=nombre)
    _named.add(_key(path))


def user_root(username: str) -> Path:
    """Carpeta raíz de un usuario. Las descargas sin cuenta van aparte, en ANON_DIR."""
    return settings.MUSIC_DOWNLOADED_ROOT / job_service.slugify(username)


def anon_root() -> Path:
    return settings.MUSIC_DOWNLOADED_ROOT / ANON_DIR


def folder_payload(path: Path) -> dict:
    meta = {**DEFAULTS, **read_meta(path)}
    return {
        "nombre": meta.get("nombre") or path.name.replace("_", " "),
        "slug": path.name,
        "canciones": _count_songs(path),
        "favorita": bool(meta["favorita"]),
        "almacenamiento": meta["almacenamiento"],
        "sincronizacion": meta["sincronizacion"],
    }


def storage_of(path: Path) -> dict:
    """Dónde vive la música de la carpeta y cómo se sincroniza (por defecto: nube, manual)."""
    meta = {**DEFAULTS, **read_meta(path)}
    return {"almacenamiento": meta["almacenamiento"], "sincronizacion": meta["sincronizacion"]}


def _known_slugs(root: Path) -> set[str]:
    """Carpetas del usuario: las que tienen datos en la base y las que existen en disco."""
    with SessionLocal() as db:
        slugs = {slug for (slug,) in db.query(Carpeta.slug).filter_by(dueno=root.name).all()}
    if root.exists():
        slugs |= {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}
    return slugs


def exists(path: Path) -> bool:
    return path.is_dir() or path.name in _known_slugs(path.parent)


def list_folders(root: Path, query: str | None = None) -> list[dict]:
    target = job_service.normalize_name(query or "")
    folders = []
    for slug in _known_slugs(root):
        payload = folder_payload(root / slug)
        if target and target not in job_service.normalize_name(payload["nombre"]) and target not in slug:
            continue
        folders.append(payload)

    # Las favoritas primero (son las que aparecen arriba al cambiar la carpeta de destino), después por nombre.
    return sorted(folders, key=lambda f: (not f["favorita"], job_service.normalize_name(f["nombre"])))


def create_folder(root: Path, nombre: str, almacenamiento: str = "nube", sincronizacion: str = "manual") -> dict:
    nombre = (nombre or "").strip()
    if not nombre:
        raise InvalidName("Escribe un nombre para la carpeta.")
    if len(nombre) > MAX_NAME:
        raise InvalidName(f"El nombre puede tener hasta {MAX_NAME} caracteres.")
    check_storage(almacenamiento, sincronizacion)

    path = root / job_service.slugify(nombre)
    if exists(path):
        raise FolderExists(f"Ya existe una carpeta llamada «{_display_name(path)}».")

    write_meta(path, nombre=nombre, almacenamiento=almacenamiento, sincronizacion=sincronizacion)
    return folder_payload(path)


def check_storage(almacenamiento: str | None, sincronizacion: str | None) -> None:
    if almacenamiento is not None and almacenamiento not in ALMACENAMIENTOS:
        raise InvalidName("Elige dónde guardar la carpeta: nube, local o ambos.")
    if sincronizacion is not None and sincronizacion not in SINCRONIZACIONES:
        raise InvalidName("La sincronización es automática o manual.")
