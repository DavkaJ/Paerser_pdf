#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_cr.py — парсер одной клинической рекомендации (КР) Минздрава из цифрового PDF
в структурированный JSON.

Идея:
  * Текст тянем PyMuPDF (fitz) — он стабильнее на тексте, отдаёт размер/жирность шрифта.
  * Режем документ на разделы по нумерованным заголовкам (1., 1.1, 1.5.1 ...),
    которые крупнее/жирнее основного текста.
  * Оглавление (front matter в начале), список литературы и приложения выносим отдельно,
    в основной текст разделов не тащим.
  * Таблицы пробуем найти pdfplumber.find_tables(), но НЕ разбираем на ячейки —
    сохраняем сырой текстовый дамп + bbox + флаг raw:true.
  * Деградируем мягко: любая ошибка на этапе -> warning в stats, не падаем.

CLI:  python parse_cr.py input.pdf -o output.json
"""

import argparse
import datetime
import json
import os
import re
import sys
import warnings

# pdfminer (под капотом pdfplumber) любит сыпать предупреждениями на "грязных" PDF — глушим.
warnings.filterwarnings("ignore")

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("Нужен PyMuPDF: pip install PyMuPDF")

try:
    import pdfplumber
except ImportError:
    sys.exit("Нужен pdfplumber: pip install pdfplumber")


# ----------------------------------------------------------------------------
# Константы / эвристики
# ----------------------------------------------------------------------------

# Множитель: заголовок считаем "крупным", если его кегль >= BODY * BIG_RATIO.
BIG_RATIO = 1.35

# Жирный флаг в PyMuPDF span["flags"]: бит 4 (значение 16).
FLAG_BOLD = 1 << 4

# Нумерованный заголовок в начале строки: "1.", "1.1", "1.5.1" + текст после.
RE_NUM_HEAD = re.compile(r"^(\d+(?:\.\d+){0,2})\.?\s+\S")
# Извлечь сам номер.
RE_NUM_ONLY = re.compile(r"^(\d+(?:\.\d+){0,2})")

# Маркеры регионов-исключений (именованные заголовки без номера).
RE_REFERENCES = re.compile(r"^\s*Список\s+литературы", re.IGNORECASE)
RE_APPENDIX = re.compile(r"^\s*Приложени[ея]\b", re.IGNORECASE)

# Точки-лидеры в оглавлении ("...."), на случай если они есть в других КР.
RE_DOT_LEADER = re.compile(r"\.{4,}")


# ----------------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------------

def _line_text(line):
    """Склеить текст строки из её спанов."""
    return "".join(s["text"] for s in line["spans"])


def _line_size(line):
    """Максимальный кегль среди спанов строки (заголовок = самый крупный спан)."""
    return max((s["size"] for s in line["spans"]), default=0.0)


def _line_bold(line):
    """Строка жирная, если хотя бы один спан помечен bold."""
    return any(s["flags"] & FLAG_BOLD for s in line["spans"])


def _collapse_ws(text):
    """Схлопнуть лишние пробелы/переводы строк, но сохранить абзацные разрывы."""
    # убираем висячие пробелы по строкам
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    # не больше двух переводов строки подряд
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_body_size(doc, sample_pages=40):
    """
    Определить кегль основного текста как самый "массовый" размер шрифта
    (по числу символов) на первых sample_pages страницах.
    """
    from collections import Counter
    sizes = Counter()
    for pno in range(min(sample_pages, doc.page_count)):
        try:
            d = doc[pno].get_text("dict")
        except Exception:
            continue
        for b in d.get("blocks", []):
            for l in b.get("lines", []):
                for s in l["spans"]:
                    sizes[round(s["size"], 1)] += len(s["text"])
    if not sizes:
        return 12.0  # дефолт, если совсем ничего не нашли
    return sizes.most_common(1)[0][0]


# ----------------------------------------------------------------------------
# Метаданные документа (первая страница)
# ----------------------------------------------------------------------------

# Слова, с которых начинается название организации-разработчика
# (для разбиения блока "Разработчик ..." на отдельные организации).
RE_DEV_STARTER = re.compile(
    r"^(Ассоциац|Общероссийск|Союз|Национальн|Российск|Федерац|"
    r"Межрегиональн|Ассоц|Общество|Фонд)", re.IGNORECASE)


def _parse_developers(lines, start_idx):
    """
    Собрать список организаций-разработчиков, начиная со строки после
    "Разработчик клинической рекомендации" и до "Одобрено ..."/конца.
    Строки-продолжения (перенос названия) приклеиваются к предыдущей организации.
    """
    devs = []
    for ln in lines[start_idx + 1:]:
        if ln.lower().startswith("одобрено"):
            break
        if RE_DEV_STARTER.match(ln) or not devs:
            devs.append(ln)
        else:
            devs[-1] = devs[-1] + " " + ln   # перенос названия на новую строку
    return [d.strip() for d in devs if d.strip()]


def extract_metadata(doc, warnings_list):
    """
    Достать с титульного листа КР всё, что задано шаблоном Минздрава:
    название, год утверждения, год пересмотра, код МКБ, возрастную категорию,
    ID и список разработчиков. Поля стандартизированы ("... : значение"),
    поэтому берём простыми правилами. Чего не нашли — None.

    Возвращает (meta_dict, doc_id_num).
    """
    meta = {
        "title": None,
        "approval_year": None,
        "revision_year": None,
        "icd_10": [],
        "age_category": None,
        "developers": [],
    }
    doc_id_num = None

    try:
        first = doc[0].get_text()
    except Exception as e:
        warnings_list.append(f"не удалось прочитать первую страницу: {e}")
        return meta, doc_id_num

    lines = [ln.strip() for ln in first.splitlines() if ln.strip()]

    # --- Название: строка сразу после "Клинические рекомендации" ---
    for i, ln in enumerate(lines):
        if ln.lower().startswith("клинические рекомендации"):
            if i + 1 < len(lines):
                meta["title"] = lines[i + 1]
            break
    if not meta["title"]:
        # запасной вариант — самая крупная строка первой страницы
        try:
            best = (0.0, None)
            for b in doc[0].get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    t = _line_text(l).strip()
                    if t and not t.lower().startswith("клинические"):
                        sz = _line_size(l)
                        if sz > best[0]:
                            best = (sz, t)
            meta["title"] = best[1]
        except Exception:
            pass

    # --- Год утверждения / пересмотра ---
    m = re.search(r"Год\s+утвержд\w*[^\d]*(\d{4})", first)
    if m:
        meta["approval_year"] = int(m.group(1))
    m = re.search(r"Пересмотр[^\n\d]*(\d{4})", first)
    if m:
        meta["revision_year"] = int(m.group(1))

    # --- Коды МКБ-10 (их может быть несколько!): ---
    # "...связанных со здоровьем:N13.0, N13.1, Q62.0" -> ["N13.0","N13.1","Q62.0"].
    # Берём весь сегмент после "здоровьем:" до "Год утверждения" (устойчиво к
    # переносу строки), затем вытаскиваем все коды формата БукваЧЧ(.Ч).
    m = re.search(r"здоровь\w*\s*:?\s*(.*?)\s*Год\s+утвержд", first, re.S)
    if not m:  # запасной вариант — одна строка
        m = re.search(r"здоровь\w*\s*:?\s*([^\n]+)", first)
    if m:
        # нормализуем кириллические двойники латинских букв, используемых в МКБ
        trans = str.maketrans("СABЕКМНОРТХ", "CABEKMHOPTX")
        seg = m.group(1).translate(trans)
        codes = re.findall(r"[A-Z]\d{2}(?:\.\d+)?", seg)
        # уникализируем, сохраняя порядок
        meta["icd_10"] = list(dict.fromkeys(codes))

    # --- Возрастная категория ---
    m = re.search(r"Возрастная\s+категория\s*:?\s*([^\n]+)", first)
    if m:
        meta["age_category"] = m.group(1).strip()

    # --- ID документа: "ID:12" ---
    m = re.search(r"\bID[:\s]+(\d+)", first)
    if m:
        doc_id_num = m.group(1)

    # --- Разработчики ---
    for i, ln in enumerate(lines):
        if ln.lower().startswith("разработчик"):
            meta["developers"] = _parse_developers(lines, i)
            break

    if meta["title"] is None:
        warnings_list.append("название КР не извлечено (эвристика не сработала)")
    if meta["approval_year"] is None:
        warnings_list.append("год утверждения не извлечён")
    if not meta["icd_10"]:
        warnings_list.append("коды МКБ-10 не извлечены")

    return meta, doc_id_num


# ----------------------------------------------------------------------------
# Таблицы (pdfplumber) — с фильтрацией мусорной детекции
# ----------------------------------------------------------------------------

def extract_tables(pdf_path, warnings_list):
    """
    Найти таблицы через pdfplumber.find_tables().

    ВАЖНО: default line-strategy на этих PDF выдаёт по одной "таблице" на страницу
    с абсурдным bbox (выходит за пределы страницы) и единственной ячейкой — это
    ложные срабатывания на сквозном векторном объекте. Поэтому фильтруем:
      * bbox должен лежать в пределах страницы (с допуском),
      * таблица должна иметь >=2 строк, >=2 столбцов и >=4 непустых ячеек.

    Возвращает список словарей: {page, bbox(top-left), raw_text} + счётчики в warnings.
    """
    tables = []
    junk = 0               # полностраничный геометрический мусор (bbox вне страницы)
    small_inbounds = 0     # bbox в пределах страницы, но структура < 2x2 / < 4 ячеек
    page_tboxes = {}  # page_index -> list of bbox (для вычитания из текста разделов)

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as e:
        warnings_list.append(f"pdfplumber не смог открыть файл: {e}")
        return tables, page_tboxes

    with pdf:
        for pno, page in enumerate(pdf.pages):
            ph = page.height or 0
            pw = page.width or 0
            try:
                found = page.find_tables()
            except Exception as e:
                warnings_list.append(f"find_tables упал на стр. {pno + 1}: {e}")
                continue

            for t in found:
                x0, top, x1, bottom = t.bbox
                # --- санити-чек bbox ---
                in_bounds = (
                    -2 <= top <= ph + 2
                    and -2 <= bottom <= ph + 2
                    and bottom > top
                    and x1 > x0
                )
                # --- структура ячеек ---
                try:
                    grid = t.extract()
                except Exception:
                    grid = []
                nrows = len(grid)
                ncols = max((len(r) for r in grid), default=0)
                nonempty = sum(1 for r in grid for c in r if c and str(c).strip())

                is_real = in_bounds and nrows >= 2 and ncols >= 2 and nonempty >= 4
                if not is_real:
                    if in_bounds:
                        small_inbounds += 1   # мелкая/одностолбцовая — останется в тексте
                    else:
                        junk += 1             # геометрический мусор
                    continue

                # Сырой текстовый дамп области таблицы (без разбора на ячейки).
                raw_text = ""
                try:
                    crop = page.crop((max(0, x0), max(0, top),
                                      min(pw, x1), min(ph, bottom)))
                    raw_text = crop.extract_text() or ""
                except Exception as e:
                    warnings_list.append(
                        f"не удалось снять дамп таблицы на стр. {pno + 1}: {e}")

                bbox = [round(float(x0), 1), round(float(top), 1),
                        round(float(x1), 1), round(float(bottom), 1)]
                tables.append({
                    "page": pno + 1,          # 1-based для человека
                    "page0": pno,             # 0-based для внутренней привязки
                    "bbox": bbox,
                    "raw_text": _collapse_ws(raw_text),
                })
                page_tboxes.setdefault(pno, []).append((x0, top, x1, bottom))

    if junk:
        warnings_list.append(
            f"отфильтровано {junk} полностраничных ложных детекций "
            f"(векторный мусор, bbox вне страницы)")
    if small_inbounds:
        warnings_list.append(
            f"{small_inbounds} мелких/одностолбцовых детекций не сохранены как "
            f"таблицы — их текст остался в прозе раздела/приложения")
    return tables, page_tboxes


# ----------------------------------------------------------------------------
# Чтение текста + нарезка на units (заголовок / абзац)
# ----------------------------------------------------------------------------

def _classify_heading(text, size, bold, body_size):
    """
    Вернуть ('num', number) | ('named', None) | None для строки-кандидата.

    * Крупная строка (>= BODY*BIG_RATIO) — заголовок: нумерованный или именованный.
    * Жирная строка кегля основного текста, начинающаяся с N.M(.K) — под-заголовок.
    * Строки оглавления (кегль основного текста, не жирные, с точками-лидерами) — НЕ заголовок.
    """
    big = size >= body_size * BIG_RATIO
    num_m = RE_NUM_HEAD.match(text)

    if big and len(text) < 200:
        if num_m:
            return ("num", RE_NUM_ONLY.match(text).group(1))
        # именованный заголовок (Список литературы, Приложение, Критерии... и т.п.)
        return ("named", None)

    # под-заголовок на кегле основного текста: жирный, с номером уровня >=2 (N.M)
    if bold and len(text) < 140 and not RE_DOT_LEADER.search(text):
        if re.match(r"^\d+\.\d+(?:\.\d+)?\.?\s+\S", text):
            return ("num", RE_NUM_ONLY.match(text).group(1))
    return None


def build_units(doc, page_tboxes, body_size, warnings_list):
    """
    Пройти по документу и собрать упорядоченный список units:
      {"kind": "heading", "htype": "num"/"named", "number": "3.1"|None,
       "title": ..., "level": int, "page": p, "y": y0}
      {"kind": "text", "text": ..., "page": p}

    Строки, попавшие в bbox реальных таблиц, в текст не включаем (они уже в raw_text).
    """
    units = []

    def covered_by_table(pno, line_bbox):
        """True, если вертикальный центр строки попал в область таблицы на странице."""
        boxes = page_tboxes.get(pno)
        if not boxes:
            return False
        ly = (line_bbox[1] + line_bbox[3]) / 2.0
        lx = (line_bbox[0] + line_bbox[2]) / 2.0
        for (x0, top, x1, bottom) in boxes:
            if top <= ly <= bottom and x0 - 2 <= lx <= x1 + 2:
                return True
        return False

    # Состояние ГЛОБАЛЬНОЕ для всего документа (а не на блок!):
    #   cur_heading живёт между блоками/страницами, чтобы корректно склеивать
    #   заголовки, перенесённые на следующую строку/блок (типичный кейс 24pt-заголовков).
    #   para — на блок (каждый блок = отдельный абзац), чтобы не терять разбивку.
    state = {"heading": None}

    def flush_heading():
        if state["heading"] is not None:
            units.append(state["heading"])
            state["heading"] = None

    for pno in range(doc.page_count):
        try:
            d = doc[pno].get_text("dict")
        except Exception as e:
            warnings_list.append(f"не удалось разобрать текст стр. {pno + 1}: {e}")
            continue

        # блоки в порядке чтения (сверху вниз)
        blocks = sorted(d.get("blocks", []), key=lambda b: b.get("bbox", [0, 0])[1])

        for b in blocks:
            if b.get("type", 0) != 0:   # 0 == текстовый блок
                continue

            para = []   # абзац основного текста этого блока

            def flush_para():
                if para:
                    txt = " ".join(para).strip()
                    if txt:
                        units.append({"kind": "text", "text": txt, "page": pno})
                    para.clear()

            for l in b.get("lines", []):
                text = _line_text(l).strip()
                if not text:
                    continue
                if covered_by_table(pno, l["bbox"]):
                    # этот текст уйдёт в raw_text таблицы — в разделы не тащим
                    continue

                size = _line_size(l)
                bold = _line_bold(l)
                cls = _classify_heading(text, size, bold, body_size)

                if cls is not None:
                    htype, number = cls
                    # Перенос крупного заголовка на следующую строку/блок:
                    # большой НЕнумерованный фрагмент сразу за уже открытым заголовком
                    # (между ними не было основного текста, та же страница) — это
                    # продолжение title. Маркеры регионов (Список литературы,
                    # Приложение) в продолжение НЕ склеиваем — это отдельные блоки.
                    is_marker = RE_REFERENCES.match(text) or RE_APPENDIX.match(text)
                    if (state["heading"] is not None and htype == "named"
                            and not para and not is_marker
                            and state["heading"]["page"] == pno
                            and size >= body_size * BIG_RATIO):
                        state["heading"]["title"] += " " + text
                        continue
                    # новый заголовок
                    flush_para()
                    flush_heading()
                    level = (number.count(".") + 1) if number else 1
                    state["heading"] = {
                        "kind": "heading", "htype": htype, "number": number,
                        "title": text, "level": level,
                        "page": pno, "y": l["bbox"][1],
                    }
                else:
                    # обычный текст -> закрываем заголовок, копим абзац
                    flush_heading()
                    para.append(text)

            flush_para()

    flush_heading()
    return units


# ----------------------------------------------------------------------------
# Нарезка units на разделы и регионы-исключения (конечный автомат)
# ----------------------------------------------------------------------------

# Ненумерованные именованные разделы -> осмысленный section_id.
NAMED_SECTION_MAP = [
    (re.compile(r"список\s+сокращ", re.IGNORECASE), "abbreviations"),
    (re.compile(r"термин\w*\s+и\s+определ", re.IGNORECASE), "terms"),
    (re.compile(r"критери\w*\s+оценки\s+качеств", re.IGNORECASE), "criteria_quality"),
]
RE_TOC_HEAD = re.compile(r"^\s*оглавлен", re.IGNORECASE)


def _named_section_id(title):
    """Вернуть осмысленный id для известного именованного раздела или None."""
    for rx, sid in NAMED_SECTION_MAP:
        if rx.search(title):
            return sid
    return None


def split_sections(units, warnings_list):
    """
    Разложить units по бакетам с помощью конечного автомата.

    "Режимы" для свободного текста (когда нет активного раздела):
      toc  -> титул + оглавление
      refs -> список литературы
      apps -> приложения

    Разделы (sections) формируются как нумерованными заголовками (1, 1.1, 1.5.1),
    так и известными именованными (Список сокращений -> abbreviations,
    Термины и определения -> terms, Критерии оценки качества -> criteria_quality).

    Возвращает (sections, toc_text, refs_text, apps_text).
    """
    sections = []
    toc_parts, refs_parts, apps_parts = [], [], []
    mode = "toc"          # куда идёт свободный текст вне раздела
    cur = None            # текущий раздел
    seen_numbered = False  # уже встречали нумерованный раздел?
    used_ids = set()
    named_fallback = 0

    def title_clean(number, title):
        """Убрать ведущий номер из заголовка нумерованного раздела."""
        if number:
            t = re.sub(r"^\d+(?:\.\d+){0,2}\.?\s*", "", title).strip()
            return t or title
        return title

    def open_section(sec_id, level, title, u):
        nonlocal cur, mode
        # гарантируем уникальность id
        base, k = sec_id, 2
        while sec_id in used_ids:
            sec_id = f"{base}_{k}"
            k += 1
        used_ids.add(sec_id)
        cur = {
            "section_id": sec_id,
            "level": level,
            "title": title,
            "_text_parts": [],
            "page_start": u["page"] + 1,
            "page_end": u["page"] + 1,
            "_y": u["y"],
            "_page0": u["page"],
            "tables": [],
        }
        sections.append(cur)
        mode = "section"

    for u in units:
        if u["kind"] == "heading":
            title = u["title"]

            # --- маркеры регионов-исключений (приоритетнее всего) ---
            if RE_REFERENCES.match(title):
                mode = "refs"; cur = None
                continue
            if RE_APPENDIX.match(title):
                mode = "apps"; cur = None
                apps_parts.append(title)   # заголовки приложений сохраняем
                continue
            if RE_TOC_HEAD.match(title):
                mode = "toc"; cur = None
                continue

            number = u["number"]
            if number is not None:
                seen_numbered = True
                open_section(number, u["level"], title_clean(number, title), u)
            else:
                # ненумерованный именованный заголовок
                sid = _named_section_id(title)
                if sid is not None:
                    open_section(sid, 1, title, u)
                elif not seen_numbered:
                    # неизвестный именованный блок до первого раздела — это титул/шум
                    toc_parts.append(title); cur = None
                else:
                    # неизвестный именованный заголовок в теле — fallback-id
                    named_fallback += 1
                    open_section(f"named{named_fallback}", 1, title, u)

        else:  # текст
            text = u["text"]
            if cur is not None:
                cur["_text_parts"].append(text)
                cur["page_end"] = u["page"] + 1
            elif mode == "refs":
                refs_parts.append(text)
            elif mode == "apps":
                apps_parts.append(text)
            else:  # toc
                toc_parts.append(text)

    if not sections:
        warnings_list.append("не найдено ни одного раздела — "
                             "проверьте структуру PDF / эвристики заголовков")

    toc_text = _collapse_ws("\n".join(toc_parts))
    refs_text = _collapse_ws("\n".join(refs_parts))
    apps_text = _collapse_ws("\n".join(apps_parts))
    return sections, toc_text, refs_text, apps_text


# ----------------------------------------------------------------------------
# Привязка таблиц к разделам
# ----------------------------------------------------------------------------

def attach_tables(sections, tables, warnings_list):
    """
    Каждую реальную таблицу привязать к разделу, активному в её позиции
    (последний раздел, начавшийся не позже страницы/координаты таблицы).
    Таблицы вне разделов (во front/refs/apps) пропускаем с предупреждением.
    """
    if not sections:
        if tables:
            warnings_list.append(f"{len(tables)} таблиц(ы) не привязаны — нет разделов")
        return

    per_section_counter = {}
    dropped = 0

    for tb in tables:
        tp, ttop = tb["page0"], tb["bbox"][1]
        owner = None
        for sec in sections:
            sp, sy = sec["_page0"], sec["_y"]
            # раздел стартовал раньше (по странице) либо на той же странице выше таблицы
            if sp < tp or (sp == tp and sy <= ttop):
                owner = sec
            else:
                break  # секции упорядочены — дальше будут только более поздние
        # таблица не должна выходить за конец раздела-владельца
        if owner is not None and owner["page_end"] >= tb["page"]:
            n = per_section_counter.get(owner["section_id"], 0) + 1
            per_section_counter[owner["section_id"]] = n
            owner["tables"].append({
                "table_id": f"{owner['section_id']}_t{n}",
                "page": tb["page"],
                "raw": True,
                "raw_text": tb["raw_text"],
                "bbox": tb["bbox"],
            })
        else:
            dropped += 1

    if dropped:
        warnings_list.append(
            f"{dropped} таблиц(ы) вне разделов (оглавление/литература/приложения) — пропущены")


# ----------------------------------------------------------------------------
# Сборка результата + статистика
# ----------------------------------------------------------------------------

def total_text_chars(doc):
    """Суммарный объём текста документа (для оценки покрытия)."""
    total = 0
    for pno in range(doc.page_count):
        try:
            total += len(doc[pno].get_text().strip())
        except Exception:
            pass
    return total


def build_output(pdf_path, doc, meta, doc_id_num, sections, excluded, tables, warnings_list):
    base = os.path.basename(pdf_path)
    stem = os.path.splitext(base)[0]
    doc_id = f"КР{doc_id_num}" if doc_id_num else stem

    # финализируем разделы: склеиваем текст, чистим служебные поля
    out_sections = []
    sections_chars = 0
    for sec in sections:
        text = _collapse_ws("\n".join(sec["_text_parts"]))
        sections_chars += len(text)
        out_sections.append({
            "section_id": sec["section_id"],
            "level": sec["level"],
            "title": sec["title"],
            "text": text,
            "page_start": sec["page_start"],
            "page_end": sec["page_end"],
            "tables": sec["tables"],
        })

    toc_text, refs_text, apps_text = excluded
    total = total_text_chars(doc)
    coverage = round(sections_chars / total * 100, 1) if total else 0.0

    # "Учтено где-либо" — проверка, что ничего не потеряли. Засчитываем ВСЁ, что
    # реально попало в JSON: прозу разделов, исключения, сырьё таблиц и заголовки
    # (иначе метрика ложно занижается — таблицы и тайтлы лежат в выводе, не в text).
    excluded_chars = len(toc_text) + len(refs_text) + len(apps_text)
    tables_chars = sum(len(t["raw_text"]) for s in out_sections for t in s["tables"])
    titles_chars = sum(len(s["title"]) for s in out_sections)
    accounted = round(
        (sections_chars + excluded_chars + tables_chars + titles_chars) / total * 100, 1
    ) if total else 0.0

    if coverage < 50:
        warnings_list.append(
            f"низкое покрытие разделами ({coverage}%) — основной текст мог уйти "
            f"в исключения или не распознались заголовки")

    result = {
        "doc_id": doc_id,
        "source_file": base,
        "metadata": {
            "title": meta["title"],
            "approval_year": meta["approval_year"],
            "revision_year": meta["revision_year"],
            "icd_10": meta["icd_10"],
            "age_category": meta["age_category"],
            "developers": meta["developers"],
            "parsed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        },
        "sections": out_sections,
        "excluded": {
            "toc_text": toc_text,
            "references_text": refs_text,
            "appendices_text": apps_text,
        },
        "parse_stats": {
            "sections_found": len(out_sections),
            "tables_found": len(tables),
            "text_coverage_pct": coverage,
            "text_accounted_pct": accounted,
            "warnings": warnings_list,
        },
    }
    return result


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Парсинг клинической рекомендации (PDF) в структурированный JSON.")
    ap.add_argument("input", help="входной PDF клинической рекомендации")
    ap.add_argument("-o", "--output", help="путь к выходному JSON "
                    "(по умолчанию рядом с input, .json)")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"Файл не найден: {args.input}")

    out_path = args.output or (os.path.splitext(args.input)[0] + ".json")
    warnings_list = []

    # 1. Открываем документ для текстового слоя
    try:
        doc = fitz.open(args.input)
    except Exception as e:
        sys.exit(f"PyMuPDF не смог открыть PDF: {e}")

    # 2. Кегль основного текста (база для детекции заголовков)
    body_size = detect_body_size(doc)

    # 3. Метаданные
    meta, doc_id_num = extract_metadata(doc, warnings_list)

    # 4. Таблицы (pdfplumber) — отдельным проходом, с фильтрацией мусора
    tables, page_tboxes = extract_tables(args.input, warnings_list)

    # 5. Текст -> units -> разделы/исключения
    units = build_units(doc, page_tboxes, body_size, warnings_list)
    sections, toc_text, refs_text, apps_text = split_sections(units, warnings_list)

    # 6. Привязка таблиц к разделам
    attach_tables(sections, tables, warnings_list)

    # 7. Сборка результата
    result = build_output(args.input, doc, meta, doc_id_num, sections,
                          (toc_text, refs_text, apps_text), tables, warnings_list)
    doc.close()

    # 8. Запись JSON
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        sys.exit(f"Не удалось записать JSON: {e}")

    # 9. Краткая сводка в консоль
    st = result["parse_stats"]
    md = result["metadata"]
    print(f"✓ {result['doc_id']}  ({result['source_file']})")
    print(f"  Название : {md['title']}")
    print(f"  Год / пересмотр : {md['approval_year']} / {md['revision_year']}")
    print(f"  МКБ-10 / возраст: {', '.join(md['icd_10']) or '—'} / {md['age_category']}")
    print(f"  Разработчиков   : {len(md['developers'])}")
    print(f"  Разделов : {st['sections_found']}")
    print(f"  Таблиц   : {st['tables_found']}")
    print(f"  Покрытие текста разделами : {st['text_coverage_pct']}%")
    print(f"  Учтено всего (разделы+искл): {st['text_accounted_pct']}%")
    if st["warnings"]:
        print(f"  Предупреждения ({len(st['warnings'])}):")
        for w in st["warnings"]:
            print(f"    - {w}")
    print(f"  -> {out_path}")


if __name__ == "__main__":
    main()
