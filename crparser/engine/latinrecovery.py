#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Latin recovery resolver (промпт 13b) — ЕДИНЫЙ резолвер починки латиницы, собирающий
все каналы в один проход по обучаемой зоне (sections, tables, metadata,
excluded.appendices). Работает ЗА ФЛАГОМ `--latin-recovery` (по умолчанию ВЫКЛ);
при выключенном флаге движок его не зовёт — вывод байт-в-байт baseline.

Иерархия каналов (дешёвый+достоверный -> дорогой), NIGHT_RUN ШАГ 1:
  1. A2-шаблон (latinnorm.normalize_code_token) — кир-гомографы БУКВ в слоте кода.
     false_substitution=0 ПО ПОСТРОЕНИЮ (срабатывает только при валидной форме).
  2. Гомограф ЦИФРЫ в цифровом слоте (latinnorm.coerce_entity) — ТОЛЬКО внутри
     РАСПОЗНАННОГО шаблона И только в TNM-staging-регионе (иначе «МО» мед.орг. цел).
  3. font-repair не-гомографов (fontrepair.decode_nonhomograph) — кир. буква БЕЗ
     латинского двойника (Б Ы Ь Ш …) в латинском токене; контур решает однозначно.
  4. eng-OCR по кропу (ocr._clip_text langs='eng') — всё, что не разрешили 1-3.
     С ГЕЙТОМ ВАЛИДАЦИИ по форме сущности для кодов (ШАГ 2).

Детекторы (ШАГ 2/3): внутрисегментный mixed-script; шаблон кода с нарушением
алфавита; «кириллический сирота» (токен начинается с Ы/Ь/Ъ, заглавная/цифра/скобка/
слэш ВНУТРИ токена); символ □ и unmapped-глифы (ШАГ 4).

Провенанс (ШАГ 5): КАЖДАЯ замена -> запись в блоке latin_recovery выходного JSON.
Молчаливых замен нет. Неуверенные -> needs_review + очередь верификации.

Ссылки [150, 151] и список литературы (excluded.references) НЕ трогаем (решение
команды): резолвер по построению работает ТОЛЬКО в sections/tables/appendices/metadata.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from crparser.engine import rumorph
from crparser.engine.latinnorm import (
    coerce_entity, normalize_code_token, _CYR2LAT)

# Критические сущности (13b Р3): решение требует ВИЗУАЛЬНОГО доказательства (E1/E2)
# или детерминированного шаблона; неразрешённая критическая латиница блокирует релиз.
CRIT_KINDS = frozenset({"icd", "atc", "tnm", "dose", "drug", "gene"})

# Кир. буквы БЕЗ латинского двойника: их присутствие внутри латиноподобного токена —
# достоверный сигнал порчи (font-repair решает по контуру однозначно).
_CYR_NONHOMO = set("БГДЖЗИЙЛПФЦЧШЩЪЫЬЭЮЯбгджзийлпфцчшщъыьэюя")
# Кир. буквы-гомографы (визуально = латиница).
_CYR_HOMO = set(_CYR2LAT.keys())
_CYR_ANY = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")

# Разделители, режущие токен на односкриптовые сегменты (I18: детектор СЕГМЕНТНЫЙ,
# иначе легит `T2-тип`, `HBsAg-положительный` дают 100% ложных срабатываний).
_SEG_SPLIT = re.compile(r"[-–—/.\s]+")
# Символы «сироты» ВНУТРИ токена (скобка/слэш/цифра/заглавная в середине).
_UNMAPPED = "□�"          # □ и replacement char

_WORD_CH = r"0-9A-Za-zА-Яа-яЁё"
_TOKEN_RE = re.compile(r"[^\s]+")
_DIG_RE = re.compile(r"\d")


def _digits(s: str) -> str:
    return "".join(_DIG_RE.findall(s))


# Цифро-глифы (буква на месте цифры): для сверки eng-кандидата с corrupt-кодом. б->6,
# О->0, l/I->1 и т.п. Помогает сопоставить `Н40.0б`<->`H40.06`, `501ЕС`<->`S01EC`.
_L2D_SIG = {"O": "0", "o": "0", "О": "0", "о": "0", "l": "1", "I": "1", "i": "1",
            "L": "1", "І": "1", "з": "3", "З": "3", "б": "6", "Ь": "6", "G": "6"}


def _digit_sig(s: str) -> str:
    """Цифровая сигнатура: цифры + цифро-глифы-буквы, приведённые к цифрам."""
    out = []
    for c in s:
        if c.isdigit():
            out.append(c)
        elif c in _L2D_SIG:
            out.append(_L2D_SIG[c])
    return "".join(out)


# Сегментация whitespace-токена: ДЕФИС, пробелы, ОДИНОЧНЫЙ слэш, `+`, скобки `()`, звёзды `*`.
# `.` и `[` не режем (коды `037.6`/`S01EC` и порча `1п[епог`/`8Иа//ег` целы). `+`/`()`/`*` —
# границы склеек «препарат+[комбинация», «слово)[цитата», «тимолол**0,25» -> режем, чтобы не
# латинизировать склеенные рус. слова/дозы (Group B, КР66_4/КР119_3). ОДИНОЧНЫЙ `/` —
# разделитель (`глютен/яйца`); ДВОЙНОЙ `//`=порча (`8Иа//ег`) -> НЕ режем (lookaround).
_SEG_RE = re.compile(r"([\s\-–—+()*]+|(?<!/)/(?!/))")

# Римская стадия с кир-гомографами (Н-Ш=II-III, П-1У=II-IV): ЛОССИ-порча (1 глиф -> 2-3
# римских), шаблон/контур бессильны, только eng-OCR по кропу. Детектим ЦЕЛЫЙ токен ДО
# сегментации (внутри дефис). Кандидат = обе стороны из римско-гомографного набора +
# есть кириллица (иначе `II-III` уже чист). Применяем ТОЛЬКО если eng даёт римскую форму.
_ROMAN_CH = "IVXLCMivxlНШПУХСМІ1lОо0"
_RX_ROMAN_STAGE = re.compile(
    r"^[%s]{1,4}[-–—][%s]{1,4}$" % (re.escape(_ROMAN_CH), re.escape(_ROMAN_CH)))
_RX_ROMAN_VALID = re.compile(r"^[IVX]{1,4}[-–][IVX]{1,4}$")


def _is_roman_stage(core: str) -> bool:
    return bool(_RX_ROMAN_STAGE.match(core)) and bool(_CYR_ANY.search(core))


