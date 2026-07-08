#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Профиль клинической рекомендации (КР).

Здесь сосредоточено ВСЁ знание о структуре документов Минздрава: как выглядят
заголовки, где метаданные, что относится к исключениям. Движок этого не знает —
он работает только через интерфейс DocumentProfile.

Эвристика заголовков объединяет лучшее из двух парсеров:
  * шрифт (размер/жирность) как сигнал «визуального» заголовка;
  * строгий разбор номера (1, 1.1, 1.5.1) с отсечением дат/номеров приказов;
  * для верхнего уровня — проверка по известным названиям разделов КР;
  * висящий номер («4.» + заголовок на следующей строке) — чинит потерю раздела 4;
  * отказ от строк оглавления (точки-лидеры) на уровне классификации.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from crparser.engine.models import (
    ExcludedSpec,
    Heading,
    HeadingKind,
    Line,
    MetadataContext,
)
from crparser.profiles.base import DocumentProfile
from crparser.profiles.registry import ClinicalRegistry

# Канонические разделы верхнего уровня КР: префикс названия -> допустимые номера.
# Шаблон Минздрава жёстко фиксирует и порядок, и нумерацию, поэтому заголовок
# уровня 1 принимается, только если ЕГО номер совпадает с каноническим. Это
# отсекает жирные нумерованные пункты-списки в теле («1. УЗ-допплерографию...»,
# «7. Краткая информация о методах...») — у них номер не соответствует названию.
_MAIN_SECTION_NUMBERS = {
    "краткая информация": {1},
    "диагностика": {2},
    "лечение": {3},
    "медицинская реабилитация": {4},
    "реабилитация": {4},
    "профилактика": {5},
    "организация оказания медицинской помощи": {6},
    "организация медицинской помощи": {6},
    "дополнительная информация": {7},
    "критерии оценки качества": {7, 8, 9},
}
_MAIN_HEADING_PREFIXES = tuple(_MAIN_SECTION_NUMBERS.keys())

# С чего заголовок начинаться НЕ может (маркеры списков и т.п.). U+F0B7 —
# «приватный» буллет, которым свёрстаны рекомендации в большинстве КР.
_BULLETS = "•·●◦‣⁃○▪□"
_FORBIDDEN_STARTS = ("+", "-", "*", "−", "–", "—", "✓") + tuple(_BULLETS)

# Признаки начала ТЕЛА раздела (а не продолжения заголовка): строку с такими
# признаками нельзя приклеивать к заголовку — иначе тело утекает в title (баг 2).
_RE_LIST_MARKER = re.compile(r"^[" + _BULLETS + r"*+–—-]\s?")
_RE_RECO_WORD = re.compile(
    r"^(не\s+)?рекоменд(уется|уются|овано|овани\w*|уем\w*)\b", re.IGNORECASE)
# тот же глагол рекомендации, но ГДЕ-УГОДНО в строке: строка-продолжение заголовка
# с «рекомендовано …» — это тело (часто остаётся, когда таблица вырезала буллет)
_RE_RECO_ANY = re.compile(
    r"(не\s+)?рекоменд(уется|уются|овано|овани\w*|уем\w*)\b", re.IGNORECASE)
_RE_SERVICE_WORD = re.compile(
    r"^(комментари\w*|уровень\s+убедительности|уровень\s+достоверности)\b",
    re.IGNORECASE)
# ГЛАГОЛЬНАЯ форма рекомендации где угодно в строке — признак, что «заголовок» на
# деле рекомендация-предложение из тела (не прилагательное «рекомендуемые»).
_RE_RECO_VERB = re.compile(
    r"\b(не\s+)?рекоменд(уется|уются|овано|овани\w*)\b", re.IGNORECASE)
# код медуслуги/АТХ в скобках: «(A22.26.010)», «(B03.016.003)».
_RE_SERVICE_CODE = re.compile(r"\([A-ZА-Я]\d{2}[.\d]+")

# Именованные (без номера) разделы -> стабильный id.
_NAMED_SECTIONS = (
    (re.compile(r"список\s+сокращ", re.IGNORECASE), "abbreviations"),
    (re.compile(r"термин\w*\s+и\s+определ", re.IGNORECASE), "terms"),
    (re.compile(r"критери\w*\s+оценки\s+качеств", re.IGNORECASE), "criteria_quality"),
)

# Лидеры оглавления (точки ИЛИ подчёркивания «....» / «____») — верный признак
# строки ToC, а не заголовка.
_RE_DOT_LEADER = re.compile(r"\.{3,}|_{3,}")
# Хвост строки оглавления: «...... 26» или «  26».
_RE_TOC_TAIL = re.compile(r"(\.{2,}\s*\d{1,4}|\s\d{1,4})\s*$")

