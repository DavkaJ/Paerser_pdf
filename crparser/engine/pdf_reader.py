#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Чтение PDF с сохранением layout (PyMuPDF / fitz).

Движок-уровень: знает только как достать из PDF строки с координатами, кеглем и
жирностью. Никакой документ-специфики. Также мягко чистит OCR-мусор на уровне
строки (общая, не доменная нормализация текста).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from typing import Dict, List

import fitz  # PyMuPDF

from crparser.engine.models import BBox, Line, Page
from crparser.engine.textnorm import (
    glyph_suspect_count, looks_glyph_corrupted, normalize_line,
    pseudo_ascii_counts)

def _pdf_sha256(path: str):
    """sha256 исходного PDF — часть content-addressed ключа OCR-кэша. None при сбое."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


# Бит 4 (16) в span["flags"] PyMuPDF — признак жирного начертания.
_FLAG_BOLD = 1 << 4

# Имена шрифтов, означающие жирность (на случай, если флаг не выставлен).
_BOLD_FONT_HINTS = ("bold", "black", "semibold", "demibold", "heavy")

# Базовая чистка текста строки (общая, не доменная).
_SOFT_HYPHEN = "­"
_NBSP = " "
_TRASH_CHARS = ("￾", "￿", "​", "﻿")


def _is_bold_span(span: Dict) -> bool:
    """Жирный ли спан — по флагу или по имени шрифта."""
    font = str(span.get("font", "")).lower()
    flags = int(span.get("flags", 0))
    return bool(flags & _FLAG_BOLD) or any(h in font for h in _BOLD_FONT_HINTS)


def _clean_line(text: str) -> str:
    """Схлопнуть пробелы, убрать мягкие переносы и мусорные символы."""
    text = text.replace(_NBSP, " ").replace(_SOFT_HYPHEN, "")
    for ch in _TRASH_CHARS:
        text = text.replace(ch, "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _norm_bbox(bbox) -> BBox:
    if not bbox:
        return (0.0, 0.0, 0.0, 0.0)
    x0, y0, x1, y1 = bbox
    return (float(x0), float(y0), float(x1), float(y1))


class PdfReader:
    """
    Открывает PDF и отдаёт его как список страниц со строками (layout).

    Использование::

        reader = PdfReader(path)
        pages = reader.read()
        body = reader.body_size(pages)
        reader.close()
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._doc = fitz.open(path)
        #: счётчики починенной/обнаруженной порчи текста (для валидатора)
        self.norm_stats: Dict[str, int] = {
            "spacing": 0, "doubling": 0, "glyph": 0,
            # «обратная» глифовая порча (кириллица->ASCII): накапливаем сырые
            # счётчики по документу, решение — по совокупной доле в parser.
            "pseudo": 0, "sig": 0}
        #: диагностика OCR-пути (для логов); в вывод документа НЕ попадает, чтобы
        #: отсутствие/сбой Tesseract не меняли JSON — файл парсится как без OCR
        self.ocr_warnings: List[str] = []

    @property
    def page_count(self) -> int:
        return self._doc.page_count

    @property
    def doc(self):
        """Открытый fitz-документ (для полного OCR до close())."""
        return self._doc

    def first_page_text(self) -> str:
        """Сырой текст первой страницы (для fallback-метаданных по титулу)."""
        try:
            return self._doc[0].get_text()
        except Exception:
            return ""

    def read(self) -> List[Page]:
        """Прочитать все страницы со строками и layout-атрибутами."""
        pages: List[Page] = []
        for index, page in enumerate(self._doc):
            page_dict = page.get_text("dict", sort=True)
            lines: List[Line] = []

            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:  # 0 — текстовый блок
                    continue
                for raw_line in block.get("lines", []):
                    spans = raw_line.get("spans", [])
                    parts, sizes, bolds = [], [], []
                    for span in spans:
                        span_text = span.get("text", "")
                        if not span_text:
                            continue
                        parts.append(span_text)
                        sizes.append(float(span.get("size", 0.0)))
                        bolds.append(_is_bold_span(span))

                    text = _clean_line("".join(parts))
                    if not text:
                        continue

                    # «обратная» глифовая порча (кириллица->ASCII) — считаем по
                    # СЫРОМУ тексту строки (до нормализации), доля агрегируется
                    # по документу; решение принимает parser (плотностной порог).
                    sig_here, pseudo_here = pseudo_ascii_counts(text)
                    self.norm_stats["sig"] += sig_here
                    self.norm_stats["pseudo"] += pseudo_here

                    # нормализация порчи (доменно-нейтрально): разрядку/удвоение
                    # чиним, глифовую подмену — НЕ трогаем (только считаем), чтобы
                    # не искажать регион, который всё равно уйдёт на OCR.
                    glyph_here = glyph_suspect_count(text)
                    if glyph_here:
                        self.norm_stats["glyph"] += glyph_here
                    elif looks_glyph_corrupted(text):
                        pass  # смешение скриптов без «плохих» биграмм — не чиним
                    else:
                        text, nsp, ndb = normalize_line(text)
                        self.norm_stats["spacing"] += nsp
                        self.norm_stats["doubling"] += ndb

                    lines.append(Line(
                        page=index + 1,
                        text=text,
                        bbox=_norm_bbox(raw_line.get("bbox")),
                        size=max(sizes) if sizes else 0.0,
                        bold=any(bolds),
                    ))

            # разрыв базовых линий к предыдущей строке (для детекции «пустых строк»,
            # которые в PDF выглядят как увеличенный вертикальный интервал)
            for i in range(1, len(lines)):
                lines[i].gap_before = lines[i].bbox[1] - lines[i - 1].bbox[1]

            pages.append(Page(
                number=index + 1,
                width=float(page.rect.width),
                height=float(page.rect.height),
                lines=lines,
                text="\n".join(ln.text for ln in lines),
            ))

        # ГИБРИД-OCR восстановление битого латинского слоя (изолировано в ocr.py).
        # Вызывается, ПОКА self._doc открыт, после сборки всех строк. Гейт —
        # плотностной §-порог по документу; для ЧИСТЫХ файлов (нет «§») работа
        # ноль: не рендерим, не зовём Tesseract, вывод байт-в-байт прежний.
        self._maybe_hybrid_ocr(pages)
        return pages

    def _maybe_hybrid_ocr(self, pages: List[Page]) -> None:
        """Если документ — кандидат на кирилло-латинскую §-порчу И доступен
        Tesseract, восстановить битые латинские токены по месту (Line.text).
        Любой сбой OCR не роняет чтение: пишем предупреждение, оставляем как есть."""
        full = "\n".join(page.text for page in pages)
        # локальный импорт: OCR-зависимости не грузятся для чистых файлов/при отказе
        from crparser.engine.ocr import OcrRecoverer, is_hybrid_candidate
        from crparser.engine import ocr_pins
        base = ocr_pins.load_base()
        if not is_hybrid_candidate(full, base):
            return
        try:
            recoverer = OcrRecoverer()
            if not recoverer.available():
                return          # OCR не настроен — тихо, вывод как без OCR
            doc_id = os.path.splitext(os.path.basename(self._path))[0]
            fixed, new_map = recoverer.hybrid_recover(
                self._doc, pages, base, doc_id=doc_id,
                pdf_sha=_pdf_sha256(self._path))
            if fixed:
                self.norm_stats["ocr_hybrid_lines"] = fixed
                self._recount_corruption(pages)
            if new_map:
                ocr_pins.write_shard(doc_id, new_map)
        except Exception as exc:  # noqa: BLE001
            self.ocr_warnings.append(f"гибрид-OCR не выполнен ({exc!r})")

    def _recount_corruption(self, pages: List[Page]) -> None:
        """Пересчитать глифовые счётчики порчи на ВОССТАНОВЛЕННОМ тексте, чтобы
        stats/валидатор видели чистый результат (а не исходные битые токены)."""
        glyph = sig = pseudo = 0
        for page in pages:
            for line in page.lines:
                glyph += glyph_suspect_count(line.text)
                s_here, p_here = pseudo_ascii_counts(line.text)
                sig += s_here
                pseudo += p_here
        self.norm_stats["glyph"] = glyph
        self.norm_stats["sig"] = sig
        self.norm_stats["pseudo"] = pseudo

    @staticmethod
    def body_size(pages: List[Page]) -> float:
        """
        Кегль основного текста = самый частый размер (по числу строк) в разумном
        диапазоне 6..20pt. На этом числе строится детекция «крупных» заголовков.
        """
        counter: Counter = Counter()
        for page in pages:
            for line in page.lines:
                if 6.0 <= line.size <= 20.0:
                    counter[round(line.size, 1)] += 1
        if not counter:
            return 12.0
        return counter.most_common(1)[0][0]

    def full_text(self, pages: List[Page]) -> str:
        return "\n".join(page.text for page in pages)

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:
            pass

    def __enter__(self) -> "PdfReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
