# -*- coding: utf-8 -*-
"""Диагностика типов порчи в извлечённом тексте (PyMuPDF-строки).

    python _corpus/diag_corrupt.py КР115_2
"""
import os, re, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from crparser.engine.pdf_reader import PdfReader

# тип 1: серия «одиночная буква + пробел» >=4 подряд (возможно с '_')
_SPACING = re.compile(r"(?:[А-Яа-яЁёA-Za-z_]\s+){4,}[А-Яа-яЁёA-Za-z]")
# тип 2: токен, целиком состоящий из удвоенных букв (>=6 симв)
_DOUBLED = re.compile(r"\b(?:([А-Яа-яЁё])\1){3,}\b")
# тип 3: смесь/непроизносимая кириллица (кандидаты глифовой подмены)
_MIXED = re.compile(r"[A-Za-z][А-Яа-яЁё]|[А-Яа-яЁё][A-Za-z]")
_BAD_BG = ("пб", "гб", "бг", "уо", "шуо", "апб", "агб", "пбаг", "оаг")


def main():
    for base in sys.argv[1:]:
        pdf = os.path.join("data", "raw", base + ".pdf")
        rd = PdfReader(pdf)
        pages = rd.read()
        rd.close()
        print("=" * 76)
        print(base, "  страниц:", len(pages))
        sp = db = gl = 0
        for p in pages:
            for ln in p.lines:
                t = ln.text
                if _SPACING.search(t):
                    if sp < 8:
                        print("  [SPACING p%d] %s" % (p.number, t[:80]))
                    sp += 1
                for m in _DOUBLED.finditer(t):
                    if db < 8:
                        print("  [DOUBLE  p%d] %r in: %s" % (p.number, m.group(0), t[:60]))
                    db += 1
                toks = t.split()
                bad = [tk for tk in toks
                       if re.fullmatch(r"[А-Яа-яЁё]{4,}", tk.strip(".,;:()"))
                       and sum(1 for b in _BAD_BG if b in tk.lower()) >= 1]
                if bad or _MIXED.search(t):
                    if gl < 8:
                        print("  [GLYPH?  p%d] %s" % (p.number, t[:80]))
                    gl += 1
        print("  ИТОГО: spacing-строк=%d  doubled-токенов=%d  glyph?-строк=%d" % (sp, db, gl))


if __name__ == "__main__":
    main()
