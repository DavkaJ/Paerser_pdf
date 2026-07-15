# -*- coding: utf-8 -*-
"""
Самопроверяющий структурный валидатор вывода парсера КР против ОРИГИНАЛА PDF.

Источник истины о структуре документа — его собственное «Оглавление». Валидатор
извлекает оглавление прямо из PDF, превращает его в список ожидаемых разделов
(номер + заголовок) и сверяет с фактическим деревом разделов, которое выдал
парсер. Работает на ПРОИЗВОЛЬНОЙ КР, без эталонного JSON.

    python validate.py путь/к/КРXXX.pdf                  # прогнать парсер на PDF
    python validate.py путь/к/КРXXX.pdf --json out.json  # взять готовый JSON
    python validate.py путь/к/папке                      # все *.pdf в папке
    python validate.py                                   # авто: tests/pdfs (или data)

Расхождения (FAIL — вероятный баг парсера; WARN — дефект источника/инфо):
    MISSING          номер+заголовок есть в ОГЛАВЛЕНИИ и в ТЕЛЕ документа, но НЕ
                     стал разделом в выводе -> FAIL (раздел потерян/всосан в блоб)
    HIERARCHY_BROKEN у узла N.N.N родитель в дереве не равен N.N -> FAIL
    TITLE_BLEED      title содержит маркер списка / служебное слово рекомендации /
                     код медуслуги в скобках -> FAIL (тело утекло в заголовок)
    DUPLICATE        один номер повторяется с тем же (пустым) заголовком -> FAIL
    SOURCE_DEFECT    дубль/пропуск/перенумерация в самом оглавлении или теле -> WARN
    EXTRA            раздел вывода, которого нет в оглавлении -> WARN
    TITLE_MISMATCH   номер совпал, заголовки расходятся (перенумерация/§4) -> WARN

Структурные инварианты (для любого файла, даже без читаемого оглавления):
    coverage_percent (низкое — FAIL); чистота заголовков; отсутствие настоящих
    дублей. Если оглавление извлечь не удалось — применяются только инварианты.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
import warnings
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# jsonschema — обязательная зависимость валидатора. Отсутствие библиотеки НЕ
# должно молча пропускать проверку схемы (fail-open): даём импорту упасть, чтобы
# валидатор не запустился вовсе, а не «прошёл» без контроля контракта.
import jsonschema

from crparser.engine.parser import DocumentParser
from crparser.engine.jsonio import JsonWriter
from crparser.engine.pdf_reader import PdfReader
from crparser.engine.toc import norm, titles_match, parse_entries, toc_bounds, TocIndex
from crparser.engine.textnorm import section_sign_counts, section_sign_glyph_tokens
from crparser.profiles import create_profile

# Порог покрытия: ниже COV_FAIL — потеря текста (FAIL); ниже COV_WARN — заметка.
COV_FAIL = 99.0
COV_WARN = 99.9

# Порог OVERCOUNT: сумма учтённого (included+excluded+tables) выше total_chars на
# >2% -> дубли между buckets. Замер: у КР848_1 сумма 61 551 при total 54 047
# (превышение 13,9%), у КР809_1 175 807 против 171 506 (2,51%). Ниже 2% — шум
# округления подписей таблиц.
OVERCOUNT_MIN = 1.02

# --- структурные гейты (промпт 04): triage-пороги, откалиброваны по КОРПУСУ (не по
# gold set — промпт 14 ещё не выполнен). Файл, прошедший гейты, — КАНДИДАТ, не
# аттестованный документ. После промпта 14 перекалибровать на gold-dev + holdout. ---
COLLAPSE_MIN_SECTIONS = 3       # <= стольких разделов
COLLAPSE_MIN_CHARS = 20000      # при таком объёме — структура схлопнулась. Замер:
#                                 ловит КР848_1 (1 секция) и КР401_2 (3).
COLLAPSE_DOMINANCE = 0.60       # доля текста крупнейшей секции от included при >3
#                                 секциях. Замер: КР973/953/205/246 = 0.61..0.77;
#                                 <0.55 начинает цеплять короткие легитимные КР.
COLLAPSE_HEADING_TAIL = 200     # >стольких символов после встроенного заголовка =
#                                 несобранный раздел внутри text (ловит КР401_2).
CANONICAL_RECALL_MIN = 5        # из 7 канонических глав; <5 -> потеряны главы, 0 -> FAIL.
TABLE_STUB_MAX_CHARS = 200      # raw_text короче -> подозрение на огрызок-шапку. Замер:
#                                 1152 из 11364 таблиц <200 симв, медиана корпуса 672.
TABLE_DENSITY_MIN = 0.002       # симв/pt^2 при <3 таблицах в документе. Замер: у
#                                 low-text таблиц p10=0.0016, p25=0.0023 -> 0.002 между.
TABLE_MIN_FOR_PCT = 3           # >= стольких таблиц -> порог = 10-й перцентиль ДОКУМЕНТА.

# --- coverage_v2 гейты (промпт 10): span-union, а не бухгалтерия символов. Пороги
# откалиброваны по КОРПУСУ (722, замер `_corpus/measure_cov_v2.py`), triage, не
# аттестация — до промпта 14 ничего не доказывают, перекалибровать на gold-dev. ---
# structured_coverage — доля символов, легших в TRAINING-структуру (span-union, дубли
# считаются один раз). Замер: p10=0.998, ВСЕ 529 PASS >= 0.90; единственный обвал —
# КР848_1 (0.41, уже FAIL). Порог 0.70 ловит катастрофу структуры («1 символ в разделе,
# 99 в excluded.other»), не задевая ни одного PASS.
STRUCTURED_COVERAGE_MIN = 0.70
# garbage_ratio — доля символов в excluded.other (корзина «сегментер не понял документ»).
# Замер: PASS p99=0.007, max по корпусу 0.86 (КР848_1). 0.30 — между шумом и обвалом.
GARBAGE_RATIO_MAX = 0.30
# lost_spans/total_spans — доля span'ов, не сохранившихся НИГДЕ (не служебная обвязка).
# Замер: max по корпусу 1.84% (<2%); реального потерянного тела нет (I12). >0.02 -> REVIEW
# (tripwire для будущих регрессий, сейчас не срабатывает), >0 -> warning (доказуемый список).
LOST_SPANS_REVIEW = 0.02
# overlap_chars/total — доля символов в span'ах с >1 владельцем (span-дубли). Замер:
# overlap_spans>0 у 80% PASS и ВСЕЙ контрольной группы (структурный table↔appendices/
# section↔table) -> REVIEW по нему утопил бы здоровое (Р3). Материальный char-переучёт
# уже ловит OVERCOUNT>1.02; здесь — warning только на ЗАМЕТНОМ дубле, чтобы не шуметь.
DUPLICATION_WARN_RATIO = 0.005

_PIN_PUNCT = ".,;:()[]«»\"'-—%<>±*"
# ссылка на таблицу в тексте: «Таблица N», «табл. N», «в таблице N.M» (N может быть
# приложенческим «П1» или дефис/слэш-составным).
_RE_TABLE_REF = re.compile(
    r"табл(?:иц[аеёуы]\w*|\.)\s*№?\s*((?:П)?\d+(?:[.\-/]\d+)?)", re.IGNORECASE)

_cr_profile_cache = None


def _canonical_chapters() -> dict:
    """Канонические главы — ИЗ ПРОФИЛЯ (константы не дублируются в validate.py)."""
    global _cr_profile_cache
    if _cr_profile_cache is None:
        _cr_profile_cache = create_profile("cr", None)
    return _cr_profile_cache.canonical_chapters()

# JSON Schema выходного документа (draft 2020-12). Валидатор fail-closed: документ,
# не соответствующий контракту, не может быть выпущен ни при каких обстоятельствах.
_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "crparser", "data", "cr_schema.json")
with open(_SCHEMA_PATH, encoding="utf-8") as _fh:
    _CR_SCHEMA = json.load(_fh)
_SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(_CR_SCHEMA)


# --- римская нумерация оглавления (промпт 03, ПРАВКА 4) ---------------------
# Кириллические гомографы римских цифр -> латинские (типовая порча источника).
_ROMAN_HOMOGRAPH = str.maketrans({
    "Х": "X", "х": "X", "С": "C", "с": "C", "І": "I", "і": "I",
    "Ѵ": "V", "ѵ": "V", "У": "Y", "у": "Y"})
# «XII. Заголовок», «V Лечение», смешанный «XII.1 …».
_RE_ROMAN_HEAD = re.compile(r"^([IVXLC]+)(?:\.(\d+))?\.?\s+(.+)$")
_ROMAN_VALS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _roman_to_int(tok: str) -> Optional[int]:
    total = 0
    prev = 0
    for ch in reversed(tok):
        v = _ROMAN_VALS.get(ch)
        if v is None:
            return None
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total


def _int_to_roman(n: int) -> str:
    table = [(100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"),
             (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for val, sym in table:
        while n >= val:
            out += sym
            n -= val
    return out


def _parse_roman_entry(title: str) -> Optional[Tuple[str, str]]:
    """Из пункта оглавления без арабского номера достать римский номер+заголовок.

    Возвращает ('XII', 'Критерии …') / ('XII.1', '…') или None. Строгая проверка:
    токен обязан быть валидной римской цифрой 1..30 (иначе — обычная строка,
    случайно начинающаяся с латинских I/V/X)."""
    orig = (title or "").strip()
    t = orig.translate(_ROMAN_HOMOGRAPH)     # гомографы — ТОЛЬКО для распознавания
    m = _RE_ROMAN_HEAD.match(t)
    if not m:
        return None
    rom = m.group(1).upper()
    val = _roman_to_int(rom)
    if val is None or not (1 <= val <= 30) or _int_to_roman(val) != rom:
        return None
    # заголовок берём из ОРИГИНАЛА (translate 1:1 сохраняет позиции), чтобы не
    # латинизировать кириллицу тела: «оценки качеСтва» != «оценки качеCтва».
    rest = orig[m.start(3):].strip()
    if len(rest) < 3:
        return None
    number = rom if not m.group(2) else "%s.%s" % (rom, m.group(2))
    return number, rest

# Якорь оглавления (тот же, что у профиля КР). Парсинг оглавления — общий код в
# crparser.engine.toc (его же использует парсер, чтобы отсеивать фантомы — баг 4).
_TOC_ANCHOR = re.compile(r"^\s*(оглавление|содержание)\s*$", re.IGNORECASE)
_NUM_RE = re.compile(r"^\d+(\.\d+)*$")
# разделы-регионы (список литературы/приложения/критерии), которым источник дал
# номер: парсер держит их в excluded, а не как нумерованный раздел — не «потеря».
_TOC_REF_APP = re.compile(
    r"^\s*(список\s+литератур|приложени|критери\w*\s+оценки\s+качеств)", re.IGNORECASE)
# битая перекрёстная ссылка Word в оглавлении — не раздел (дефект источника).
_TOC_BROKEN_BOOKMARK = re.compile(r"ошибка!?\s*закладка\s+не\s+определена", re.IGNORECASE)
# запись автора из состава рабочей группы (нумерованный список ФИО в оглавлении):
# учёные степени «д.м.н./к.м.н./проф.» или «является членом … ассоциации». Строгие
# маркеры — обычных разделов КР не касаются.
_TOC_AUTHOR = re.compile(
    r"\b[дкpп]\s*\.?\s*м\s*\.?\s*н\s*\.?|\bпроф\.|\bакадемик\b"
    r"|являетс\w*\s+членом\b|профессиональн\w*\s+ассоциаци", re.IGNORECASE)

# --- признаки утечки тела в заголовок (инварианты) --------------------------
_TITLE_LIST_MARKER = re.compile(r"[•·●▪‣◦⁃]")
# только ГЛАГОЛЬНЫЕ формы (рекомендуется/рекомендовано) — признак утёкшего тела;
# прилагательное «рекомендуемые (препараты)» легитимно в названии раздела.
_TITLE_RECO = re.compile(
    r"\b(не\s+)?рекоменд(уется|уются|овано|овани\w*)\b", re.IGNORECASE)
_TITLE_SERVICE = re.compile(
    r"\b(комментари\w*|уровень\s+убедительности|уровень\s+достоверности)\b",
    re.IGNORECASE)
_TITLE_CODE = re.compile(r"\([A-ZА-Я]\d{2}[.\d]+")


# ============================================================================
# Извлечение и разбор оглавления (через общий crparser.engine.toc)
# ============================================================================

class Toc:
    """Разобранное оглавление + нормализованный текст ТЕЛА (вне оглавления)."""

    def __init__(self, entries: List[Tuple[Optional[str], str]], body_norm: str) -> None:
        self.entries = entries
        self.body_norm = body_norm

    def numbered(self) -> List[Tuple[str, str]]:
        """Нумерованные пункты оглавления: И арабские, И римские (верхний уровень).
        Римские пункты источник хранит как (None, 'XII. …') — парсер оглавления их
        не нумерует; извлекаем римский номер здесь."""
        out: List[Tuple[str, str]] = []
        for n, t in self.entries:
            if n and _NUM_RE.match(n):
                out.append((n, t))
            elif n is None:
                r = _parse_roman_entry(t)
                if r:
                    out.append(r)
        return out

    def body_has_numbered(self, number: str, title: str) -> bool:
        """В теле документа номер стоит рядом со своим заголовком (это раздел)."""
        words = norm(title).split()
        if not words:
            return False
        key = norm(number) + " " + " ".join(words[:4])
        return key in self.body_norm

    def body_has_title(self, title: str) -> bool:
        words = norm(title).split()
        return bool(words) and " ".join(words[:4]) in self.body_norm


# Кэш разбора оглавления (промпт 00). Включён по умолчанию; --no-toc-cache отключает.
_TOC_CACHE_ENABLED = True
_toc_code_sig_cache: Optional[str] = None
_ocr_stack_sig_cache: Optional[str] = None


def _toc_code_sig() -> str:
    """Короткий (8 hex) хеш кода, влияющего на РАЗБОР оглавления (entries+body_norm):
    toc.py целиком + parse_toc + якорь TOC. НЕ включает Toc.numbered()/римские хелперы
    и гейты валидатора — они работают ПОСЛЕ кэша, поверх сырых entries, поэтому их
    правки (промпты 04/10/12) кэш не инвалидируют. Смена самого разбора — инвалидирует."""
    global _toc_code_sig_cache
    if _toc_code_sig_cache is not None:
        return _toc_code_sig_cache
    import hashlib
    import inspect
    from crparser.engine import toc as _toc_mod
    parts = [open(_toc_mod.__file__, "rb").read(),
             inspect.getsource(parse_toc).encode("utf-8"),
             _TOC_ANCHOR.pattern.encode("utf-8")]
    _toc_code_sig_cache = hashlib.sha256(b"\x00".join(parts)).hexdigest()[:8]
    return _toc_code_sig_cache


def _ocr_stack_sig() -> str:
    """Отпечаток OCR-стека (8 hex). КРИТИЧНО для ключа TOC-кэша: parse_toc ->
    PdfReader.read() -> ГИБРИД-OCR (pdf_reader._maybe_hybrid_ocr). Значит entries/
    body_norm ЗАВИСЯТ от версии Tesseract/traineddata/Pillow/DPI и самой доступности
    OCR. Без этого отпечатка кэш молча отдал бы оглавление, посчитанное при ДРУГОМ
    состоянии стека, — а TOC определяет MISSING, MISSING определяет FAIL (тот же класс
    бага, что убран из OCR-кэша в промпте 02). Переиспользуем ocr._stack_signature."""
    global _ocr_stack_sig_cache
    if _ocr_stack_sig_cache is not None:
        return _ocr_stack_sig_cache
    try:
        from crparser.engine import ocr
        cmd = ocr._resolve_tesseract()
        tessdata = os.environ.get("TESSDATA_PREFIX") or None
        langs = os.environ.get("OCR_LANGS", "rus+eng")
        dpi = max(int(os.environ.get("OCR_DPI", ocr._DEFAULT_DPI)), 300)
        sig = ocr._stack_signature(cmd, tessdata, langs, dpi, ocr._PREPROC_VERSION)[:8]
    except Exception:  # noqa: BLE001 — при недоступном OCR-модуле отдельное пространство
        sig = "noocrmod"
    _ocr_stack_sig_cache = sig
    return _ocr_stack_sig_cache


def _toc_cache_key() -> str:
    """Ключ разбора для имени кэша: <toc_code_sha8>_<ocr_stack_sha8>."""
    return "%s_%s" % (_toc_code_sig(), _ocr_stack_sig())


def parse_toc(pdf_path: str) -> Optional[Toc]:
    """Прочитать PDF, разобрать оглавление (общий код) и текст тела вне него.
    Результат кэшируется по (sha256 PDF + хеш кода разбора + отпечаток OCR-стека) —
    PDF не открывается повторно при попадании (промпт 00). OCR-стек в ключе обязателен:
    parse_toc гибрид-OCR-ит кандидатов, поэтому оглавление зависит и от стека."""
    from crparser.engine import toc_cache
    from crparser.engine.pdf_reader import _pdf_sha256
    sha = _pdf_sha256(pdf_path) if _TOC_CACHE_ENABLED else None
    key = _toc_cache_key() if sha else None
    if sha:
        hit = toc_cache.load(sha[:12], key)
        if hit is not toc_cache._MISSING:
            ents = hit.get("entries")
            if ents is None:
                return None
            return Toc([tuple(e) for e in ents], hit.get("body_norm", ""))
    reader = PdfReader(pdf_path)
    try:
        pages = reader.read()
    finally:
        reader.close()
    lines = [ln.text for page in pages for ln in page.lines]
    entries = parse_entries(lines, _TOC_ANCHOR)
    if entries is None:
        if sha:
            toc_cache.store(sha[:12], key, None, "")
        return None
    # текст тела (вне региона оглавления) — для проверки «номер стоит рядом со
    # своим заголовком в теле» (отличает реальную потерю от перенумерации)
    bounds = toc_bounds(lines, _TOC_ANCHOR)
    start, end = bounds if bounds else (0, -1)
    body_norm = norm(" ".join(lines[:start] + lines[end + 1:]))
    if sha:
        toc_cache.store(sha[:12], key, list(entries), body_norm)
    return Toc(entries, body_norm)


# ============================================================================
# Обход дерева разделов парсера
# ============================================================================

def walk(sections: List[dict], parent: Optional[dict] = None):
    for s in sections:
        yield s, parent
        yield from walk(s.get("children", []), s)


# ============================================================================
# Отчёт + валидатор одного документа
# ============================================================================

class Report:
    def __init__(self, name: str) -> None:
        self.name = name
        self.fails: List[str] = []
        self.warns: List[str] = []
        self.reviews: List[str] = []          # порча текста -> статус REVIEW
        self.corruption: Dict[str, int] = {}  # разбивка по типам порчи
        self.skipped: Optional[str] = None   # причина SKIPPED_SCAN (скан без текста)

    def fail(self, kind: str, msg: str) -> None:
        self.fails.append(f"{kind}: {msg}")

    def warn(self, kind: str, msg: str) -> None:
        self.warns.append(f"{kind}: {msg}")

    def review(self, kind: str, msg: str) -> None:
        self.reviews.append(f"{kind}: {msg}")

    def skip(self, reason: str) -> None:
        self.skipped = reason

    @property
    def ok(self) -> bool:
        # fail-closed: REVIEW тоже НЕ ok. PASS — только чистый файл без замечаний.
        return not self.fails and not self.reviews and self.skipped is None

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        if self.fails:
            return "FAIL"
        if self.reviews:
            return "REVIEW"
        return "PASS"


def _schema_error(doc) -> Optional[str]:
    """Первая ошибка схемы (или None). Пустой {} и любой не-контрактный документ
    вернут ошибку -> SCHEMA FAIL (fail-closed)."""
    if not isinstance(doc, dict):
        return "документ не является объектом JSON"
    errs = sorted(_SCHEMA_VALIDATOR.iter_errors(doc),
                  key=lambda e: [str(p) for p in e.path])
    if errs:
        e = errs[0]
        loc = "/".join(str(p) for p in e.path) or "<root>"
        return "%s: %s" % (loc, e.message[:160])
    return None


def _recount_stats(doc: dict) -> Dict[str, int]:
    """Пересчитать счётчики ПРЯМО из doc (той же формулой, что stats.py), чтобы
    сверить с заявленными в doc['stats'] — их никто иначе не перепроверяет."""
    flat = [s for s, _ in walk(doc.get("sections", []))]
    included = sum(len(s.get("title") or "") + len(s.get("text") or "") for s in flat)
    excluded = 0
    for bucket in (doc.get("excluded", {}) or {}).values():
        for item in bucket:
            excluded += len(item.get("title", "")) + len(item.get("text", ""))
    tables = doc.get("tables", []) or []
    table = sum(len(t.get("raw_text") or "") + len(t.get("caption") or "")
                for t in tables)
    return {"sections_found": len(flat), "tables_found": len(tables),
            "included_chars": included, "excluded_chars": excluded,
            "table_chars": table}


def _check_stats_and_coverage(rep: "Report", doc: dict, stats: dict) -> None:
    """ПРАВКА 3: stats пересчитываются, а не принимаются на веру."""
    recount = _recount_stats(doc)
    # STATS_MISMATCH — расхождение заявленного и фактического любого счётчика.
    for field, actual in recount.items():
        if field in stats and stats.get(field) != actual:
            rep.fail("STATS_MISMATCH",
                     "%s: заявлено %s, факт %s" % (field, stats.get(field), actual))
    # coverage_percent вне [0,100] / NaN / None -> FAIL.
    cov = stats.get("coverage_percent")
    bad = (cov is None or not isinstance(cov, (int, float))
           or isinstance(cov, bool)
           or (isinstance(cov, float) and math.isnan(cov))
           or cov < 0 or cov > 100)
    if bad:
        rep.fail("COVERAGE_INVALID",
                 "coverage_percent вне [0,100]/NaN/None: %r" % (cov,))
    # OVERCOUNT — сумма учтённого превышает источник (дубли между buckets).
    total = stats.get("total_chars", 0) or 0
    accounted = (recount["included_chars"] + recount["excluded_chars"]
                 + recount["table_chars"])
    if total > 0 and accounted > total * OVERCOUNT_MIN:
        rep.review("OVERCOUNT",
                   "сумма учтённого %d превышает источник %d на %d символов "
                   "(%.1f%%) — дубли между buckets"
                   % (accounted, total, accounted - total, accounted / total * 100 - 100))
    # coverage_percent пересчитываем той же формулой, что stats.py, и сверяем с
    # заявленным: подделанное покрытие (100 при фактических 40) -> STATS_MISMATCH.
    if isinstance(total, int) and total > 0 and not bad:
        accounted_capped = min(accounted, total)
        exp_cov = (round(accounted_capped / total * 100, 2)
                   if recount["included_chars"] else 0.0)
        if abs(float(cov) - exp_cov) > 0.01:
            rep.fail("STATS_MISMATCH",
                     "coverage_percent: заявлено %s, факт %s" % (cov, exp_cov))


def _successor_numbers(num: str) -> set:
    """Кандидаты «следующего» номера: инкремент последнего компонента и следующая
    глава верхнего уровня. Для «1.4» -> {«1.5», «2»}."""
    parts = num.split(".")
    out = set()
    if parts and parts[-1].isdigit():
        out.add(".".join(parts[:-1] + [str(int(parts[-1]) + 1)]))
    if parts and parts[0].isdigit():
        out.add(str(int(parts[0]) + 1))
    return out


def _embedded_heading_count(section: dict) -> int:
    """Число РАЗНЫХ «несобранных заголовков» внутри плоского text секции с номером N:
    последовательные номера N.k+1, N.k+2 …, каждый как «<номер> <Заглавная>» и с
    >COLLAPSE_HEADING_TAIL символов после него. На плоском тексте (без переносов
    строк) единичное совпадение «<число> <Заглавная>» шумно (глава «1» содержит
    «2 Недели»), поэтому сигнал — НЕСКОЛЬКО подряд идущих подчинённых номеров,
    ровно как у КР401_2 (в 1.4 сидят 1.5, 1.6, 2). Возвращает их количество."""
    num = section.get("number")
    text = section.get("text") or ""
    if not num or "." not in num or len(text) < COLLAPSE_HEADING_TAIL + 5:
        return 0
    prefix, last = num.rsplit(".", 1)
    if not last.isdigit():
        return 0
    found = 0
    for k in range(int(last) + 1, int(last) + 6):     # N.k+1 .. N.k+5
        succ = "%s.%d" % (prefix, k)
        pat = re.compile(r"(?:^|[.!?)\s])" + re.escape(succ) + r"\s+[А-ЯЁA-Z]")
        m = pat.search(text)
        if m and len(text) - m.start() > COLLAPSE_HEADING_TAIL:
            found += 1
        else:
            break                                     # номера идут подряд — разрыв = конец
    return found


def _has_embedded_heading(section: dict) -> bool:
    """>=2 подряд несобранных подчинённых заголовка в одной секции (сигнатура КР401_2)."""
    return _embedded_heading_count(section) >= 2


def _canonical_recall(doc: dict) -> int:
    """Сколько из 7 канонических глав представлено в дереве секций. Понимает И
    арабскую нумерацию (первый компонент номера 1..7), И заголовок канонической
    главы (роман/именованная глава без арабского номера) — иначе роман-документы
    (КР66_4/876_1, где парсер перенумеровал в арабские) ложно теряли бы recall."""
    canon = _canonical_chapters()          # префикс -> {номера}
    present: set = set()
    for s, _ in walk(doc.get("sections", [])):
        num = s.get("number")
        if num:
            head = num.split(".")[0]
            if head.isdigit() and 1 <= int(head) <= 7:
                present.add(int(head))
        low = norm(s.get("title") or "")
        if low:
            for prefix, numbers in canon.items():
                singles = {n for n in numbers if 1 <= n <= 7}
                if len(numbers) == 1 and singles and low.startswith(prefix):
                    present |= singles
    return len(present)


def _pin_hits(text: str, base) -> int:
    """Точные попадания ключей активной базы пинов (нормализация как в anchor_hit)."""
    n = 0
    for tok in text.split():
        core = tok.strip(_PIN_PUNCT).lower()
        if len(core) >= 3 and core in base:
            n += 1
    return n


def _mixed_script_words(text: str) -> int:
    """Число ТОКЕНОВ с кирилло-латинской мешаниной (широкий детектор, для триажа)."""
    n = 0
    for tok in text.split():
        has_cyr = any("а" <= c.lower() <= "я" or c in "ёЁ" for c in tok)
        has_lat = any("a" <= c.lower() <= "z" for c in tok)
        if has_cyr and has_lat:
            n += 1
    return n


def _residual_pin_gate(rep: "Report", doc: dict) -> None:
    """ГЕЙТ 4: остаточные пины по зонам. ТЕЛО>=1 -> REVIEW; только БИБЛИО/СЛУЖЕБНОЕ
    -> warning. База — v2 map через ocr_pins.load_base (не верхний уровень словаря)."""
    from crparser.engine import ocr_pins
    base = ocr_pins.load_base()
    if not base:
        return
    exc = doc.get("excluded", {}) or {}
    body = []
    for s, _ in walk(doc.get("sections", [])):
        body.append(s.get("title") or "")
        body.append(s.get("text") or "")
    for t in doc.get("tables", []) or []:
        body.append(t.get("raw_text") or "")
        body.append(t.get("caption") or "")
    for v in (doc.get("metadata") or {}).values():
        if isinstance(v, str):
            body.append(v)
    for item in exc.get("appendices", []):
        body.append(item.get("title", ""))
        body.append(item.get("text", ""))
    biblio = " ".join(item.get("title", "") + " " + item.get("text", "")
                      for item in exc.get("references", []))
    service = " ".join(item.get("title", "") + " " + item.get("text", "")
                       for k in ("toc", "front_matter", "other")
                       for item in exc.get(k, []))
    body_hits = _pin_hits(" ".join(body), base)
    if body_hits >= 1:
        rep.review("RESIDUAL_PIN",
                   "%d точных попаданий пинов в ТЕЛЕ (sections/tables/metadata/"
                   "appendices) — остаточная кирилло-латинская порча" % body_hits)
    else:
        bh, sh = _pin_hits(biblio, base), _pin_hits(service, base)
        if bh or sh:
            rep.warn("RESIDUAL_PIN",
                     "пины только в БИБЛИО(%d)/СЛУЖЕБНОМ(%d) — эти зоны вырезаются "
                     "контрактом обучения" % (bh, sh))
    mixed = _mixed_script_words(_all_text(doc))
    if mixed:
        rep.warn("MIXED_SCRIPT",
                 "%d слов с кирилло-латинской мешаниной (широкий детектор, для "
                 "триажа — НЕ REVIEW)" % mixed)


def _tables_suspect_gate(rep: "Report", doc: dict, nodes: List[dict]) -> None:
    """ГЕЙТ 5: TABLE_STUB (warning, по плотности) + TABLES_MISSING (REVIEW)."""
    tables = doc.get("tables", []) or []
    dens = []
    for t in tables:
        x0, y0, x1, y1 = (t.get("bbox") or [0, 0, 0, 0])[:4]
        area = max((x1 - x0) * (y1 - y0), 1.0)
        dens.append(len(t.get("raw_text") or "") / area)
    p10 = None
    if len(tables) >= TABLE_MIN_FOR_PCT and dens:
        sd = sorted(dens)
        p10 = sd[min(len(sd) - 1, int(len(sd) * 0.10))]
    stubs = sum(1 for t, dv in zip(tables, dens)
                if len(t.get("raw_text") or "") < TABLE_STUB_MAX_CHARS
                and dv < (p10 if p10 is not None else TABLE_DENSITY_MIN))
    if stubs:
        rep.warn("TABLE_STUB",
                 "%d таблиц: raw_text<%d и низкая плотность (вырезана шапка, тело "
                 "потеряно) — проверить извлечение" % (stubs, TABLE_STUB_MAX_CHARS))
    mentioned = set()
    for s in nodes:
        for m in _RE_TABLE_REF.finditer(s.get("text") or ""):
            mentioned.add(m.group(1))
    if len(mentioned) > len(tables):
        rep.review("TABLES_MISSING",
                   "в тексте упомянуто %d уникальных номеров таблиц, извлечено %d "
                   "объектов" % (len(mentioned), len(tables)))


def _structural_gates(rep: "Report", doc: dict, stats: dict) -> None:
    """Пять независимых семантических гейтов структуры (промпт 04). Каждый — свой
    kind, чтобы дельту можно было разложить по причинам."""
    nodes = [s for s, _ in walk(doc.get("sections", []))]
    total = stats.get("total_chars", 0) or 0
    inc = stats.get("included_chars", 0) or 0
    sec = len(nodes)

    # ГЕЙТ 1. COLLAPSE
    if sec <= COLLAPSE_MIN_SECTIONS and total > COLLAPSE_MIN_CHARS:
        rep.review("COLLAPSE",
                   "sections_found=%d при total_chars=%d — документ такого размера "
                   "не может иметь столько разделов" % (sec, total))
    elif sec > 3 and inc > 0:
        biggest = max((len(s.get("title") or "") + len(s.get("text") or "")
                       for s in nodes), default=0)
        if biggest / inc > COLLAPSE_DOMINANCE:
            rep.review("COLLAPSE",
                       "крупнейшая секция держит %.0f%% текста разделов (>%.0f%%) — "
                       "остальное свалено в один узел"
                       % (biggest / inc * 100, COLLAPSE_DOMINANCE * 100))
    for s in nodes:
        if _has_embedded_heading(s):
            rep.review("COLLAPSE",
                       "заголовок следующего уровня внутри text секции %r — "
                       "несобранный раздел" % s.get("number"))
            break

    # ГЕЙТ 2. CANONICAL_RECALL
    recall = _canonical_recall(doc)
    if recall == 0 and inc > 0:
        rep.fail("CANONICAL_RECALL",
                 "0 из 7 канонических глав в дереве при непустом тексте")
    elif recall < CANONICAL_RECALL_MIN:
        rep.review("CANONICAL_RECALL",
                   "%d из 7 канонических глав распознано (<%d) — потеряны главы"
                   % (recall, CANONICAL_RECALL_MIN))

    # ГЕЙТ 3. OCR_REQUIRED — needs_ocr никогда не corpus-ready
    cor = stats.get("corruption", {}) or {}
    ocr_warn = any("ocr" in (w or "").lower() for w in doc.get("warnings", []))
    if (int(cor.get("glyph_tokens", 0)) > 0
            or int(cor.get("pseudo_ascii_tokens", 0)) > 0 or ocr_warn):
        rep.review("OCR_REQUIRED",
                   "нужен OCR (glyph_tokens=%s, pseudo_ascii=%s) — файл не может быть "
                   "corpus-ready" % (cor.get("glyph_tokens"),
                                     cor.get("pseudo_ascii_tokens")))

    # ГЕЙТ 4. RESIDUAL_PIN / MIXED_SCRIPT
    _residual_pin_gate(rep, doc)

    # ГЕЙТ 5. TABLES_SUSPECT
    _tables_suspect_gate(rep, doc, nodes)

    # ГЕЙТ 6. COVERAGE_V2 (промпт 10) — сохранность структуры по span-union
    _coverage_v2_gate(rep, stats)


def _coverage_v2_gate(rep: "Report", stats: dict) -> None:
    """ГЕЙТ 6 (промпт 10). Читает stats.coverage_v2 (его считает stats.py по span-union
    из page_ir — валидатор не пересчитывает, S/сироты из JSON не восстановить). Обвал
    структуры (structured/garbage) -> REVIEW; потеря span'ов -> REVIEW при >2% / warning
    при >0; span-дубли -> warning (REVIEW утопил бы контрольную группу, см. константы)."""
    v2 = stats.get("coverage_v2")
    if not v2:
        return                         # старый JSON без provenance — гейт пропускаем
    sc = v2.get("structured_coverage")
    if sc is not None and sc < STRUCTURED_COVERAGE_MIN:
        rep.review("LOW_STRUCTURED_COVERAGE",
                   "structured_coverage=%.3f < %.2f — содержательный текст не лёг в "
                   "структуру (span-union; coverage_percent при этом может быть 100)"
                   % (sc, STRUCTURED_COVERAGE_MIN))
    gr = v2.get("garbage_ratio")
    if gr is not None and gr > GARBAGE_RATIO_MAX:
        rep.review("HIGH_GARBAGE",
                   "garbage_ratio=%.3f > %.2f — сегментер свалил текст в excluded.other"
                   % (gr, GARBAGE_RATIO_MAX))
    lost = int(v2.get("lost_spans", 0) or 0)
    total = int(v2.get("total_spans", 0) or 0)
    uids = ", ".join(v2.get("lost_span_uids", []) or [])
    if total and lost / total > LOST_SPANS_REVIEW:
        rep.review("LOST_CONTENT",
                   "lost_spans=%d/%d (%.1f%%) не сохранились нигде: %s"
                   % (lost, total, 100 * lost / total, uids))
    elif lost > 0:
        rep.warn("LOST_CONTENT",
                 "%d span'ов не сохранились нигде (не обвязка), первые: %s" % (lost, uids))
    overlap = int(v2.get("overlap_spans", 0) or 0)
    ochars = int(v2.get("overlap_chars", 0) or 0)
    tot_chars = int(stats.get("total_chars", 0) or 0)
    if overlap > 0 and tot_chars and ochars / tot_chars > DUPLICATION_WARN_RATIO:
        rep.warn("DUPLICATION",
                 "%d span'ов заявлены >1 владельцем (%d симв, %.1f%% документа) — "
                 "дубль между buckets" % (overlap, ochars, 100 * ochars / tot_chars))


def _latin_gate(rep: "Report", doc: dict) -> None:
    """ГЕЙТ latin recovery (промпт 13b, ШАГ 8). Читает top-level блок latin_recovery
    (есть ТОЛЬКО при прогоне за флагом --latin-recovery). Документ с хотя бы ОДНОЙ
    неразрешённой КРИТИЧЕСКОЙ латинской сущностью (ICD/ATC/TNM/ген/доза) в обучаемой зоне
    -> REVIEW (карантин LATIN_UNRESOLVED): в обучение не едет. Замены needs_review НЕ
    блокируют (лучший кандидат применён, спан в очереди на человека), но выводятся числом."""
    lr = doc.get("latin_recovery")
    if not lr:
        return                          # флаг выключен / нет блока — гейт инертен
    unresolved = lr.get("unresolved_critical") or []
    if unresolved:
        sample = ", ".join(
            "%s(%s)" % (u.get("source_text"), u.get("kind")) for u in unresolved[:8])
        rep.review("LATIN_UNRESOLVED",
                   "%d неразрешённых КРИТИЧЕСКИХ латинских сущностей в обучаемой зоне "
                   "(на карантин до ручной проверки): %s" % (len(unresolved), sample))
    nr = sum(1 for c in (lr.get("corrections") or [])
             if c.get("decision") == "needs_review")
    if nr:
        rep.warn("LATIN_REVIEW",
                 "%d латинских замен помечены needs_review (лучший кандидат применён, "
                 "спаны в очереди верификации)" % nr)


def validate_doc(name: str, doc: dict, toc: Optional[Toc]) -> Report:
    rep = Report(name)
    # ---- ПРАВКА 1: JSON Schema (fail-closed). Пустой {} и любой не-контрактный
    # документ -> SCHEMA FAIL, а НЕ SKIP: файл, не отвечающий контракту, выпущен
    # быть не может ни при каких условиях. ----
    serr = _schema_error(doc)
    if serr is not None:
        rep.fail("SCHEMA", serr)
        return rep
    sections = doc.get("sections", [])
    stats = doc.get("stats", {})
    # ---- ПРАВКА 3: перепроверка stats/coverage до сверки со скан-логикой ----
    _check_stats_and_coverage(rep, doc, stats)

    # ---- скан без текстового слоя -> SKIPPED_SCAN (не FAIL, без сверки с TOC) ----
    # PDF-скан даёт пустой/почти пустой извлекаемый текст (total_chars ≈ 0): файл
    # на OCR/ручную обработку. Случай «текст есть, но в разделы не попал» — это уже
    # не скан, а честный REVIEW (см. проверку пустого вывода ниже).
    total_chars = stats.get("total_chars", 0) or 0
    cov = stats.get("coverage_percent", 0.0)
    sec_found = stats.get("sections_found", 0)
    included_chars = stats.get("included_chars", 0) or 0
    # весь извлечённый текст документа (разделы + excluded, где лежат references) —
    # для честного статуса ниже и для §-глиф-детекции ПО ВСЕМУ тексту (порча может
    # сидеть только в библиографии, как у КР263_2, — по одному телу не поймать).
    all_text = _all_text(doc)

    # SKIPPED_SCAN — ТОЛЬКО при реально пустом текстовом слое (total_chars<200).
    # Раньше сюда же попадала ветка «cov<=0 и sec_found==0», но она поглощена
    # проверкой пустого вывода ниже (sec_found==0 => included_chars==0 => REVIEW):
    # файл с текстом, но без разделов — это не скан на OCR, а честный REVIEW.
    # not rep.fails: реальный скан имеет согласованные ~0 stats и не даёт FAIL;
    # если stats уже дали FAIL (подделаны) — это НЕ безобидный скан, не маскируем.
    if total_chars < 200 and not rep.fails:
        rep.skip(f"текстовый слой пуст (total_chars={total_chars}, "
                 f"coverage={cov}) — скан, на OCR/ручную обработку")
        return rep

    # ---- пустой/бестекстовый вывод: текст ЕСТЬ, но в разделы не попал -> REVIEW ----
    # total_chars>=200 (не скан), но sections_found==0 ИЛИ included_chars==0: в
    # дерево разделов не попало НИЧЕГО (excluded/таблицы могли дать фиктивное
    # покрытие). Такой файл НЕ должен молча пройти как PASS. Поднимаем REVIEW и
    # НЕ ведём сверку с оглавлением — нулевые разделы дали бы лавину MISSING/FAIL,
    # а причина одна: вывод пуст. Аналог SKIPPED_SCAN, но текстовый слой не пуст.
    if sec_found == 0 or included_chars == 0:
        kind = "NO_SECTIONS" if sec_found == 0 else "EMPTY_OUTPUT"
        rep.review(kind,
                   f"в разделы не попало содержимое (sections_found={sec_found}, "
                   f"included_chars={included_chars}, total_chars={total_chars}) — "
                   f"вывод пуст, требуется ручная проверка")
        _corruption_review(rep, stats, all_text)   # порча могла сопутствовать
        return rep

    # ---- тотальная «обратная» глифовая порча (кириллица->ASCII) -> REVIEW ----
    # Плотностной детектор пометил документ как тотально битый (текстовый слой
    # НЕ пуст, но это ASCII-каша из битого cmap). Структурную сверку с TOC не
    # ведём: нулевые разделы и MISSING — СЛЕДСТВИЕ порчи, а не самостоятельный
    # дефект. Файл уходит на OCR: REVIEW, не FAIL (аналог SKIPPED_SCAN, но текст
    # есть). Порог pseudo_ascii_tokens>0 срабатывает только на тотальной порче.
    if int((stats.get("corruption", {}) or {}).get("pseudo_ascii_tokens", 0)) > 0:
        _corruption_review(rep, stats, all_text)
        return rep

    nodes = [s for s, _ in walk(sections)]
    parent_of = {id(s): p for s, p in walk(sections)}
    numbered = [s for s in nodes if s.get("number") and _NUM_RE.match(s["number"])]
    act_count = Counter(s["number"] for s in numbered)
    act_titles: Dict[str, List[str]] = defaultdict(list)
    for s in numbered:
        act_titles[s["number"]].append(s.get("title") or "")

    # ---- инвариант 1: покрытие ----
    if cov <= 0.0:
        rep.warn("COVERAGE", "0% — вероятно скан без текстового слоя")
    elif cov < COV_FAIL:
        rep.fail("COVERAGE", f"{cov}% < {COV_FAIL} — потеря текста")
    elif cov < COV_WARN:
        rep.warn("COVERAGE", f"{cov}% < {COV_WARN}")

    # ---- инвариант 1б: крупный документ без таблиц -> детектор мог отвалиться ----
    tables_found = stats.get("tables_found", 0)
    if sec_found >= 20 and tables_found == 0:
        rep.warn("TABLES", f"{sec_found} разделов, но 0 таблиц — проверить детектор таблиц")

    # ---- инвариант 1в: порча извлечённого текста -> статус REVIEW ----
    # Разрядка/удвоение парсер уже починил (счётчики в stats), глифовую подмену
    # только обнаружил (на OCR). Если порчи выше порога — файл НЕ должен молча
    # проходить как PASS: поднимаем REVIEW с разбивкой по типам.
    _corruption_review(rep, stats, all_text)

    # ---- структурные гейты (промпт 04): COLLAPSE / CANONICAL_RECALL / OCR_REQUIRED
    # / RESIDUAL_PIN / TABLES_SUSPECT. Ловят «структура развалилась» там, где честный
    # 03 (схема/exit/stats) молчит. Независимые kinds — дельта раскладывается по причинам.
    _structural_gates(rep, doc, stats)

    # индекс оглавления (для проверок «подтверждён ли раздел оглавлением»)
    tindex = TocIndex(toc.entries) if toc else None

    # ---- инвариант 2: чистота заголовков (утечка тела) ----
    # Заголовок, ПОДТВЕРЖДЁННЫЙ оглавлением, — это настоящее название раздела:
    # код МКБ «(F10.0)», слово «Рекомендуемые», скобки в нём легитимны, а не утечка
    # тела. Признаки утечки проверяем только у НЕподтверждённых заголовков.
    for s in nodes:
        title = (s.get("title") or "").strip()
        num = s.get("number")
        if num and tindex is not None and tindex.confirmed(num, title):
            continue
        tag = num or ("«" + title[:30] + "…»")
        if _TITLE_LIST_MARKER.search(title):
            rep.fail("TITLE_BLEED", f"{tag}: маркер списка в заголовке -> {title[:70]!r}")
        elif _TITLE_RECO.search(title):
            rep.fail("TITLE_BLEED", f"{tag}: слово рекомендации в заголовке -> {title[:70]!r}")
        elif _TITLE_SERVICE.search(title):
            rep.fail("TITLE_BLEED", f"{tag}: служебное слово в заголовке -> {title[:70]!r}")
        elif _TITLE_CODE.search(title):
            rep.fail("TITLE_BLEED", f"{tag}: код медуслуги в заголовке -> {title[:70]!r}")

    # ---- инвариант 3: настоящие дубли (тот же номер + тот же/пустой заголовок) ----
    for num, titles in act_titles.items():
        if len(titles) < 2:
            continue
        nonempty = {norm(t) for t in titles if norm(t)}
        if len(nonempty) <= 1:
            rep.fail("DUPLICATE", f"{num}: повтор номера с тем же заголовком {titles!r}")

    # ---- иерархия по номеру (только для разделов из оглавления, без коллизий) ----
    toc_numbers = {n for n, _ in toc.numbered()} if toc else set()
    all_numbers = set(act_count)
    for s in numbered:
        num = s["number"]
        if "." not in num or act_count[num] > 1:
            continue
        if toc and num not in toc_numbers:        # фантом/не из оглавления — не судим
            continue
        prefix = num.rsplit(".", 1)[0]
        if prefix not in all_numbers:
            rep.warn("SOURCE_DEFECT", f"{num}: родитель {prefix} отсутствует в документе")
            continue
        parent = parent_of.get(id(s))
        if not parent or parent.get("number") != prefix:
            got = parent.get("number") if parent else None
            rep.fail("HIERARCHY_BROKEN", f"{num}: родитель в дереве = {got!r}, ожидался {prefix!r}")

    # ---- сверка с оглавлением ----
    # ПРАВКА 4: нераспознанное/пустое оглавление — НЕ повод для PASS. Сейчас такой
    # файл молча проходил структурную сверку «ни с чем».
    if toc is None:
        rep.review("TOC_NONE",
                   "оглавление не извлечено — структура не сверена, файл не может быть PASS")
        return rep

    exp = toc.numbered()
    if not exp:
        rep.review("TOC_NONE",
                   "оглавление распознано, но без нумерованных пунктов — сверять не с чем")
    exp_count = Counter(n for n, _ in exp)
    exp_titles: Dict[str, List[str]] = defaultdict(list)
    for n, t in exp:
        exp_titles[n].append(t)

    # дубли в самом оглавлении -> дефект источника
    for num, titles in exp_titles.items():
        uniq = {norm(t) for t in titles if norm(t)}
        if len(titles) > 1 and len(uniq) > 1:
            rep.warn("SOURCE_DEFECT", f"{num}: номер повторяется в оглавлении: {titles!r}")

    # MISSING: каждый пункт оглавления должен быть представлен в выводе.
    # ВАЖНО: точный сигнал «номер стоит вплотную к своему заголовку в теле»
    # (body_has_numbered) имеет приоритет над НЕЧЁТКИМ «перенумерация» — иначе
    # шаблонные заголовки КР («…заболевания или состояния…») похожи друг на друга
    # и реальный пропуск (1.2 «Этиология…» ~ 1.3 «Эпидемиология…») маскируется.
    for num, title in exp:
        matched = any(s["number"] == num and titles_match(title, s.get("title") or "")
                      for s in numbered)
        # номер ПРИСУТСТВУЕТ в выводе, но заголовок записан иначе (аббревиатура:
        # «РМП» вместо «раком мочевого пузыря») — это не потеря, а TITLE_MISMATCH.
        # Считаем раздел представленным, если первые 3 значимых слова совпали.
        if not matched and num in act_count:
            matched = any(_prefix_words_match(title, t) for t in act_titles[num])
        if matched:
            continue
        # римский пункт оглавления, чей заголовок присутствует в выводе под АРАБСКИМ
        # номером (парсер нумерует римские главы канонически: «V. Краткая» -> раздел
        # «1»). Номера НИКОГДА не совпадут (V != 1), поэтому body_has_numbered ниже
        # ложно дал бы MISSING. Это перенумерация, а не потеря -> WARN.
        if not _NUM_RE.match(num) and any(
                titles_match(title, s.get("title") or "") for s in numbered):
            rep.warn("SOURCE_DEFECT",
                     f"{num} {title!r} — римская глава под арабским номером (перенумерация)")
            continue
        # «Список литературы»/«Приложение…»/«Критерии оценки качества», которым
        # источник дал номер раздела, парсер сохраняет как регион-исключение
        # (references/appendices), а не как нумерованный раздел — это не потеря.
        if _TOC_REF_APP.match(title):
            rep.warn("SOURCE_DEFECT",
                     f"{num} {title!r} — нумерованные ссылки/приложение, сохранены как регион-исключение")
            continue
        # битая закладка Word в оглавлении — это сломанная перекрёстная ссылка,
        # а не раздел; парсер её справедливо не создаёт.
        if _TOC_BROKEN_BOOKMARK.search(title):
            rep.warn("SOURCE_DEFECT", f"{num} {title!r} — битая закладка Word, не раздел")
            continue
        # нумерованный список авторов (состав рабочей группы) в оглавлении — не
        # разделы; отсекаем по учёным степеням/членству в ассоциации.
        if _TOC_AUTHOR.search(title):
            rep.warn("SOURCE_DEFECT", f"{num} {title!r} — запись автора, не раздел")
            continue
        if toc.body_has_numbered(num, title):
            # номер вплотную к своему заголовку в ТЕЛЕ, но раздела в выводе нет -> потеря
            rep.fail("MISSING", f"{num} {title!r} — есть в теле, но потерян в выводе")
        elif any(titles_match(title, s.get("title") or "") for s in numbered):
            rep.warn("SOURCE_DEFECT",
                     f"{num} {title!r} — в выводе под другим номером (перенумерация источника)")
        elif not toc.body_has_title(title):
            rep.warn("SOURCE_DEFECT", f"{num} {title!r} — нет в теле документа (дефект оглавления)")
        else:
            rep.warn("SOURCE_DEFECT", f"{num} {title!r} — в оглавлении, но без номера в теле")

    # PHANTOM_SECTION: раздел вывода, который НЕ подтверждён оглавлением и при
    # этом ДУБЛИРУЕТ номер другого раздела вывода, который оглавлением ПОДТВЕРЖДЁН
    # → мнимый дубль (ложный заголовок из нумерованного списка в прозе).
    # Отличаем от: (а) легитимной коллизии источника — там оба заголовка есть в
    # оглавлении, оба confirmed; (б) простого расхождения заголовка единственного
    # раздела (нет подтверждённого «двойника» того же номера) — это лишь WARN.
    index = TocIndex(toc.entries)
    confirmed_numbers = {s["number"] for s in numbered
                         if index.confirmed(s["number"], s.get("title") or "")}
    for s in numbered:
        num, title = s["number"], s.get("title") or ""
        # номер, который САМО оглавление перечисляет несколько раз (напр. «3.3
        # Подраздел 1/2» при КР16_4), — это дефект нумерации источника, а не
        # фантом из прозы: расхождение заголовка тут лишь WARN (TITLE_MISMATCH).
        if exp_count.get(num, 0) > 1:
            continue
        if num in confirmed_numbers and not index.confirmed(num, title) \
                and not index.title_anywhere(title):
            rep.fail("PHANTOM_SECTION",
                     f"{num} {title[:50]!r} — нет в оглавлении, дублирует подтверждённый раздел {num}")

    # EXTRA / TITLE_MISMATCH — информационно (WARN)
    for num in sorted(act_count, key=_num_key):
        if num not in exp_count:
            rep.warn("EXTRA", f"{num} {act_titles[num][0][:60]!r} — нет в оглавлении")
    for num in exp_count:
        if num in act_count and exp_count[num] == 1 == act_count[num]:
            if not titles_match(exp_titles[num][0], act_titles[num][0]):
                rep.warn("TITLE_MISMATCH",
                         f"{num}: оглавление {exp_titles[num][0][:45]!r} != вывод {act_titles[num][0][:45]!r}")
    return rep


# Пороги порчи для REVIEW. Разрядка/удвоение — редкие события в чистом тексте
# (сильный сигнал), но держим запас, чтобы единичный ложный случай не поднимал
# REVIEW на чистом файле. Глифовая подмена — на OCR при любом заметном объёме.
_COR_SPACING = 5
_COR_DOUBLING = 3
_COR_GLYPH_TOK = 4


def _all_text(doc: dict) -> str:
    """Весь извлечённый текст документа: заголовки+тело всех разделов И все
    регионы-исключения (references/appendices). §-глиф-порча оригинала часто
    сидит ТОЛЬКО в библиографии (КР263_2) — по одному телу разделов её не поймать,
    поэтому знаменатель/числитель плотности считаем по ВСЕМУ тексту."""
    parts: List[str] = []
    for s, _ in walk(doc.get("sections", [])):
        parts.append(s.get("title") or "")
        parts.append(s.get("text") or "")
    for bucket in (doc.get("excluded", {}) or {}).values():
        for item in bucket:
            parts.append(item.get("title", ""))
            parts.append(item.get("text", ""))
    return " ".join(parts)


def _corruption_review(rep: "Report", stats: dict, all_text: str = "") -> None:
    cor = stats.get("corruption", {}) or {}
    sp = int(cor.get("spacing_fixed", 0))
    db = int(cor.get("doubling_fixed", 0))
    gt = int(cor.get("glyph_tokens", 0))
    gr = int(cor.get("glyph_regions", 0))
    # «обратная» глифовая порча (кириллица->ASCII); уже плотностно-отфильтрована
    # парсером (0, если порча не тотальная), поэтому здесь просто добавляем на OCR.
    pa = int(cor.get("pseudo_ascii_tokens", 0))
    # кирилло-латинская глиф-мешанина с впаянным «§» (BCLC->ВСЬС, HBsAg->НВзА§):
    # парсер её НЕ считает — детектируем здесь плотностно по ВСЕМУ тексту документа
    # (включая references). Единичные легит-§ (фамилии/сноски) порог отсекает.
    sig, ss_raw = section_sign_counts(all_text)
    ss = section_sign_glyph_tokens(sig, ss_raw)
    rep.corruption = {"fixable_spacing": sp, "fixable_doubling": db,
                      "needs_ocr": gt + gr + pa + ss}
    if sp >= _COR_SPACING:
        rep.review("CORRUPTION",
                   f"fixable_spacing: разрядка текста, склеено {sp} серий")
    if db >= _COR_DOUBLING:
        rep.review("CORRUPTION",
                   f"fixable_doubling: удвоение букв, схлопнуто {db} токенов")
    if gr >= 1 or gt >= _COR_GLYPH_TOK or pa >= _COR_GLYPH_TOK:
        detail = f"{gt} токенов, {gr} таблиц-регионов"
        if pa:
            detail += f", {pa} псевдо-ASCII токенов (кириллица->ASCII)"
        rep.review("CORRUPTION",
                   f"needs_ocr: глифовая подмена ({detail}) — на OCR")
    if ss:
        rep.review("CORRUPTION",
                   f"needs_ocr: кирилло-латинская глиф-порча с впаянным «§» "
                   f"({ss} токенов из {sig} значимых, "
                   f"{ss / sig * 1000:.1f} на 1000) — на OCR")


def _num_key(num: str):
    return tuple(int(p) for p in num.split(".") if p.isdigit())


def _prefix_words_match(a: str, b: str, k: int = 3) -> bool:
    """Первые k значимых слов заголовков совпадают (тот же раздел, иначе записан)."""
    wa, wb = norm(a).split(), norm(b).split()
    if len(wa) < k or len(wb) < k:
        return False
    return wa[:k] == wb[:k]


# ============================================================================
# CLI
# ============================================================================

def _default_dir() -> str:
    for cand in (os.path.join("tests", "pdfs"),
                 os.path.join("data", "текст_после_чистки"), "data", "."):
        if os.path.isdir(cand) and glob.glob(os.path.join(cand, "*.pdf")):
            return cand
    return "."


def _find_registry() -> Optional[str]:
    for cand in glob.glob(os.path.join("tests", "*.xlsx")) + glob.glob("*.xlsx"):
        return cand
    return None


def _iter_pdfs(path: Optional[str]) -> List[str]:
    if path is None:
        path = _default_dir()
    if os.path.isfile(path) and path.lower().endswith(".pdf"):
        return [path]
    if os.path.isdir(path):
        return sorted(os.path.join(path, n) for n in os.listdir(path)
                      if n.lower().endswith(".pdf"))
    return []


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="validate.py",
        description="Структурная самопроверка вывода парсера КР против оглавления PDF.")
    ap.add_argument("input", nargs="?", default=None,
                    help="PDF-файл или папка с PDF (по умолчанию: tests/pdfs)")
    ap.add_argument("--json", default=None,
                    help="готовый JSON вывода парсера (иначе парсер прогоняется на PDF)")
    ap.add_argument("--registry", default=None, help="путь к Excel-реестру КР")
    ap.add_argument("--quiet", action="store_true", help="скрыть WARN, печатать только FAIL")
    ap.add_argument("--allow-review", action="store_true",
                    help="локальная отладка: не считать REVIEW провалом (по умолчанию "
                         "REVIEW/FAIL/SKIP -> ненулевой exit; PASS — единственный успех)")
    ap.add_argument("--no-toc-cache", action="store_true",
                    help="отключить кэш разбора оглавления (для отладки/сверки)")
    args = ap.parse_args(argv)
    if args.no_toc_cache:
        global _TOC_CACHE_ENABLED
        _TOC_CACHE_ENABLED = False

    pdfs = _iter_pdfs(args.input)
    if not pdfs:
        print(f"Не найдено PDF по пути: {args.input}", file=sys.stderr)
        return 2

    registry = args.registry or _find_registry()
    parser = DocumentParser(create_profile("cr", registry))
    writer = JsonWriter()

    reports: List[Report] = []
    for pdf in pdfs:
        name = os.path.basename(pdf)
        try:
            if args.json and len(pdfs) == 1:
                with open(args.json, encoding="utf-8") as fh:
                    doc = json.load(fh)
            else:
                doc = writer.to_dict(parser.parse(pdf))
            rep = validate_doc(name, doc, parse_toc(pdf))
        except Exception as exc:  # noqa: BLE001
            rep = Report(name)
            rep.fail("CRASH", repr(exc))
        reports.append(rep)

        print(f"\n[{rep.status}] {name}")
        if rep.skipped:
            print(f"    ↷ SKIPPED_SCAN: {rep.skipped}")
        for f in rep.fails:
            print(f"    ✗ {f}")
        for r in rep.reviews:
            print(f"    ⚑ {r}")
        if not args.quiet:
            for w in rep.warns:
                print(f"    · {w}")

    # ПРАВКА 2: честный exit. PASS — единственный успех; REVIEW/FAIL/SCHEMA, а также
    # SKIP при непустом слое — НЕ успех. Три числа печатаем раздельно.
    npass = sum(1 for r in reports if r.status == "PASS")
    nreview = sum(1 for r in reports if r.status == "REVIEW")
    nfail = sum(1 for r in reports if r.status == "FAIL")
    skipped = [r for r in reports if r.status == "SKIP"]
    print("\n" + "=" * 70)
    print(f"ИТОГ ({len(reports)} файлов): PASS={npass}  REVIEW={nreview}  "
          f"FAIL={nfail}  SKIP={len(skipped)}")
    if skipped:
        print("СКАНЫ (не обработаны, на OCR/ручную обработку): "
              + ", ".join(r.name for r in skipped))
    # exit 0 ТОЛЬКО если каждый файл PASS. --allow-review прощает лишь REVIEW.
    if args.allow_review:
        bad = nfail + len(skipped)
    else:
        bad = len(reports) - npass
    print(f"exit {0 if bad == 0 else 1}"
          + ("  (--allow-review: REVIEW не считается провалом)" if args.allow_review else ""))
    print("=" * 70)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
