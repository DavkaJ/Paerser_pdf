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

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Tuple

from crparser.engine.models import (
    ExcludedSpec,
    Heading,
    Line,
    MetadataContext,
)


# --------------------------------------------------------------------------- #
# Явный контракт метаданных (промпт 11): вместо ключа-призрака `_warnings`      #
# в словаре метаданных профиль возвращает пару (metadata, warnings).           #
# --------------------------------------------------------------------------- #
@dataclass
class MetadataResult:
    """Результат извлечения метаданных: сам словарь + предупреждения профиля.
    Заменяет магический `metadata.pop("_warnings")` явным контрактом."""
    metadata: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Пять доменных политик (промпт 11). Дефолты доменно-НЕЙТРАЛЬНЫ (латиница,      #
# арабские номера, английские якоря) — КР-специфику переопределяет clinical.py. #
# Движок спрашивает политику вместо хардкода. Это разрывает скрытую связанность: #
# «общий» движок больше не знает про КР там, где связанность концентрирована.   #
# --------------------------------------------------------------------------- #

# нейтральный заголовок подраздела: дотированный номер + заглавная (лат/кир/цифра)
_DEFAULT_SUBSEC_HEAD = re.compile(r"^\s*\d{1,2}(?:\.\d{1,3})+\.?\s+[A-ZА-ЯЁ]")
# нейтральная подпись таблицы/рисунка (английская)
_DEFAULT_TABLE_CAPTION = re.compile(r"^(Table|Figure|Tab\.|Fig\.)\b", re.IGNORECASE)


class NumberingPolicy:
    """Что считать номером раздела и как проверять порядок. Дефолт — арабские
    дотированные номера, латиница/кириллица в заголовке."""

    #: предел длины заголовка подраздела (длиннее — это абзац прозы с номером).
    max_subtitle_len: int = 200

    #: паттерн строки-заголовка подраздела (дотированный номер + заглавная).
    subsection_head: Pattern[str] = _DEFAULT_SUBSEC_HEAD

    #: использует ли профиль главы с РИМСКОЙ нумерацией (механизм в сегментере
    #: активируется только если classify_heading отдаёт roman=True).
    uses_roman_chapters: bool = False

    @staticmethod
    def heading_order_valid(new_number: Optional[str], last_number: Optional[str]) -> bool:
        """Номера разделов должны монотонно расти (1 < 1.1 < 1.2 < 2 ...).
        Перенесён из DocumentProfile (промпт 11); ПОДКЛЮЧЕНИЕ — отдельным шагом 11b
        (сейчас не вызывается ниоткуда — байт-в-байт требует, чтобы поведение не менялось)."""
        if not new_number or not last_number:
            return True

        def as_tuple(num: str):
            return tuple(int(p) for p in num.split(".") if p.isdigit())

        try:
            return as_tuple(new_number) > as_tuple(last_number)
        except Exception:
            return True


class RegionPolicy:
    """Имена и якоря регионов-исключений. `excluded_spec` — маркеры ToC/литературы/
    приложений (конечный автомат сегментера). `body_start_markers` — заголовки, чьё
    ВТОРОЕ (телесное) вхождение закрывает оглавление без точек-лидеров."""

    def excluded_spec(self) -> ExcludedSpec:
        # нейтральные английские якоря
        return ExcludedSpec(
            toc=re.compile(r"^\s*(contents|table\s+of\s+contents)\s*$", re.IGNORECASE),
            references=re.compile(r"^\s*references\s*[.:]?\s*$", re.IGNORECASE),
            appendices=re.compile(r"^\s*appendix\b", re.IGNORECASE),
        )

    def body_start_markers(self) -> Tuple[Pattern[str], ...]:
        return ()


class ContentBoundaryPolicy:
    """Где начинается тело документа, что считать front matter. Дом для
    `_find_content_start` (промпт 12 будет чинить именно эту политику). В прагматичном
    объёме 11 сам метод остаётся в сегментере (перенос — дороже в разборе, чем в жизни,
    записано в inventory); политика существует как типизированный дом для 12."""

    #: минимальное число строк прозы после заголовка, чтобы счесть его началом тела.
    prose_run_min: int = 3


class OcrPolicy:
    """Когда включать OCR и какие зоны/якоря использовать. `region_anchors` —
    словарь якорей регионов для гибрид-OCR (сокращения/термины/литература). В
    прагматичном объёме 11 глубина ocr.py (алгоритм починки) остаётся, якоря
    доступны через политику (record в inventory)."""

    def region_anchors(self) -> Dict[str, Pattern[str]]:
        return {}


class TablePolicy:
    """Паттерны подписей таблиц и пороги reconcile (промпт 09). Дефолт —
    английская подпись, пороги как в движке."""

    caption_pattern: Pattern[str] = _DEFAULT_TABLE_CAPTION
    reconcile_min: float = 0.80
    reconcile_min_lowconf: float = 0.90


class DocumentProfile(ABC):
    """База для всех профилей. Инкапсулирует знание о структуре типа документа."""

    # ---- идентификация профиля -------------------------------------------

    #: ключ профиля для CLI (`--profile cr`). Переопределяется в наследниках.
    key: str = "base"

    #: источник реестра метаданных (часть контракта профиля; может быть None).
    #: Объявлено здесь, чтобы движок брал `profile.registry`, а не гадал getattr'ом.
    registry: Any = None

    # ---- доменные политики (промпт 11): дефолты нейтральны, КР — в clinical ----

    @property
    def numbering(self) -> NumberingPolicy:
        return NumberingPolicy()

    @property
    def regions(self) -> RegionPolicy:
        return RegionPolicy()

    @property
    def content_boundary(self) -> ContentBoundaryPolicy:
        return ContentBoundaryPolicy()

    @property
    def ocr(self) -> OcrPolicy:
        return OcrPolicy()

    @property
    def tables(self) -> TablePolicy:
        return TablePolicy()

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

    def metadata_result(self, ctx: MetadataContext) -> MetadataResult:
        """Явный контракт (промпт 11): (metadata, warnings) вместо ключа-призрака.
        Дефолт — извлечь метаданные и снять из словаря legacy-ключ `_warnings`; профиль
        может переопределить и строить MetadataResult напрямую (без ключа-призрака)."""
        meta = dict(self.extract_metadata(ctx))
        return MetadataResult(meta, meta.pop("_warnings", []))

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

    def heading_breaks_before(self, line: Line, current_title: str) -> bool:
        """
        Должна ли склейка заголовка ОБОРВАТЬСЯ перед этой строкой, потому что
        она — начало тела (маркер списка, служебное слово «Рекомендуется…»,
        код медуслуги и т.п.). По умолчанию — нет.
        """
        return False

    def main_title_canonical(self, title: str, number: Optional[str]) -> bool:
        """Заголовок верхнего уровня — канонический раздел шаблона? По умолчанию —
        считаем любой допустимым (профиль уточняет; движок иначе требует TOC)."""
        return True

    def title_is_body_label(self, title: str) -> bool:
        """Заголовок — на деле метка тела (рекомендация/служебный лейбл) с номером?
        По умолчанию — нет (профиль уточняет)."""
        return False

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
        """Монотонность номеров — теперь живёт в NumberingPolicy (промпт 11). Тонкий
        делегат оставлен для обратной совместимости; ПОДКЛЮЧЕНИЕ проверки — шаг 11b."""
        return NumberingPolicy.heading_order_valid(new_number, last_number)
