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

from crparser.engine.parser import DocumentParser
from crparser.engine.jsonio import JsonWriter
from crparser.engine.pdf_reader import PdfReader
from crparser.profiles import create_profile

# Порог покрытия: ниже COV_FAIL — потеря текста (FAIL); ниже COV_WARN — заметка.
COV_FAIL = 99.0
COV_WARN = 99.9

# --- регулярки оглавления ---------------------------------------------------
_LEADER = re.compile(r"\.{3,}|_{3,}")
_TOC_ANCHOR = re.compile(r"^\s*(оглавление|содержание)\s*$", re.IGNORECASE)
_NUM_HEAD = re.compile(r"^(\d+(?:\.\d+)*)\.?\s*(.*)$")
_NUM_RE = re.compile(r"^\d+(\.\d+)*$")
# строка оглавления начинает новый пункт: «1. …», «2.5.2 …» или оторванный
# многосоставный номер на своей строке («2.4.2.2», затем заголовок на следующей).
_TOC_ENTRY_START = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+\S|^\s*\d+(?:\.\d+)+\.?\s*$")

# --- признаки утечки тела в заголовок (инварианты) --------------------------
_TITLE_LIST_MARKER = re.compile(r"[•·●▪‣◦⁃]")
_TITLE_RECO = re.compile(
    r"\b(не\s+)?рекоменд(уется|уются|овано|овани\w*|уем\w*)\b", re.IGNORECASE)
_TITLE_SERVICE = re.compile(
    r"\b(комментари\w*|уровень\s+убедительности|уровень\s+достоверности)\b",
    re.IGNORECASE)
_TITLE_CODE = re.compile(r"\([A-ZА-Я]\d{2}[.\d]+")


# ============================================================================
# Нормализация и сравнение заголовков
# ============================================================================

def norm(text: str) -> str:
    """Нормализация для сравнения: регистр, ё→е, только буквы/цифры/пробел."""
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def titles_match(expected: str, actual: str) -> bool:
    """Терпимо: регистр/пунктуация/перенос/обрезка/леттерспейсинг/частичное."""
    e, a = norm(expected), norm(actual)
    if not e or not a:
        return True
    if e == a or a.startswith(e) or e.startswith(a):
        return True
    # леттерспейсинг и переносы («п р и з ы в у», «г р у д н о г о») — сравниваем
    # вовсе без пробелов, чтобы артефакт вёрстки PDF не выглядел как расхождение
    ec, ac = e.replace(" ", ""), a.replace(" ", "")
    if ec == ac or ac.startswith(ec) or ec.startswith(ac):
        return True
    ew, aw = set(e.split()), set(a.split())
    if not ew or not aw:
        return True
    return len(ew & aw) / len(ew | aw) >= 0.6


# ============================================================================
# Извлечение и разбор оглавления
# ============================================================================

class Toc:
    """Разобранное оглавление + нормализованный текст ТЕЛА (вне оглавления)."""

    def __init__(self, entries: List[Tuple[Optional[str], str]], body_norm: str) -> None:
        self.entries = entries
        self.body_norm = body_norm

    def numbered(self) -> List[Tuple[str, str]]:
        return [(n, t) for n, t in self.entries if n and _NUM_RE.match(n)]

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


def _toc_bounds(lines: List[str]) -> Optional[Tuple[int, int]]:
    """Границы [start, end] региона оглавления по кластеру точек-лидеров."""
    leaders = [i for i, t in enumerate(lines) if _LEADER.search(t)]
    if not leaders:
        return None
    anchor = next((i for i, t in enumerate(lines)
                   if _TOC_ANCHOR.match(t.strip())), None)
    if anchor is not None:
        near = [i for i in leaders if 0 <= i - anchor <= 80]
        start = anchor + 1
    else:
        near = [i for i in leaders if i <= 400]
        start = near[0] if near else None
    if len(near) < 4 or start is None:
        return None
    end = near[0]
    for i in leaders:
        if i < near[0]:
            continue
        if i - end <= 40:           # тот же кластер (учёт пословной вёрстки ToC)
            end = i
        else:
            break
    return start, end


def parse_toc(pdf_path: str) -> Optional[Toc]:
    """Разобрать оглавление; склеить перенос заголовка и оторванный номер."""
    reader = PdfReader(pdf_path)
    try:
        pages = reader.read()
    finally:
        reader.close()
    lines = [ln.text for page in pages for ln in page.lines]

    bounds = _toc_bounds(lines)
    if bounds is None:
        return None
    start, end = bounds
    toc_lines = lines[start:end + 1]
    body_norm = norm(" ".join(lines[:start] + lines[end + 1:]))

    entries: List[Tuple[Optional[str], str]] = []
    buf: List[str] = []

    def flush() -> None:
        full = re.sub(r"\s+", " ", " ".join(buf)).strip()
        buf.clear()
        if not full:
            return
        nm = _NUM_HEAD.match(full)
        if nm and nm.group(1):
            entries.append((nm.group(1), nm.group(2).strip()))
        else:
            entries.append((None, full))

    for line in toc_lines:
        text = line.strip()
        if not text or _TOC_ANCHOR.match(text):
            continue
        if re.fullmatch(r"\d{1,4}", text):     # номер страницы на отдельной строке
            flush()
            continue
        if buf and _TOC_ENTRY_START.match(text):  # начался новый пункт — закрыть прежний
            flush()
        clean, closed = _strip_tail(text)
        if clean:
            buf.append(clean)
        if closed:                              # лидеры/страница в конце строки -> конец пункта
            flush()
    flush()
    return Toc(entries, body_norm)