# ============================ КАНАЛ C5 — АРХИТЕКТУРА ============================
# Класс C5 = ЛАТИНСКОЕ СЛОВО, ОТРЕНДЕРЕННОЕ ЦЕЛИКОМ КИРИЛЛИЦЕЙ (`рока`->portal,
# `йуег`->liver, `уапсез`->varices, `ШСС`->UICC). Замер recall показал: это 88% ВСЕХ
# пропусков детектора. Структурной аномалии у них НЕТ — от русского слова они неотличимы,
# поэтому anomaly-детекторы (Ы-старт/camelCase/цифра-внутри) их не видят ПО ПОСТРОЕНИЮ.
#
# ТРИ ГЕЙТА. Роли РАЗДЕЛЕНЫ ЖЁСТКО — не смешивать:
#
#   ГЕЙТ 1 — ГЕНЕРАТОР КАНДИДАТОВ (отвечает за RECALL): **БИТЫЙ ШРИФТ**.
#       Согласие контурной карты и /ToUnicode: здоровый шрифт 0.97-1.0, битый 0.00-0.01
#       (разрыв огромен, промежутка нет — I17). Спан в БИТОМ шрифте = кандидат. Аномалия
#       НЕ требуется -> C5 становится видимым. Замер: 68% документов имеют ВСЕ шрифты
#       честными -> у них НОЛЬ кандидатов и НОЛЬ вызовов OCR (Group B по построению,
#       здоровый корпус физически не может быть тронут).
#
#   ГЕЙТ 2 — ЯЗЫК (отвечает за PRECISION; ЗДЕСЬ ДЕРЖИТСЯ GROUP B): **pymorphy3
#       `word_is_known()`** (rumorph.py). Токен — реальная русская словоформа по словарю
#       OpenCorpora -> НЕ ТРОГАТЬ НИКОГДА. `боль` остаётся `боль`, `bone` НЕВОЗМОЖЕН.
#
#   ГЕЙТ 3 — ФОРМА РЕЗУЛЬТАТА (precision, но НЕ защита Group B): eng-OCR по кропу +
#       LAT-словарь корпуса. Результат обязан быть НАСТОЯЩИМ латинским словом, иначе
#       замены нет. ВНИМАНИЕ: словарь построен по ПОРЧЕНОМУ baseline и ЗАГРЯЗНЁН
#       транслит-мусором — в нём лежат `nayuenmoe`(=пациентов) и `ypobehb`(=Уровень),
#       т.е. РОВНО пины катастрофы (проверено). Гейт 3 их НЕ остановит. Русский спасает
#       ТОЛЬКО гейт 2 — см. I22.
#
# ПОЧЕМУ GROUP B НЕ МОЖЕТ ДЕРЖАТЬСЯ НА ГЕЙТЕ 1 (проверено, КР1_4): в битом документе
# ЧЕСТНОГО шрифта НЕТ — русский текст («Клинические», «печени», «стеатогепатита») лежит
# в ТОМ ЖЕ битом шрифте, что и порченая латиница. Флагать «всё в битом шрифте» = флагать
# весь русский. Поэтому русский спасает ТОЛЬКО гейт 2.
#
# ПОЧЕМУ ИМЕННО ЯЗЫКОВОЙ АРБИТР, А НЕ ВИЗУАЛЬНЫЙ МЕТОД. Порча сидит в /ToUnicode, а ГЛИФЫ
# РУССКОГО ТЕКСТА РЕНДЕРЯТСЯ ВЕРНО -> на странице ВИЗУАЛЬНО лежит правильный русский, и
# отличить его от правильной латиницы ВИЗУАЛЬНО НЕЧЕМ (любой OCR читает то же самое). Два
# отката это доказали: рукодельная карта транслитерации -> 196 рус. словоформ
# латинизировано; второе мнение движка `-l rus` -> `боль`->`bone` (I21). Отличает ТОЛЬКО
# ЯЗЫК. Поэтому гейт 2 — морфоанализатор, а `-l rus` УБРАН (он не только не защищал, но и
# ВРЕДИЛ recall: читая реально-латинские глифы «UICC» кириллицей, он «подтверждал» родной
# `ШСС` и блокировал ПРАВИЛЬНУЮ починку).
#
# ГРАНИЦА ГЕЙТА 2 (ЗАМЕРЕНО, см. rumorph.py): OpenCorpora — словарь ОБЩЕГО языка, и
# МЕДИЦИНСКОЙ лексики в нём НЕТ (`гепатоцеллюлярном`/`стеатогепатита`/`ГЦР` -> known=False).
# Гейт 2 закрывает общий русский (ровно те слова, на которых канал падал). Медицинский
# русский держат ОСТАЛЬНЫЕ гейты стека, и снимать их нельзя:
#   * 7+ СТРОЧНЫХ кириллических -> НЕ кандидат ПО ПОСТРОЕНИЮ. ГЛАВНАЯ защита мед. русского:
#     `гепатоцеллюлярном`/`стеатогепатита`/`холангиокарцинома` — все 7+ строчных;
#   * ALL-CAPS кириллица  -> НЕ кандидат ПО ПОСТРОЕНИЮ (рус. аббревиатура: ГЦР/СНВС/СОД/ВГД;
#     eng-OCR читает их реальным лат. словом — `СОД`->`COD` — и LAT-словарь это пропустит);
#   * freq>=3 в документе -> реальное повторяющееся слово/термин (I20).
# Транслит-гейт (`C5_TRANSLIT_SIM`) в этот список НЕ входит: замерено, что на КОРОТКИХ
# словах он бесполезен (`боль`->`bone` sim=0.50 -> ПРОПУСКАЕТ). Он лишь дешёвый фильтр
# очевидных артефактов, а не защита.
#
# НЕ «ОПТИМИЗИРОВАТЬ»: снятие ЛЮБОГО из этих гейтов = латинизация русского. Гейт 1 без
# гейта 2 НЕ безопасен, а гейт 3 Group B НЕ держит (словарь загрязнён, см. выше).
# Порог менять ради прохождения пина — ЗАПРЕЩЕНО (I11).
# КОРПУСНЫЙ RU-СЛОВАРЬ (>=3 док.) СЮДА ВНОСИТЬ НЕЛЬЗЯ: на нём стоит единственный
# НЕциркулярный тест (`test_c5_did_not_touch_corpus_russian_vocabulary`) — внесёшь в гейт,
# и он станет тавтологией, как уже случалось дважды.
# ==============================================================================
# Визуальная транслитерация: что eng-OCR выдаёт, читая кириллический глиф латиницей.
_TRANSLIT = {
    "А": "A", "Б": "b", "В": "B", "Г": "r", "Д": "A", "Е": "E", "Ё": "E", "Ж": "x",
    "З": "3", "И": "u", "Й": "u", "К": "K", "Л": "n", "М": "M", "Н": "H", "О": "O",
    "П": "n", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Ф": "o", "Х": "X", "Ц": "u",
    "Ч": "4", "Ш": "w", "Щ": "w", "Ъ": "b", "Ы": "bl", "Ь": "b", "Э": "3", "Ю": "10",
    "Я": "R",
    "а": "a", "б": "6", "в": "b", "г": "r", "д": "a", "е": "e", "ё": "e", "ж": "x",
    "з": "3", "и": "u", "й": "u", "к": "k", "л": "n", "м": "m", "н": "h", "о": "o",
    "п": "n", "р": "p", "с": "c", "т": "t", "у": "y", "ф": "o", "х": "x", "ц": "u",
    "ч": "4", "ш": "w", "щ": "w", "ъ": "b", "ы": "bl", "ь": "b", "э": "3", "ю": "10",
    "я": "r",
}
# 7+ подряд СТРОЧНЫХ кириллических = русское слово (в т.ч. МЕДИЦИНСКОЕ, которого нет ни
# в OpenCorpora, ни в частотной защите). C5 не трогает такой токен НИКОГДА — правило 4
# гейта 2, `_c5_protected`. Порченая латиница почти всегда несёт заглавную/цифру/чужой
# глиф (`оезорЬадиз`, `8уз1етайс`), а короткая (`рока`/`уапсез`) не дотягивает до 7.
_RX_LONG_LOWER_CYR = re.compile(r"^[а-яё]{7,}$")

# Порог МОЛЧАЛИВОЙ (auto) замены термина: скелет native должен совпасть с eng-словом.
# Ниже — только needs_review (человек по кропу). Замер: E3 («слово есть в этом документе»)
# правом на auto быть НЕ МОЖЕТ — `the`/`and`/`for` есть везде, и любой сбой выравнивания
# давал молчаливую порчу термина (307 замен при sim<0.5). См. `_sweep_line`.
C5_AUTO_SIM = 0.8

# OCR ≈ транслит -> артефакт чтения кириллицы (держит ДЛИННЫЕ мед. формы, см. гейт 2).
C5_TRANSLIT_SIM = 0.62
# OCR совсем не похож на чтение native -> выравнивание сбилось, не улика.
C5_ALIGN_MIN = 0.30

# ===================== C5: ВКЛЮЧЁН, но ТОЛЬКО с языковым арбитром =====================
# История (I21): канал был NO-GO, потому что гейт 2 (precision) проваливался ДВАЖДЫ —
# рукодельная карта транслитерации (196 рус. словоформ латинизировано) и второе мнение
# движка `-l rus` (`боль`->`bone`). Оба отката ПРАВИЛЬНЫ, и причина была не в реализации:
# ВИЗУАЛЬНЫМ методом русский от латиницы в битом документе не отличить.
# Теперь гейт 2 — ЯЗЫКОВОЙ арбитр (pymorphy3 `word_is_known`, rumorph.py): `боль` —
# валидная рус. словоформа -> защищена -> `bone` НЕВОЗМОЖЕН.
#
# FAIL-CLOSED (I5): арбитр недоступен -> канал ВЫКЛЮЧЕН ЦЕЛИКОМ. Трактовать «пакета нет»
# как «слово неизвестно» = латинизировать русский на машине без зависимости и молча
# произвести ДРУГОЙ корпус — ровно дыра I5 (отсутствующий Tesseract -> exit 0, другой выход).
# Поэтому C5_ENABLED — не константа-переключатель, а ФУНКЦИЯ от наличия арбитра.
C5_ENABLED = True


def c5_active() -> bool:
    """Канал C5 работает? Требует ЯЗЫКОВОГО арбитра (fail-closed, I5): без pymorphy3
    канал не запускается вообще, а не «латинизирует всё подряд»."""
    from crparser.engine import rumorph
    return bool(C5_ENABLED) and rumorph.available()


def _translit(s: str) -> str:
    """Визуальное чтение кириллицы латинским алфавитом (то, что делает eng-OCR)."""
    return "".join(_TRANSLIT.get(c, c) for c in s).lower()


_LAT_VOCAB: Optional[frozenset] = None


def latin_vocab() -> frozenset:
    """Корпусный словарь ЧИСТОЙ латиницы (построен по baseline ДО восстановления).
    Роль: PRECISION-подпорка гейта 2 — отсекает OCR-мусор, который случайно разошёлся
    с транслитерацией. НЕ является защитой Group B (её держит транслит-гейт)."""
    global _LAT_VOCAB
    if _LAT_VOCAB is None:
        try:
            p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "data", "latin_vocab.json")
            _LAT_VOCAB = frozenset(json.load(open(p, encoding="utf-8"))["words"])
        except Exception:  # noqa: BLE001
            _LAT_VOCAB = frozenset()
    return _LAT_VOCAB


def _strip_edges(tok: str) -> Tuple[str, str, str]:
    """Отделить ВСЮ ведущую/замыкающую пунктуацию от ядра (буквы/цифры). Внутренние
    `.`/`/`/`[` сохраняются (коды D37.6, порча 8Иа//ег/1п[епог). `(Н40.0б)[` -> `Н40.0б`."""
    m = re.match(r"^(\W*)(.*?)(\W*)$", tok, re.S)
    if not m or not m.group(2):
        return "", tok, ""
    return m.group(1), m.group(2), m.group(3)


# ---- гейты формы сущности (eng-OCR-результат принимается только валидной формы) ----
_RX_ATC = re.compile(r"^[A-Z]\d{2}[A-Z]{2}\d{2}$|^[A-Z]\d{2}[A-Z]{2}$|^[A-Z]\d{2}[A-Z]$")
_RX_ICD = re.compile(r"^[A-Z]\d{2}(?:\.\d{1,2})?$")
_RX_TNM = re.compile(r"^p?(?:T(?:is|[0-4][a-d]?|[xX])|N[0-3xX]|M[01xX]|G[1-4xX])$")
_RX_STAGE = re.compile(r"^[IVX]{1,4}(?:[-–][IVX]{1,4})?$")


def entity_valid(text: str, kind: str) -> bool:
    """Форма-гейт: код-кандидат принимается ТОЛЬКО валидной формы (501ЕС->SOLEC
    отвергается — не ATC). Термин: чистая латиница без кириллицы."""
    t = text.strip("().,;:[] ")
    if kind == "atc":
        return bool(_RX_ATC.match(t))
    if kind == "icd":
        return bool(_RX_ICD.match(t))
    if kind == "tnm":
        return bool(_RX_TNM.match(t))
    if kind == "stage":
        return bool(_RX_STAGE.match(t))
    if kind in ("term", "drug", "gene"):
        return bool(t) and not _CYR_ANY.search(t) and any(c.isalpha() for c in t)
    if kind == "dose":
        return bool(_DIGIT.search(t))
    return False


# ---- TNM-контекст: цифро-гомограф коэрцим ТОЛЬКО в staging-регионе (Group B safety) --
# Кортеж стадирования: T.. N. M. рядом (кир/лат). Определение категории: «Мх -», «МО -».
_RX_TNM_TUPLE = re.compile(
    r"[TТ]\d[a-dа-дЬъ]?\s+[NNНЫ]\d?\s+[MМ][O0Oо0]", re.IGNORECASE)
_RX_TNM_DEFN = re.compile(
    r"(?:^|[\s(])[pрTТNNНЫMМGГ][ОOoO0x1-4хХ][a-dа-д]?\s*[-—–]\s", re.MULTILINE)
# M-категория (метастазы) — ОДНОЗНАЧНЫЙ маркер стадирования: Мх/МО/М1 - определение.
# T-клеточная номенклатура (Т2-лимфоциты/T0-) M-категории НЕ имеет -> не staging.
_RX_TNM_M = re.compile(r"(?:^|[\s(])[MМ][ОOoO0хХx1]\s*[-—–]\s", re.MULTILINE)


