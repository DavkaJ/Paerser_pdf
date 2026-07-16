# -*- coding: utf-8 -*-
"""КОНТРАКТ latin recovery (внешнее ревью 2026-07-16) — тесты, которые ловят ДВА
критических дефекта, которые прошлый регресс-набор пропустил:

  1. `_latin_gate` был МЁРТВЫМ кодом (определение без вызова) -> документы с
     неразрешёнными КРИТИЧЕСКИМИ латинскими сущностями имели PASS.
  2. `needs_review` применялся к тексту наравне с `auto` (`_apply` не смотрел decision)
     -> неподтверждённые замены попадали в корпус, а «уровень 2 = подсказка» был фикцией.

ПОЧЕМУ ПРОШЛЫЙ НАБОР НЕ ЛОВИЛ: OCR глобально выключен в conftest, latin-тесты читают
ГОТОВЫЙ `outout_latin/`, а не перепарсивают PDF -> путь _apply/_latin_gate в них не
исполнялся. Тест ниже (a) перепарсивает РЕАЛЬНЫЙ PDF с ВКЛЮЧЁННЫМ OCR (иначе eng-OCR
канал не работает и needs_review не рождается) и (b) проверяет гейт напрямую.
Правило I30: «гейт работает» = есть ВЫЗОВ, а не определение; тест исполняет путь, а не
читает артефакт.
"""
import os

import pytest

import validate as V

TESS = os.environ.get("TESSERACT_CMD") or os.path.expanduser(
    "~/Tesseract-OCR/tesseract.exe")
TESSDATA = os.environ.get("TESSDATA_PREFIX") or os.path.expanduser(
    "~/Tesseract-OCR/tessdata")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KR14 = os.path.join(ROOT, "data", "raw", "КР1_4.pdf")


def _zone_text(res) -> str:
    """Весь текст обучаемой зоны ParseResult (sections/tables/appendices)."""
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


# ---------------- (1) ГЕЙТ ПОДКЛЮЧЁН: быстрый, без OCR ----------------
def test_latin_gate_is_wired_unresolved_critical_not_pass(make_doc):
    """Документ с неразрешённой КРИТИЧЕСКОЙ латинской сущностью НЕ должен быть PASS.
    Ловит дефект «_latin_gate — мёртвый код» напрямую: строим иначе-проходящий документ,
    вешаем блок latin_recovery.unresolved_critical, валидируем."""
    doc = make_doc()
    doc["latin_recovery"] = {
        "corrections": [],
        "unresolved_critical": [
            {"source_text": "С10AС", "kind": "atc", "page": 5}],
    }
    rep = V.validate_doc("synthetic.pdf", doc, None)
    assert rep.status != "PASS", (
        "документ с unresolved_critical получил %s — _latin_gate не подключён" % rep.status)
    assert any(s.startswith("LATIN_UNRESOLVED") for s in rep.reviews), (
        "нет LATIN_UNRESOLVED в reviews: %s" % rep.reviews)


def test_latin_gate_inert_without_block(make_doc):
    """Без блока latin_recovery гейт инертен (baseline не затронут)."""
    doc = make_doc()
    rep = V.validate_doc("synthetic.pdf", doc, None)
    assert not any(s.startswith("LATIN_UNRESOLVED") or s.startswith("LATIN_REVIEW")
                   for s in (rep.reviews + rep.warns))


# ---------------- (2) needs_review НЕ применяется: интеграция, OCR ON ----------------
@pytest.fixture(scope="module")
def kr14_recovered(request):
    """Перепарс РЕАЛЬНОГО КР1_4 с ВКЛЮЧЁННЫМ OCR и latin_recovery. Одна тяжёлая парс-
    операция на модуль. Skip, если нет Tesseract/PDF."""
    if not os.path.isfile(TESS):
        pytest.skip("нет Tesseract (%s) — интеграционный тест требует OCR" % TESS)
    if not os.path.isfile(KR14):
        pytest.skip("нет data/raw/КР1_4.pdf")
    os.environ["TESSERACT_CMD"] = TESS
    os.environ["TESSDATA_PREFIX"] = TESSDATA
    # conftest занулил резолвер — ВОССТАНАВЛИВАЕМ реальный (env-based) для этого теста
    import crparser.engine.ocr as ocr
    mp = pytest.MonkeyPatch()
    mp.setattr(ocr, "_resolve_tesseract",
               lambda: TESS if os.path.isfile(TESS) else None)
    request.addfinalizer(mp.undo)
    from crparser.engine.parser import DocumentParser
    from crparser.profiles.clinical import ClinicalRecommendationProfile
    res = DocumentParser(ClinicalRecommendationProfile(),
                         latin_recovery=True).parse(KR14)
    return res


def test_ocr_actually_ran(kr14_recovered):
    """Санити: OCR реально работал (иначе eng-OCR канал молчит и тест ничего не проверяет —
    ровно та дыра, что делала прошлый набор фиктивным)."""
    corr = kr14_recovered.latin_recovery
    assert corr, "нет latin_recovery-коррекций — OCR не сработал, тест недействителен"
    assert any(c.get("method") == "ocr_eng" for c in corr), (
        "нет eng-OCR коррекций — OCR-путь не исполнился")


def test_needs_review_source_preserved_in_text(kr14_recovered):
    """КОНТРАКТ: needs_review НЕ применён к тексту — исходный порченый токен ОСТАЛСЯ.
    Ловит дефект «_apply применял оба». Проверяем на РЕАЛЬНОМ выходе."""
    res = kr14_recovered
    zone = _zone_text(res)
    nrs = [c for c in res.latin_recovery if c.get("decision") == "needs_review"]
    if not nrs:
        pytest.skip("нет needs_review коррекций в КР1_4 (маловероятно)")
    leaked = []
    for c in nrs:
        src = c.get("source_text") or ""
        # исходный токен needs_review ОБЯЗАН остаться в тексте (замена не применена)
        if src and src not in zone:
            leaked.append((src, c.get("resolved_text")))
    assert not leaked, (
        "needs_review-замены ПРИМЕНЕНЫ к тексту (исходный токен исчез) — %d: %s"
        % (len(leaked), leaked[:10]))


def test_auto_applied_provenance_flag(kr14_recovered):
    """auto ПРИМЕНЁН (resolved в тексте), и поле provenance `applied` соответствует
    decision (auto->True, needs_review->False)."""
    res = kr14_recovered
    for c in res.latin_recovery:
        assert c.get("applied") == (c.get("decision") == "auto"), (
            "provenance.applied != (decision==auto): %s" % c)
    zone = _zone_text(res)
    autos = [c for c in res.latin_recovery if c.get("decision") == "auto"]
    if autos:
        # хотя бы часть auto-результатов реально в тексте (применены)
        hit = sum(1 for c in autos if (c.get("resolved_text") or "") in zone)
        assert hit > 0, "ни один auto-результат не найден в тексте — auto не применяется?"


def test_needs_review_not_pass_via_gate(kr14_recovered):
    """Документ КР1_4 (битый, с латиницей) через реальный validate_doc: если есть
    unresolved_critical — статус НЕ PASS (гейт подключён и исполнился на реальном выходе)."""
    from crparser.engine.jsonio import JsonWriter
    doc = JsonWriter().to_dict(kr14_recovered)
    lr = doc.get("latin_recovery") or {}
    rep = V.validate_doc("КР1_4.pdf", doc, None)
    if lr.get("unresolved_critical"):
        assert rep.status != "PASS", (
            "КР1_4 с %d unresolved_critical получил PASS — гейт не сработал"
            % len(lr["unresolved_critical"]))
