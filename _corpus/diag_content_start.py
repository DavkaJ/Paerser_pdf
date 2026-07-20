# -*- coding: utf-8 -*-
"""Content-start триаж (Phase 1 / промпт 12, ШАГ 1 — ДИАГНОСТИКА, не фикс).

Классифицирует КАЖДЫЙ документ корпуса по ПРИЧИНЕ обвала начала тела:
  * START_IN_TOC        — строки оглавления (точки-лидеры) утекли в sections (тело),
                          вместо того чтобы уйти в excluded.toc;
  * START_IN_FRONTMATTER— front-matter (список сокращений / термины / критерии качества)
                          стал разделом тела, а не excluded.front_matter;
  * NUMBERING_MISMATCH   — старт корректен, но распознано <5/7 канонических глав;
  * OK.
Классификаторы — по МАРКЕРАМ САМОГО КОДА (segmenter._RE_LEADER, clinical _NAMED_SECTIONS,
validate._canonical_recall), не по выдуманным эвристикам. Сегментер не менялся со времени
outout/ (Phase 0 трогал stats/release), поэтому классифицируем по испечённому JSON — быстро.

Выход: _corpus/content_start_classes.json + таблица. Ожидается ~16 не-OK (оценка аудита).

    python _corpus/diag_content_start.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from validate import _canonical_recall            # noqa: E402  (единый источник recall)

OUTOUT = "outout"
CANON_MIN = 5                                      # == validate.CANONICAL_RECALL_MIN

# маркеры КОДА (segmenter/clinical), не переизобретаем
_RE_LEADER = re.compile(r"\.{3,}|_{3,}|…+|‥+|․{2,}")
_RE_TOC_MARK = re.compile(r"^\s*(оглавление|содержание)\s*$", re.IGNORECASE)
# ИСТИННЫЙ front-matter, который ДОЛЖЕН быть в excluded, а не разделом тела. «Критерии
# оценки качества» СЮДА НЕ ВХОДИТ — это ЛЕГИТИМНАЯ глава КР (иначе 661 ложный FM).
_FRONT_MATTER = (
    re.compile(r"список\s+сокращ", re.IGNORECASE),
    re.compile(r"термин\w*\s+и\s+определ", re.IGNORECASE),
    re.compile(r"состав\s+рабоч\w*\s+групп", re.IGNORECASE),
    re.compile(r"конфликт\w*\s+интерес", re.IGNORECASE),
)
# хвост строки оглавления (clinical._RE_TOC_TAIL): «...... 26» или «  26». Заголовок раздела,
# ОКАНЧИВАЮЩИЙСЯ номером страницы, — это пункт TOC, утёкший в тело (КР715_2 «…печени 96»).
_RE_TOC_TAIL = re.compile(r"(\.{2,}\s*\d{1,4}|\s\d{1,4})\s*$")
TOC_TAIL_MIN = 2                                   # >=N заголовков-с-хвостом = TOC утёк в тело


def _walk(sections):
    for s in sections or []:
        yield s
        yield from _walk(s.get("children", []) or [])


def _toc_tail_titles(doc):
    """Заголовки разделов, ОКАНЧИВАЮЩИЕСЯ номером страницы (пункт TOC в теле). Реальный
    заголовок КР так не кончается — «Трансплантация печени 96» = строка оглавления."""
    hits = []
    for s in _walk(doc.get("sections", [])):
        title = (s.get("title") or "").strip()
        if _RE_TOC_TAIL.search(title) and re.search(r"[А-Яа-яA-Za-z]", title):
            hits.append(title[:45])
    return hits


def _leaders_in_body(doc) -> int:
    n = 0
    for s in _walk(doc.get("sections", [])):
        for field in (s.get("title") or "", s.get("text") or ""):
            n += len(_RE_LEADER.findall(field))
    return n


def _front_matter_sections(doc):
    """Разделы тела, чьё имя — front-matter (сокращения/термины/критерии качества)."""
    hits = []
    for s in _walk(doc.get("sections", [])):
        title = (s.get("title") or "").strip()
        if any(rx.search(title) for rx in _FRONT_MATTER):
            hits.append(title[:50])
    return hits


def _toc_marker_as_section(doc):
    for s in _walk(doc.get("sections", [])):
        if _RE_TOC_MARK.match((s.get("title") or "").strip()):
            return (s.get("title") or "").strip()
    return None


def _toc_glued(doc) -> bool:
    """excluded.toc[0] СКЛЕИВАЕТ оглавление + сокращения + термины (аудит §4.1, КР1_4 pp3-7,
    КР628_2 pp2-6). Строгий сигнал (ШАГ 3): элемент toc ДЛИННЫЙ (многостраничный, >2000 симв)
    И несёт >=2 РАЗНЫХ front-matter-маркера (не мимолётное упоминание «...сокращений... 3» в
    строке оглавления). Иначе 707 ложных (почти каждый toc упоминает «сокращения»)."""
    toc = (doc.get("excluded", {}) or {}).get("toc") or []
    for it in toc:
        text = (it.get("text") or "")
        n = sum(1 for rx in _FRONT_MATTER if rx.search(text))
        if len(text) > 2000 and n >= 2:
            return True
    return False


def classify(doc, rep):
    recall = _canonical_recall(doc)
    toc_tails = _toc_tail_titles(doc)
    fm = _front_matter_sections(doc)
    toc_sec = _toc_marker_as_section(doc)
    lost = recall < CANON_MIN                       # потеряны ГЛАВЫ верхнего уровня
    toc_leak = len(toc_tails) >= TOC_TAIL_MIN or toc_sec is not None
    kinds = set(rep.get("fail_kinds", [])) | {x.split(":", 1)[0] for x in rep.get("reviews", [])}
    # приоритет причины. START_IN_FRONTMATTER — только когда FM в теле И главы ПОТЕРЯНЫ
    # (иначе «Термины как раздел» при целых главах — не дефект, а FM_PREPEND, низкая severity).
    if toc_leak:
        klass = "START_IN_TOC"
    elif lost and fm:
        klass = "START_IN_FRONTMATTER"
    elif lost:
        klass = "NUMBERING_MISMATCH"
    elif fm:
        klass = "FM_PREPEND"                         # watchlist: FM в теле, главы целы
    else:
        klass = "OK"
    return klass, {"recall": recall, "toc_tail_titles": toc_tails,
                   "front_matter_sections": fm, "toc_marker_section": toc_sec,
                   "toc_glued": _toc_glued(doc), "missing": "MISSING" in kinds,
                   "n_sections": sum(1 for _ in _walk(doc.get("sections", [])))}


CORE = ("START_IN_TOC", "START_IN_FRONTMATTER", "NUMBERING_MISMATCH")


def main():
    rep_by = {}
    rp = os.path.join("_corpus", "report.json")
    if os.path.exists(rp):
        rep_by = {r["file"]: r for r in json.load(open(rp, encoding="utf-8"))}

    out = {}
    from collections import Counter, defaultdict
    counts = Counter()
    examples = defaultdict(list)
    glued = []
    for jf in sorted(glob.glob(os.path.join(OUTOUT, "*.json"))):
        b = os.path.splitext(os.path.basename(jf))[0]
        doc = json.load(open(jf, encoding="utf-8"))
        rep = rep_by.get(b, {})
        klass, info = classify(doc, rep)
        info["status"] = rep.get("status")
        out[b] = dict(klass=klass, **info)
        counts[klass] += 1
        if klass != "OK":
            examples[klass].append((b, info))
        if info["toc_glued"]:
            glued.append(b)

    json.dump(out, open(os.path.join("_corpus", "content_start_classes.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)

    core_n = sum(counts[k] for k in CORE)
    print("== content-start классы (%d док.) ==" % len(out))
    for k in ("OK",) + CORE + ("FM_PREPEND",):
        print("  %-22s %d" % (k, counts[k]))
    print("  ---")
    print("  CORE не-OK (TOC+FRONTMATTER+NUMBERING): %d  (оценка аудита ~16)" % core_n)
    print("  FM_PREPEND (watchlist, главы целы — низкая severity): %d" % counts["FM_PREPEND"])

    for k in CORE:
        ex = examples[k]
        print("\n== %s (%d) ==" % (k, len(ex)))
        for b, info in sorted(ex, key=lambda x: (x[1]["recall"], x[0]))[:40]:
            extra = ""
            if info["toc_tail_titles"]:
                extra += " tails=%s" % info["toc_tail_titles"][:2]
            if info["front_matter_sections"]:
                extra += " fm=%s" % info["front_matter_sections"][:2]
            if info["toc_marker_section"]:
                extra += " toc_sec=%r" % info["toc_marker_section"]
            print("  %-10s [%s] recall=%d nsec=%d%s"
                  % (b, info["status"], info["recall"], info["n_sections"], extra))

    # пины из промпта — проверить, что попали в ожидаемый класс
    pins = {"START_IN_TOC": ["КР715_2", "КР675_2"],
            "START_IN_FRONTMATTER": ["КР891_1", "КР115_2", "КР467_3"],
            "NUMBERING_MISMATCH": ["КР284_2", "КР750_1", "КР848_1"]}
    print("\n== пины промпта 12 (ожидаемый класс) ==")
    for exp, bs in pins.items():
        for b in bs:
            got = out.get(b, {}).get("klass", "НЕТ")
            mark = "OK" if got == exp else ("~" if got != "OK" else "!!МИМО")
            print("  %-10s ожид=%-22s факт=%-22s %s" % (b, exp, got, mark))

    print("\n== toc-элемент несёт >=2 FM-маркера, >2000 симв: %d (НЕнадёжный сигнал) ==" % len(glued))
    print("   КР1_4=%s КР628_2=%s — но склейку (pp3-7) ШАГ 3 проверяет по PDF напрямую:"
          % ("КР1_4" in glued, "КР628_2" in glued))
    print("   TOC ПЕРЕЧИСЛЯЕТ «Список сокращений/Термины» как ПУНКТЫ -> сигнал ловит и"
          " здоровые (КР1000_1). Реальная склейка = АБЗАЦЫ определений в toc, не пункты.")

    # ОГРАНИЧЕНИЕ recall<5: он видит потерю ГЛАВ верхнего уровня, но НЕ потерю
    # ПОДРАЗДЕЛОВ (КР284_2/750_1: recall=7, но потеряны 4 подраздела) и НЕ full-recall
    # front-matter-prepend (КР115_2/467_3). Эти файлы валидатор уже ловит гейтом MISSING
    # (FAIL), но по recall глав они «OK». Отдельный watchlist для ШАГА 4.
    sub_loss = [b for b, v in out.items()
                if v["klass"] == "OK" and v["missing"] and v["recall"] >= CANON_MIN]
    print("\n== recall>=5, но MISSING (потеря ПОДРАЗДЕЛОВ — вне recall глав): %d ==" % len(sub_loss))
    print("   аудит-пины сюда: КР284_2=%s КР750_1=%s КР115_2=%s КР467_3=%s"
          % tuple(b in sub_loss for b in ("КР284_2", "КР750_1", "КР115_2", "КР467_3")))
    print("   первые:", ", ".join(sorted(sub_loss)[:25]))
    print("\nСВЕРКА С АУДИТОМ: CORE %d (потеря ГЛАВ) ~ оценка ~16; расхождение объяснимо —"
          % core_n)
    print("  recall<5 ловит потерю ГЛАВ (высшая severity); потеря ПОДРАЗДЕЛОВ (%d, вкл."
          % len(sub_loss))
    print("  КР284_2/750_1) и FM_PREPEND (%d) — отдельные, ниже severity, чинятся ШАГ 3/4."
          % counts["FM_PREPEND"])


if __name__ == "__main__":
    main()
