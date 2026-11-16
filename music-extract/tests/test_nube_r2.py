"""Biblioteca en la nube (R2): se sube lo descargado, se borra lo borrado y biblioteca.json sigue a la biblioteca.

R2 se reemplaza por un almacenamiento en memoria con la misma interfaz de boto3 que usa el motor.
"""

import json
import re

import pytest

from test_biblioteca_limpieza import downloaded
from test_web_flow import create_youtube, preview_ready, settled, yt

KEY = re.compile(r"^v1/u/(\d+)/t/(\d+)-([a-z0-9]{10})\.mp3$")


class FakeR2:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        with open(filename, "rb") as f:
            self.objects[key] = {"body": f.read(), **(ExtraArgs or {})}

    def put_object(self, Bucket, Key, Body, **extra):
        self.objects[Key] = {"body": Body, **extra}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)], "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://r2.example/{Params['Key']}?firma=x&expira={ExpiresIn}"

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key]["body"])}

    def download_file(self, bucket, key, filename):
        with open(filename, "wb") as f:
            f.write(self.objects[key]["body"])

    def mp3s(self):
        return sorted(k for k in self.objects if k.endswith(".mp3"))

    def manifest(self, user_id):
        return json.loads(self.objects[f"v1/u/{user_id}/biblioteca.json"]["body"])


@pytest.fixture
def r2():
    from core import nube

    fake = FakeR2()
    nube.use_client(fake)
    yield fake
    nube.wait()
    nube.use_client(None)


def user_id(client):
    return client.get("/auth/me").json()["usuario"]["id"]


def synced(r2):
    from core import nube

    nube.wait()
    return r2


def test_sin_configurar_no_se_sube_nada(client, fake_engine):
    from core import nube

    assert nube.enabled() is False
    downloaded(client, "Sin Nube")
    nube.wait()  # no hay nada encolado: no falla ni intenta conectarse


def test_cancion_descargada_se_sube_con_clave_por_id(client, fake_engine, r2):
    track = downloaded(client, "Nube Uno")
    uid = user_id(client)

    [key] = [k for k in synced(r2).mp3s() if f"/t/{track['track_id']}-" in k]
    m = KEY.match(key)
    assert m and int(m.group(1)) == uid and int(m.group(2)) == track["track_id"]
    obj = r2.objects[key]
    assert obj["ContentType"] == "audio/mpeg"
    assert 'filename="Canal - Video.mp3"' in obj["ContentDisposition"]

    manifest = r2.manifest(uid)
    folder = next(c for c in manifest["carpetas"] if c["slug"] == "nube_uno")
    assert folder["nombre"] == "Nube Uno"
    song = next(s for s in folder["canciones"] if s["id"] == track["track_id"])
    assert song["clave"] == key and song["temas"] == []


def test_mover_y_renombrar_solo_cambian_la_lista(client, fake_engine, r2):
    track = downloaded(client, "Nube Origen")
    uid = user_id(client)
    before = synced(r2).mp3s()

    assert client.post("/library/tracks/move", json={"track_ids": [track["track_id"]], "carpeta": "Nube Destino"}).status_code == 200
    assert client.patch("/library/folders/nube_destino", json={"nombre": "Nube Final"}).status_code == 200
    assert client.post("/library/folders/nube_final/favorite", json={"favorita": True}).status_code == 200

    assert synced(r2).mp3s() == before  # ningún MP3 se copió ni se borró
    folders = {c["slug"]: c for c in r2.manifest(uid)["carpetas"]}
    assert "nube_origen" not in folders
    assert folders["nube_final"]["favorita"] is True
    assert [s["id"] for s in folders["nube_final"]["canciones"]] == [track["track_id"]]


def test_borrar_cancion_y_carpeta_las_quita_de_la_nube(client, fake_engine, r2):
    one = downloaded(client, "Nube Borrar", tag="nb1")
    two = downloaded(client, "Nube Carpeta", tag="nb2")
    uid = user_id(client)
    synced(r2)
    assert any(f"/t/{one['track_id']}-" in k for k in r2.mp3s())

    assert client.post("/library/tracks/delete", json={"track_ids": [one["track_id"]]}).status_code == 200
    assert client.delete("/library/folders/nube_carpeta").status_code == 200

    synced(r2)
    assert not any(f"/t/{one['track_id']}-" in k or f"/t/{two['track_id']}-" in k for k in r2.mp3s())
    slugs = {c["slug"] for c in r2.manifest(uid)["carpetas"]}
    assert "nube_borrar" not in slugs and "nube_carpeta" not in slugs


