# -*- coding: utf-8 -*-
"""Пересобрать _verified_corrections.json из ПОЛНОГО набора LS-аннотаций (латин_verify),
СОХРАНЯЯ пере-сверку критических по кропам (старые reverified/crop-arbitrated решения
побеждают) и ИСКЛЮЧАЯ 3 невалидных ATC (в пере-очередь _requeue_critical.json).

Вход:  _corpus/verify_queue/_ls_annotations.jsonl  (экспорт LS: source_text, resolved_text,
       decision ∈ {принять,ошибка}, corrected_text, entity_kind_fix, task_entity_kind, ...)
       _corpus/verify_queue/_verified_corrections.json  (старые 72 — источник пере-сверки)
Выход: _corpus/verify_queue/_verified_corrections.json  (обновлённый merged; бэкап _.bak)

Логика формы:
  corrected0 = corrected_text (ошибка) | resolved_text (принять)
  homoglyph-norm ТОЛЬКО если результат БЕЗ остаточной кириллицы (иначе «см H2O» -> «cм H2O»
  портит легит рус. единицу; но `J01XХ`->`J01XX` и `М1а`->`M1a` чинятся). == source -> skip.
  3 невалидных ATC (СО2АВО2/СО2АВО1/СО8САО2) -> requeue, не в текст.
  MERGE: старая запись ПОБЕЖДАЕТ на пересечении (сохраняет pO2-регистр, T2в->T2b (в не
  гомограф), crop-арбитраж JO1XX) — это и есть «сохранить пере-сверку».
"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from crparser.engine.latinnorm import _CYR2LAT

QDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "_corpus", "verify_queue")
ANNOS = os.path.join(QDIR, "_ls_annotations.jsonl")
VERIFIED = os.path.join(QDIR, "_verified_corrections.json")
CYR = re.compile(r"[А-Яа-яЁё]")
REQUEUE3 = {"СО2АВО2", "СО2АВО1", "СО8САО2"}   # оставить в _requeue_critical.json


def homoglyph_norm(s):
    return "".join(_CYR2LAT.get(c, c) for c in s)


def main():
    rows = [json.loads(l) for l in open(ANNOS, encoding="utf-8") if l.strip()]
    by_src = {}
    for r in rows:
        if r.get("source_text") is not None:
            by_src.setdefault(r["source_text"], r)   # первая аннотация формы

    new = {}
    stats = {"nochange": 0, "empty": 0, "requeue": 0, "norm_applied": 0}
    for st, r in by_src.items():
        c0 = r["corrected_text"] if r["decision"] == "ошибка" else r["resolved_text"]
        if not c0:
            stats["empty"] += 1
            continue
        kind = r.get("entity_kind_fix") or r.get("task_entity_kind")
        if kind == "not_entity":
            kind = None
        c = c0
        n = homoglyph_norm(c0)
        if not CYR.search(n) and n != c0:         # чиним гомографы ТОЛЬКО до чистой латиницы
            c = n
            stats["norm_applied"] += 1
        if c == st:
            stats["nochange"] += 1
            continue
        if st in REQUEUE3:                        # 3 невалидных ATC -> не в текст
            stats["requeue"] += 1
            continue
        new[st] = {
            "corrected": c, "entity_kind": kind,
            "provenance": "ls_verified_full", "confidence": 0.95,
            "note": "ls_export decision=%s" % r["decision"],
        }

    old = json.load(open(VERIFIED, encoding="utf-8"))
    # MERGE: старое побеждает (сохраняет пере-сверку/crop-арбитраж/pO2-регистр)
    merged = dict(new)
    kept_old = 0
    for st, e in old.items():
        if st in REQUEUE3:
            continue
        if st in merged and merged[st]["corrected"] != e["corrected"]:
            kept_old += 1                          # конфликт -> старая крип-сверка выигрывает
        merged[st] = e                             # старое поверх (или добавить old-only)

    # бэкап + запись
    if not os.path.exists(VERIFIED + ".bak"):
        json.dump(old, open(VERIFIED + ".bak", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    json.dump(merged, open(VERIFIED, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("аннотаций: %d | уникальных форм: %d" % (len(rows), len(by_src)))
    print("new-derived corrections: %d  (%s)" % (len(new), stats))
    print("old set: %d | merged (union, old-wins): %d форм" % (len(old), len(merged)))
    print("конфликтов, где старая пере-сверка перекрыла новую: %d" % kept_old)
    print("исключено 3 невалидных ATC -> _requeue_critical.json (не в текст)")
    print("бэкап: %s" % (VERIFIED + ".bak"))


if __name__ == "__main__":
    main()
