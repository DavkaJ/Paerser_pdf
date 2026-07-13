# -*- coding: utf-8 -*-
"""Структурные гейты валидатора (промпт 04) — прямые юнит-тесты (промпт 07, группа 1)."""
import validate as V


def _gate(doc):
    rep = V.Report("x")
    V._structural_gates(rep, doc, doc.get("stats", {}))
    return rep


def test_collapse_a_few_sections_big_doc(make_doc):
    """<=3 секции при total>20000 -> COLLAPSE (модель КР848_1/КР401_2)."""
    doc = make_doc(sections=[{"number": "1", "title": "A", "text": "x" * 40000,
                              "level": 1, "children": []}], total_chars=54000)
    rep = _gate(doc)
    assert any("COLLAPSE" in r for r in rep.reviews)


def test_collapse_b_dominant_section(make_doc):
    """Крупнейшая секция держит >60% при >3 секциях -> COLLAPSE (модель КР973_1)."""
    secs = [{"number": str(i), "title": "S", "text": "y" * 100, "level": 1, "children": []}
            for i in range(1, 5)]
    secs[0]["text"] = "z" * 5000          # доминирует
    doc = make_doc(sections=secs)
    rep = _gate(doc)
    assert any("COLLAPSE" in r for r in rep.reviews)


def test_canonical_recall_zero_is_fail(make_doc):
    """0 канонических глав при непустом тексте -> FAIL (модель КР848_1)."""
    doc = make_doc(sections=[{"number": None, "title": "XII. Критерии", "text": "x" * 500,
                              "level": 1, "children": []}])
    rep = _gate(doc)
    assert any("CANONICAL_RECALL" in f for f in rep.fails)


def test_canonical_recall_full_is_clean(make_doc):
    """1..7 присутствуют -> ни FAIL, ни REVIEW по CANONICAL_RECALL."""
    secs = [{"number": str(i), "title": t, "text": "x" * 50, "level": 1, "children": []}
            for i, t in enumerate(["Краткая информация", "Диагностика", "Лечение",
                                   "Медицинская реабилитация", "Профилактика",
                                   "Организация медицинской помощи",
                                   "Дополнительная информация"], 1)]
    doc = make_doc(sections=secs)
    rep = _gate(doc)
    assert not any("CANONICAL_RECALL" in x for x in rep.fails + rep.reviews)


def test_ocr_required_on_glyph_tokens(make_doc):
    """glyph_tokens>0 -> OCR_REQUIRED (модель КР396_4)."""
    doc = make_doc(total_chars=500)
    doc["stats"]["corruption"]["glyph_tokens"] = 1
    rep = _gate(doc)
    assert any("OCR_REQUIRED" in r for r in rep.reviews)


def test_residual_pin_body_hit(make_doc):
    """Ключ активной базы пинов в теле -> RESIDUAL_PIN; только в references -> warn."""
    from crparser.engine import ocr_pins
    key = next(k for k in ocr_pins.load_base() if len(k) >= 3)
    body = make_doc(sections=[{"number": "1", "title": "Краткая информация",
                               "text": "текст %s текст" % key, "level": 1, "children": []}])
    assert any("RESIDUAL_PIN" in r for r in _gate(body).reviews)
    # тот же ключ ТОЛЬКО в references -> warning, НЕ review
    ref = make_doc(sections=[{"number": "1", "title": "Краткая", "text": "чисто",
                              "level": 1, "children": []}],
                   excluded={"references": [{"title": "", "text": key}]})
    r = _gate(ref)
    assert not any("RESIDUAL_PIN" in x for x in r.reviews)
    assert any("RESIDUAL_PIN" in w for w in r.warns)


def test_tables_missing_review(make_doc):
    """Упомянуто больше номеров таблиц, чем извлечено -> TABLES_MISSING (модель КР401_2)."""
    doc = make_doc(sections=[{"number": "1", "title": "Краткая",
                              "text": "см. таблица 1, таблица 2, таблица 3, таблица 4",
                              "level": 1, "children": []}], tables=[])
    rep = _gate(doc)
    assert any("TABLES_MISSING" in r for r in rep.reviews)
