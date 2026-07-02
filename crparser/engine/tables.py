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

import pdfplumber

from crparser.engine.models import BBox, Page, Table
from crparser.engine.textnorm import looks_glyph_corrupted, normalize_line

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

        with pdf:
            # перенос несматченной подписи снизу предыдущей страницы (2б)
            carry: Optional[Dict] = None
            for pno, page in enumerate(pdf.pages):
                ph = float(page.height or 0.0)
                pw = float(page.width or 0.0)
                caps = captions_by_page.get(pno + 1, [])
                try:
                    found = page.find_tables()
                except Exception as exc:  # noqa: BLE001
                    warnings_list.append(f"find_tables упал на стр. {pno + 1}: {exc}")
                    carry = None
                    continue

                # 1) вердикт по каждой детекции, сверху вниз (детерминированно)
                dets = []
                for tbl in found:
                    bbox = self._norm(tbl.bbox)
                    grid = self._safe_extract(tbl)
                    dets.append((bbox, grid, self._validate(bbox, grid, pw, ph)))
                dets.sort(key=lambda d: d[0][1])

                # 2) привязка подписи: сначала ОСНОВНОЙ путь (подпись сверху) —
                #    без изменений; затем новые пути (внутри bbox / спасение small)
                page_tables: List[Dict] = []
                used: set = set()
                for bbox, grid, verdict in dets:
                    if verdict == "junk":
                        self.junk_count += 1
                        continue
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
                        "bbox": bbox,
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

                # 4) эмиссия
                for t in page_tables:
                    raw_text = self._dump_text(page, t["bbox"], pw, ph,
                                               warnings_list, pno)
                    tables.append(Table(
                        page=pno + 1,
                        number=t["number"],
                        caption=t["caption"],
                        raw_text=_collapse_ws(raw_text),
                        bbox=tuple(round(v, 1) for v in t["bbox"]),  # type: ignore[arg-type]
                        low_confidence=t["low_conf"],
                    ))
                    self.subtraction_map.setdefault(pno, []).append(t["bbox"])

                # 5) вычислить перенос для следующей страницы
                carry = self._carry_out(caps, used, page_tables, ph)

            # Fallback: ТОЛЬКО если рамочный детектор не нашёл ничего во всём
            # документе — тогда ищем безрамочные таблицы по подписи + колонкам.
            # Так мы не трогаем файлы, где таблицы уже находятся.
            if not tables:
                self._whitespace_fallback(pdf, tables, warnings_list)

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

    # ---- внутренняя кухня ------------------------------------------------

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

    # ---- fallback: безрамочные (whitespace-выровненные) таблицы -----------

    def _whitespace_fallback(self, pdf, tables: List[Table],
                             warnings_list: List[str]) -> None:
        """Найти безрамочные таблицы по подписи «Таблица N» + колоночному
        выравниванию. Вызывается только когда рамочный детектор пуст."""
        for pno, page in enumerate(pdf.pages):
            try:
                words = page.extract_words(use_text_flow=False,
                                           keep_blank_chars=False)
            except Exception:  # noqa: BLE001
                continue
            if not words:
                continue
            rows = _group_rows(words)
            cap_idx = [i for i, r in enumerate(rows)
                       if _RE_TABLE_CAPTION.match(_row_text(r))]
            if not cap_idx:
                continue

            for k, ci in enumerate(cap_idx):
                stop = cap_idx[k + 1] if k + 1 < len(cap_idx) else len(rows)
                table = self._build_fallback_table(rows, ci, stop, pno, page,
                                                    warnings_list)
                if table is not None:
                    tables.append(table)
                    self.fallback_count += 1
                    x0, y0, x1, y1 = table.bbox
                    self.subtraction_map.setdefault(pno, []).append(
                        (x0, y0, x1, y1))

    def _build_fallback_table(self, rows, ci, stop, pno, page,
                              warnings_list) -> Optional[Table]:
        """Собрать одну безрамочную таблицу из региона rows[ci..stop)."""
        m = _RE_TABLE_CAPTION.match(_row_text(rows[ci]))
        number = _norm_table_number(m.group(1)) if m else None

        window, caption_rows = _region_body(rows, ci, stop)
        if len(window) < 3:
            return None

        # колоночное выравнивание: нужны >= 1 устойчивого внутреннего промежутка
        # (>= 2 колонок) на >= 3 строках. Иначе это подпись без реальной таблицы.
        # Для ОБРЕЗКИ берём «терпимые к прозе» реки (strict=False).
        cuts0, rr0 = _column_cuts(window, strict=False)
        if not cuts0 or rr0 < 3:
            return None

        # обрезать регион на первой полноширинной строке прозы/заголовка (текст
        # «течёт» сквозь ВСЕ колоночные реки) — чтобы bbox не съел соседний
        # раздел и его текст не пропал из вывода.
        body_rows = _trim_to_columns(window, cuts0)
        if len(body_rows) < 3:
            return None

        # финальные реки — уже по чистому телу таблицы, со строгим фильтром
        # (отсекает случайные «реки» внутри широкой колонки текста).
        cuts, river_rows = _column_cuts(body_rows, strict=True)
        if not cuts or river_rows < 3:
            return None

        # подпись (может быть многострочной) — для поля caption и проверки порчи
        caption_text = " ".join(_row_text(rows[i]) for i in caption_rows).strip()
        body_text = " ".join(_row_text(r) for r in body_rows)

        # глифовая порча: латиница, отрендеренная битым cmap как кириллица.
        # Такой регион НЕ структурируем и НЕ схлопываем — помечаем на OCR.
        if looks_glyph_corrupted(body_text) or looks_glyph_corrupted(caption_text):
            self.corrupt_count += 1
            sect = f"«{caption_text[:60]}»" if caption_text else f"№{number}"
            warnings_list.append(
                f"font_corruption: подозрение на глифовую порчу — на OCR "
                f"(таблица {sect}, стр. {pno + 1})")
            return None

        raw_text, low_conf = _build_grid_text(body_rows, cuts)
        if not raw_text.strip():
            return None

        bbox = _region_bbox(rows[ci], body_rows)
        return Table(
            page=pno + 1,
            number=number,
            caption=caption_text or None,
            raw_text=_collapse_ws(raw_text),
            bbox=tuple(round(v, 1) for v in bbox),  # type: ignore[arg-type]
            low_confidence=low_conf,
        )


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