# Санитарный потолок ГЛУБИНЫ номера подраздела (число компонентов N.M.K.L…).
# Реальные КР доходят до 7 уровней (КР1041_1: «3.1.1.1.2.1.1 Натрий»); 9 даёт
# запас, не открывая дорогу мусору. Единый источник и для регулярок номера, и
# для _valid_number, чтобы предел глубины не разъезжался между ними.
_MAX_NUMBER_DEPTH = 9
# Квантификатор «ещё до (_MAX_NUMBER_DEPTH-1) компонентов .N» после первого числа.
_NUM_TAIL = r"(?:\s*\.\s*\d+){0,%d}" % (_MAX_NUMBER_DEPTH - 1)

# Номер раздела + заголовок. Разделитель между ними — ЛЮБОЙ: пробел, «точка+пробел»
# или «точка вплотную» без пробела («1.2.Этиология») — баг 5. Граница «номер кончился»
# определяется первой БУКВОЙ заголовка (кириллица/латиница/«), а не обязательным
# пробелом; это же отсекает десятичные числа в прозе («1.2 раза» — далее идёт строчная
# буква и заголовок отвергается на уровне валидности подзаголовка).
_RE_NUMBERED = re.compile(
    r"^(\d+%s)\s*\.?\s*([A-Za-zА-Яа-яЁё«].*)$" % _NUM_TAIL)
# Признаки того, что «заголовок» на самом деле фрагмент тела (перекрёстная ссылка
# «…в разделе 5. Профилактика…»): ссылки [3,4,37], маркеры УУР/УДД «(5С)»,
# незакрытая скобка. У настоящего заголовка раздела КР такого не бывает.
_RE_REFCITE = re.compile(r"\[\s*\d")
_RE_EVID = re.compile(r"\(\s*\d+\s*[А-СA-Cа-сa-c]\s*\)")
_RE_NUMBER_ONLY = re.compile(r"^(\d+%s)\s*\.?$" % _NUM_TAIL)
# Артефакт извлечения: РОВНО ОДИН ведущий непробельный символ + ОБЯЗАТЕЛЬНЫЙ пробел
# перед ТОЧЕЧНЫМ номером подраздела («I 3.1. Консервативное лечение» — осколок
# колонтитула/номера перед номером раздела). Терпим только к ДОТ-номерам (после
# префикса сразу «цифра . цифра»): целочисленный level-1 «X 3 Лечение» под это НЕ
# подпадает. Группа 1 — остаток строки, начинающийся с самого номера.
_RE_LEADING_GLYPH = re.compile(r"^\S\s+(\d+\s*\.\s*\d.*)$")
_RE_DATE_LIKE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")
# OCR иногда ставит запятую вместо точки в номере раздела («4,2.2»). Чиним ТОЛЬКО
# однозначно иерархический номер в начале строки — c >=2 разделителями (>=3 группы
# цифр), чтобы не трогать десятичные в прозе («в 4,2 раза»).
_RE_COMMA_NUMBER = re.compile(r"^\d+[.,]\d+[.,]\d+(?:[.,]\d+)*")
_RE_CAPTION = re.compile(r"^(Таблица|Рисунок|Табл\.|Рис\.)\b", re.IGNORECASE)

# Глава верхнего уровня, пронумерованная РИМСКОЙ цифрой: «I. Краткая информация»,
# «V. Краткая…», «VII. Лечение…». Номер главы берём НЕ из римской цифры (часть КР
# нумерует главы с V/VI/…), а из канонического названия раздела шаблона (Краткая→1,
# Диагностика→2 …). Римскую цифру принимаем ТОЛЬКО как визуальный заголовок с
# каноническим названием — так отсекаются инлайновые «I»/«Тип I»/римские в
# классификациях («I. Плоскоклеточный … рак», набранные кеглем тела). Движок
# дополнительно отключает римские главы для файлов с одиночно-арабскими
# подразделами (там номера столкнулись бы) — см. Segmenter._roman_ok.
_RE_ROMAN_HEAD = re.compile(r"^([IVXL]{1,6})\s*[.)]\s+([A-Za-zА-Яа-яЁё«].*)$")
# висящий римский номер главы отдельной строкой: «IV.» (название — следующей
# строкой, как у арабского висящего «4.»).
_RE_ROMAN_ONLY = re.compile(r"^([IVXL]{1,6})\s*[.)]\s*$")
_ROMAN_VALS = {"I": 1, "V": 5, "X": 10, "L": 50}


def _roman_to_int(s: str) -> int:
    """Римское число -> int с проверкой канонической формы (иначе 0)."""
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN_VALS.get(ch, 0)
        if v == 0:
            return 0
        total += -v if v < prev else v
        prev = max(prev, v)
    return total if 1 <= total <= 39 and _int_to_roman(total) == s else 0


def _int_to_roman(n: int) -> str:
    out, table = "", ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
    for val, sym in table:
        while n >= val:
            out += sym
            n -= val
    return out

# Верхняя граница номера основного раздела КР (1..9: семь типовых разделов +
# «Критерии оценки качества» как 8/9; заодно отсекает даты и номера приказов).
_MAX_TOP_LEVEL = 9

