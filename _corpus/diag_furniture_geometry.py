# -*- coding: utf-8 -*-
"""Диагностика (Phase 0.1, I30 #6): чем на самом деле являются «сироты», списанные
в page_furniture ДЛИННОЙ, а не геометрией.

Не чинит ничего. Перепарсивает выборку, для КАЖДОГО осиротевшего span'а (span_uid из
page_ir, не заявленный ни секцией, ни таблицей, ни excluded) печатает текст + геометрию
+ вердикт СТАРОГО (длинного) классификатора + кандидатные ГЕОМЕТРИЧЕСКИЕ сигналы.

Цель — увидеть глазами: сколько из 57 460 furniture-span'ов корпуса реально являются
колонтитулом/номером страницы (у края, повторяются), а сколько — короткое ТЕЛО (стадия
«II», уровень «5», ячейка «2.5») в центральной полосе, ошибочно списанное по длине.

    python _corpus/diag_furniture_geometry.py            # выборка по умолчанию
    python _corpus/diag_furniture_geometry.py КР1_4 …     # свои файлы
"""
from __future__ import annotations

import glob
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from crparser.engine.parser import DocumentParser          # noqa: E402
from crparser.profiles import create_profile               # noqa: E402
from crparser.engine.stats import _is_furniture, _RE_FURNITURE  # noqa: E402

RAW = os.path.join("data", "raw")
I1 = ["КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"]
# I12/I14-помеченные (реальный lost / continuation / МКБ) + TOC-номера + высокий furniture
FLAGGED = ["КР58_2", "КР1024_1", "КР895_1", "КР742_1", "КР931_1",
           "КР1_4", "КР628_2", "КР848_1",
           "КР569_2", "КР606_3", "КР479_2", "КР814_1"]

EDGE_FRAC = 0.10           # полоса «у края» — 10% высоты набора страницы сверху/снизу
REPEAT_PAGES = 3           # повтор на >= N страницах => колонтитул/номер-страничная зона


def _norm(t: str) -> str:
    return re.sub(r"\d+", "#", (t or "").strip().lower())


def owned_uids(pr):
    """span_uid'ы, заявленные хоть кем-то (секция/таблица/excluded)."""
    from crparser.engine.stats import _walk_sections
    owned = set()
    for s in _walk_sections(pr.sections):
        owned.update(s.span_uids or [])
    for t in pr.tables:
        owned.update(t.claimed_span_uids or [])
    for bucket in pr.excluded.values():
        for item in bucket:
            owned.update(item.get("span_uids", []) or [])
    return owned


