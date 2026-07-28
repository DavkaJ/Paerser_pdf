# -*- coding: utf-8 -*-
"""ШАГ 5-хардненинг: B3a GAP-осколок обязан нести кир-БУКВЫ, а не цифры.

Найдено на ШАГ 5: `COVID-19 [2]`->`COVID-19 II`, `virus 2)`->`virus HIV` — цитаты/числа
съедались как «рассыпанное слово». Тихая порча легит-контента. Тест исполняет матчинг
напрямую (быстро, без OCR).
"""
from crparser.engine.latinrecovery import LatinRecoverer


def _selfrepair(blob):
    r = LatinRecoverer.__new__(LatinRecoverer)
    r._tmpl_first = {}
    r._page_hint = {}
    r.prov = []
    r.queue = []
    r._queue_dir = None
    r._locate = lambda needle, hint=None: (None, None, None)
    r._emit = lambda rec, page, uids: r.prov.append(rec)
    r._build_phrase_templates([{"text": blob}])
    return r._phrase_selfrepair(blob, 1, [])


def test_digit_gap_citation_not_matched():
    """`COVID-19 1 2` (цифры цитаты) НЕ становится `COVID-19 II` (шаблон-роман)."""
    blob = "risk COVID-19 II noted COVID-19 II again but COVID-19 1 2 cited here"
    out = _selfrepair(blob)
    assert "COVID-19 1 2" in out, "цифры цитаты съедены как осколок -> порча"
    assert out.count("COVID-19 II") == 2  # исходные 2 чистых не тронуты, третье не добавлено


def test_cyr_letter_gap_still_repaired():
    """Регресс-страховка: настоящий осколок с кир-буквами всё ещё чинится."""
    blob = ("вирус (Hepatitis C virus) и (Hepatitis C virus) но (Hepatitis С у 1 ш з)")
    out = _selfrepair(blob)
    assert "у 1 ш з" not in out
    assert out.count("Hepatitis C virus") == 3
