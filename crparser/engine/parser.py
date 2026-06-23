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
from crparser.profiles.base import DocumentProfile


class DocumentParser:
    """Парсит один PDF выбранным профилем в ParseResult."""

    def __init__(self, profile: DocumentProfile) -> None:
        self._profile = profile
        self._stats = StatsCalculator()

    def parse(self, pdf_path: str) -> ParseResult:
        warnings_list: List[str] = []
        source_file = os.path.basename(pdf_path)

        # 1. layout-чтение
        reader = PdfReader(pdf_path)
        try:
            pages = reader.read()
            body_size = reader.body_size(pages)
            full_text = reader.full_text(pages)
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
