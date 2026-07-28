# -*- coding: utf-8 -*-
"""Прогон fallback-детектора с печатью причины по каждой подписи.

    python _corpus/diag_fb.py КР119_3
"""
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pdfplumber
from crparser.engine import tables as T


def main():
    for base in sys.argv[1:]:
        pdf = os.path.join("data", "raw", base + ".pdf")
        print("=" * 74)
        print(base)
        with pdfplumber.open(pdf) as pf:
            for pno, page in enumerate(pf.pages):
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                if not words:
                    continue
                rows = T._group_rows(words)
                cap_idx = [i for i, r in enumerate(rows)
                           if T._RE_TABLE_CAPTION.match(T._row_text(r))]
                if not cap_idx:
                    continue
                for k, ci in enumerate(cap_idx):
                    stop = cap_idx[k + 1] if k + 1 < len(cap_idx) else len(rows)
                    cap = T._row_text(rows[ci])[:48]
                    body, capi = T._region_body(rows, ci, stop)
                    if len(body) < 3:
                        print("  p%-3d SKIP(body<3 =%d) %s" % (pno + 1, len(body), cap))
                        continue
                    btext = " ".join(T._row_text(r) for r in body)
                    if T._looks_font_corrupted(btext):
                        print("  p%-3d CORRUPT          %s" % (pno + 1, cap))
                        continue
                    cuts0, rr0 = T._column_cuts(body, strict=False)
                    if not cuts0 or rr0 < 3:
                        print("  p%-3d SKIP(cuts0=%d rr0=%d win=%d) %s"
                              % (pno + 1, len(cuts0), rr0, len(body), cap))
                        continue
                    trimmed = T._trim_to_columns(body, cuts0)
                    if len(trimmed) < 3:
                        print("  p%-3d SKIP(trim=%d<3 win=%d) %s"
                              % (pno + 1, len(trimmed), len(body), cap))
                        continue
                    cuts, rr = T._column_cuts(trimmed, strict=True)
                    if not cuts or rr < 3:
                        print("  p%-3d SKIP(cuts=%d rr=%d trim=%d) %s"
                              % (pno + 1, len(cuts), rr, len(trimmed), cap))
                        continue
                    txt, lc = T._build_grid_text(trimmed, cuts)
                    print("  p%-3d OK cols=%d rr=%d win=%d trim=%d low=%s %s"
                          % (pno + 1, len(cuts) + 1, rr, len(body), len(trimmed), lc, cap))


if __name__ == "__main__":
    main()
