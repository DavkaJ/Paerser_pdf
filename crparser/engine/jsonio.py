#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сериализация результата парсинга в JSON по схеме ТЗ.

Движок-уровень: превращает датаклассы (Section/Table/ExcludedItem) в словари нужной
формы и пишет файл. Схема фиксирована и расширяется ТОЛЬКО аддитивно (промпт 08):
существующие поля metadata / sections / tables / excluded / stats / warnings не
меняются ни по имени, ни по порядку; provenance-поля добавляются В КОНЕЦ каждого
объекта, а сводка по спанам — отдельным top-level блоком "provenance".

`include_provenance=False` (флаг CLI `--no-provenance`) даёт КОМПАКТНЫЙ выход —
ровно прежнюю форму, без единого нового поля (совпадает байт-в-байт с baseline).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from crparser.engine.models import ParseResult, PageIR, Section, Table

# Сколько расхождений каналов выводить поимённо (общее число — отдельным полем).
_DISAGREEMENTS_CAP = 200


class JsonWriter:
    """Собирает финальный словарь и пишет его в файл.

    `include_provenance` — включать ли provenance (по умолчанию да). При False вывод
    компактный и ПОБАЙТОВО совпадает с дополнением-08-агностичной схемой."""

    def __init__(self, include_provenance: bool = True) -> None:
        self._prov = include_provenance

    def to_dict(self, result: ParseResult) -> Dict[str, Any]:
        # В компактном режиме warnings инварианта владения (provenance:) тоже убираем —
        # они часть провенанса; тогда выход совпадает с прежней формой байт-в-байт.
        warnings = (result.warnings if self._prov
                    else [w for w in result.warnings
                          if not w.startswith("provenance:")])
        doc = {
            "metadata": result.metadata,
            "sections": [self._section(s) for s in result.sections],
            "tables": [self._table(t) for t in result.tables],
            "excluded": self._excluded(result.excluded),
            "stats": result.stats,
            "warnings": warnings,
        }
        # provenance — top-level блок В КОНЦЕ (после warnings), аддитивно.
        if self._prov:
            doc["provenance"] = self._provenance(result.page_ir)
        return doc

    def write(self, result: ParseResult, out_path: str) -> None:
        """Атомарно записать JSON: пишем во временный файл рядом, fsync, затем
        os.replace на целевой. На диске всегда либо полный старый, либо полный
        новый файл — обрывов/торн-состояний при прерывании батча не бывает.

        Форматирование не меняется (ensure_ascii=False, indent=2); при
        include_provenance=False вывод для незатронутых файлов остаётся байт-в-байт.
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
        out: Dict[str, Any] = {
            "number": section.number,
            "title": section.title,
            "level": section.level,
            "text": section.text,
            "children": [self._section(c) for c in section.children],
        }
        # provenance — новые ключи В КОНЦЕ (диффы читаются, strip даёт baseline).
        if self._prov:
            out["section_id"] = section.section_id
            out["page"] = section.page
            out["bbox"] = list(section.bbox)
            out["span_uids"] = list(section.span_uids)
        return out

    def _table(self, table: Table) -> Dict[str, Any]:
        out: Dict[str, Any] = {
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
        if self._prov:
            out["claimed_span_uids"] = list(table.claimed_span_uids)
            out["source"] = table.source
            # недеструктивность (промпт 09) — аддитивно, В КОНЕЦ
            out["reconciled"] = table.reconciled
            out["reconcile_score"] = table.reconcile_score
            out["continues_table"] = table.continues_table
            out["row_count"] = table.row_count
            out["cell_count"] = table.cell_count
            out["empty_cell_ratio"] = table.empty_cell_ratio
        return out

    def _excluded(self, excluded: Dict[str, List]) -> Dict[str, List[Dict[str, Any]]]:
        """excluded[bucket] = [ {title, text[, span_uids]} ]. Форма JSON прежняя:
        title/text первыми, span_uids — В КОНЦЕ (аддитивно)."""
        out: Dict[str, List[Dict[str, Any]]] = {}
        for bucket, items in excluded.items():
            ser: List[Dict[str, Any]] = []
            for item in items:
                d: Dict[str, Any] = {"title": item.get("title", ""),
                                     "text": item.get("text", "")}
                if self._prov:
                    d["span_uids"] = list(item.get("span_uids", []) or [])
                ser.append(d)
            out[bucket] = ser
        return out

    # ---- provenance top-level блок ---------------------------------------

    @staticmethod
    def _provenance(page_ir: List[PageIR]) -> Dict[str, Any]:
        """Сводка по спанам: всего, по страницам, по ВЫБРАННОМУ каналу и расхождения
        native/OCR (самое ценное поле — каждое место, где выбрали одно из двух мнений)."""
        spans_total = 0
        pages: List[Dict[str, Any]] = []
        channels: Dict[str, int] = {}
        disagreements: List[Dict[str, str]] = []
        disagreements_total = 0
        for pir in page_ir:
            spans_total += len(pir.spans)
            pages.append({
                "page": pir.page,
                "primary_channel": pir.primary_channel,
                "auxiliary_channels": list(pir.auxiliary_channels),
                "spans": len(pir.spans),
            })
            for sp in pir.spans:
                channels[sp.selected] = channels.get(sp.selected, 0) + 1
                nat = sp.candidates.get("native")
                ocr = sp.candidates.get("ocr")
                if nat is not None and ocr is not None and nat != ocr:
                    disagreements_total += 1
                    if len(disagreements) < _DISAGREEMENTS_CAP:
                        disagreements.append(
                            {"span_uid": sp.span_uid, "native": nat, "ocr": ocr})
        return {
            "spans_total": spans_total,
            "pages": pages,
            "channels": channels,
            "disagreements": disagreements,
            "disagreements_total": disagreements_total,
        }
