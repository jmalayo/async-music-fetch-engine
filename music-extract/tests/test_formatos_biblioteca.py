"""Formatos en la biblioteca: cada canción dice su extensión, cada carpeta cuenta sus formatos y una canción que ya
está en MP3 se puede bajar también en original (y al revés)."""

from test_formato_original import make_opus_webm
from test_web_flow import create_youtube, preview_ready, settled, yt


def original_engine(monkeypatch, fake_engine):
    from infrastructure.external import downloader_service

    plain = fake_engine.download

    def download(url, *, out_dir, **kwargs):
        if kwargs.get("fmt") != "original":
            return plain(url, out_dir=out_dir, **kwargs)
        from musictools.download import audio

        audio.keep_original(make_opus_webm(out_dir / "Canal - Video.webm"))
        return {"url": url, "ok": True, "estado": "successful", "mensaje": "ok", "intentos": [], "titulo": None, "artista": None}

    monkeypatch.setattr(downloader_service, "download", download)


def download_twice(client, url, carpeta, formato):
    """Dos enlaces (así no es canción individual, que va siempre en MP3): el de relleno se quita."""
    job_id = create_youtube(client, [url, yt("relleno")], carpeta=carpeta)
    tracks = preview_ready(client, job_id)["tracks"]
    client.patch(f"/jobs/{job_id}/tracks/{tracks[1]['track_id']}", json={"accion": "quitar"})
    preview = client.patch(f"/jobs/{job_id}", json={"formato": formato}).json()
    return job_id, preview


def test_la_misma_cancion_en_mp3_y_en_original(client, fake_engine, monkeypatch):
    original_engine(monkeypatch, fake_engine)
    url = yt("dosfmt")

    job_id, preview = download_twice(client, url, "Dos Formatos", "mp3")
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, preview["tracks"][0]["track_id"], {"successful"})

    # En MP3 ya está: pedirla otra vez en MP3 la marca «ya descargada»…
    _, again = download_twice(client, url, "Dos Formatos", "mp3")
    assert again["tracks"][0]["ya_descargada"] is True
    # …pero en original no: se puede bajar también así.
    job_id, other = download_twice(client, url, "Dos Formatos", "original")
    assert other["tracks"][0]["ya_descargada"] is False
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, other["tracks"][0]["track_id"], {"successful"})

    folder = client.get("/library/folders/dos_formatos").json()
    assert sorted(t["extension"] for t in folder["tracks"] if t["archivo_disponible"]) == ["mp3", "opus"]
    card = next(c for c in client.get("/library").json()["carpetas"] if c["slug"] == "dos_formatos")
    assert card["formatos"] == {"mp3": 1, "opus": 1}
