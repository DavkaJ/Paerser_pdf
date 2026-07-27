# -*- coding: utf-8 -*-
"""Гейт пригодности под синтетику (synthready.py) — исполняющие тесты.

Проверяется РЕШЕНИЕ гейта на модельных документах, а не наличие констант: каждый
кейс воспроизводит реальный класс из корпуса (см. комментарии).
"""
import json

import synthready as S


def _doc(sections=None, tables=None, excluded=None, coverage=100.0,
         corruption=None, latin=None, cv2=None, metadata=None):
    sections = sections if sections is not None else [
        {"number": str(i), "title": t, "level": 1, "text": "слово " * 200,
         "children": []}
        for i, t in enumerate(
            ["Краткая информация", "Диагностика", "Лечение",
             "Медицинская реабилитация", "Профилактика",
             "Организация медицинской помощи", "Дополнительная информация"], 1)]
    return {
        "metadata": metadata or {"id": "1_1", "title": "T", "url": "u",
                                 "mkb_codes": ["J45.0"]},
        "sections": sections,
        "tables": tables or [],
        "excluded": excluded or {},
        "stats": {"coverage_percent": coverage,
                  "coverage_v2": cv2 or {"lost_spans": 0, "overcount_ratio": 1.0,
                                         "structured_coverage": 1.0},
                  "corruption": corruption or {}},
        "latin_recovery": latin or {},
    }


def _tier(doc, status=None):
    m = S.measure(doc)
    return S.verdict(m, status)


# ---- карантин ---------------------------------------------------------------

def test_control_chars_quarantined():
    """КР153_2: битый cmap отдаёт код глифа. Валидатор держал его REVIEW только по
    OVERCOUNT, то есть при допуске benign-REVIEW документ уехал бы в обучение."""
    doc = _doc()
    doc["sections"][0]["text"] = "Hhl\x12m\x1adi\x12 R\x07gistr\x1a " * 20
    tier, flags = _tier(doc, "REVIEW")
    assert tier == "C" and flags[0].startswith("control_chars"), (tier, flags)


def test_lost_text_quarantined():
    """Потерянные спаны — текст источника никуда не попал."""
    tier, flags = _tier(_doc(cv2={"lost_spans": 5, "overcount_ratio": 1.0}))
    assert tier == "C" and flags[0].startswith("lost_text")


def test_broken_structure_quarantined():
    """Меньше 5 канонических глав — структура развалилась (КР848_1-класс)."""
    doc = _doc(sections=[{"number": "1", "title": "Краткая информация", "level": 1,
                          "text": "слово " * 900, "children": []}])
    tier, flags = _tier(doc)
    assert tier == "C" and flags[0].startswith("structure")


def test_corrupt_layer_quarantined():
    """Тотальная глифовая порча слоя (pseudo_ascii) — только на OCR."""
    tier, flags = _tier(_doc(corruption={"pseudo_ascii_tokens": 120}))
    assert tier == "C" and flags == ["corrupt_layer"]


# ---- тиры A/B ---------------------------------------------------------------

def test_clean_doc_is_tier_a():
    tier, flags = _tier(_doc(), "PASS")
    assert tier == "A" and flags == [], flags


def test_unresolved_critical_is_flag_not_quarantine():
    """Неразрешённый критический код НЕ выбрасывает документ: он помечается, чтобы
    генератор мог отфильтровать сам (решение пользователя — тир A+B с флагами)."""
    doc = _doc(latin={"unresolved_critical": [{"source_text": "СО2"}]})
    tier, flags = _tier(doc, "REVIEW")
    assert tier == "B"
    assert any(f.startswith("unresolved_critical") for f in flags)


def test_validator_fail_with_full_text_still_shippable():
    """62 FAIL корпуса — все MISSING при coverage 100%: текст на месте, потерян
    только матч записи оглавления. Для синтетики это тир B, а не карантин."""
    tier, flags = _tier(_doc(), "FAIL")
    assert tier == "B" and "validator=FAIL" in flags


def test_table_stub_flagged():
    doc = _doc(tables=[{"page": 1, "number": "1", "caption": "Таблица 1",
                        "raw_text": "шапка"}])
    tier, flags = _tier(doc, "PASS")
    assert tier == "B" and any(f.startswith("table_stubs") for f in flags)


# ---- контракт выгрузки ------------------------------------------------------

def test_cut_drops_references_and_stats():
    """references/toc/front_matter/other, stats и provenance в обучение не идут;
    приложения — идут (в них клинические таблицы и шкалы)."""
    doc = _doc(excluded={"references": [{"title": "", "text": "апа1уз1з"}],
                         "toc": [{"title": "", "text": "оглавление"}],
                         "appendices": [{"title": "Приложение А", "text": "шкала"}]})
    doc["provenance"] = {"spans_total": 10}
    cut = S.cut_document(doc, "A", [], S.measure(doc))
    assert set(cut) == {"metadata", "sections", "tables", "appendices", "quality"}
    assert cut["appendices"][0]["text"] == "шкала"
    assert "апа1уз1з" not in json.dumps(cut, ensure_ascii=False)


def test_garble_measured_only_in_trainable_zone():
    """Порча в references не влияет на вердикт: контракт их режет. Замер по корпусу:
    80% всей порчи живёт именно там (0.92% против 0.08% в разделах)."""
    doc = _doc(excluded={"references": [
        {"title": "", "text": " ".join(["апа1уз1з"] * 500)}]})
    m = S.measure(doc)
    assert m["suspicious"] == 0
    assert S.verdict(m, "PASS")[0] == "A"


def test_flatten_carries_path_and_flags():
    cut = S.cut_document(_doc(), "B", ["recall=6"], S.measure(_doc()))
    units = S.flatten(cut)
    assert len(units) == 7
    u = units[0]
    assert u["doc_id"] == "1_1" and u["tier"] == "B" and u["flags"] == ["recall=6"]
    assert u["path"] == ["1 Краткая информация"]
    assert u["mkb_codes"] == ["J45.0"]


def test_suspicious_token_does_not_flag_legit_russian():
    """Детектор не трогает обычные слова и легитимные русские аббревиатуры —
    иначе гейт ловил бы `типа`/`ЭхоКГ`/`СанПиН` (проверено на корпусе)."""
    for tok in ("типа", "того", "ЭхоКГ", "СанПиН", "аГУС", "Clostridium", "IgG"):
        assert not S.suspicious_token(tok), tok
    for tok in ("М01А", "апа1уз1з", "HBsAgА"):
        assert S.suspicious_token(tok), tok
