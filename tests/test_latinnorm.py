# -*- coding: utf-8 -*-
"""A2-нормализация медкодов (промпт 13b, INVARIANTS I19). Главное — НЕГАТИВНЫЕ пины:
false_substitution = 0 по построению (замена только внутри распознанного шаблона)."""
from crparser.engine.latinnorm import normalize_code_token, normalize_text


# --- ПОЗИТИВНЫЕ: кириллический гомограф в латинском слоте кода -> латиница ---
def test_atc_codes_normalized():
    assert normalize_code_token("C10AС")[0] == "C10AC"       # ATC, Cyrillic С
    assert normalize_code_token("N06AА")[0] == "N06AA"       # Cyrillic А
    assert normalize_code_token("D08АС02")[0] == "D08AC02"   # ATC7
    assert normalize_code_token("В01А")[0] == "B01A"         # Cyrillic В


def test_icd_codes_normalized():
    assert normalize_code_token("С22")[0] == "C22"           # ICD, Cyrillic С
    assert normalize_code_token("Н83.3")[0] == "H83.3"       # Cyrillic Н


def test_tnm_valid_normalized():
    assert normalize_code_token("Т1а")[0] == "T1a"
    assert normalize_code_token("М1")[0] == "M1"
    assert normalize_code_token("Мх")[0] == "Mx"
    assert normalize_code_token("В12")[0] == "B12"           # витамин/код -> латиница


# --- НЕГАТИВНЫЕ: НИЧЕГО не трогаем (false_substitution = 0) ---
def test_real_russian_untouched():
    # настоящие русские слова из одних гомографов НЕ становятся кодами
    for w in ("Сорок", "Соса", "Рост", "Отек", "Хром"):
        assert normalize_code_token(w) is None, w


def test_ambiguous_digit_letter_deferred():
    # «МО» = мед.организация ИЛИ M0 — НЕ угадываем (О↔0 неоднозначно, class C3 -> eng-OCR)
    assert normalize_code_token("МО") is None
    # M3/M5 — невалидный TNM (кубометр/сила мышц), не трогаем
    assert normalize_code_token("М3") is None
    assert normalize_code_token("М5") is None


def test_non_code_untouched():
    for w in ("COVID-19", "рН", "мг", "мл", "сут", "Список", "препараты", "ГЦР", "α", "β"):
        assert normalize_code_token(w) is None, w


def test_already_latin_untouched():
    # чистая латиница без кириллицы — не A2 (font-repair/OCR территория)
    for w in ("N0", "G3", "BCLC", "T2", "037.6"):
        assert normalize_code_token(w) is None, w


def test_normalize_text_preserves_non_codes():
    src = "Стадия Т1а по классификации, витамин В12 и МО не трогаем."
    out, corr = normalize_text(src)
    assert "T1a" in out and "B12" in out
    assert "МО" in out                      # мед.организация сохранена
    assert "Стадия" in out and "классификации" in out
    assert {c["to"] for c in corr} == {"T1a", "B12"}
