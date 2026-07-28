# -*- coding: utf-8 -*-
"""Показать строки-кандидаты в главы/подразделы: римские и арабские номера в
начале строки, с size/bold и ведущими пробелами (из PyMuPDF, как видит парсер).

    python _corpus/diag_chapters.py КР66_4 КР662_2
"""
import os, re, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fitz
from crparser.engine.pdf_reader import PdfReader

_ROMAN = re.compile(r"^\s*([IVXLC]{1,5})\s*[.)]\s+(.+)$")
_ARAB = re.compile(r"^\s*(\d{1,2})(?:\s*\.\s*\d+){0,3}\s*\.?\s+(.+)$")


def main():
    for base in sys.argv[1:]:
        pdf = os.path.join("data", "raw", base + ".pdf")
        rd = PdfReader(pdf)
        pages = rd.read()
        body = rd.body_size(pages)
        rd.close()
        print("=" * 78)
        print("%s  body_size=%.1f" % (base, body))
        shown = 0
        for p in pages:
            for ln in p.lines:
                raw = ln.text
                mr = _ROMAN.match(raw)
                ma = _ARAB.match(raw)
                if not (mr or ma):
                    continue
                # интересуют «главные»: римские ИЛИ верхнеуровневые арабские 1..9
                tag = ""
                if mr:
                    tag = "ROMAN " + mr.group(1)
                else:
                    num = ma.group(1)
                    tag = "ARAB  " + num
                vis = "V" if (ln.size >= body * 1.35 or ln.bold) else " "
                print("  p%-3d sz=%4.1f %s %-10s | %s" %
                      (p.number, ln.size, vis, tag, raw[:66]))
                shown += 1
                if shown > 120:
                    return


if __name__ == "__main__":
    main()
