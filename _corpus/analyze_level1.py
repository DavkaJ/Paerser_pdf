# -*- coding: utf-8 -*-
"""Разбор level1-MISSING: каноничный префикс (сдвиг номера) vs нестандартное имя;
   подтверждён ли потерянный раздел оглавлением (TOC)."""
import json, os, re, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

from crparser.engine.pdf_reader import PdfReader
from crparser.engine.toc import TocIndex
import re as _re
ANCHOR = _re.compile(r"^\s*(оглавление|содержание)\s*$", _re.I)

CANON = ("краткая информация", "диагностика", "лечение", "медицинская реабилитаци",
         "реабилитация", "профилактика", "организация оказания",
         "организация медицинской", "дополнительная информация",
         "критерии оценки качеств")

results = json.load(open(os.path.join("_corpus", "report.json"), encoding="utf-8"))
ok_fail = [r for r in results if r["cls"] == "OK" and r["status"] == "FAIL"]
_M = re.compile(r"MISSING:\s*(\S+)\s+'(.*?)'\s+—", re.S)

def norm(t):
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zа-яё ]+", " ", t.lower().replace("ё", "е"))).strip()

lvl1 = []
for r in ok_fail:
    for f in r["fails"]:
        if f.startswith("MISSING"):
            m = _M.search(f)
            if m and m.group(1).count(".") == 0:
                lvl1.append((r["file"], m.group(1), m.group(2)))

cat = Counter()
samples = {"canonical_shift": [], "noncanonical": []}
for file, num, title in lvl1:
    n = norm(title)
    is_canon = any(n.startswith(c) for c in CANON)
    c = "canonical_shift" if is_canon else "noncanonical"
    cat[c] += 1
    if len(samples[c]) < 12:
        samples[c].append((file, num, title[:55]))

print("level1 MISSING:", len(lvl1))
print("категории:", dict(cat))
for c, items in samples.items():
    print("\n[%s]" % c)
    for file, num, title in items:
        print("   %-12s %-3s %r" % (file, num, title))
