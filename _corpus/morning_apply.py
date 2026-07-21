# -*- coding: utf-8 -*-
"""Утренние задачи (model-free часть): применить РАЗМЕЧЕННЫЕ ПАРЫ + фильтры к очереди.

Вход : verify_queue/tasks_min.json (3237), verify_queue/ls_export.json (80 разметок),
       outout_latin/ (контекст для URL-фильтра).
НИЧЕГО В ТЕКСТ КОРПУСА не применяется — только очередь/пре-аннотации/провенанс.

Шаги (порядок MORNING_TASKS):
  - разобрать 80 разметок: 54 подтверждённых правки (src->correct), 26 whitelist (source_ok);
  - resolve 80 размеченных (уходят из активной очереди, решение -> _resolved_human.jsonl);
  - 5а РАЗМНОЖЕНИЕ: неразмеченные задачи той же формы -> human_verified пре-резолв (не в текст);
  - 5б WHITELIST: форма source_ok -> убрать из очереди + в whitelist (детектор не флагует впредь);
  - 2  URL-ФИЛЬТР: спаны в URL-контексте -> _url_noise.jsonl;
  - 7  РЕКЛАССИФИКАЦИЯ: патогены/стадии/типы клеток/классы препаратов -> приоритет; пересортировка.
Выход: tasks_min_v2.json (+ _resolved_human/_whitelist/_url_noise.jsonl) + числа по бакетам.

    python _corpus/morning_apply.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
QDIR = os.path.join("_corpus", "verify_queue")
LATIN = "outout_latin"


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


# ---- item 7: детекторы приоритетной критичности (патоген/стадия/клетка/класс) ----
_PATHO = re.compile(
    r"(ЦМВ|ВПГ|ВЭБ|ВИЧ|ВГВ|ВГС|ВПЧ|CMV|HSV|EBV|HIV|HBV|HCV|HPV|"
    r"Chlamyd|Salmonell|Herpes|Aspergill|Candida|Mycobacter|Escherichia|Klebsiel|"
    r"Staphyloc|Streptoc|Pseudomon|Helicobacter|Treponema|Toxoplasm|Legionell)", re.I)
# стадия: римские с дефисом (III-IV, Ш-1У, Н-Ш), «стадия», T/N/M уже tnm
_STAGE = re.compile(r"(стади|\b[IVXШНУ1]{1,3}\s*[-–]\s*[IVXШНУ1]{1,3}\b)", re.I)
# типы клеток: NK/ЫК, T-/B-клетки, CD-маркеры
_CELL = re.compile(r"(\bNK\b|\bЫК\b|CD\d{1,3}|[TBТВ]-?(клет|лимфоц|cell|helper|reg))", re.I)
_DRUGCLASS = re.compile(r"(ингибитор|블|блокатор|антагонист|аналог|моноклонал|-mab\b|-ib\b|"
                        r"статин|прил\b|сартан|инкретин|глифлозин)", re.I)


def reclass_priority(src, ek):
    """Вернуть ('critical'|'priority'|'term', new_kind). Критические kind — как в билдере;
    патоген/стадия/клетка/класс — поднять в 'priority' (средний между critical и term)."""
    s = src or ""
    if _PATHO.search(s):
        return "priority", "pathogen"
    if _STAGE.search(s) or ek == "stage":
        return "priority", "stage"
    if _CELL.search(s):
        return "priority", "cell"
    if _DRUGCLASS.search(s):
        return "priority", "drug_class"
    return None, ek


# ---- item 2: URL-контекст ----
_URL_MARK = re.compile(
    r"(https?://|ЬЦ?[рt]?[рp]?[зs]?://|www\.|\bdoi\b|10\.\d{4,}/|\.ru/|\.ги/|\.com|\.org|"
    r"\.pdf|rosoncoweb|elibrary|pubmed|medline|/standards/|/upload/)", re.I)


def _doc_text(doc, cache):
    if doc in cache:
        return cache[doc]
    txt = ""
    p = os.path.join(LATIN, doc + ".json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            parts = []

            def w(secs):
                for x in secs:
                    if x.get("text"):
                        parts.append(x["text"])
                    w(x.get("children", []))
            w(d.get("sections", []))
            for t in d.get("tables", []):
                if t.get("raw_text"):
                    parts.append(t["raw_text"])
            txt = "\n".join(parts)
        except Exception:  # noqa: BLE001
            pass
    cache[doc] = txt
    return txt


def in_url_context(task, cache):
    """Спан в URL-контексте: ±120 симв. вокруг вхождения source_text содержат URL-маркер."""
    src = norm(task.get("source_text", ""))
    if not src:
        return False
    txt = _doc_text(task.get("doc", ""), cache)
    if not txt:
        return False
    i = txt.find(src)
    while i != -1:
        window = txt[max(0, i - 120):i + len(src) + 120]
        if _URL_MARK.search(window):
            return True
        i = txt.find(src, i + 1)
    return False


def main():
    tm = json.load(open(os.path.join(QDIR, "tasks_min.json"), encoding="utf-8"))
    ex = json.load(open(os.path.join(QDIR, "ls_export.json"), encoding="utf-8"))

    # разобрать разметки
    annotated = {}        # task_id -> {decision, correct, ekfix}
    for t in ex:
        data = t.get("data", {})
        for a in t.get("annotations", []):
            vals = {}
            for r in a.get("result", []):
                v = r.get("value", {})
                vals[r.get("from_name")] = (v.get("choices") or v.get("text") or [None])[0]
            dec = vals.get("decision")
            correct = data.get("resolved_text") if dec == "принять" else vals.get("corrected_text")
            annotated[data.get("task_id")] = {
                "decision": dec, "correct": correct, "ekfix": vals.get("entity_kind_fix"),
                "src": data.get("source_text"), "ek": data.get("entity_kind"),
                "crit": data.get("is_critical")}

    corrections = {}      # norm(src) -> correct  (54: правка отличается от источника)
    whitelist = set()     # norm(src)  (26: source_ok — correct==source)
    for a in annotated.values():
        s, c = norm(a["src"]), norm(a["correct"] or "")
        if c and s and c != s:
            corrections[s] = a["correct"]
        elif c and s and c == s:
            whitelist.add(s)
        if a["ekfix"] == "not_entity":
            whitelist.add(s)

    resolved_human, whitelisted, url_noise, active = [], [], [], []
    cache = {}
    multiplied = 0
    for t in tm:
        tid = t.get("task_id")
        s = norm(t.get("source_text", ""))
        if tid in annotated:
            a = annotated[tid]
            t["resolution"] = {"source": "human", "decision": a["decision"],
                               "corrected_text": a["correct"], "entity_kind_fix": a["ekfix"]}
            resolved_human.append(t)
            continue
        if s in whitelist:                       # 5б
            t["_filter"] = "whitelist_source_ok"
            whitelisted.append(t)
            continue
        if s in corrections:                     # 5а размножение (пре-резолв human_verified)
            t["resolution"] = {"source": "human_verified_multiplied",
                               "corrected_text": corrections[s]}
            resolved_human.append(t)
            multiplied += 1
            continue
        if in_url_context(t, cache):             # 2 URL-фильтр
            t["_filter"] = "url_context"
            url_noise.append(t)
            continue
        active.append(t)                         # остаётся в очереди человеку

    # 7 реклассификация + пересортировка активной очереди
    CRIT = {"icd", "atc", "tnm", "dose", "drug", "gene"}
    prio_new = Counter()
    for t in active:
        tier, nk = reclass_priority(t.get("source_text"), t.get("entity_kind"))
        if tier == "priority" and not (t.get("entity_kind") in CRIT or t.get("is_critical")):
            t["priority_kind"] = nk
            t["priority_tier"] = "priority"
            prio_new[nk] += 1

    def tier_of(t):
        if t.get("entity_kind") in CRIT or t.get("is_critical"):
            return 0
        if t.get("priority_tier") == "priority":
            return 1
        return 2

    def has_cand(t):
        c = [x for x in (t.get("candidates") or []) if x and x != t.get("source_text")]
        return bool(c) or (t.get("resolved_text") and t.get("resolved_text") != t.get("source_text"))
    active.sort(key=lambda t: (tier_of(t), 0 if has_cand(t) else 1,
                               -t.get("occurrences", 1), -len(t.get("docs", []))))
    for i, t in enumerate(active):
        t["task_id"] = "lt_%05d" % i

    # запись
    json.dump(active, open(os.path.join(QDIR, "tasks_min_v2.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    for name, arr in (("_resolved_human.jsonl", resolved_human),
                      ("_whitelist.jsonl", whitelisted), ("_url_noise.jsonl", url_noise)):
        with open(os.path.join(QDIR, name), "w", encoding="utf-8") as fh:
            for t in arr:
                fh.write(json.dumps(t, ensure_ascii=False) + "\n")
    json.dump(sorted(whitelist), open(os.path.join(QDIR, "whitelist_forms.json"), "w",
              encoding="utf-8"), ensure_ascii=False, indent=1)

    # доклад по бакетам
    crit = [t for t in active if tier_of(t) == 0]
    prio = [t for t in active if tier_of(t) == 1]
    term = [t for t in active if tier_of(t) == 2]
    print("=" * 64)
    print("УТРЕННЯЯ ОБРАБОТКА ОЧЕРЕДИ (model-free) — ЧИСЛА ПО БАКЕТАМ")
    print("=" * 64)
    print("вход tasks_min:                     %d" % len(tm))
    print("-" * 64)
    print("resolved человеком (80 разметок):   %d  (54 правки + 26 whitelist)"
          % (len(annotated)))
    print("5а размножено (human_verified):     %d  той же формы -> пре-резолв" % multiplied)
    print("5б whitelist убрано из очереди:     %d" % len(whitelisted))
    print("2  URL-контекст -> _url_noise:       %d" % len(url_noise))
    print("-" * 64)
    print("АКТИВНАЯ ОЧЕРЕДЬ (осталось человеку): %d" % len(active))
    print("  критические (icd/atc/tnm/dose/gene): %d" % len(crit))
    print("  ПРИОРИТЕТ (7: патоген/стадия/клетка/класс): %d  %s"
          % (len(prio), dict(prio_new)))
    print("  обычные термины:                    %d" % len(term))
    print("  с кандидатом (actionable):          %d" % sum(1 for t in active if has_cand(t)))
    print("-" * 64)
    print("файлы: tasks_min_v2.json | _resolved_human(%d) | _whitelist(%d) | _url_noise(%d)"
          % (len(resolved_human), len(whitelisted), len(url_noise)))
    print("whitelist форм: %d | corrections форм: %d" % (len(whitelist), len(corrections)))


if __name__ == "__main__":
    main()
