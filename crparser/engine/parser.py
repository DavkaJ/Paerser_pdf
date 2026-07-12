#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Оркестратор движка.

`DocumentParser` связывает все компоненты движка и работает ТОЛЬКО через
абстракцию `DocumentProfile`. Он не знает, КР перед ним или иной документ:
весь доменный смысл инкапсулирован в профиле.

Конвейер:
    PDF --PdfReader--> страницы(layout)
        --TableExtractor--> таблицы + карта вычитания
        --profile.extract_metadata--> метаданные
        --Segmenter(profile)--> разделы + исключения
        --StatsCalculator--> покрытие
        --> ParseResult
"""

from __future__ import annotations

import os
from typing import List

from crparser.engine.models import MetadataContext, ParseResult
from crparser.engine.pdf_reader import PdfReader
from crparser.engine.segmenter import Segmenter
from crparser.engine.stats import StatsCalculator
from crparser.engine.tables import TableExtractor
from crparser.engine.textnorm import (
    glyph_suspect_count, pseudo_ascii_counts, pseudo_ascii_glyph_tokens)
from crparser.profiles.base import DocumentProfile


class DocumentParser:
    """Парсит один PDF выбранным профилем в ParseResult."""

    def __init__(self, profile: DocumentProfile) -> None:
        self._profile = profile
        self._stats = StatsCalculator()

    def parse(self, pdf_path: str) -> ParseResult:
        warnings_list: List[str] = []
        source_file = os.path.basename(pdf_path)

        # 1. layout-чтение (внутри — ГИБРИД-OCR для §-кандидатов, если доступен)
        reader = PdfReader(pdf_path)
        try:
            pages = reader.read()
            norm_stats = dict(reader.norm_stats)
            full_text = reader.full_text(pages)
            # ПОЛНЫЙ OCR: годного текста НЕТ (скан) ИЛИ тотальная «обратная» порча
            # (кириллица->ASCII). Пересобираем документ из полного OCR, ПОКА
            # reader._doc открыт; при недоступном OCR оставляем прежнее поведение.
            pseudo_ascii = pseudo_ascii_glyph_tokens(
                norm_stats.get("sig", 0), norm_stats.get("pseudo", 0))
            if not full_text.strip() or pseudo_ascii > 0:
                ocr_pages = self._full_ocr(reader, warnings_list)
                if ocr_pages:
                    pages = ocr_pages
                    full_text = reader.full_text(pages)
                    norm_stats = self._recount_corruption(pages)
                    pseudo_ascii = pseudo_ascii_glyph_tokens(
                        norm_stats.get("sig", 0), norm_stats.get("pseudo", 0))
            body_size = reader.body_size(pages)
            first_page_text = reader.first_page_text()
        finally:
            reader.close()

        if not full_text.strip():
            warnings_list.append(
                "PDF без текстового слоя (вероятно скан) — разделы и таблицы не "
                "извлечены; метаданные взяты только из реестра")

        # 2. таблицы по bbox + карта вычитания
        extractor = TableExtractor(pdf_path)
        tables = extractor.extract(pages, warnings_list)

        # 3. метаданные (через профиль)
        ctx = MetadataContext(
            pdf_path=pdf_path,
            source_file=source_file,
            full_text=full_text,
            first_page_text=first_page_text,
            pages=pages,
            registry=getattr(self._profile, "registry", None),
        )
        metadata = self._profile.extract_metadata(ctx)
        metadata.setdefault("source_file", source_file)
        metadata.setdefault("document_type", self._profile.document_type)
        # профиль мог записать предупреждения
        warnings_list.extend(metadata.pop("_warnings", []))

        # 4. нарезка на разделы/исключения (через профиль)
        segmenter = Segmenter(self._profile, body_size)
        segmented = segmenter.segment(pages, extractor.subtraction_map, warnings_list)
        sections = segmented["sections"]
        excluded = segmented["excluded"]

        # 5. статистика покрытия
        stats = self._stats.compute(full_text, sections, excluded, tables)
        # счётчики порчи текста: разрядка/удвоение (починены) + глиф-токены
        # (обнаружены, на OCR) — валидатор по ним поднимает статус CORRUPTION.
        glyph_regions = getattr(extractor, "corrupt_count", 0)
        # «обратная» глифовая порча (кириллица->ASCII): засчитываем токены на OCR
        # только при ТОТАЛЬНОЙ порче по документу (плотностной порог), иначе 0 —
        # единичные лаб-токены (>50%, p<0.05) в здоровом файле не считаются.
        pseudo_ascii = pseudo_ascii_glyph_tokens(
            norm_stats.get("sig", 0), norm_stats.get("pseudo", 0))
        stats["corruption"] = {
            "spacing_fixed": norm_stats.get("spacing", 0),
            "doubling_fixed": norm_stats.get("doubling", 0),
            "glyph_tokens": norm_stats.get("glyph", 0),
            "glyph_regions": glyph_regions,
            "pseudo_ascii_tokens": pseudo_ascii,
        }
        if stats["coverage_percent"] < 50:
            warnings_list.append(
                f"низкое покрытие ({stats['coverage_percent']}%) — текст мог уйти "
                f"в исключения или не распознались заголовки")

        return ParseResult(
            metadata=metadata,
            sections=sections,
            tables=tables,
            excluded=excluded,
            stats=stats,
            warnings=warnings_list,
        )

    # ---- OCR-восстановление (изолировано в crparser.engine.ocr) --------------

    def _full_ocr(self, reader: PdfReader, warnings_list: List[str]):
        """Собрать страницы из ПОЛНОГО OCR (скан/тотальная порча) до close()
        reader. Пустой список -> прежнее поведение (OCR недоступен/пусто)."""
        from crparser.engine.ocr import OcrRecoverer
        try:
            recoverer = OcrRecoverer()
            if not recoverer.available():
                return []      # OCR не настроен — тихо, поведение как без OCR
            doc_id = os.path.splitext(os.path.basename(reader._path))[0]
            from crparser.engine.pdf_reader import _pdf_sha256
            pages = recoverer.full_ocr(reader.doc, doc_id=doc_id,
                                       pdf_sha=_pdf_sha256(reader._path))
        except Exception as exc:  # noqa: BLE001
            warnings_list.append(f"полный OCR не выполнен ({exc!r})")
            return []
        if pages and any(page.lines for page in pages):
            warnings_list.append(
                "документ восстановлен ПОЛНЫМ OCR (скан/тотальная глифовая порча)")
            return pages
        return []

    @staticmethod
    def _recount_corruption(pages) -> dict:
        """Счётчики порчи на ВОССТАНОВЛЕННОМ полным OCR тексте (он чистый)."""
        glyph = sig = pseudo = 0
        for page in pages:
            for line in page.lines:
                glyph += glyph_suspect_count(line.text)
                s_here, p_here = pseudo_ascii_counts(line.text)
                sig += s_here
                pseudo += p_here
        return {"spacing": 0, "doubling": 0, "glyph": glyph, "sig": sig, "pseudo": pseudo}