def test_tracklist_sube_el_set_y_cada_tema(client, fake_engine, r2):
    url = yt("nmix")
    fake_engine.behavior[f"{url}#seconds"] = 6.0
    fake_engine.resolved[url] = {
        "origen": "youtube", "title": "Mix nube", "artist": "DJ", "duration_s": 6, "disponible": True, "motivo": None, "candidatos": [],
        "chapters": [{"inicio_s": 0, "fin_s": 2, "titulo": "Intro"}, {"inicio_s": 2, "fin_s": 4, "titulo": "Medio"}, {"inicio_s": 4, "fin_s": None, "titulo": "Final"}],
    }
    job_id = create_youtube(client, [url], carpeta="Nube Sets", tracklist={"cortar": True})
    track = preview_ready(client, job_id)["tracks"][0]
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})

    keys = [k for k in synced(r2).mp3s() if f"/t/{track['track_id']}-" in k]
    base = next(k for k in keys if KEY.match(k))[: -len(".mp3")]
    assert sorted(keys) == sorted([f"{base}.mp3", f"{base}/01.mp3", f"{base}/02.mp3", f"{base}/03.mp3"])

    folder = next(c for c in r2.manifest(user_id(client))["carpetas"] if c["slug"] == "nube_sets")
    song = folder["canciones"][0]
    assert [(t["indice"], t["titulo"], t["clave"]) for t in song["temas"]] == [
        (0, "Intro", f"{base}/01.mp3"), (1, "Medio", f"{base}/02.mp3"), (2, "Final", f"{base}/03.mp3"),
    ]

    # Borrarla quita el set y sus temas.
    assert client.post("/library/tracks/delete", json={"track_ids": [track["track_id"]]}).status_code == 200
    assert not [k for k in synced(r2).mp3s() if k.startswith(base)]


def test_sin_cuenta_no_se_sube(anon_client, fake_engine, r2):
    job_id = create_youtube(anon_client, [yt("nanon")])
    track = preview_ready(anon_client, job_id)["tracks"][0]
    anon_client.post(f"/jobs/{job_id}/confirm")
    settled(anon_client, job_id, track["track_id"], {"successful"})
    assert not [k for k in synced(r2).mp3s() if f"/t/{track['track_id']}-" in k]


def test_al_arrancar_sube_lo_que_faltaba(client, fake_engine, r2):
    from core import nube
    from infrastructure.persistence.database import SessionLocal
    from infrastructure.persistence.models import Track

    nube.use_client(None)  # se baja sin nube configurada…
    track = downloaded(client, "Nube Despues", tag="ndesp")
    nube.wait()
    nube.use_client(r2)  # …y luego se configura

    nube.sync_pending()
    [key] = [k for k in synced(r2).mp3s() if f"/t/{track['track_id']}-" in k]
    with SessionLocal() as db:
        assert db.get(Track, track["track_id"]).nube == key[: -len(".mp3")]


def test_llaves_desde_el_token_de_r2(monkeypatch):
    import hashlib

    import requests

    from core import nube

    calls = []

    class Resp:
        def __init__(self, ok, data):
            self.ok, self._data = ok, data

        def json(self):
            return self._data

    def fake_get(url, headers, timeout):
        calls.append((url, headers["Authorization"]))
        if "/accounts/abc123/" in url:
            return Resp(False, {"success": False})  # no es token de cuenta…
        return Resp(True, {"result": {"id": "id-del-token", "status": "active"}})  # …es de usuario

    monkeypatch.setattr(requests, "get", fake_get)
    key_id, secret = nube.credentials_from_token("mi-token", "https://abc123.r2.cloudflarestorage.com")
    assert key_id == "id-del-token"
    assert secret == hashlib.sha256(b"mi-token").hexdigest()
    assert [c[1] for c in calls] == ["Bearer mi-token", "Bearer mi-token"]



# --- Solo en la nube: R2 como fuente y sin copia local ---------------------------------------------------------

@pytest.fixture
def solo_nube(r2):
    return r2


def local_mp3s(root, slug):
    return sorted(p.name for p in (root / "prueba" / slug).rglob("*.mp3"))


