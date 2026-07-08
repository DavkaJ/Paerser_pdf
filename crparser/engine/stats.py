#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Статистика покрытия.

Движок-уровень: считает, какая доля исходного текста «учтена» в результате
(разделы + исключения + таблицы). Высокий coverage_percent = почти ничего не
потеряли при нарезке.
"""

from __future__ import annotations

from typing import Any, Dict, List

from crparser.engine.models import Section, Table


class StatsCalculator:
    """Считает счётчики и процент покрытия по собранному результату."""

    def compute(
        self,
        full_text: str,
        sections: List[Section],
        excluded: Dict[str, List[Dict[str, str]]],
        tables: List[Table],
    ) -> Dict[str, Any]:
        total_chars = len(full_text)
        total_words = len(full_text.split())

        flat = list(self._iter_sections(sections))
        included_chars = sum(len(s.title) + len(s.text) for s in flat)

        excluded_chars = 0
        for bucket in excluded.values():
            for item in bucket:
                excluded_chars += len(item.get("title", "")) + len(item.get("text", ""))

        table_chars = sum(len(t.raw_text) + len(t.caption or "") for t in tables)

        accounted_raw = included_chars + excluded_chars + table_chars
        # дубли подписей/таблиц не должны давать >100%
        accounted_chars = min(accounted_raw, total_chars)
        # Вырожденный случай: в разделы не попало НИЧЕГО (included_chars==0), но
        # excluded/таблицы добирают accounted до ~100% -> фиктивное «покрытие».
        # Такой файл покрытым не считаем: coverage=0. Для нормальных файлов
        # (included_chars>0) формула прежняя — вывод байт-в-байт не меняется.
        if total_chars and included_chars:
            coverage = round(accounted_chars / total_chars * 100, 2)
        else:
            coverage = 0.0

        return {
            "total_chars": total_chars,
            "total_words": total_words,
            "sections_found": len(flat),
            "tables_found": len(tables),
            "included_chars": included_chars,
            "excluded_chars": excluded_chars,
            "table_chars": table_chars,
            "accounted_chars": accounted_chars,
            "coverage_percent": coverage,
        }

    @classmethod
    def _iter_sections(cls, sections: List[Section]):
        for section in sections:
            yield section
            yield from cls._iter_sections(section.children)
