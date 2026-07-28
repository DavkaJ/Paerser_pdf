# -*- coding: utf-8 -*-
"""Сравнить два прогона: регрессии (PASS->FAIL) и улучшения (FAIL->PASS).

    python _corpus/compare.py [baseline.json] [current.json]
"""
import json, os, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

base_p = sys.argv[1] if len(sys.argv) > 1 else os.path.join("_corpus", "report_baseline.json")
cur_p = sys.argv[2] if len(sys.argv) > 2 else os.path.join("_corpus", "report.json")

base = {r["file"]: r for r in json.load(open(base_p, encoding="utf-8"))}
cur = {r["file"]: r for r in json.load(open(cur_p, encoding="utf-8"))}

# рассматриваем только OK-группу для регрессий по PASS/FAIL
def status_ok(r):
    return r["cls"] == "OK"

regress = []   # был PASS -> стал FAIL (жёсткая регрессия)
to_review = []  # был PASS -> стал REVIEW (порча; ожидаемо только на битых файлах)
improve = []   # был FAIL -> стал PASS
still_fail = []
for f, rc in cur.items():
    rb = base.get(f)
    if rb is None or not status_ok(rc):
        continue
    bs, cs = rb["status"], rc["status"]
    if bs == "PASS" and cs == "FAIL":
        regress.append((f, rc["fail_kinds"]))
    elif bs == "PASS" and cs == "REVIEW":
        to_review.append((f, rc.get("corruption", {})))
    elif bs == "FAIL" and cs == "PASS":
        improve.append(f)
    elif bs == "FAIL" and cs == "FAIL":
        still_fail.append(f)

print("=" * 64)
print("РЕГРЕССИИ (PASS->FAIL): %d" % len(regress))
for f, k in regress[:40]:
    print("   ! %-12s %s" % (f, k))
print("\nPASS->REVIEW (порча, ожидаемо на битых): %d" % len(to_review))
for f, c in to_review[:40]:
    print("   ⚑ %-12s %s" % (f, c))
print("\nУЛУЧШЕНИЯ (FAIL->PASS): %d" % len(improve))
print("   " + ", ".join(improve[:60]))
print("\nвсё ещё FAIL: %d" % len(still_fail))

# сводка по кластерам (OK FAIL)
def clusters(rep):
    c = Counter()
    for r in rep.values():
        if r["cls"] == "OK" and r["status"] == "FAIL":
            for k in r["fail_kinds"]:
                c[k] += 1
    return c

cb, cc = clusters(base), clusters(cur)
print("\nКЛАСТЕРЫ FAIL (файлов с данным видом):")
for k in sorted(set(cb) | set(cc), key=lambda k: -cc.get(k, 0)):
    print("   %-16s %3d -> %3d" % (k, cb.get(k, 0), cc.get(k, 0)))

nb = sum(1 for r in base.values() if r["cls"] == "OK" and r["status"] == "PASS")
nc = sum(1 for r in cur.values() if r["cls"] == "OK" and r["status"] == "PASS")
print("\nPASS (OK-группа): %d -> %d  (Δ%+d)" % (nb, nc, nc - nb))
