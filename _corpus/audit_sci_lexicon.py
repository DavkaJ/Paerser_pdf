#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""АУДИТ закрытого научного лексикона (правило 4: авто-набор правок аудируется против
источника ЦЕЛИКОМ, а не выборочно).

Перечисляет КАЖДОЕ вхождение, которое `latinrecovery.sci_lexicon_form` авторешает в
обучаемой зоне корпуса, с контекстом по границе сегмента (той же, что режет `_apply`),
и отдельно — вхождения, снятые гейтом TNM-региона.

    python _corpus/audit_sci_lexicon.py [корпус] > _corpus/_sci_lexicon_audit.txt

Только чтение. Запускать ПО КОРПУСУ ДО ПЕРЕСБОРКИ (в пересобранном порченых форм уже
нет — они заменены, и аудировать будет нечего).
"""
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crparser.engine.latinrecovery import (          # noqa: E402
    _SEG_RE, _strip_edges, is_tnm_region, sci_lexicon_form)


def _segments(text):
    """(смещение, ядро) по сегментам — ровно как их видит `_apply`."""
    out, pos = [], 0
    for i, part in enumerate(_SEG_RE.split(text)):
        part = part or ""
        if i % 2 == 0 and part:
            lead, core, _ = _strip_edges(part)
            if core:
                out.append((pos + len(lead), core))
        pos += len(part)
    return out


def _zones(d):
    """(метка, текст, tnm?) обучаемой зоны — sections/tables/appendices/metadata.title."""
    z = []

    def walk(secs):
        for s in secs or []:
            sid = s.get("section_id") or "?"
            t = s.get("text") or ""
            tnm = is_tnm_region(t)
            z.append(("section:%s/title" % sid, s.get("title") or "", tnm))
            z.append(("section:%s/text" % sid, t, tnm))
            walk(s.get("children"))
    walk(d.get("sections"))
    for t in d.get("tables") or []:
        blob = (t.get("raw_text") or "") + " " + (t.get("caption") or "")
        tnm = is_tnm_region(blob)
        z.append(("table:%s/raw" % t.get("number"), t.get("raw_text") or "", tnm))
        z.append(("table:%s/cap" % t.get("number"), t.get("caption") or "", tnm))
    for it in (d.get("excluded") or {}).get("appendices") or []:
        z.append(("appendix/text", it.get("text") or "", False))
        z.append(("appendix/title", it.get("title") or "", False))
    if (d.get("metadata") or {}).get("title"):
        z.append(("metadata/title", d["metadata"]["title"], False))
    return [(w, t, n) for w, t, n in z if t]


def main(corpus):
    hits, blocked, cnt = defaultdict(list), [], Counter()
    for fn in sorted(os.listdir(corpus)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(corpus, fn), encoding="utf-8") as fh:
            d = json.load(fh)
        zones = _zones(d)
        # tnm-контекст в движке ГЛОБАЛЬНЫЙ по документу (core, встреченный в TNM-зоне)
        doc_tnm = set()
        for _, txt, tnm in zones:
            if tnm:
                doc_tnm.update(c for _, c in _segments(txt))
        for where, txt, _ in zones:
            for pos, core in _segments(txt):
                lat = sci_lexicon_form(core)
                if not lat:
                    continue
                ctx = txt[max(0, pos - 75):pos + len(core) + 75].replace("\n", " ")
                if core in doc_tnm:
                    blocked.append((fn[:-5], where, core, lat, ctx))
                    continue
                cnt[(lat, core)] += 1
                hits[lat].append((fn[:-5], where, core, ctx))

    print("АВТО-ЗАМЕН: %d ; уникальных пар: %d ; форм: %d ; снято TNM-гейтом: %d"
          % (sum(cnt.values()), len(cnt), len(hits), len(blocked)))
    print()
    for (lat, core), n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])):
        print("  %-7s <- %-7s x%d" % (lat, core, n))
    print("\n=== СНЯТО ГЕЙТОМ TNM-РЕГИОНА ===")
    for doc, where, core, lat, ctx in blocked:
        print("  [%s] %s %r->%r\n      ...%s..." % (doc, where, core, lat, ctx))
    print("\n=== ВСЕ ЗАМЕНЫ С КОНТЕКСТОМ ===")
    for lat in sorted(hits):
        print("\n########## %s (%d) ##########" % (lat, len(hits[lat])))
        for doc, where, core, ctx in hits[lat]:
            print("  [%s] %s %r\n      ...%s..." % (doc, where, core, ctx))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "outout_latin_v2")
