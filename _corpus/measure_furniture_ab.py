# -*- coding: utf-8 -*-
"""A/B-замер geometry-furniture БЕЗ дрейфа (Phase 0.1, I30 #6).

Изолирует ТОЛЬКО правку классификатора: перепарс один раз (Jul-20 код), для КАЖДОГО
дока берём ОДИН и тот же набор сирот + геометрию и применяем ДВА классификатора —
СТАРЫЙ (по длине: `len<=2 or bare`) и НОВЫЙ (геометрия). Так дельта furniture/lost —
чисто от правки, а не от изменений движка между baseline-прогоном и сейчас.

    python _corpus/measure_furniture_ab.py
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW = os.path.join("data", "raw")
LOST_SPANS_REVIEW = 0.02
I1 = {"КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"}
_PARSER = None


def _init(registry):
    global _PARSER
    from crparser.engine.parser import DocumentParser
    from crparser.profiles import create_profile
    _PARSER = DocumentParser(create_profile("cr", registry))


def _old_is_furniture(t, _RE):
    """Классификатор ДО Phase 0.1 (по длине)."""
    t = (t or "").strip()
    if not t:
        return True
    if len(t) <= 2:
        return True
    return bool(_RE.match(t))


def work(base):
    from crparser.engine.stats import _is_furniture, _RE_FURNITURE, _walk_sections
    from crparser.engine.stats import _FURNITURE_EDGE_FRAC, _FURNITURE_REPEAT_PAGES
    pdf = os.path.join(RAW, base + ".pdf")
    try:
        pr = _PARSER.parse(pdf)
    except Exception as exc:  # noqa: BLE001
        return base, {"error": repr(exc)}

    owned = set()
    for s in _walk_sections(pr.sections):
        owned.update(s.span_uids or [])
    for t in pr.tables:
        owned.update(t.claimed_span_uids or [])
    for bucket in pr.excluded.values():
        for item in bucket:
            owned.update(item.get("span_uids", []) or [])

    geom, text, band = {}, {}, {}
    for pir in pr.page_ir:
        for sp in pir.spans:
            tt = sp.candidates.get(sp.selected, "") or ""
            if sp.span_uid not in text or len(tt) > len(text[sp.span_uid]):
                text[sp.span_uid] = tt
            geom.setdefault(sp.span_uid, (sp.page, sp.bbox))
            lo, hi = band.get(sp.page, (float("inf"), float("-inf")))
            band[sp.page] = (min(lo, sp.bbox[1]), max(hi, sp.bbox[3]))

    orphans = [u for u in geom if u not in owned]
    total_spans = len(owned | set(geom))       # |S| — знаменатель гейта LOST_CONTENT
    orphan_pages = {}
    for u in orphans:
        orphan_pages.setdefault((text.get(u, "") or "").strip(), set()).add(geom[u][0])

    def at_edge(u):
        page, bbox = geom[u]
        lo, hi = band[page]
        h = (hi - lo) or 1.0
        return (bbox[1] - lo) / h < _FURNITURE_EDGE_FRAC or (hi - bbox[3]) / h < _FURNITURE_EDGE_FRAC

    old_f = old_l = new_f = new_l = 0
    new_lost_uids = []
    for u in orphans:
        t = text.get(u, "")
        rep = len(orphan_pages.get((t or "").strip(), ())) >= _FURNITURE_REPEAT_PAGES
        if _old_is_furniture(t, _RE_FURNITURE):
            old_f += 1
        else:
            old_l += 1
        if _is_furniture(t, at_edge=at_edge(u), repeats=rep):
            new_f += 1
        else:
            new_l += 1
            new_lost_uids.append(u)
    return base, {"total": len(orphans), "total_spans": total_spans,
                  "old_f": old_f, "old_l": old_l, "new_f": new_f, "new_l": new_l,
                  "new_lost_uids": sorted(new_lost_uids)[:6]}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    registry = glob.glob("*.xlsx")[0]
    classmap = {}
    for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8")):
        classmap[os.path.splitext(r["file"])[0]] = r["class"]
    bases = sorted(b for b, c in classmap.items() if c in ("OK", "LOW_TEXT_REVIEW"))
    print("A/B перепарс %d док…" % len(bases))

    t0 = time.time()
    res = {}
    with ProcessPoolExecutor(max_workers=14, initializer=_init, initargs=(registry,)) as ex:
        for i, (b, d) in enumerate(ex.map(work, bases), 1):
            res[b] = d
            if i % 150 == 0:
                print("  ...%d/%d (%.0fs)" % (i, len(bases), time.time() - t0))

    ok = {b: d for b, d in res.items() if "error" not in d}
    OF = sum(d["old_f"] for d in ok.values()); OL = sum(d["old_l"] for d in ok.values())
    NF = sum(d["new_f"] for d in ok.values()); NL = sum(d["new_l"] for d in ok.values())
    print("\n== ИЗОЛИРОВАННАЯ дельта (одни и те же сироты, два классификатора) ==")
    print("  сирот всего: %d" % sum(d["total"] for d in ok.values()))
    print("  furniture: OLD %d -> NEW %d  (%+d)" % (OF, NF, NF - OF))
    print("  lost:      OLD %d -> NEW %d  (%+d)" % (OL, NL, NL - OL))

    moved_to_lost = [(b, d["old_l"], d["new_l"], d["total"], d["new_lost_uids"])
                     for b, d in ok.items() if d["new_l"] != d["old_l"]]
    moved_to_lost.sort(key=lambda x: -(x[2] - x[1]))
    print("\n== док., где lost изменился (NEW vs OLD, оба на тех же сиротах): %d ==" % len(moved_to_lost))
    for b, ol, nl, tot, uids in moved_to_lost[:25]:
        print("  %-10s lost %d -> %d (%+d)  orphans=%d  I1=%s"
              % (b, ol, nl, nl - ol, tot, b in I1))

    # ГЕЙТ LOST_CONTENT: знаменатель — total_spans (|S|), НЕ число сирот.
    crossed = [b for b, d in ok.items()
               if d["total_spans"] and d["new_l"] / d["total_spans"] > LOST_SPANS_REVIEW]
    print("\n== NEW lost/total_spans > 0.02 (=> LOST_CONTENT REVIEW): %d ==" % len(crossed))
    for b in crossed:
        d = ok[b]
        print("  %-10s %d/%d = %.3f" % (b, d["new_l"], d["total_spans"], d["new_l"] / d["total_spans"]))

    print("\n== контрольная I1 (old_l/new_l обязаны совпадать и быть малы) ==")
    for b in sorted(I1):
        d = ok.get(b, {})
        print("  %-10s total=%d  furn %d->%d  lost %d->%d"
              % (b, d.get("total", 0), d.get("old_f", 0), d.get("new_f", 0),
                 d.get("old_l", 0), d.get("new_l", 0)))
    print("\n(%.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    main()
