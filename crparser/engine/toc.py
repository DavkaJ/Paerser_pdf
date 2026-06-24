# -*- coding: utf-8 -*-
"""
Извлечение и индексирование «Оглавления» документа.

Общий код для ДВУХ потребителей:
  * парсер (Segmenter) — использует оглавление как арбитра при приёме заголовка,
    чтобы не плодить фантомные разделы из нумерованных списков в прозе (баг 4);
  * валидатор (validate.py) — сверяет вывод парсера с оглавлением.

Движок-уровень, но текст оглавления специфичен для КР, поэтому якорь
(«Оглавление/Содержание») передаётся извне (его даёт профиль через ExcludedSpec).
Механика (точки-лидеры, перенос заголовка, оторванный номер) — общая.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Pattern, Tuple

_LEADER = re.compile(r"\.{3,}|_{3,}")
_NUM_HEAD = re.compile(r"^(\d+(?:\.\d+)*)\.?\s*(.*)$")
_NUM_RE = re.compile(r"^\d+(\.\d+)*$")
# строка оглавления начинает новый пункт: «1. …», «2.5.2 …» или оторванный
# многосоставный номер на своей строке («2.4.2.2», заголовок — на следующей).
_ENTRY_START = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+\S|^\s*\d+(?:\.\d+)+\.?\s*$")


def norm(text: str) -> str:
    """Нормализация для сравнения: регистр, ё→е, только буквы/цифры/пробел."""
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def titles_match(expected: str, actual: str) -> bool:
    """Терпимо: регистр/пунктуация/перенос/обрезка/леттерспейсинг/частичное."""
    e, a = norm(expected), norm(actual)
    if not e or not a:
        return True
    if e == a or a.startswith(e) or e.startswith(a):
        return True
    # леттерспейсинг и переносы («п р и з ы в у») — сравниваем без пробелов
    ec, ac = e.replace(" ", ""), a.replace(" ", "")
    if ec == ac or ac.startswith(ec) or ec.startswith(ac):
        return True
    ew, aw = set(e.split()), set(a.split())
    if not ew or not aw:
        return True
    return len(ew & aw) / len(ew | aw) >= 0.6


def _strip_tail(text: str) -> Tuple[str, bool]:
    """Отрезать хвост строки оглавления (точки-лидеры и/или номер страницы)."""
    s = re.sub(r"\s*\.{2,}\s*\d{0,4}\s*$", "", text)   # «....», «.... 46»
    if s != text:
        return s.strip(), True
    s = re.sub(r"\s+\d{1,4}\s*$", "", text)            # « 13» без лидеров
    if s != text and len(text) - len(s) <= 6:
        return s.strip(), True
    return text.strip(), False


def toc_bounds(lines: List[str], anchor: Pattern[str]) -> Optional[Tuple[int, int]]:
    """Границы [start, end] региона оглавления по кластеру точек-лидеров."""
    leaders = [i for i, t in enumerate(lines) if _LEADER.search(t)]
    if not leaders:
        return None
    a = next((i for i, t in enumerate(lines) if anchor.match(t.strip())), None)
    if a is not None:
        near = [i for i in leaders if 0 <= i - a <= 80]
        start = a + 1
    else:
        near = [i for i in leaders if i <= 400]
        start = near[0] if near else None
    if len(near) < 4 or start is None:
        return None
    end = near[0]
    for i in leaders:
        if i < near[0]:
            continue
        if i - end <= 40:          # тот же кластер (учёт пословной вёрстки ToC)
            end = i
        else:
            break
    return start, end


def parse_entries(lines: List[str],
                  anchor: Pattern[str]) -> Optional[List[Tuple[Optional[str], str]]]:
    """
    Разобрать оглавление в список (номер|None, заголовок). Склеивает перенос
    заголовка и оторванный номер («2.4.2.2» + «Лучевые методы …. 16»). None —
    если оглавление не извлечено (тогда потребитель работает по fallback).
    """
    bounds = toc_bounds(lines, anchor)
    if bounds is None:
        return None
    start, end = bounds
    entries: List[Tuple[Optional[str], str]] = []
    buf: List[str] = []

    def flush() -> None:
        full = re.sub(r"\s+", " ", " ".join(buf)).strip()
        buf.clear()
        if not full:
            return
        nm = _NUM_HEAD.match(full)
        if nm and nm.group(1):
            entries.append((nm.group(1), nm.group(2).strip()))
        else:
            entries.append((None, full))

    for line in lines[start:end + 1]:
        text = line.strip()
        if not text or anchor.match(text):
            continue
        if re.fullmatch(r"\d{1,4}", text):     # номер страницы на отдельной строке
            flush()
            continue
        if buf and _ENTRY_START.match(text):   # начался новый пункт — закрыть прежний
            flush()
        clean, closed = _strip_tail(text)
        if clean:
            buf.append(clean)
        if closed:                             # лидеры/страница в конце строки
            flush()
    flush()
    return entries


class TocIndex:
    """
    Индекс оглавления: быстрые ответы «знаем ли такой номер», «подтверждён ли
    номер+заголовок», «есть ли такой заголовок где-нибудь в оглавлении».
    """

    def __init__(self, entries: List[Tuple[Optional[str], str]]) -> None:
        self.entries = entries
        self._by_number: Dict[str, List[str]] = defaultdict(list)
        self._titles: List[str] = []
        for number, title in entries:
            t = (title or "").strip()
            if number and _NUM_RE.match(number):
                self._by_number[number].append(title or "")
            if t:
                self._titles.append(title)

    def known_number(self, number: str) -> bool:
        return number in self._by_number

    def confirmed(self, number: str, title: str) -> bool:
        """Номер есть в оглавлении И один из его заголовков похож на этот."""
        return (number in self._by_number
                and any(titles_match(t, title) for t in self._by_number[number]))

    def title_anywhere(self, title: str) -> bool:
        """Такой заголовок встречается в оглавлении (возможно, под другим номером)."""
        if not norm(title):
            return False
        return any(titles_match(t, title) for t in self._titles)

    @classmethod
    def from_lines(cls, line_texts: List[str],
                   anchor: Pattern[str]) -> Optional["TocIndex"]:
        entries = parse_entries(line_texts, anchor)
        return cls(entries) if entries is not None else None
