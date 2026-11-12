import itertools

from conftest import make_mp3, wait_until

_ids = itertools.count(1)


def yt(tag="v"):
    """Una URL de YouTube distinta en cada test (la base es compartida y deduplica por URL)."""
    prefix = tag[:4]
    return f"https://www.youtube.com/watch?v={prefix}{next(_ids):0{11 - len(prefix)}d}"


def preview_ready(client, job_id):
    return wait_until(lambda: (p := client.get(f"/jobs/{job_id}/preview").json())["status"] == "preview" and p)


def track_of(client, job_id, track_id):
    return next(t for t in client.get(f"/jobs/{job_id}/tracks").json() if t["track_id"] == track_id)


def settled(client, job_id, track_id, statuses):
    return wait_until(lambda: (t := track_of(client, job_id, track_id))["status"] in statuses and t)


def create_youtube(client, urls, **extra):
    r = client.post("/jobs", json={"origen": "youtube", "urls": urls, **extra})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def test_vista_previa_y_descarga(client, fake_engine, downloads_root):
    url = yt()
    job_id = create_youtube(client, [url], carpeta="Prueba Flujo")
    preview = preview_ready(client, job_id)
    assert [t["status"] for t in preview["tracks"]] == ["preview"]
    assert fake_engine.calls == []  # la vista previa no descarga nada

    r = client.post(f"/jobs/{job_id}/confirm")
    assert r.status_code == 200
    track = settled(client, job_id, preview["tracks"][0]["track_id"], {"successful"})
    assert track["rondas_usadas"] == 0

    folder = downloads_root / "prueba" / "prueba_flujo"
    assert (folder / "Canal - Video.mp3").exists()
    assert not list(folder.glob(".parcial-*"))  # la carpeta temporal se borra
    assert client.get(f"/jobs/{job_id}").json()["status"] == "completed"


def test_url_ya_descargada_no_se_vuelve_a_bajar(client, fake_engine):
    url = yt()
    first = create_youtube(client, [url])
    preview_ready(client, first)
    client.post(f"/jobs/{first}/confirm")
    wait_until(lambda: client.get(f"/jobs/{first}").json()["status"] == "completed")

    second = create_youtube(client, [url])
    track = preview_ready(client, second)["tracks"][0]
    assert track["ya_descargada"] is True
    assert track["status"] == "successful"


def test_intentos_vuelven_a_cero_al_bajar_en_el_segundo(client, fake_engine):
    url = yt()
    fake_engine.behavior[url] = lambda n: "fail" if n == 1 else "ok"
    job_id = create_youtube(client, [url])
    track_id = preview_ready(client, job_id)["tracks"][0]["track_id"]
    client.post(f"/jobs/{job_id}/confirm")

    failed = settled(client, job_id, track_id, {"failed"})
    assert failed["rondas_usadas"] == 1
    assert failed["motivo"] == "no_candidates"

    assert client.post(f"/jobs/{job_id}/tracks/{track_id}/retry").status_code == 200
    done = settled(client, job_id, track_id, {"successful"})
    assert done["rondas_usadas"] == 0


def test_sin_intentos_a_los_tres_y_cambiar_url_reinicia(client, fake_engine):
    url = yt()
    fake_engine.behavior[url] = "fail"
    job_id = create_youtube(client, [url])
    track_id = preview_ready(client, job_id)["tracks"][0]["track_id"]
    client.post(f"/jobs/{job_id}/confirm")

    for expected in (1, 2, 3):
        track = settled(client, job_id, track_id, {"failed"})
        assert track["rondas_usadas"] == expected
        if expected < 3:
            client.post(f"/jobs/{job_id}/tracks/{track_id}/retry")
            wait_until(lambda: track_of(client, job_id, track_id)["rondas_usadas"] == expected + 1 or track_of(client, job_id, track_id)["status"] != "failed")

    r = client.post(f"/jobs/{job_id}/tracks/{track_id}/retry")
    assert r.status_code == 409

    new_url = yt("nuevo")
    r = client.patch(f"/jobs/{job_id}/tracks/{track_id}", json={"accion": "cambiar_url", "url": new_url})
    assert r.status_code == 200, r.text
    done = settled(client, job_id, track_id, {"successful"})
    assert done["url"] == new_url
    assert done["rondas_usadas"] == 0


