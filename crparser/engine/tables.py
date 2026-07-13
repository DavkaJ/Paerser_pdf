#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Извлечение таблиц по bbox (pdfplumber).

Движок-уровень: ищем таблицы, фильтруем ложные детекции, снимаем сырой текстовый
дамп области (best-effort, без разбора на ячейки), привязываем подпись «Таблица N»
и строим карту bbox для ВЫЧИТАНИЯ табличного текста из прозы разделов.

Основной детектор — границо-ориентированный (`page.find_tables()` ищет векторные
линии/рамки). Когда он не находит НИ ОДНОЙ таблицы во всём документе, включается
fallback-детектор безрамочных (whitespace-выровненных) таблиц: он якорится на
подписи «Таблица N», подтверждает наличие устойчивых колоночных промежутков и
сохраняет регион ОТДЕЛЬНЫМ табличным объектом, чтобы его текст не растворялся в
прозе раздела переплетёнными колонками. Регионы с признаками шрифтовой порчи
(латиница, отрендеренная битым cmap как кириллица) не структурируются, а
помечаются предупреждением «на OCR».
"""

from __future__ import annotations

import re
import statistics
import warnings
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF — дешёвый гейт find_tables по векторным линиям (промпт 09)
import pdfplumber

from crparser.engine.models import BBox, Page, Table
from crparser.engine.textnorm import looks_glyph_corrupted, normalize_line

# --- предфильтр find_tables (промпт 09) -------------------------------------
# find_tables() = 94% CPU pdfplumber (замер: 85 мс/стр). Стратегия по умолчанию —
# `lines`: таблица НЕ находится без ВЕКТОРНЫХ линий. Значит страницу без H+V линий
# можно пропустить БЕЗ потери recall (замер: 0 страниц «нет линий, но таблица есть»
# на 2786 стр). Линии видны из PyMuPDF (`get_drawings`) за 1.4 мс/стр (60× дешевле).
# ВНИМАНИЕ: гарантия recall держится на стратегии `lines`. Любая смена table_settings
# на text/explicit (промпт 15) АННУЛИРУЕТ гейт — перепрогнать recall-замер.
_HLINE_MIN = 10.0   # мин. длина горизонтального сегмента, pt
_VLINE_MIN = 5.0    # мин. длина вертикального сегмента, pt


def _page_line_stats(fpage: "fitz.Page") -> Tuple[int, int]:
    """(#горизонтальных, #вертикальных) векторных линий-сегментов страницы.
    Считаем и явные линии («l»), и стороны прямоугольников («re»). При сбое —
    (большие числа): не гейтить (безопасно — прогнать find_tables как раньше)."""
    h = v = 0
    try:
        for d in fpage.get_drawings():
            for it in d.get("items", []):
                op = it[0]
                if op == "l":
                    p1, p2 = it[1], it[2]
                    if abs(p1.y - p2.y) < 1.0 and abs(p1.x - p2.x) >= _HLINE_MIN:
                        h += 1
                    elif abs(p1.x - p2.x) < 1.0 and abs(p1.y - p2.y) >= _VLINE_MIN:
                        v += 1
                elif op == "re":
                    r = it[1]
                    if r.width >= _HLINE_MIN:
                        h += 1
                    if r.height >= _VLINE_MIN:
                        v += 1
    except Exception:  # noqa: BLE001 — при сбое гейт не срабатывает
        return 999, 999
    return h, v


def _page_has_table_lines(fpage: "fitz.Page") -> bool:
    """Есть ли на странице рамка-кандидат: >=1 горизонтальная И >=1 вертикальная
    линия. Только такие страницы имеет смысл гонять через find_tables (стратегия
    `lines`); остальные детерминированно вернули бы пусто."""
    h, v = _page_line_stats(fpage)
    return h >= 1 and v >= 1


# --- дедуп ансамбля по ВЛОЖЕННОСТИ (промпт 09) -------------------------------
# find_tables эмитит и всю таблицу, и её подрегион (ячейку/строку) как отдельные
# таблицы. Замер: 251 из 252 table↔table-пар — «small-in-big» с IoU<0.5 (у вложенной
# IoU мал: площадь_малой/площадь_большой). Дедуп — по ВЛОЖЕННОСТИ (доля меньшего,
# накрытая пересечением), а НЕ по IoU. Порядок: junk-фильтр -> ПОТОМ дедуп (иначе
# «предпочесть большую» выбрало бы ложную полностраничную детекцию).
_CONTAIN_MIN = 0.70   # кандидат накрыт лучшим на >= 70% своей площади -> подрегион
# приоритет вердикта при дедупе: «ok»-таблицу НЕ снимаем ради «small»-детекции, даже
# если та крупнее (иначе потеряли бы реальную таблицу внутри рыхлой большой детекции).
_VERDICT_RANK = {"ok": 2, "small": 1, "junk": 0}


def _bbox_area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _covered_by(a: BBox, b: BBox) -> float:
    """Доля площади a, накрытая пересечением с b (0..1). Направленно: «насколько a
    сидит внутри b»."""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    aa = _bbox_area(a)
    return inter / aa if aa > 0 else 0.0


# --- reconcile (промпт 09): A=сетка pdfplumber vs B=IR-спаны -----------------
# Пороги (ПРАВКА 3): рамочные — 0.80, безрамочные — 0.90. Вычитание ТОЛЬКО при
# score >= порога И непустом raw_text.
_RECONCILE_MIN = 0.80
_RECONCILE_MIN_LOWCONF = 0.90
_RE_TOKEN_STRIP = "«».,;:()[]{}\"'`-—–…%№*|/\\"


def _norm_token(tok: str) -> str:
    """casefold, ё->е, снять пунктуацию по краям — для сравнения токенов A/B."""
    return tok.strip(_RE_TOKEN_STRIP).casefold().replace("ё", "е")


def _tokenize(text: str) -> "Counter":
    from collections import Counter
    out = Counter()
    for tok in (text or "").split():
        t = _norm_token(tok)
        if t:
            out[t] += 1
    return out


def _grid_tokens(grid: List[List]) -> "Counter":
    """Мультимножество токенов сетки pdfplumber (A)."""
    from collections import Counter
    out = Counter()
    for row in grid or []:
        for cell in row:
            if cell:
                out.update(_tokenize(str(cell)))
    return out


def _reconcile_score(b_tokens: "Counter", a_tokens: "Counter") -> float:
    """|tokens(B) ∩ tokens(A)| / |tokens(B)| (по мультимножеству). Пусто в B -> 1.0
    (нечего сохранять). Падает, когда A НЕ захватила часть текста B (усечение/
    overcapture-проза)."""
    total = sum(b_tokens.values())
    if total == 0:
        return 1.0
    inter = sum(min(b_tokens[t], a_tokens.get(t, 0)) for t in b_tokens)
    return inter / total


def _grid_metrics(grid: List[List]) -> "tuple[int, int, float]":
    """(row_count, cell_count непустых, empty_cell_ratio) по сетке A."""
    rows = [r for r in (grid or []) if r]
    nrow = len(rows)
    total = sum(len(r) for r in rows)
    nonempty = sum(1 for r in rows for c in r if c and str(c).strip())
    empty_ratio = (total - nonempty) / total if total else 0.0
    return nrow, nonempty, round(empty_ratio, 3)


def _lines_in_bbox(page: Page, bbox: BBox) -> List:
    """IR-строки (Line), чей ЦЕНТР лежит в bbox (та же геометрия, что вычитание
    сегментера _covered_by_table). Это множество B для reconcile и источник raw_text."""
    x0, y0, x1, y1 = bbox
    out = []
    for ln in page.lines:
        lx0, ly0, lx1, ly1 = ln.bbox
        cx, cy = (lx0 + lx1) / 2.0, (ly0 + ly1) / 2.0
        if x0 - 2 <= cx <= x1 + 2 and y0 <= cy <= y1:
            out.append(ln)
    return out


def _col_cuts_from_cells(tbl) -> List[float]:
    """Внутренние вертикальные границы колонок из ячеек рамочной таблицы pdfplumber.
    Пусто -> нет надёжной сетки (raw_text-from-IR упадёт на построчную раскладку)."""
    try:
        xs = sorted({round(c[0], 1) for c in tbl.cells}
                    | {round(c[2], 1) for c in tbl.cells})
    except Exception:  # noqa: BLE001
        return []
    return xs[1:-1] if len(xs) >= 3 else []


def _raw_text_from_ir(b_lines: List, col_cuts: List[float]) -> "tuple[str, int]":
    """raw_text из IR-строк B, разложенных по сетке (ПРАВКА 4). Строки группируем в
    визуальные ряды по y; в ряду каждую строку кладём в колонку по x-центру
    относительно col_cuts (границы из A). Так текст берётся из IR (восстановленный,
    когда font-repair 13 наполнит спаны), а СТРУКТУРА — из геометрии A. Возвращает
    (raw_text, row_count_ir). Пустой col_cuts -> одна колонка (построчно)."""
    if not b_lines:
        return "", 0
    # визуальные ряды: группировка по y0 (та же логика, что _visual_rows, но на Line)
    rows: List[List] = []
    for ln in sorted(b_lines, key=lambda l: (round(l.bbox[1], 1), l.bbox[0])):
        placed = False
        for r in rows:
            if abs(r[0].bbox[1] - ln.bbox[1]) <= _ROW_Y_TOL:
                r.append(ln)
                placed = True
                break
        if not placed:
            rows.append([ln])
    ncol = len(col_cuts) + 1
    out_lines: List[str] = []
    for row in rows:
        cells: Dict[int, List[str]] = {}
        for ln in sorted(row, key=lambda l: l.bbox[0]):
            cx = (ln.bbox[0] + ln.bbox[2]) / 2.0
            col = sum(1 for c in col_cuts if cx > c)
            cells.setdefault(col, []).append(ln.text.strip())
        parts = [" ".join(cells.get(k, [])) for k in range(ncol)]
        line = "\t".join(parts).rstrip()
        if line.strip():
            out_lines.append(line)
    return "\n".join(out_lines), len(out_lines)

# pdfminer (под капотом pdfplumber) шумит на «грязных» PDF — глушим.
warnings.filterwarnings("ignore")

# Подпись таблицы. Номер — ЗАКРЫТОЕ множество форм (не «любое слово после Таблица»):
#   • обычный «3», «3.1» (запятая как точка), приложенческий «П1», OCR «ПЗ»→«П3»;
#   • приложенческий БУКВЕННЫЙ префикс «ПА3-1», «ПГ-2»: П + 1-2 ЗАГЛАВНЫЕ кир.
#     буквы + опц. цифра + дефис + ОБЯЗАТЕЛЬНОЕ число (без числа не матчим);
#   • слэш-суффикс приложения «1/А2»: номер + «/» + заглавная кир. буква + цифры,
#     захватывается ЦЕЛИКОМ (иначе «/А2» уезжал в заголовок, а number обрезался).
# `(?-i:…)` держит буквы приложения строго заглавными даже под IGNORECASE (само
# слово «Таблица» остаётся регистронезависимым). Разрядку внутри дефисной/слэш-
# формы схлопывает _norm_table_number (её якорит дефис/слэш, а не пробел).
_RE_TABLE_CAPTION = re.compile(
    r"^\s*Таблица\s+("
    r"П(?-i:[А-ЯЁ]){1,2}\d?\s*-\s*\d+"            # ПА3-1, ПГ-2
    r"|\d+\s*/\s*(?-i:[А-ЯЁ])\d*"                  # 1/А2
    r"|П?[\dЗз]+(?:[.,][\dЗз]+)*(?:\s+[\dЗз]\b)*"  # N, N.N, ПN (+ разрядка «1 0»)
    r")\.?\s*(.*)$",
    re.IGNORECASE)


def _norm_table_number(num: str) -> str:
    """«ПЗ» → «П3» (OCR кир. З → цифра 3); запятые → точки; разрядку ВНУТРИ
    номера схлопываем («1 0» → «10», «ПА3 - 1» → «ПА3-1»): пробел внутри уже
    выделенной группы номера — это letterspacing, а не разделитель. Пробелы
    трогаем только в самой группе номера, не по всей строке.

    З→3 применяем ТОЛЬКО к простому цифровому/«ПN»-номеру: в буквенной (ПА3-1) и
    слэш-форме (1/А2) буквы — часть номера приложения и цифрой не подменяются."""
    num = re.sub(r"\s+", "", num)
    if "-" in num or "/" in num:
        return num.replace(",", ".")
    return num.replace("З", "3").replace("з", "3").replace(",", ".")


def _collapse_ws(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --- пересборка визуальных строк подписи (разрядка дробит строку в PyMuPDF) ---
# Порог «та же базовая линия»: АБСОЛЮТНЫЙ, в pt (как ytol в _group_rows).
# Фрагменты разряженной строки делят почти один y0 (доли pt); разные строки
# отстоят на кегль (~13pt) и выше. Порог НЕ привязан к высоте фрагмента: у
# повёрнутых ячеек-заголовков таблиц высота огромна (h~110pt), и доля высоты
# затянула бы в «строку» подпись с соседними ячейками (регрессия КР687_3).
_ROW_Y_TOL = 4.0
# Перенос заголовка подписи: макс. вертикальный зазор (доля высоты строки) и
# порог «многоколоночности» — большой внутренний горизонтальный разрыв выдаёт
# строку ТЕЛА таблицы (колонки), а не перенос сплошного заголовка.
_WRAP_MAX_VGAP_FRAC = 0.9
_WRAP_MAX_COL_GAP = 30.0
_WRAP_MAX_LINES = 3
# нумерованный заголовок раздела («3.1 Название») — не тянем в подпись как перенос
_RE_NUM_HEAD = re.compile(r"^\d{1,2}(?:\.\d{1,3}){0,4}\.?\s+\S")

# --- привязка подписи к таблице -------------------------------------------
# Основное окно «подпись над таблицей» (низ подписи не ниже верха таблицы + люфт,
# зазор до неё в пределах). НЕ менять — это поведение по умолчанию.
_CAP_ABOVE_GAP = 120.0
_CAP_ABOVE_SLACK = 10.0
# (2а) подпись ВНУТРИ слитого bbox таблицы у её верха: верх подписи в пределах
# этого окна от верха таблицы (берём самую верхнюю). Ловит подпись, «зашитую»
# в одну детекцию на всю страницу (проза+подпись+тело).
_CAP_INSIDE_WINDOW = 130.0
# (2в) спасение «small»-детекции ТОЛЬКО под подписью ВПЛОТНУЮ сверху.
_CAP_RESCUE_GAP = 30.0
# (2б) перенос подписи через границу страницы: целевая таблица — самая верхняя
# на след. странице и её верх близок к верху страницы; подпись — в нижней части
# своей страницы и под ней на её странице таблиц нет.
_CARRY_TARGET_TOP_MAX = 150.0
_CARRY_CAPTION_MIN_FRAC = 0.5


def _visual_rows(lines) -> List[Dict]:
    """Сгруппировать Line страницы в ВИЗУАЛЬНЫЕ строки (одна базовая линия).

    Разрядка заставляет PyMuPDF дробить строку на несколько Line с почти
    одинаковым y — собираем их обратно. Возвращает список dict, отсортированный
    по y0: text (фрагменты, склеенные по x одним пробелом), bbox, nfrag (сколько
    Line собрано), y0/y1, maxgap (макс. горизонтальный разрыв между соседними
    фрагментами — отличает сплошной текст от колонок таблицы)."""
    groups: List[Dict] = []
    for ln in sorted(lines, key=lambda l: (l.bbox[1], l.bbox[0])):
        y0, y1 = ln.bbox[1], ln.bbox[3]
        row = None
        for r in groups:
            if abs(r["y0"] - y0) <= _ROW_Y_TOL:
                row = r
                break
        if row is None:
            groups.append({"y0": y0, "y1": y1, "frags": [ln]})
        else:
            row["frags"].append(ln)
            row["y0"] = min(row["y0"], y0)
            row["y1"] = max(row["y1"], y1)
    out: List[Dict] = []
    for r in groups:
        frags = sorted(r["frags"], key=lambda l: l.bbox[0])
        text = re.sub(
            r"\s+", " ",
            " ".join(f.text.strip() for f in frags if f.text.strip())).strip()
        gaps = [frags[k + 1].bbox[0] - frags[k].bbox[2]
                for k in range(len(frags) - 1)]
        out.append({
            "text": text,
            "bbox": (min(f.bbox[0] for f in frags), r["y0"],
                     max(f.bbox[2] for f in frags), r["y1"]),
            "nfrag": len(frags),
            "y0": r["y0"], "y1": r["y1"],
            "maxgap": max(gaps) if gaps else 0.0,
        })
    out.sort(key=lambda x: x["y0"])
    return out


def _glue_caption_wrap(caption: str, rows: List[Dict], i: int) -> str:
    """Подклеить к подписи её «оторванный» перенос заголовка из следующих строк.

    Клеим строку ТОЛЬКО когда она примыкает по вертикали (малый зазор), НЕ
    является новой подписью/номером страницы/нумерованным заголовком и НЕ
    многоколоночная (большой внутренний разрыв = строка тела таблицы)."""
    cap_row = rows[i]
    lh = max(1.0, cap_row["y1"] - cap_row["y0"])
    prev = cap_row
    for j in range(i + 1, min(len(rows), i + 1 + _WRAP_MAX_LINES)):
        nxt = rows[j]
        vgap = nxt["y0"] - prev["y1"]
        if vgap < 0 or vgap > _WRAP_MAX_VGAP_FRAC * lh:
            break
        t = nxt["text"]
        if (not t or _RE_TABLE_CAPTION.match(t) or t.isdigit()
                or _RE_NUM_HEAD.match(t) or nxt["maxgap"] > _WRAP_MAX_COL_GAP):
            break
        caption = (caption + " " + t).strip()
        prev = nxt
    return caption


class TableExtractor:
    """
    Находит таблицы через pdfplumber и валидирует их.

    Ложные детекции отсекаем по двум правилам (объединённый опыт обоих парсеров):
      * bbox должен лежать в пределах страницы (иначе это сквозной векторный мусор);
      * структура >= 2x2 и >= 4 непустых ячеек (иначе это абзац/линия, не таблица).
    Валидные таблицы сохраняем сырыми; их bbox отдаём для вычитания из текста.

    Если рамочный детектор не нашёл НИЧЕГО — включается fallback по пробельному
    выравниванию (см. модуль docstring).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        #: карта page_index(0-based) -> список bbox валидных таблиц (для вычитания)
        self.subtraction_map: Dict[int, List[BBox]] = {}
        #: счётчики отфильтрованного для warnings
        self.junk_count = 0
        self.small_count = 0
        #: сколько таблиц добавил fallback и сколько регионов помечено на OCR
        self.fallback_count = 0
        self.corrupt_count = 0
        #: сколько страниц пропущено предфильтром find_tables (09, диагностика)
        self.gated_pages = 0
        #: сколько вложенных подрегионов снято дедупом (09)
        self.dedup_count = 0
        #: reconcile-диагностика (09): огрызки-шапки и непрошедшие reconcile
        self.stub_count = 0
        self.unreconciled_count = 0

    def extract(self, pages: List[Page], warnings_list: List[str]) -> List[Table]:
        """
        Вернуть список валидных таблиц. `pages` нужны для привязки подписей
        (строки с layout из PdfReader), `warnings_list` — для мягкой деградации.
        """
        tables: List[Table] = []
        try:
            pdf = pdfplumber.open(self._path)
        except Exception as exc:  # noqa: BLE001
            warnings_list.append(f"pdfplumber не смог открыть файл: {exc}")
            return tables

        captions_by_page = self._collect_captions(pages)
        # PyMuPDF-документ для дешёвого гейта find_tables по векторным линиям (09).
        try:
            fdoc = fitz.open(self._path)
        except Exception:  # noqa: BLE001 — без гейта работаем как раньше (все страницы)
            fdoc = None

        with pdf:
            # перенос несматченной подписи снизу предыдущей страницы (2б)
            carry: Optional[Dict] = None
            # номер таблицы, доходящей до низа прошлой страницы (кандидат на
            # продолжение вверху этой) — ПРАВКА 5, многостраничные таблицы
            cont_number: Optional[str] = None
            for pno, page in enumerate(pdf.pages):
                ph = float(page.height or 0.0)
                pw = float(page.width or 0.0)
                caps = captions_by_page.get(pno + 1, [])
                # ПРЕДФИЛЬТР (09): страница без H+V векторных линий не может нести
                # рамочную таблицу (стратегия lines) — find_tables там дал бы пусто.
                # Пропускаем её без запуска тяжёлого детектора (−65% времени, 0 потерь).
                if fdoc is not None and pno < fdoc.page_count \
                        and not _page_has_table_lines(fdoc[pno]):
                    self.gated_pages += 1
                    found = []
                else:
                    try:
                        found = page.find_tables()
                    except Exception as exc:  # noqa: BLE001
                        warnings_list.append(f"find_tables упал на стр. {pno + 1}: {exc}")
                        carry = None
                        continue

                # 1) вердикт по каждой детекции (tbl хранится — нужен для сетки колонок)
                dets = []
                for tbl in found:
                    bbox = self._norm(tbl.bbox)
                    grid = self._safe_extract(tbl)
                    dets.append((bbox, grid, self._validate(bbox, grid, pw, ph), tbl))
                # ПОРЯДОК (09): junk-фильтр ПЕРВЫМ — полностраничный/out-of-bounds
                # мусор выбрасываем ДО дедупа, иначе «предпочесть большую» могло бы
                # выбрать ложную полностраничную детекцию вместо настоящей меньшей.
                survivors = []
                for d in dets:
                    if d[2] == "junk":
                        self.junk_count += 1
                    else:
                        survivors.append(d)
                # затем дедуп по ВЛОЖЕННОСТИ среди выживших (small-in-big -> убрать
                # small, оставить большую рамочную с полной сеткой)
                _before = len(survivors)
                survivors = self._dedup_contained(survivors)
                self.dedup_count += _before - len(survivors)
                survivors.sort(key=lambda d: d[0][1])

                # 2) привязка подписи: сначала ОСНОВНОЙ путь (подпись сверху) —
                #    без изменений; затем новые пути (внутри bbox / спасение small)
                page_tables: List[Dict] = []
                used: set = set()
                for bbox, grid, verdict, tbl in survivors:
                    low_conf = False
                    if verdict == "small":
                        # (2в) спасаем ТОЛЬКО под подписью вплотную сверху
                        cap = self._pick_above(bbox, caps, _CAP_RESCUE_GAP, used)
                        if cap is None or not self._rescue_shape(grid):
                            self.small_count += 1
                            continue
                        low_conf = True
                    else:
                        cap = self._pick_above(bbox, caps, _CAP_ABOVE_GAP, None)
                        if cap is None:
                            cap = self._pick_inside(bbox, caps, used)  # (2а)
                    if cap is not None:
                        used.add(id(cap))
                    page_tables.append({
                        "bbox": bbox, "grid": grid, "tbl": tbl,
                        "number": cap["number"] if cap else None,
                        "caption": cap["caption"] if cap else None,
                        "low_conf": low_conf,
                    })

                # 3) (2б) перенос подписи с прошлой страницы — к САМОЙ ВЕРХНЕЙ
                #    таблице этой страницы, если та без подписи и стоит у верха
                if carry is not None and page_tables:
                    top = min(page_tables, key=lambda t: t["bbox"][1])
                    if top["number"] is None \
                            and top["bbox"][1] <= _CARRY_TARGET_TOP_MAX:
                        top["number"] = carry["number"]
                        top["caption"] = carry["caption"]

                # 3b) ПРАВКА 1: безрамочные кандидаты ТОЙ ЖЕ страницы (по подписи +
                #     колонкам). Гейт по наличию подписи (whitespace-детектор всё равно
                #     якорится на «Таблица N»). Слить с рамочными; безрамочный, что
                #     перекрывает рамочную, отбрасываем (у рамочной достовернее сетка).
                if caps:
                    for wc in self._whitespace_candidates(page, pno, warnings_list):
                        if any(_covered_by(wc["bbox"], ft["bbox"]) >= 0.5
                               or _covered_by(ft["bbox"], wc["bbox"]) >= 0.5
                               for ft in page_tables):
                            continue
                        page_tables.append(wc)
                    page_tables.sort(key=lambda t: t["bbox"][1])

                # 3c) ПРАВКА 5: продолжение таблицы с прошлой страницы (вверху, без
                #     подписи). Связываем через continues_table, строки не теряем.
                if cont_number is not None:
                    cont = self._top_continuation(page, pno, warnings_list)
                    if cont is not None and not any(
                            _covered_by(cont["bbox"], ft["bbox"]) >= 0.5
                            for ft in page_tables):
                        cont["continues"] = cont_number
                        page_tables.append(cont)
                        page_tables.sort(key=lambda t: t["bbox"][1])

                # 4) эмиссия с reconcile (09): текст raw_text из IR-спанов B по сетке A,
                #    reconcile_score = |B∩A|/|B|, вычитание ТОЛЬКО у reconciled-таблиц.
                ir_page = next((p for p in pages if p.number == pno + 1), None)
                for t in page_tables:
                    self._emit_table(t, ir_page, page, pw, ph, warnings_list, pno,
                                     tables)

                # 5) перенос подписи + кандидат-продолжение для следующей страницы:
                #    таблица, доходящая до низа (bbox снизу >= 0.80*высоты).
                carry = self._carry_out(caps, used, page_tables, ph)
                cont_number = None
                if page_tables and ph > 0:
                    low = max(page_tables, key=lambda t: t["bbox"][3])
                    if low["bbox"][3] >= ph * 0.80:
                        cont_number = low.get("number") or low.get("continues")

        if fdoc is not None:
            fdoc.close()

        if self.junk_count:
            warnings_list.append(
                f"отфильтровано {self.junk_count} полностраничных ложных детекций "
                f"(векторный мусор, bbox вне страницы)")
        if self.small_count:
            warnings_list.append(
                f"{self.small_count} мелких/одностолбцовых детекций не сохранены "
                f"как таблицы — их текст остался в прозе раздела")
        if self.fallback_count:
            warnings_list.append(
                f"найдено {self.fallback_count} безрамочных таблиц по пробельному "
                f"выравниванию (fallback-детектор)")
        return tables

    # ---- эмиссия одной таблицы с reconcile (промпт 09) -------------------

    def _emit_table(self, t: Dict, ir_page, ppage, pw: float, ph: float,
                    warnings_list: List[str], pno: int, tables: List[Table]) -> None:
        """Собрать Table: raw_text из IR-спанов B по сетке A; reconcile_score=|B∩A|/|B|;
        вычитание (subtraction_map + claimed_span_uids) ТОЛЬКО если reconciled.
        reconciled = score>=порог И raw_text непуст И это не огрызок-шапка (TABLE_STUB)."""
        bbox = t["bbox"]
        grid = t["grid"]
        b_lines = _lines_in_bbox(ir_page, bbox) if ir_page is not None else []
        a_tokens = _grid_tokens(grid)
        b_tokens = _tokenize(" ".join(ln.text for ln in b_lines))
        score = _reconcile_score(b_tokens, a_tokens)
        col_cuts = (_col_cuts_from_cells(t["tbl"]) if t.get("tbl") is not None
                    else t.get("col_cuts", []))
        raw_text, _ = _raw_text_from_ir(b_lines, col_cuts)
        if not raw_text.strip():
            # IR-строк нет (полоса не выровнена / full-OCR) -> дамп pdfplumber
            raw_text = _collapse_ws(self._dump_text(ppage, bbox, pw, ph,
                                                    warnings_list, pno))
        row_count, cell_count, empty_ratio = _grid_metrics(grid)
        # (Р2) ВТОРОЙ сторож: подпись «Таблица N» + row_count<=1 -> огрызок шапки,
        # reconcile тут слеп (крошечный bbox), вычитания нет НЕЗАВИСИМО от score.
        is_stub = t["number"] is not None and row_count <= 1
        threshold = _RECONCILE_MIN_LOWCONF if t["low_conf"] else _RECONCILE_MIN
        # ИНВАРИАНТ: пустой raw_text -> вычитания нет никогда (ни при каком score).
        reconciled = (score >= threshold) and bool(raw_text.strip()) and not is_stub
        if is_stub:
            self.stub_count += 1
            warnings_list.append(
                "TABLE_STUB: «Таблица %s» стр.%d row_count=%d — огрызок шапки, "
                "вычитания нет" % (t["number"], pno + 1, row_count))
        elif not reconciled:
            self.unreconciled_count += 1
            warnings_list.append(
                "reconcile не прошёл: «Таблица %s» стр.%d score=%.2f<%.2f — текст "
                "оставлен в теле" % (t["number"] or "?", pno + 1, score, threshold))
        # claimed_span_uids — ТОЛЬКО у reconciled (они и вычитаются). Так claimed ==
        # subtracted, а raw_text строится из тех же B -> символы вычтенного ВСЕГДА в
        # raw_text (инвариант E2 выполнен по построению).
        claimed = [ln.span_uid for ln in b_lines] if reconciled else []
        tables.append(Table(
            page=pno + 1, number=t["number"], caption=t["caption"],
            raw_text=raw_text,
            bbox=tuple(round(v, 1) for v in bbox),  # type: ignore[arg-type]
            low_confidence=t["low_conf"],
            claimed_span_uids=claimed, source="native",
            reconciled=reconciled, reconcile_score=round(score, 3),
            continues_table=t.get("continues"),
            row_count=row_count, cell_count=cell_count, empty_cell_ratio=empty_ratio,
        ))
        if reconciled:
            self.subtraction_map.setdefault(pno, []).append(bbox)

    # ---- внутренняя кухня ------------------------------------------------

    @staticmethod
    def _dedup_contained(dets: List) -> List:
        """Снять вложенные подрегионы. Кандидатов фиксируем в порядке (лучший вердикт,
        затем бо́льшая площадь); кандидата, чья площадь на >= _CONTAIN_MIN накрыта уже
        принятым (== он сидит внутри лучшего-или-равного), отбрасываем. Так «ok»-таблицу
        не снимает «small»-детекция, даже если та крупнее (junk уже отфильтрован)."""
        order = sorted(dets, key=lambda d: (-_VERDICT_RANK.get(d[2], 0),
                                            -_bbox_area(d[0])))
        kept: List = []
        for d in order:
            if any(_covered_by(d[0], k[0]) >= _CONTAIN_MIN for k in kept):
                continue
            kept.append(d)
        return kept

    @staticmethod
    def _norm(bbox) -> BBox:
        x0, top, x1, bottom = bbox
        return (float(x0), float(top), float(x1), float(bottom))

    @staticmethod
    def _safe_extract(tbl) -> List[List]:
        try:
            return tbl.extract() or []
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _validate(bbox: BBox, grid: List[List], pw: float, ph: float) -> str:
        """Вернуть 'ok' | 'junk' (bbox вне страницы) | 'small' (структура < 2x2)."""
        x0, top, x1, bottom = bbox
        in_bounds = (
            -2 <= top <= ph + 2
            and -2 <= bottom <= ph + 2
            and bottom > top
            and x1 > x0
            and x0 >= -2
            and x1 <= pw + 2
        )
        if not in_bounds:
            return "junk"
        nrows = len(grid)
        ncols = max((len(r) for r in grid), default=0)
        nonempty = sum(1 for r in grid for c in r if c and str(c).strip())
        if nrows >= 2 and ncols >= 2 and nonempty >= 4:
            return "ok"
        return "small"

    @staticmethod
    def _dump_text(page, bbox: BBox, pw: float, ph: float,
                   warnings_list: List[str], pno: int) -> str:
        x0, top, x1, bottom = bbox
        try:
            crop = page.crop((max(0, x0), max(0, top), min(pw, x1), min(ph, bottom)))
            return crop.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            warnings_list.append(f"не удалось снять дамп таблицы на стр. {pno + 1}: {exc}")
            return ""

    @staticmethod
    def _collect_captions(pages: List[Page]) -> Dict[int, List[Dict]]:
        """Найти строки-подписи «Таблица N ...» на каждой странице.

        При сплошной разрядке PyMuPDF дробит строку подписи на несколько Line с
        одной базовой линией — сначала пересобираем визуальные строки (склейка
        фрагментов по y), затем матчим подпись по СОБРАННОЙ строке. Если подпись
        сама пришла разбитой (>1 фрагмента) и её заголовок «оторван» в следующую
        визуальную строку — аккуратно подклеиваем перенос."""
        out: Dict[int, List[Dict]] = {}
        for page in pages:
            rows = _visual_rows(page.lines)
            caps = []
            for i, row in enumerate(rows):
                text = row["text"]
                m = _RE_TABLE_CAPTION.match(text)
                if not m:
                    # подпись могла оказаться не в начале строки (слева —
                    # колонтитул/номер страницы на той же базовой линии): пробуем
                    # от фрагмента «Таблица…». Ищем ЗАГЛАВНОЕ «Таблица» — прозаичные
                    # ссылки «в таблице/таблицы N» (строчные) подписью не считаем.
                    idx = text.find("Таблица")
                    if idx <= 0:
                        continue
                    text = text[idx:]
                    m = _RE_TABLE_CAPTION.match(text)
                    if not m:
                        continue
                caption = text.strip()
                # заголовок подписи мог «оторваться» в следующие строки ТОЛЬКО
                # когда сама подпись пришла разбитой разрядкой (>1 фрагмента);
                # чистую однострочную подпись не трогаем — поведение как раньше.
                if row["nfrag"] > 1:
                    caption = _glue_caption_wrap(caption, rows, i)
                caps.append({
                    "number": _norm_table_number(m.group(1)),
                    "caption": caption,
                    "bbox": row["bbox"],
                })
            if caps:
                out[page.number] = caps
        return out

    @staticmethod
    def _pick_above(table_bbox: BBox, captions: List[Dict], max_gap: float,
                    used: Optional[set]) -> Optional[Dict]:
        """Подпись СВЕРХУ таблицы — ближайшая, низ не ниже верха таблицы (+люфт),
        зазор в пределах max_gap. Это исходное поведение привязки (не менять).
        `used` (если задан) исключает уже занятые подписи — только для новых
        путей (спасение small); основной путь передаёт used=None."""
        ty0 = table_bbox[1]
        cand = [c for c in captions
                if (used is None or id(c) not in used)
                and c["bbox"][3] <= ty0 + _CAP_ABOVE_SLACK
                and ty0 - c["bbox"][3] <= max_gap]
        if not cand:
            return None
        return min(cand, key=lambda c: abs(ty0 - c["bbox"][3]))

    @staticmethod
    def _pick_inside(table_bbox: BBox, captions: List[Dict],
                     used: set) -> Optional[Dict]:
        """(2а) Подпись ВНУТРИ bbox таблицы у её верха — когда над таблицей
        кандидатов нет (слитая детекция на всю страницу: проза+подпись+тело).
        Верх подписи в окне от верха таблицы, сама подпись в пределах таблицы;
        берём САМУЮ ВЕРХНЮЮ. Подпись глубоко внутри (вторая на странице) не в
        окне — остаётся в raw_text."""
        ty0, ty1 = table_bbox[1], table_bbox[3]
        cand = [c for c in captions
                if id(c) not in used
                and ty0 - _CAP_ABOVE_SLACK <= c["bbox"][1] <= ty0 + _CAP_INSIDE_WINDOW
                and c["bbox"][3] <= ty1]
        if not cand:
            return None
        return min(cand, key=lambda c: c["bbox"][1])

    @staticmethod
    def _rescue_shape(grid: List[List]) -> bool:
        """(2в) Разрешить спасение «small»-детекции: >=2 строк, >=1 колонки,
        >=2 непустых ячейки. Ослабление ncols действует ТОЛЬКО под якорем-подписью
        вплотную сверху (см. вызов), без подписи фильтры не трогаем."""
        nrows = len(grid)
        ncols = max((len(r) for r in grid), default=0)
        nonempty = sum(1 for r in grid for c in r if c and str(c).strip())
        return nrows >= 2 and ncols >= 1 and nonempty >= 2

    @staticmethod
    def _carry_out(captions: List[Dict], used: set, page_tables: List[Dict],
                   ph: float) -> Optional[Dict]:
        """(2б) Несматченная подпись в НИЖНЕЙ части страницы, под которой на этой
        странице таблиц нет и которая не лежит ВНУТРИ какой-либо таблицы, —
        кандидат на перенос к самой верхней таблице следующей страницы. Берём
        самую нижнюю такую подпись (ближе всех к границе страницы)."""
        cand = []
        for c in captions:
            if id(c) in used:
                continue
            ctop, cbot = c["bbox"][1], c["bbox"][3]
            if ctop < _CARRY_CAPTION_MIN_FRAC * ph:
                continue
            inside = any(t["bbox"][1] <= ctop <= t["bbox"][3] for t in page_tables)
            below = any(t["bbox"][1] >= cbot - _CAP_ABOVE_SLACK for t in page_tables)
            if inside or below:
                continue
            cand.append(c)
        if not cand:
            return None
        return max(cand, key=lambda c: c["bbox"][1])

    # ---- безрамочные (whitespace-выровненные) кандидаты, ПЕР-СТРАНИЧНО (09) ---

    def _whitespace_candidates(self, page, pno: int,
                               warnings_list: List[str]) -> List[Dict]:
        """Кандидаты безрамочных таблиц ОДНОЙ страницы (по подписи «Таблица N» +
        колонкам). Возвращает dict'ы для общего пути эмиссии (_emit_table): у них
        сетка A из pdfplumber-слов и col_cuts — reconcile применяется как к рамочным.
        Раньше это был документный fallback под `if not tables` (терял безрамочную
        на странице с рамочной); теперь — часть пер-страничного ансамбля."""
        try:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        except Exception:  # noqa: BLE001
            return []
        if not words:
            return []
        rows = _group_rows(words)
        cap_idx = [i for i, r in enumerate(rows)
                   if _RE_TABLE_CAPTION.match(_row_text(r))]
        if not cap_idx:
            return []
        out: List[Dict] = []
        for k, ci in enumerate(cap_idx):
            stop = cap_idx[k + 1] if k + 1 < len(cap_idx) else len(rows)
            cand = self._build_whitespace_candidate(rows, ci, stop, pno, warnings_list)
            if cand is not None:
                out.append(cand)
        return out

    def _top_continuation(self, page, pno: int,
                          warnings_list: List[str]) -> Optional[Dict]:
        """Кандидат-ПРОДОЛЖЕНИЕ таблицы у ВЕРХА страницы, БЕЗ подписи (ПРАВКА 5):
        колоночная структура, начинается у верхнего края, до первой подписи. Строки
        продолжения многостраничной таблицы иначе теряются (КР1_4 Table 2 стр.64)."""
        try:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        except Exception:  # noqa: BLE001
            return None
        if not words:
            return None
        rows = _group_rows(words)
        if not rows or rows[0]["top"] > _CARRY_TARGET_TOP_MAX:
            return None                       # содержимое не у верха -> не продолжение
        stop = next((i for i, r in enumerate(rows)
                     if _RE_TABLE_CAPTION.match(_row_text(r))), len(rows))
        window = rows[:stop]
        if len(window) < 3:
            return None
        # продолжение — весь связный верхний регион до следующей подписи, обрезанный
        # лишь на БОЛЬШОМ вертикальном разрыве (конец таблицы). НЕ применяем строгий
        # _trim_to_columns: у таблиц с широкими многострочными ячейками (режимы КР1_4)
        # он рубит регион на первой же «полноширинной» строке и теряет строки.
        tops = [r["top"] for r in window]
        diffs = [b - a for a, b in zip(tops, tops[1:]) if b - a > 0]
        pitch = statistics.median(diffs) if diffs else 14.0
        body_rows = [window[0]]
        for r in window[1:]:
            if r["top"] - body_rows[-1]["top"] > pitch * 4.0:
                break
            if _is_section_head_row(r):
                break            # заголовок раздела -> конец продолжения таблицы
            body_rows.append(r)
        if len(body_rows) < 3:
            return None
        cuts, rr = _column_cuts(body_rows, strict=True)
        if not cuts or rr < 3:
            return None
        if looks_glyph_corrupted(" ".join(_row_text(r) for r in body_rows)):
            self.corrupt_count += 1
            return None
        ncol = len(cuts) + 1
        grid = [[" ".join(_assign_columns(r, cuts).get(c, [])) for c in range(ncol)]
                for r in body_rows]
        if not _looks_like_real_table(grid):   # ужесточение: не проза/список/библио
            return None
        bbox = (min(r["x0"] for r in body_rows), min(r["top"] for r in body_rows),
                max(r["x1"] for r in body_rows), max(r["bottom"] for r in body_rows))
        return {"bbox": tuple(round(v, 1) for v in bbox), "grid": grid, "tbl": None,
                "col_cuts": cuts, "number": None, "caption": None, "low_conf": True}

    def _build_whitespace_candidate(self, rows, ci, stop, pno,
                                    warnings_list) -> Optional[Dict]:
        """Собрать один безрамочный кандидат из региона rows[ci..stop) как dict со
        своей сеткой A (list-of-lists) и col_cuts — для reconcile в _emit_table."""
        m = _RE_TABLE_CAPTION.match(_row_text(rows[ci]))
        number = _norm_table_number(m.group(1)) if m else None
        window, caption_rows = _region_body(rows, ci, stop)
        if len(window) < 3:
            return None
        cuts0, rr0 = _column_cuts(window, strict=False)
        if not cuts0 or rr0 < 3:
            return None
        body_rows = _trim_to_columns(window, cuts0)
        # ДОП. ЗАЩИТА (09): не тянуть регион ЧЕРЕЗ заголовок подраздела (2.2/3.5) —
        # это конец таблицы. _trim_to_columns рубит только если заголовок ПЕРЕСЕКАЕТ
        # реки; короткий заголовок в левой колонке проскакивал, и whitespace-регион
        # съедал подразделы (регресс 28 MISSING). Рубим на ЛЮБОМ заголовке подраздела.
        hcut = next((i for i, r in enumerate(body_rows)
                     if _is_section_head_row(r)), None)
        if hcut is not None:
            body_rows = body_rows[:hcut]
        if len(body_rows) < 3:
            return None
        cuts, river_rows = _column_cuts(body_rows, strict=True)
        if not cuts or river_rows < 3:
            return None
        caption_text = " ".join(_row_text(rows[i]) for i in caption_rows).strip()
        body_text = " ".join(_row_text(r) for r in body_rows)
        # глифовая порча: латиница, отрендеренная битым cmap как кириллица — НЕ
        # структурируем, помечаем на OCR (как раньше).
        if looks_glyph_corrupted(body_text) or looks_glyph_corrupted(caption_text):
            self.corrupt_count += 1
            sect = f"«{caption_text[:60]}»" if caption_text else f"№{number}"
            warnings_list.append(
                f"font_corruption: подозрение на глифовую порчу — на OCR "
                f"(таблица {sect}, стр. {pno + 1})")
            return None
        # сетка A (list-of-lists) для reconcile/метрик — слова pdfplumber по колонкам
        ncol = len(cuts) + 1
        grid = [[" ".join(_assign_columns(r, cuts).get(c, [])) for c in range(ncol)]
                for r in body_rows]
        if not _looks_like_real_table(grid):   # ужесточение: не проза/список/библио
            return None
        self.fallback_count += 1
        bbox = _region_bbox(rows[ci], body_rows)
        return {"bbox": tuple(round(v, 1) for v in bbox), "grid": grid, "tbl": None,
                "col_cuts": cuts, "number": number,
                "caption": caption_text or None, "low_conf": True}


# ====================================================================== #
#  Помощники fallback-детектора (модульного уровня, чистые функции)        #
# ====================================================================== #

# минимальный межсловный промежуток, считающийся колоночным (а не пробелом
# внутри ячейки). Пробел внутри ячейки ~3-6pt, колоночная река >= ~15pt.
_MIN_COL_GAP = 13.0
# поля при проверке «накрыто ли слово точкой x» (склейка дробящихся глифов)
_PAD = 1.5
# минимальная ширина устойчивой реки, pt (уже отсекает случайные щели)
_MIN_RIVER = 6.0


def _row_text(row) -> str:
    return " ".join(w["text"] for w in row["words"])


def _group_rows(words, ytol: float = 4.0):
    """Сгруппировать слова в визуальные строки по координате top."""
    rows: List[Dict] = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        placed = False
        for r in rows:
            if abs(r["top"] - w["top"]) <= ytol:
                r["words"].append(w)
                r["top"] = min(r["top"], w["top"])
                placed = True
                break
        if not placed:
            rows.append({"top": float(w["top"]), "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])
        r["bottom"] = max(float(w["bottom"]) for w in r["words"])
        r["x0"] = min(float(w["x0"]) for w in r["words"])
        r["x1"] = max(float(w["x1"]) for w in r["words"])
    rows.sort(key=lambda r: r["top"])
    return rows


def _region_body(rows, ci, stop):
    """Собрать строки-кандидаты тела таблицы: после подписи до явного конца.

    Возвращает (window_rows, caption_row_indices). «Окно» намеренно щедрое (может
    содержать хвост прозы) — точную нижнюю границу таблицы задаёт позже
    `_trim_to_columns`. Здесь обрываемся лишь на грубых сигналах: следующая
    подпись (stop), строка-номер страницы, ОЧЕНЬ большой вертикальный разрыв,
    либо предохранитель в 80 строк.
    """
    tops = [rows[i]["top"] for i in range(ci, min(stop, len(rows)))]
    diffs = [b - a for a, b in zip(tops, tops[1:]) if b - a > 0]
    pitch = statistics.median(diffs) if diffs else 14.0

    caption_idx = [ci]
    window: List[Dict] = []
    started = False
    prev_top = rows[ci]["top"]
    for i in range(ci + 1, min(stop, len(rows))):
        r = rows[i]
        gap = r["top"] - prev_top
        # очень большой разрыв после начала таблицы — конец окна
        if started and gap > pitch * 3.0:
            break
        txt = _row_text(r).strip()
        # одинокий номер страницы внизу — конец. Но ТОЛЬКО когда он оторван
        # большим разрывом (футер): иначе это код-ячейка шкалы (УДД/УУР), где
        # «2»/«3» нередко стоят отдельной строкой внутри таблицы.
        if started and re.fullmatch(r"\d{1,4}", txt) and gap > pitch * 2.0:
            break
        # многострочное продолжение подписи (до первой «колоночной» строки)
        if not started and _has_col_gap(r):
            started = True
        if not started:
            # ещё часть подписи?
            if i - ci <= 3 and not _has_col_gap(r):
                caption_idx.append(i)
                prev_top = r["top"]
                continue
            started = True
        window.append(r)
        prev_top = r["top"]
        if len(window) >= 80:
            break
    return window, caption_idx


def _has_col_gap(row) -> bool:
    """Есть ли в строке хотя бы один межсловный промежуток >= _MIN_COL_GAP."""
    ws = row["words"]
    for a, b in zip(ws, ws[1:]):
        if b["x0"] - a["x1"] >= _MIN_COL_GAP:
            return True
    return False


def _row_crosses(row, cut: float, pad: float = 2.0) -> bool:
    """Текст строки «течёт» сквозь рез cut: есть слова по обе стороны И нет
    межсловного промежутка, накрывающего рез (т.е. сплошная проза/заголовок)."""
    ws = row["words"]
    has_left = any(w["x1"] <= cut for w in ws)
    has_right = any(w["x0"] >= cut for w in ws)
    if not (has_left and has_right):
        return False
    # слово физически пересекает рез — однозначно «течёт»
    for w in ws:
        if w["x0"] < cut - pad and w["x1"] > cut + pad:
            return True
    # есть ли реальный промежуток, накрывающий рез?
    for a, b in zip(ws, ws[1:]):
        if a["x1"] <= cut <= b["x0"] and (b["x0"] - a["x1"]) >= 6.0:
            return False
    return True


# строка-заголовок подраздела: ДОТИРОВАННЫЙ номер («1.6», «3.2») + заглавная.
# Только дотированный — одиночный «1.» бывает строкой-критерием внутри таблицы
# («1. Количество дефекаций»), а «1.6»/«3.2» табличной ячейкой не бывает.
_RE_SECTION_HEAD = re.compile(r"^\s*\d{1,2}(?:\.\d{1,3})+\.?\s+[А-ЯЁ]")


def _looks_like_heading(text: str) -> bool:
    return bool(_RE_SECTION_HEAD.match(text))


# заголовок подраздела: ДОТИРОВАННЫЙ номер + ЗАГЛАВНАЯ (кир/лат — «1.6.2 AL-амилоидоз»
# начинается с латинской). Доза «1.5 мг» — строчная «м», не матчит.
_RE_SUBSEC_HEAD = re.compile(r"^\s*\d{1,2}(?:\.\d{1,3})+\.?\s+[А-ЯЁA-Z]")


_TABLE_KEY_CELL_MAX = 20   # средняя длина ячеек «ключевой» колонки таблицы, симв


def _looks_like_real_table(grid: List[List]) -> bool:
    """Ужесточение безрамочного детектора (09): регион — ТАБЛИЦА, а не абзац прозы.
    Признак — есть КОЛОНКА КОРОТКИХ ячеек (ключ/метка: №, уровень «Да/Нет»/«C», имя
    препарата): у таблицы всегда есть узкая колонка-ключ. У прозы (в т.ч. разбитой
    ложной «рекой» на 2 колонки текстовых фрагментов) короткой колонки-ключа нет —
    все колонки длинные. Критерии качества с ПЕРЕНОСАМИ длинных ячеек проходят (у них
    колонка № коротка). >=4 непустых строк. Замер: 896 whitespace-таблиц -> меньше,
    абзацы прозы отсеяны, критерии/препараты/шкалы целы."""
    rows = [r for r in grid if any(str(c).strip() for c in r)]
    if len(rows) < 4:
        return False
    ncol = max((len(r) for r in rows), default=0)
    for i in range(ncol):
        cells = [str(r[i]).strip() for r in rows if i < len(r) and str(r[i]).strip()]
        if len(cells) >= 3 and sum(len(c) for c in cells) / len(cells) < _TABLE_KEY_CELL_MAX:
            return True                       # есть узкая колонка-ключ -> таблица
    return False


def _is_section_head_row(row) -> bool:
    """Строка-РЯД — заголовок ПОДРАЗДЕЛА (конец таблицы для whitespace-региона):
    ДОТИРОВАННЫЙ номер + заглавная («2.2 Физикальное», «1.6.2 AL-амилоидоз»). Только
    дотированный: одиночный номер неотличим от табличной ячейки-ряда («1 Тремелимумаб**
    300 мг…» vs глава «2 Диагностика…») — риск срезать строки таблицы (КР1_4 Table 2)
    выше пользы. Редкий случай главы под whitespace-таблицей (КР524_3/714_2) остаётся
    как структурный REVIEW, контент сохранён в raw_text (не E2)."""
    return bool(_RE_SUBSEC_HEAD.match(_row_text(row).strip()))


def _trim_to_columns(window, cuts) -> List[Dict]:
    """Оставить начальный непрерывный участок строк, согласованных с колонками.

    Регион обрываем на строке, которая ЛИБО «течёт» сквозь ВСЕ реки (сплошная
    проза), ЛИБО выглядит как заголовок подраздела (дотированный номер) и при
    этом полноширинна (пересекает реку) — чтобы bbox не накрыл соседний раздел
    и его текст не пропал из вывода.
    """
    out: List[Dict] = []
    for r in window:
        if _looks_like_heading(_row_text(r)) and cuts and \
                any(_row_crosses(r, c) for c in cuts):
            break
        if cuts and all(_row_crosses(r, c) for c in cuts):
            break
        out.append(r)
    return out


def _column_cuts(body_rows, strict: bool = True) -> Tuple[List[float], int]:
    """Найти устойчивые вертикальные реки методом x-покрытия.

    Река — вертикальная полоса x, которую на >= 3 строках «раскалывают» слова
    слева и справа при белом поле в самой точке. Метод устойчив к примеси прозы
    в окне (проза накрывает x сплошняком и просто не голосует за реку): счёт
    ведём по АБСОЛЮТНОМУ числу расколотых строк.

    strict=True добавляет условие «белое поле держится почти на всех строках»
    (>= 0.6n) — это отсекает случайные «реки» внутри широкой колонки текста и
    применяется для финальной сетки. strict=False (только split>=3) терпим к
    прозе в окне и используется для обрезки региона `_trim_to_columns`.

    Возвращает (cut_x_positions, max_support).
    """
    n = len(body_rows)
    if n < 3:
        return [], 0
    x0 = min(r["x0"] for r in body_rows)
    x1 = max(r["x1"] for r in body_rows)
    if x1 - x0 < 20.0:
        return [], 0

    rowspans = [sorted((w["x0"], w["x1"]) for w in r["words"]) for r in body_rows]
    step = 2.0
    nx = int((x1 - x0) / step) + 1
    split = [0] * nx  # строк, реально «расколотых» в точке x (слова слева и справа)
    white = [0] * nx  # строк с белым полем в точке x (слово не накрывает x)
    for i in range(nx):
        x = x0 + i * step
        sp = wh = 0
        for spans in rowspans:
            left = right = covered = False
            for a, b in spans:
                if b < x:
                    left = True
                if a > x:
                    right = True
                if a - _PAD <= x <= b + _PAD:
                    covered = True
                    break
            if not covered:
                wh += 1
                if left and right:
                    sp += 1
        split[i] = sp
        white[i] = wh

    floor = max(3, int(round(n * 0.6)))
    river = [1 if split[i] >= 3 and (not strict or white[i] >= floor) else 0
             for i in range(nx)]
    cuts: List[float] = []
    support = 0
    i = 0
    while i < nx:
        if river[i]:
            j = i
            while j < nx and river[j]:
                j += 1
            if (j - i) * step >= _MIN_RIVER:
                cuts.append(x0 + ((i + j) // 2) * step)
                support = max(support, max(split[i:j]))
            i = j
        else:
            i += 1
    return cuts, support


def _cell_text(words: List[str]) -> str:
    """Текст ячейки: чиним разрядку/удвоение (глифовую порчу сюда не пускаем —
    такой регион отсекается раньше)."""
    text, _, _ = normalize_line(" ".join(words))
    return text


def _assign_columns(row, cuts) -> Dict[int, List[str]]:
    """Разложить слова строки по колонкам относительно границ cuts."""
    cells: Dict[int, List[str]] = {}
    for w in row["words"]:
        cx = (w["x0"] + w["x1"]) / 2.0
        col = sum(1 for c in cuts if cx > c)
        cells.setdefault(col, []).append(w["text"])
    return cells


def _build_grid_text(body_rows, cuts) -> Tuple[str, bool]:
    """Собрать текст таблицы по ячейкам (слова не рвём — группируем по колонкам).

    Строки, попавшие в одну колонку и являющиеся переносом ячейки сверху,
    подклеиваем к предыдущей строке. Возвращает (raw_text, low_confidence).
    low_confidence=True, когда колонки выделяются ненадёжно (много слов сидит
    «верхом» на реке) — текст всё равно сохраняем, но честно помечаем.
    """
    ncol = len(cuts) + 1
    straddle = 0
    total_words = 0
    out_lines: List[str] = []
    prev_cells: Optional[Dict[int, List[str]]] = None

    for r in body_rows:
        cells = _assign_columns(r, cuts)
        # подсчёт «верховых» слов (слово, чьё тело пересекает рез) — индикатор
        # ненадёжного разбора
        for w in r["words"]:
            total_words += 1
            for c in cuts:
                if w["x0"] < c - 1 < w["x1"] or w["x0"] < c + 1 < w["x1"]:
                    straddle += 1
                    break

        # строка-продолжение: занят ровно один столбец и НЕ первый (текстовый
        # перенос ячейки) — приклеиваем к соответствующей ячейке прошлой строки
        if (prev_cells is not None and len(cells) == 1
                and 0 not in cells and out_lines):
            col = next(iter(cells))
            frag = _cell_text(cells[col])
            out_lines[-1] = _append_frag(out_lines[-1], frag, col, ncol)
            continue

        parts = []
        for col in range(ncol):
            parts.append(_cell_text(cells.get(col, [])))
        out_lines.append("\t".join(parts).rstrip())
        prev_cells = cells

    low_conf = total_words > 0 and straddle / total_words > 0.12
    return "\n".join(ln for ln in out_lines if ln.strip()), low_conf


def _append_frag(line: str, frag: str, col: int, ncol: int) -> str:
    """Дописать фрагмент-перенос в нужную колонку строки (таб-разделители)."""
    cols = line.split("\t")
    while len(cols) < ncol:
        cols.append("")
    if col < len(cols):
        cols[col] = (cols[col] + " " + frag).strip() if cols[col] else frag
    return "\t".join(cols).rstrip()


def _region_bbox(caption_row, body_rows) -> BBox:
    """bbox, охватывающий подпись и тело таблицы (для вычитания из прозы)."""
    xs0 = [caption_row["x0"]] + [r["x0"] for r in body_rows]
    xs1 = [caption_row["x1"]] + [r["x1"] for r in body_rows]
    top = caption_row["top"]
    bottom = max(r["bottom"] for r in body_rows)
    return (min(xs0), top, max(xs1), bottom)


def _looks_font_corrupted(text: str) -> bool:
    """Эвристика шрифтовой порчи: латиница, отрендеренная кириллическими
    глифами (битый cmap). Признаки — смешение латиницы и кириллицы внутри слов
    ИЛИ кириллические токены с биграммами, типичными для такой подмены."""
    toks = text.split()
    if not toks:
        return False
    mixed = len(_RE_MIXED.findall(text))
    suspect = 0
    for t in toks:
        s = t.strip(".,;:()[]«»\"'-—%<>")
        if len(s) >= 4 and re.fullmatch(r"[А-Яа-яЁё]+", s):
            low = s.lower()
            if sum(1 for bg in _CORRUPT_BIGRAMS if bg in low) >= 1:
                suspect += 1
    ratio = suspect / max(1, len(toks))
    # порча: либо явное смешение скриптов в нескольких словах, либо >=2
    # подозрительных кириллических токенов с заметной плотностью
    return mixed >= 3 or (suspect >= 2 and ratio >= 0.03)
