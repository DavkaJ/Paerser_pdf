# -*- coding: utf-8 -*-
"""ЗАКРЫТЫЙ НАУЧНЫЙ ЛЕКСИКОН (задача 1, 2026-07-28).

`СО2` с кириллическими С/О — РЕАЛЬНАЯ порча (правильный ответ `CO2` латиницей), но
ни один канал её не чинил: шрифт честен -> font-repair молчит, eng-OCR читает тот же
глиф. Зато ICD-регексп `_looks_code` её матчил, и форма уезжала в `unresolved_critical`
(16 документов из 41 держались одним углекислым газом).

I30: гейт/канал считается работающим ТОЛЬКО если есть вызов и тест, ИСПОЛНЯЮЩИЙ путь.
Ниже — юнит-гейты лексикона + ИНТЕГРАЦИЯ на реальном КР204_2.pdf (перепарс, а не чтение
готового JSON): в нём ровно один unresolved_critical, и это `СО2`.
"""
import os

import pytest

from crparser.engine.latinrecovery import sci_lexicon_form

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KR204 = os.path.join(ROOT, "data", "raw", "КР204_2.pdf")


# ---------------- юнит: что лексикон берёт и, главное, чего НЕ берёт ----------------
@pytest.mark.parametrize("core,expect", [
    ("СО2", "CO2"),          # углекислый газ / CO2-лазер
    ("CО2", "CO2"),          # смешанный: латинская C + кириллическая О
    ("рН", "pH"),
    ("рH", "pH"),
    ("рСО2", "pCO2"),
    ("РСО2", "PCO2"),
    ("РаО2", "PaO2"),
    ("раО2", "paO2"),
    ("РаСО2", "PaCO2"),
    ("SpО2", "SpO2"),
    ("SаO2", "SaO2"),
    ("FiО2", "FiO2"),
    ("р53", "p53"),
    ("р24", "p24"),
    ("р27", "p27"),
    ("р16", "p16"),
])
def test_lexicon_resolves(core, expect):
    assert sci_lexicon_form(core) == expect


@pytest.mark.parametrize("core,why", [
    ("РН", "ALL-CAPS = рус. аббревиатура (ретинопатия недоношенных/рад. нефрэктомия)"),
    ("со2", "рус. «со 2-3 дня после операции» (КР691_2)"),
    ("О2", "анкетный пункт «О2. ТРЕВОГА» (КР451_3), «ИФН-о2» (КР627_3)"),
    ("о2", "то же, строчная"),
    ("р14", "в корпусе НЕ белок: `рТ, р14, рМ` = pN (КР1_4)"),
    ("CO2", "уже чистая латиница — нечего чинить"),
    ("pH", "уже чистая латиница"),
    ("боль", "русское слово"),
    ("рМ", "не в лексиконе"),
    ("", "пусто"),
])
def test_lexicon_rejects(core, why):
    assert sci_lexicon_form(core) is None, why


def test_tnm_region_gate_blocks_ph():
    """В TNM-регионе `рН` — это pN (патологоанатомическая категория), а не pH.
    Единственное ложное вхождение корпуса (КР450_3) снимается именно этим гейтом."""
    assert sci_lexicon_form("рН", tnm_ctx=False) == "pH"
    assert sci_lexicon_form("рН", tnm_ctx=True) is None


def test_lexicon_entries_are_pure_latin():
    """Инвариант списка: каждая запись — чистая латиница (иначе замена бессмысленна)."""
    import re

    from crparser.engine.latinrecovery import _SCI_LEXICON
    for w in _SCI_LEXICON:
        assert not re.search(r"[А-Яа-яЁё]", w), w
        assert w == w.strip() and len(w) >= 2, w


# ---------------- интеграция: путь исполняется на реальном PDF ----------------
@pytest.fixture(scope="module")
def kr204():
    """Перепарс РЕАЛЬНОГО КР204_2 с latin_recovery=True. OCR не нужен: лексикон
    детерминирован и работает ДО визуальных каналов (conftest держит OCR выключенным —
    значит тест доказывает именно лексикон, а не удачное чтение кропа)."""
    if not os.path.isfile(KR204):
        pytest.skip("нет data/raw/КР204_2.pdf")
    from crparser.engine.parser import DocumentParser
    from crparser.profiles.clinical import ClinicalRecommendationProfile
    return DocumentParser(ClinicalRecommendationProfile(),
                          latin_recovery=True).parse(KR204)


def _zone_text(res) -> str:
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
    for it in (res.excluded.get("appendices") or []):
        parts.append((it.get("text") or "") + " " + (it.get("title") or ""))
    return "\n".join(parts)


def test_co2_applied_to_real_document(kr204):
    """КР204_2: `СО2`(кир.) ИСЧЕЗ из обучаемой зоны, `CO2`(лат.) на месте."""
    zone = _zone_text(kr204)
    assert "СО2" not in zone, "кириллическая СО2 осталась в тексте — лексикон не применён"
    assert "CO2" in zone, "латинской CO2 нет в тексте — замена не выполнена"


def test_co2_provenance_is_auto_and_applied(kr204):
    """Провенанс на замену: method=sci_lexicon, decision=auto, applied=True."""
    recs = [c for c in kr204.latin_recovery if c.get("method") == "sci_lexicon"]
    assert recs, "нет ни одной записи sci_lexicon — путь не исполнился"
    srcs = {c["source_text"] for c in recs}
    assert "СО2" in srcs, "СО2 не среди замен лексикона: %s" % sorted(srcs)
    for c in recs:
        assert c["decision"] == "auto" and c.get("applied") is True, c
        assert c["confidence"] == 1.0 and c["is_critical"] is False, c
        assert c["resolved_text"] == sci_lexicon_form(c["source_text"]), c


def test_co2_no_longer_unresolved_critical(kr204):
    """ЦЕЛЬ ЗАДАЧИ: форма больше НЕ висит неразрешённым критическим кодом.
    В КР204_2 до правки был ровно один unresolved_critical — `СО2`."""
    unres = {u.get("source_text") for u in (kr204.latin_unresolved_critical or [])}
    assert "СО2" not in unres, "СО2 всё ещё в unresolved_critical: %s" % unres


def test_co2_not_left_in_verify_queue(kr204):
    """Авторешённая форма не должна одновременно висеть задачей человеку."""
    q = {t.get("source_text") for t in (kr204.latin_queue or [])}
    assert "СО2" not in q, "СО2 авторешён, но остался в очереди верификации"
