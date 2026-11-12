"""Temas de un tracklist: el capítulo no siempre trae artista y su duración es la del tramo en el set."""

from musictools.download import core
from musictools.download.candidate_scoring import score


def test_el_año_se_quita_del_titulo_del_tema():
    assert core.split_tema_title("Young Turks (1981)") == ("", "Young Turks")
    assert core.split_tema_title("03. Berlin - The Metro [1982]") == ("Berlin", "The Metro")
    assert core.split_tema_title("Major Tom (Coming Home) (1982)") == ("", "Major Tom (Coming Home)")


def test_tema_sin_artista_y_recortado_en_el_set_encuentra_la_cancion_completa():
    # El tramo del set dura 212 s; la canción real, 245 s. Antes este candidato quedaba en −7.
    real = {"title": 'Michael Sembello - Maniac (From "Flashdance")', "channel": "Michael Sembello", "duration": 245}
    assert score(real, "", "Maniac", 212) < 60
    assert score(real, "", "Maniac", 212, tema=True) >= 60


def test_tema_mas_corto_que_el_tramo_si_se_castiga():
    corto = {"title": "Maniac", "channel": "Alguien", "duration": 150}
    completo = {"title": "Maniac", "channel": "Alguien", "duration": 260}
    assert score(corto, "", "Maniac", 212, tema=True) < score(completo, "", "Maniac", 212, tema=True)


def test_spotify_no_cambia():
    c = {"title": "Toto - Africa", "channel": "TOTO", "duration": 295}
    assert score(c, "Toto", "Africa", 295) == score(c, "Toto", "Africa", 295, tema=False)
