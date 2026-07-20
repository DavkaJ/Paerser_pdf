# -*- coding: utf-8 -*-
"""Content-start (промпт 12). Пины восстановления начала тела — ИСПОЛНЯЮТ путь (перепарс
реального PDF через сегментер), а не читают артефакт (I30).

ШАГ 2 (START_IN_TOC): оглавление, свёрстанное номерами страниц БЕЗ точек-лидеров
(КР715_2), уводило content_start ВНУТРЬ оглавления — тело «начиналось» с пункта TOC
«3.1.2 Физическая активность 58», главы 1-7 терялись (recall=2, FAIL). Правка (tail-based
TOC при явном маркере «Оглавление») закрывает оглавление на кластере строк-с-номером и
начинает тело ПОСЛЕ него.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")


def _parse(base):
    from crparser.engine.parser import DocumentParser
    from crparser.profiles.clinical import ClinicalRecommendationProfile
    return DocumentParser(ClinicalRecommendationProfile()).parse(
        os.path.join(RAW, base + ".pdf"))


def _canonical_tops(sections):
    tops = {(s.number or "").split(".")[0] for s in sections if s.number}
    return len({n for n in tops if n.isdigit() and 1 <= int(n) <= 7})


def test_kr715_2_body_starts_after_page_tailed_toc():
    """КР715_2 (born-digital): тело больше не начинается пунктом оглавления; 7 канонических
    глав восстановлены (не 2 фрагмента TOC). Исполняет сегментер, не читает outout."""
    base = "КР715_2"
    if not os.path.exists(os.path.join(RAW, base + ".pdf")):
        pytest.skip("нет data/raw/%s.pdf" % base)
    res = _parse(base)
    top = res.sections
    assert top, "нет разделов"
    first_nums = [s.number for s in top[:3]]
    # первый раздел — глава 1, а НЕ пункт оглавления «3.1.2» (как было до фикса)
    assert top[0].number == "1", (
        "первый раздел должен быть главой 1, а не пунктом TOC: %s" % first_nums)
    # заголовок первой главы НЕ оканчивается номером страницы (признак строки TOC)
    import re
    assert not re.search(r"\s\d{1,4}\s*$", (top[0].title or "").strip()), (
        "заголовок первой главы окончен номером страницы — это строка TOC: %r" % top[0].title)
    canon = _canonical_tops(top)
    assert canon >= 5, "восстановлено <5 канонических глав (было 2 фрагмента TOC): %d" % canon


# ШАГ 4 (NUMBERING_MISMATCH): римские главы (I.–XII.) терялись из-за safe-гейта, который
# ГЛУХО отключал римскую модель, когда за римской главой шёл одиночно-арабский подраздел.
# Фикс — перебор моделей и выбор по каноническому recall (не угадывание).
def test_kr1021_1_roman_chapters_recovered():
    """КР1021_1: римские главы V–XI (=канонические 1–7) восстановлены (было recall=2)."""
    if not os.path.exists(os.path.join(RAW, "КР1021_1.pdf")):
        pytest.skip("нет data/raw/КР1021_1.pdf")
    res = _parse("КР1021_1")
    canon = _canonical_tops(res.sections)
    assert canon >= 5, "римская модель не выбрана — глав <5: recall=%d" % canon


def test_kr848_1_no_single_section_collapse():
    """КР848_1 (I10 ложный PASS: 1 секция, coverage 100%): римская структура восстановлена
    → >1 секции и честный не-PASS (не «одна секция на весь документ»)."""
    if not os.path.exists(os.path.join(RAW, "КР848_1.pdf")):
        pytest.skip("нет data/raw/КР848_1.pdf")
    res = _parse("КР848_1")
    assert len(res.sections) > 1, "КР848_1 всё ещё коллапс в 1 секцию"
    assert _canonical_tops(res.sections) >= 3, "структура КР848_1 не восстановлена"
