# -*- coding: utf-8 -*-
"""Поиск всех упоминаний «Таблиц…» (в т.ч. не в начале строки / OCR-варианты)
и дамп строк региона для проверки шрифтовой порчи.

    python _corpus/diag_probe.py КР176_2
    python _corpus/diag_probe.py КР330_2 --region 13
"""
import os, re, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pdfplumber

_ANY = re.compile(r"Табл", re.IGNORECASE)


def main():
    base = sys.argv[1]
    region_page = None
    if "--region" in sys.argv:
        region_page = int(sys.argv[sys.argv.index("--region") + 1])
    pdf = os.path.join("data", "raw", base + ".pdf")
    with pdfplumber.open(pdf) as pf:
        for pno, page in enumerate(pf.pages, 1):
            txt = page.extract_text() or ""
            if region_page and pno == region_page:
                print("=== PAGE %d raw lines ===" % pno)
                for ln in txt.splitlines():
                    print("   |", ln)
            for ln in txt.splitlines():
                if _ANY.search(ln):
                    print("p%-3d | %s" % (pno, ln[:100]))


if __name__ == "__main__":
    main()
