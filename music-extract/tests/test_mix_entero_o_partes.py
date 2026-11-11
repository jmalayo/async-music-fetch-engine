"""Tracklist: el set completo siempre se guarda y cada tema se busca y se baja como canción completa (si no hay un
video bueno, se corta del set). «Ya descargada» se valida tema por tema."""

import io
import zipfile

from test_web_flow import create_youtube, preview_ready, settled, yt

CHAPTERS = [{"inicio_s": 0, "fin_s": 3, "titulo": "Uno"}, {"inicio_s": 3, "fin_s": None, "titulo": "Dos"}]
SET3 = [{"inicio_s": 0, "fin_s": 2, "titulo": "01. Artista A - Uno"}, {"inicio_s": 2, "fin_s": 4, "titulo": "Artista B - Dos"},
        {"inicio_s": 4, "fin_s": None, "titulo": "Tres"}]


def mix(fake_engine, tag, chapters=CHAPTERS):
    url = yt(tag)
    fake_engine.behavior[f"{url}#seconds"] = 6.0
    fake_engine.resolved[url] = {"origen": "youtube", "title": f"Mix {tag}", "artist": "DJ", "duration_s": 6, "disponible": True,
                                 "motivo": None, "candidatos": [], "chapters": chapters}
    return url


def preview(client, url, carpeta, cortar=True):
    job_id = create_youtube(client, [url], carpeta=carpeta, tracklist={"cortar": cortar})
    return job_id, preview_ready(client, job_id)["tracks"][0]


def bajar(client, url, carpeta, cortar=True, excluidos=None):
    job_id, track = preview(client, url, carpeta, cortar)
    if track["ya_descargada"]:
        return track
    if excluidos is not None:
        client.patch(f"/jobs/{job_id}/tracks/{track['track_id']}", json={"accion": "tracklist", "excluidos": excluidos})
    client.post(f"/jobs/{job_id}/confirm")
    return settled(client, job_id, track["track_id"], {"successful"})


def test_cada_tema_se_busca_como_cancion_completa(client, fake_engine, downloads_root):
    url = mix(fake_engine, "busca", SET3)
    _, track = preview(client, url, "Sets Uno")
    temas = track["tracklist"]["temas"]
    # «01. Artista A - Uno» → artista y canción por separado; cada tema con el video que se encontró.
    assert [(t["artista"], t["cancion"]) for t in temas] == [("Artista A", "Uno"), ("Artista B", "Dos"), ("", "Tres")]
    assert all(t["video"]["url"].startswith("https://www.youtube.com/watch?v=tem") for t in temas)
    assert all(t["video"]["thumbnail"] for t in temas)

    done = bajar(client, url, "Sets Uno")
    folder = downloads_root / "prueba" / "sets_uno"
    assert (folder / "Canal - Video.mp3").exists()  # el set completo, siempre
    assert sorted(p.name for p in (folder / "Mix busca").glob("*.mp3")) == [
        "01 - 01. Artista A - Uno.mp3", "02 - Artista B - Dos.mp3", "03 - Tres.mp3"]
    assert fake_engine.song_downloads == ["Uno", "Dos", "Tres"]

    lib = client.get("/library/folders/sets_uno").json()["tracks"][0]["tracklist"]["temas"]
    assert [(t["descargado"], t["fuente"]) for t in lib] == [(True, "video")] * 3
    assert done["guardado"] == {"entero": True, "partes": True}


def test_sin_video_bueno_o_si_falla_se_corta_del_set(client, fake_engine, downloads_root):
    url = mix(fake_engine, "corte", SET3)
    fake_engine.temas.update({"Dos": "sin_video", "Tres": "falla"})
    _, track = preview(client, url, "Sets Dos")
    assert track["tracklist"]["temas"][1]["video"] is None  # en la vista previa: «se corta del set»

    bajar(client, url, "Sets Dos")
    lib = client.get("/library/folders/sets_dos").json()["tracks"][0]["tracklist"]["temas"]
    assert [t["fuente"] for t in lib] == ["video", "corte", "corte"]
    assert len(list((downloads_root / "prueba" / "sets_dos" / "Mix corte").glob("*.mp3"))) == 3


