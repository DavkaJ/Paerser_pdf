# -*- coding: utf-8 -*-
"""Замер распределения coverage_v2 по всему корпусу (промпт 10, регламент шаг 1).

Читает outout/*.json (coverage_v2, испечён батчем) + _corpus/report.json (текущий
статус). Печатает перцентили метрик, влияние гейтов на ТЕКУЩИЕ PASS и контрольную
группу I1. Гейты НЕ включает — только измеряет, чтобы выбрать пороги (Р3).
"""
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

I1 = {"КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"}
PINS_LOW = ["КР848_1", "КР401_2", "КР675_2", "КР284_2", "КР839_1"]


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


def main():
    status = {x["file"]: x["status"] for x in
              json.load(open("_corpus/report.json", encoding="utf-8"))}
    docs = {}
    for jf in glob.glob(os.path.join("outout", "*.json")):
        b = os.path.splitext(os.path.basename(jf))[0]
        d = json.load(open(jf, encoding="utf-8"))
        v2 = (d.get("stats", {}) or {}).get("coverage_v2")
        if v2:
            docs[b] = v2
    print("документов с coverage_v2: %d / %d" % (len(docs), len(status)))

    metrics = ["source_retention", "structured_coverage", "garbage_ratio",
               "overlap_spans", "overlap_chars", "lost_spans", "page_furniture",
               "overcount_ratio", "total_spans"]
    print("\n== перцентили по ВСЕМ %d ==" % len(docs))
    for m in metrics:
        vals = [v2[m] for v2 in docs.values()]
        print("  %-20s p10=%s p50=%s p90=%s p99=%s max=%s"
              % (m, pct(vals, 10), pct(vals, 50), pct(vals, 90),
                 pct(vals, 99), max(vals)))

    passed = {b: docs[b] for b in docs if status.get(b) == "PASS"}
    print("\n== перцентили по ТЕКУЩИМ PASS (%d) ==" % len(passed))
    for m in metrics:
        vals = [v2[m] for v2 in passed.values()]
        print("  %-20s p10=%s p50=%s p90=%s p99=%s"
              % (m, pct(vals, 10), pct(vals, 50), pct(vals, 90), pct(vals, 99)))

    def impact(name, pred):
        hit = [b for b in passed if pred(passed[b])]
        print("\n-- гейт %s: уводит %d из %d PASS (%.1f%%) --"
              % (name, len(hit), len(passed), 100 * len(hit) / max(1, len(passed))))
        for b in hit[:5]:
            v2 = passed[b]
            print("     %-9s struct=%.3f garbage=%.3f lost=%d overlap=%d over=%.3f"
                  % (b, v2["structured_coverage"], v2["garbage_ratio"],
                     v2["lost_spans"], v2["overlap_spans"], v2["overcount_ratio"]))
        i1hit = [b for b in hit if b in I1]
        if i1hit:
            print("     !!! I1 задет: %s" % i1hit)

    for thr in (0.60, 0.70, 0.80):
        impact("structured_coverage<%.2f" % thr,
               lambda v2, t=thr: v2["structured_coverage"] < t)
    for thr in (0.20, 0.30, 0.40):
        impact("garbage_ratio>%.2f" % thr, lambda v2, t=thr: v2["garbage_ratio"] > t)
    for thr in (0.02, 0.05):
        impact("lost/total>%.2f" % thr,
               lambda v2, t=thr: v2["total_spans"] and
               v2["lost_spans"] / v2["total_spans"] > t)
    impact("lost_spans>0", lambda v2: v2["lost_spans"] > 0)
    impact("overlap_spans>0", lambda v2: v2["overlap_spans"] > 0)
    impact("overcount>1.02", lambda v2: v2["overcount_ratio"] > 1.02)

    print("\n== контрольная группа I1 ==")
    for b in sorted(I1):
        v2 = docs.get(b)
        if v2:
            print("  %-9s [%s] struct=%.3f garbage=%.3f lost=%d overlap=%d furn=%d over=%.3f"
                  % (b, status.get(b), v2["structured_coverage"], v2["garbage_ratio"],
                     v2["lost_spans"], v2["overlap_spans"], v2["page_furniture"],
                     v2["overcount_ratio"]))
        else:
            print("  %-9s НЕТ coverage_v2 (%s)" % (b, status.get(b)))

    print("\n== пины низкого structured (аудит §1.1 P0-2) ==")
    for b in PINS_LOW + ["КР628_2", "КР973_1", "КР953_1", "КР809_1"]:
        v2 = docs.get(b)
        if v2:
            print("  %-9s [%s] struct=%.3f garbage=%.3f overlap=%d over=%.3f"
                  % (b, status.get(b), v2["structured_coverage"], v2["garbage_ratio"],
                     v2["overlap_spans"], v2["overcount_ratio"]))

    real_lost = [(b, docs[b]["lost_spans"], docs[b]["total_spans"], docs[b]["lost_span_uids"])
                 for b in docs if docs[b]["lost_spans"] > 0]
    real_lost.sort(key=lambda x: -x[1] / max(1, x[2]))
    print("\n== документы с lost_spans>0 (реально потерянное, не обвязка): %d ==" % len(real_lost))
    for b, ls, ts, uids in real_lost[:15]:
        print("  %-9s lost=%d/%d (%.2f%%) [%s]  %s"
              % (b, ls, ts, 100 * ls / ts, status.get(b), uids[:3]))

    hi_struct_pass = sum(1 for b in passed if passed[b]["structured_coverage"] >= 0.90)
    print("\nPASS со structured>=0.90: %d / %d" % (hi_struct_pass, len(passed)))


if __name__ == "__main__":
    main()
