# -*- coding: utf-8 -*-
"""Подсчёт подписей «Таблица N» и проверка на шрифтовую порчу по странице.

    python _corpus/diag_caps.py КР176_2 КР330_2
"""
import os, re, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pdfplumber

_CAP = re.compile(r"^\s*Таблица\s+(\d+(?:\.\d+)?)\.?\s*(.*)$", re.IGNORECASE)
# латиница вперемешку с кириллицей внутри слова — признак подмены глифов
_MIXED = re.compile(r"[A-Za-z][А-Яа-я]|[А-Яа-я][A-Za-z]")
_SPACED = re.compile(r"\b\w \w \w")


def main():
    for base in sys.argv[1:]:
        pdf = os.path.join("data", "raw", base + ".pdf")
        caps = []
        with pdfplumber.open(pdf) as pf:
            for pno, page in enumerate(pf.pages, 1):
                txt = page.extract_text() or ""
                for ln in txt.splitlines():
                    m = _CAP.match(ln)
                    if m:
                        caps.append((pno, m.group(1), ln[:70]))
        print("=" * 70)
        print("%s  подписей «Таблица N»: %d" % (base, len(caps)))
        for pno, num, ln in caps:
            print("   p%-3d №%-5s %s" % (pno, num, ln))
        # порча: посчитать по всему документу
        with pdfplumber.open(pdf) as pf:
            full = "\n".join((p.extract_text() or "") for p in pf.pages)
        mixed = len(_MIXED.findall(full))
        spaced = len(_SPACED.findall(full))
        print("   шрифт-порча: mixed_lat_cyr=%d  letterspaced=%d" % (mixed, spaced))


if __name__ == "__main__":
    main()
