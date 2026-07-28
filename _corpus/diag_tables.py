# -*- coding: utf-8 -*-
"""Диагностика безрамочных таблиц: текущее tables_found + word-layout под
подписями «Таблица N» в целевых файлах.

    python _corpus/diag_tables.py КР176_2
"""
import os, re, sys, glob, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pdfplumber
from crparser.engine.parser import DocumentParser
from crparser.engine.jsonio import JsonWriter
from crparser.profiles import create_profile

_CAP = re.compile(r"^\s*Таблица\s+(\d+(?:\.\d+)?)\.?\s*(.*)$", re.IGNORECASE)


def words_to_rows(words, ytol=3.0):
    rows = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        placed = False
        for r in rows:
            if abs(r["top"] - w["top"]) <= ytol:
                r["words"].append(w)
                placed = True
                break
        if not placed:
            rows.append({"top": w["top"], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])
    rows.sort(key=lambda r: r["top"])
    return rows


def main():
    reg = glob.glob("*.xlsx")[0]
    parser = DocumentParser(create_profile("cr", reg))
    writer = JsonWriter()
    for base in sys.argv[1:]:
        pdf = os.path.join("data", "raw", base + ".pdf")
        doc = writer.to_dict(parser.parse(pdf))
        st = doc["stats"]
        print("=" * 78)
        print("%s  cov=%s sec=%s TABLES=%s" %
              (base, st["coverage_percent"], st["sections_found"], st["tables_found"]))
        with pdfplumber.open(pdf) as pf:
            for pno, page in enumerate(pf.pages, 1):
                bt = page.find_tables()
                caps = [ln for ln in (page.extract_text() or "").splitlines()
                        if _CAP.match(ln)]
                if not caps and not bt:
                    continue
                print("-- page %d  border_tables=%d  captions=%d" % (pno, len(bt), len(caps)))
                for c in caps:
                    print("   CAP:", c[:90])
                # показать первые ~12 строк-рядов с x-координатами слов
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                rows = words_to_rows(words)
                # найти ряд с подписью, показать 10 рядов после него
                cap_idx = None
                for i, r in enumerate(rows):
                    txt = " ".join(w["text"] for w in r["words"])
                    if _CAP.match(txt):
                        cap_idx = i
                        break
                if cap_idx is None:
                    continue
                for r in rows[cap_idx:cap_idx + 12]:
                    xs = [(round(w["x0"]), w["text"]) for w in r["words"]]
                    print("     y=%5.0f | %s" % (r["top"], xs))


if __name__ == "__main__":
    main()
