# -*- coding: utf-8 -*-
"""Замер дельты geometry-furniture (Phase 0.1, I30 #6) БЕЗ клоббера outout/.

Перепарсивает 701 OK/LOW док. (тот же набор, что batch.py), считает НОВЫЙ coverage_v2
и сравнивает с baseline-снимком (`_corpus/_cov_v2_pre_furniture.json`). Правка меняет
ТОЛЬКО split lost/furniture -> ТОЛЬКО гейт LOST_CONTENT (`lost/total>0.02 -> REVIEW`).
baseline max lost/total = 0.003 (<0.02) => ни один док сейчас НЕ REVIEW из-за LOST_CONTENT,
значит единственный возможный флип статуса — PASS->REVIEW при новом lost/total>0.02.
Статус считаем точно: FAIL при любом fail; иначе REVIEW при др. review-причине ИЛИ
новом LOST_CONTENT; иначе PASS.

    python _corpus/measure_furniture_delta.py
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW = os.path.join("data", "raw")
LOST_SPANS_REVIEW = 0.02        # == validate.LOST_SPANS_REVIEW
I1 = {"КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"}

_PARSER = None


def _init(registry):
    global _PARSER
    from crparser.engine.parser import DocumentParser
    from crparser.profiles import create_profile
    _PARSER = DocumentParser(create_profile("cr", registry))


def work(base):
    pdf = os.path.join(RAW, base + ".pdf")
    try:
        pr = _PARSER.parse(pdf)
        v2 = pr.stats.get("coverage_v2", {}) or {}
        return base, {"lost_spans": v2.get("lost_spans", 0),
                      "page_furniture": v2.get("page_furniture", 0),
                      "total_spans": v2.get("total_spans", 0),
                      "lost_span_uids": v2.get("lost_span_uids", [])}
    except Exception as exc:  # noqa: BLE001
        return base, {"error": repr(exc)}


def _lost_review(v2):
    t = v2.get("total_spans", 0) or 0
    l = v2.get("lost_spans", 0) or 0
    return bool(t and l / t > LOST_SPANS_REVIEW)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    registry = glob.glob("*.xlsx")[0]
    base_v2 = json.load(open("_corpus/_cov_v2_pre_furniture.json", encoding="utf-8"))
    report = {r["file"]: r for r in json.load(open("_corpus/report.json", encoding="utf-8"))}

    classmap = {}
    for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8")):
        classmap[os.path.splitext(r["file"])[0]] = r["class"]
    bases = sorted(b for b, c in classmap.items() if c in ("OK", "LOW_TEXT_REVIEW"))
    print("перепарс %d док. (geometry-furniture)…" % len(bases))

    t0 = time.time()
    new = {}
    with ProcessPoolExecutor(max_workers=14, initializer=_init, initargs=(registry,)) as ex:
        for i, (b, v2) in enumerate(ex.map(work, bases), 1):
            new[b] = v2
            if i % 150 == 0:
                print("  ...%d/%d (%.0fs)" % (i, len(bases), time.time() - t0))

    errs = {b: v["error"] for b, v in new.items() if "error" in v}
    if errs:
        print("ОШИБКИ парса (%d): %s" % (len(errs), list(errs.items())[:5]))

    # корпусные суммы
    of = sum((base_v2.get(b) or {}).get("page_furniture", 0) for b in bases)
    ol = sum((base_v2.get(b) or {}).get("lost_spans", 0) for b in bases)
    nf = sum(new[b].get("page_furniture", 0) for b in bases if "error" not in new[b])
    nl = sum(new[b].get("lost_spans", 0) for b in bases if "error" not in new[b])
    print("\n== корпус (701) ==")
    print("  furniture: %d -> %d  (%+d)" % (of, nf, nf - of))
    print("  lost:      %d -> %d  (%+d)" % (ol, nl, nl - ol))

    # статус-дельта (только PASS->REVIEW возможна)
    flips = []
    new_lost_docs = []
    for b in bases:
        v2 = new.get(b, {})
        if "error" in v2:
            continue
        rep = report.get(b, {})
        old_status = rep.get("status")
        fails = rep.get("fails", [])
        other_reviews = [r for r in rep.get("reviews", [])
                         if not r.startswith("LOST_CONTENT")]
        if fails:
            ns = "FAIL"
        elif other_reviews or _lost_review(v2):
            ns = "REVIEW"
        else:
            ns = "PASS"
        if ns != old_status:
            flips.append((b, old_status, ns, v2["lost_spans"], v2["total_spans"]))
        if _lost_review(v2):
            new_lost_docs.append((b, v2["lost_spans"], v2["total_spans"]))

    print("\n== статус-флипы (%d) ==" % len(flips))
    for b, o, n, l, t in flips:
        print("  %-10s %s -> %s  (lost=%d/%d=%.3f) I1=%s"
              % (b, o, n, l, t, l / t if t else 0, b in I1))
    print("\n== док. с новым LOST_CONTENT REVIEW (lost/total>0.02): %d ==" % len(new_lost_docs))
    for b, l, t in sorted(new_lost_docs, key=lambda x: -x[1] / max(1, x[2]))[:15]:
        print("  %-10s lost=%d/%d = %.3f  uids=%s"
              % (b, l, t, l / t, new[b]["lost_span_uids"][:4]))

    # рост lost по док. (top)
    grew = []
    for b in bases:
        if "error" in new.get(b, {}):
            continue
        o = (base_v2.get(b) or {}).get("lost_spans", 0)
        nn = new[b].get("lost_spans", 0)
        if nn != o:
            grew.append((b, o, nn, new[b]["total_spans"]))
    grew.sort(key=lambda x: -(x[2] - x[1]))
    print("\n== рост/спад lost по док. (top 20 по |дельте|) ==")
    for b, o, nn, t in sorted(grew, key=lambda x: -abs(x[2] - x[1]))[:20]:
        print("  %-10s lost %d -> %d  (%+d)  total=%d  I1=%s" % (b, o, nn, nn - o, t, b in I1))

    print("\n== контрольная группа I1 ==")
    for b in sorted(I1):
        o = (base_v2.get(b) or {})
        n = new.get(b, {})
        print("  %-10s furn %d->%d  lost %d->%d  total=%d  lost/total=%.4f"
              % (b, o.get("page_furniture", 0), n.get("page_furniture", 0),
                 o.get("lost_spans", 0), n.get("lost_spans", 0),
                 n.get("total_spans", 0),
                 n.get("lost_spans", 0) / max(1, n.get("total_spans", 1))))

    json.dump({"new": new}, open("_corpus/_furniture_delta.json", "w", encoding="utf-8"),
              ensure_ascii=False)
    print("\n(детали -> _corpus/_furniture_delta.json, %.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    main()
