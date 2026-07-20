# -*- coding: utf-8 -*-
"""Cowork-ревью 3: НЕ авто-латинизировать смешанно-регистровые русские аббревиатуры.

Четвёртый класс тихой порчи: B1 (ocr_eng_perword) прогонял eng-OCR по легит рус. аббревиатуре
и авто-заменял транслит-мусором: `микроРНК`->`MUKPOPHK`, `аутоТГСК`->`ayTOTTCK`. Фикс:
языковой гейт на источник (детекция + `_protected` + `_c5_protected`). Порченая латиница
(`ТгапзрП`->Transpl, `КатоРзку`->Karnofsky) НЕ затронута — источник не рус.аббревиатура.
"""
from crparser.engine.latinrecovery import (
    LatinRecoverer, _is_ru_mixcase_abbr, _cyr_orphan)

ABBR = ["микроРНК", "аутоТГСК", "ЭндоУЗИ", "РостГМУ", "аллоТГСК", "КрасГМУ"]
LATIN = ["ТетрогаШ", "ТгапзрП", "Ыуег", "ЫгЛз", "8рае1И", "КатоРзку"]


def test_mixcase_abbr_detected():
    for c in ABBR:
        assert _is_ru_mixcase_abbr(c) is True, c


def test_corrupt_latin_not_mixcase_abbr():
    for c in LATIN:
        assert _is_ru_mixcase_abbr(c) is False, c


def test_cyr_orphan_excludes_abbr_keeps_corrupt_latin():
    for c in ABBR:
        assert _cyr_orphan(c) is False, "abbrev %s falsely flagged" % c
    # corrupt latin still flagged (recoverable)
    assert _cyr_orphan("ТетрогаШ") is True
    assert _cyr_orphan("КатоРзку") is True


def test_protected_blocks_abbr_allows_corrupt_latin():
    r = LatinRecoverer.__new__(LatinRecoverer)
    r._freq = {}
    for c in ABBR:
        assert r._protected(c) is True, "abbrev %s not protected" % c
    # corrupt latin must NOT be protected (else recovery is lost)
    for c in ["Ыуег", "8рае1И", "ТгапзрП"]:
        assert r._protected(c) is False, "corrupt latin %s wrongly protected" % c
