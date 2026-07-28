#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЗАМЕР класса «огрызок таблицы» (задача 3, 2026-07-28).

Три независимые проверки гипотезы «вырезана шапка, тело потеряно»:

  A. КРИТЕРИИ. Сколько таблиц ловит критерий ПО ДЛИНЕ (`raw_text < 200`) и сколько —
     по УЛИКЕ ДВИЖКА (`low_confidence` детекции или `row_count <= 1`), и насколько
     эти множества расходятся.
  B. BBOX vs ТЕКСТ. Содержит ли `raw_text` ВЕСЬ текст своего bbox (тогда извлечение
     внутри bbox ничего не теряет и «обрезка» — это про размер bbox, а не про дамп).
  C. РАМКА. Продолжаются ли векторные горизонтальные линии того же x-диапазона ниже
     bbox, и лежит ли под ним ТАБЛИЧНЫЙ текст (>=2 колоночных зазора >= 12pt в
     строке — считаем по словам, спаны склеивают строку и зазоров не видно).

    python _corpus/measure_table_stubs.py [корпус]

Только чтение.
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict

import fitz

NEAR, OVERLAP, GAP = 60.0, 0.6, 12.0
_TOK = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{3,}")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crparser.engine.latinnorm import _CYR2LAT   # noqa: E402


def _norm(tok: str) -> str:
    """casefold + кир-гомографы -> латиница. Без этого сравнение B даёт ЛОЖНЫЕ
    потери: `raw_text` уже прошёл latin recovery (`SaO2`), а текстовый слой PDF
    держит исходную форму (`SаO2` с кириллической `а`) — это одно и то же слово."""
    t = tok.casefold()
    return "".join(_CYR2LAT.get(c, _CYR2LAT.get(c.upper(), c).lower()) for c in t)


def is_stub(t):
    if t.get("low_confidence"):
        return True
    rc = t.get("row_count")
    return rc is not None and rc <= 1


def hlines(page):
    out = []
    try:
        for d in page.get_drawings():
            for it in d.get("items", []):
                if it[0] == "l":
                    p1, p2 = it[1], it[2]
                    if abs(p1.y - p2.y) < 1.0 and abs(p1.x - p2.x) >= 20:
                        out.append((min(p1.x, p2.x), max(p1.x, p2.x), p1.y))
                elif it[0] == "re":
                    r = it[1]
                    if r.width >= 20:
                        out.append((r.x0, r.x1, r.y0))
                        out.append((r.x0, r.x1, r.y1))
    except Exception:  # noqa: BLE001
        pass
    return out


def columnar(page, x0, x1, y0, y1):
    """(строк, из них ТАБЛИЧНЫХ) в полосе."""
    lines = defaultdict(list)
    for w in page.get_text("words"):
        if w[1] >= y0 and w[3] <= y1 and w[2] > x0 and w[0] < x1:
            lines[round(w[1] / 3)].append((w[0], w[2]))
    n = tab = 0
    for ws in lines.values():
        ws.sort()
        n += 1
        if sum(1 for i in range(1, len(ws)) if ws[i][0] - ws[i - 1][1] >= GAP) >= 2:
            tab += 1
    return n, tab


def main(corpus):
    tot = 0
    by_len = by_ev = both = 0
    docs_len, docs_ev = set(), set()
    verd = Counter()
    region_extra = Counter()
    for fn in sorted(os.listdir(corpus)):
        if not fn.endswith(".json"):
            continue
        d = json.load(open(os.path.join(corpus, fn), encoding="utf-8"))
        tables = d.get("tables") or []
        tot += len(tables)
        shorts = []
        for t in tables:
            L = len(t.get("raw_text") or "")
            ev = is_stub(t)
            if L < 200:
                by_len += 1
                docs_len.add(fn)
                shorts.append(t)
            if ev:
                by_ev += 1
                docs_ev.add(fn)
            if L < 200 and ev:
                both += 1
        if not shorts:
            continue
        p = os.path.join("data", "raw", fn[:-5] + ".pdf")
        pdf = fitz.open(p) if os.path.isfile(p) else None
        if pdf is None:
            continue
        cache = {}
        for t in shorts:
            pno = (t.get("page") or 1) - 1
            if not (0 <= pno < len(pdf)):
                continue
            page = pdf[pno]
            x0, y0, x1, y1 = (t.get("bbox") or [0, 0, 0, 0])[:4]
            # B: текст области bbox vs raw_text
            reg = Counter(_norm(w) for w in _TOK.findall(
                page.get_text("text", clip=fitz.Rect(x0, y0, x1, y1))))
            raw = Counter(_norm(w) for w in _TOK.findall(t.get("raw_text") or ""))
            region_extra[sum((reg - raw).values()) > 0] += 1
            # C: рамка + табличность полосы под bbox
            if pno not in cache:
                cache[pno] = hlines(page)
            cont = 0
            for lx0, lx1, ly in cache[pno]:
                if y1 + 2 <= ly <= y1 + NEAR:
                    ov = min(lx1, x1) - max(lx0, x0)
                    if ov > 0 and ov / max(x1 - x0, 1) >= OVERLAP:
                        cont += 1
            if not cont:
                verd["рамка кончается на bbox"] += 1
                continue
            covered = any(
                o is not t and o.get("page") == t.get("page")
                and min(x1, (o.get("bbox") or [0, 0, 0, 0])[2]) > max(x0, (o.get("bbox") or [0, 0, 0, 0])[0])
                and min(y1 + NEAR, (o.get("bbox") or [0, 0, 0, 0])[3]) > max(y1 + 1, (o.get("bbox") or [0, 0, 0, 0])[1])
                for o in tables)
            if covered:
                verd["рамка ниже накрыта ДРУГИМ объектом (расщепление)"] += 1
                continue
            _, tab = columnar(page, x0, x1, y1 + 1, min(y1 + NEAR, page.rect.y1 - 30))
            verd["ОБРЕЗКА подтверждена (>=2 табличных строк ниже)" if tab >= 2
                 else "рамка продолжается, но ниже НЕ таблица (проза/сноска)"] += 1
        pdf.close()

    print("таблиц всего: %d" % tot)
    print("A. критерий ПО ДЛИНЕ (<200):        %5d таблиц в %d док." % (by_len, len(docs_len)))
    print("   критерий ПО УЛИКЕ движка:        %5d таблиц в %d док." % (by_ev, len(docs_ev)))
    print("   пересечение:                     %5d" % both)
    print("   ложно флагнуто длиной:           %5d" % (by_len - both))
    print("   пропущено длиной (есть улика):   %5d" % (by_ev - both))
    print("\nB. raw_text содержит ВЕСЬ текст своего bbox: %d ; теряет: %d"
          % (region_extra[False], region_extra[True]))
    print("\nC. что под нижней границей bbox у коротких таблиц:")
    for k, v in verd.most_common():
        print("   %-52s %d" % (k, v))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "outout_latin_v2")
