# -*- coding: utf-8 -*-
"""Отчёт по подкорпусу безрамочных таблиц: сколько файлов получили таблицы,
сколько осталось с 0 (и почему), список файлов на OCR (шрифтовая порча)."""
import os, sys, json, glob, csv, re
from collections import Counter
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

base = {r["file"]: r for r in json.load(open("_corpus/report_baseline.json", encoding="utf-8"))}
cur = {r["file"]: r for r in json.load(open("_corpus/report.json", encoding="utf-8"))}

# файлы, бывшие с 0 таблиц (OK-группа)
prior_zero = [f for f, r in base.items() if r["cls"] == "OK" and r.get("tables", 0) == 0]
gained = [f for f in prior_zero if cur.get(f, {}).get("tables", 0) > 0]
still0 = [f for f in prior_zero if cur.get(f, {}).get("tables", 0) == 0]

print("ПОДКОРПУС «0 таблиц» (OK-группа): %d файлов" % len(prior_zero))
print("  получили таблицы: %d  (всего %d новых таблиц)"
      % (len(gained), sum(cur[f]["tables"] for f in gained)))
print("  осталось с 0:     %d" % len(still0))
print()

# причины «осталось 0» и список OCR — из parser-warnings в outout/*.json
ocr = []
reasons = Counter()
for f in still0 + gained:
    jf = os.path.join("outout", f + ".json")
    if not os.path.exists(jf):
        continue
    doc = json.load(open(jf, encoding="utf-8"))
    warns = doc.get("warnings", [])
    corrupt = [w for w in warns if "шрифтовую порчу" in w]
    if corrupt:
        ocr.append((f, len(corrupt)))
    if f in still0:
        # есть ли вообще подпись «Таблица N» в тексте?
        if corrupt:
            reasons["шрифтовая порча (на OCR)"] += 1
        else:
            reasons["нет подписи/структуры таблицы"] += 1

print("ОСТАЛОСЬ 0 ТАБЛИЦ — причины:")
for r, n in reasons.most_common():
    print("   %-32s %d" % (r, n))
print("   файлы:", ", ".join(sorted(still0)))
print()
print("ФАЙЛЫ С ПОДОЗРЕНИЕМ НА ШРИФТОВУЮ ПОРЧУ (на OCR): %d" % len(ocr))
for f, n in sorted(ocr):
    print("   %-12s регионов: %d" % (f, n))