def test_solo_nube_borra_la_copia_local_y_entrega_desde_r2(client, fake_engine, solo_nube, downloads_root):
    track = downloaded(client, "Solo Nube", tag="snube")
    synced(solo_nube)
    assert local_mp3s(downloads_root, "solo_nube") == []  # ya no ocupa disco
    key = next(k for k in solo_nube.mp3s() if f"/t/{track['track_id']}-" in k)

    # Bajar: redirige a un enlace temporal de R2.
    r = client.get(f"/tracks/{track['track_id']}/file", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith(f"https://r2.example/{key}?")

    # Sigue en la biblioteca, cuenta en el selector de carpetas y como «ya descargada».
    assert [t["track_id"] for t in client.get("/library/folders/solo_nube").json()["tracks"]] == [track["track_id"]]
    picker = {c["slug"]: c for c in client.get("/folders").json()["carpetas"]}
    assert picker["solo_nube"]["canciones"] == 1
    job_id = create_youtube(client, [track["url"]], carpeta="Solo Nube")
    assert preview_ready(client, job_id)["tracks"][0]["ya_descargada"] is True

    # El ZIP de la carpeta trae el archivo de R2 y lo vuelve a borrar al terminar.
    r = client.get("/library/folders/solo_nube/zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert local_mp3s(downloads_root, "solo_nube") == []


def test_solo_nube_tracklist_temas_y_zip(client, fake_engine, solo_nube, downloads_root):
    import io
    import zipfile

    url = yt("snmix")
    fake_engine.behavior[f"{url}#seconds"] = 6.0
    fake_engine.resolved[url] = {
        "origen": "youtube", "title": "Mix solo nube", "artist": "DJ", "duration_s": 6, "disponible": True, "motivo": None, "candidatos": [],
        "chapters": [{"inicio_s": 0, "fin_s": 3, "titulo": "Uno"}, {"inicio_s": 3, "fin_s": None, "titulo": "Dos"}],
    }
    job_id = create_youtube(client, [url], carpeta="Solo Sets", tracklist={"cortar": True})
    track = preview_ready(client, job_id)["tracks"][0]
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})
    synced(solo_nube)
    assert local_mp3s(downloads_root, "solo_sets") == []

    r = client.get(f"/tracks/{track['track_id']}/file?tema=1", follow_redirects=False)
    assert r.status_code == 307 and "/02.mp3?" in r.headers["location"]

    r = client.get(f"/tracks/{track['track_id']}/file?modo=partes")
    assert r.status_code == 200
    names = sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert [n.split("/")[-1] for n in names] == ["01 - Uno.mp3", "02 - Dos.mp3"]
    assert local_mp3s(downloads_root, "solo_sets") == []

    # La vista del trabajo sigue mostrando los temas como guardados.
    recent = next(t for t in client.get("/tracks/recent?limit=200").json()["items"] if t["track_id"] == track["track_id"])
    assert recent["archivo_disponible"] is True


def test_solo_nube_mover_y_borrar(client, fake_engine, solo_nube, downloads_root):
    track = downloaded(client, "Nube Mover", tag="snmov")
    before = synced(solo_nube).mp3s()
    assert client.post("/library/tracks/move", json={"track_ids": [track["track_id"]], "carpeta": "Nube Movida"}).status_code == 200
    assert synced(solo_nube).mp3s() == before
    assert client.get(f"/tracks/{track['track_id']}/file", follow_redirects=False).status_code == 307

    assert client.post("/library/tracks/delete", json={"track_ids": [track["track_id"]]}).status_code == 200
    assert not [k for k in synced(solo_nube).mp3s() if f"/t/{track['track_id']}-" in k]
    assert client.get(f"/tracks/{track['track_id']}/file", follow_redirects=False).status_code == 404


def test_no_borra_la_copia_local_si_r2_no_la_tiene_completa(client, fake_engine, r2, downloads_root, monkeypatch):
    from core import nube
    from infrastructure.persistence.database import SessionLocal
    from infrastructure.persistence.models import Track

    def upload_recortado(filename, bucket, key, ExtraArgs=None):
        r2.objects[key] = {"body": b"recortado", **(ExtraArgs or {})}  # en R2 quedó distinto

    monkeypatch.setattr(r2, "upload_file", upload_recortado)
    track = downloaded(client, "Nube Incompleta", tag="sninc")
    synced(r2)
    with SessionLocal() as db:
        assert nube.free_local(db.get(Track, track["track_id"])) == 0
    assert local_mp3s(downloads_root, "nube_incompleta") == ["Canal - Video.mp3"]
