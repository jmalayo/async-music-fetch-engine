"""Sublista del tracklist: volver a buscar un tema y elegir entre otras coincidencias, como en la lista principal."""

from conftest import wait_until
from test_web_flow import create_youtube, preview_ready, settled, yt


def mix(client, fake_engine, temas_sin_video=("Uno",)):
    url = yt("elmix")
    fake_engine.behavior[f"{url}#seconds"] = 6.0
    fake_engine.resolved[url] = {
        "origen": "youtube", "title": "Mix para elegir", "artist": "DJ", "duration_s": 6, "disponible": True, "motivo": None, "candidatos": [],
        "chapters": [{"inicio_s": 0, "fin_s": 3, "titulo": "Uno"}, {"inicio_s": 3, "fin_s": None, "titulo": "Dos"}],
    }
    for titulo in temas_sin_video:
        fake_engine.temas[titulo] = "sin_video"
    job_id = create_youtube(client, [url], carpeta="Elegir", tracklist={"cortar": True})
    return job_id, preview_ready(client, job_id)["tracks"][0]


def tema(client, job_id, track_id, index):
    track = next(t for t in client.get(f"/jobs/{job_id}/preview").json()["tracks"] if t["track_id"] == track_id)
    return track["tracklist"]["temas"][index]


def test_volver_a_buscar_un_tema_trae_sugerencias_y_la_aceptada_se_baja(client, fake_engine):
    job_id, track = mix(client, fake_engine)
    tid = track["track_id"]
    assert track["tracklist"]["temas"][0]["video"] is None  # sin coincidencia: se cortaría del set

    # Ahora la búsqueda encuentra dos videos.
    fake_engine.temas["Uno"] = "segundo"
    r = client.post(f"/jobs/{job_id}/tracks/{tid}/temas/0/research")
    assert r.status_code == 200, r.text
    wait_until(lambda: client.get(f"/jobs/{job_id}/preview").json()["status"] == "preview")
    uno = tema(client, job_id, tid, 0)
    assert uno["video"]["url"].endswith("temUnoxxxxx") and uno["mejor_score"] >= 60
    assert [s["url"] for s in uno["sugerencias"]] == ["https://www.youtube.com/watch?v=segUnoxxxxx"]

    # Aceptar la sugerencia: pasa a ser el video del tema y no quedan sugerencias.
    seg = uno["sugerencias"][0]["url"]
    r = client.patch(f"/jobs/{job_id}/tracks/{tid}", json={"accion": "aceptar_sugerencia", "tema": 0, "url": seg})
    assert r.status_code == 200, r.text
    uno = r.json()["tracklist"]["temas"][0]
    assert uno["video"]["url"] == seg and uno["elegido"] is True and uno["sugerencias"] == []

    # Al descargar, ese tema se intenta primero con el video elegido.
    from core import web_jobs
    from infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        plan = web_jobs.download_plan(db, tid)
    assert plan["temas"][0]["candidatos"][0]["url"] == seg
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, tid, {"successful"})


def test_elegir_aunque_el_score_sea_bajo_y_rechazar(client, fake_engine, monkeypatch):
    from infrastructure.external import downloader_service

    job_id, track = mix(client, fake_engine, temas_sin_video=("Uno", "Dos"))
    tid = track["track_id"]
    flojos = [{"id": f"flo{i}", "title": f"Algo {i}", "channel": "X", "duration": 3, "url": f"https://www.youtube.com/watch?v=flojo{i:06d}",
               "source": "yt-dlp", "score": 30.0 - i} for i in range(3)]
    monkeypatch.setattr(downloader_service, "search_song", lambda *a, **kw: [dict(c) for c in flojos])

    client.post(f"/jobs/{job_id}/tracks/{tid}/temas/1/research")
    wait_until(lambda: client.get(f"/jobs/{job_id}/preview").json()["status"] == "preview")
    dos = tema(client, job_id, tid, 1)
    # Nada alcanza el mínimo: sigue sin video, pero se ofrecen las mejores para elegir.
    assert dos["video"] is None and len(dos["sugerencias"]) == 3 and dos["mejor_score"] == 30.0

    r = client.patch(f"/jobs/{job_id}/tracks/{tid}", json={"accion": "rechazar_sugerencia", "tema": 1, "url": flojos[2]["url"]})
    assert [s["url"] for s in r.json()["tracklist"]["temas"][1]["sugerencias"]] == [flojos[0]["url"], flojos[1]["url"]]
    r = client.patch(f"/jobs/{job_id}/tracks/{tid}", json={"accion": "aceptar_sugerencia", "tema": 1, "url": flojos[1]["url"]})
    dos = r.json()["tracklist"]["temas"][1]
    assert dos["video"]["url"] == flojos[1]["url"] and dos["elegido"] is True  # elegido por el usuario: vale


