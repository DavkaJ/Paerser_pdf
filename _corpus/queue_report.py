# -*- coding: utf-8 -*-
"""ЧЕГО СТОИТ ОЧЕРЕДЬ ВЕРИФИКАЦИИ — в задачах и в человеко-часах.

Очередь — настоящий выход ночного прогона, и она ДОРОГАЯ: это единственное, что стоит
живого времени людей. Замер отвечает: сколько задач, какого класса, и что из этого можно
снять механикой ДО того, как звать модели/людей.

Оценка времени: 10-20 с на задачу с кропом (посмотреть картинку, сверить с кандидатом,
нажать). Берём коридор, а не одно число.

    python _corpus/queue_report.py [_corpus/verify_queue/tasks.json]
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

Q = sys.argv[1] if len(sys.argv) > 1 else os.path.join("_corpus", "verify_queue", "tasks.json")
SEC_MIN, SEC_MAX = 10, 20


def main():
    tasks = json.load(open(Q, encoding="utf-8"))
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks", [])
    n = len(tasks)
    by_class = Counter()
    by_kind = Counter()
    by_doc = Counter()
    crit = 0
    no_crop = 0
    for t in tasks:
        ws = t.get("why_suspect") or []
        if "c5_ambiguous" in ws:
            g = next((w.split(":", 1)[1] for w in ws if w.startswith("c5_gate2:")), "?")
            by_class["C5 неоднозначно (гейт 2: %s)" % g] += 1
        elif "c5_corrupt_font" in ws:
            by_class["C5 починка -> подтвердить"] += 1
        elif "roman_stage" in ws:
            by_class["римская стадия"] += 1
        elif "line_context" in ws:
            by_class["сосед по строке"] += 1
        elif t.get("method") == "none":
            by_class["не разрешено (детектор увидел, кандидата нет)"] += 1
        else:
            by_class["eng-OCR термин/код -> подтвердить"] += 1
        by_kind[t.get("entity_kind") or "term"] += 1
        by_doc[t.get("doc")] += 1
        if t.get("is_critical"):
            crit += 1
        if not t.get("crop_png"):
            no_crop += 1

    print("=" * 74)
    print("ОЧЕРЕДЬ: %d задач в %d документах" % (n, len(by_doc)))
    print("=" * 74)
    print("ЦЕНА ЧЕЛОВЕКА: %.0f-%.0f чел.-часов (%d-%d с/задача)"
          % (n * SEC_MIN / 3600.0, n * SEC_MAX / 3600.0, SEC_MIN, SEC_MAX))
    print("критических (блокируют релиз): %d" % crit)
    print("БЕЗ КРОПА (задача бесполезна человеку): %d" % no_crop)

    print("\n--- по классам ---")
    for k, v in by_class.most_common():
        print("  %-46s %6d  (%4.1f%%)" % (k, v, 100.0 * v / n))
    print("\n--- по типу сущности ---")
    for k, v in by_kind.most_common(8):
        print("  %-46s %6d" % (k, v))
    print("\n--- ТОП-10 документов (концентрация работы) ---")
    for k, v in by_doc.most_common(10):
        print("  %-46s %6d" % (k, v))
    top10 = sum(v for _, v in by_doc.most_common(10))
    print("  ТОП-10 держат %.0f%% всей очереди" % (100.0 * top10 / n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
