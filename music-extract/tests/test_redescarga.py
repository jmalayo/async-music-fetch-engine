"""Descargar, borrar de la biblioteca y volver a descargar (con y sin tracklist)."""

from test_mix_entero_o_partes import CHAPTERS, mix
from test_web_flow import create_youtube, preview_ready, settled, yt


def ciclo(client, url, carpeta, **tracklist):
    job_id = create_youtube(client, [url], carpeta=carpeta, tracklist=tracklist)
    track = preview_ready(client, job_id)["tracks"][0]
    return job_id, track


def test_redescarga_sin_tracklist(client, fake_engine, downloads_root):
    url = yt("rsin")
    job_id, track = ciclo(client, url, "Redes A", cortar=False)
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})
    assert client.post("/library/tracks/delete", json={"track_ids": [track["track_id"]]}).status_code == 200

    job_id, track = ciclo(client, url, "Redes A", cortar=False)
    assert track["ya_descargada"] is False, track
    assert track["status"] == "preview", track
    r = client.post(f"/jobs/{job_id}/confirm")
    assert r.status_code == 200, r.text
    done = settled(client, job_id, track["track_id"], {"successful", "failed"})
    assert done["status"] == "successful", done
    assert (downloads_root / "prueba" / "redes_a" / "Canal - Video.mp3").exists()
    lib = client.get("/library/folders/redes_a").json()["tracks"]
    assert [t["track_id"] for t in lib] == [track["track_id"]]


def test_redescarga_con_tracklist(client, fake_engine, downloads_root):
    url = mix(fake_engine, "rtl")
    job_id, track = ciclo(client, url, "Redes B", cortar=True, guardar_completo=False)
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})
    assert client.post("/library/tracks/delete", json={"track_ids": [track["track_id"]]}).status_code == 200
    assert not (downloads_root / "prueba" / "redes_b" / f"Mix rtl").exists()

    job_id, track = ciclo(client, url, "Redes B", cortar=True, guardar_completo=False)
    assert track["ya_descargada"] is False, track
    client.post(f"/jobs/{job_id}/confirm")
    done = settled(client, job_id, track["track_id"], {"successful", "failed"})
    assert done["status"] == "successful", done
    assert sorted(p.name for p in (downloads_root / "prueba" / "redes_b" / "Mix rtl").glob("*.mp3")) == ["01 - Uno.mp3", "02 - Dos.mp3"]


def bajar(client, url, carpeta, **tracklist):
    job_id, track = ciclo(client, url, carpeta, **tracklist)
    if not track["ya_descargada"]:
        client.post(f"/jobs/{job_id}/confirm")
        track = settled(client, job_id, track["track_id"], {"successful", "failed"})
    return job_id, track


def test_bug_copia_ya_descargada_bloqueaba_la_redescarga(client, fake_engine, downloads_root):
    """El caso real: bajar, pedirla otra vez (queda una copia «ya descargada»), borrar el original y volver a pedirla.
    Antes la copia (sin archivo) contaba como descargada y la canción no se podía volver a bajar nunca."""
    for tracklist, tag in (({"cortar": False}, "bsin"), ({"cortar": True, "guardar_completo": False}, "btl")):
        url = mix(fake_engine, tag)
        carpeta = f"Bug {tag}"
        _, original = bajar(client, url, carpeta, **tracklist)
        _, copia = bajar(client, url, carpeta, **tracklist)
        assert copia["ya_descargada"] is True
        # La copia no se repite en la biblioteca.
        assert [t["track_id"] for t in client.get(f"/library/folders/bug_{tag}").json()["tracks"]] == [original["track_id"]]

        client.post("/library/tracks/delete", json={"track_ids": [original["track_id"]]})
        _, nueva = bajar(client, url, carpeta, **tracklist)
        assert nueva["ya_descargada"] is False and nueva["status"] == "successful", (tag, nueva)


def test_archivo_borrado_a_mano_permite_volver_a_bajar(client, fake_engine, downloads_root):
    url = yt("amano")
    _, track = bajar(client, url, "A Mano")
    (downloads_root / "prueba" / "a_mano" / "Canal - Video.mp3").unlink()
    _, again = ciclo(client, url, "A Mano")
    assert again["ya_descargada"] is False


def test_cambiar_carpeta_recalcula_ya_descargada(client, fake_engine, downloads_root):
    url = yt("mover")
    bajar(client, url, "Carpeta Uno")

    job_id, track = ciclo(client, url, "Carpeta Uno")
    assert track["ya_descargada"] is True

    # En otra carpeta no está: pasa a la vista previa para bajarse ahí.
    tracks = client.patch(f"/jobs/{job_id}", json={"carpeta": "Carpeta Dos"}).json()["tracks"]
    assert tracks[0]["ya_descargada"] is False and tracks[0]["status"] == "preview"

    # De vuelta a la primera: otra vez «ya descargada».
    tracks = client.patch(f"/jobs/{job_id}", json={"carpeta": "Carpeta Uno"}).json()["tracks"]
    assert tracks[0]["ya_descargada"] is True

    client.patch(f"/jobs/{job_id}", json={"carpeta": "Carpeta Dos"})
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})
    assert (downloads_root / "prueba" / "carpeta_dos" / "Canal - Video.mp3").exists()
    assert (downloads_root / "prueba" / "carpeta_uno" / "Canal - Video.mp3").exists()


def test_boton_de_descarga_de_un_tracklist_entrega_los_temas(client, fake_engine):
    import io
    import zipfile

    _, track = bajar(client, mix(fake_engine, "zipd"), "Zip Temas", cortar=True, guardar_completo=True)
    r = client.get(f"/tracks/{track['track_id']}/file")  # sin modo: los temas, no el audio completo
    assert r.headers["content-type"] == "application/zip"
    assert sorted(n.split("/")[-1] for n in zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == ["01 - Uno.mp3", "02 - Dos.mp3"]

    r = client.get("/library/folders/zip_temas/zip")
    assert sorted(n.split("/")[-1] for n in zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == ["01 - Uno.mp3", "02 - Dos.mp3"]


def test_en_curso_trae_los_temas_con_su_fin(client, fake_engine):
    url = mix(fake_engine, "curso")
    fake_engine.behavior[url] = "slow"
    job_id, track = ciclo(client, url, "En Curso", cortar=True, guardar_completo=False)
    client.post(f"/jobs/{job_id}/confirm")
    from conftest import wait_until

    job = wait_until(lambda: next((j for j in client.get("/jobs").json() if j["job_id"] == job_id and j.get("descargando")), None))
    temas = job["descargando"][0]["tracklist"]["temas"]
    assert [(t["inicio_s"], t["fin_s"]) for t in temas] == [(0, 3), (3, 6)]
    assert job["descargando"][0]["progreso"]["fase"] == "descargando"
    client.post(f"/jobs/{job_id}/cancel")


def test_bajar_un_tema_suelto_del_tracklist(client, fake_engine):
    url = mix(fake_engine, "tema")
    job_id, track = ciclo(client, url, "Temas Sueltos", cortar=True, guardar_completo=False)
    client.patch(f"/jobs/{job_id}/tracks/{track['track_id']}", json={"accion": "tracklist", "excluidos": [0]})
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})
    tid = track["track_id"]

    r = client.get(f"/tracks/{tid}/file?tema=1")  # el segundo del tracklist, primero entre los cortados
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert "Dos" in r.headers["content-disposition"]
    assert client.get(f"/tracks/{tid}/file?tema=0").status_code == 404  # excluido: no se cortó
    assert client.get(f"/tracks/{tid}/file?tema=9").status_code == 404