# Префиксы строк-продолжений заголовка, перенесённого на новую строку.
_CONTINUATION_PREFIXES = (
    "или ", "и ", "в том числе", "показания", "медицинские показания",
    "противопоказания", "и противопоказания", "к применению", "применению",
    "методов", "метода", "диагностики", "лечения", "профилактики",
    "реабилитации", "инфекция", "инфекции", "заболевания", "заболеваний",
    "состояний", "состояния",
)

# Слова-разделы для инлайн-разбиения строки со спрятанным заголовком.
_INLINE_SPLIT = re.compile(
    r"\s+(?=(?:[1-7](?:\.\d+){0,4})\.?\s+"
    r"(?:Краткая|Диагностика|Лечение|Медицинская|Профилактика|Организация|"
    r"Дополнительная|Критерии)\b)"
)


# Битые концевые глифы, в которые OCR/битый cmap рендерит закрывающую «)»
# перенесённого скобочного хвоста («…состояний)» -> «…состояний!»/«…состояний’»).
_BROKEN_CLOSERS = "!’'`"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _ends_with_broken_closer(text: str) -> bool:
    """Строка кончается битым глифом-«закрывашкой» скобки (см. _BROKEN_CLOSERS)."""
    t = text.rstrip()
    return bool(t) and t[-1] in _BROKEN_CLOSERS


def _is_upper_run(s: str) -> bool:
    """Строка ПРОПИСНАЯ: есть буквы и НИ ОДНОЙ строчной. Тем же регистром свёрстана
    первая строка прописного заголовка-главы; у тела всегда есть строчные буквы."""
    has_alpha = False
    for ch in s:
        if ch.isalpha():
            has_alpha = True
            if ch.islower():
                return False
    return has_alpha


def _sentence_closed(s: str) -> bool:
    """Накопленный заголовок «закрыт» концом предложения (. ! ?)."""
    s = s.rstrip()
    return bool(s) and s[-1] in ".!?"


