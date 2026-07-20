# -*- coding: utf-8 -*-
"""Stats / coverage (промпт 07, группы 1 и 3)."""
import math

import pytest

import validate as V


@pytest.mark.parametrize("bad", [-1, 101, 100.5, float("nan"), None, "x"])
def test_03_coverage_out_of_range_is_fail(make_doc, bad):
    """coverage_percent вне [0,100] / NaN / None / не-число -> FAIL."""
    doc = make_doc(total_chars=500)
    doc["stats"]["coverage_percent"] = bad
    rep = V.validate_doc("cov.pdf", doc, None)
    # FAIL в любом случае: числа вне [0,100]/NaN -> COVERAGE_INVALID; None/не-число
    # ловит уже схема (SCHEMA) — тоже fail-closed. Важно, что PASS невозможен.
    assert rep.status == "FAIL"
    assert any(k in f for f in rep.fails
               for k in ("COVERAGE_INVALID", "STATS_MISMATCH", "SCHEMA"))


def test_stats_mismatch_on_counts(make_doc):
    doc = make_doc(total_chars=500)
    doc["stats"]["included_chars"] = 999999
    rep = V.validate_doc("m.pdf", doc, None)
    assert any("STATS_MISMATCH" in f for f in rep.fails)


def test_19_20_coverage_v2_metrics(make_doc):
    """Синтетика 1 симв в sections + 99 в excluded: source_retention ~1.0 (ничего не
    потеряно), structured_coverage ~0.01 (вот измеряемая величина). Обе проверки в
    одном тесте — иначе смысл метрик размывается (промпт 10). Поле overlap — span-union
    (`overlap_spans`, переименовано из line_ids: геометрический якорь, промпт 08)."""
    doc = make_doc(
        sections=[{"number": "1", "title": "", "text": "x", "level": 1, "children": []}],
        excluded={"other": [{"title": "", "text": "y" * 99}]}, total_chars=100)
    v2 = doc["stats"].get("coverage_v2")
    assert v2 is not None
    assert v2["source_retention"] >= 0.99      # ничего не потеряно — это ПРАВИЛЬНО
    assert v2["structured_coverage"] < 0.1     # ~0.01 — вот где виден дефект
    assert "overlap_spans" in v2
    assert v2["structured_coverage"] <= 1.0    # Р4: пересечение обязательно, не >1


# --------------------------------------------------------------------------- #
# Phase 0.1 (I30 #6): обвязка по ГЕОМЕТРИИ, не по длине                        #
# --------------------------------------------------------------------------- #
def test_furniture_by_geometry_not_length():
    """Прямой юнит `_is_furniture`: длина больше НЕ признак обвязки. Стадия «II» в
    центре тела — НЕ обвязка; голый номер, короткий маркер у края и повтор — обвязка."""
    from crparser.engine.stats import _is_furniture
    # НЕ обвязка: короткий НЕ-числовой токен в центральной полосе (стадия/уровень) —
    # прежний порог len<=2 списывал его молча (дефект I30 #6).
    assert _is_furniture("II") is False
    assert _is_furniture("5A") is False
    assert _is_furniture("Клинические рекомендации") is False   # тело, не у края
    # обвязка: голый (дотированный) номер ВНЕ зависимости от позиции — номер страницы,
    # TOC-колонка «1.2.» (I14), номер источника, маркер списка «2.».
    assert _is_furniture("7") is True
    assert _is_furniture("1.2.") is True
    assert _is_furniture("2.3.1.") is True
    assert _is_furniture("") is True
    # обвязка ПО ГЕОМЕТРИИ: короткий маркер у края / повторяющийся колонтитул.
    assert _is_furniture("II", at_edge=True) is True
    assert _is_furniture("Клинические рекомендации", at_edge=True, repeats=True) is True
    assert _is_furniture("•", repeats=True) is True


def test_furniture_geometry_via_coverage_core():
    """Интеграция ЧЕРЕЗ реальный путь `coverage_v2_from_objects` (page_ir с геометрией,
    как у парсера — не синтетика). Номер страницы в колонтитуле и повторяющийся заголовок
    у края -> page_furniture; стадия «II» в центральной полосе -> lost (surface, не списан).
    """
    from crparser.engine.models import make_source_span, make_page_ir, span_uid, Section
    from crparser.engine.stats import coverage_v2_from_objects

    def span(page, bbox, text):
        uid = span_uid(page, bbox)
        return make_source_span(uid, page, bbox, {"native": text}, {}, "native"), uid

    pages, owned = [], []
    for p in range(1, 6):                      # 5 страниц
        spans = []
        header, _ = span(p, (70, 20, 500, 34), "Клинические рекомендации")   # верх. колонтитул (повтор)
        spans.append(header)
        for i in range(3):                     # 3 строки тела (owned секцией)
            s, uid = span(p, (70, 100 + i * 60, 500, 130 + i * 60), "тело строка %d" % i)
            spans.append(s); owned.append(uid)
        pnum, _ = span(p, (280, 770, 300, 784), str(p))                      # номер страницы (низ)
        spans.append(pnum)
        if p == 3:                             # стадия «II» в центре тела (сирота)
            spans.append(span(p, (250, 400, 270, 414), "II")[0])
        pages.append(make_page_ir(p, spans, "native", [], {}))

    sec = Section(number="1", title="Раздел", level=1, text="тело", span_uids=owned)
    v2 = coverage_v2_from_objects([sec], [], {}, pages, accounted_raw=100, total_chars=100)

    assert v2["page_furniture"] == 10          # 5 колонтитулов + 5 номеров страниц
    assert v2["lost_spans"] == 1               # ровно стадия «II» — surface, не списана

