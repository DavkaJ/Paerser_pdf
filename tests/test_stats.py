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
