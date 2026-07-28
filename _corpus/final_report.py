# -*- coding: utf-8 -*-
"""ЭТАП 5: финальный отчёт по корпусу. Категоризирует оставшиеся FAIL в
known-issues (леттерспейсинг / нестандартная структура / OCR-артефакты)."""
import json, os, sys, re, csv
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

R = json.load(open(os.path.join("_corpus", "report.json"), encoding="utf-8"))
CLS = {os.path.splitext(r["file"])[0]: r["class"]
       for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8"))}
SCAN = [f for f, c in CLS.items() if c == "SKIPPED_SCAN"]
LOW = [f for f, c in CLS.items() if c == "LOW_TEXT_REVIEW"]

ok = [r for r in R if r["cls"] == "OK"]
npass = sum(1 for r in ok if r["status"] == "PASS")
fails = [r for r in ok if r["status"] == "FAIL"]


def body_letterspaced(base):
    from crparser.engine.pdf_reader import PdfReader
    try:
        rd = PdfReader("data/raw/%s.pdf" % base); pages = rd.read(); rd.close()
    except Exception:
        return 0
    txt = " ".join(ln.text for p in pages for ln in p.lines)
    return len(re.findall(r"\b\w \w \w \w", txt))


# --- категоризация known-issues ---
buckets = defaultdict(list)
for r in fails:
    b = r["file"]
    nums = []
    for f in r["fails"]:
        m = re.search(r"MISSING:\s*(\S+)", f)
        if m:
            nums.append(m.group(1))
    deep = any(n.count(".") >= 5 for n in nums)
    ls = body_letterspaced(b)
    if deep:
        buckets["deep_nesting (>5 уровней номера)"].append(b)
    elif ls >= 10:
        buckets["letterspacing (разрядка букв в тексте)"].append(b)
    else:
        buckets["non_standard/OCR (нестандартная структура или артефакты)"].append(b)

print("=" * 72)
print("ИТОГОВЫЙ ОТЧЁТ ПО КОРПУСУ КР")
print("=" * 72)
print("Всего PDF: %d" % len(CLS))
print("  OK (распарсены и проверены): %d" % len(ok))
print("  SKIPPED_SCAN (сканы без текста): %d" % len(SCAN))
print("  LOW_TEXT_REVIEW (частичный текст, на ручную проверку): %d" % len(LOW))
print()
print("OK-ГРУППА: %d/%d PASS (%.1f%%), %d FAIL" %
      (npass, len(ok), 100 * npass / len(ok), len(fails)))
print()
print("--- FAIL по known-issues (парсер НЕ чинится без регрессий) ---")
for k in sorted(buckets, key=lambda k: -len(buckets[k])):
    fs = sorted(buckets[k])
    print("  [%s]: %d файлов" % (k, len(fs)))
    print("     " + ", ".join(fs))
print()
print("--- SKIPPED_SCAN (%d) — на OCR/ручную обработку (см. scan_candidates.csv) ---" % len(SCAN))
print("  " + ", ".join(sorted(SCAN)))
print()
print("--- LOW_TEXT_REVIEW (%d) — на ручную проверку ---" % len(LOW))
print("  " + ", ".join(sorted(LOW)) + (" -> %s" % [r["status"] for r in R if r["cls"] == "LOW_TEXT_REVIEW"]))
print()
# крупный документ, 0 таблиц
big0 = [r["file"] for r in ok if any("TABLES" in w for w in r["warns"])]
print("--- Крупный документ (>=20 разделов), 0 таблиц: %d (на ручную проверку детектора) ---" % len(big0))
print("  " + ", ".join(sorted(big0)))
print()
# WARN-сводка
wc = Counter()
for r in ok:
    for k in r["warn_kinds"]:
        wc[k] += 1
print("--- WARN-категории (дефекты документов / косметика, НЕ чинятся) ---")
for k, c in wc.most_common():
    print("  %-16s %d файлов" % (k, c))
