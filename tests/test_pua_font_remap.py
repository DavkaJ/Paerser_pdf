# -*- coding: utf-8 -*-
"""PUA ВНЕ таблицы Adobe Symbol -> Unicode ПО СЕМЕЙСТВУ ШРИФТА (задача 2, 2026-07-28).

Остаток после Symbol — 1 579 глифов в 47 док., 8 кодпоинтов. Разрешать их кодпоинтом
НЕЛЬЗЯ: один и тот же U+F0EA — это `⬇` в Wingdings (КР954_1, таблица лабораторных
сдвигов) и `★` в Wingdings 2 (КР661_2, маркер сноски). Ключ — СЕМЕЙСТВО шрифта,
которым глиф нарисован; каждое правило получено рендером глифа из реального PDF
(см. `_corpus/_pua_font_remap.md`).

I30: интеграционные тесты ПЕРЕПАРСИВАЮТ реальные PDF, а не читают готовый JSON.
OCR для этого пути не нужен (ремап детерминирован) — conftest держит его выключенным,
значит тест доказывает именно ремап, а не удачное чтение кропа.
"""
import os

import pytest

from crparser.engine.pua_symbol import FONT_PUA, font_family, font_pua_char

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KR661 = os.path.join(ROOT, "data", "raw", "КР661_2.pdf")   # Wingdings 2 -> ★
KR660 = os.path.join(ROOT, "data", "raw", "КР660_2.pdf")   # ДВА семейства на один кодпоинт


# ------------------------------- юнит -------------------------------
@pytest.mark.parametrize("name,fam", [
    ("AAAAAM+SymbolMT", "symbol"),
    ("SymbolMT", "symbol"),
    ("Symbol Regular", "symbol"),
    ("ABCDEF+Wingdings-Regular", "wingdings"),
    ("Wingdings Regular", "wingdings"),
    ("Wingdings 2 Regular", "wingdings 2"),     # цифра — ЧАСТЬ семейства
    ("Wingdings 2", "wingdings 2"),
    ("OpenSymbol", "opensymbol"),
    ("CIDFont+F10", "cidfont"),                 # безымянный сабсет -> правила нет
    ("", ""),
])
def test_font_family_normalization(name, fam):
    assert font_family(name) == fam


def test_same_codepoint_differs_by_family():
    """ГЛАВНОЕ СВОЙСТВО: U+F0EA — стрелка вниз в Wingdings и звезда в Wingdings 2.
    Кодпоинтный ремап превратил бы звезду-сноску в стрелку."""
    assert font_pua_char("wingdings", 0xF0EA) == "⬇"
    assert font_pua_char("wingdings 2", 0xF0EA) == "★"
    assert font_pua_char("wingdings", 0xF0EA) != font_pua_char("wingdings 2", 0xF0EA)


def test_unknown_family_has_no_rule():
    """Семейство без правила (Times New Roman, безымянный сабсет) -> None,
    кодпоинт остаётся needs_review."""
    assert font_pua_char("times", 0xF0FC) is None
    assert font_pua_char("cidfont", 0xF0FC) is None
    assert font_pua_char("wingdings", 0xF0B7) is None    # покрыт таблицей Symbol


def test_all_rules_are_single_char():
    """Каждое правило — ровно 1 кодпоинт: длина текста сохраняется, stats не плывут."""
    for fam, table in FONT_PUA.items():
        for cp, ch in table.items():
            assert len(ch) == 1, "%s U+%04X -> %r" % (fam, cp, ch)
            assert 0xE000 <= cp <= 0xF8FF, "%s: U+%04X не PUA" % (fam, cp)
            assert not (0xE000 <= ord(ch) <= 0xF8FF), "%s: результат снова PUA" % fam


# ---------------------- интеграция: путь исполняется ----------------------
def _parse(path):
    from crparser.engine.parser import DocumentParser
    from crparser.profiles.clinical import ClinicalRecommendationProfile
    return DocumentParser(ClinicalRecommendationProfile(),
                          latin_recovery=True).parse(path)


def _zone(res):
    parts = []

    def walk(ss):
        for s in ss:
            parts.append((getattr(s, "title", "") or "") + " "
                         + (getattr(s, "text", "") or ""))
            walk(getattr(s, "children", []) or [])
    walk(res.sections)
    for t in res.tables:
        parts.append((getattr(t, "raw_text", "") or "") + " "
                     + (getattr(t, "caption", "") or ""))
    for items in (res.excluded or {}).values():
        for it in items:
            if isinstance(it, dict):
                parts.append((it.get("text") or "") + " " + (it.get("title") or ""))
            else:
                parts.append((getattr(it, "text", "") or "") + " "
                             + (getattr(it, "title", "") or ""))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def kr661():
    if not os.path.isfile(KR661):
        pytest.skip("нет data/raw/КР661_2.pdf")
    return _parse(KR661)


@pytest.fixture(scope="module")
def kr660():
    if not os.path.isfile(KR660):
        pytest.skip("нет data/raw/КР660_2.pdf")
    return _parse(KR660)


def test_wingdings2_star_applied(kr661):
    """КР661_2: U+F0EA нарисован Wingdings 2 -> `★` (а НЕ `⬇`), провенанс auto."""
    recs = [c for c in kr661.latin_recovery
            if c.get("source") == "pua_normalize" and c["source_text"] == "U+F0EA"]
    assert recs, "нет провенанса для U+F0EA — путь ремапа не исполнился"
    for c in recs:
        assert c["decision"] == "auto" and c.get("applied") is True, c
        assert c["resolved_text"] == "★", c
        assert c["rule"] == "font:wingdings 2", c
    assert "\uf0ea" not in _zone(kr661), "исходный PUA остался в тексте"
    assert "★" in _zone(kr661)


def test_ambiguous_family_stays_needs_review(kr660):
    """КР660_2 рисует U+F0EA ДВУМЯ семействами (Wingdings ⬇ в таблице и Wingdings 2 ★
    в сноске). Обоснования нет -> кодпоинт НЕ трогаем, он остаётся needs_review и
    ОСТАЁТСЯ В ТЕКСТЕ (молча не дропаем). Соседний U+F0E9 однозначен -> решён."""
    pua = {c["source_text"]: c for c in kr660.latin_recovery
           if c.get("source") == "pua_normalize"}
    amb = pua.get("U+F0EA")
    assert amb, "нет провенанса для U+F0EA"
    assert amb["decision"] == "needs_review" and amb.get("applied") is False, amb
    assert amb["resolved_text"] is None, amb
    zone = _zone(kr660)
    assert "\uf0ea" in zone, "неоднозначный глиф дропнут молча"
    ok = pua.get("U+F0E9")
    assert ok and ok["decision"] == "auto" and ok["resolved_text"] == "⬆", ok


def test_no_length_drift(kr661):
    """Ремап 1:1 — все auto-замены pua_normalize односимвольные (stats не плывут)."""
    for c in kr661.latin_recovery:
        if c.get("source") == "pua_normalize" and c.get("decision") == "auto":
            assert len(c["resolved_text"]) == 1, c
