#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сериализация результата парсинга в JSON по схеме ТЗ.

Движок-уровень: превращает датаклассы (Section/Table) в словари нужной формы и
пишет файл. Схема фиксирована: metadata / sections / tables / excluded / stats.
"""

from __future__ import annotations

import json
import os
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
        """Атомарно записать JSON: пишем во временный файл рядом, fsync, затем
        os.replace на целевой. На диске всегда либо полный старый, либо полный
        новый файл — обрывов/торн-состояний при прерывании батча не бывает.

        Содержимое и форматирование не меняются (ensure_ascii=False, indent=2),
        поэтому для незатронутых файлов вывод остаётся байт-в-байт прежним.
        """
        self._atomic_dump(self.to_dict(result), out_path)

    @staticmethod
    def _atomic_dump(obj: Any, out_path: str) -> None:
        tmp = "%s.tmp.%d" % (out_path, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, out_path)          # атомарная замена (в т.ч. на Windows)
        except BaseException:
            # при любой ошибке — целевой файл не трогаем, временный убираем
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

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
        out = {
            "page": table.page,
            "number": table.number,
            "caption": table.caption,
            "raw_text": table.raw_text,
            "bbox": list(table.bbox),
        }
        # Поле выводим только для безрамочных таблиц с неуверенным разбором,
        # чтобы JSON обычных (рамочных) таблиц остался байт-в-байт прежним.
        if table.low_confidence:
            out["low_confidence"] = True
        return out
