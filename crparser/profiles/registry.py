#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Реестр клинических рекомендаций (Excel).

Профиль-уровень: загружает Excel-реестр Минздрава и отдаёт строку по ID.
Реестр — самый надёжный источник метаданных (работает даже на сканированных
титулах). Ключ — ID вида «1046_1», совпадающий с именем файла «КР1046_1.pdf».
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import load_workbook


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


class ClinicalRegistry:
    """
    Обёртка над Excel-реестром. Лениво грузит файл, индексирует по ID.

    Использование::

        reg = ClinicalRegistry.load("registry.xlsx")
        row_id, row = reg.find("1046_1")
    """

    def __init__(self, rows: Dict[str, Dict[str, Any]]) -> None:
        self._rows = rows

    # ---- загрузка --------------------------------------------------------

    @classmethod
    def load(cls, xlsx_path: Optional[str]) -> "ClinicalRegistry":
        """Загрузить реестр; при отсутствии/ошибке вернуть пустой (не падаем)."""
        if not xlsx_path or not os.path.exists(xlsx_path):
            return cls({})
        try:
            wb = load_workbook(xlsx_path, read_only=True, data_only=True)
        except Exception:  # noqa: BLE001
            return cls({})
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return cls({})

        header_idx = cls._find_header_row(rows)
        if header_idx is None:
            return cls({})
        headers = [_norm(h) for h in rows[header_idx]]

        indexed: Dict[str, Dict[str, Any]] = {}
        for raw in rows[header_idx + 1:]:
            item = {h: v for h, v in zip(headers, raw) if h}
            raw_id = item.get("ID") or item.get("Id") or item.get("id")
            cr_id = _norm(raw_id)
            if cr_id:
                indexed[cr_id] = item
        return cls(indexed)

    @staticmethod
    def _find_header_row(rows: List[tuple]) -> Optional[int]:
        for idx, row in enumerate(rows[:30]):
            values = [_norm(c).lower() for c in row]
            if "id" in values and any("наименование" in v for v in values):
                return idx
        return None

    # ---- доступ ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def is_empty(self) -> bool:
        return not self._rows

    def find(self, cr_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """
        Найти строку по ID. Поддерживает несовпадение версии: «79» <-> «79_2».
        Возвращает (фактический_id, строка) или (None, None).
        """
        cr_id = _norm(cr_id)
        if not cr_id:
            return None, None
        if cr_id in self._rows:
            return cr_id, self._rows[cr_id]
        for rid, row in self._rows.items():
            if rid.startswith(cr_id + "_") or cr_id.startswith(rid + "_"):
                return rid, row
        # совпадение по базовому номеру (без версии)
        base = cr_id.split("_")[0]
        for rid, row in self._rows.items():
            if rid.split("_")[0] == base:
                return rid, row
        return None, None

    @staticmethod
    def value(row: Dict[str, Any], candidates: List[str]) -> Optional[str]:
        """Достать значение по первому подходящему имени колонки (без регистра)."""
        for cand in candidates:
            for key, val in row.items():
                if _norm(key).lower() == cand.lower():
                    text = _norm(val)
                    if text:
                        return text
        return None