def test_progreso_y_cancelar_sin_gastar_intento(client, fake_engine):
    url = yt()
    fake_engine.behavior[url] = "slow"
    job_id = create_youtube(client, [url])
    track_id = preview_ready(client, job_id)["tracks"][0]["track_id"]
    client.post(f"/jobs/{job_id}/confirm")

    running = wait_until(lambda: (t := track_of(client, job_id, track_id))["progreso"] and t["progreso"]["porcentaje"] > 5 and t)
    assert running["status"] == "in_progress"
    assert running["progreso"]["fase"] == "descargando"

    r = client.post(f"/jobs/{job_id}/tracks/{track_id}/cancel")
    assert r.status_code == 202
    cancelled = settled(client, job_id, track_id, {"cancelada"})
    assert cancelled["rondas_usadas"] == 0
    assert cancelled["progreso"] is None

    fake_engine.behavior[url] = "ok"
    assert client.post(f"/jobs/{job_id}/tracks/{track_id}/requeue").status_code == 200
    settled(client, job_id, track_id, {"successful"})


def test_cancelar_en_espera_y_trabajo_completo(client, fake_engine):
    urls = [yt() for _ in range(5)]
    for u in urls:
        fake_engine.behavior[u] = "slow"
    job_id = create_youtube(client, urls)
    preview_ready(client, job_id)
    client.post(f"/jobs/{job_id}/confirm")
    wait_until(lambda: sum(t["status"] == "in_progress" for t in client.get(f"/jobs/{job_id}/tracks").json()) == 3)

    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 202
    wait_until(lambda: all(t["status"] == "cancelada" for t in client.get(f"/jobs/{job_id}/tracks").json()))
    assert client.get(f"/jobs/{job_id}").json()["status"] == "cancelled"
    assert all(t["rondas_usadas"] == 0 for t in client.get(f"/jobs/{job_id}/tracks").json())


def test_cookies_vencidas_no_gastan_intento(client, fake_engine):
    url = yt()
    fake_engine.behavior[url] = "needs_auth"
    job_id = create_youtube(client, [url])
    track_id = preview_ready(client, job_id)["tracks"][0]["track_id"]
    client.post(f"/jobs/{job_id}/confirm")
    track = settled(client, job_id, track_id, {"waiting_auth"})
    assert track["rondas_usadas"] == 0
    assert track["motivo"] == "needs_auth"


