# -*- coding: utf-8 -*-
"""Дельта статусов промпта 10: report.json (с гейтами v2) vs report_pre10gates.json.
Раскладывает по причинам и проверяет контрольную группу I1."""
import collections
import json

I1 = {"КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"}

before = {x["file"]: x for x in json.load(open("_corpus/report_pre10gates.json", encoding="utf-8"))}
after = {x["file"]: x for x in json.load(open("_corpus/report.json", encoding="utf-8"))}

cb = collections.Counter(x["status"] for x in before.values())
ca = collections.Counter(x["status"] for x in after.values())
print("ДО (пред-гейты):", dict(cb))
print("ПОСЛЕ (v2-гейты):", dict(ca))

changed = [b for b in after if before.get(b, {}).get("status") != after[b]["status"]]
print("\nизменили статус: %d" % len(changed))
for b in changed:
    print("  %-9s %s -> %s  (new reviews: %s)"
          % (b, before.get(b, {}).get("status"), after[b]["status"],
             [r.split(":", 1)[0] for r in after[b].get("reviews", [])]))

# новые kind'ы по всему корпусу (в т.ч. на уже-не-PASS)
newk = collections.Counter()
for b in after:
    bk = set(before.get(b, {}).get("warn_kinds", []) + [r.split(":", 1)[0] for r in before.get(b, {}).get("reviews", [])])
    ak = set(after[b].get("warn_kinds", []) + [r.split(":", 1)[0] for r in after[b].get("reviews", [])])
    for k in ak - bk:
        newk[k] += 1
print("\nновые kind'ы (появились где раньше не было), по числу документов:")
for k, n in newk.most_common():
    print("  %-24s %d" % (k, n))

print("\nконтрольная группа I1:")
for b in sorted(I1):
    print("  %-9s %s" % (b, after.get(b, {}).get("status")))

# сколько получили новые v2-предупреждения/ревью
for k in ("LOW_STRUCTURED_COVERAGE", "HIGH_GARBAGE", "LOST_CONTENT", "DUPLICATION"):
    docs = [b for b in after if k in after[b].get("warn_kinds", [])
            or any(r.startswith(k) for r in after[b].get("reviews", []))]
    print("%-26s: %d документов" % (k, len(docs)))
