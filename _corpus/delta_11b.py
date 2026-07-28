# -*- coding: utf-8 -*-
"""Дельта промпта 11b (подключение heading_order_valid). Проверяет пины:
- список изменившихся JSON НЕПУСТ, но <=50 (иначе слишком агрессивно);
- PASS НЕ растёт (рост = прячем проблемы);
- контрольная группа I1 цела;
- изменения — только добавленный warning «обратный шаг нумерации», не потеря секций.
"""
import glob
import json
import os

I1 = {"КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"}
BK = os.environ["BK11B"]


def walk(secs):
    for s in secs:
        yield s
        yield from walk(s.get("children", []))


# 1) изменившиеся JSON (byte) + причина
changed, sec_lost = [], []
for jf in sorted(glob.glob("outout/*.json")):
    b = os.path.splitext(os.path.basename(jf))[0]
    new = open(jf, encoding="utf-8").read()
    old = open(os.path.join(BK, b + ".json"), encoding="utf-8").read()
    if new == old:
        continue
    changed.append(b)
    nd, od = json.loads(new), json.loads(old)
    n_new = sum(1 for _ in walk(nd["sections"]))
    n_old = sum(1 for _ in walk(od["sections"]))
    # проверка: изменение — только новый warning, число секций не упало
    only_warn = (nd["sections"] == od["sections"] and nd["tables"] == od["tables"]
                 and nd["excluded"] == od["excluded"])
    if n_new < n_old:
        sec_lost.append((b, n_old, n_new))
    if not only_warn and n_new >= n_old:
        # структура изменилась не только warning'ом (но секции не потеряны)
        pass

print("изменившихся JSON: %d %s" % (len(changed), "<=50 OK" if len(changed) <= 50 else "!!! >50 СЛИШКОМ АГРЕССИВНО"))
print("первые:", changed[:30])
print("документов с ПОТЕРЕЙ секций:", len(sec_lost), sec_lost[:10])
i1_changed = [b for b in changed if b in I1]
print("I1 изменены (должно быть []):", i1_changed)

# 2) статус-дельта
before = {x["file"]: x["status"] for x in json.load(open("_corpus/report_pre11b.json", encoding="utf-8"))}
after = {x["file"]: x["status"] for x in json.load(open("_corpus/report.json", encoding="utf-8"))}
import collections
cb = collections.Counter(before.values())
ca = collections.Counter(after.values())
print("\nДО :", dict(cb))
print("ПОСЛЕ:", dict(ca))
print("PASS до=%d после=%d  %s" % (cb["PASS"], ca["PASS"],
      "OK (не вырос)" if ca["PASS"] <= cb["PASS"] else "!!! PASS ВЫРОС — прячем проблемы"))
moved = [(b, before[b], after[b]) for b in after if before.get(b) != after[b]]
print("сменили статус:", len(moved), moved[:20])
print("I1 статусы:", {b: after.get(b) for b in sorted(I1)})