class ClinicalRecommendationProfile(DocumentProfile):
    """Профиль клинических рекомендаций Минздрава."""

    key = "cr"

    def __init__(self, registry: Optional[ClinicalRegistry] = None) -> None:
        #: реестр (может быть None/пустым — тогда работает fallback по титулу)
        self.registry: Optional[ClinicalRegistry] = registry

    @property
    def document_type(self) -> str:
        return "clinical_recommendation"

    # ======================================================================
    # 1. КЛАССИФИКАЦИЯ ЗАГОЛОВКОВ
    # ======================================================================

    def classify_heading(self, line: Line, body_size: float) -> Optional[Heading]:
        text = _norm(line.text)
        if not text or text.startswith(_FORBIDDEN_STARTS):
            return None
        # «4,2.2 Обезболивание…» -> «4.2.2 Обезболивание…» (OCR-запятая в номере)
        m = _RE_COMMA_NUMBER.match(text)
        if m and "," in m.group(0):
            text = m.group(0).replace(",", ".") + text[m.end():]
        # строки оглавления (с точками-лидерами) заголовками не считаем
        if _RE_DOT_LEADER.search(text):
            return None
        # подписи таблиц/рисунков — не заголовки разделов
        if _RE_CAPTION.match(text):
            return None

        visual = self._is_visual(line, body_size)

        numbered = self._parse_numbered(text, visual)
        if numbered is not None:
            return self._attach_pos(numbered, line)

        roman = self._parse_roman_chapter(text, visual)
        if roman is not None:
            return self._attach_pos(roman, line)

        roman_only = self._parse_roman_only(text, visual)
        if roman_only is not None:
            return self._attach_pos(roman_only, line)

        number_only = self._parse_number_only(text)
        if number_only is not None:
            return self._attach_pos(number_only, line)

        named = self._parse_named(text, visual)
        if named is not None:
            return self._attach_pos(named, line)

        # Последняя попытка: снять РОВНО один ведущий мусорный глиф + пробел перед
        # ТОЧЕЧНЫМ номером («I 3.1. …») и повторить разбор. Все прочие проверки
        # (валидность номера, заглавная/визуальность, не-фрагмент-тела) остаются в
        # силе — префикс лишь «съедается» до разбора. Терпимость строго к дот-
        # номерам: принимаем результат только если он level>=2 (есть точка).
        gm = _RE_LEADING_GLYPH.match(text)
        if gm is not None:
            rest = gm.group(1)
            deglyph = self._parse_numbered(rest, visual) \
                or self._parse_number_only(rest)
            if deglyph is not None and deglyph.number.count(".") >= 1:
                return self._attach_pos(deglyph, line)

        return None

    def _parse_numbered(self, text: str, visual: bool) -> Optional[Heading]:
        """Полный нумерованный заголовок «1.1 Жалобы и анамнез»."""
        m = _RE_NUMBERED.match(text)
        if not m:
            return None
        number = re.sub(r"\s+", "", m.group(1)).rstrip(".")
        title = _norm(m.group(2))
        if not title or title.startswith(_FORBIDDEN_STARTS):
            return None
        if self._looks_like_body_fragment(title):
            return None
        if not self._valid_number(number):
            return None
        level = number.count(".") + 1

        canonical = True
        if level == 1:
            if len(title) > 500:
                return None
            # канонический раздел КР (название+номер шаблона) — быстрый путь.
            canonical = self._valid_main_title(title, int(number.split(".")[0]))
            # но настоящий заголовок главы шаблона ВСЕГДА с заглавной буквы и не
            # оканчивается на «;»: «5. профилактика пролежней;» — это пункт списка
            # тела с приклеенным номером, а не глава. Снимаем с него каноничность,
            # тогда общий фильтр правдоподобности ниже его и отвергнет (баг 2).
            if canonical and (not (title[:1].isupper() or title[:1] == "«")
                              or title.rstrip().endswith(";")):
                canonical = False
            # неканонический/со сдвигом номера верхний раздел (нестандартная КР,
            # доп. разделы «Рецидив»/«Осложнения», реабилитация под №6 и т.п.):
            # принимаем как КАНДИДАТА — движок подтвердит его оглавлением. Требуем
            # правдоподобности заголовка (заглавная буква), не фрагмент тела.
            if not canonical and not (title[:1].isupper() or title[:1] == "«"):
                return None
        else:
            if len(title) > 300:
                return None
            # подзаголовок: либо подкреплён шрифтом (жирный/крупный), либо
            # начинается с заглавной (в части КР подзаголовки идут кеглем тела
            # и не жирные — тогда опираемся на заглавную букву + строгий номер).
            # Это отсекает нумерованные пункты-перечисления внутри предложений
            # («...методов [21]. 3.1 затем...» -> title со строчной отвергается).
            if not (visual or title[:1].isupper()):
                return None
        return Heading(number=number, title=title, level=level,
                       kind=HeadingKind.NUMBERED, visual=visual, canonical=canonical)

    def _parse_roman_chapter(self, text: str, visual: bool) -> Optional[Heading]:
        """Глава верхнего уровня с РИМСКИМ номером «I./V./VII. <канон. название>».

        Принимаем только ВИЗУАЛЬНЫЙ заголовок с каноническим названием раздела КР;
        номер берём из названия (Краткая→1 … Дополнительная→7). Это отсекает
        римские в прозе/классификациях.
        """
        if not visual:
            return None
        m = _RE_ROMAN_HEAD.match(text)
        if not m:
            return None
        if _roman_to_int(m.group(1)) == 0:      # не каноничная римская цифра
            return None
        title = _norm(m.group(2))
        if not title or self._looks_like_body_fragment(title):
            return None
        num = self._canonical_chapter_number(title)
        if num is None:
            return None
        return Heading(number=str(num), title=title, level=1,
                       kind=HeadingKind.NUMBERED, visual=True, canonical=True,
                       roman=True)

    def _parse_roman_only(self, text: str, visual: bool) -> Optional[Heading]:
        """Висящий римский номер главы «IV.» отдельной строкой (название придёт
        следующей строкой). Номер — по значению римской цифры (1..9); реальный
        фильтр — каноничность названия при доклейке (can_attach_title)."""
        if not visual:
            return None
        m = _RE_ROMAN_ONLY.match(text)
        if not m:
            return None
        val = _roman_to_int(m.group(1))
        if val < 1 or val > _MAX_TOP_LEVEL:
            return None
        return Heading(number=str(val), title="", level=1,
                       kind=HeadingKind.NUMBER_ONLY, visual=True, roman=True)

    @staticmethod
    def _canonical_chapter_number(title: str) -> Optional[int]:
        """Номер 1..7 для канонического названия ОДНОЙ главы шаблона КР (или None).

        Только названия с ОДНОЗНАЧНЫМ номером 1..7; «Критерии оценки качества»
        ({7,8,9}) сюда не попадают — их ловит именованный раздел.
        """
        low = _norm(title).lower()
        for prefix, numbers in _MAIN_SECTION_NUMBERS.items():
            if (low.startswith(prefix) or prefix.startswith(low + " ")) \
                    and len(numbers) == 1:
                num = next(iter(numbers))
                if 1 <= num <= 7:
                    return num
        return None

    def _parse_number_only(self, text: str) -> Optional[Heading]:
        """Висящий номер «4.» (заголовок придёт следующей строкой)."""
        m = _RE_NUMBER_ONLY.match(text)
        if not m:
            return None
        number = re.sub(r"\s+", "", m.group(1)).rstrip(".")
        if not self._valid_number(number):
            return None
        level = number.count(".") + 1
        return Heading(number=number, title="", level=level,
                       kind=HeadingKind.NUMBER_ONLY, visual=False)

    def _parse_named(self, text: str, visual: bool) -> Optional[Heading]:
        """Именованный раздел (Список сокращений / Критерии оценки качества...)."""
        sid = self.named_section_id(text)
        if sid is None:
            return None
        # именованный раздел — это короткий ЗАГОЛОВОК, подкреплённый шрифтом,
        # а не предложение в теле, начинающееся с тех же слов
        if not visual or len(text) > 90:
            return None
        return Heading(number=None, title=text, level=1,
                       kind=HeadingKind.NAMED, section_id=sid)

    @staticmethod
    def _looks_like_body_fragment(title: str) -> bool:
        """Похоже на фрагмент тела (перекрёстная ссылка), а не заголовок раздела."""
        if _RE_REFCITE.search(title):           # ссылки вида [3,4,37]
            return True
        if _RE_EVID.search(title):              # маркеры доказательности (5С)
            return True
        if title.count(")") > title.count("("):  # незакрытая/«висящая» скобка
            return True
        return False

    @staticmethod
    def _valid_number(number: str) -> bool:
        if _RE_DATE_LIKE.match(number):
            return False
        parts = [int(p) for p in number.split(".") if p.isdigit()]
        if not parts:
            return False
        # основные разделы КР — 1..9; это же отсекает даты и номера приказов
        if parts[0] < 1 or parts[0] > _MAX_TOP_LEVEL:
            return False
        if number.count(".") + 1 > _MAX_NUMBER_DEPTH:
            return False
        return True

    @staticmethod
    def _valid_main_title(title: str, number_top: Optional[int]) -> bool:
        """
        Заголовок уровня 1 валиден, если начинается с канонического названия
        раздела КР И его верхний номер совпадает с каноническим для этого названия
        (для number_top=None проверяем только название — случай висящего раздела).
        """
        low = _norm(title).lower()
        if low.startswith(_FORBIDDEN_STARTS):
            return False
        for prefix, numbers in _MAIN_SECTION_NUMBERS.items():
            # совпадение по канону ИЛИ по неполному заголовку — пословной вёрстке
            # («4. Медицинская» — первая строка заголовка «Медицинская реабилитация…»);
            # требуем границу слова, чтобы не ловить случайные предложения
            if low.startswith(prefix) or prefix.startswith(low + " "):
                return number_top is None or number_top in numbers
        return False

    def title_is_body_label(self, title: str) -> bool:
        """
        Заголовок — на самом деле метка ТЕЛА раздела с приклеенным номером, а не
        название раздела: рекомендация-предложение («Рекомендуется…», «…не
        рекомендовано») или служебный лейбл («Комментарии:», «Уровень
        убедительности/достоверности»). Берём ГЛАГОЛЬНЫЕ формы рекомендации (не
        прилагательное «рекомендуемые», легитимное в названии). Движок отвергает
        такой заголовок только если он НЕ подтверждён оглавлением.
        """
        t = _norm(title)
        return bool(_RE_RECO_VERB.search(t) or _RE_SERVICE_WORD.match(t))

    def main_title_canonical(self, title: str, number: Optional[str]) -> bool:
        """Публичная обёртка: заголовок level-1 — канонический раздел КР по шаблону?
        Движок этим решает, нужно ли подтверждать неканонический раздел оглавлением."""
        top = None
        if number:
            head = number.split(".")[0]
            top = int(head) if head.isdigit() else None
        return self._valid_main_title(title, top)

    @staticmethod
    def _is_visual(line: Line, body_size: float) -> bool:
        return line.size >= body_size * 1.35 or line.bold

    @staticmethod
    def _attach_pos(heading: Heading, line: Line) -> Heading:
        heading.page = line.page
        heading.bbox = line.bbox
        return heading

    def named_section_id(self, title: str) -> Optional[str]:
        for rx, sid in _NAMED_SECTIONS:
            if rx.search(title):
                return sid
        return None

    # ======================================================================
    # 2. СКЛЕЙКА МНОГОСТРОЧНЫХ ЗАГОЛОВКОВ / ВИСЯЩИЙ НОМЕР
    # ======================================================================

    @staticmethod
    def _is_body_line(text: str) -> bool:
        """
        Строка — начало ТЕЛА раздела (пункт рекомендации), а не продолжение
        заголовка. Используется, чтобы оборвать склейку заголовка (баг 2):
          * маркер списка в начале (буллет, тире-маркер);
          * служебные слова «Рекомендуется/Не рекомендуется/Комментарии/
            Уровень убедительности/достоверности»;
          * код медуслуги/АТХ в скобках «(A22.26.010)».
        """
        t = (text or "").strip()
        if not t:
            return False
        return bool(_RE_LIST_MARKER.match(t) or _RE_RECO_WORD.match(t)
                    or _RE_SERVICE_WORD.match(t) or _RE_SERVICE_CODE.search(t))

    def heading_breaks_before(self, line: Line, current_title: str) -> bool:
        text = _norm(line.text)
        # тело (маркер/служебное слово/код) — склейку заголовка обрываем
        if self._is_body_line(text):
            return True
        # глагол рекомендации где-либо в строке — это тело (частый кейс, когда
        # таблица вырезала ведущий буллет и осталась «… рекомендовано назначать…»)
        if _RE_RECO_ANY.search(text):
            return True
        # «осиротевшая» закрывающая скобка: ) без своей ( в текущем заголовке —
        # значит скобка открылась в теле (напр. в вырезанной строке) -> это тело
        cur = _norm(current_title)
        if text.count(")") > text.count("(") and cur.count("(") <= cur.count(")"):
            return True
        # строка-кандидат — завершённое предложение (несколько слов, конец на
        # . ! ?): это тело раздела, а не свёрстанный по словам фрагмент названия
        if re.search(r"[.!?]$", text) and len(text.split()) >= 3:
            return True
        # предыдущая принятая строка заголовка закончилась знаком конца
        # предложения — дальше идёт тело, а не продолжение названия
        prev = (current_title or "").rstrip()
        return bool(prev) and prev[-1] in ".!?;:"

    def is_title_continuation(self, current_title: str, line: Line,
                              heading: Heading, body_size: float) -> bool:
        text = _norm(line.text)
        if not text or text.startswith(_FORBIDDEN_STARTS):
            return False
        if _RE_DOT_LEADER.search(text):
            return False
        if self.heading_breaks_before(line, current_title):   # баг 2: не тянуть тело
            return False
        if re.match(r"^(Таблица|Рисунок|Список литературы|Приложение|"
                    r"Ключевые слова|Список сокращений|Оглавление)\b", text, re.IGNORECASE):
            return False
        if len(text) > 320:
            return False

        current = _norm(current_title)
        low = text.lower()

        # незакрытая скобка/висящий союз в текущем заголовке -> явно продолжение
        if current.endswith(("-", ",", "(", "/", " к", " и", " или", " по",
                             " при", " с", " в", " для")):
            return True
        # незакрытая «(» — продолжаем ТОЛЬКО до строки, где скобка закрывается:
        # иначе при потерянной OCR-ом «)» заголовок утягивает всё тело раздела.
        # Закрытием считаем и БИТЫЙ концевой глиф («)»->«!»/«’»/«`»): тот же битый
        # шрифт-слой, что рендерит «(группы … состояний)» как «…состояний!».
        if current.count("(") > current.count(")") and (
                ")" in text or _ends_with_broken_closer(text)):
            return True
        # перенесённый скобочный ХВОСТ названия: строка НАЧИНАЕТСЯ со «(», открывая
        # скобочный фрагмент «(группы заболеваний или состояний)». Принимаем и когда
        # скобка закрывается в этой же строке (сбалансированный хвост), и когда она
        # открыта в перенос — её закрытие подхватит ветка незакрытой «(» на следующем
        # шаге. heading_breaks_before уже отсёк тело/reco/код/законченное предложение;
        # «висящую» закрывающую скобку без своей открывающей отсекает body-fragment.
        if text.startswith("(") and len(text) < 120 \
                and not self._looks_like_body_fragment(text):
            return True
        # ПЕРЕНЕСЁННЫЙ фрагмент названия по служебному слову — но ТОЛЬКО со строчной
        # буквы: настоящее продолжение свёрстанного заголовка идёт серединой фразы
        # («…заболевания или / состояния», «…(группы…) / медицинские показания»),
        # т.е. со строчной. ПРОПИСНАЯ «Инфекция…»/«Заболевание…» — это НАЧАЛО нового
        # предложения тела (определение раздела), совпавшее с префиксом лишь после
        # приведения к нижнему регистру; в заголовок его тянуть нельзя (баг 2).
        if text[:1].islower() and low.startswith(_CONTINUATION_PREFIXES):
            return True
        # длинные заголовки верхнего уровня нередко продолжаются «показаниями» и т.п.
        if heading.level == 1 and len(text) < 260 and any(
                w in low for w in ("показания", "противопоказания", "применению", "методов")):
            return True
        # ПРОПИСНОЕ продолжение канонической главы верхнего уровня: заголовок-глава
        # свёрстан ПРОПИСНЫМИ и перенесён на несколько строк («ЛЕЧЕНИЕ, ВКЛЮЧАЯ …» /
        # «ТЕРАПИИ, ДИЕТОТЕРАПИЮ …»). islower()==False у всех букв, поэтому ветка
        # text.islower() ниже его не ловит, а список слов не покрывает («ТЕРАПИИ»,
        # «ЛЕЧЕНИЯ»). Принимаем по СТРУКТУРЕ: level-1 каноническая глава, накопленный
        # заголовок ещё «открыт» (нет конца предложения), строка короткая и ЦЕЛИКОМ
        # прописная — тем же регистром, что первая строка. Тело (обычный регистр,
        # есть строчные) сюда не попадает; списки/reco/коды отсёк heading_breaks_before.
        if heading.level == 1 and heading.canonical and len(text) < 200 \
                and _is_upper_run(text) and _is_upper_run(current) \
                and not _sentence_closed(current):
            return True
        if text[:1].islower() and len(text) < 220:
            return True
        return False

    def can_attach_title(self, line: Line, pending: Heading, body_size: float) -> bool:
        text = _norm(line.text)
        if not text or text.startswith(_FORBIDDEN_STARTS):
            return False
        if _RE_DOT_LEADER.search(text):
            return False
        if re.match(r"^(Таблица|Рисунок|Список литературы|Приложение|Оглавление)\b",
                    text, re.IGNORECASE):
            return False
        if self._is_body_line(text):       # баг 2: тело — не заголовок висящего номера
            return False
        if self._looks_like_body_fragment(text):
            return False
        # сам заголовок/номер следующей строкой — это не title для висящего номера
        if self._parse_numbered(text, True) or self._parse_number_only(text):
            return False
        if pending.level == 1:
            number_top = int(pending.number.split(".")[0]) if pending.number else None
            if self._valid_main_title(text, number_top):
                return True
            # неканонический верхний раздел — кандидат, движок подтвердит TOC
            return bool(text[:1].isupper()) and len(text) <= 300
        if len(text) > 300:
            return False
        return self._is_visual(line, body_size) or text[:1].isupper()

    def split_inline_headings(self, line: Line) -> List[Line]:
        text = _norm(line.text)
        if not text:
            return [line]
        parts = _INLINE_SPLIT.split(text)
        if len(parts) <= 1:
            return [line]
        return [line.clone(_norm(p)) for p in parts if _norm(p)]

    # ======================================================================
    # 3. РЕГИОНЫ-ИСКЛЮЧЕНИЯ
    # ======================================================================

    def excluded_regions(self) -> ExcludedSpec:
        return ExcludedSpec(
            toc=re.compile(r"^\s*(оглавление|содержание)\s*$", re.IGNORECASE),
            # строгий матч целой строки: «Список литературы» как заголовок,
            # а не строка оглавления (с точками) и не предложение в теле
            references=re.compile(r"^\s*список\s+литературы\s*[.:]?\s*$", re.IGNORECASE),
            # единственное число «Приложение» (не «Приложения ...» в перекрёстных
            # ссылках); вход в режим приложений дополнительно гейтится в движке
            # фактом, что список литературы уже встречен
            appendices=re.compile(r"^\s*приложение\b", re.IGNORECASE),
        )

    # ======================================================================
    # 4. МЕТАДАННЫЕ (реестр > титул, с fallback и warnings)
    # ======================================================================

    def extract_metadata(self, ctx: MetadataContext) -> Dict[str, Any]:
        warnings: List[str] = []

        meta: Dict[str, Any] = {
            "source_file": ctx.source_file,
            "document_type": self.document_type,
            "title": None,
            "id": None,
            "url": None,
            "year": None,
            "end_year": None,
            "age_group": None,
            "mkb_codes": [],
        }

        # --- fallback: разбор титульного листа PDF ---
        self._parse_title_page(ctx, meta)

        cr_id = self._extract_id(ctx)
        meta["id"] = cr_id
        if not cr_id:
            warnings.append("ID не извлечён из имени файла/титула")

        # --- основной источник: Excel-реестр по ID ---
        registry = ctx.registry if isinstance(ctx.registry, ClinicalRegistry) else None
        if registry is None or registry.is_empty:
            warnings.append("реестр недоступен — метаданные взяты из титульного листа PDF")
        elif cr_id:
            rid, row = registry.find(cr_id)
            if row is not None:
                meta["id"] = rid or cr_id
                self._apply_registry(meta, row)
            else:
                warnings.append(
                    f"КР '{cr_id}' не найдена в реестре — метаданные из титульного листа PDF")

        if not meta["title"]:
            warnings.append("название КР не извлечено")
        if not meta["mkb_codes"]:
            warnings.append("коды МКБ не извлечены")

        # публичная ссылка на карточку КР. Строим из финального id (стем имени
        # файла = ключ реестра, напр. «9_3»); при пустом/невалидном id — None,
        # чтобы не отдавать битую ссылку.
        cr_final_id = meta.get("id")
        meta["url"] = (f"https://cr.minzdrav.gov.ru/view-cr/{cr_final_id}"
                       if cr_final_id else None)

        meta["_warnings"] = warnings
        return meta

    # ---- титульный лист (fallback) ---------------------------------------

    def _parse_title_page(self, ctx: MetadataContext, meta: Dict[str, Any]) -> None:
        text = ctx.full_text or ""
        lines = [ln.strip() for ln in (ctx.first_page_text or "").splitlines() if ln.strip()]

        # название — строка после «Клинические рекомендации»
        for i, ln in enumerate(lines):
            if "клинические рекомендации" in ln.lower():
                for nxt in lines[i + 1:i + 6]:
                    if len(nxt) > 5 and not nxt.lower().startswith(("год", "id")):
                        meta["title"] = _norm(nxt)
                        break
                break

        # год утверждения (целенаправленно, не первый попавшийся 20xx)
        m = re.search(r"Год\s+утвержд\w*[^\d]{0,20}(\d{4})", text, re.IGNORECASE)
        if m:
            meta["year"] = m.group(1)

        # год окончания действия (если указан на титуле)
        m = re.search(r"Год\s+окончани\w*\s+действия\s*[:\-]?\s*(\d{4})", text, re.IGNORECASE)
        if m:
            meta["end_year"] = m.group(1)

        # возрастная категория
        m = re.search(r"Возрастная\s+категория\s*[:\-]?\s*"
                      r"(Взрослые\s+и\s+дети|Взрослые|Дети)", text, re.IGNORECASE)
        if m:
            meta["age_group"] = _norm(m.group(1))

        # коды МКБ из сегмента после «...здоровьем:» до «Год утверждения»
        seg = None
        m = re.search(r"здоровь\w*\s*:?\s*(.*?)\s*Год\s+утвержд", text, re.S | re.IGNORECASE)
        if m:
            seg = m.group(1)
        else:
            m = re.search(r"(?:МКБ[- ]?10|код\w*\s+по\s+МКБ)[^\n:]*:?\s*([^\n]+)",
                          text, re.IGNORECASE)
            if m:
                seg = m.group(1)
        if seg:
            meta["mkb_codes"] = self._normalize_mkb(seg)

    # ---- реестр (приоритетный источник) ----------------------------------

    def _apply_registry(self, meta: Dict[str, Any], row: Dict[str, Any]) -> None:
        title = ClinicalRegistry.value(row, ["Наименование", "Название",
                                             "Клиническая рекомендация"])
        mkb = ClinicalRegistry.value(row, ["МКБ-10", "МКБ 10", "Код МКБ", "Коды МКБ"])
        age = ClinicalRegistry.value(row, ["Возрастная категория", "Возраст"])
        developer = ClinicalRegistry.value(row, ["Разработчик"])
        approval = ClinicalRegistry.value(row, ["Статус одобрения НПС", "Статус одобрения"])
        pub_date = ClinicalRegistry.value(row, ["Дата размещения", "Дата публикации",
                                                "Дата утверждения"])
        app_status = ClinicalRegistry.value(row, ["Статус применения", "Статус"])

        if title:
            meta["title"] = title
        if mkb:
            meta["mkb_codes"] = self._normalize_mkb(mkb)
        if age:
            meta["age_group"] = age
        # год: приоритет — год утверждения с титула; иначе год даты размещения
        if not meta.get("year"):
            year = ClinicalRegistry.value(row, ["Год утверждения", "Год"])
            if not year and pub_date:
                ym = re.search(r"\b(20\d{2})\b", pub_date)
                year = ym.group(1) if ym else None
            meta["year"] = year

        # подчистка артефакта реестра «ассоциация., Московское» -> «ассоциация, Московское»
        if developer:
            developer = re.sub(r"\.\s*,", ",", developer)
        meta["developer"] = developer
        meta["approval_status"] = approval
        meta["publication_date"] = pub_date
        meta["application_status"] = app_status

    # ---- утилиты метаданных ----------------------------------------------

    @staticmethod
    def _extract_id(ctx: MetadataContext) -> Optional[str]:
        # Имя файла — авторитетный ключ реестра («КР1046_1.pdf» -> «1046_1»),
        # совпадает с форматом ID реестра. Берём его в первую очередь: «ID:...»
        # в теле PDF может быть ссылкой на ДРУГУЮ КР и давать ложное совпадение.
        stem = re.sub(r"\.pdf$", "", ctx.source_file, flags=re.IGNORECASE)
        m = re.search(r"КР\s*(\d+(?:_\d+)?)", stem, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"(\d+(?:_\d+)?)", stem)
        if m:
            return m.group(1)
        # запасной вариант — «ID:12» из текста титула
        m = re.search(r"\bID[:\s]+(\d+(?:_\d+)?)", ctx.first_page_text or "", re.IGNORECASE)
        return m.group(1) if m else None

    @staticmethod
    def _normalize_mkb(segment: str) -> List[str]:
        """
        Достать коды МКБ списком, нормализуя кириллические двойники латинских
        букв (С→C, Е→E, ...). «N13.0, N13.1, Q62.0» -> ['N13.0','N13.1','Q62.0'].
        """
        trans = str.maketrans("СABЕКМНОРТХ", "CABEKMHOPTX")
        seg = (segment or "").translate(trans)
        codes = re.findall(r"[A-Z]\d{2}(?:\.\d+)?", seg)
        # уникализируем, сохраняя порядок
        return list(dict.fromkeys(codes))
