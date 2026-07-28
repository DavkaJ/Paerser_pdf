# -*- coding: utf-8 -*-
"""ЭТАП 3: кластеризация FAIL по сигнатуре первопричины (читает report.json).

    python _corpus/cluster.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPORT = os.path.join("_corpus", "report.json")


def main():
    results = json.load(open(REPORT, encoding="utf-8"))
    by_cls = defaultdict(list)
    for r in results:
        by_cls[r["cls"]].append(r)

    ok = by_cls["OK"]
    low = by_cls["LOW_TEXT_REVIEW"]
    st = Counter(r["status"] for r in ok)
    print("=" * 72)
    print("OK-группа: %d файлов -> %s" % (len(ok), dict(st)))
    print("LOW_TEXT_REVIEW: %d -> %s" %
          (len(low), Counter(r["status"] for r in low)))
    print("=" * 72)

    # ---- кластеры FAIL (только OK) по виду причины ----
    fails = [r for r in ok if r["status"] == "FAIL"]
    clusters = defaultdict(list)
    for r in fails:
        for k in r["fail_kinds"]:
            clusters[k].append(r)
    print("\n### КЛАСТЕРЫ FAIL (OK-группа): %d файлов в FAIL ###" % len(fails))
    for kind, rs in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        print("\n[%s]  размер=%d" % (kind, len(rs)))
        for r in rs[:5]:
            msgs = [f for f in r["fails"] if f.startswith(kind)]
            ex = msgs[0][:120] if msgs else ""
            print("   %-13s cov=%-6s sec=%-3s tab=%-3s | %s"
                  % (r["file"], r["coverage"], r["sections"], r["tables"], ex))

    # ---- файлы с несколькими видами FAIL ----
    multi = [r for r in fails if len(r["fail_kinds"]) > 1]
    if multi:
        print("\n### Файлы с НЕСКОЛЬКИМИ видами FAIL: %d ###" % len(multi))
        for r in multi[:15]:
            print("   %-13s %s" % (r["file"], r["fail_kinds"]))

    # ---- LOW_TEXT_REVIEW FAIL отдельно (не чинить) ----
    low_fail = [r for r in low if r["status"] == "FAIL"]
    print("\n### LOW_TEXT_REVIEW FAIL (держим отдельно, не чиним): %d ###" % len(low_fail))
    for r in low_fail:
        print("   %-13s %s" % (r["file"], r["fail_kinds"]))

    # ---- WARN-сводка (дефекты документов, не чиним) ----
    wc = Counter()
    for r in ok:
        for k in r["warn_kinds"]:
            wc[k] += 1
    print("\n### WARN-категории (OK, информационно): %s" % dict(wc.most_common()))

    # ---- крупный документ, 0 таблиц ----
    big0 = [r for r in ok if any("TABLES" in w for w in r["warns"])]
    print("\n### Крупный документ, 0 таблиц: %d ###" % len(big0))
    print("   " + ", ".join(r["file"] for r in big0[:40]))


if __name__ == "__main__":
    main()
