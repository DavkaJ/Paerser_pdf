#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Общие модели данных движка.

Это «контракт», на который опираются ОБА слоя — и движок (engine), и профили
(profiles). Здесь нет ни знания о типе документа, ни сторонних библиотек: только
простые типизированные структуры, чтобы слои не зависели друг от друга напрямую.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Pattern, Tuple

# Прямоугольник в координатах страницы PDF (точки, начало — верхний левый угол).
BBox = Tuple[float, float, float, float]

# Перечень каналов происхождения строки/спана (промпт 08). Фиксирован.
SOURCE_CHANNELS = ("native", "ocr", "ocr_merged", "font_repair", "vlm")


def span_uid(page: int, bbox: BBox) -> str:
    """ГЕОМЕТРИЧЕСКИЙ якорь единицы источника: p{page}_{sha1(bbox)[:8]}.

    Стабилен между прогонами и НЕ зависит от порядка чтения (в отличие от индекса
    p3_l17). Native-строка и OCR-строка, покрывающие одну область страницы, получают
    ОДИН span_uid — это «один span с двумя кандидатами», а не два несопоставимых
    объекта (ключевой инвариант промпта 08)."""
    x0, y0, x1, y1 = bbox
    key = "%.1f,%.1f,%.1f,%.1f" % (x0, y0, x1, y1)
    return "p%d_%s" % (page, hashlib.sha1(key.encode("utf-8")).hexdigest()[:8])


# --------------------------------------------------------------------------- #
# Provenance IR (промпт 08): неизменяемые единицы источника                    #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SourceSpan:
    """Неизменяемая единица источника: область страницы с одним или несколькими
    представлениями текста (по одному на канал). Существует независимо от того, кто
    её потом присвоил. Якорится ГЕОМЕТРИЧЕСКИ (span_uid), поэтому native-строка и её
    OCR-восстановление — ОДИН span с двумя кандидатами, а не два объекта.

    Глубокая иммутабельность: candidates/confidence — MappingProxyType (frozen=True
    сам вложенные dict не замораживает), bbox — tuple. Попытка мутации падает."""

    span_uid: str
    page: int
    bbox: BBox
    candidates: Mapping[str, str]      # {"native": "Вагсе1опа", "ocr": "Barcelona"}
    confidence: Mapping[str, float]    # {"ocr": 0.91}; native обычно пуст
    selected: str                      # какой канал выбран: см. SOURCE_CHANNELS


@dataclass(frozen=True)
class PageIR:
    """Промежуточное представление одной страницы: её спаны + чем получена основная
    масса текста. `spans` — tuple, `diagnostics` — MappingProxyType (глубокая
    иммутабельность обязательна: ревью право, frozen=True вложенное не морозит)."""

    page: int
    spans: Tuple[SourceSpan, ...]
    primary_channel: str                     # чем получена основная масса текста
    auxiliary_channels: Tuple[str, ...]      # какие каналы ещё дали кандидатов
    diagnostics: Mapping[str, Any]           # MappingProxyType, НЕ голый dict


def make_source_span(uid: str, page: int, bbox: BBox,
                     candidates: Dict[str, str], confidence: Dict[str, float],
                     selected: str) -> SourceSpan:
    """Собрать неизменяемый SourceSpan (dict-аргументы оборачиваются в proxy)."""
    return SourceSpan(span_uid=uid, page=page, bbox=bbox,
                      candidates=MappingProxyType(dict(candidates)),
                      confidence=MappingProxyType(dict(confidence)),
                      selected=selected)


def make_page_ir(page: int, spans: List[SourceSpan], primary: str,
                 auxiliary: List[str], diagnostics: Dict[str, Any]) -> PageIR:
    """Собрать неизменяемый PageIR (последовательности -> tuple, dict -> proxy)."""
    return PageIR(page=page, spans=tuple(spans), primary_channel=primary,
                  auxiliary_channels=tuple(auxiliary),
                  diagnostics=MappingProxyType(dict(diagnostics)))


class HeadingKind(Enum):
    """Тип распознанного заголовка."""

    NUMBERED = "numbered"        # «1.1 Жалобы и анамнез» — номер и текст в одной строке
    NUMBER_ONLY = "number_only"  # «4.» отдельной строкой, заголовок на следующей
    NAMED = "named"              # «Список сокращений» — именованный, без номера


@dataclass
class Line:
    """Одна строка текста с layout-атрибутами (то, что отдаёт PdfReader)."""

    page: int          # номер страницы, 1-based
    text: str          # очищенный текст строки (после нормализации/OCR-слияния)
    bbox: BBox         # координаты строки на странице
    size: float        # максимальный кегль среди спанов строки
    bold: bool         # хотя бы один спан жирный
    gap_before: float = 0.0  # разрыв базовых линий к предыдущей строке (y0-y0), для «пустых строк»
    # --- provenance (промпт 08) ---
    span_uid: str = ""             # геометрический якорь источника
    text_raw: str = ""             # текст ДО нормализации (доказательство правки/откат)
    source: str = "native"         # канал: см. SOURCE_CHANNELS
    confidence: Optional[float] = None  # средняя уверенность OCR (native -> None)

    def __post_init__(self):
        if not self.span_uid:
            self.span_uid = span_uid(self.page, self.bbox)
        if not self.text_raw:
            self.text_raw = self.text

    def clone(self, text: str) -> "Line":
        """Копия строки с другим текстом (для инлайн-разбиения заголовков). span_uid
        сохраняется — половины разбиения происходят из ОДНОГО источника."""
        return Line(page=self.page, text=text, bbox=self.bbox,
                    size=self.size, bold=self.bold, gap_before=self.gap_before,
                    span_uid=self.span_uid, text_raw=self.text_raw,
                    source=self.source, confidence=self.confidence)


