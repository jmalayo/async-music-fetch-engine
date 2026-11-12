"""Copia de la biblioteca en la nube (Cloudflare R2, compatible con S3).

El disco local sigue siendo la fuente: R2 es una copia para usar la biblioteca con la PC apagada. Cada canción
se sube con claves por identificador (no por nombre), así que renombrar o mover carpetas no toca los archivos:

    v1/u/{id_usuario}/t/{id_cancion}-{clave}.mp3       canción entera o set completo (.opus / .m4a si es el original)
    v1/u/{id_usuario}/t/{id_cancion}-{clave}/NN.mp3    temas del tracklist (NN = índice en el tracklist + 1)
    v1/u/{id_usuario}/biblioteca.json                  carpetas, nombres y claves (lo que lee la app)

`{clave}` son 10 caracteres al azar: los enlaces no se pueden adivinar y una nueva descarga no pisa la anterior.
Las descargas sin cuenta no se suben. Todo corre en un solo hilo aparte: subir nunca frena una descarga, y si
falla, la canción queda sin `nube` y se reintenta al arrancar el motor (`sync_pending`).
"""

import json
import logging
import os
import secrets
import string
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_futures
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from config import settings
from infrastructure.persistence.database import SessionLocal
from infrastructure.persistence.models import Job, Track

log = logging.getLogger(__name__)

VERSION = "v1"
_AUDIO_EXTS = (".mp3", ".opus", ".m4a", ".ogg")
_ALPHABET = string.ascii_lowercase + string.digits

_client = None
_bucket: str | None = None
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nube")
_pending: list[Future] = []
_pending_lock = threading.Lock()


# --- Cliente -------------------------------------------------------------------------------------------------

def use_client(client, bucket: str = "pruebas") -> None:
    """Reemplaza el cliente de R2 (las pruebas usan uno en memoria). `None` vuelve a la configuración del .env."""
    global _client, _bucket
    _client = client
    _bucket = bucket if client is not None else None


def bucket() -> str:
    return _bucket or settings.R2_BUCKET


def enabled() -> bool:
    has_keys = (settings.R2_ACCESS_KEY_ID and settings.R2_SECRET_ACCESS_KEY) or settings.R2_TOKEN_VALUE
    return _client is not None or bool(settings.R2_ENDPOINT and settings.R2_BUCKET and has_keys)


def credentials_from_token(token: str, endpoint: str) -> tuple[str, str]:
    """Llaves S3 a partir del token de R2: la secreta es el SHA-256 del token y la de acceso es el id del token,
    que Cloudflare devuelve al verificarlo (como token de cuenta o, si no, como token de usuario)."""
    import hashlib

    import requests

    secret = hashlib.sha256(token.encode()).hexdigest()
    account = (urlparse(endpoint).hostname or "").split(".")[0]
    headers = {"Authorization": f"Bearer {token}"}
    for url in (f"https://api.cloudflare.com/client/v4/accounts/{account}/tokens/verify",
                "https://api.cloudflare.com/client/v4/user/tokens/verify"):
        r = requests.get(url, headers=headers, timeout=15)
        if r.ok and (r.json().get("result") or {}).get("id"):
            return r.json()["result"]["id"], secret
    raise RuntimeError("Cloudflare no reconoce R2_TOKEN_VALUE: revisa que el token esté activo y sea de R2.")


def _s3():
    global _client
    if _client is None:
        import boto3

        key_id, secret = settings.R2_ACCESS_KEY_ID, settings.R2_SECRET_ACCESS_KEY
        if not (key_id and secret):
            key_id, secret = credentials_from_token(settings.R2_TOKEN_VALUE, settings.R2_ENDPOINT)
        _client = boto3.client(
            "s3",
            endpoint_url=settings.R2_ENDPOINT,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name="auto",
        )
    return _client


# --- Claves --------------------------------------------------------------------------------------------------

def new_base(user_id: int, track_id: int) -> str:
    clave = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    return f"{VERSION}/u/{user_id}/t/{track_id}-{clave}"


def main_key(base: str, ext: str = ".mp3") -> str:
    return f"{base}{ext.lower()}"


def tema_key(base: str, index: int, ext: str = ".mp3") -> str:
    return f"{base}/{index + 1:02d}{ext.lower()}"


def track_main_key(track: Track) -> str:
    return main_key(track.nube, Path(track.archivo or "").suffix or ".mp3")


def track_tema_key(track: Track, index: int) -> str:
    from core import web_jobs

    path = web_jobs.tema_files(track).get(index)
    return tema_key(track.nube, index, path.suffix if path is not None else ".mp3")


def manifest_key(user_id: int) -> str:
    return f"{VERSION}/u/{user_id}/biblioteca.json"


def _disposition(filename: str) -> str:
    """Al bajarlo, el teléfono lo guarda con su nombre real (con tildes incluidas, vía filename*)."""
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "owlo.mp3"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


# --- Cola ----------------------------------------------------------------------------------------------------

