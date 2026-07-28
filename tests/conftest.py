# -*- coding: utf-8 -*-
"""
Фикстуры регресс-набора (промпт 07a).

OCR принудительно ОТКЛЮЧЁН на уровне conftest: тесты НЕ требуют Tesseract/GPU
(требование промпта). parse_toc при этом работает по нативному текстовому слою —
детерминированно и быстро. Отключение — до импорта validate/pdf_reader, чтобы
_resolve_tesseract уже возвращал None.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --- отключить OCR ДО того, как что-либо его резолвит ---
from crparser.engine import ocr as _ocr  # noqa: E402
_ocr._resolve_tesseract = lambda: None
try:
    _ocr._stack_sig_cache.clear()
except Exception:  # noqa: BLE001
    pass

OUTOUT = os.path.join(ROOT, "outout")
RAW = os.path.join(ROOT, "data", "raw")


def has_fixture(base: str) -> bool:
    return (os.path.exists(os.path.join(OUTOUT, base + ".json"))
            and os.path.exists(os.path.join(RAW, base + ".pdf")))


def load_outout(base: str) -> dict:
    with open(os.path.join(OUTOUT, base + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def outout_doc():
    """Загрузить эталонный JSON из outout/ по базовому имени."""
    def _load(base):
        if not has_fixture(base):
            pytest.skip("нет фикстуры %s (нужны outout/ + data/raw/)" % base)
        return load_outout(base)
    return _load


@pytest.fixture
def validate_base():
    """Прогнать валидатор по готовому JSON + оглавлению реального PDF (OCR off)."""
    import validate as V

    def _run(base):
        if not has_fixture(base):
            pytest.skip("нет фикстуры %s (нужны outout/ + data/raw/)" % base)
        doc = load_outout(base)
        toc = V.parse_toc(os.path.join(RAW, base + ".pdf"))
        return V.validate_doc(base + ".pdf", doc, toc)
    return _run


def _consistent_stats(sections, excluded, tables, total_chars=None):
    from crparser.engine.stats import coverage_v2_from_doc
    inc = sum(len(s.get("title", "")) + len(s.get("text", "")) for s in sections)
    exc = sum(len(i.get("title", "")) + len(i.get("text", ""))
              for b in excluded.values() for i in b)
    tab = sum(len(t.get("raw_text", "")) + len(t.get("caption") or "") for t in tables)
    total = total_chars if total_chars is not None else (inc + exc + tab)
    accounted = min(inc + exc + tab, total)
    cov = round(accounted / total * 100, 2) if (total and inc) else 0.0
    stats = {"total_chars": total, "total_words": max(1, total // 6),
             "sections_found": len(sections), "tables_found": len(tables),
             "included_chars": inc, "excluded_chars": exc, "table_chars": tab,
             "accounted_chars": accounted, "coverage_percent": cov,
             "corruption": {"spacing_fixed": 0, "doubling_fixed": 0, "glyph_tokens": 0,
                            "glyph_regions": 0, "pseudo_ascii_tokens": 0}}
    # coverage_v2 по синтетике: без provenance каждый unit — один псевдо-span весом
    # в свои символы (тот же путь, что использует парсер при пустом page_ir).
    # sections здесь УЖЕ плоский (make_doc передаёт _flat) — детей обнуляем, чтобы
    # рекурсивный обход coverage_v2_from_doc не посчитал узлы дважды.
    flat_nodes = [{**s, "children": []} for s in sections]
    stats["coverage_v2"] = coverage_v2_from_doc(
        {"sections": flat_nodes, "excluded": excluded, "tables": tables, "stats": stats})
    return stats


@pytest.fixture
def make_doc():
    """Фабрика синтетических документов, проходящих схему, с согласованными stats."""
    def _make(sections=None, excluded=None, tables=None, total_chars=None,
              metadata=None, warnings=None):
        sections = sections if sections is not None else [
            {"number": "1", "title": "Краткая информация", "level": 1,
             "text": "x" * 400, "children": []}]
        excluded = excluded or {}
        tables = tables or []
        doc = {
            "metadata": metadata or {"source_file": "x.pdf", "document_type": "КР",
                                     "title": "T", "id": "1_1"},
            "sections": sections, "tables": tables, "excluded": excluded,
            "warnings": warnings or [],
            "stats": _consistent_stats(_flat(sections), excluded, tables, total_chars),
        }
        return doc
    return _make


def _flat(sections):
    out = []
    for s in sections:
        out.append(s)
        out.extend(_flat(s.get("children", [])))
    return out