@dataclass
class Page:
    """Страница: размеры + строки с layout + сырой текст."""

    number: int        # 1-based
    width: float
    height: float
    lines: List[Line] = field(default_factory=list)
    text: str = ""


@dataclass
class Heading:
    """
    Распознанный заголовок. Возвращается профилем из classify_heading() и
    интерпретируется движком (по полю kind) при сборке разделов.
    """

    number: Optional[str]      # «3.1.2» или None для именованных
    title: str                 # текст заголовка (может быть пустым для NUMBER_ONLY)
    level: int                 # глубина вложенности (1 — верхний уровень)
    kind: HeadingKind
    section_id: Optional[str] = None  # стабильный id для именованных разделов
    visual: bool = False              # подкреплён ли крупным/жирным шрифтом
    # раздел верхнего уровня с КАНОНИЧЕСКИМ названием+номером шаблона Минздрава.
    # Неканонические/со сдвигом номера level-1 (canonical=False) движок принимает
    # только при подтверждении оглавлением — иначе это ложный заголовок.
    canonical: bool = True
    # глава верхнего уровня, распознанная по РИМСКОМУ номеру («I. Краткая…»).
    # Движок включает такие главы только для документов, где подразделы —
    # пунктирные (N.M): в файлах с одиночно-арабскими подразделами римские главы
    # столкнулись бы номерами, поэтому там они отключаются целиком.
    roman: bool = False
    page: int = 0
    bbox: BBox = (0.0, 0.0, 0.0, 0.0)


@dataclass
class Table:
    """Таблица, найденная по bbox. Сохраняется сырой (best-effort)."""

    page: int
    number: Optional[str]      # «3» из подписи «Таблица 3 ...», если нашлась
    caption: Optional[str]
    raw_text: str              # текстовый дамп области таблицы
    bbox: BBox
    # True — таблица найдена fallback-детектором безрамочных таблиц и надёжно
    # разбить её на ячейки не удалось (текст сохранён блоком). В JSON поле
    # выводится только когда True, чтобы не менять схему обычных таблиц.
    low_confidence: bool = False
    # --- provenance (промпт 08); сериализуются В КОНЕЦ объекта, аддитивно ---
    claimed_span_uids: List[str] = field(default_factory=list)  # спаны в bbox таблицы
    source: str = "native"                                      # канал дампа таблицы


@dataclass
class Section:
    """Узел дерева разделов. Рекурсивно вложен через children."""

    number: Optional[str]
    title: str
    level: int
    text: str = ""
    children: List["Section"] = field(default_factory=list)
    # --- provenance (промпт 08); сериализуются В КОНЕЦ объекта, аддитивно ---
    section_id: Optional[str] = None   # стабильный id (для именованных разделов)
    page: int = 0                      # страница открывающего заголовка
    bbox: BBox = (0.0, 0.0, 0.0, 0.0)  # bbox открывающего заголовка
    span_uids: List[str] = field(default_factory=list)  # заголовок+тело узла (без детей)


@dataclass
class ExcludedSpec:
    """
    Маркеры регионов-исключений, которые профиль отдаёт движку, чтобы тот
    переключал режим конечного автомата. Каждый паттерн матчит НАЧАЛО строки,
    открывающей соответствующий регион.
    """

    toc: Pattern[str]          # «Оглавление»
    references: Pattern[str]   # «Список литературы»
    appendices: Pattern[str]   # «Приложение ...»

    def detect(self, text: str) -> Optional[str]:
        """Вернуть имя бакета ('toc'|'references'|'appendices') или None."""
        if self.toc.match(text):
            return "toc"
        if self.references.match(text):
            return "references"
        if self.appendices.match(text):
            return "appendices"
        return None


@dataclass
class ExcludedItem:
    """Элемент региона-исключения (промпт 08). Раньше это был плоский dict
    {title, text}; теперь несёт ещё provenance (span_uids). В JSON форма та же —
    объект с полями title/text (обратная совместимость) плюс новое span_uids В КОНЦЕ.

    Поддерживает dict-подобный доступ (.get/[]), чтобы код, читавший старые dict-и
    (stats/валидатор через JSON), продолжал работать без правок семантики."""

    title: str
    text: str
    span_uids: List[str] = field(default_factory=list)
    kind: str = "other"    # "toc"|"front_matter"|"references"|"appendices"|"other"

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        return key in ("title", "text", "span_uids", "kind")


@dataclass
class MetadataContext:
    """Всё, что нужно профилю для извлечения метаданных (передаёт движок)."""

    pdf_path: str
    source_file: str
    full_text: str             # очищенный текст всего документа
    first_page_text: str       # сырой текст первой страницы (титул)
    pages: List[Page]
    registry: Any = None       # источник реестра (профиль знает его тип) или None


@dataclass
class ParseResult:
    """Итог парсинга одного документа — сериализуется в JSON."""

    metadata: Dict[str, Any]
    sections: List[Section]
    tables: List[Table]
    excluded: Dict[str, List["ExcludedItem"]]
    stats: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    # provenance (промпт 08): промежуточное представление страниц (спаны/каналы).
    # Сериализуется в top-level блок "provenance"; на существующие поля не влияет.
    page_ir: List[PageIR] = field(default_factory=list)