def _strip_tail(text: str) -> Tuple[str, bool]:
    """Отрезать хвост строки оглавления (точки-лидеры и/или номер страницы)."""
    s = re.sub(r"\s*\.{2,}\s*\d{0,4}\s*$", "", text)   # «....», «.... 46»
    if s != text:
        return s.strip(), True
    s = re.sub(r"\s+\d{1,4}\s*$", "", text)            # « 13» без лидеров
    if s != text and len(text) - len(s) <= 6:
        return s.strip(), True
    return text.strip(), False


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

    def fail(self, kind: str, msg: str) -> None:
        self.fails.append(f"{kind}: {msg}")

    def warn(self, kind: str, msg: str) -> None:
        self.warns.append(f"{kind}: {msg}")

    @property
    def ok(self) -> bool:
        return not self.fails


def validate_doc(name: str, doc: dict, toc: Optional[Toc]) -> Report:
    rep = Report(name)
    sections = doc.get("sections", [])
    stats = doc.get("stats", {})

    nodes = [s for s, _ in walk(sections)]
    parent_of = {id(s): p for s, p in walk(sections)}
    numbered = [s for s in nodes if s.get("number") and _NUM_RE.match(s["number"])]
    act_count = Counter(s["number"] for s in numbered)
    act_titles: Dict[str, List[str]] = defaultdict(list)
    for s in numbered:
        act_titles[s["number"]].append(s.get("title") or "")

    # ---- инвариант 1: покрытие ----
    cov = stats.get("coverage_percent", 0.0)
    if cov <= 0.0:
        rep.warn("COVERAGE", "0% — вероятно скан без текстового слоя")
    elif cov < COV_FAIL:
        rep.fail("COVERAGE", f"{cov}% < {COV_FAIL} — потеря текста")
    elif cov < COV_WARN:
        rep.warn("COVERAGE", f"{cov}% < {COV_WARN}")

    # ---- инвариант 2: чистота заголовков (утечка тела) ----
    for s in nodes:
        title = (s.get("title") or "").strip()
        tag = s.get("number") or ("«" + title[:30] + "…»")
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
    if toc is None:
        rep.warn("TOC", "оглавление не извлечено — проверены только структурные инварианты")
        return rep

    exp = toc.numbered()
    exp_count = Counter(n for n, _ in exp)
    exp_titles: Dict[str, List[str]] = defaultdict(list)
    for n, t in exp:
        exp_titles[n].append(t)

    # дубли в самом оглавлении -> дефект источника
    for num, titles in exp_titles.items():
        uniq = {norm(t) for t in titles if norm(t)}
        if len(titles) > 1 and len(uniq) > 1:
            rep.warn("SOURCE_DEFECT", f"{num}: номер повторяется в оглавлении: {titles!r}")

    # MISSING: каждый пункт оглавления должен быть представлен в выводе
    for num, title in exp:
        matched = any(s["number"] == num and titles_match(title, s.get("title") or "")
                      for s in numbered)
        if matched:
            continue
        renumbered = any(titles_match(title, s.get("title") or "") for s in numbered)
        if toc.body_has_numbered(num, title) and not renumbered:
            # номер стоит рядом с заголовком в ТЕЛЕ, но раздела в выводе нет -> потеря
            rep.fail("MISSING", f"{num} {title!r} — есть в теле, но потерян в выводе")
        elif renumbered:
            rep.warn("SOURCE_DEFECT",
                     f"{num} {title!r} — в выводе под другим номером (перенумерация источника)")
        elif not toc.body_has_title(title):
            rep.warn("SOURCE_DEFECT", f"{num} {title!r} — нет в теле документа (дефект оглавления)")
        else:
            rep.warn("SOURCE_DEFECT", f"{num} {title!r} — в оглавлении, но без номера в теле")

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


def _num_key(num: str):
    return tuple(int(p) for p in num.split(".") if p.isdigit())


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
    args = ap.parse_args(argv)

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

        print(f"\n[{'PASS' if rep.ok else 'FAIL'}] {name}")
        for f in rep.fails:
            print(f"    ✗ {f}")
        if not args.quiet:
            for w in rep.warns:
                print(f"    · {w}")

    npass = sum(1 for r in reports if r.ok)
    print("\n" + "=" * 70)
    print(f"ИТОГ: {npass}/{len(reports)} файлов PASS")
    print("=" * 70)
    return 0 if npass == len(reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
