# -*- coding: utf-8 -*-
"""ШАГ 3 (промпт 17): B3c — одиночный кир-гомограф между латиницей -> латиница.

Меняем СКРИПТ буквы (mixed-script порча), не семантику. Русское одиночное в русском
окружении НЕ трогаем. Тест исполняет `_homoglyph_in_latin` напрямую (быстро, без OCR).
"""
from crparser.engine.latinrecovery import LatinRecoverer


def _run(text):
    r = LatinRecoverer.__new__(LatinRecoverer)
    r.prov = []
    r.queue = []
    r._queue_dir = None
    r._emit = lambda rec, page, uids: r.prov.append(rec)
    return r._homoglyph_in_latin(text, page=1, span_uids=[])


def test_hepatitis_ve_to_b():
    assert _run("вируса (Hepatitis В virus) в крови") == "вируса (Hepatitis B virus) в крови"


def test_influenza_a_to_a():
    assert _run("the Influenza А virus type") == "the Influenza A virus type"


def test_salmonella_es_to_c_scriptfix():
    """`Salmonella С enterica`: С уже стояла кириллицей -> латиница (script fix)."""
    assert _run("(Salmonella С enterica)") == "(Salmonella C enterica)"


def test_russian_context_untouched():
    """Одиночная кир-буква в РУССКОМ окружении НЕ трогается (соседи не чистая латиница)."""
    src = "вируса гепатита В крови методом"
    assert _run(src) == src


def test_single_letter_neighbor_untouched():
    """Соседи короче 2 символов -> слабый контекст, не трогаем."""
    src = "тип A С B группа"
    assert _run(src) == src


def test_healthy_latin_unchanged():
    src = "performed in vitro and in vivo study"
    assert _run(src) == src


def test_provenance_auto_applied():
    r = LatinRecoverer.__new__(LatinRecoverer)
    r.prov = []
    r._queue_dir = None
    r._emit = lambda rec, page, uids: r.prov.append(rec)
    r._homoglyph_in_latin("Hepatitis В virus", page=1, span_uids=[])
    assert r.prov and r.prov[0]["decision"] == "auto"
    assert r.prov[0]["method"] == "homoglyph_in_latin"
    assert r.prov[0]["source_text"] == "В" and r.prov[0]["resolved_text"] == "B"