def test_spotify_dudosas_sin_candidatos_y_quitar(client, fake_engine, monkeypatch):
    from infrastructure.external import spotify_client

    urls = [f"https://open.spotify.com/track/sp{next(_ids):020d}" for _ in range(3)]
    tracks = [
        {"name": n, "artists": "Artista", "duration_ms": 200000, "spotify_url": u}
        for n, u in zip(["Buena", "Dudosa", "Sin candidatos"], urls)
    ]
    monkeypatch.setattr(spotify_client, "search_and_get_tracks", lambda entrada, db: ({"id": "pl1", "name": "Mi playlist"}, tracks))

    fake_engine.resolved[urls[1]] = {"origen": "spotify", "title": "Dudosa", "artist": "Artista", "duration_s": 200, "disponible": True,
                                     "motivo": None, "chapters": [], "candidatos": [{"title": "Dudosa (Live)", "channel": "X", "duration": 320,
                                     "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "source": "youtube", "score": 41.0}]}
    fake_engine.resolved[urls[2]] = {"origen": "spotify", "title": "Sin candidatos", "artist": "Artista", "duration_s": 200,
                                     "disponible": False, "motivo": "no_candidates", "chapters": [], "candidatos": []}

    r = client.post("/jobs", json={"origen": "spotify", "entrada": "Mi playlist"})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    preview = preview_ready(client, job_id)
    by_title = {t["title"]: t for t in preview["tracks"]}
    assert by_title["Dudosa"]["mejor"]["score_pick"] == 41.0
    assert by_title["Sin candidatos"]["mejor"] is None
    assert preview["carpeta"] == "Mi playlist"

    r = client.patch(f"/jobs/{job_id}/tracks/{by_title['Buena']['track_id']}", json={"accion": "quitar"})
    assert r.json()["status"] == "rechazada"

    client.post(f"/jobs/{job_id}/confirm")
    final = wait_until(lambda: client.get(f"/jobs/{job_id}").json()["status"] in ("completed", "completed_with_errors") and client.get(f"/jobs/{job_id}/tracks").json())
    status = {t["title"]: t["status"] for t in final}
    assert status == {"Buena": "rechazada", "Dudosa": "successful", "Sin candidatos": "rechazada"}

    # Se bajó exactamente el candidato revisado en la vista previa, sin volver a buscar.
    used = [c for u, c in fake_engine.calls if u == urls[1]][0]
    assert used[0]["score"] == 41.0


def test_spotify_playlist_ajena_devuelve_403(client, monkeypatch):
    from infrastructure.external import spotify_client

    def raise_403(entrada, db):
        raise spotify_client.InsufficientSpotifyAuth("Spotify devolvio 403")

    monkeypatch.setattr(spotify_client, "search_and_get_tracks", raise_403)
    assert client.post("/jobs", json={"origen": "spotify", "entrada": "playlist ajena"}).status_code == 403


def test_youtube_rechaza_enlaces_invalidos(client):
    r = client.post("/jobs", json={"origen": "youtube", "urls": ["https://open.spotify.com/track/abc"]})
    assert r.status_code == 422


def test_tracklist_corta_en_temas(client, fake_engine, downloads_root):
    url = yt("mix")
    fake_engine.behavior[f"{url}#seconds"] = 6.0
    fake_engine.resolved[url] = {
        "origen": "youtube", "title": "Mix de prueba", "artist": "DJ", "duration_s": 6, "disponible": True, "motivo": None, "candidatos": [],
        "chapters": [{"inicio_s": 0, "fin_s": 2, "titulo": "Intro"}, {"inicio_s": 2, "fin_s": 4, "titulo": "Tema/Dos"}, {"inicio_s": 4, "fin_s": None, "titulo": "Final"}],
    }
    job_id = create_youtube(client, [url], carpeta="Mixes", tracklist={"cortar": True, "guardar_completo": False})
    track = preview_ready(client, job_id)["tracks"][0]
    assert track["tracklist"]["disponible"] is True
    assert [t["titulo"] for t in track["tracklist"]["temas"]] == ["Intro", "Tema/Dos", "Final"]

    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})

    folder = downloads_root / "prueba" / "mixes" / "Mix de prueba"
    assert sorted(p.name for p in folder.glob("*.mp3")) == ["01 - Intro.mp3", "02 - Tema Dos.mp3", "03 - Final.mp3"]
    assert (downloads_root / "prueba" / "mixes" / "Canal - Video.mp3").exists()  # el set completo siempre se guarda


def test_endpoint_anterior_sigue_funcionando(client, fake_engine):
    url = yt()
    r = client.post("/downloads/download-urls-manual", json={"urls": [url], "group_name": "legacy"})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    wait_until(lambda: client.get(f"/stages/view-download-status?job_id={job_id}").json()["successful"] == 1)


def test_lista_compartida_spotify_youtube_tiktok(client, fake_engine, monkeypatch, downloads_root):
    from infrastructure.external import spotify_client

    pl_tracks = [{"name": "De playlist", "artists": "Artista", "duration_ms": 180000,
                  "spotify_url": f"https://open.spotify.com/track/pl{next(_ids):020d}"}]
    monkeypatch.setattr(spotify_client, "search_and_get_tracks", lambda entrada, db: ({"id": "p9", "name": "Mi lista"}, pl_tracks))

    youtube = yt()
    tiktok = f"https://www.tiktok.com/@alguien/video/{7300000000000000000 + next(_ids)}?is_from_webapp=1"
    spotify_track = f"https://open.spotify.com/intl-es/track/tr{next(_ids):020d}?si=abc"
    r = client.post("/jobs", json={
        "origen": "enlaces",
        "urls": [youtube, tiktok, spotify_track, "https://open.spotify.com/intl-es/playlist/37i9dQZF1DX4UtSsGT1Sbe?si=x"],
        "carpeta": "Mezcla",
    })
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    preview = preview_ready(client, job_id)

    assert preview["origen"] == "mixto"
    assert preview["carpeta"] == "Mezcla"
    by_origin = sorted(t["origen"] for t in preview["tracks"])
    assert by_origin == ["spotify", "spotify", "tiktok", "youtube"]
    urls = {t["url"] for t in preview["tracks"]}
    assert tiktok.split("?")[0] in urls  # sin parámetros de seguimiento
    assert spotify_track.split("?")[0] in urls

    client.post(f"/jobs/{job_id}/confirm")
    wait_until(lambda: client.get(f"/jobs/{job_id}").json()["status"] in ("completed", "completed_with_errors"))
    assert (downloads_root / "prueba" / "mezcla").is_dir()


def test_lista_compartida_rechaza_enlaces_de_otros_sitios(client):
    r = client.post("/jobs", json={"origen": "enlaces", "urls": ["https://vimeo.com/123"]})
    assert r.status_code == 422


def test_carpetas_listar_buscar_y_crear(client, downloads_root):
    r = client.post("/folders", json={"nombre": "Para Correr 2026"})
    assert r.status_code == 201, r.text
    assert r.json() == {"nombre": "Para Correr 2026", "slug": "para_correr_2026", "canciones": 0, "favorita": False}
    assert (downloads_root / "prueba" / "para_correr_2026").is_dir()

    assert client.post("/folders", json={"nombre": "para correr 2026"}).status_code == 409
    assert client.post("/folders", json={"nombre": "   "}).status_code == 422

    found = client.get("/folders", params={"q": "correr"}).json()
    assert found["por_defecto"] == "Canciones sueltas"
    assert [f["nombre"] for f in found["carpetas"]] == ["Para Correr 2026"]
    assert client.get("/folders", params={"q": "no-existe-xyz"}).json()["carpetas"] == []


def test_carpeta_de_un_trabajo_guarda_su_nombre_visible(client, fake_engine):
    job_id = create_youtube(client, [yt()], carpeta="Fiesta de Año Nuevo")
    preview_ready(client, job_id)
    client.post(f"/jobs/{job_id}/confirm")
    wait_until(lambda: client.get(f"/jobs/{job_id}").json()["status"] == "completed")
    names = [f["nombre"] for f in client.get("/folders", params={"q": "fiesta"}).json()["carpetas"]]
    assert names == ["Fiesta de Año Nuevo"]


def test_vista_previa_trae_la_imagen_del_video(client, fake_engine):
    youtube = yt()
    fake_engine.resolved[youtube] = {"origen": "youtube", "title": "Video", "artist": "Canal", "duration_s": 60, "disponible": True, "motivo": None,
                                     "candidatos": [], "chapters": [], "thumbnail": "https://i.ytimg.com/vi/abc/mqdefault.jpg"}
    job_id = create_youtube(client, [youtube])
    track = preview_ready(client, job_id)["tracks"][0]
    assert track["thumbnail"] == "https://i.ytimg.com/vi/abc/mqdefault.jpg"


def test_candidato_de_spotify_lleva_la_miniatura_de_su_video():
    from musictools.download.core import _public_candidate

    c = _public_candidate({"id": "HzdD8kbDzZA", "title": "Take on Me", "channel": "a-ha", "duration": 226, "url": "https://www.youtube.com/watch?v=HzdD8kbDzZA", "source": "ytmusic", "score": 110})
    assert c["thumbnail"] == "https://i.ytimg.com/vi/HzdD8kbDzZA/mqdefault.jpg"


def test_fallidas_de_todos_los_trabajos(client, fake_engine):
    bad_a, bad_b, good = yt("fa"), yt("fb"), yt("ok")
    fake_engine.behavior[bad_a] = "fail"
    fake_engine.behavior[bad_b] = "fail"
    job_a = create_youtube(client, [bad_a, good], carpeta="Trabajo A")
    job_b = create_youtube(client, [bad_b], carpeta="Trabajo B")
    for job in (job_a, job_b):
        preview_ready(client, job)
        client.post(f"/jobs/{job}/confirm")
    wait_until(lambda: all(client.get(f"/jobs/{j}").json()["status"] in ("completed", "completed_with_errors") for j in (job_a, job_b)))

    failed = client.get("/tracks/failed").json()
    mine = {t["url"]: t for t in failed if t["url"] in (bad_a, bad_b, good)}
    assert set(mine) == {bad_a, bad_b}
    assert mine[bad_a]["trabajo"]["carpeta"] == "Trabajo A"
    assert mine[bad_b]["trabajo"]["job_id"] == job_b
    assert mine[bad_a]["rondas_usadas"] == 1


def test_video_que_no_se_pudo_leer_se_marca_no_disponible(client, fake_engine):
    url = yt("geo")
    fake_engine.resolved[url] = {"origen": "youtube", "title": None, "artist": None, "duration_s": None, "candidatos": [],
                                 "disponible": False, "motivo": "geo_restricted", "chapters": []}
    job_id = create_youtube(client, [url])
    track = preview_ready(client, job_id)["tracks"][0]
    assert track["disponible"] is False
    assert track["motivo"] == "geo_restricted"


def test_resumen_informa_las_descargas_en_paralelo(client, fake_engine):
    from config.settings import THREADS

    job_id = create_youtube(client, [yt("para")])
    preview_ready(client, job_id)
    assert client.get(f"/jobs/{job_id}").json()["en_paralelo"] == THREADS


def test_volver_a_buscar_una_sola_cancion(client, fake_engine):
    urls = [yt("rebus"), yt("rebus")]
    job_id = create_youtube(client, urls)
    tracks = preview_ready(client, job_id)["tracks"]
    target, other = tracks[0], tracks[1]
    fake_engine.resolved[target["url"]] = {"origen": "youtube", "title": "Encontrada otra vez", "artist": "Canal",
                                           "duration_s": 100, "disponible": True, "motivo": None, "candidatos": [], "chapters": []}

    r = client.post(f"/jobs/{job_id}/tracks/{target['track_id']}/research")
    assert r.status_code == 200, r.text
    again = preview_ready(client, job_id)["tracks"]
    assert next(t for t in again if t["track_id"] == target["track_id"])["title"] == "Encontrada otra vez"
    assert next(t for t in again if t["track_id"] == other["track_id"])["title"] == other["title"]  # la otra, igual

    client.post(f"/jobs/{job_id}/confirm")
    assert client.post(f"/jobs/{job_id}/tracks/{target['track_id']}/research").status_code == 409


def test_zip_del_trabajo_solo_con_las_marcadas(client, fake_engine):
    import io
    import zipfile

    from conftest import wait_until

    urls = [yt("zips"), yt("zips")]
    for u in urls:
        fake_engine.resolved[u] = {"origen": "youtube", "title": f"T{u[-3:]}", "artist": "C", "duration_s": 60,
                                   "disponible": True, "motivo": None, "candidatos": [], "chapters": []}
    job_id = create_youtube(client, urls)
    tracks = preview_ready(client, job_id)["tracks"]
    client.post(f"/jobs/{job_id}/confirm")
    wait_until(lambda: client.get(f"/jobs/{job_id}").json()["status"] == "completed")

    one = tracks[0]["track_id"]
    r = client.get(f"/jobs/{job_id}/zip?ids={one}")
    assert r.status_code == 200
    assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == 1
    assert client.get(f"/jobs/{job_id}/zip?ids=999999").status_code == 404
