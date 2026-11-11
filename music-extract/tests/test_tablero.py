"""Tablero de Historial: contadores, cola, sin confirmar, rechazadas, recientes y volver a bajar."""

import itertools

from conftest import wait_until

_ids = itertools.count(1)


def yt(tag="tab"):
    prefix = tag[:4]
    return f"https://www.youtube.com/watch?v={prefix}{next(_ids):0{11 - len(prefix)}d}"


def preview_job(c, urls, **extra):
    r = c.post("/jobs", json={"origen": "enlaces", "urls": urls, **extra})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    wait_until(lambda: c.get(f"/jobs/{job_id}/preview").json()["status"] == "preview")
    return job_id


def tracks_by_url(c, job_id):
    return {t["url"]: t for t in c.get(f"/jobs/{job_id}/tracks").json()}


def finished(c, job_id):
    return c.get(f"/jobs/{job_id}").json()["status"] in ("completed", "completed_with_errors", "cancelled")


def test_tablero_pide_sesion(anon_client):
    for path in ("/jobs/dashboard", "/tracks/queue", "/tracks/rejected", "/tracks/recent"):
        assert anon_client.get(path).status_code == 401, path


def test_sin_confirmar_se_lista_y_se_descarta(client, fake_engine):
    job_id = preview_job(client, [yt("unc")])
    tablero = client.get("/jobs/dashboard").json()
    assert job_id in [j["job_id"] for j in tablero["sin_confirmar"]]
    assert tablero["contadores"]["sin_confirmar"] >= 1

    assert client.delete(f"/jobs/{job_id}").status_code == 204
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert job_id not in [j["job_id"] for j in client.get("/jobs/dashboard").json()["sin_confirmar"]]


def test_no_se_descarta_un_trabajo_confirmado(client, fake_engine):
    job_id = preview_job(client, [yt("conf")])
    client.post(f"/jobs/{job_id}/confirm")
    wait_until(lambda: finished(client, job_id))
    assert client.delete(f"/jobs/{job_id}").status_code == 409


def test_cola_con_posicion(client, fake_engine):
    urls = [yt("cola") for _ in range(5)]
    for url in urls:
        fake_engine.behavior[url] = "slow"
    job_id = preview_job(client, urls)
    client.post(f"/jobs/{job_id}/confirm")
    wait_until(lambda: client.get("/tracks/queue").json()["total"] >= 1)

    cola = client.get("/tracks/queue").json()
    posiciones = [t["posicion"] for t in cola["items"]]
    assert posiciones == list(range(1, len(posiciones) + 1))
    assert all(t["status"] == "pending" for t in cola["items"])
    ids = [t["track_id"] for t in cola["items"]]
    assert ids == sorted(ids)

    tablero = client.get("/jobs/dashboard").json()
    assert tablero["contadores"]["en_curso"] >= 1 and tablero["contadores"]["en_cola"] == cola["total"]
    assert tablero["en_curso"][0]["descargando"]

    client.post(f"/jobs/{job_id}/cancel")
    wait_until(lambda: finished(client, job_id), timeout=20)


def test_rechazada_se_recupera_y_baja(client, fake_engine):
    keep, drop = yt("keep"), yt("drop")
    job_id = preview_job(client, [keep, drop])
    preview = client.get(f"/jobs/{job_id}/preview").json()["tracks"]
    drop_id = next(t["track_id"] for t in preview if t["url"] == drop)
    assert client.patch(f"/jobs/{job_id}/tracks/{drop_id}", json={"accion": "quitar"}).status_code == 200
    client.post(f"/jobs/{job_id}/confirm")
    wait_until(lambda: finished(client, job_id))

    rechazadas = client.get("/tracks/rejected").json()
    assert drop_id in [t["track_id"] for t in rechazadas["items"]]

    r = client.post(f"/jobs/{job_id}/tracks/{drop_id}/recover")
    assert r.status_code == 200, r.text
    wait_until(lambda: tracks_by_url(client, job_id)[drop]["status"] == "successful")
    assert drop_id not in [t["track_id"] for t in client.get("/tracks/rejected").json()["items"]]
    # Una que no está rechazada no se recupera.
    assert client.post(f"/jobs/{job_id}/tracks/{drop_id}/recover").status_code == 409


def test_recientes_paginadas_y_por_categoria(client, fake_engine):
    ok = [yt("rec") for _ in range(3)]
    bad = yt("rbad")
    fake_engine.behavior[bad] = "fail"
    job_id = preview_job(client, ok + [bad], carpeta="Recientes")
    client.post(f"/jobs/{job_id}/confirm")
    wait_until(lambda: finished(client, job_id))

    page1 = client.get("/tracks/recent", params={"limit": 2}).json()
    assert page1["page"] == 1 and len(page1["items"]) == 2 and page1["pages"] >= 2
    page2 = client.get("/tracks/recent", params={"limit": 2, "page": 2}).json()
    assert not {t["track_id"] for t in page1["items"]} & {t["track_id"] for t in page2["items"]}

    fallidas = client.get("/tracks/recent", params={"categoria": "fallida", "limit": 50}).json()["items"]
    assert bad in [t["url"] for t in fallidas] and all(t["status"] in ("failed", "skipped", "waiting_auth") for t in fallidas)
    assert all("carpeta_slug" in t["trabajo"] for t in page1["items"])


def test_volver_a_bajar_crea_un_trabajo_en_la_misma_carpeta(client, fake_engine):
    url = yt("redo")
    job_id = preview_job(client, [url], carpeta="Otra vez")
    client.post(f"/jobs/{job_id}/confirm")
    wait_until(lambda: finished(client, job_id))
    track_id = tracks_by_url(client, job_id)[url]["track_id"]

    r = client.post(f"/tracks/{track_id}/redownload")
    assert r.status_code == 202, r.text
    new_job = r.json()["job_id"]
    assert new_job != job_id
    wait_until(lambda: finished(client, new_job))
    assert client.get(f"/jobs/{new_job}").json()["carpeta"] == "Otra vez"
    assert tracks_by_url(client, new_job)[url]["status"] == "successful"
    assert client.post("/tracks/999999/redownload").status_code == 404
