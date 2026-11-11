import io
import itertools
import zipfile

from conftest import TEST_USER, wait_until

_ids = itertools.count(1)


def yt(tag="bib"):
    prefix = tag[:4]
    return f"https://www.youtube.com/watch?v={prefix}{next(_ids):0{11 - len(prefix)}d}"


def run_job(c, urls, **extra):
    r = c.post("/jobs", json={"origen": "enlaces", "urls": urls, **extra})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    wait_until(lambda: c.get(f"/jobs/{job_id}/preview").json()["status"] == "preview")
    assert c.post(f"/jobs/{job_id}/confirm").status_code == 200
    wait_until(lambda: c.get(f"/jobs/{job_id}").json()["status"] in ("completed", "completed_with_errors"))
    return job_id


def test_login_me_y_logout(anon_client):
    assert anon_client.get("/auth/me").json() == {"usuario": None}
    assert anon_client.post("/auth/login", json={"username": TEST_USER[0], "password": "mala"}).status_code == 401
    r = anon_client.post("/auth/login", json={"username": TEST_USER[0], "password": TEST_USER[1]})
    assert r.status_code == 200 and r.json()["username"] == TEST_USER[0]
    assert anon_client.get("/auth/me").json()["usuario"]["username"] == TEST_USER[0]
    anon_client.post("/auth/logout")
    assert anon_client.get("/auth/me").json() == {"usuario": None}


def test_sin_cuenta_solo_youtube_y_tiktok_y_sin_biblioteca(anon_client, fake_engine):
    r = anon_client.post("/jobs", json={"origen": "enlaces", "urls": ["https://open.spotify.com/track/abc123"]})
    assert r.status_code == 401
    for path in ("/library", "/folders", "/jobs", "/tracks/failed"):
        assert anon_client.get(path).status_code == 401, path

    job_id = run_job(anon_client, [yt("anon")])
    track = anon_client.get(f"/jobs/{job_id}/tracks").json()[0]
    assert track["archivo_disponible"] is True
    # Descarga directa al equipo.
    r = anon_client.get(f"/tracks/{track['track_id']}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"


def test_un_usuario_no_ve_los_trabajos_de_otro(client, anon_client, fake_engine):
    job_id = run_job(client, [yt("priv")])
    assert anon_client.get(f"/jobs/{job_id}").status_code == 404
    assert anon_client.get(f"/jobs/{job_id}/tracks").status_code == 404


def test_biblioteca_categorias_archivo_y_contador(client, fake_engine):
    ok, bad, fixed = yt("ok"), yt("bad"), yt("fix")
    fake_engine.behavior[bad] = "fail"
    fake_engine.behavior[fixed] = lambda n: "fail" if n == 1 else "ok"
    job_id = run_job(client, [ok, bad, fixed], carpeta="Mi Biblioteca")

    tracks = {t["url"]: t for t in client.get(f"/jobs/{job_id}/tracks").json()}
    client.post(f"/jobs/{job_id}/tracks/{tracks[fixed]['track_id']}/retry")
    wait_until(lambda: {t["url"]: t for t in client.get(f"/jobs/{job_id}/tracks").json()}[fixed]["status"] == "successful")

    lib = client.get("/library").json()
    folder = next(f for f in lib["carpetas"] if f["nombre"] == "Mi Biblioteca")
    assert (folder["descargada"], folder["recuperada"], folder["fallida"]) == (1, 1, 1)
    assert lib["usado_bytes"] > 0 and lib["cuota_bytes"] == 1024 ** 3

    detail = client.get(f"/library/folders/{folder['slug']}").json()
    cats = {t["url"]: t["categoria"] for t in detail["tracks"]}
    assert cats == {ok: "descargada", bad: "fallida", fixed: "recuperada"}

    track_id = next(t["track_id"] for t in detail["tracks"] if t["url"] == ok)
    assert client.get(f"/tracks/{track_id}/file").status_code == 200
    again = {t["url"]: t for t in client.get(f"/library/folders/{folder['slug']}").json()["tracks"]}
    assert again[ok]["descargas"] == 1

    z = client.get(f"/library/folders/{folder['slug']}/zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert len(names) == 2  # la descargada y la recuperada; la fallida no tiene archivo


def test_cuota_llena_no_deja_confirmar(client, fake_engine):
    from core import auth
    from infrastructure.persistence.database import SessionLocal
    from infrastructure.persistence.models import User

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == TEST_USER[0]).one()
        user.quota_bytes = 1
        db.commit()
    try:
        r = client.post("/jobs", json={"origen": "enlaces", "urls": [yt("full")]})
        job_id = r.json()["job_id"]
        wait_until(lambda: client.get(f"/jobs/{job_id}/preview").json()["status"] == "preview")
        r = client.post(f"/jobs/{job_id}/confirm")
        assert r.status_code == 409 and "límite" in r.json()["detail"]
    finally:
        with SessionLocal() as db:
            user = db.query(User).filter(User.username == TEST_USER[0]).one()
            user.quota_bytes = 1024 ** 3
            db.commit()