def _submit(fn, *args) -> None:
    if not enabled():
        return

    def run():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 — la nube nunca rompe el motor; se reintenta al arrancar
            log.exception("Nube: falló %s%s", fn.__name__, args)

    future = _executor.submit(run)
    with _pending_lock:
        _pending[:] = [f for f in _pending if not f.done()] + [future]


def wait(timeout: float = 30.0) -> None:
    """Espera a que termine lo encolado (para las pruebas)."""
    with _pending_lock:
        pending = list(_pending)
    wait_futures(pending, timeout=timeout)


def track_ready(track_id: int) -> None:
    """Una canción terminó de bajar: subirla y actualizar la biblioteca de su usuario."""
    _submit(_upload_track, track_id)


def tracks_removed(user_id: int, bases: list[str]) -> None:
    """Canciones borradas de la biblioteca: borrar sus archivos de la nube."""
    if bases:
        _submit(_remove, user_id, list(bases))


def library_changed(user_id: int | None) -> None:
    """Carpetas renombradas, canciones movidas, favoritas: solo cambia biblioteca.json."""
    if user_id is not None:
        _submit(_publish_manifest, user_id)


def sync_pending() -> None:
    """Al arrancar: sube lo que falta (canciones de antes o subidas que fallaron) y rehace cada biblioteca.json."""
    _submit(_sync_all)


# --- Trabajo (en el hilo de la nube) -------------------------------------------------------------------------

