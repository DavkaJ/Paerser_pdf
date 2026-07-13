# -*- coding: utf-8 -*-
"""Схема (промпт 07, группа 6). Все текущие JSON обязаны проходить cr_schema.json."""
import glob
import json
import os

import pytest

import validate as V

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_28_all_outout_validate_against_schema():
    """Все текущие JSON валидируются против cr_schema.json (невалидный — назвать)."""
    files = glob.glob(os.path.join(ROOT, "outout", "*.json"))
    if not files:
        pytest.skip("нет outout/")
    bad = []
    for jf in files:
        d = json.load(open(jf, encoding="utf-8"))
        if V._schema_error(d) is not None:
            bad.append((os.path.basename(jf), V._schema_error(d)))
    assert not bad, "невалидные по схеме: %s" % bad[:10]


def test_empty_and_broken_fail_schema():
    assert V._schema_error({}) is not None
    assert V._schema_error({"metadata": {}, "sections": "not a list"}) is not None
    # минимальный валидный проходит
    ok = {"metadata": {"source_file": "x", "document_type": "КР", "title": "t", "id": "1"},
          "sections": [], "tables": [], "excluded": {},
          "stats": {"total_chars": 0, "sections_found": 0, "tables_found": 0,
                    "coverage_percent": 0.0}, "warnings": []}
    assert V._schema_error(ok) is None
