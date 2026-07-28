# -*- coding: utf-8 -*-
"""Прототип нормализаторов порчи — калибровка на КР115_2 (порча) и КР66_4 (чистый).

    python _corpus/proto_norm.py
"""
import os, re, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from crparser.engine.pdf_reader import PdfReader

_LETTER = re.compile(r"[А-Яа-яЁёA-Za-z]")
_ARTIFACT = set("_„“”‘’^™°|")


def is_single_letter(tok):
    return len(tok) == 1 and bool(_LETTER.match(tok))


def collapse_letterspacing(text, min_run=4):
    """Схлопнуть серии одиночных букв (>=min_run), убрать '_'-артефакты внутри."""
    toks = text.split(" ")
    out, i, n_fixed = [], 0, 0
    while i < len(toks):
        # набрать максимальную серию: одиночная буква ИЛИ одиночный артефакт
        j = i
        letters = 0
        while j < len(toks):
            t = toks[j]
            if is_single_letter(t):
                letters += 1
                j += 1
            elif len(t) == 1 and t in _ARTIFACT:
                j += 1
            else:
                break
        if letters >= min_run:
            word = "".join(t for t in toks[i:j] if is_single_letter(t))
            out.append(word)
            n_fixed += 1
            i = j
        else:
            out.append(toks[i])
            i += 1
    return " ".join(out), n_fixed


def main():
    for base in ("КР115_2", "КР66_4"):
        rd = PdfReader(os.path.join("data", "raw", base + ".pdf"))
        pages = rd.read(); rd.close()
        total = 0
        samples = []
        for p in pages:
            for ln in p.lines:
                new, k = collapse_letterspacing(ln.text)
                if k:
                    total += k
                    if len(samples) < 12:
                        samples.append((p.number, ln.text, new))
        print("=" * 76)
        print("%s: строк со схлопыванием=%d" % (base, total))
        for pn, old, new in samples:
            print("  p%d OLD: %s" % (pn, old[:75]))
            print("        NEW: %s" % new[:75])


if __name__ == "__main__":
    main()
