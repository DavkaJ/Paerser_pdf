#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Абстрактный профиль документа.

`DocumentProfile` — единственная точка, через которую движок узнаёт специфику
типа документа. Движок НЕ знает, КР это, монография или что-то ещё: он лишь
вызывает методы профиля. Новый тип документа = новый наследник этого класса.

Контракт (обязательные абстрактные методы из ТЗ):
    classify_heading()    — является ли строка заголовком и каким
    extract_metadata()    — собрать метаданные документа
    excluded_regions()    — маркеры ToC / литературы / приложений
    named_section_id()    — стабильный id для именованного раздела

Дополнительно профиль может переопределить «хуки» (template-method) — у них есть
безопасные дефолты, поэтому минимальный профиль реализует только 4 метода выше.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from crparser.engine.models import (
    ExcludedSpec,
    Heading,
    Line,
    MetadataContext,
)


class DocumentProfile(ABC):
    """База для всех профилей. Инкапсулирует знание о структуре типа документа."""

    # ---- идентификация профиля -------------------------------------------

    #: ключ профиля для CLI (`--profile cr`). Переопределяется в наследниках.
    key: str = "base"

    @property
    @abstractmethod
    def document_type(self) -> str:
        """Машинное имя типа документа (попадает в metadata.document_type)."""
        raise NotImplementedError

    # ---- обязательный контракт (ТЗ) --------------------------------------

    @abstractmethod
    def classify_heading(self, line: Line, body_size: float) -> Optional[Heading]:
        """
        Классифицировать строку как заголовок.

        Возвращает Heading (NUMBERED / NUMBER_ONLY / NAMED) либо None, если
        строка — обычный текст. Именно здесь живёт вся эвристика заголовков
        конкретного типа документа (шрифт + структура номера + валидность).
        """
        raise NotImplementedError

    @abstractmethod
    def extract_metadata(self, ctx: MetadataContext) -> Dict[str, object]:
        """
        Собрать метаданные документа. Профиль сам решает источники и приоритеты
        (например, реестр > титульный лист) и добавляет warnings в ctx при нужде.
        """
        raise NotImplementedError

    @abstractmethod
    def excluded_regions(self) -> ExcludedSpec:
        """Отдать движку маркеры регионов-исключений для конечного автомата."""
        raise NotImplementedError

    @abstractmethod
    def named_section_id(self, title: str) -> Optional[str]:
        """
        Сопоставить именованному (без номера) заголовку стабильный id
        (например, «Список сокращений» -> 'abbreviations'); иначе None.
        """
        raise NotImplementedError

    # ---- перекрываемые хуки (дефолты безопасны) --------------------------

    def is_title_continuation(
        self,
        current_title: str,
        line: Line,
        heading: Heading,
        body_size: float,
    ) -> bool:
        """
        Является ли строка продолжением уже открытого заголовка, перенесённого
        на несколько строк. По умолчанию — нет (профиль может уточнить).
        """
        return False

    def can_attach_title(
        self,
        line: Line,
        pending: Heading,
        body_size: float,
    ) -> bool:
        """
        Можно ли считать строку заголовком для «висящего» номера (случай, когда
        номер «4.» и заголовок «Медицинская реабилитация» на разных строках).
        По умолчанию — да, если строка непустая.
        """
        return bool(line.text.strip())

    def split_inline_headings(self, line: Line) -> List[Line]:
        """
        Разрезать строку, если внутри неё спрятан новый заголовок
        (например, «... [75]. 4. Медицинская реабилитация ...»). По умолчанию —
        не трогаем.
        """
        return [line]

    # ---- утилита, общая для большинства профилей -------------------------

    @staticmethod
    def heading_order_valid(new_number: Optional[str], last_number: Optional[str]) -> bool:
        """Номера разделов должны монотонно расти (1 < 1.1 < 1.2 < 2 ...)."""
        if not new_number or not last_number:
            return True

        def as_tuple(num: str):
            return tuple(int(p) for p in num.split(".") if p.isdigit())

        try:
            return as_tuple(new_number) > as_tuple(last_number)
        except Exception:
            return True
