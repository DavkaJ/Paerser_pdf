#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сериализация результата парсинга в JSON по схеме ТЗ.

Движок-уровень: превращает датаклассы (Section/Table) в словари нужной формы и
пишет файл. Схема фиксирована: metadata / sections / tables / excluded / stats.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from crparser.engine.models import ParseResult, Section, Table


class JsonWriter:
    """Собирает финальный словарь и пишет его в файл."""

    def to_dict(self, result: ParseResult) -> Dict[str, Any]:
        return {
            "metadata": result.metadata,
            "sections": [self._section(s) for s in result.sections],
            "tables": [self._table(t) for t in result.tables],
            "excluded": result.excluded,
            "stats": result.stats,
            "warnings": result.warnings,
        }

    def write(self, result: ParseResult, out_path: str) -> None:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(result), fh, ensure_ascii=False, indent=2)

    # ---- датакласс -> словарь --------------------------------------------

    def _section(self, section: Section) -> Dict[str, Any]:
        return {
            "number": section.number,
            "title": section.title,
            "level": section.level,
            "text": section.text,
            "children": [self._section(c) for c in section.children],
        }

    @staticmethod
    def _table(table: Table) -> Dict[str, Any]:
        return {
            "page": table.page,
            "number": table.number,
            "caption": table.caption,
            "raw_text": table.raw_text,
            "bbox": list(table.bbox),
        }
