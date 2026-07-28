# -*- coding: utf-8 -*-
"""Для каждого региона-кандидата (от подписи «Таблица N» до след. подписи/конца
страницы) посчитать сигналы шрифтовой порчи на ТЕЛЕ таблицы. Цель — найти один
сигнал, отделяющий КР330_2 (порча) от КР176_2/КР119_3/КР588_3 (норма).

    python _corpus/diag_signals.py КР176_2 КР330_2 КР119_3 КР588_3
"""
import os, re, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pdfplumber

_CAP = re.compile(r"^\s*Таблица\s+(П?[\dЗ]+(?:[.,][\dЗ]+)*)\.?\s*(.*)$", re.IGNORECASE)
_MIXED = re.compile(r"[A-Za-z][А-Яа-яЁё]|[А-Яа-яЁё][A-Za-z]")
_LATIN = re.compile(r"[A-Za-z]")
_VOWEL = set("аеиоуыэюяёAEIOUYaeiouy")
_JUNK = re.compile(r"[\\|§¶]")


def suspect_cyr(tok):
    """Кириллический токен, похожий на латиницу через битый cmap.
    Признак: чередование редких согласных без гласных-паттерна И длина>=5,
    либо наличие 'битых' биграмм типа 'пб','гб','уо','ш§'."""
    t = tok.strip(".,;:()[]«»\"'-—%<>")
    if len(t) < 4:
        return False
    if not re.fullmatch(r"[А-Яа-яЁё]+", t):
        return False
    bad = ("пб", "гб", "бг", "уо", "оа", "апб", "агб", "шуо", "пбаг")
    return any(b in t.lower() for b in bad)


def main():
    for base in sys.argv[1:]:
        pdf = os.path.join("data", "raw", base + ".pdf")
        print("=" * 74)
        print(base)
        with pdfplumber.open(pdf) as pf:
            for pno, page in enumerate(pf.pages, 1):
                lines = (page.extract_text() or "").splitlines()
                cap_idx = [i for i, ln in enumerate(lines) if _CAP.match(ln)]
                if not cap_idx:
                    continue
                for k, ci in enumerate(cap_idx):
                    end = cap_idx[k + 1] if k + 1 < len(cap_idx) else len(lines)
                    body = lines[ci + 1:end]
                    btext = " ".join(body)
                    toks = btext.split()
                    mixed = len(_MIXED.findall(btext))
                    latin = sum(1 for t in toks if _LATIN.search(t))
                    junk = len(_JUNK.findall(btext))
                    susp = sum(1 for t in toks if suspect_cyr(t))
                    ntok = max(1, len(toks))
                    cap = lines[ci][:40]
                    print("  p%-3d %-22s rows=%-3d tok=%-4d mixed=%-3d latin=%-3d junk=%-2d susp=%-3d susp%%=%.1f"
                          % (pno, cap, len(body), len(toks), mixed, latin, junk, susp,
                             100 * susp / ntok))


if __name__ == "__main__":
    main()
