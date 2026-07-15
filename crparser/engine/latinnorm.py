#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A2-нормализация (промпт 13b): кириллический гомограф в латинском слоте РАСПОЗНАННОГО
медицинского кода → латинская буква. Отдельный класс порчи A2 (INVARIANTS I19): шрифт
ЧЕСТЕН, /ToUnicode честен, но в ИСТОЧНИКЕ кириллический двойник (автор набрал с рус.
раскладки). Ни font-repair (шрифт здоров), ни eng-OCR (глиф реально кириллический) не
чинят — только шаблон сущности.

**false_substitution = 0 ПО ПОСТРОЕНИЮ.** Замена срабатывает ТОЛЬКО когда:
  1. в токене есть кириллица;
  2. после замены ТОЛЬКО гомографов-БУКВ на латиницу не остаётся ни одной кириллицы
     (иначе это настоящее русское слово — не трогаем);
  3. результат — валидный медкод (ATC/МКБ/TNM) по строгому шаблону.
За пределами шаблона портить нечего. Цифро-буквенные неоднозначности (О↔0, I↔1 — «МО»
это «мед. организация» ИЛИ «M0»?) СЮДА НЕ входят: их разрешает eng-OCR по кропу (класс C3).
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Кириллические буквы, ВИЗУАЛЬНО ИДЕНТИЧНЫЕ латинским (гомографы). Только они — и только
# буква→буква. Цифро-буквенные (О→0) СЮДА НЕ входят (неоднозначны, class C3 → eng-OCR).
_CYR2LAT = {
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
}
_CYR = re.compile(r"[А-Яа-яЁё]")
_LEAD = "«»\"'([{"
_TRAIL = ".,:;!?»\"')]}"

# Строгие шаблоны медкодов (на УЖЕ латинизированной форме). Полное совпадение токена.
_CODE_RE = (
    ("ATC7", re.compile(r"^[A-Z]\d{2}[A-Z]{2}\d{2}$")),   # L04AA01
    ("ATC5", re.compile(r"^[A-Z]\d{2}[A-Z]{2}$")),        # L04AA / S01EC
    ("ATC4", re.compile(r"^[A-Z]\d{2}[A-Z]$")),           # L04A
    ("ICD",  re.compile(r"^[A-Z]\d{2}(?:\.\d{1,2})?$")),  # C22, D37.6, H40.06
    # TNM только с ДОПУСТИМЫМИ значениями (T0-4, N0-3, M0-1, G1-4 + x/is): иначе «М3»
    # (кубометр/сила мышц по MRC) ложно ушёл бы в «M3». Строгие пределы = fsr защита.
    ("TNM",  re.compile(r"^p?(?:T(?:is|[0-4][a-d]?|[xX])|N[0-3xX]|M[01xX]|G[1-4xX])$")),
)


def normalize_code_token(token: str) -> Optional[Tuple[str, str, str]]:
    """Если токен — медкод с кириллическими гомографами → вернуть (исправленный,
    шаблон, ядро-как-было); иначе None. Пунктуация по краям сохраняется вызывающим."""
    core = token.strip(_LEAD + _TRAIL)
    if len(core) < 2 or not _CYR.search(core):
        return None                                   # нет кириллицы → не A2 (font-repair/OCR)
    latin = "".join(_CYR2LAT.get(ch, ch) for ch in core)
    if _CYR.search(latin):
        return None                                   # осталась кириллица → настоящее рус. слово
    if latin == core:
        return None
    for name, rx in _CODE_RE:
        if rx.fullmatch(latin):
            return latin, name, core
    return None


# Гомографы ЦИФР: буква в позиции цифры -> цифра. Применять ТОЛЬКО в цифровом слоте
# распознанного шаблона И только если результат — валидное значение (тогда fsr=0:
# грамматика TNM после M допускает лишь 0/1/x, значит «MO» невалидно, а «M0» валидно →
# O обязана быть нулём; это следствие шаблона, а не догадка).
_L2D = {"O": "0", "o": "0", "l": "1", "I": "1", "i": "1", "L": "1", "S": "5", "s": "5",
        "B": "8", "G": "6", "g": "6", "З": "3", "з": "3", "б": "6", "Б": "6",
        "Ь": "6", "ь": "6"}
_RX_TNM_C = re.compile(r"^p?(?:T(?:is|[0-4][a-d]?|[xX])|N[0-3xX]|M[01xX]|G[1-4xX])$")
_RX_ICD_C = re.compile(r"^[A-Z]\d{2}(?:\.\d{1,2})?$")
_RX_ATC_C = re.compile(r"^[A-Z]\d{2}[A-Z]{2}(?:\d{2})?$")


def _to_digit(ch: str, allow: str = "") -> str:
    if ch in allow or ch.isdigit():
        return ch
    return _L2D.get(ch, ch)


def coerce_entity(text: str, kind: str) -> Optional[str]:
    """Кир-буквы -> латиница + гомограф ЦИФРЫ в ЦИФРОВОМ слоте -> цифра, ВНУТРИ шаблона.
    Буквенные слоты остаются буквами (цифра там неоднозначна: 0->D или 0->O — это зона
    eng-OCR, не шаблона). Возврат: валидная форма или None. fsr=0 (только валидный итог)."""
    t = "".join(_CYR2LAT.get(c, c) for c in text.strip("().,;:«»[] "))
    if kind == "tnm":
        m = re.match(r"^(p?)([TNMG])(.)([a-d]?)$", t)
        if not m:
            return None
        pfx, L, d, suf = m.groups()
        cand = pfx + L + _to_digit(d, "xX") + suf
        return cand if _RX_TNM_C.match(cand) else None
    if kind == "icd":
        m = re.match(r"^([A-Za-z])(.)(.)((?:\.\d{1,2})?)$", t)
        if not m:
            return None
        L, a, b, tail = m.groups()
        cand = L.upper() + _to_digit(a) + _to_digit(b) + tail
        return cand if _RX_ICD_C.match(cand) else None
    if kind == "atc":
        m = re.match(r"^([A-Za-z])(.)(.)([A-Za-z]{2})((?:.{2})?)$", t)
        if not m:
            return None
        L, a, b, ll, dd = m.groups()
        dd2 = "".join(_to_digit(c) for c in dd)
        cand = L.upper() + _to_digit(a) + _to_digit(b) + ll.upper() + dd2
        return cand if _RX_ATC_C.match(cand) else None
    return None


def normalize_text(text: str) -> Tuple[str, List[dict]]:
    """Пройти по токенам текста, A2-нормализовать медкоды. Вернуть (новый_текст,
    список коррекций для provenance). Разбиение сохраняет разделители."""
    if not text:
        return text, []
    corrections: List[dict] = []
    out: List[str] = []
    for piece in re.split(r"(\s+)", text):
        if not piece or piece.isspace():
            out.append(piece)
            continue
        res = normalize_code_token(piece)
        if res is None:
            out.append(piece)
            continue
        latin, name, core = res
        fixed = piece.replace(core, latin, 1)
        out.append(fixed)
        corrections.append({"from": core, "to": latin, "template": name})
    return "".join(out), corrections