def _local(path: str | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    return p if p.is_absolute() else settings.MUSIC_DOWNLOADED_ROOT / p


def _uploadable(db, track_id: int):
    from core import web_jobs

    row = (db.query(Track, Job).join(Job, Job.job_id == Track.job_id)
           .filter(Track.track_id == track_id).first())
    if row is None:
        return None
    track, job = row
    if (job.user_id is None or track.eliminada or track.ya_descargada or not track.archivo
            or track.status not in ("successful", "incorrect")):
        return None
    return track, job, web_jobs.tema_files(track)


def _upload_track(track_id: int, publish: bool = True) -> None:
    from core.job_service import _lock

    with SessionLocal() as db:
        found = _uploadable(db, track_id)
        if found is None:
            return
        track, job, temas = found
        user_id, old = job.user_id, track.nube
        main = _local(track.archivo)
        base = new_base(user_id, track.track_id)

    s3 = _s3()
    from musictools.download.audio import media_type

    if main is not None and main.is_file():
        s3.upload_file(str(main), bucket(), main_key(base, main.suffix),
                       ExtraArgs={"ContentType": media_type(main), "ContentDisposition": _disposition(main.name)})
    for index, path in sorted(temas.items()):
        if path.is_file():
            s3.upload_file(str(path), bucket(), tema_key(base, index, path.suffix),
                           ExtraArgs={"ContentType": media_type(path), "ContentDisposition": _disposition(path.name)})

    with _lock, SessionLocal() as db:
        track = db.get(Track, track_id)
        # Si la borraron mientras se subía, lo recién subido sobra.
        if track is None or track.eliminada or not track.archivo:
            stale = base
        else:
            track.nube, stale = base, old
            db.commit()
    if stale:
        _delete_prefix(stale)
    if not keep_local():
        with SessionLocal() as db:
            track = db.get(Track, track_id)
            if track is not None and track.nube == base:
                free_local(track)
    if publish:
        _publish_manifest(user_id)


def _delete_prefix(base: str) -> None:
    """Borra el set (`base.mp3`, `base.opus`…) y sus temas (`base/…`)."""
    s3 = _s3()
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": bucket(), "Prefix": base}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        keys += [obj["Key"] for obj in page.get("Contents", []) if obj["Key"][len(base):][:1] in (".", "/")]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=bucket(), Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})


def _remove(user_id: int, bases: list[str]) -> None:
    for base in bases:
        _delete_prefix(base)
    _publish_manifest(user_id)


def build_manifest(db, user_id: int) -> dict:
    """La biblioteca del usuario tal como la ve la app sin la PC: carpetas con sus canciones subidas."""
    from core import folders, job_service, web_jobs
    from infrastructure.persistence.models import User

    user = db.get(User, user_id)
    rows = (db.query(Track, Job).join(Job, Job.job_id == Track.job_id)
            .filter(Job.user_id == user_id, Track.nube.isnot(None), Track.eliminada.is_(False))
            .order_by(Track.updated_at.desc()).all())

    carpetas: dict[str, dict] = {}
    for track, job in rows:
        nombre = job.carpeta or folders.DEFAULT_FOLDER
        slug = job_service.slugify(nombre)
        entry = carpetas.get(slug)
        if entry is None:
            path = folders.user_root(user.username) / slug if user else None
            entry = carpetas[slug] = {"slug": slug, "nombre": nombre, "favorita": bool(path and folders.is_favorite(path)), "canciones": []}

        main = _local(track.archivo)
        chapters = web_jobs._loads(track.preview, {}).get("chapters") or []
        temas = [
            {"indice": i, "titulo": (chapters[i].get("titulo") if i < len(chapters) else None) or f"Tema {i + 1}", "clave": tema_key(track.nube, i, p.suffix)}
            for i, p in sorted(web_jobs.tema_files(track).items())
        ]
        entry["canciones"].append({
            "id": track.track_id,
            "titulo": track.title,
            "artista": track.artist,
            "bytes": track.bytes,
            "duracion_s": track.duration_s,
            "clave": main_key(track.nube, main.suffix) if main is not None and main.suffix.lower() in _AUDIO_EXTS else None,
            "temas": temas,
            "actualizada": track.updated_at,
        })

    return {
        "version": 1,
        "actualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "carpetas": list(carpetas.values()),
    }


def _publish_manifest(user_id: int) -> None:
    with SessionLocal() as db:
        manifest = build_manifest(db, user_id)
    _s3().put_object(
        Bucket=bucket(), Key=manifest_key(user_id),
        Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8", CacheControl="no-cache",
    )


def _sync_all() -> None:
    with SessionLocal() as db:
        missing = [t.track_id for t, _ in (
            db.query(Track, Job).join(Job, Job.job_id == Track.job_id)
            .filter(Job.user_id.isnot(None), Track.nube.is_(None), Track.archivo.isnot(None), Track.eliminada.is_(False),
                    Track.ya_descargada.is_(False), Track.status.in_(("successful", "incorrect"))).all())]
        users = [u for (u,) in db.query(Job.user_id).filter(Job.user_id.isnot(None)).distinct().all()]
    for track_id in missing:
        try:
            _upload_track(track_id, publish=False)
        except Exception:  # noqa: BLE001 — una que falle no frena a las demás
            log.exception("Nube: no se pudo subir la canción %s", track_id)
    for user_id in users:
        _publish_manifest(user_id)


# --- La nube como fuente: entregar desde R2 y liberar el disco local ------------------------------------------

def in_cloud(track: Track) -> bool:
    return bool(track.nube) and enabled()


def presigned_url(key: str, expires_s: int = 900) -> str:
    """Enlace temporal de descarga directa desde R2 (el nombre del archivo ya va en su Content-Disposition)."""
    return _s3().generate_presigned_url("get_object", Params={"Bucket": bucket(), "Key": key}, ExpiresIn=expires_s)


def has_main(track: Track) -> bool:
    """Si la canción tiene el audio entero en R2 (los tracklists guardados solo por partes no lo tienen)."""
    return in_cloud(track) and Path(track.archivo or "").suffix.lower() in _AUDIO_EXTS


def hydrate(tracks: list[Track]) -> list[Path]:
    """Trae de R2 los archivos que ya no están en disco (para armar un ZIP). Devuelve los que bajó, para borrarlos
    después de entregar."""
    from core import web_jobs

    created: list[Path] = []
    for track in tracks:
        if not in_cloud(track):
            continue
        wanted = []
        main = _local(track.archivo)
        if main is not None and has_main(track) and not main.exists():
            wanted.append((main_key(track.nube, main.suffix), main))
        wanted += [(tema_key(track.nube, i, p.suffix), p) for i, p in web_jobs.tema_files(track).items() if not p.exists()]
        for key, path in wanted:
            path.parent.mkdir(parents=True, exist_ok=True)
            _s3().download_file(bucket(), key, str(path))
            created.append(path)
    return created


def discard(paths: list[Path]) -> None:
    """Borra los archivos traídos con `hydrate` (y las carpetas de temas que queden vacías)."""
    for path in paths:
        path.unlink(missing_ok=True)
        parent = path.parent
        if parent != settings.MUSIC_DOWNLOADED_ROOT and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


def free_local(track: Track) -> int:
    """Borra del disco la copia local de una canción que ya está en R2, solo si cada archivo está en R2 con el
    mismo tamaño. Devuelve los bytes liberados (0 si no se pudo comprobar)."""
    from core import web_jobs

    if not in_cloud(track):
        return 0
    pairs = []
    main = _local(track.archivo)
    if main is not None and main.is_file():
        pairs.append((main_key(track.nube, main.suffix), main))
    pairs += [(tema_key(track.nube, i, p.suffix), p) for i, p in web_jobs.tema_files(track).items() if p.is_file()]
    s3 = _s3()
    for key, path in pairs:
        try:
            remote = s3.head_object(Bucket=bucket(), Key=key)["ContentLength"]
        except Exception:  # noqa: BLE001 — si no está o no se puede comprobar, no se borra nada
            return 0
        if remote != path.stat().st_size:
            return 0
    freed = 0
    for _, path in pairs:
        freed += path.stat().st_size
        path.unlink()
        parent = path.parent
        if parent.is_dir() and not any(parent.iterdir()) and not (parent / ".owlo-carpeta.json").exists():
            parent.rmdir()
    return freed


def keep_local() -> bool:
    """Con NUBE_SIN_COPIA_LOCAL=1, la copia en disco se borra en cuanto la canción queda en R2."""
    return os.getenv("NUBE_SIN_COPIA_LOCAL", "").strip().lower() not in ("1", "true", "si", "sí")
