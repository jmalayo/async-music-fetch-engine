"""Títulos de los enlaces de la lista (oEmbed), sin red: `requests.get` se reemplaza."""

import pytest


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        import requests

        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._data


@pytest.fixture
def oembed(monkeypatch):
    from api import links

    calls = []
    responses = {}

    def fake_get(endpoint, params=None, **kwargs):
        calls.append((endpoint, params["url"]))
        return responses.get(params["url"], _Resp({}, 404))

    links._cache.clear()
    monkeypatch.setattr(links.requests, "get", fake_get)
    return calls, responses


def test_youtube_tiktok_y_spotify(anon_client, oembed):
    calls, responses = oembed
    yt, tt, sp = ("https://www.youtube.com/watch?v=abcdefghijk", "https://www.tiktok.com/@a/video/123",
                  "https://open.spotify.com/track/xyz")
    responses[yt] = _Resp({"title": "Mala Pata", "author_name": "Canal", "thumbnail_url": "https://i.ytimg.com/x.jpg"})
    responses[tt] = _Resp({"title": "Mi baile nuevo #fyp #viral", "author_name": "alguien"})
    responses[sp] = _Resp({"title": "Take On Me", "thumbnail_url": "https://i.scdn.co/x.jpg"})

    assert anon_client.get("/links/meta", params={"url": yt}).json() == {
        "origen": "youtube", "titulo": "Mala Pata", "autor": "Canal", "miniatura": "https://i.ytimg.com/x.jpg"}
    assert anon_client.get("/links/meta", params={"url": tt}).json()["titulo"] == "Mi baile nuevo"  # sin hashtags
    meta = anon_client.get("/links/meta", params={"url": sp}).json()
    assert meta["titulo"] == "Take On Me" and meta["autor"] is None  # Spotify no trae el artista
    assert [c[0] for c in calls] == ["https://www.youtube.com/oembed", "https://www.tiktok.com/oembed", "https://open.spotify.com/oembed"]


def test_se_guarda_en_memoria(anon_client, oembed):
    calls, responses = oembed
    url = "https://youtu.be/abcdefghijk"
    responses[url] = _Resp({"title": "Una"})
    anon_client.get("/links/meta", params={"url": url})
    anon_client.get("/links/meta", params={"url": url})
    assert len(calls) == 1


def test_enlace_que_no_se_puede_leer_o_de_otro_sitio(anon_client, oembed):
    assert anon_client.get("/links/meta", params={"url": "https://www.youtube.com/watch?v=privadoxxxx"}).status_code == 404
    assert anon_client.get("/links/meta", params={"url": "https://example.com/x"}).status_code == 422
    assert anon_client.get("/links/meta", params={"url": "http://127.0.0.1:8000/secreto"}).status_code == 422


def test_tiktok_sin_titulo_usa_el_usuario_del_enlace(anon_client, oembed):
    _, responses = oembed
    url = "https://www.tiktok.com/@scout2015/video/6718335390845095173"
    responses[url] = _Resp({"type": "video", "html": "<iframe></iframe>"})  # lo que manda TikTok hoy
    meta = anon_client.get("/links/meta", params={"url": url}).json()
    assert meta == {"origen": "tiktok", "titulo": None, "autor": "@scout2015", "miniatura": None}
