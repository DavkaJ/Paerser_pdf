#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Минимальный доменно-НЕЙТРАЛЬНЫЙ профиль (промпт 11, ШАГ 3).

Доказательство расширяемости: движок строит дерево разделов ЧУЖОГО (не-КР) документа
через тот же интерфейс DocumentProfile, не зная ничего про КР. Профиль реализует
ровно четыре обязательных метода контракта (classify_heading / extract_metadata /
excluded_regions / named_section_id) на нейтральных правилах — латиница, арабские
номера, английские якоря регионов, без реестра. Доменные политики (numbering/regions/
tables/ocr/content_boundary) берутся из НЕЙТРАЛЬНЫХ дефолтов base.py.

Если бы такой профиль написать не удавалось — рефакторинг «engine не знает про КР» не
состоялся бы (движок содержал бы КР-условия). Он пишется — значит структурный конвейер
профиль-агностичен (языковые русские соглашения — отдельный, осознанно оставленный слой,
см. _corpus/coupling_inventory.md, Р1).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from crparser.engine.models import ExcludedSpec, Heading, HeadingKind, Line, MetadataContext
from crparser.profiles.base import DocumentProfile

# Нейтральный нумерованный заголовок: «1 Title», «1. Title», «2.3 Sub-title».
_RE_NUMBERED = re.compile(r"^(\d+(?:\.\d+){0,5})\.?\s+([A-Za-z].*)$")
# Висящий номер отдельной строкой: «4.».
_RE_NUMBER_ONLY = re.compile(r"^(\d+(?:\.\d+){0,5})\.?$")


class MinimalProfile(DocumentProfile):
    """Нейтральный профиль: латиница, арабские номера, английские якоря."""

    key = "minimal"

    @property
    def document_type(self) -> str:
        return "generic_document"

    def classify_heading(self, line: Line, body_size: float) -> Optional[Heading]:
        text = " ".join((line.text or "").split())
        if not text:
            return None
        m = _RE_NUMBERED.match(text)
        if m:
            number = m.group(1)
            title = m.group(2).strip()
            level = number.count(".") + 1
            h = Heading(number=number, title=title, level=level,
                        kind=HeadingKind.NUMBERED,
                        visual=(line.size >= body_size * 1.35 or line.bold))
            h.page, h.bbox = line.page, line.bbox
            return h
        m = _RE_NUMBER_ONLY.match(text)
        if m:
            number = m.group(1)
            h = Heading(number=number, title="", level=number.count(".") + 1,
                        kind=HeadingKind.NUMBER_ONLY)
            h.page, h.bbox = line.page, line.bbox
            return h
        return None

    def excluded_regions(self) -> ExcludedSpec:
        return ExcludedSpec(
            toc=re.compile(r"^\s*(contents|table\s+of\s+contents)\s*$", re.IGNORECASE),
            references=re.compile(r"^\s*(references|bibliography)\s*[.:]?\s*$", re.IGNORECASE),
            appendices=re.compile(r"^\s*appendix\b", re.IGNORECASE),
        )

    def named_section_id(self, title: str) -> Optional[str]:
        return None

    def extract_metadata(self, ctx: MetadataContext) -> Dict[str, Any]:
        # нейтральные метаданные без реестра; движок допишет source_file/document_type
        return {"source_file": ctx.source_file, "document_type": self.document_type,
                "title": None, "id": None}
