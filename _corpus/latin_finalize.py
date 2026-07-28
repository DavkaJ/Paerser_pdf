# -*- coding: utf-8 -*-
"""Финализация ночного прогона latin recovery: дедуп очереди verify_queue/tasks.json
(одна задача на уникальную (doc, source_text) — резолвер эмитит по-ОКУРРЕНСНО), затем
корпусный отчёт. Запускать ПОСЛЕ _corpus/batch_latin.py.

    python _corpus/latin_finalize.py
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS = os.path.join(ROOT, "_corpus", "verify_queue", "tasks.json")


def dedupe_tasks():
    if not os.path.exists(TASKS):
        print("tasks.json нет"); return 0, 0
    tasks = json.load(open(TASKS, encoding="utf-8"))
    seen = {}
    for t in tasks:
        key = (t.get("doc"), t.get("source_text"), t.get("resolved_text"))
        # держим задачу С кропом в приоритете
        if key not in seen or (not seen[key].get("crop_png") and t.get("crop_png")):
            seen[key] = t
    dedup = list(seen.values())
    # критические — вперёд (человек видит важное первым)
    dedup.sort(key=lambda t: (not t.get("is_critical"), t.get("doc") or "", t.get("source_text") or ""))
    json.dump(dedup, open(TASKS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return len(tasks), len(dedup)


if __name__ == "__main__":
    before, after = dedupe_tasks()
    print("очередь: %d -> %d задач (дедуп по doc+source)" % (before, after))
    # корпусный отчёт
    import runpy
    runpy.run_path(os.path.join(ROOT, "_corpus", "latin_report_gen.py"), run_name="__main__")