def test_ya_descargada_por_tema(client, fake_engine, downloads_root):
    """Bajar un tema del mix, volver por otro: el primero aparece tachado y no se vuelve a bajar."""
    url = mix(fake_engine, "porte", SET3)
    bajar(client, url, "Sets Tres", excluidos=[1, 2])  # solo el primero
    assert fake_engine.song_downloads == ["Uno"]

    job_id, track = preview(client, url, "Sets Tres")
    assert track["ya_descargada"] is False
    assert track["tracklist"]["ya_descargados"] == 1
    assert [t["ya_descargado"] for t in track["tracklist"]["temas"]] == [True, False, False]

    client.patch(f"/jobs/{job_id}/tracks/{track['track_id']}", json={"accion": "tracklist", "excluidos": [2]})
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})
    assert fake_engine.song_downloads == ["Uno", "Dos"]  # el primero no se volvió a bajar
    folder = downloads_root / "prueba" / "sets_tres" / "Mix porte"
    assert sorted(p.name for p in folder.glob("*.mp3")) == ["01 - 01. Artista A - Uno.mp3", "02 - Artista B - Dos.mp3"]

    # Falta solo el tercero; después de bajarlo, el set entero queda «ya descargado».
    bajar(client, url, "Sets Tres")
    assert fake_engine.song_downloads == ["Uno", "Dos", "Tres"]
    assert bajar(client, url, "Sets Tres")["ya_descargada"] is True

    # En otra carpeta no están: se bajan de nuevo.
    assert preview(client, url, "Sets Otra")[1]["tracklist"]["ya_descargados"] == 0


def test_sin_tracklist_basta_el_set_completo(client, fake_engine):
    url = mix(fake_engine, "solo")
    bajar(client, url, "Sets Cuatro")
    assert bajar(client, url, "Sets Cuatro", cortar=False)["ya_descargada"] is True

    url2 = mix(fake_engine, "solo2")
    bajar(client, url2, "Sets Cuatro", cortar=False)
    _, track = preview(client, url2, "Sets Cuatro")  # bajado entero: con tracklist faltan los temas
    assert track["ya_descargada"] is False and track["tracklist"]["ya_descargados"] == 0


def test_activar_tracklist_en_la_vista_previa_busca_los_temas(client, fake_engine):
    url = mix(fake_engine, "activa")
    job_id, track = preview(client, url, "Sets Cinco", cortar=False)
    assert track["tracklist"]["temas"][0]["buscado"] is False

    r = client.patch(f"/jobs/{job_id}/tracks/{track['track_id']}", json={"accion": "tracklist", "cortar": True})
    assert r.status_code == 200
    track = preview_ready(client, job_id)["tracks"][0]
    assert all(t["buscado"] and t["video"] for t in track["tracklist"]["temas"])


def test_bajar_al_equipo_temas_y_set(client, fake_engine):
    track = bajar(client, mix(fake_engine, "eq"), "Sets Seis")
    tid = track["track_id"]

    r = client.get(f"/tracks/{tid}/file")  # el botón de un tracklist: sus temas
    assert r.headers["content-type"] == "application/zip"
    assert sorted(n.split("/")[-1] for n in zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == ["01 - Uno.mp3", "02 - Dos.mp3"]
    assert client.get(f"/tracks/{tid}/file?modo=entero").headers["content-type"] == "audio/mpeg"
    assert client.get(f"/tracks/{tid}/file?tema=1").status_code == 200


def test_borrar_un_tracklist_deja_los_temas_de_otra_descarga(client, fake_engine, downloads_root):
    url = mix(fake_engine, "borra", SET3)
    first = bajar(client, url, "Sets Siete", excluidos=[1, 2])
    bajar(client, url, "Sets Siete", excluidos=[0, 2])
    client.post("/library/tracks/delete", json={"track_ids": [first["track_id"]]})
    folder = downloads_root / "prueba" / "sets_siete" / "Mix borra"
    assert sorted(p.name for p in folder.glob("*.mp3")) == ["02 - Artista B - Dos.mp3"]


def test_progreso_por_tema_no_lo_pisa_el_hook():
    from musictools.download.progress import Reporter

    reporter = Reporter()
    reporter.set_context(etapa="temas", tema_actual=1, temas_estado=["listo", "bajando"])
    reporter.update(fase="descargando", porcentaje=40.0)
    snap = reporter.snapshot()
    assert snap["porcentaje"] == 40.0 and snap["tema_actual"] == 1 and snap["temas_estado"] == ["listo", "bajando"]


def test_enlace_de_cada_tema_es_lo_que_se_bajo(client, fake_engine):
    url = mix(fake_engine, "enla", SET3)
    fake_engine.temas.update({"Dos": "segundo", "Tres": "sin_video"})
    bajar(client, url, "Sets Ocho")
    temas = client.get("/library/folders/sets_ocho").json()["tracks"][0]["tracklist"]["temas"]
    assert temas[0]["url_descargada"] == temas[0]["video"]["url"]
    # El primer candidato falló y se bajó el segundo: el enlace va al segundo, no al de la vista previa.
    assert temas[1]["url_descargada"].startswith("https://www.youtube.com/watch?v=seg")
    assert temas[1]["url_descargada"] != temas[1]["video"]["url"]
    # Cortado del set: el set, en el minuto donde empieza el tema.
    assert temas[2]["url_descargada"] == f"{url}&t=4s"