def test_tema_que_no_existe_o_trabajo_confirmado(client, fake_engine):
    job_id, track = mix(client, fake_engine)
    tid = track["track_id"]
    assert client.post(f"/jobs/{job_id}/tracks/{tid}/temas/9/research").status_code == 404
    client.post(f"/jobs/{job_id}/confirm")
    assert client.post(f"/jobs/{job_id}/tracks/{tid}/temas/0/research").status_code == 409


def test_tema_con_score_bajo_trae_dos_sugerencias_en_la_primera_busqueda(client, fake_engine, monkeypatch):
    from infrastructure.external import downloader_service

    flojos = [{"id": f"flo{i}", "title": f"Algo {i}", "channel": "X", "duration": 3, "url": f"https://www.youtube.com/watch?v=flojo{i:06d}",
               "source": "yt-dlp", "score": 40.0 - i} for i in range(4)]
    real = downloader_service.search_song
    monkeypatch.setattr(downloader_service, "search_song",
                        lambda artist, title, *a, **kw: [dict(c) for c in flojos] if title == "Uno" else real(artist, title, *a, **kw))

    job_id, track = mix(client, fake_engine, temas_sin_video=())
    uno, dos = track["tracklist"]["temas"]
    # Sin video que alcance: se ofrecen las 2 mejores para aceptar, rechazar o escuchar.
    assert uno["video"] is None and [s["url"] for s in uno["sugerencias"]] == [flojos[0]["url"], flojos[1]["url"]]
    # Con buen score: ya tiene su video y no hace falta sugerir.
    assert dos["video"] is not None and dos["sugerencias"] == []


def test_lo_buscado_con_el_puntaje_anterior_se_vuelve_a_buscar(client, fake_engine):
    from core import web_jobs
    from infrastructure.external import downloader_service
    from infrastructure.persistence.database import SessionLocal

    url = yt("viejomix")
    fake_engine.behavior[f"{url}#seconds"] = 6.0
    viejo = {"inicio_s": 0, "fin_s": 3, "titulo": "Uno", "candidatos": []}  # buscado antes, sin versión
    data = {"origen": "youtube", "title": "Mix viejo", "artist": "DJ", "duration_s": 6, "disponible": True, "motivo": None,
            "candidatos": [], "chapters": [viejo, {**viejo, "inicio_s": 3, "fin_s": None, "titulo": "Dos"}]}
    fake_engine.resolved[url] = data
    with SessionLocal() as db:
        web_jobs.store_resolution(db, url, data)

    job_id = create_youtube(client, [url], carpeta="Viejo", tracklist={"cortar": True})
    temas = preview_ready(client, job_id)["tracks"][0]["tracklist"]["temas"]
    assert all(t["video"] is not None for t in temas)  # se buscaron de nuevo con el puntaje de ahora
    with SessionLocal() as db:
        guardado = web_jobs.cached_resolution(db, url)
    assert all(c["busqueda"] == downloader_service.TEMA_SEARCH_VERSION for c in guardado["chapters"])
