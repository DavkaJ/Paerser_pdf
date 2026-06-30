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
from crparser.engine.toc import norm, titles_match, parse_entries, toc_bounds, TocIndex
from crparser.profiles import create_profile

# Порог покрытия: ниже COV_FAIL — потеря текста (FAIL); ниже COV_WARN — заметка.
COV_FAIL = 99.0
COV_WARN = 99.9

# Якорь оглавления (тот же, что у профиля КР). Парсинг оглавления — общий код в
# crparser.engine.toc (его же использует парсер, чтобы отсеивать фантомы — баг 4).
_TOC_ANCHOR = re.compile(r"^\s*(оглавление|содержание)\s*$", re.IGNORECASE)
_NUM_RE = re.compile(r"^\d+(\.\d+)*$")
# разделы-регионы (список литературы/приложения/критерии), которым источник дал
# номер: парсер держит их в excluded, а не как нумерованный раздел — не «потеря».
_TOC_REF_APP = re.compile(
    r"^\s*(список\s+литератур|приложени|критери\w*\s+оценки\s+качеств)", re.IGNORECASE)

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


def parse_toc(pdf_path: str) -> Optional[Toc]:
    """Прочитать PDF, разобрать оглавление (общий код) и текст тела вне него."""
    reader = PdfReader(pdf_path)
    try:
        pages = reader.read()
    finally:
        reader.close()
    lines = [ln.text for page in pages for ln in page.lines]
    entries = parse_entries(lines, _TOC_ANCHOR)
    if entries is None:
        return None
    # текст тела (вне региона оглавления) — для проверки «номер стоит рядом со
    # своим заголовком в теле» (отличает реальную потерю от перенумерации)
    bounds = toc_bounds(lines, _TOC_ANCHOR)
    start, end = bounds if bounds else (0, -1)
    body_norm = norm(" ".join(lines[:start] + lines[end + 1:]))
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
        self.skipped: Optional[str] = None   # причина SKIPPED_SCAN (скан без текста)

    def fail(self, kind: str, msg: str) -> None:
        self.fails.append(f"{kind}: {msg}")

    def warn(self, kind: str, msg: str) -> None:
        self.warns.append(f"{kind}: {msg}")

    def skip(self, reason: str) -> None:
        self.skipped = reason

    @property
    def ok(self) -> bool:
        return not self.fails and self.skipped is None


def validate_doc(name: str, doc: dict, toc: Optional[Toc]) -> Report:
    rep = Report(name)
    sections = doc.get("sections", [])
    stats = doc.get("stats", {})

    # ---- скан без текстового слоя -> SKIPPED_SCAN (не FAIL, без сверки с TOC) ----
    # PDF-скан даёт пустой/почти пустой извлекаемый текст: total_chars ≈ 0 или
    # coverage == 0 при отсутствии разделов. Такой файл — на OCR/ручную обработку.
    total_chars = stats.get("total_chars", 0) or 0
    cov = stats.get("coverage_percent", 0.0)
    sec_found = stats.get("sections_found", 0)
    if total_chars < 200 or (cov <= 0.0 and sec_found == 0):
        rep.skip(f"текстовый слой пуст (total_chars={total_chars}, "
                 f"coverage={cov}) — скан, на OCR/ручную обработку")
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
        # «Список литературы»/«Приложение…»/«Критерии оценки качества», которым
        # источник дал номер раздела, парсер сохраняет как регион-исключение
        # (references/appendices), а не как нумерованный раздел — это не потеря.
        if _TOC_REF_APP.match(title):
            rep.warn("SOURCE_DEFECT",
                     f"{num} {title!r} — нумерованные ссылки/приложение, сохранены как регион-исключение")
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

        status = "SKIP" if rep.skipped else ("PASS" if rep.ok else "FAIL")
        print(f"\n[{status}] {name}")
        if rep.skipped:
            print(f"    ↷ SKIPPED_SCAN: {rep.skipped}")
        for f in rep.fails:
            print(f"    ✗ {f}")
        if not args.quiet:
            for w in rep.warns:
                print(f"    · {w}")

    skipped = [r for r in reports if r.skipped]
    judged = [r for r in reports if not r.skipped]
    npass = sum(1 for r in judged if r.ok)
    print("\n" + "=" * 70)
    print(f"ИТОГ: {npass}/{len(judged)} файлов PASS"
          + (f"  (+{len(skipped)} SKIPPED_SCAN)" if skipped else ""))
    if skipped:
        print("СКАНЫ (на OCR/ручную обработку): "
              + ", ".join(r.name for r in skipped))
    print("=" * 70)
    return 0 if npass == len(judged) else 1


if __name__ == "__main__":
    raise SystemExit(main())
