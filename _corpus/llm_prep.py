# -*- coding: utf-8 -*-
"""Подготовка входов для LLM-прохода (MORNING_TASKS 6/1). Для КАЛИБРОВОЧНОГО набора
(80 размеченных человеком) и АКТИВНОЙ очереди (tasks_min_v2) извлекает контекст ±200 симв.
из outout_latin вокруг спана. Пишет компактные JSON, которые читают под-агенты.

Калибровочный набор несёт СКРЫТЫЙ human-ответ (для сверки agreement, не показывать агенту).

    python _corpus/llm_prep.py
"""
import json, os, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
QDIR = os.path.join("_corpus", "verify_queue")
LATIN = "outout_latin"
_cache = {}


def norm(s): return re.sub(r"\s+", " ", (s or "").strip())


def doc_text(doc):
    if doc in _cache: return _cache[doc]
    txt = ""; p = os.path.join(LATIN, doc + ".json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8")); parts = []
            def w(ss):
                for x in ss:
                    if x.get("text"): parts.append(x["text"])
                    w(x.get("children", []))
            w(d.get("sections", []))
            for t in d.get("tables", []):
                if t.get("raw_text"): parts.append(t["raw_text"])
            txt = "\n".join(parts)
        except Exception: pass
    _cache[doc] = txt; return txt


def context(task, win=200):
    src = norm(task.get("source_text", "")); txt = doc_text(task.get("doc", ""))
    if not src or not txt: return ""
    i = txt.find(src)
    if i < 0: return ""
    c = txt[max(0, i - win):i + len(src) + win]
    return re.sub(r"\s+", " ", c).strip()


def rec(t):
    return {"task_id": t.get("task_id"), "source_text": t.get("source_text"),
            "context": context(t), "candidates": t.get("candidates") or [],
            "entity_kind": t.get("entity_kind"), "is_critical": bool(t.get("is_critical")),
            "occurrences": t.get("occurrences", 1)}


def main():
    # калибровка: 80 размеченных + скрытый human-ответ
    ex = json.load(open(os.path.join(QDIR, "ls_export.json"), encoding="utf-8"))
    calib = []
    for t in ex:
        data = t.get("data", {})
        for a in t.get("annotations", []):
            vals = {}
            for r in a.get("result", []):
                v = r.get("value", {})
                vals[r.get("from_name")] = (v.get("choices") or v.get("text") or [None])[0]
            dec = vals.get("decision")
            human_correct = data.get("resolved_text") if dec == "принять" else vals.get("corrected_text")
            r = rec(data)
            r["_human"] = {"decision": dec, "correct": human_correct}
            calib.append(r)
    json.dump(calib, open(os.path.join(QDIR, "calib_input.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    active = json.load(open(os.path.join(QDIR, "tasks_min_v2.json"), encoding="utf-8"))
    inp = [rec(t) for t in active]
    json.dump(inp, open(os.path.join(QDIR, "active_input.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    ctx = sum(1 for r in inp if r["context"])
    print("calib_input: %d (с контекстом %d)" % (len(calib), sum(1 for r in calib if r["context"])))
    print("active_input: %d (с контекстом %d / %d)" % (len(inp), ctx, len(inp)))


if __name__ == "__main__":
    main()
