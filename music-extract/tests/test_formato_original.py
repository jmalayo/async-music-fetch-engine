"""Formato de la descarga: MP3 (convertido) u original (Opus o AAC tal como vino, sin convertir)."""

import subprocess

from test_web_flow import create_youtube, preview_ready, settled, yt


def codec(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return out.stdout.strip()


def make_opus_webm(path, seconds=1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
                    "-c:a", "libopus", "-b:a", "96k", str(path)], check=True)
    return path


def test_keep_original_cambia_el_contenedor_sin_convertir(tmp_path):
    from musictools.download import audio

    src = make_opus_webm(tmp_path / "video.webm")
    out = audio.keep_original(src)
    assert out.suffix == ".opus" and codec(out) == "opus" and not src.exists()
    assert audio.media_type(out) == "audio/ogg"


def test_el_formato_se_elige_en_la_vista_previa(client, anon_client, fake_engine):
    job_id = create_youtube(client, [yt("fmt"), yt("fmt")], carpeta="Formatos")
    assert preview_ready(client, job_id)["formato"] == "mp3"  # por defecto

    r = client.patch(f"/jobs/{job_id}", json={"formato": "original"})
    assert r.status_code == 200, r.text
    assert r.json()["formato"] == "original" and r.json()["carpeta"] == "Formatos"  # la carpeta no se toca

    assert client.patch(f"/jobs/{job_id}", json={"formato": "flac"}).status_code == 422
    # Sin cuenta también se puede elegir el formato (pero no la carpeta).
    anon_job = create_youtube(anon_client, [yt("fmtan"), yt("fmtan")])
    preview_ready(anon_client, anon_job)
    assert anon_client.patch(f"/jobs/{anon_job}", json={"formato": "original"}).status_code == 200


def test_descarga_original_guarda_el_opus_y_se_entrega_con_su_tipo(client, fake_engine, monkeypatch, downloads_root):
    from infrastructure.external import downloader_service

    formatos = []
    original = fake_engine.download

    def download(url, *, out_dir, **kwargs):
        formatos.append(kwargs.get("fmt"))
        if kwargs.get("fmt") != "original":
            return original(url, out_dir=out_dir, **kwargs)
        from musictools.download import audio

        audio.keep_original(make_opus_webm(out_dir / "Canal - Original.webm"))
        return {"url": url, "ok": True, "estado": "successful", "mensaje": "ok", "intentos": [], "titulo": None, "artista": None}

    monkeypatch.setattr(downloader_service, "download", download)
    job_id = create_youtube(client, [yt("fmtdl"), yt("fmtdl")], carpeta="Originales")
    tracks = preview_ready(client, job_id)["tracks"]
    client.patch(f"/jobs/{job_id}/tracks/{tracks[1]['track_id']}", json={"accion": "quitar"})
    track = tracks[0]
    client.patch(f"/jobs/{job_id}", json={"formato": "original"})
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})

    assert formatos == ["original"]
    saved = downloads_root / "prueba" / "originales" / "Canal - Original.opus"
    assert saved.exists() and codec(saved) == "opus"
    r = client.get(f"/tracks/{track['track_id']}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/ogg"


def test_original_en_la_nube_usa_su_extension(client, fake_engine, monkeypatch):
    from core import nube
    from infrastructure.external import downloader_service
    from test_nube_r2 import FakeR2

    fake = FakeR2()
    nube.use_client(fake)
    monkeypatch.setenv("NUBE_SIN_COPIA_LOCAL", "1")
    try:
        def download(url, *, out_dir, **kwargs):
            from musictools.download import audio

            audio.keep_original(make_opus_webm(out_dir / "Canal - Nube.webm"))
            return {"url": url, "ok": True, "estado": "successful", "mensaje": "ok", "intentos": [], "titulo": None, "artista": None}

        monkeypatch.setattr(downloader_service, "download", download)
        job_id = create_youtube(client, [yt("fmtnb"), yt("fmtnb")], carpeta="Originales Nube")
        tracks = preview_ready(client, job_id)["tracks"]
        client.patch(f"/jobs/{job_id}/tracks/{tracks[1]['track_id']}", json={"accion": "quitar"})
        track = tracks[0]
        client.patch(f"/jobs/{job_id}", json={"formato": "original"})
        client.post(f"/jobs/{job_id}/confirm")
        settled(client, job_id, track["track_id"], {"successful"})
        nube.wait()

        [key] = [k for k in fake.objects if f"/t/{track['track_id']}-" in k]
        assert key.endswith(".opus") and fake.objects[key]["ContentType"] == "audio/ogg"
        r = client.get(f"/tracks/{track['track_id']}/file", follow_redirects=False)
        assert r.status_code == 307 and f"/{key}?" in r.headers["location"]
    finally:
        nube.wait()
        nube.use_client(None)



def test_cancion_individual_siempre_en_mp3(client, fake_engine):
    job_id = create_youtube(client, [yt("fmtind")])
    assert preview_ready(client, job_id)["formato"] == "mp3"
    r = client.patch(f"/jobs/{job_id}", json={"formato": "original"})
    assert r.status_code == 409 and "MP3" in r.json()["detail"]