def is_tnm_region(text: str) -> bool:
    """Регион является TNM-стадированием? Кортеж «T.. N. M.» ИЛИ M-категория-определение
    + >=2 определения. Требование M-категории отсекает T-клеточную номенклатуру
    (Т2-лимфоциты/Т0-хелперы), где нет метастаз-категории (Group B: здоровый файл цел)."""
    if not text:
        return False
    if _RX_TNM_TUPLE.search(text):
        return True
    return bool(_RX_TNM_M.search(text)) and len(_RX_TNM_DEFN.findall(text)) >= 2


# ---- детекция подозрительных токенов -------------------------------------------------
def _seg_mixed_script(core: str) -> bool:
    """Внутрисегментное смешение алфавитов (I18), НО только когда токен — порченая
    ЛАТИНИЦА (латиница в БОЛЬШИНСТВЕ). Иначе `ляторами(L` (склейка рус.слова с латинским
    хвостом) ложно латинизировался бы — рус. часть больше, значит это рус. слово (Group B)."""
    for seg in _SEG_SPLIT.split(core):
        if len(seg) < 2:
            continue
        n_lat = len(re.findall(r"[A-Za-z]", seg))
        n_cyr = len(re.findall(r"[А-Яа-яЁё]", seg))
        if n_lat and n_cyr and n_lat >= n_cyr:
            return True
    return False


_HAS_LOWER = re.compile(r"[а-яёa-z]")


def _cyr_orphan(core: str) -> bool:
    """«Кириллический сирота» (NIGHT_RUN ШАГ 3): почти-кир. токен, который на самом деле
    латинское слово. КОНСЕРВАТИВНО (Group B): ALL-CAPS рус. аббревиатуры (ГЦР/РФ/СНВС/
    СОСТАВ) и чистые рус. слова НЕ ловим. Сигнал требует СТРОЧНОЙ буквы (у порченой
    латиницы 8рае1И/Ыуег строчные есть; у рус. аббревиатуры — нет) ИЛИ Ы/Ь/Ъ-старта."""
    if not core or not _CYR_ANY.search(core):
        return False
    has_lower = bool(_HAS_LOWER.search(core))
    letters = [c for c in core if c.isalpha()]
    n_digit = sum(1 for c in core if c.isdigit())
    # 1) начинается с Ы/Ь/Ъ — невозможно в начале русского слова (Ых, ЫазаШ, Ыуег)
    if core[0] in "ЫЬЪ":
        return True
    if not has_lower:
        return False                       # ALL-CAPS кириллица -> рус. аббревиатура, не сирота
    # 2) скобка/двойной-слэш/$ ВНУТРИ токена, за которой СРАЗУ БУКВА (8Иа//ег: `/е`;
    #    1п[епог: `[е`) — глиф-ошибка внутри слова. Скобка перед ЦИФРОЙ (`стационар[1,2`,
    #    `лечение[4`) — это ЦИТАТА, склеенная с рус. словом -> НЕ латинизируем (Group B).
    if re.search(r"[\[\]/$][а-яёa-zА-ЯЁA-Z]", core):
        return True
    # 3) camelCase: >=3 подряд строчных, затем КИРИЛЛИЧЕСКАЯ ЗАГЛАВНАЯ, за которой НЕ идёт
    #    длинное (>=4) строчное слово (ТетрогаШ: «етрога»+Ш; КатоРзку: Р+зку). Заглавная,
    #    открывающая ВТОРОЕ рус. слово (`эндокардитаРекомендуется`), — склейка двух рус. слов
    #    -> НЕ трогаем. Заглавная ЛАТИНСКАЯ (`неуточненнаяD56`) — тоже не наш случай (Group B).
    if re.search(r"[а-яё]{3,}[А-ЯЁ](?![а-яё]{4})", core):
        return True
    # 4) цифра ВНУТРИ слова — БУКВА-ЦИФРА-БУКВА (8рае1И: `е1И`; Е1зепЬаиег: `Е1з`). Цифра
    #    ОБЯЗАНА быть окружена буквами: отсекает и ХВОСТОВЫЕ сноски (`исследования12`,
    #    `Дискератоз7` — цифра в конце), и ВЕДУЩИЕ номера списков (`3Эозинофильные`,
    #    `8Фиброз` — цифра в начале, КР1000_1 healthy!). Плюс исключаем число+рус.слово.
    if (len(letters) >= 3 and len(letters) > n_digit
            and re.search(r"[а-яёa-zА-ЯЁA-Z]\d[а-яёa-zА-ЯЁA-Z]", core)
            and not re.fullmatch(r"\d+[а-яёa-z]+", core)):
        return True
    return False


def _has_unmapped(core: str) -> bool:
    return any(c in _UNMAPPED for c in core)


def _looks_code(core: str) -> Optional[str]:
    """Токен ПОХОЖ на медкод, но с нарушением алфавита? Вернуть kind-гипотезу
    (icd/atc/tnm) или None. Проверяет форму ПОСЛЕ маппинга гомографов-БУКВ.
    ПЕРВЫЙ символ ОБЯЗАН быть буквой (лат/кир) — иначе чистые числа (324/100/430)
    ложно матчатся как коды. Ведущая цифро-глиф-буква (037.6/501ЕС) ловится
    отдельным узким правилом `_leading_glyph_code`."""
    lat = "".join(_CYR2LAT.get(c, c) for c in core)
    has_digit = _has_digit(core)
    # ICD/ATC ОБЯЗАНЫ содержать реальную цифру (иначе «ООО»(рус.) ложно = «O00»(icd)).
    if has_digit:
        # ICD: БУКВА + 2 симв + опц .цифры  (D37.6, H40.06, Н40.0б)
        if re.fullmatch(r"[A-Za-z][0-9OoОо][0-9OoОо](?:\.[0-9OoОоБбЗз]{1,2})?", lat):
            return "icd"
        # ATC: БУКВА + 2 цифры + 2 буквы (+2 цифры)  (S01EC, L04AA02, C10AС)
        if re.fullmatch(r"[A-Za-z][0-9Oo][0-9Oo][A-Za-z]{2}(?:[0-9Oo]{2})?", lat):
            return "atc"
    # TNM: p?[TNMG] + значение (T1a, МО, Ых, Т1Ъ). Требуем НЕ-пустое значение.
    if re.fullmatch(r"p?[TNMGТНМГ][ОOoо0-9xXхХa-dа-дЬъ]{1,3}", lat):
        return "tnm"
    return None


# Узкие правила для кодов с ВЕДУЩИМ цифро-глифом (буква отрендерена цифрой):
#   ICD никогда не начинается с 0 -> `0dd(.d)?` = O/D/Q->0 порча (037.6->D37.6);
#   ATC S-класс -> `[58]dd + 2 буквы` = S->5/8 порча (501ЕС/801ЕС->S01EC).
_RX_LEAD_ICD = re.compile(r"^0[0-9OoОо][0-9OoОо](?:\.[0-9OoОо]{1,2})?$")
_RX_LEAD_ATC = re.compile(r"^[58][0-9Oo][0-9Oo][А-Яа-яA-Za-z]{2}(?:[0-9Oo]{2})?$")


def _leading_glyph_code(core: str) -> Optional[str]:
    """Код с ведущим цифро-глифом вместо буквы (узко, чтобы не ловить числа)."""
    if _RX_LEAD_ICD.match(core):
        return "icd"
    if _RX_LEAD_ATC.match(core):
        return "atc"
    return None


def _has_digit(core: str) -> bool:
    return bool(_DIGIT.search(core))


def detect(core: str, tnm_ok: bool = False) -> Tuple[bool, List[str], Optional[str]]:
    """Подозрителен ли токен? -> (suspicious, why[], kind_hint). КОНСЕРВАТИВНО:
    здоровый русский/латинский токен -> (False, [], None). tnm_ok — находимся ли в
    TNM-staging-регионе (иначе бездигитные TNM-формы МО/Тх = рус. аббревиатуры, не коды)."""
    why: List[str] = []
    if not core:
        return False, why, None
    if _has_unmapped(core):
        why.append("unmapped_glyph")
    kind = _looks_code(core)
    lead = _leading_glyph_code(core)
    if lead:
        kind = kind or lead
        why.append("leading_glyph_code")
    # TNM АМБИВАЛЕНТНА с рус./клеточной номенклатурой (МО=мед.орг, Т2=Т2-лимфоцит) ->
    # код ТОЛЬКО в staging-регионе. Вне региона TNM-форму не трогаем (Group B).
    if kind == "tnm" and not tnm_ok:
        kind = None
    # ген/белок с кириллицей (СТЪА4->CTLA4, РИ1->PD1): буквы+цифра-форма + кириллица.
    # Разрешается ТОЛЬКО через E3 (чистое вхождение в документе) -> ложные (ВГВ2000) не
    # проходят гейт применения. Здесь только помечаем подозрение.
    if not kind and _CYR_ANY.search(core) and _looks_critical_term(core):
        kind = "gene"
        why.append("gene_shape")
    # нарушение алфавита в КОД-шаблоне: кириллица внутри кода (C10AС, Т1а, МО-в-регионе)
    if kind and kind != "gene" and _CYR_ANY.search(core):
        why.append("code_alphabet_violation")
    if _cyr_orphan(core):
        why.append("cyr_orphan")
    # mixed_script НЕ триггерит сам по себе (Group B: `ляторами(L`/формулы). Информативно.
    if _seg_mixed_script(core) and (kind or "cyr_orphan" in why):
        why.append("mixed_script")
    return (bool(why), why, kind)


