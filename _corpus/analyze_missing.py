# -*- coding: utf-8 -*-
"""Подкластеризация MISSING: разобрать причину каждой потери раздела."""
import json, os, re, sys
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

results = json.load(open(os.path.join("_corpus", "report.json"), encoding="utf-8"))
ok_fail = [r for r in results if r["cls"] == "OK" and r["status"] == "FAIL"]

# собрать все MISSING-строки
miss = []  # (file, number, title)
_M = re.compile(r"MISSING:\s*(\S+)\s+'(.*?)'\s+—", re.S)
for r in ok_fail:
    for f in r["fails"]:
        if f.startswith("MISSING"):
            m = _M.search(f)
            if m:
                miss.append((r["file"], m.group(1), m.group(2)))

print("Всего MISSING-строк:", len(miss), "в", len({m[0] for m in miss}), "файлах")

def letterspaced(t):
    # доля одиночных «слов» (1 символ) высокая -> леттерспейсинг «г р у п п ы»
    w = t.split()
    if len(w) < 4:
        return False
    return sum(1 for x in w if len(x) == 1) / len(w) >= 0.4

cat = Counter()
ex = defaultdict(list)
for file, num, title in miss:
    lvl = num.count(".") + 1
    if letterspaced(title):
        c = "letterspaced"
    elif lvl == 1:
        c = "level1"
    elif lvl == 2:
        c = "level2"
    else:
        c = "level3+"
    cat[c] += 1
    if len(ex[c]) < 8:
        ex[c].append((file, num, title[:60]))

print("\nКатегории MISSING:", dict(cat))
for c, items in ex.items():
    print("\n[%s] (%d)" % (c, cat[c]))
    for file, num, title in items:
        print("   %-12s %-8s %r" % (file, num, title))

# файлы, где ВСЕ потери — level1 (вероятно нестандартные названия разделов)
byfile = defaultdict(list)
for file, num, title in miss:
    byfile[file].append((num, title))
only_lvl1 = [f for f, lst in byfile.items() if all(n.count(".") == 0 for n, _ in lst)]
print("\nФайлы, где все MISSING — level1:", len(only_lvl1))
print("  ", only_lvl1[:25])
