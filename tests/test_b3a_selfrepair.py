# -*- coding: utf-8 -*-
"""ШАГ 1 (промпт 17): B3a — внутридокументный self-repair фраз (детерминизм, без OCR).

Пин НЕ читает артефакт — ПЕРЕПАРСИВАЕТ КР1_4 с OCR ON (I30). Ловит §2.3-случай:
рассыпанное `Hepatitis С у 1 ш з` чинится по чистому образцу той же фразы в документе.

КОРРЕКЦИЯ ПЛАНА (проверено по источнику): в §2.3 пять вхождений фразы делятся по
РУССКОЙ букве генотипа — «гепатита В» -> Hepatitis B (×2), «гепатита С» -> Hepatitis C
(×3). Пин промпта «Hepatitis C virus чист во ВСЕХ 5» ОШИБОЧЕН: он превратил бы 2 легит.
гепатита B в C (клиническая порча). Верный исход: осколок `у 1 ш з` убран, occ4 ->
Hepatitis C virus (C=3), Hepatitis B остаётся 2. Различающий гомограф (С->C / В->B)
совпадает с русской буквой генотипа.
"""
import os
import re

import pytest

TESS = os.environ.get("TESSERACT_CMD") or os.path.expanduser("~/Tesseract-OCR/tesseract.exe")
TESSDATA = os.environ.get("TESSDATA_PREFIX") or os.path.expanduser("~/Tesseract-OCR/tessdata")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KR14 = os.path.join(ROOT, "data", "raw", "КР1_4.pdf")


@pytest.fixture(scope="module")
def kr14(request):
    if not os.path.isfile(TESS):
        pytest.skip("нет Tesseract — B3a-тест требует OCR ON (I30)")
    if not os.path.isfile(KR14):
        pytest.skip("нет data/raw/КР1_4.pdf")
    os.environ["TESSERACT_CMD"] = TESS
    os.environ["TESSDATA_PREFIX"] = TESSDATA
    import crparser.engine.ocr as ocr
    mp = pytest.MonkeyPatch()
    mp.setattr(ocr, "_resolve_tesseract", lambda: TESS if os.path.isfile(TESS) else None)
    request.addfinalizer(mp.undo)
    from crparser.engine.parser import DocumentParser
    from crparser.profiles.clinical import ClinicalRecommendationProfile
    return DocumentParser(ClinicalRecommendationProfile(), latin_recovery=True).parse(KR14)


def _zone_text(res):
    parts = []

    def walk(ss):
        for s in ss:
            parts.append((getattr(s, "title", "") or "") + " " + (getattr(s, "text", "") or ""))
            walk(getattr(s, "children", []) or [])
    walk(res.sections)
    for t in res.tables:
        parts.append((getattr(t, "raw_text", "") or "") + " " + (getattr(t, "caption", "") or ""))
    for it in (res.excluded.get("appendices") or []):
        parts.append((it.get("text") or "") + " " + (it.get("title") or ""))
    return "\n".join(parts)


def test_shatter_removed_and_phrase_repaired(kr14):
    z = _zone_text(kr14)
    assert "у 1 ш з" not in z, "осколок `у 1 ш з` остался — B3a не сработал"
    # occ4 «Hepatitis С у 1 ш з» -> «Hepatitis C virus» (плюс 2 чистых) = 3
    assert len(re.findall(r"Hepatitis C virus", z)) >= 3


def test_hepatitis_b_not_corrupted_to_c(kr14):
    """Клиническая безопасность: 2 легит. `Hepatitis B virus` НЕ стали C."""
    z = _zone_text(kr14)
    assert len(re.findall(r"Hepatitis B virus", z)) >= 2, (
        "гепатит B пропал/сконвертирован в C — клиническая порча")
    assert "Hepatitis С" not in z, "остался кир. гомограф С в фразе Hepatitis"
    assert "Hepatitis В" not in z, "остался кир. гомограф В в фразе Hepatitis"


def test_b3a_provenance_auto_and_deterministic(kr14):
    """Провенанс intra_doc_selfrepair присутствует, шаттер разрешён auto (применён)."""
    b3a = [c for c in (kr14.latin_recovery or []) if c.get("method") == "intra_doc_selfrepair"]
    assert b3a, "нет intra_doc_selfrepair коррекций — канал B3a не исполнился"
    shatter = [c for c in b3a if "shattered_latin" in (c.get("why_suspect") or [])
               and c.get("resolved_text") == "Hepatitis C virus"]
    assert shatter, "нет B3a-починки шаттера -> Hepatitis C virus"
    assert all(c.get("applied") == (c.get("decision") == "auto") for c in b3a)
    assert any(c.get("decision") == "auto" and c.get("applied") for c in shatter)
