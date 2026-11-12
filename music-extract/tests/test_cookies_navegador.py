"""Cookies del navegador (Firefox) solo cuando YouTube pide iniciar sesión."""

import pytest
import yt_dlp

from musictools.download import ytdlp_lib


class FakeYDL:
    """Falla con «Sign in» si no trae cookies del navegador; con ellas, devuelve el video."""

    calls: list[dict] = []

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.calls.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        if self.opts.get("cookiesfrombrowser") is None:
            raise yt_dlp.utils.DownloadError("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.")
        return {"id": "abc", "title": "Con edad"}


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.calls = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_sin_navegador_queda_needs_auth(fake_ydl, monkeypatch, tmp_path):
    monkeypatch.setattr(ytdlp_lib, "BROWSER_COOKIES", None)
    info, motivo = ytdlp_lib.probe_info("https://www.youtube.com/watch?v=abc", cookies_file=tmp_path / "c.txt", js_runtime="node")
    assert info is None and motivo == "needs_auth"
    assert len(fake_ydl.calls) == 1


def test_con_firefox_reintenta_una_vez_con_sus_cookies(fake_ydl, monkeypatch, tmp_path):
    monkeypatch.setattr(ytdlp_lib, "BROWSER_COOKIES", "firefox:/firefox/perfil")
    with ytdlp_lib.browser_cookies_allowed(True):
        info, motivo = ytdlp_lib.probe_info("https://www.youtube.com/watch?v=abc", cookies_file=tmp_path / "c.txt", js_runtime="node")
    assert motivo is None and info["title"] == "Con edad"

    first, retry = fake_ydl.calls
    assert "cookiefile" in first and "cookiesfrombrowser" not in first  # primero, como siempre
    assert retry["cookiesfrombrowser"] == ("firefox", "/firefox/perfil", None, None)
    assert "cookiefile" not in retry


def test_otros_errores_no_usan_el_navegador(monkeypatch, tmp_path):
    calls = []

    class Geo(FakeYDL):
        def extract_info(self, url, download):
            calls.append(self.opts)
            raise yt_dlp.utils.DownloadError("ERROR: This video is not available in your country")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Geo)
    monkeypatch.setattr(ytdlp_lib, "BROWSER_COOKIES", "firefox")
    with ytdlp_lib.browser_cookies_allowed(True):
        _, motivo = ytdlp_lib.probe_info("https://www.youtube.com/watch?v=abc", cookies_file=tmp_path / "c.txt", js_runtime="node")
    assert motivo == "geo_restricted" and len(calls) == 1


def test_formato_de_la_variable(monkeypatch):
    monkeypatch.setattr(ytdlp_lib, "BROWSER_COOKIES", "Firefox")
    assert ytdlp_lib.browser_cookies() == ("firefox", None, None, None)


def test_sin_cuenta_no_se_prestan_las_cookies(fake_ydl, monkeypatch, tmp_path):
    """Por defecto (descargas sin cuenta en owlo) nunca se usan las cookies de la cuenta de Google del dueño."""
    monkeypatch.setattr(ytdlp_lib, "BROWSER_COOKIES", "firefox")
    info, motivo = ytdlp_lib.probe_info("https://www.youtube.com/watch?v=abc", cookies_file=tmp_path / "c.txt", js_runtime="node")
    assert info is None and motivo == "needs_auth" and len(fake_ydl.calls) == 1
    with ytdlp_lib.browser_cookies_allowed(False):
        assert ytdlp_lib.probe_info("https://www.youtube.com/watch?v=abc", cookies_file=tmp_path / "c.txt", js_runtime="node")[1] == "needs_auth"


def test_el_motor_presta_las_cookies_solo_a_trabajos_con_cuenta(client, anon_client, fake_engine, monkeypatch):
    """El hilo de descarga enciende las cookies del navegador solo si el trabajo es de un usuario con cuenta."""
    from test_web_flow import create_youtube, preview_ready, settled, yt

    seen = {}
    original = fake_engine.download

    def spy(url, **kwargs):
        seen[url] = ytdlp_lib._browser_allowed.get()
        return original(url, **kwargs)

    from infrastructure.external import downloader_service
    monkeypatch.setattr(downloader_service, "download", spy)

    for c, tag in ((client, "ckcon"), (anon_client, "cksin")):
        url = yt(tag)
        job_id = create_youtube(c, [url])
        track = preview_ready(c, job_id)["tracks"][0]
        c.post(f"/jobs/{job_id}/confirm")
        settled(c, job_id, track["track_id"], {"successful"})
        seen[tag] = seen.pop(url)

    assert seen == {"ckcon": True, "cksin": False}
