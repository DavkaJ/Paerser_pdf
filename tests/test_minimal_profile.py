# -*- coding: utf-8 -*-
"""
Тест расширяемости (промпт 11, ШАГ 3): движок строит дерево разделов НЕ-КР
документа через нейтральный MinimalProfile, не зная ничего про КР.

Это «главное доказательство» рефакторинга: если такой тест проходит, значит
структурный конвейер (reader → segmenter → sections) профиль-агностичен, а
КР-специфика (канонические главы, нумерация, реестр) — целиком в профиле.
"""
import os

import pytest

fitz = pytest.importorskip("fitz")

from crparser.engine.parser import DocumentParser  # noqa: E402
from crparser.profiles.minimal import MinimalProfile  # noqa: E402


def _make_english_pdf(path: str) -> None:
    """Простой англоязычный документ «1. Introduction / 2. Methods / 3. Results»
    с прозой под каждым заголовком (сегментеру нужен прогон прозы для старта тела)."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    y = 72
    body = ("This section describes the material in plain English prose so that the "
            "segmenter recognises real body text after the heading.")

    def heading(text):
        nonlocal y
        page.insert_text((72, y), text, fontsize=17, fontname="hebo")  # bold, larger
        y += 26

    def prose():
        nonlocal y
        for _ in range(4):
            page.insert_text((72, y), body, fontsize=11, fontname="helv")
            y += 16
        y += 6

    for num, title in ((1, "Introduction"), (2, "Methods"), (3, "Results")):
        heading("%d. %s" % (num, title))
        prose()
    doc.save(path)
    doc.close()


def _titles(sections):
    out = []
    for s in sections:
        out.append((s.get("number"), (s.get("title") or "").strip()))
        out.extend(_titles(s.get("children", [])))
    return out


def test_minimal_profile_contract():
    """MinimalProfile реализует контракт нейтрального профиля (без реестра). Регистрация
    в фабрике живёт в __init__.py, который в этом репо под .gitignore (`_*.py` ловит
    `__init__.py`) — поэтому проверяем прямую конструкцию, не завися от факта регистрации."""
    prof = MinimalProfile()
    assert prof.document_type == "generic_document"
    assert prof.registry is None
    # доменные политики берутся из НЕЙТРАЛЬНЫХ дефолтов base (не КР)
    assert prof.numbering.max_subtitle_len == 200
    assert prof.numbering.uses_roman_chapters is False


def test_engine_parses_english_doc_with_minimal_profile(tmp_path):
    """Движок строит осмысленное дерево разделов англоязычного PDF нейтральным
    профилем — доказательство, что engine не содержит КР-специфичных условий."""
    pdf = str(tmp_path / "english.pdf")
    _make_english_pdf(pdf)

    result = DocumentParser(MinimalProfile()).parse(pdf)
    titles = _titles([_as_dict(s) for s in result.sections])
    joined = " | ".join("%s %s" % (n, t) for n, t in titles)
    # три верхнеуровневых нумерованных раздела распознаны с их английскими названиями
    assert any(n == "1" and t.startswith("Introduction") for n, t in titles), joined
    assert any(n == "2" and t.startswith("Methods") for n, t in titles), joined
    assert any(n == "3" and t.startswith("Results") for n, t in titles), joined
    # метаданные нейтральны (без реестра), document_type от профиля
    assert result.metadata["document_type"] == "generic_document"


def _as_dict(section):
    return {"number": section.number, "title": section.title,
            "children": [_as_dict(c) for c in section.children]}
