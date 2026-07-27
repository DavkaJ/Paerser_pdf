# -*- coding: utf-8 -*-
"""PUA-нормализация (символьный шрифт -> Unicode по Adobe Symbol). Детерминированно, без
моделей. Юнит: таблица + _pua_fix. Интеграция (OCR-гейт): перепарс реального дока -> провенанс
pua_normalize (покрытые auto, непокрытые needs_review, символ вне таблицы НЕ дропнут)."""
import os
import pytest

from crparser.engine.pua_symbol import symbol_char, SYMBOL
from crparser.engine.latinrecovery import _pua_fix

TESS = os.environ.get("TESSERACT_CMD") or os.path.expanduser("~/Tesseract-OCR/tesseract.exe")
TESSDATA = os.environ.get("TESSDATA_PREFIX") or os.path.expanduser("~/Tesseract-OCR/tessdata")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KR153 = os.path.join(ROOT, "data", "raw", "КР153_2.pdf")


def test_symbol_table_key_mappings():
    """Ключевые коды Adobe Symbol (правильные, не наивный младший байт)."""
    assert symbol_char(0xF0B7) == "•"          # bullet
    assert symbol_char(0xF02D) == "−"          # minus
    assert symbol_char(0xF061) == "α" and symbol_char(0xF062) == "β"
    assert symbol_char(0xF067) == "γ" and symbol_char(0xF06D) == "μ"
    assert symbol_char(0xF0A3) == "≤" and symbol_char(0xF0B3) == "≥"
    assert symbol_char(0xF0B4) == "×"          # multiply = 0xB4 (НЕ 0xD7)
    assert symbol_char(0xF0D7) == "⋅"          # dotmath = 0xD7 (по стандарту, не ×)
    assert symbol_char(0xF07F) is None         # 0x7F вне таблицы -> needs_review


def test_symbol_values_single_char():
    """Все Symbol-значения — ровно 1 кодпоинт (длина текста сохраняется, stats не плывут)."""
    for code, ch in SYMBOL.items():
        assert len(ch) == 1, "0x%X -> %r не одиночный символ" % (code, ch)


def test_pua_fix_covers_and_leaves():
    s = " bullet  beta  uncov"
    new, cov, unc = _pua_fix(s)
    assert new == "• bullet β beta  uncov"     # covered заменены, uncov оставлен
    assert cov[0xF0B7] == 1 and cov[0xF062] == 1
    assert unc[0xE12C] == 1                           # вне таблицы -> unresolved, НЕ дропнут


def test_pua_fix_noop_on_clean():
    s = "обычный текст без PUA"
    new, cov, unc = _pua_fix(s)
    assert new == s and not cov and not unc


@pytest.mark.skipif(not (os.path.isfile(TESS) and os.path.isfile(KR153)),
                    reason="нет Tesseract/КР153_2.pdf — интеграция требует OCR")
def test_engine_pua_pass_provenance():
    """Перепарс КР153_2 (E12C/E46F — E-block вне Symbol): движок оставляет символ И пишет
    needs_review-провенанс pua_normalize (не дропает молча). I30: тест исполняет путь."""
    os.environ["TESSERACT_CMD"] = TESS
    os.environ["TESSDATA_PREFIX"] = TESSDATA
    os.environ["CR_PUA_NORMALIZE"] = "1"
    import crparser.engine.ocr as ocr
    mp = pytest.MonkeyPatch()
    mp.setattr(ocr, "_resolve_tesseract", lambda: TESS if os.path.isfile(TESS) else None)
    try:
        from crparser.engine.parser import DocumentParser
        from crparser.profiles.clinical import ClinicalRecommendationProfile
        res = DocumentParser(ClinicalRecommendationProfile(),
                             latin_recovery=True).parse(KR153)
    finally:
        mp.undo()
    pua = [c for c in res.latin_recovery if c.get("source") == "pua_normalize"]
    assert pua, "нет pua_normalize провенанса — PUA-проход не исполнился"
    unresolved = [c for c in pua if c.get("decision") == "needs_review"]
    assert any(c["source_text"] == "U+E12C" for c in unresolved), (
        "U+E12C (вне Symbol) должен быть needs_review, а не дропнут молча")
    for c in pua:
        if c.get("decision") == "auto":
            assert c.get("resolved_text") and len(c["resolved_text"]) == 1


def test_pua_reaches_excluded_dataclass_items():
    """PUA чинится и в регионах-исключениях (ExcludedItem — ДАТАКЛАСС, не dict).

    Обход, умеющий только dict/list, молча проваливался на нём, и приложения (они
    входят в обучающий контракт) уезжали с сырыми маркерами списка U+F0B7: замер по
    корпусу — 4 078 штук в .excluded.appendices после штатного прогона против 0 у
    патч-скрипта, работавшего по сериализованному JSON. I30: тест исполняет путь."""
    from crparser.engine.latinrecovery import LatinRecoverer
    from crparser.engine.models import ExcludedItem

    rec = LatinRecoverer.__new__(LatinRecoverer)      # без открытия PDF
    rec.prov = []
    excluded = {"appendices": [ExcludedItem(title="Приложение ",
                                            text=" пункт  5")],
                "other": [{"title": "", "text": " dict-элемент"}]}
    rec._pua_normalize_all([], [], excluded, {})

    item = excluded["appendices"][0]
    assert item.text == "• пункт ≤ 5", item.text
    assert item.title == "Приложение •", item.title
    assert excluded["other"][0]["text"] == "• dict-элемент"
    assert any(c["source_text"] == "U+F0B7" and c["decision"] == "auto"
               for c in rec.prov), rec.prov
