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


def test_control_chars_review(make_doc):
    """Управляющие C0 вместо букв -> CORRUPTION (модель КР153_2: битый cmap отдаёт
    код глифа, «Chlamydia» -> «Hhl\x12m\x1adi\x12»). Ни один прежний детектор
    (кириллица/псевдо-ASCII/«§») этот класс не видит."""
    garbled = " ".join(["Hhl\x12m\x1adi\x12 R\x07gistr\x1a"] * 10)   # 5 управляющих x 10
    doc = make_doc(sections=[{"number": "1", "title": "Краткая информация",
                              "text": garbled, "level": 1, "children": []}])
    rep = V.Report("x")
    V._corruption_review(rep, doc["stats"], V._all_text(doc), doc["tables"])
    assert any("control_chars" in r for r in rep.reviews), rep.reviews
    assert rep.corruption["control_chars"] == 50


def test_control_chars_counted_in_tables(make_doc):
    """Текст ячеек идёт мимо pdf_reader — гейт обязан смотреть и в tables[].raw_text."""
    doc = make_doc(tables=[{"page": 1, "number": "1", "caption": "Таблица 1",
                            "raw_text": "\x07" * 25, "bbox": [0, 0, 10, 10]}])
    rep = V.Report("x")
    V._corruption_review(rep, doc["stats"], V._all_text(doc), doc["tables"])
    assert any("control_chars" in r for r in rep.reviews), rep.reviews


def test_control_chars_below_threshold_clean(make_doc):
    """Единичные управляющие (шум извлечения) порога не дают."""
    doc = make_doc(sections=[{"number": "1", "title": "Краткая информация",
                              "text": "обычный текст\x07 с одним артефактом",
                              "level": 1, "children": []}])
    rep = V.Report("x")
    V._corruption_review(rep, doc["stats"], V._all_text(doc), doc["tables"])
    assert not any("control_chars" in r for r in rep.reviews)


def test_format_chars_stripped_but_text_kept():
    """Невидимые форматные символы снимаются; буквы и пробелы не трогаются."""
    from crparser.engine.pdf_reader import _clean_line
    assert _clean_line("сли​тно⁠ текст‎") == "слитно текст"
    assert _clean_line("Hhl\x12m\x1adi\x12") == "Hhl\x12m\x1adi\x12"   # C0 НЕ удаляем


def test_tables_missing_review(make_doc):
    """Упомянуто больше номеров таблиц, чем извлечено -> TABLES_MISSING (модель КР401_2)."""
    doc = make_doc(sections=[{"number": "1", "title": "Краткая",
                              "text": "см. таблица 1, таблица 2, таблица 3, таблица 4",
                              "level": 1, "children": []}], tables=[])
    rep = _gate(doc)
    assert any("TABLES_MISSING" in r for r in rep.reviews)
