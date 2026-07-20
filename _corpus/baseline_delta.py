# -*- coding: utf-8 -*-
"""Слить сканы обратно в report.json (batch делает 701, сканы карантинны/неизменны) и
показать ДЕЛЬТУ нового baseline (Jul-20, весь committed код) против Jul-14 по ПРИЧИНАМ.

Ожидаемо: КР715_2 FAIL->PASS (ШАГ 2), КР931_1 furniture (Phase 0.1, статус НЕ флипает).
Прочие переходы = дрейф Jul-14->Jul-20 (промежуточные latin/VLM/выравнивание коммиты).

    python _corpus/baseline_delta.py
"""
import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLD = "_corpus/report_pre_baseline.json"      # Jul-14, 722
NEW = "_corpus/report.json"                    # batch только что переписал (701)


def kinds(r):
    fk = list(r.get("fail_kinds", []))
    rk = sorted({x.split(":", 1)[0] for x in r.get("reviews", [])})
    return fk + rk


def main():
    old = {r["file"]: r for r in json.load(open(OLD, encoding="utf-8"))}
    new_list = json.load(open(NEW, encoding="utf-8"))
    new = {r["file"]: r for r in new_list}

    # СЛИТЬ сканы (в old, но не в new) обратно, чтобы report.json остался 722
    merged = list(new_list)
    added = 0
    for b, r in old.items():
        if b not in new:
            merged.append(r)
            added += 1
    merged.sort(key=lambda r: r["file"])
    json.dump(merged, open(NEW, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("report.json: %d (new %d + слито сканов/прочих из Jul-14 %d)"
          % (len(merged), len(new_list), added))

    def counts(d):
        return Counter(r["status"] for r in d)
    print("\n== СТАТУСЫ ==")
    print("  Jul-14:", dict(counts(old.values())))
    print("  Jul-20:", dict(counts(merged)))

    # переходы статуса
    trans = []
    for b in sorted(set(old) & set(new)):
        o, n = old[b]["status"], new[b]["status"]
        if o != n:
            trans.append((b, o, n))
    print("\n== ПЕРЕХОДЫ статуса: %d ==" % len(trans))
    tc = Counter((o, n) for _, o, n in trans)
    for (o, n), c in tc.most_common():
        print("  %-8s -> %-8s : %d" % (o, n, c))

    known = {"КР715_2": "ШАГ2 (START_IN_TOC fix)", "КР931_1": "Phase0.1 furniture"}
    print("\n== PASS -> не-PASS (потенциальные регрессы дрейфа): ==")
    for b, o, n in trans:
        if o == "PASS" and n != "PASS":
            print("  %-10s %s->%s  new-kinds=%s  %s"
                  % (b, o, n, kinds(new[b])[:4], known.get(b, "")))
    print("\n== не-PASS -> PASS (восстановления): ==")
    for b, o, n in trans:
        if o != "PASS" and n == "PASS":
            print("  %-10s %s->%s  %s" % (b, o, n, known.get(b, "ДРЕЙФ?")))
    print("\n== известные (мои правки) ==")
    for b, why in known.items():
        o = old.get(b, {}).get("status"); n = new.get(b, {}).get("status")
        print("  %-10s %s -> %s  [%s]" % (b, o, n, why))


if __name__ == "__main__":
    main()
