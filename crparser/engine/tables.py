#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Извлечение таблиц по bbox (pdfplumber).

Движок-уровень: ищем таблицы, фильтруем ложные детекции, снимаем сырой текстовый
дамп области (best-effort, без разбора на ячейки), привязываем подпись «Таблица N»
и строим карту bbox для ВЫЧИТАНИЯ табличного текста из прозы разделов.
"""

from __future__ import annotations

import re
import warnings
from typing import Dict, List, Optional, Tuple

import pdfplumber

from crparser.engine.models import BBox, Page, Table

# pdfminer (под капотом pdfplumber) шумит на «грязных» PDF — глушим.
warnings.filterwarnings("ignore")

_RE_TABLE_CAPTION = re.compile(r"^\s*Таблица\s+(\d+(?:\.\d+)?)\.?\s*(.*)$", re.IGNORECASE)


def _collapse_ws(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class TableExtractor:
    """
    Находит таблицы через pdfplumber и валидирует их.

    Ложные детекции отсекаем по двум правилам (объединённый опыт обоих парсеров):
      * bbox должен лежать в пределах страницы (иначе это сквозной векторный мусор);
      * структура >= 2x2 и >= 4 непустых ячеек (иначе это абзац/линия, не таблица).
    Валидные таблицы сохраняем сырыми; их bbox отдаём для вычитания из текста.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        #: карта page_index(0-based) -> список bbox валидных таблиц (для вычитания)
        self.subtraction_map: Dict[int, List[BBox]] = {}
        #: счётчики отфильтрованного для warnings
        self.junk_count = 0
        self.small_count = 0

    def extract(self, pages: List[Page], warnings_list: List[str]) -> List[Table]:
        """
        Вернуть список валидных таблиц. `pages` нужны для привязки подписей
        (строки с layout из PdfReader), `warnings_list` — для мягкой деградации.
        """
        tables: List[Table] = []
        try:
            pdf = pdfplumber.open(self._path)
        except Exception as exc:  # noqa: BLE001
            warnings_list.append(f"pdfplumber не смог открыть файл: {exc}")
            return tables

        captions_by_page = self._collect_captions(pages)

        with pdf:
            for pno, page in enumerate(pdf.pages):
                ph = float(page.height or 0.0)
                pw = float(page.width or 0.0)
                try:
                    found = page.find_tables()
                except Exception as exc:  # noqa: BLE001
                    warnings_list.append(f"find_tables упал на стр. {pno + 1}: {exc}")
                    continue

                for tbl in found:
                    bbox = self._norm(tbl.bbox)
                    grid = self._safe_extract(tbl)
                    verdict = self._validate(bbox, grid, pw, ph)
                    if verdict != "ok":
                        if verdict == "junk":
                            self.junk_count += 1
                        else:
                            self.small_count += 1
                        continue

                    raw_text = self._dump_text(page, bbox, pw, ph, warnings_list, pno)
                    number, caption = self._match_caption(bbox, captions_by_page.get(pno + 1, []))

                    tables.append(Table(
                        page=pno + 1,
                        number=number,
                        caption=caption,
                        raw_text=_collapse_ws(raw_text),
                        bbox=tuple(round(v, 1) for v in bbox),  # type: ignore[arg-type]
                    ))
                    self.subtraction_map.setdefault(pno, []).append(bbox)

        if self.junk_count:
            warnings_list.append(
                f"отфильтровано {self.junk_count} полностраничных ложных детекций "
                f"(векторный мусор, bbox вне страницы)")
        if self.small_count:
            warnings_list.append(
                f"{self.small_count} мелких/одностолбцовых детекций не сохранены "
                f"как таблицы — их текст остался в прозе раздела")
        return tables

    # ---- внутренняя кухня ------------------------------------------------

    @staticmethod
    def _norm(bbox) -> BBox:
        x0, top, x1, bottom = bbox
        return (float(x0), float(top), float(x1), float(bottom))

    @staticmethod
    def _safe_extract(tbl) -> List[List]:
        try:
            return tbl.extract() or []
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _validate(bbox: BBox, grid: List[List], pw: float, ph: float) -> str:
        """Вернуть 'ok' | 'junk' (bbox вне страницы) | 'small' (структура < 2x2)."""
        x0, top, x1, bottom = bbox
        in_bounds = (
            -2 <= top <= ph + 2
            and -2 <= bottom <= ph + 2
            and bottom > top
            and x1 > x0
            and x0 >= -2
            and x1 <= pw + 2
        )
        if not in_bounds:
            return "junk"
        nrows = len(grid)
        ncols = max((len(r) for r in grid), default=0)
        nonempty = sum(1 for r in grid for c in r if c and str(c).strip())
        if nrows >= 2 and ncols >= 2 and nonempty >= 4:
            return "ok"
        return "small"

    @staticmethod
    def _dump_text(page, bbox: BBox, pw: float, ph: float,
                   warnings_list: List[str], pno: int) -> str:
        x0, top, x1, bottom = bbox
        try:
            crop = page.crop((max(0, x0), max(0, top), min(pw, x1), min(ph, bottom)))
            return crop.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            warnings_list.append(f"не удалось снять дамп таблицы на стр. {pno + 1}: {exc}")
            return ""

    @staticmethod
    def _collect_captions(pages: List[Page]) -> Dict[int, List[Dict]]:
        """Найти строки-подписи «Таблица N ...» на каждой странице."""
        out: Dict[int, List[Dict]] = {}
        for page in pages:
            caps = []
            for line in page.lines:
                m = _RE_TABLE_CAPTION.match(line.text)
                if m:
                    caps.append({
                        "number": m.group(1),
                        "caption": line.text.strip(),
                        "bbox": line.bbox,
                    })
            if caps:
                out[page.number] = caps
        return out

    @staticmethod
    def _match_caption(table_bbox: BBox, captions: List[Dict]
                       ) -> Tuple[Optional[str], Optional[str]]:
        """Подпись обычно стоит чуть выше таблицы — берём ближайшую сверху."""
        tx0, ty0, tx1, ty1 = table_bbox
        candidates = [c for c in captions
                      if c["bbox"][3] <= ty0 + 10 and ty0 - c["bbox"][3] <= 120]
        if not candidates:
            return None, None
        best = min(candidates, key=lambda c: abs(ty0 - c["bbox"][3]))
        return best["number"], best["caption"]
