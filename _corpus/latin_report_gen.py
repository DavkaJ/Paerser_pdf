# -*- coding: utf-8 -*-
"""Собрать корпусный отчёт latin recovery (промпт 13b, Group D) из report_latin.json +
outout_latin/. Пишет _corpus/latin_recovery_report.md.

    python _corpus/latin_report_gen.py
"""
import glob
import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATIN = os.path.join(ROOT, "outout_latin")
REPORT = os.path.join(ROOT, "_corpus", "report_latin.json")
OUT = os.path.join(ROOT, "_corpus", "latin_recovery_report.md")


def main():
    rep = json.load(open(REPORT, encoding="utf-8")) if os.path.exists(REPORT) else []
    by_method = Counter()
    by_kind = Counter()
    by_decision = Counter()
    total_corr = total_auto = total_nr = total_unres = 0
    docs_touched = docs_blocked = 0
    unres_by_doc = Counter()
    unres_kinds = Counter()
    crit_kinds_total = Counter()
    files = sorted(glob.glob(os.path.join(LATIN, "*.json")))
    for p in files:
        doc = json.load(open(p, encoding="utf-8"))
        base = os.path.splitext(os.path.basename(p))[0]
        block = doc.get("latin_recovery") or {}
        corr = block.get("corrections", []) or []
        unres = block.get("unresolved_critical", []) or []
        if corr:
            docs_touched += 1
        if unres:
            docs_blocked += 1
            unres_by_doc[base] = len(unres)
            for u in unres:
                unres_kinds[u.get("kind")] += 1
        total_unres += len(unres)
        for c in corr:
            total_corr += 1
            by_method[c.get("method")] += 1
            by_kind[c.get("entity_kind")] += 1
            by_decision[c.get("decision")] += 1
            if c.get("decision") == "auto":
                total_auto += 1
            elif c.get("decision") == "needs_review":
                total_nr += 1
            if c.get("is_critical"):
                crit_kinds_total[c.get("entity_kind")] += 1

    status = Counter(r["status"] for r in rep)
    # дельта статусов vs baseline report.json (если есть)
    base_report = os.path.join(ROOT, "_corpus", "report.json")
    delta = ""
    if os.path.exists(base_report):
        try:
            b = {r["file"]: r["status"] for r in json.load(open(base_report, encoding="utf-8"))}
            moves = Counter()
            for r in rep:
                bs = b.get(r["file"])
                if bs and bs != r["status"]:
                    moves["%s->%s" % (bs, r["status"])] += 1
            delta = ", ".join("%s: %d" % (k, v) for k, v in moves.most_common())
        except Exception:  # noqa: BLE001
            delta = "(не удалось сопоставить с baseline)"

    L = []
    L.append("# Latin recovery — корпусный отчёт (промпт 13b, за флагом --latin-recovery)\n")
    L.append("Прогон: `_corpus/batch_latin.py` (OCR on), выход `outout_latin/`, "
             "baseline `outout/` НЕ тронут. Документов: %d.\n" % len(files))
    L.append("## Статусы восстановленного корпуса")
    L.append("```\n%s\n```" % dict(status))
    if delta:
        L.append("**Дельта статусов vs baseline report.json:** %s\n" % (delta or "нет"))
    L.append("## Замены")
    L.append("- всего замен: **%d** в **%d** документах" % (total_corr, docs_touched))
    L.append("- уверенных (auto): **%d**  •  на проверку (needs_review): **%d**"
             % (total_auto, total_nr))
    L.append("- по КАНАЛАМ: %s" % dict(by_method.most_common()))
    L.append("- по СУЩНОСТЯМ: %s" % dict(by_kind.most_common()))
    L.append("- критических замен по типам: %s\n" % dict(crit_kinds_total.most_common()))
    L.append("## Остаток: неразрешённые КРИТИЧЕСКИЕ латинские сущности (release-block)")
    L.append("- всего: **%d** в **%d** документах (карантин LATIN_UNRESOLVED)"
             % (total_unres, docs_blocked))
    L.append("- по типам: %s" % dict(unres_kinds.most_common()))
    L.append("- топ-документы: %s\n"
             % ", ".join("%s(%d)" % (d, n) for d, n in unres_by_doc.most_common(15)))
    L.append("## Очередь верификации (Label Studio)")
    tasks_p = os.path.join(ROOT, "_corpus", "verify_queue", "tasks.json")
    ntasks = len(json.load(open(tasks_p, encoding="utf-8"))) if os.path.exists(tasks_p) else 0
    ncrops = len(glob.glob(os.path.join(ROOT, "_corpus", "verify_queue", "crops", "*.png")))
    L.append("- задач: **%d**  •  кропов: **%d**  •  конфиг: "
             "`_corpus/verify_queue/labeling_config.xml`" % (ntasks, ncrops))
    open(OUT, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("написано:", OUT)
    print("замен: %d (auto %d / nr %d), неразрешённых крит: %d в %d док., статусы: %s"
          % (total_corr, total_auto, total_nr, total_unres, docs_blocked, dict(status)))


if __name__ == "__main__":
    main()
