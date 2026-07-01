#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Нормализация порчи извлечённого текста (движок-уровень, доменно-нейтрально).

Три типа порчи, которые встречаются в PDF Минздрава:

  Тип 1 — РАЗРЯДКА: буквы слова разделены пробелами («н е й р о н»), иногда с
          артефактами «_». Детерминированно чиним: серию из >= 4 одиночных букв
          подряд склеиваем обратно в слово. Союзы («и/в/с»), инициалы («А.»),
          римские/арабские номера НЕ трогаем — порог серии (>=4) их отсекает.

  Тип 2 — УДВОЕНИЕ: каждая буква продублирована («ССттааддиияя» = «Стадия»).
          ОПАСНО: в русском есть законные удвоения (ссадина, рассеянный,
          аккумулятор, введение). Схлопываем ТОЛЬКО токен, целиком состоящий из
          пар одинаковых букв, И только если результат — произносимое слово.

  Тип 3 — ГЛИФОВАЯ ПОДМЕНА: латиница отрендерена кириллицей через битый cmap
          («Мапбагб» = Mandard, «Шуогак» = Dworak). Строками НЕ чинится — только
          детектируется и флагается на OCR.

Все функции — чистые (строка -> строка/счётчик), без знания о документе.
"""

from __future__ import annotations

import re
from typing import Tuple

# --- общие классы символов ---------------------------------------------------
_RE_LETTER = re.compile(r"[А-Яа-яЁёA-Za-z]")
_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
_VOWELS_RU = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
# одиночные символы-артефакты, встречающиеся ВНУТРИ разрядки (их выкидываем)
_ARTIFACT = set("_„“”‘’^™°|")


# ============================================================================
# Тип 1 — разрядка (letterspacing)
# ============================================================================

def _is_single_letter(tok: str) -> bool:
    return len(tok) == 1 and bool(_RE_LETTER.match(tok))


def collapse_letterspacing(text: str, min_run: int = 4) -> Tuple[str, int]:
    """Склеить серии одиночных БУКВ (>= min_run подряд) обратно в слово.

    Серия может включать одиночные символы-артефакты («_», «^» …) — они внутри
    серии выкидываются. Цифры серию РАЗРЫВАЮТ (римские/арабские номера, TNM
    «I 1 0 0» не трогаем). Серию склеиваем ТОЛЬКО если в ней есть хотя бы одна
    строчная буква: разрядка слова всегда содержит строчные («н е й р о н»), а
    последовательности заглавных одиночных букв — это, как правило, легитимные
    метки-столбцы/аббревиатуры (TNM «Т N М», «С П И Д»), их не трогаем.
    Возвращает (текст, число_склеек).
    """
    if " " not in text:
        return text, 0
    toks = text.split(" ")
    out, i, fixed = [], 0, 0
    while i < len(toks):
        j, letters, has_lower, cyr, lat = i, 0, False, 0, 0
        while j < len(toks):
            t = toks[j]
            if _is_single_letter(t):
                letters += 1
                has_lower = has_lower or t.islower()
                if _CYR.match(t):
                    cyr += 1
                else:
                    lat += 1
                j += 1
            elif len(t) == 1 and t in _ARTIFACT:
                j += 1
            else:
                break
        # Склеиваем только моноскриптовую серию строчных букв длиной >= min_run.
        # Моноскриптовость отсекает перечисления-метки типа «G, A и M в крови»
        # (латиница A/M вперемешку с русскими и/в), разрядка же слова — один
        # алфавит. Наличие строчной отсекает аббревиатуры заглавными (TNM).
        mono = (cyr == 0 or lat == 0)
        if letters >= min_run and has_lower and mono:
            out.append("".join(t for t in toks[i:j] if _is_single_letter(t)))
            fixed += 1
            i = j
        else:
            out.append(toks[i])
            i += 1
    return " ".join(out), fixed


# ============================================================================
# Тип 2 — равномерное удвоение (letter doubling)
# ============================================================================

def _pronounceable_ru(word: str) -> bool:
    """Грубая проверка «похоже на русское слово»: есть гласная, нет длинного
    прогона согласных, доля гласных в разумных пределах."""
    low = word.lower()
    if not _CYR.search(low):
        return False
    vowels = sum(1 for ch in word if ch in _VOWELS_RU)
    if vowels == 0:
        return False
    ratio = vowels / len(word)
    if ratio < 0.15 or ratio > 0.85:
        return False
    run = 0
    for ch in word:
        if _CYR.match(ch) and ch not in _VOWELS_RU:
            run += 1
            if run > 3:
                return False
        else:
            run = 0
    return True


_RE_AFFIX = re.compile(r"^([^А-Яа-яЁё]*)([А-Яа-яЁё]+)([^А-Яа-яЁё]*)$")


def _collapse_doubled_token(tok: str) -> str | None:
    """Если ядро токена целиком из пар одинаковых букв и схлопывается в
    произносимое слово — вернуть исправленный токен, иначе None."""
    m = _RE_AFFIX.match(tok)
    if not m:
        return None
    lead, core, trail = m.groups()
    if len(core) < 6 or len(core) % 2:
        return None
    # каждая соседняя пара — одинаковые буквы?
    if any(core[k] != core[k + 1] for k in range(0, len(core), 2)):
        return None
    collapsed = core[0::2]
    if not _pronounceable_ru(collapsed):
        return None
    return lead + collapsed + trail


def collapse_doubling(text: str) -> Tuple[str, int]:
    """Схлопнуть равномерно-удвоенные токены. Обычные слова с одним законным
    удвоением («ссадина») НЕ трогаются — они не являются целиком парными."""
    if not text:
        return text, 0
    out, fixed = [], 0
    for tok in text.split(" "):
        c = _collapse_doubled_token(tok)
        if c is not None and c != tok:
            out.append(c)
            fixed += 1
        else:
            out.append(tok)
    return " ".join(out), fixed


# ============================================================================
# Тип 3 — глифовая подмена (не чиним, только детект)
# ============================================================================

# биграммы, типичные для латиницы, отрендеренной кириллическими глифами
# (n->п, d->б, r->г, w->у/ш …). В нормальном русском крайне редки.
_CORRUPT_BIGRAMS = ("пб", "гб", "бг", "уо", "шуо", "апб", "агб", "пбаг", "оаг", "уог")
_RE_MIXED = re.compile(r"[A-Za-z][А-Яа-яЁё]|[А-Яа-яЁё][A-Za-z]")
# известные шкалы, вышедшие кракозяброй
_GLYPH_MARKERS = ("мапбагб", "шуогак", "буогак", "мапёагё")


def _is_glyph_token(tok: str) -> bool:
    core = tok.strip(".,;:()[]«»\"'-—%<>")
    low = core.lower()
    if low in _GLYPH_MARKERS:
        return True
    if len(core) >= 4 and _CYR.fullmatch(core) and \
            sum(1 for b in _CORRUPT_BIGRAMS if b in low) >= 1:
        return True
    return False


def glyph_suspect_count(text: str) -> int:
    """Число токенов с признаками глифовой подмены (кириллица из битого cmap)."""
    return sum(1 for tok in text.split() if _is_glyph_token(tok))


def looks_glyph_corrupted(text: str) -> bool:
    """Регион (заголовок/подпись/тело таблицы) похож на глифовую порчу.

    Порог намеренно консервативный, чтобы НЕ задеть чистые токены латиницы и
    редкие OCR-замены одной буквы («cводы») в нормальных файлах: нужен либо
    явный маркер, либо >= 2 подозрительных токена с заметной плотностью, либо
    массовое смешение скриптов.
    """
    toks = text.split()
    if not toks:
        return False
    suspect = sum(1 for t in toks if _is_glyph_token(t))
    if any(t.strip(".,;:()[]«»\"'-—%<>").lower() in _GLYPH_MARKERS for t in toks):
        return True
    mixed = len(_RE_MIXED.findall(text))
    ratio = suspect / len(toks)
    return mixed >= 3 or (suspect >= 2 and ratio >= 0.03)


# ============================================================================
# Составной нормализатор для одной строки (Тип 1 + Тип 2)
# ============================================================================

def normalize_line(text: str) -> Tuple[str, int, int]:
    """Применить к строке разрядку и удвоение. Глифовую порчу НЕ трогаем.

    Возвращает (текст, число_склеек_разрядки, число_схлопываний_удвоения).
    """
    text, n_sp = collapse_letterspacing(text)
    text, n_db = collapse_doubling(text)
    return text, n_sp, n_db
