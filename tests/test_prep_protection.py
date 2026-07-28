# -*- coding: utf-8 -*-
"""Cowork-ревью 2: B3a/B3c НЕ авто-латинизируют одиночные русские предлоги.

Третий класс тихой порчи: `а у`->MRSA (КР1028_1), `у`->Y (КР942_1), `с`(with)->c (~30).
Одиночный строчный рус. предлог (с/у/а/о/и/в/к/я/б/ж) — валидное РУССКОЕ слово, не осколок
и не лат. гомограф. -> needs_review, не auto. ЗАГЛАВНЫЕ генотипы (Hepatitis В/С, Influenza А)
сохраняются (auto). Тесты исполняют каналы напрямую (быстро, без OCR).
"""
from crparser.engine.latinrecovery import LatinRecoverer


def _b3a(blob):
    r = LatinRecoverer.__new__(LatinRecoverer)
    r._tmpl_first = {}
    r._page_hint = {}
    r.prov = []
    r.queue = []
    r._queue_dir = None
    r._locate = lambda n, h=None: (None, None, None)
    r._emit = lambda rec, p, u: r.prov.append(dict(rec, applied=(rec.get("decision") == "auto")))
    r._build_phrase_templates([{"text": blob}])
    out = r._phrase_selfrepair(blob, 1, [])
    return out, r.prov


def _b3c(blob):
    r = LatinRecoverer.__new__(LatinRecoverer)
    r.prov = []
    r._queue_dir = None
    r._page_hint = {}
    r._locate = lambda n, h=None: (None, None, None)
    r._emit = lambda rec, p, u: r.prov.append(dict(rec, applied=(rec.get("decision") == "auto")))
    out = r._homoglyph_in_latin(blob, 1, [])
    return out, r.prov


def test_b3a_all_preposition_gap_rejected():
    """`а у` (два предлога) НЕ заполняется словом шаблона (не MRSA)."""
    blob = ("P aeruginosa MRSA seen and aeruginosa MRSA again but aeruginosa а у cases")
    out, prov = _b3a(blob)
    assert "aeruginosa а у" in out, "русское `а у` заменено словом шаблона — порча"
    assert not any(p["decision"] == "auto" for p in prov)


def test_b3a_preposition_homoglyph_needs_review():
    """`CHOP с`(with) -> needs_review, текст сохраняет `с`."""
    blob = "CHOP c regimen and CHOP c again but CHOP с here"
    out, prov = _b3a(blob)
    assert "CHOP с" in out
    assert prov and all(p["decision"] == "needs_review" and not p["applied"] for p in prov)


def test_b3a_genuine_shatter_with_prep_letter_still_repaired():
    """Осколок `у 1 ш з`=virus (у — предлог, но ш/з нет) всё ещё чинится."""
    blob = "(Hepatitis C virus) и (Hepatitis C virus) но (Hepatitis С у 1 ш з)"
    out, _ = _b3a(blob)
    assert "у 1 ш з" not in out
    assert out.count("Hepatitis C virus") == 3


def test_b3c_lowercase_preposition_needs_review():
    """`FOLFOXIRI с bevacizumab`: `с` -> needs_review, НЕ конвертируется."""
    out, prov = _b3c("FOLFOXIRI с bevacizumab therapy")
    assert " с " in out
    assert prov and all(p["decision"] == "needs_review" and not p["applied"] for p in prov)


def test_b3c_uppercase_genotype_still_auto():
    """Заглавный генотип `В` (Hepatitis B) остаётся auto."""
    out, prov = _b3c("вирус (Hepatitis В virus) крови")
    assert "Hepatitis B virus" in out
    assert prov and prov[0]["decision"] == "auto" and prov[0]["applied"]
