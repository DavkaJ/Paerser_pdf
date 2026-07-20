# -*- coding: utf-8 -*-
"""ШАГ 6 (промпт 17): МИНИМИЗАЦИЯ очереди верификации -> готовность к Label Studio.

Вход  : _corpus/verify_queue/tasks.json (needs_review + неразрешённые; auto НЕ в очереди).
Выход : _corpus/verify_queue/tasks_min.json     — дедуплицировано, шум отсеян, крит-первыми
        _corpus/verify_queue/_noise.jsonl        — легит-формы (не терять, но не человеку)
        _corpus/verify_queue/crops_min/          — 3 уровня кропа (слово 600DPI/строка/страница)
        + доклад точных чисел на stdout.

Минимизация (по плану):
  1. auto уже вне очереди (в очереди только needs_review/unresolved) — проверяем.
  2. ДЕДУП по форме: (source_text, entity_kind, набор кандидатов) -> ОДНА задача; разные
     кандидаты той же формы -> НЕ схлопываем (разбиваем). Копим occurrences + документы.
  3. ОТСЕВ ШУМА -> _noise.jsonl: номера страниц (цифры), одиночные служебные буквы,
     реальные рус. слова (pymorphy), единицы (CO2/pCO2/ОФВ1…), фрагменты рус. стоп-слов.
  4. СОРТИРОВКА: критические (icd/atc/tnm/dose/gene) первыми, затем термины; внутри — по
     числу вхождений (частые формы выгоднее размечать первыми).
  5. КРОПЫ: слово (600 DPI, узкий bbox) / строка (широкий по X) / страница (bbox подсвечен).

    python _corpus/build_labeling_queue.py [tasks.json] [--no-crops]
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from crparser.engine import rumorph

QDIR = os.path.join("_corpus", "verify_queue")
RAW = os.path.join("data", "raw")
CRIT_KINDS = {"icd", "atc", "tnm", "dose", "drug", "gene"}

_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
# Единицы/легит-формы, которые скринер/детектор ловит как «смешанные», но это НОРМА.
_UNIT_RE = re.compile(
    r"^(?:p?[CСO О]?[CСO О]?[O О]?2?|СО2|CO2|O2|О2|pCO2|pO2|SpO2|SaO2|pH|рН|"
    r"ОФВ\d?|ФЖЕЛ|ЖЕЛ|ПСВ|СОЭ|АД|ЧСС|ЧДД|Hb|HbA1c|IgG|IgM|IgA|IgE|рT\d|мЗв)$",
    re.IGNORECASE)
# Русские служебные слова/предлоги — фрагменты, бесполезные человеку по одному.
_RU_STOP1 = set("и в с а к у о я й б ь ы е р н т м д л г з х ц ч ш щ ж ф п".split())


def _norm_src(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _all_digits(s: str) -> bool:
    core = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", s)
    return bool(core) and core.isdigit()


def is_noise(t: dict) -> str:
    """Причина, по которой задача — ШУМ (не человеку), или '' если реальная задача."""
    src = _norm_src(t.get("source_text", ""))
    core = src.strip(".,;:()[]«»\"'`-—/ ")
    if not core:
        return "empty"
    if _all_digits(src):
        return "page_number_or_count"
    letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", core)
    if len(letters) <= 1:
        return "single_letter"
    if _UNIT_RE.match(core):
        return "unit/legit_form"
    # фрагмент из одних русских стоп-букв/слов (`с и`, `и К`, `З р`)
    toks = [w.strip(".,;:()[]") for w in src.split()]
    toks = [w for w in toks if w]
    if toks and all(len(w) <= 1 or w.lower() in _RU_STOP1 for w in toks):
        return "ru_stopword_fragment"
    # реальное русское слово целиком (pymorphy) и НЕ содержит латиницы -> легит рус.
    if not _LAT.search(core) and rumorph.available() and rumorph.word_is_known(core):
        return "russian_word"
    # план: явные примеры шума
    if core.lower() in ("почти", "со2", "000"):
        return "known_noise"
    return ""


def dedup_key(t: dict):
    """Форма = (нормализованный source, entity_kind, набор кандидатов). Разные кандидаты
    той же формы НЕ схлопываются (могут требовать разных решений в разных контекстах)."""
    cands = tuple(sorted(set((t.get("candidates") or []) + (
        [t["resolved_text"]] if t.get("resolved_text") and
        t.get("resolved_text") != t.get("source_text") else []))))
    return (_norm_src(t.get("source_text", "")), t.get("entity_kind") or "term", cands)


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_crops = "--no-crops" in sys.argv
    qpath = argv[0] if argv else os.path.join(QDIR, "tasks.json")
    tasks = json.load(open(qpath, encoding="utf-8"))
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks", [])
    print("вход: %s — %d задач" % (qpath, len(tasks)))

    # 1. auto не должно быть в очереди (санити)
    autos = [t for t in tasks if t.get("decision") == "auto"]
    if autos:
        print("ВНИМАНИЕ: %d auto-задач в очереди (должны быть 0) — исключаю" % len(autos))
    tasks = [t for t in tasks if t.get("decision") != "auto"]

    # 2+3. дедуп + отсев шума
    noise = []
    groups: "dict" = {}
    for t in tasks:
        reason = is_noise(t)
        if reason:
            t["_noise_reason"] = reason
            noise.append(t)
            continue
        k = dedup_key(t)
        if k not in groups:
            g = dict(t)
            g["occurrences"] = 0
            g["docs"] = []
            groups[k] = g
        g = groups[k]
        g["occurrences"] += 1
        if t.get("doc") and t["doc"] not in g["docs"]:
            g["docs"].append(t["doc"])
        # держим лучший кроп/локацию (первый с crop_png)
        if not g.get("crop_png") and t.get("crop_png"):
            g["crop_png"] = t["crop_png"]
            g["bbox"] = t.get("bbox")
            g["page"] = t.get("page")
            g["doc"] = t.get("doc")

    uniq = list(groups.values())

    def _is_crit(g):
        return bool(g.get("entity_kind") in CRIT_KINDS or g.get("is_critical"))

    def _has_cand(g):
        c = [x for x in (g.get("candidates") or []) if x and x != g.get("source_text")]
        return bool(c) or (g.get("resolved_text") and
                           g.get("resolved_text") != g.get("source_text"))

    # 4. сортировка: критические-С-кандидатом -> критические-догадки -> термины;
    #    внутри — по числу вхождений (частые формы выгоднее размечать первыми).
    def sortkey(g):
        if _is_crit(g):
            bucket = 0 if _has_cand(g) else 1
        else:
            bucket = 2
        return (bucket, -g.get("occurrences", 1), -len(g.get("docs", [])))
    uniq.sort(key=sortkey)

    crit = [g for g in uniq if _is_crit(g)]
    crit_actionable = [g for g in crit if _has_cand(g)]
    crit_guess = [g for g in crit if not _has_cand(g)]

    # 5. кропы (3 уровня) — только для минимизированного набора
    n_crop = 0
    if not no_crops:
        n_crop = _generate_crops(uniq)

    # запись
    out_tasks = os.path.join(QDIR, "tasks_min.json")
    for i, g in enumerate(uniq):
        g["task_id"] = "lt_%05d" % i
        g.pop("_noise_reason", None)
    with open(out_tasks, "w", encoding="utf-8") as fh:
        json.dump(uniq, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(QDIR, "_noise.jsonl"), "w", encoding="utf-8") as fh:
        for t in noise:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")

    # доклад
    by_kind = Counter(g.get("entity_kind") or "term" for g in uniq)
    noise_by = Counter(t.get("_noise_reason") for t in noise)
    print("\n" + "=" * 70)
    print("ИТОГ МИНИМИЗАЦИИ ОЧЕРЕДИ")
    print("=" * 70)
    print("вход задач (needs_review/unresolved): %d" % len(tasks))
    print("ШУМ отсеян -> _noise.jsonl:            %d" % len(noise))
    print("  по причинам: %s" % dict(noise_by.most_common()))
    print("-" * 70)
    print("ЗАДАЧ ЧЕЛОВЕКУ (дедуп, без шума):      %d" % len(uniq))
    print("  из них КРИТИЧЕСКИХ (icd/atc/tnm/dose/gene): %d" % len(crit))
    print("    - с кандидатом (actionable):        %d" % len(crit_actionable))
    print("    - догадка формы, без кандидата:     %d" % len(crit_guess))
    print("  по типу сущности: %s" % dict(by_kind.most_common()))
    print("  с кропом: %d / %d" % (sum(1 for g in uniq if g.get("crop_png")), len(uniq)))
    print("\nфайлы: %s  |  _noise.jsonl  |  crops_min/ (%d)" % (out_tasks, n_crop))
    return 0


def _generate_crops(tasks):
    """Три уровня кропа на задачу: слово (600 DPI, узкий), строка (широкий X),
    страница (bbox подсвечен). Пишет crops_min/, проставляет пути в задачу."""
    try:
        import fitz  # noqa
    except Exception:  # noqa: BLE001
        print("PyMuPDF недоступен — кропы пропущены")
        return 0
    crops = os.path.join(QDIR, "crops_min")
    os.makedirs(crops, exist_ok=True)
    docs = {}
    n = 0
    for g in tasks:
        doc_id = g.get("doc")
        bbox = g.get("bbox")
        page = g.get("page")
        if not (doc_id and bbox and page):
            continue
        pdf = os.path.join(RAW, doc_id + ".pdf")
        if not os.path.isfile(pdf):
            continue
        try:
            if doc_id not in docs:
                docs[doc_id] = fitz.open(pdf)
            d = docs[doc_id]
            pg = d[page - 1]
            x0, y0, x1, y1 = bbox
            safe = re.sub(r"[^0-9A-Za-z]", "_", "%s_p%d_%s" % (
                doc_id, page, g.get("task_id", str(n))))[:70]
            # слово: узкий bbox, 600 DPI
            wclip = fitz.Rect(x0 - 3, y0 - 3, x1 + 3, y1 + 3)
            wp = os.path.join(crops, safe + "_word.png")
            pg.get_pixmap(matrix=fitz.Matrix(600 / 72.0, 600 / 72.0), clip=wclip).save(wp)
            # строка: широкий по X (вся ширина страницы), 300 DPI
            lclip = fitz.Rect(pg.rect.x0 + 4, y0 - 4, pg.rect.x1 - 4, y1 + 4)
            lp = os.path.join(crops, safe + "_line.png")
            pg.get_pixmap(matrix=fitz.Matrix(300 / 72.0, 300 / 72.0), clip=lclip).save(lp)
            # страница: bbox подсвечен красной рамкой, 150 DPI
            pp = os.path.join(crops, safe + "_page.png")
            hl = pg.new_shape()
            hl.draw_rect(fitz.Rect(x0, y0, x1, y1))
            hl.finish(color=(1, 0, 0), width=1.5)
            hl.commit(overlay=True)
            pg.get_pixmap(matrix=fitz.Matrix(150 / 72.0, 150 / 72.0)).save(pp)
            g["crop_word"] = os.path.relpath(wp, QDIR)
            g["crop_line"] = os.path.relpath(lp, QDIR)
            g["crop_page"] = os.path.relpath(pp, QDIR)
            n += 1
        except Exception:  # noqa: BLE001
            continue
    for d in docs.values():
        try:
            d.close()
        except Exception:  # noqa: BLE001
            pass
    return n


if __name__ == "__main__":
    sys.exit(main())