class LatinRecoverer:
    """Единый резолвер. Держит (лениво) fitz-документ, OCR-восстановитель и
    font-repair; строит индекс native-строк для локации токенов и кропов."""

    def __init__(self, pdf_path: str, doc_id: str = "",
                 queue_dir: Optional[str] = None, enable_ocr: bool = True) -> None:
        self._path = pdf_path
        self._doc_id = doc_id or os.path.splitext(os.path.basename(pdf_path))[0]
        self._queue_dir = queue_dir
        self._enable_ocr = enable_ocr
        self._doc = None                       # fitz.Document (лениво)
        self._ocr = None                       # OcrRecoverer (лениво)
        self._fr = None                        # FontRepairer (лениво)
        self._ocr_ok = False
        self._line_index: Dict[int, List[Tuple[Tuple[float, float, float, float], str]]] = {}
        self._trace_index: Dict[int, List[dict]] = {}
        self._doc_latin_vocab: Optional[set] = None
        self._corrupt_fonts: Optional[set] = None   # ГЕЙТ 1 канала C5 (лениво)
        self._c5_queued: set = set()           # дедуп задач `c5_long_lowercase` в очередь
        self._eng_line_cache: Dict[Tuple[int, tuple], str] = {}
        self.prov: List[Dict[str, Any]] = []   # блок latin_recovery
        self.queue: List[Dict[str, Any]] = []  # неуверенные -> verify_queue
        self.unresolved_critical: List[Dict[str, Any]] = []

    # ---- ленивая инициализация тяжёлых ресурсов ----
    def _ensure_doc(self):
        if self._doc is None:
            import fitz  # noqa
            self._doc = fitz.open(self._path)
        return self._doc

    def _ensure_ocr(self) -> bool:
        if self._ocr is None and self._enable_ocr:
            from crparser.engine.ocr import OcrRecoverer
            from crparser.engine.pdf_reader import _pdf_sha256
            self._ocr = OcrRecoverer()
            self._ocr_ok = self._ocr.available()
            if self._ocr_ok:
                self._ocr._doc_id = self._doc_id
                try:
                    self._ocr._pdf_sha = _pdf_sha256(self._path)
                except Exception:  # noqa: BLE001
                    self._ocr._pdf_sha = "latinrec_" + self._doc_id
        return self._ocr_ok

    def _ensure_fr(self):
        if self._fr is None:
            from crparser.engine.fontrepair import FontRepairer
            self._fr = FontRepairer(self._ensure_doc())
        return self._fr

    def close(self):
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:  # noqa: BLE001
                pass
            self._doc = None

    # ---- индексы native-слоя (для локации токена и кропа) ----
    def _build_line_index(self, pno: int):
        if pno in self._line_index:
            return
        lines = []
        try:
            page = self._ensure_doc()[pno]
            for blk in page.get_text("dict").get("blocks", []):
                for ln in blk.get("lines", []):
                    txt = "".join(sp["text"] for sp in ln.get("spans", []))
                    if txt.strip():
                        lines.append((tuple(ln["bbox"]), txt))
        except Exception:  # noqa: BLE001
            pass
        self._line_index[pno] = lines

    def _locate(self, needle: str, page_hint: Optional[int] = None):
        """Найти (pno0, bbox, line_text) строки native-слоя с needle. page_hint (1-based)
        сначала, затем весь документ. corrupt-формы рендерятся в native даже если
        JSON-текст восстановлен full-OCR."""
        key = needle.replace(" ", "")
        doc = self._ensure_doc()
        order = []
        if page_hint:
            order = [page_hint - 1]
        order += [p for p in range(len(doc)) if p not in order]
        for pno in order:
            if pno < 0 or pno >= len(doc):
                continue
            self._build_line_index(pno)
            for bbox, txt in self._line_index[pno]:
                if key and key in txt.replace(" ", ""):
                    return pno, bbox, txt
        return None, None, None

    # ---- каналы ----
    def _fontrepair(self, pno: int, core: str, kind_hint: Optional[str]):
        """font-repair не-гомографов по контуру (Ъ->b, Ы->N …). -> (fixed, ...) | None."""
        if not any(c in _CYR_NONHOMO for c in core):
            return None
        try:
            fr = self._ensure_fr()
            page = self._ensure_doc()[pno]
            pf: Dict[str, list] = {}
            for f in page.get_fonts(full=True):
                pf.setdefault(f[3].split("+")[-1], []).append(f[0])
            key = core.replace(" ", "")
            for span in page.get_texttrace():
                tu = "".join(chr(c[0]) for c in span["chars"])
                if key not in tu.replace(" ", ""):
                    continue
                gids = [c[1] for c in span["chars"]]
                for x in pf.get(span["font"].split("+")[-1], []):
                    info = fr._font(x)
                    if not info or not info.corrupt:
                        continue
                    if not all(g in info.contour for g in gids):
                        continue
                    fixed, idx = fr.decode_nonhomograph(x, gids, tu)
                    if not idx:
                        continue
                    # достать соответствующее слово из починенной строки
                    for w in fixed.split():
                        wc = w.strip("().,;:[]")
                        if wc and not _CYR_ANY.search(wc):
                            # догнать гомографы шаблоном
                            a2 = normalize_code_token(wc)
                            cand = a2[0] if a2 else wc
                            if kind_hint and entity_valid(cand, kind_hint):
                                return cand, "font_repair", "non-homograph-contour", 0.9
                            if not kind_hint and len(wc) >= 2:
                                return wc, "font_repair", "non-homograph-contour", 0.85
        except Exception:  # noqa: BLE001
            return None
        return None

    def _eng_line(self, pno: int, bbox, langs: str = "eng") -> str:
        """OCR кропа строки. `langs='rus'` — ВТОРОЕ МНЕНИЕ для гейта 2 канала C5: спросить
        движок, читается ли это как РУССКИЙ (см. архитектуру C5)."""
        ck = (pno, tuple(round(x) for x in bbox), langs)
        if ck in self._eng_line_cache:
            return self._eng_line_cache[ck]
        if not self._ensure_ocr():
            self._eng_line_cache[ck] = ""
            return ""
        try:
            page = self._ensure_doc()[pno]
            txt = self._ocr._clip_text(page, pno, bbox, psm=7, scale=2.5, langs=langs)
        except Exception:  # noqa: BLE001
            txt = ""
        self._eng_line_cache[ck] = txt
        return txt

    # ---- главный проход: РЕЗОЛЮЦИЯ (строит карты) -> ПРИМЕНЕНИЕ (по зонам) ----
    def recover(self, sections: List, tables: List, excluded: Dict,
                metadata: Dict) -> None:
        """Мутирует текстовые поля обучаемой зоны, наполняет self.prov/queue/…."""
        zones = self._gather_zones(sections, tables, excluded, metadata)
        self._doc_latin_vocab = _collect_latin_vocab(sections, tables, excluded)
        # частоты кир-токенов по всему документу — защита от латинизации рус. слов
        # (Group B): токен, повторяющийся >=3 раз, — реальная рус. аббревиатура/слово.
        self._freq: Dict[str, int] = {}
        self._page_hint: Dict[str, int] = {}
        core_tnm: set = set()
        core_seen: set = set()
        roman: set = set()                 # римские стадии (целый токен, до сегментации)
        for z in zones:
            for tok in z["text"].split():
                _, wc, _ = _strip_edges(tok)
                if wc and _is_roman_stage(wc):
                    roman.add(wc)
                    if z["page"] and wc not in self._page_hint:
                        self._page_hint[wc] = z["page"]
            for core in _iter_segments(z["text"]):
                if not core:
                    continue
                if _CYR_ANY.search(core):
                    self._freq[core] = self._freq.get(core, 0) + 1
                core_seen.add(core)
                if z["page"] and core not in self._page_hint:
                    self._page_hint[core] = z["page"]
                if z["tnm"]:
                    core_tnm.add(core)
        # РЕЗОЛЮЦИЯ: заполнить self._g (глобальные), self._tnm (в регионе), self._whole
        # (римские стадии — целый токен), собрать визуальные кандидаты для line-sweep.
        self._g: Dict[str, dict] = {}
        self._tnm: Dict[str, dict] = {}
        self._whole: Dict[str, dict] = {}
        self._loc: Dict[str, tuple] = {}   # core -> (pno,bbox): кроп для очереди (фикс B)
        self._resolve_roman(roman)
        self._meta: Dict[str, dict] = {}      # core -> {kind, crit, why}
        visual: List[str] = []
        for core in core_seen:
            tnm_ctx = core in core_tnm
            susp, why, kind = detect(core, tnm_ctx)
            if not susp:
                continue
            # ген-форма с хвостовой цифрой у ЗАЩИЩЁННОЙ рус. аббревиатуры (ГЦР7 = ГЦР+сноска)
            # — НЕ ген. Снимаем подозрение (иначе ложный unresolved-critical).
            if kind == "gene":
                stem = re.sub(r"\d+$", "", core)
                if stem and self._freq.get(stem, 0) >= 3:
                    continue
            crit = (kind in CRIT_KINDS) or (kind is None and _looks_critical_term(core))
            self._meta[core] = {"kind": kind, "crit": crit, "why": why}
            # unmapped-глиф без буквенного ядра -> удалить (ШАГ 4)
            if "unmapped_glyph" in why and not re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", core):
                self._g[core] = _mkrec(core, "", "drop_unmapped", "unmapped", 1.0,
                                       kind or "symbol", False, "auto", why)
                continue
            # канал 1: A2 буквенный гомограф (детерминизм). ICD/ATC — глобально безопасно;
            # TNM-результат (Т2->T2) — ТОЛЬКО в staging-регионе (иначе «Т2-лимфоциты» в
            # здоровом файле ложно менялся бы; шаблонно верно, но Group B требует не трогать).
            a2 = normalize_code_token(core)
            if a2 and a2[0] != core and not (a2[1] == "TNM" and not tnm_ctx):
                self._g[core] = _mkrec(core, a2[0], "a2_template", a2[1], 1.0,
                                       kind, crit, "auto", why)
                continue
            # канал 2: цифро-гомограф в TNM — ТОЛЬКО контекстно (МО->M0 лишь в staging)
            if kind == "tnm" and tnm_ctx and not _has_digit(core):
                ce = coerce_entity(core, "tnm")
                if ce and entity_valid(ce, "tnm") and ce != core:
                    self._tnm[core] = _mkrec(core, ce, "a2_template",
                                             "digit-slot(tnm)", 1.0, kind, crit, "auto", why)
                    continue
            # канал 2b: N-категория из Ы в TNM-регионе (Ых->Nx, Ы0->N0). Ы невозможна в
            # начале рус. слова -> в staging-контексте это латинская N (детерминизм, fsr=0).
            if tnm_ctx and core and core[0] in "Ыы" and len(core) <= 3:
                cand = coerce_entity(("N" if core[0] == "Ы" else "n") + core[1:], "tnm")
                if cand and entity_valid(cand, "tnm"):
                    self._tnm[core] = _mkrec(core, cand, "a2_template", "N-from-Ы(tnm)",
                                             1.0, "tnm", True, "auto", why)
                    continue
            # остальное -> визуальные каналы (font-repair / eng-OCR по кропу)
            visual.append(core)
        # LINE-SWEEP: сгруппировать визуальные кандидаты по native-строке, один eng-OCR
        # на строку, выровнять и починить (+ соседей строки — контекст ловит хап->van).
        self._line_sweep(visual, core_tnm)
        # КАНАЛ C5: генератор = битый шрифт (не аномалия), решение = ЯЗЫКОВОЙ арбитр.
        # Здоровый документ -> ноль кандидатов, ноль OCR (Group B по построению).
        # Арбитра нет -> канал молчит целиком (fail-closed, I5), см. c5_active().
        if c5_active():
            all_zone_tokens = set()
            for z in zones:
                all_zone_tokens.update(_iter_segments(z["text"]))
            self._font_sweep(all_zone_tokens)
        # применить карты по зонам
        for z in zones:
            new = self._apply(z["text"], z["page"], z["uids"], z["tnm"], z["where"])
            z["set"](new)

    def _gather_zones(self, sections, tables, excluded, metadata) -> List[dict]:
        """Плоский список редактируемых зон: sections(title/text), tables(caption/
        raw_text), excluded.appendices(title/text), metadata.title. references/toc/
        front_matter/other — НЕ трогаем (решение команды: ссылки не резать/не менять)."""
        zones: List[dict] = []

        def add(text, page, uids, tnm, where, setter):
            if text and text.strip():
                zones.append({"text": text, "page": page, "uids": uids,
                              "tnm": tnm, "where": where, "set": setter})
        for sec in _walk(sections):
            page = getattr(sec, "page", 0) or 0
            tnm = is_tnm_region(getattr(sec, "text", "") or "")
            uids = list(getattr(sec, "span_uids", []) or [])
            sid = "section:%s" % getattr(sec, "section_id", "")
            add(getattr(sec, "title", "") or "", page, uids, tnm, sid,
                lambda v, s=sec: setattr(s, "title", v))
            add(getattr(sec, "text", "") or "", page, uids, tnm, sid,
                lambda v, s=sec: setattr(s, "text", v))
        for t in tables:
            page = getattr(t, "page", 0) or 0
            blob = (getattr(t, "raw_text", "") or "") + " " + (getattr(t, "caption", "") or "")
            tnm = is_tnm_region(blob)
            uids = list(getattr(t, "claimed_span_uids", []) or [])
            where = "table:%s" % getattr(t, "number", "")
            add(getattr(t, "raw_text", "") or "", page, uids, tnm, where,
                lambda v, tb=t: setattr(tb, "raw_text", v))
            if getattr(t, "caption", None):
                add(t.caption, page, uids, tnm, where,
                    lambda v, tb=t: setattr(tb, "caption", v))
        for item in (excluded.get("appendices") or []):
            uids = list(item.get("span_uids", []) or [])
            add(item.get("text", "") or "", None, uids, False, "appendix",
                lambda v, it=item: it.__setitem__("text", v))
            if item.get("title"):
                add(item["title"], None, uids, False, "appendix",
                    lambda v, it=item: it.__setitem__("title", v))
        if metadata.get("title"):
            add(metadata["title"], 1, [], False, "metadata:title",
                lambda v: metadata.__setitem__("title", v))
        return zones

    def _protected(self, core: str) -> bool:
        """Защищённое рус. слово: НЕ латинизировать (Group B). Частотность >=3 (реальное
        повторяющееся слово/аббревиатура: ГЦР/РФ/СНВС), стоп-слово, или слишком короткое."""
        if self._freq.get(core, 0) >= 3:
            return True
        if core.lower() in _RU_STOP:
            return True
        return False

    def _c5_protected(self, core: str) -> Optional[str]:
        """ГЕЙТ 2 канала C5 — «это русский, не трогать». Возвращает ПРИЧИНУ защиты
        (замена ЗАПРЕЩЕНА) или None (кандидат идёт дальше, к гейту 3).

        Четыре правила, от структурного к языковому. Каждое закрывает класс, который
        ОСТАЛЬНЫЕ закрыть не могут — снятие любого = латинизация русского:

        1. `all_caps` — ALL-CAPS кириллица = рус. аббревиатура (ГЦР/СНВС/СОД/ВГД).
           ПО ПОСТРОЕНИЮ, а не по частоте: eng-OCR читает такой токен РЕАЛЬНЫМ лат.
           словом (`СОД`->`COD`, `ВГД`->`BID`), и LAT-словарь (гейт 3) это ПРОПУСТИТ ->
           для all-caps гейт 3 не работает вовсе. Ценой правила ОСОЗНАННО уходит
           `ШСС`->UICC (единственный all-caps класса C5): он лосси (4 глифа -> 3 симв.,
           контур бессилен) и от рус. аббревиатуры НЕОТЛИЧИМ. I20 уже зафиксировал
           этот исход как валидный: его место — в очереди на человека, не в автозамене.
        2. `freq` / `stop` — повторяющееся слово документа (I20).
        3. `morphology` — pymorphy `word_is_known`: валидная рус. словоформа
           (`боль`, `пациентов`, `Уровень`). ИМЕННО ЭТО делает `боль`->`bone`
           НЕВОЗМОЖНЫМ — ни один другой гейт его не держит (`bone` есть в LAT-словаре).
        4. `long_lowercase` — 7+ подряд СТРОЧНЫХ кириллических. Закрывает то, чего НЕ
           МОЖЕТ гейт 3: МЕДИЦИНСКИЙ русский. OpenCorpora — словарь общего языка, и
           `гепатоцеллюлярном`/`стеатогепатита`/`холангиокарцинома` ему НЕИЗВЕСТНЫ
           (замерено, rumorph.py), а редкая словоформа не защищена и частотой. Все они
           — 7+ строчных кириллических -> правило делает ВЕСЬ класс неприкосновенным
           ПО ПОСТРОЕНИЮ (тот же приём, что fsr=0 у A2-шаблона, I19).
           ЦЕНА ИЗМЕРЕНА: правило ошибается на порче вида `аззеззшеп`(=assessment) —
           реальном лат. слове БЕЗ заглавной/цифры/чужого глифа. Такой токен НЕ
           латинизируется молча: он уходит В ОЧЕРЕДЬ НА ЧЕЛОВЕКА с кропом (см.
           `_font_sweep`), т.е. класс остаётся ВИДИМЫМ, но не автоправится.

        ВНИМАНИЕ (циркулярность). Правило 4 совпадает с критерием теста-стража
        `test_latin_c5_morphology.py` -> тот тест проходит ПО ПОСТРОЕНИЮ и empirical
        доказательством НЕ является. Эмпирическую нагрузку несёт ВТОРОЙ тест — по
        КОРПУСНОМУ словарю русского (слово, чисто встречающееся в >=3 документах
        baseline): он не совпадает ни с одним гейтом (гейты смотрят частоту ВНУТРИ
        документа, pymorphy и форму токена) и потому МОЖЕТ упасть. Не удалять его.
        """
        letters = [c for c in core if c.isalpha()]
        if letters and not any(c.islower() for c in letters):
            return "all_caps"
        if self._freq.get(core, 0) >= 3:
            return "freq"
        if core.lower() in _RU_STOP:
            return "stop"
        if rumorph.word_is_known(core):
            return "morphology"
        if _RX_LONG_LOWER_CYR.match(core):
            return "long_lowercase"
        return None

    # Причины блокировки гейтом 2, при которых токен ВСЁ РАВНО отдаём человеку в очередь.
    # Замер (КР1_4, самый битый док): гейт 2 блокирует 22 уник. токена, из них по этим трём
    # причинам — 10. Внутри: `ШСС`->UICC, `1МКТ`->IMRT, `рока`->portal, `аззеззшеп`->
    # assessment (РЕАЛЬНАЯ порча) и `ней`/`базе`/`связи`/`того`/`АЛТ` (шум). ~44% precision
    # на задачу человека — приемлемо: «нет» по кропу стоит секунды, потерянный дефект —
    # навсегда. Классы `freq`/`stop` НЕ отдаём: повторяемость в документе (>=3) — сильная
    # улика реального рус. слова (`пациентов`/`может`/`чтобы`), человеку там смотреть нечего,
    # а очередь они бы утопили.
    _C5_DEFER_REASONS = frozenset({"morphology", "all_caps", "long_lowercase"})

    def _c5_defer(self, core: str, oc: str, why: str, pno: int, bbox) -> None:
        """Кандидат прошёл гейт 1 (битый шрифт) и гейт 3 (eng-OCR дал НАСТОЯЩЕЕ лат. слово),
        но гейт 2 запретил автозамену. АВТОПРАВКИ НЕТ НИКОГДА — но и МОЛЧА НЕ ТЕРЯЕМ:
        задача человеку с кропом (AUTONOMY: «не уверен -> needs_review + кроп, идёшь дальше»).

        Почему именно эти причины (`_C5_DEFER_REASONS`): каждая из них запрещает замену на
        основании ОБЩЕГО знания, которое НЕ знает латинской терминологии этого корпуса, —
        словарь общего языка (`рока` = родительный от «рок» И реально `portal`), правило
        формы (`аззеззшеп` = 7+ строчных И реально `assessment`), all-caps (`ШСС` = похож на
        рус. аббревиатуру И реально `UICC`). Это НЕОУСТРАНИМАЯ неоднозначность: её решает
        человек по картинке, а не порог. `freq`/`stop` — другое дело: там улика внутри
        документа сильная, задача была бы шумом.
        """
        if why not in self._C5_DEFER_REASONS or core in self._c5_queued:
            return
        self._c5_queued.add(core)          # один токен документа = одна задача
        self._enqueue(
            core, oc, "term", None, pno, bbox, ["c5_ambiguous", "c5_gate2:" + why],
            [oc], "ocr_eng", 0.0,
            "гейт 2 (%s) запретил автозамену, но eng-OCR по кропу читает «%s» — "
            "решает человек по картинке" % (why, oc))

    def _line_sweep(self, cores: List[str], core_tnm: set) -> None:
        """Один eng-OCR на строку; выровнять native<->eng; починить визуальные
        кандидаты И их соседей по строке (контекст восстанавливает хап->van)."""
        lines: Dict[tuple, dict] = {}
        for core in cores:
            pno, bbox, ltext = self._locate(core, self._page_hint.get(core))
            if pno is None:
                # не нашли в native -> оставить нерешённым (кроп невозможен)
                self._mark_unresolved(core, None, None, None)
                continue
            # ЛОКАЦИЯ ЗАПОМИНАЕТСЯ: нужна, чтобы у НЕРАЗРЕШЁННОГО спана в очереди был КРОП
            # (без картинки задача бесполезна человеку).
            self._loc[core] = (pno, bbox)
            key = (pno, tuple(round(x) for x in bbox))
            e = lines.setdefault(key, {"pno": pno, "bbox": bbox, "native": ltext,
                                       "targets": set()})
            e["targets"].add(core)
        for e in lines.values():
            self._sweep_line(e, core_tnm)
        # кандидаты, не решённые line-sweep -> очередь (+ карантин, если критический код)
        for core in cores:
            if core not in self._g and core not in self._tnm:
                pno, bbox = self._loc.get(core, (None, None))
                self._mark_unresolved(core, self._page_hint.get(core), bbox, pno)

    def _corrupt_font_names(self) -> set:
        """ГЕЙТ 1: имена шрифтов документа, чья контурная карта РАСХОДИТСЯ с /ToUnicode
        (agreement < 0.85 при sample >= 12). Пустое множество -> документ ЗДОРОВ, канал C5
        не делает НИ ОДНОГО вызова OCR (68% корпуса; Group B по построению)."""
        if self._corrupt_fonts is not None:
            return self._corrupt_fonts
        names: set = set()
        try:
            doc = self._ensure_doc()
            fr = self._ensure_fr()
            for pno in range(len(doc)):
                for f in doc[pno].get_fonts(full=True):
                    r = fr.font_report(f[0])
                    if r and r["corrupt"]:
                        names.add(f[3].split("+")[-1])
        except Exception:  # noqa: BLE001
            names = set()
        self._corrupt_fonts = names
        return names

    def _font_sweep(self, zone_tokens: set) -> None:
        """КАНАЛ C5 (см. «КАНАЛ C5 — АРХИТЕКТУРА» вверху модуля).
        ГЕЙТ 1 (recall): строки в БИТОМ шрифте -> кандидаты (аномалия НЕ нужна).
        ГЕЙТ 2 (precision, ДЕРЖИТ GROUP B): OCR != визуальная транслитерация native."""
        bad = self._corrupt_font_names()
        if not bad or not self._ensure_ocr():
            return                       # здоровый шрифт -> ноль кандидатов, ноль OCR
        vocab = latin_vocab()
        doc = self._ensure_doc()
        for pno in range(len(doc)):
            page = doc[pno]
            try:
                blocks = page.get_text("dict").get("blocks", [])
            except Exception:  # noqa: BLE001
                continue
            for blk in blocks:
                for ln in blk.get("lines", []):
                    spans = ln.get("spans", [])
                    fonts = {sp.get("font", "").split("+")[-1] for sp in spans}
                    if not (fonts & bad):
                        continue          # ГЕЙТ 1: честный шрифт -> не трогаем
                    native = "".join(sp.get("text", "") for sp in spans)
                    if not native.strip() or not _CYR_ANY.search(native):
                        continue
                    nat = [c for c in (_strip_edges(w)[1] for w in native.split()) if c]
                    if not nat:
                        continue
                    # только строки обучаемой зоны (references/toc не трогаем)
                    hit = sum(1 for c in nat if c in zone_tokens or c in self._g)
                    if hit / len(nat) < 0.5:
                        continue
                    eng = self._eng_line(pno, tuple(ln["bbox"]))
                    if not eng:
                        continue
                    ew = [c for c in (_strip_edges(w)[1] for w in eng.split()) if c]
                    al = _align_line(nat, ew)
                    for i, core in enumerate(nat):
                        if (core in self._g or core in self._tnm or len(core) < 2
                                or not _CYR_ANY.search(core) or core not in zone_tokens):
                            continue
                        oc = al.get(i)
                        if not oc or _CYR_ANY.search(oc) or not _plausible_latin(oc):
                            continue
                        # подстраховка: eng-вывод ≈ визуальное чтение кириллицы -> артефакт
                        tr_sim = _similar(oc.lower(), _translit(core))
                        if tr_sim >= C5_TRANSLIT_SIM:
                            continue
                        if tr_sim < C5_ALIGN_MIN:
                            continue      # выравнивание сбилось -> не улика
                        # ГЕЙТ 3: результат обязан быть НАСТОЯЩИМ латинским словом.
                        if oc.lower() not in vocab and oc not in (self._doc_latin_vocab or set()):
                            continue      # OCR-мусор
                        # ГЕЙТ 2 — ЯЗЫК. ЗДЕСЬ ДЕРЖИТСЯ GROUP B (архитектура вверху модуля).
                        # Проверяем ПОСЛЕ гейта 3 сознательно: знание «OCR дал настоящее
                        # лат. слово» нужно, чтобы отличить рус. слово (задавить молча) от
                        # `аззеззшеп`-класса (отдать человеку). На стоимость не влияет —
                        # OCR вызывается на СТРОКУ, а не на токен.
                        why_protected = self._c5_protected(core)
                        if why_protected:
                            self._c5_defer(core, oc, why_protected, pno, tuple(ln["bbox"]))
                            continue
                        e3 = oc in (self._doc_latin_vocab or set())
                        self._g[core] = _mkrec(
                            core, oc, "ocr_eng",
                            "C5:corrupt-font+eng-crop(tr=%.2f)%s" % (tr_sim, "+E3" if e3 else ""),
                            round(min(0.9, 0.6 + 0.2 * (1 - tr_sim) + (0.1 if e3 else 0)), 2),
                            "term", False, "needs_review", ["c5_corrupt_font"],
                            bbox=tuple(ln["bbox"]), pno=pno, candidates=[oc])

    def _resolve_roman(self, roman: set) -> None:
        """Римские стадии (Н-Ш->II-III, П-1У->II-IV): eng-OCR по кропу строки, принять
        ТОЛЬКО валидную римскую форму `[IVX]+-[IVX]+` (иначе не трогаем). needs_review."""
        for core in roman:
            pno, bbox, _ = self._locate(core, self._page_hint.get(core))
            if pno is None:
                continue
            eng = self._eng_line(pno, bbox)
            for t in re.findall(r"\S+", eng):
                cand = t.strip("().,;:[]").replace("—", "–")
                norm = cand.replace("–", "-")
                if _RX_ROMAN_VALID.match(norm) and norm.replace("-", "") != core.replace("-", ""):
                    self._whole[core] = _mkrec(core, cand, "ocr_eng",
                                               "roman-stage+eng-crop", 0.85, "stage",
                                               False, "needs_review", ["roman_stage"],
                                               bbox=bbox, pno=pno, candidates=[cand])
                    break

    def _sweep_line(self, e: dict, core_tnm: set) -> None:
        """Починить ТОЛЬКО задетектированные сегменты-цели этой строки (соседей НЕ
        трогаем — Group B). Сегментное выравнивание native<->eng по позиции."""
        pno, bbox = e["pno"], e["bbox"]
        native_segs = [s for s in _iter_segments(e["native"])]
        # 1) font-repair не-гомографов (контур; не нужен eng-OCR)
        for core in list(e["targets"]):
            if core in self._g or not any(c in _CYR_NONHOMO for c in core):
                continue
            m = self._meta.get(core, {})
            fr = self._fontrepair(pno, core, m.get("kind"))
            if fr and not _CYR_ANY.search(fr[0]):
                fixed, method, rule, conf = fr
                self._g[core] = _mkrec(core, fixed, method, rule, conf, m.get("kind"),
                                       m.get("crit"), "auto", m.get("why", []),
                                       bbox=bbox, pno=pno, candidates=[fixed])
        # 2) eng-OCR строки + позиционное выравнивание СЕГМЕНТОВ
        eng = self._eng_line(pno, bbox)
        eng_words = [w.strip(".,;:()[]«»") for w in re.findall(r"\S+", eng)]
        eng_words = [w for w in eng_words if w]
        align = _align_line(native_segs, eng_words)
        seg_idx: Dict[str, int] = {}
        for idx, s in enumerate(native_segs):
            seg_idx.setdefault(s, idx)
        for core in list(e["targets"]):
            if core in self._g:
                continue
            m = self._meta.get(core, {})
            kind = m.get("kind")
            crit = bool(m.get("crit"))
            why = m.get("why", [])
            # -- код (icd/atc/tnm + ведущий глиф): гейт формы + цифро-сигнатура --
            code_kind = kind if kind in ("icd", "atc", "tnm") else _leading_glyph_code(core)
            if code_kind:
                cand = self._code_from_eng(core, code_kind, eng_words)
                if cand and cand != core:
                    self._g[core] = _mkrec(core, cand, "ocr_eng",
                                           "entity-gate:%s+eng-crop" % code_kind, 0.9,
                                           kind or code_kind, True, "auto", why,
                                           bbox=bbox, pno=pno, candidates=[cand])
                continue
            # -- термин: выровненное eng-слово (позиция сегмента) --
            if not _CYR_ANY.search(core) or self._protected(core):
                continue
            ew = align.get(seg_idx.get(core, -1))
            if not ew or _CYR_ANY.search(ew) or not _plausible_latin(ew) or ew == core:
                continue
            skel = _lat_skeleton(core)
            sim = _similar(skel, ew.lower())
            e3 = ew in (self._doc_latin_vocab or set())
            # ген/белок (СТЪА4/РИ1): применяем ТОЛЬКО с E3-подтверждением (чистое вхождение
            # CTLA4/PD1 в документе) — иначе рус.«ВГВ2000» латинизировался бы по OCR-мусору.
            if kind == "gene" and not e3:
                continue
            # ЦЕЛЬ подтверждена детектором как порча -> принимаем выровненную латиницу.
            # МОЛЧАЛИВАЯ (auto) замена — ТОЛЬКО при СИЛЬНОЙ улике: скелет native совпал с
            # eng-словом (sim >= C5_AUTO_SIM). Всё остальное -> человеку по кропу.
            #
            # ПОЧЕМУ E3 БОЛЬШЕ НЕ ДАЁТ ПРАВА НА auto (замер по корпусу, 4801 term-замена).
            # Было `not (sim >= 0.8 or e3)`, т.е. E3 САМ ПО СЕБЕ разрешал молчаливую замену
            # ПРИ ЛЮБОЙ похожести. Но E3 = «слово встречается где-то В ЭТОМ ЖЕ документе», а
            # `the`/`and`/`for`/`Vol` есть в любом документе с англоязычными ссылками ->
            # ЛЮБОЙ сбой выравнивания давал МОЛЧАЛИВУЮ порчу термина. Замерено: auto=467, из
            # них 383 разрешены ИМЕННО E3, и 307 из них при sim<0.5. Глазами: `Ыуег`->`the`,
            # `НаетоггЬаде`->`Guideline`, `аи1о1ттипе`->`overlap`, `аз50с1а1ес1`->`from`,
            # `с!еуе1ортеп1`->`Organization` — термин заменён СОСЕДНИМ словом строки.
            # ЧИСТЫЙ LAT-СЛОВАРЬ ЭТОГО НЕ ЛОВИТ: `the` — настоящее латинское слово, гейт 3
            # его пропускает по построению. Лечится только требованием улики НА САМОМ ТОКЕНЕ.
            # Цена: часть ВЕРНЫХ низко-sim починок (`Ьийтгщ`->Ludwig sim=0.00, `ЫеесЬп`->
            # bleeding sim=0.25) уходит из auto в очередь. Это правильный размен: человек
            # подтвердит по кропу за секунды, а молча испорченный термин — навсегда.
            uncertain = sim < C5_AUTO_SIM
            rule = "eng-crop+align(sim=%.2f)%s" % (sim, "+E3" if e3 else "")
            conf = round(min(0.97, 0.55 + 0.35 * sim + (0.1 if e3 else 0)), 2)
            self._g[core] = _mkrec(core, ew, "ocr_eng", rule, conf, kind or "term",
                                   crit, "needs_review" if uncertain else "auto", why,
                                   bbox=bbox, pno=pno, candidates=[ew])
        # 3) УЗКОЕ восстановление СОСЕДЕЙ: только если на строке уже разрешено >=2 цели
        # (сильная улика, что строка — порченая латиница: авторы/классификации). Ловит
        # хап->van рядом с 8рае1И/8Иа//ег. На здоровом файле не срабатывает (там нет целей).
        resolved_on_line = sum(1 for c in e["targets"] if c in self._g)
        if resolved_on_line >= 2:
            for i, core in enumerate(native_segs):
                if (not core or core in self._g or core in self._tnm
                        or not _CYR_ANY.search(core) or self._protected(core)
                        or len(core) < 2 or core in e["targets"]):
                    continue
                ew = align.get(i)
                if not ew or _CYR_ANY.search(ew) or not _plausible_latin(ew) or ew == core:
                    continue
                sim = _similar(_lat_skeleton(core), ew.lower())
                e3 = ew in (self._doc_latin_vocab or set())
                self._g[core] = _mkrec(
                    core, ew, "ocr_eng", "eng-crop+align(sim=%.2f)+neighbor%s" % (
                        sim, "+E3" if e3 else ""),
                    round(min(0.9, 0.5 + 0.3 * sim), 2), "term", False,
                    "needs_review", ["line_context"], bbox=bbox, pno=pno, candidates=[ew])

    def _code_from_eng(self, core: str, kind: str, eng_words: List[str]) -> Optional[str]:
        """Выбрать из eng-OCR токен, дающий валидную форму сущности, чья цифро-сигнатура
        совпадает с corrupt (допускаем расхождение ВЕДУЩЕГО символа: 501ЕС->S01EC,
        037.6->D37.6, где ведущая цифра — это буква). Не прошёл форму -> None."""
        sig = _digit_sig(core)
        dcore = _digits(core)
        for t in eng_words:
            c = coerce_entity(t, kind)
            cand = c if (c and entity_valid(c, kind)) else (
                t if entity_valid(t, kind) else None)
            if not cand:
                continue
            cds = _digits(cand)
            if cds in (sig, sig[1:], dcore, dcore[1:]) or (not sig and not cds):
                return cand
        return None

    def _mark_unresolved(self, core: str, page, bbox, pno=None) -> None:
        """Нерешённый ЗАДЕТЕКТИРОВАННЫЙ спан.

        ФИКС B (замер recall, `_corpus/detector_recall.md`): в очередь идёт **ЛЮБОЙ**
        неразрешённый спан, а не только критический. Раньше некритический термин, который
        детектор УВИДЕЛ, но резолюция не осилила (`8уз1етайс`->Systematic, `1МКТ`->IMRT),
        исчезал БЕЗ СЛЕДА: ни правки, ни записи, ни задачи человеку — 12% всех пропусков.
        Детектор обязан отдавать человеку всё, в чём усомнился.

        РАЗДЕЛЕНИЕ: очередь (человек посмотрит) != карантин (документ не едет в обучение).
        Блокирует релиз ТОЛЬКО СИЛЬНАЯ улика — кир. гомограф в ВАЛИДНОМ шаблоне ICD/ATC/TNM
        (`code_alphabet_violation`). Слабые улики (ген-форма, ведущий глиф: 001/Уо1.22) и
        термины — в очередь, но БЕЗ блокировки: иначе цитаты и числа топили бы документы."""
        m = self._meta.get(core, {})
        if not hasattr(self, "_unresolved_seen"):
            self._unresolved_seen = set()
        if core in self._unresolved_seen:
            return
        self._unresolved_seen.add(core)
        why = m.get("why", [])
        kind = m.get("kind")
        strong_code = "code_alphabet_violation" in why and kind in ("icd", "atc", "tnm")
        if m.get("crit") and strong_code:
            self.unresolved_critical.append(
                {"source_text": core, "kind": kind, "page": page})
            reason = "критический код не разрешён (карантин)"
        elif kind in ("gene", "icd", "atc", "tnm"):
            reason = "код/ген-кандидат не разрешён (проверить)"
        else:
            reason = "подозрительный спан не разрешён (проверить по кропу)"
        # ЛЮБОЙ неразрешённый -> в очередь, с кропом если спан локализован (фикс B)
        self._enqueue(core, core, kind, page, pno, bbox, why, [], "none", 0.0, reason)

    def _apply(self, text: str, page, span_uids, tnm_ok: bool, where: str) -> str:
        """Применить карты замен к тексту зоны НА УРОВНЕ СЕГМЕНТА (сплит по дефису):
        чиним только порченый сегмент, чистый рус. хвост не трогаем (Group B)."""
        if not text or not text.strip():
            return text
        # пред-проход: римские стадии — ЦЕЛЫМ токеном (до дефис-сегментации)
        if self._whole:
            ws = re.split(r"(\s+)", text)
            for i in range(0, len(ws), 2):
                if not ws[i]:
                    continue
                lead, core, trail = _strip_edges(ws[i])
                rec = self._whole.get(core)
                if rec and rec["resolved_text"] != core:
                    self._emit(rec, page, span_uids)
                    ws[i] = lead + rec["resolved_text"] + trail
            text = "".join(ws)
        parts = _SEG_RE.split(text)      # [word, sep, word, sep, ...]
        for i in range(0, len(parts), 2):
            word = parts[i]
            if not word:
                continue
            lead, core, trail = _strip_edges(word)
            if not core:
                continue
            rec = self._g.get(core)
            if rec is None and tnm_ok:
                rec = self._tnm.get(core)
            if rec is None or rec["resolved_text"] == core:
                continue
            self._emit(rec, page, span_uids)
            parts[i] = lead + rec["resolved_text"] + trail
        return "".join(parts)

    def _emit(self, rec: dict, page, span_uids) -> None:
        """Записать провенанс этого применения (+ очередь при needs_review). Внутренние
        поля (_pno/_bbox_raw) не сериализуются."""
        r = {"span_uid": span_uids[0] if span_uids else None,
             "page": page if page is not None else rec.get("page")}
        r.update({k: v for k, v in rec.items() if not k.startswith("_")})
        self.prov.append(r)
        if rec.get("decision") == "needs_review":
            self._enqueue(rec["source_text"], rec["resolved_text"], rec.get("entity_kind"),
                          page, rec.get("_pno"), rec.get("_bbox_raw"),
                          rec.get("why_suspect", []), rec.get("candidates", []),
                          rec.get("method"), rec.get("confidence", 0.0),
                          "eng-OCR, нужна визуальная проверка")

    # ---- провенанс + очередь ----
    def _enqueue(self, src, best, kind, page, pno, bbox, why, candidates,
                 method, conf, reason):
        """Положить неуверенное в очередь верификации (+ кроп PNG при возможности)."""
        task = {
            "doc": self._doc_id,
            "span_uid": None,
            "page": (pno + 1) if pno is not None else page,
            "bbox": [round(float(x), 1) for x in bbox] if bbox else None,
            "source_text": src,
            "resolved_text": best,
            "candidates": list(candidates),
            "method": method,
            "confidence": round(float(conf), 3),
            "entity_kind": kind,
            "is_critical": bool((kind in CRIT_KINDS)),
            "why_uncertain": reason,
            "why_suspect": list(why),
            "crop_png": None,
        }
        if self._queue_dir and pno is not None and bbox is not None:
            crop = self._save_crop(pno, bbox, src)
            task["crop_png"] = crop
        self.queue.append(task)

    def _save_crop(self, pno: int, bbox, label: str) -> Optional[str]:
        try:
            import fitz  # noqa
            crops = os.path.join(self._queue_dir, "crops")
            os.makedirs(crops, exist_ok=True)
            safe = re.sub(r"[^0-9A-Za-z]", "_", "%s_p%d_%s" % (self._doc_id, pno + 1, label))[:80]
            path = os.path.join(crops, safe + ".png")
            if not os.path.exists(path):
                page = self._ensure_doc()[pno]
                pad = 4.0
                clip = fitz.Rect(bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
                pix = page.get_pixmap(matrix=fitz.Matrix(400 / 72.0, 400 / 72.0), clip=clip)
                pix.save(path)
            return os.path.relpath(path, self._queue_dir)
        except Exception:  # noqa: BLE001
            return None


# ---- вспомогательное ----
# Частые рус. слова/аббревиатуры: НИКОГДА не латинизируем (даже если eng-OCR соблазняет).
_RU_STOP = frozenset(
    "и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по "
    "или из от до для при без под над о об про это эта эти тот та те при чем чём над "
    "мг мл сут ч мкг ммоль мм см кг мес нед год лет раз".split())


def _mkrec(src, dst, method, rule, conf, kind, crit, decision, why,
           bbox=None, pno=None, candidates=None) -> dict:
    """Собрать запись-замену (общую для всех применений). Сериализуемые поля +
    внутренние (_pno/_bbox_raw) для генерации кропа очереди."""
    rec = {
        "source_text": src, "resolved_text": dst, "method": method, "rule": rule,
        "confidence": round(float(conf), 3), "entity_kind": kind,
        "is_critical": bool(crit), "decision": decision, "why_suspect": list(why),
    }
    if candidates is not None:
        rec["candidates"] = list(candidates)
    if decision == "needs_review":
        rec["needs_review"] = True
    if bbox is not None:
        rec["bbox"] = [round(float(x), 1) for x in bbox]
        rec["_bbox_raw"] = tuple(bbox)
    if pno is not None:
        rec["_pno"] = pno
    return rec


_VOWEL = re.compile(r"[aeiouyAEIOUY]")


def _plausible_latin(w: str) -> bool:
    """eng-OCR-слово похоже на настоящее латинское слово (>=2 букв, есть гласная) —
    отсекает OCR-мусор вроде 'HZ'/'lll' при выравнивании соседей."""
    letters = re.sub(r"[^A-Za-z]", "", w)
    return len(letters) >= 2 and bool(_VOWEL.search(w))


# Выравнивание native<->eng: цена пропуска токена. Оценка пары = 2*sim-1 (-1..+1).
#
# ЛОКАЛЬНОГО ПОРОГА ПОХОЖЕСТИ ЗДЕСЬ НЕТ — И ЭТО ИЗМЕРЕНО, А НЕ ЗАБЫТО. У класса A1 (битый
# /ToUnicode) маппинг ПРОИЗВОЛЕН (`й`->l, `у`->i, `е`->v, `г`->e), скелет теряет
# не-гомографы (`_lat_skeleton('йуег') == _lat_skeleton('Ыуег') == 'ye'`), и похожесть
# ВРАЖДЕБНА истине:
#     ВЕРНАЯ пара  `йуег` ~ `liver` = 0.20
#     ЛОЖНАЯ пара  `Ыуег` ~ `the`   = 0.33   <- выше!
# Любой порог, отсекающий ложную пару, отсекает и верную. Различает ТОЛЬКО глобальная
# структура строки: `The` уже занят токеном `ТЪе`, поэтому `Ыуег` физически не может его
# получить, а `liver` остаётся ровно там, где стоит `йуег`. Отсюда: пары ставит NW по
# контексту, а качество пары решают гейты НИЖЕ (транслит-гейт, LAT-словарь, pymorphy,
# sim>=0.8 для auto). Добавить сюда порог = вернуть `Ыуег`->`the` и потерять `йуег`->liver.
_ALIGN_GAP = -0.5


def _align_line(native: List[str], eng: List[str]) -> Dict[int, str]:
    """ГЛОБАЛЬНОЕ выравнивание native-токенов строки на eng-OCR-токены (Needleman-Wunsch)
    по похожести латинского скелета. Токен без пары -> ключа НЕТ (не «ближайший мусор»).

    ПОЧЕМУ НЕ ЖАДНОЕ ОКНО (замерено, это был КОРЕНЬ двух дефектов сразу):
      * прежняя версия при n==m равняла ПОЗИЦИОННО, вообще не глядя на похожесть: одна
        лишняя/склеенная OCR-строка сдвигала ВСЮ строку;
      * иначе шла жадно вперёд окном 3, БЕЗ возврата, и `out[i] = best` ставила пару даже
        при sim=0.0. На длинной библиографической строке КР1_4 стр.69 eng-OCR читает текст
        ИДЕАЛЬНО («...Systematic review: The model for end-stage liver disease...»), но
        выравнивание уводило `nat[10]=йуег` -> `it` (sim=0.00).
      Отсюда РОВНО ОДИН механизм порождал обе беды: `йуег`->liver терялся (пропуск
      детектора), а `Ыуег`->`the`/`НаетоггЬаде`->`Guideline` ПРИМЕНЯЛИСЬ (E3 разрешал auto
      на ложной паре). NW чинит обе стороны: правильные пары находятся, ложные становятся
      разрывами. Поэтому это не «+N% recall», а устранение источника мусорных кандидатов.

    Сложность O(n*m) на строку (токенов в строке единицы-десятки) — стоимость на фоне
    OCR-вызова пренебрежима.
    """
    out: Dict[int, str] = {}
    n, m = len(native), len(eng)
    if n == 0 or m == 0:
        return out
    engl = [t.lower() for t in eng]
    # МАТРИЦА ПОХОЖЕСТИ = max(скелет, транслит). ДВЕ ГИПОТЕЗЫ, а не одна:
    #   * токен РЕАЛЬНО латинский (битый /ToUnicode) -> eng-OCR даст истинное лат. слово,
    #     грубая модель этого — латинский СКЕЛЕТ (гомографы + цифро-визуалы);
    #   * токен РЕАЛЬНО русский -> eng-OCR ЧИТАЕТ КИРИЛЛИЦУ ЛАТИНИЦЕЙ, и модель этого —
    #     ТРАНСЛИТЕРАЦИЯ (`или`->`uau`, `целесообразно`->`yenecooOpa3vo`).
    # Для ВЫРАВНИВАНИЯ (в отличие от РЕШЕНИЯ) не важно, какая гипотеза верна: нужен ответ
    # «могут ли эти двое быть одним токеном» -> берём максимум.
    #
    # ПОЧЕМУ ОДНОГО СКЕЛЕТА МАЛО (замерено, КР628_2 стр.17 — регресс `Shaffer`->`uau`):
    # скелет РОНЯЕТ не-гомографы, поэтому у РУССКИХ слов он пуст или почти пуст —
    # `_lat_skeleton('или') == ''`. Пустой скелет не совпадает НИ С ЧЕМ -> русские токены
    # перестают быть ЯКОРЯМИ, и NW предпочитает разрывы: на строке из 10 native и 10 eng
    # (идеальное 1:1) хвост уезжал на два токена, `8Иа//ег` получал `uau` вместо `Shaffer`
    # (проигрыш верного пути был 0.08 — вот цена пустых якорей). С транслитом
    # `или`~`uau`=0.67 и `целесообразно`~`yenecooOpa3vo` — якоря держат строку.
    skel = [_lat_skeleton(t) for t in native]      # считаем ОДИН раз на токен, не n*m раз
    tr = [_translit(t) for t in native]
    S = [[max(_similar(skel[i], engl[j]), _similar(tr[i], engl[j])) for j in range(m)]
         for i in range(n)]
    # F — накопленный счёт, P — указатель трассировки: 0=пара, 1=пропуск native, 2=пропуск eng
    F = [[0.0] * (m + 1) for _ in range(n + 1)]
    P = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        F[i][0] = F[i - 1][0] + _ALIGN_GAP
        P[i][0] = 1
    for j in range(1, m + 1):
        F[0][j] = F[0][j - 1] + _ALIGN_GAP
        P[0][j] = 2
    for i in range(1, n + 1):
        Fi, Fp, Si = F[i], F[i - 1], S[i - 1]
        Pi = P[i]
        for j in range(1, m + 1):
            diag = Fp[j - 1] + (2.0 * Si[j - 1] - 1.0)
            up = Fp[j] + _ALIGN_GAP          # native[i-1] без пары
            left = Fi[j - 1] + _ALIGN_GAP    # eng[j-1] без пары
            if diag >= up and diag >= left:
                Fi[j], Pi[j] = diag, 0
            elif up >= left:
                Fi[j], Pi[j] = up, 1
            else:
                Fi[j], Pi[j] = left, 2
    i, j = n, m
    while i > 0 or j > 0:
        p = P[i][j]
        if p == 0:
            out[i - 1] = eng[j - 1]
            i -= 1
            j -= 1
        elif p == 1:
            i -= 1
        else:
            j -= 1
    return out


def _iter_segments(text: str):
    """Итерировать ЯДРА слов-сегментов текста (сплит по дефису/пробелу, пунктуация
    краёв снята). Совпадает с сегментами, которые правит `_apply`."""
    parts = _SEG_RE.split(text)
    for i in range(0, len(parts), 2):
        word = parts[i]
        if not word:
            continue
        _, core, _ = _strip_edges(word)
        if core:
            yield core


def _walk(sections):
    for s in sections:
        yield s
        yield from _walk(getattr(s, "children", []) or [])


def _lat_skeleton(core: str) -> str:
    """Латинский скелет кир-порченого токена: гомографы->латиница, прочие кир->'?',
    цифро-визуалы 1->l,0->o. Для fuzzy-выравнивания с eng-OCR токеном."""
    out = []
    for c in core:
        if c in _CYR2LAT:
            out.append(_CYR2LAT[c].lower())
        elif c.isascii() and c.isalpha():
            out.append(c.lower())
        elif c == "1":
            out.append("l")
        elif c == "0":
            out.append("o")
        elif c.isdigit():
            out.append(c)
    return "".join(out)


def _similar(a: str, b: str) -> float:
    """Похожесть по нормализованной длине общей подпоследовательности (0..1)."""
    if not a or not b:
        return 0.0
    # LCS
    la, lb = len(a), len(b)
    dp = [0] * (lb + 1)
    for i in range(1, la + 1):
        prev = 0
        for j in range(1, lb + 1):
            tmp = dp[j]
            dp[j] = prev + 1 if a[i - 1] == b[j - 1] else max(dp[j], dp[j - 1])
            prev = tmp
    lcs = dp[lb]
    return lcs / max(la, lb)


_CRIT_TERM_HINT = re.compile(r"[A-ZА-Я]{2,}\d|\d[A-ZА-Я]{2,}")


def _looks_critical_term(core: str) -> bool:
    """Термин, похожий на ген/белок/аббревиатуру (PD1, CTLA4, VEGF) -> критический."""
    lat = "".join(_CYR2LAT.get(c, c) for c in core)
    return bool(_CRIT_TERM_HINT.search(lat))


def _collect_latin_vocab(sections, tables, excluded) -> set:
    """Чистые латинские слова (>=3 симв), уже присутствующие в документе (E3-свидетель)."""
    vocab: set = set()
    blobs: List[str] = []
    for s in _walk(sections):
        blobs.append(getattr(s, "text", "") or "")
        blobs.append(getattr(s, "title", "") or "")
    for t in tables:
        blobs.append(getattr(t, "raw_text", "") or "")
    for item in (excluded.get("appendices") or []):
        blobs.append(item.get("text", "") or "")
    for b in blobs:
        for tok in _TOKEN_RE.findall(b):
            w = tok.strip("().,;:[]«»/\\")
            if len(w) >= 3 and not _CYR_ANY.search(w) and any(c.isalpha() for c in w):
                vocab.add(w)
    return vocab
