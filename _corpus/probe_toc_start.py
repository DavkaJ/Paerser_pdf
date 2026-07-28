# -*- coding: utf-8 -*-
"""Проба (Phase 1 ШАГ 2): почему content_start попал ВНУТРЬ оглавления (START_IN_TOC).

Инструментирует реальный Segmenter._find_content_start: перехватывает lines+возврат,
печатает окрестность старта, маркер «Оглавление», строки с точками-лидерами и строки с
хвостом-номером-страницы. Показывает, ГДЕ кончается TOC и почему старт сел раньше.

    python _corpus/probe_toc_start.py КР715_2 КР620_3
"""
from __future__ import annotations

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from crparser.engine.segmenter import Segmenter                 # noqa: E402
from crparser.engine.parser import DocumentParser               # noqa: E402
from crparser.profiles import create_profile                    # noqa: E402

_RE_LEADER = re.compile(r"\.{3,}|_{3,}|…+|‥+|․{2,}")
_RE_TOC_TAIL = re.compile(r"(\.{2,}\s*\d{1,4}|\s\d{1,4})\s*$")
_RE_TOC_MARK = re.compile(r"^\s*(оглавление|содержание)\s*$", re.IGNORECASE)

CAP = {}
_orig = Segmenter._find_content_start


def _wrapped(self, lines):
    start = _orig(self, lines)
    CAP["lines"] = lines
    CAP["start"] = start
    CAP["spec"] = self._spec
    return start


Segmenter._find_content_start = _wrapped


def probe(base, parser):
    CAP.clear()
    parser.parse(os.path.join("data", "raw", base + ".pdf"))
    lines = CAP["lines"]
    start = CAP["start"]
    n = len(lines)
    toc_mark = [k for k in range(n) if _RE_TOC_MARK.match(lines[k].text.strip())]
    leaders = [k for k in range(n) if _RE_LEADER.search(lines[k].text)]
    tails = [k for k in range(n)
             if _RE_TOC_TAIL.search(lines[k].text.strip())
             and re.search(r"[А-Яа-яA-Za-z]", lines[k].text)]
    print("\n==== %s  (всего строк %d) ====" % (base, n))
    print("  content_start = %d" % start)
    print("  маркер «Оглавление/Содержание» на строках: %s" % toc_mark[:5])
    print("  строк с точками-лидерами: %d (первые idx %s)" % (len(leaders), leaders[:8]))
    print("  строк с хвостом-номером-страницы: %d (первые idx %s)" % (len(tails), tails[:8]))
    lo = max(0, start - 6)
    print("  --- строки [%d..%d) вокруг старта (*=старт, L=лидер, T=хвост) ---" % (lo, start + 8))
    for k in range(lo, min(n, start + 8)):
        t = lines[k].text.strip()
        mark = "*" if k == start else " "
        flags = ("L" if _RE_LEADER.search(t) else " ") + ("T" if _RE_TOC_TAIL.search(t) else " ")
        print("   %s%s %4d: %r" % (mark, flags, k, t[:70]))


def main():
    registry = glob.glob("*.xlsx")[0]
    parser = DocumentParser(create_profile("cr", registry))
    for b in (sys.argv[1:] or ["КР715_2", "КР620_3"]):
        probe(b, parser)


if __name__ == "__main__":
    main()