def analyse(base, agg):
    pdf = os.path.join(RAW, base + ".pdf")
    if not os.path.exists(pdf):
        print("  НЕТ pdf:", base); return
    pr = _PARSER.parse(pdf)
    owned = owned_uids(pr)

    # геометрия по span_uid + полоса набора страницы (из ВСЕХ спанов страницы)
    geom = {}                                  # uid -> (page, bbox, text)
    page_y = defaultdict(lambda: [float("inf"), float("-inf")])
    for pir in pr.page_ir:
        for sp in pir.spans:
            x0, y0, x1, y1 = sp.bbox
            band = page_y[sp.page]
            band[0] = min(band[0], y0)
            band[1] = max(band[1], y1)
            t = sp.candidates.get(sp.selected, "") or ""
            if sp.span_uid not in geom or len(t) > len(geom[sp.span_uid][2]):
                geom[sp.span_uid] = (sp.page, sp.bbox, t)

    orphans = [u for u in geom if u not in owned]

    # повторяемость: нормализованный текст сироты на скольких страницах; и «край-слот»
    text_pages = defaultdict(set)
    edge_pages = defaultdict(set)              # (edge_side, x-корзина) -> страницы
    for u in orphans:
        page, bbox, t = geom[u]
        y0min, y1max = page_y[page]
        h = (y1max - y0min) or 1.0
        x0, y0, x1, y1 = bbox
        rel_top = (y0 - y0min) / h
        rel_bot = (y1max - y1) / h
        side = "top" if rel_top < EDGE_FRAC else ("bot" if rel_bot < EDGE_FRAC else None)
        text_pages[_norm(t)].add(page)
        if side:
            edge_pages[(side, round((x0 + x1) / 2 / 40))].add(page)

    # для контекста: все спаны страницы, отсортированные по y (текст + owned-флаг)
    page_spans = defaultdict(list)             # page -> [(y_center, text, owned?)]
    for pir in pr.page_ir:
        for sp in pir.spans:
            x0, y0, x1, y1 = sp.bbox
            tt = sp.candidates.get(sp.selected, "") or ""
            page_spans[sp.page].append(((y0 + y1) / 2, tt, sp.span_uid in owned))
    for p in page_spans:
        page_spans[p].sort(key=lambda r: r[0])

    def context(page, bbox):
        yc = (bbox[1] + bbox[3]) / 2
        col = page_spans[page]
        idx = min(range(len(col)), key=lambda i: abs(col[i][0] - yc)) if col else 0
        out = []
        for j in range(max(0, idx - 2), min(len(col), idx + 3)):
            mark = "*" if j == idx else ("o" if col[j][2] else " ")
            out.append("%s%r" % (mark, (col[j][1] or "")[:30]))
        return " | ".join(out)

    rows = []
    for u in orphans:
        page, bbox, t = geom[u]
        y0min, y1max = page_y[page]
        h = (y1max - y0min) or 1.0
        x0, y0, x1, y1 = bbox
        rel_top = (y0 - y0min) / h
        rel_bot = (y1max - y1) / h
        side = "top" if rel_top < EDGE_FRAC else ("bot" if rel_bot < EDGE_FRAC else None)
        at_edge = side is not None
        is_bare = bool(_RE_FURNITURE.match((t or "").strip()))
        is_short = len((t or "").strip()) <= 2
        txt_rep = len(text_pages[_norm(t)]) >= REPEAT_PAGES
        pos_rep = at_edge and len(edge_pages[(side, round((x0 + x1) / 2 / 40))]) >= REPEAT_PAGES
        old_furn = _is_furniture(t)
        rows.append(dict(uid=u, page=page, text=t, at_edge=at_edge, side=side,
                         is_bare=is_bare, is_short=is_short, txt_rep=txt_rep,
                         pos_rep=pos_rep, old_furn=old_furn,
                         rel_top=rel_top, rel_bot=rel_bot))
        # агрегаты
        agg["old_furn"] += old_furn
        agg["orphans"] += 1
        if old_furn:
            # старый=furniture; куда его денет геометрия?
            if not at_edge and not is_bare and not is_short:
                agg["furn_central_word"] += 1           # ЯВНО тело, списано по длине
                agg["ex_central_word"].append((base, page, t, context(page, bbox)))
            elif not at_edge and (is_bare or is_short):
                agg["furn_central_numeric"] += 1         # стадия/уровень/ячейка?
                agg["ex_central_numeric"].append((base, page, t, context(page, bbox)))
            elif at_edge and (is_bare or txt_rep or pos_rep):
                agg["furn_edge_confirmed"] += 1          # честная обвязка
            else:
                agg["furn_edge_oneoff"] += 1             # у края, но НЕ повтор — continuation?
                agg["ex_edge_oneoff"].append((base, page, t))

    n_old_furn = sum(1 for r in rows if r["old_furn"])
    print("\n== %-10s orphans=%d old_furniture=%d ==" % (base, len(orphans), n_old_furn))
    # показать сироты, где старый=furniture, но геометрия говорит «центр/не повтор»
    susp = [r for r in rows if r["old_furn"] and (
        (not r["at_edge"]) or (r["at_edge"] and not (r["is_bare"] or r["txt_rep"] or r["pos_rep"])))]
    for r in susp[:12]:
        print("   furn?→%-5s edge=%-4s bare=%d short=%d txt_rep=%d pos_rep=%d  %r"
              % ("LOST" if not r["at_edge"] else "edge1off",
                 r["side"], r["is_bare"], r["is_short"], r["txt_rep"], r["pos_rep"],
                 (r["text"][:40])))
    if len(susp) > 12:
        print("   ... ещё %d подозрительных" % (len(susp) - 12))


def main():
    global _PARSER
    registry = glob.glob("*.xlsx")[0]
    _PARSER = DocumentParser(create_profile("cr", registry))
    bases = sys.argv[1:] or (I1 + FLAGGED)
    agg = Counter()
    for k in ("ex_central_word", "ex_central_numeric", "ex_edge_oneoff"):
        agg[k] = []
    for b in bases:
        analyse(b, agg)

    print("\n" + "=" * 70)
    print("ИТОГ по выборке (%d файлов):" % len(bases))
    print("  сирот всего:              %d" % agg["orphans"])
    print("  старый=furniture:         %d" % agg["old_furn"])
    print("  -- разбивка furniture по геометрии --")
    print("  край + (bare/повтор):     %d   ← честная обвязка (номер стр./колонтитул)"
          % agg["furn_edge_confirmed"])
    print("  край, НЕ повтор:          %d   ← у края, но одиночный (continuation?)"
          % agg["furn_edge_oneoff"])
    print("  ЦЕНТР + число/коротк.:    %d   ← стадия/уровень/ячейка? (I30 #6)"
          % agg["furn_central_numeric"])
    print("  ЦЕНТР + слово:            %d   ← ЯВНО тело, списано по ДЛИНЕ"
          % agg["furn_central_word"])
    for label, key in (("ЦЕНТР+слово", "ex_central_word"),
                       ("ЦЕНТР+число", "ex_central_numeric")):
        ex = agg[key]
        if ex:
            print("\n  примеры [%s] (%d) — *=сирота o=owned, соседи по y:" % (label, len(ex)))
            for base, page, t, ctx in ex[:30]:
                print("     %-10s p%-3d %-8r  ctx: %s" % (base, page, t[:16], ctx))
    if agg["ex_edge_oneoff"]:
        print("\n  примеры [край-одиночка] (%d):" % len(agg["ex_edge_oneoff"]))
        for base, page, t in agg["ex_edge_oneoff"][:15]:
            print("     %-10s p%-3d %r" % (base, page, t[:50]))


if __name__ == "__main__":
    main()
