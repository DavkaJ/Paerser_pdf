# -*- coding: utf-8 -*-
"""Слить LLM-предсказания (llm_batches/pred_*.json) в активную очередь + числа по бакетам.

Пишет llm_suggestion в КАЖДУЮ задачу tasks_min_v2 (пре-аннотация, В ТЕКСТ НЕ применяется).
Раскладывает очередь на «быстрый accept» (некрит., высокая уверенность) и «ручной разбор»
(критические + низкая уверенность + fix без кандидата). Критические — человек ВСЕГДА.

    python _corpus/morning_merge.py
"""
import glob, json, os, re, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
QDIR = os.path.join("_corpus", "verify_queue")
BD = os.path.join(QDIR, "llm_batches")
HICONF = 0.80


def n(s): return re.sub(r"\s+", "", (s or "")).strip()


def main():
    active = json.load(open(os.path.join(QDIR, "tasks_min_v2.json"), encoding="utf-8"))
    preds = {}
    files = sorted(glob.glob(os.path.join(BD, "pred_*.json")))
    for fp in files:
        try:
            for p in json.load(open(fp, encoding="utf-8")):
                if p.get("task_id"):
                    preds[p["task_id"]] = p
        except Exception as e:  # noqa: BLE001
            print("bad pred file", fp, e)
    print("pred-файлов: %d | предсказаний: %d" % (len(files), len(preds)))

    CRIT = {"icd", "atc", "tnm", "dose", "drug", "gene"}
    def has_cand(t):
        c = [x for x in (t.get("candidates") or []) if x and x != t.get("source_text")]
        return bool(c) or (t.get("resolved_text") and t.get("resolved_text") != t.get("source_text"))
    def is_crit(t): return t.get("entity_kind") in CRIT or t.get("is_critical")
    def is_prio(t): return t.get("priority_tier") == "priority"

    covered = 0
    act_c = Counter(); conf_hi = 0
    for t in active:
        p = preds.get(t.get("task_id"))
        if not p:
            t["llm_suggestion"] = None
            continue
        covered += 1
        pc = p.get("predicted"); action = p.get("action"); conf = p.get("confidence") or 0
        t["llm_suggestion"] = {"model_version": "opus48-context", "predicted": pc,
                               "action": action, "confidence": conf, "reason": p.get("reason")}
        act_c[action] += 1
        if conf >= HICONF:
            conf_hi += 1

    # раскладка на быстрый-accept vs ручной
    quick, manual = [], []
    for t in active:
        p = t.get("llm_suggestion")
        crit = is_crit(t)
        if crit:
            manual.append(t); continue                     # критические — всегда человек
        if p and (p["confidence"] or 0) >= HICONF and p["action"] in ("source_ok", "fix"):
            quick.append(t)
        else:
            manual.append(t)

    json.dump(active, open(os.path.join(QDIR, "tasks_min_v3.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- ЧИСЛА ПО БАКЕТАМ ----
    crit = [t for t in active if is_crit(t)]
    prio = [t for t in active if is_prio(t) and not is_crit(t)]
    term = [t for t in active if not is_crit(t) and not is_prio(t)]
    resolved = sum(1 for _ in open(os.path.join(QDIR, "_resolved_human.jsonl"), encoding="utf-8"))
    urln = sum(1 for _ in open(os.path.join(QDIR, "_url_noise.jsonl"), encoding="utf-8"))
    wl = json.load(open(os.path.join(QDIR, "whitelist_forms.json"), encoding="utf-8"))
    print("\n" + "=" * 66)
    print("ОЧЕРЕДЬ ПОСЛЕ УТРЕННЕЙ ОБРАБОТКИ — ЧИСЛА ПО БАКЕТАМ")
    print("=" * 66)
    print("вход (tasks_min):                        3237")
    print("  -> resolved человеком (80) + 5а размнож: %d" % resolved)
    print("  -> 2 URL-контекст (_url_noise):          %d" % urln)
    print("  -> 5б whitelist форм:                    %d (форм)" % len(wl))
    print("-" * 66)
    print("АКТИВНАЯ ОЧЕРЕДЬ:                          %d" % len(active))
    print("  критические (человек ВСЕГДА):            %d" % len(crit))
    print("  приоритет 7 (патоген/стадия/клетка):     %d" % len(prio))
    print("  обычные термины:                         %d" % len(term))
    print("-" * 66)
    print("LLM-предсказания (пре-аннотации):          %d / %d" % (covered, len(active)))
    print("  по action: %s" % dict(act_c))
    print("  высокая уверенность (>=%.2f):            %d" % (HICONF, conf_hi))
    print("-" * 66)
    print("РАСКЛАДКА ДЛЯ ЧЕЛОВЕКА:")
    print("  «быстрый accept» (некрит., LLM conf>=%.2f): %d — листать подтверждения" % (HICONF, len(quick)))
    print("  «ручной разбор» (крит. + низкая уверен.):   %d — решать внимательно" % len(manual))
    print("-" * 66)
    print("файл: tasks_min_v3.json (с llm_suggestion). В ТЕКСТ КОРПУСА НИЧЕГО не применено.")


if __name__ == "__main__":
    main()
