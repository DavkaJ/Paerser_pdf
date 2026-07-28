# -*- coding: utf-8 -*-
"""Верификация КОНТРАКТА после фикса (внешнее ревю): в корпусе применены ТОЛЬКО auto,
needs_review — предложения (текст не тронут), гейт LATIN_UNRESOLVED сработал.

    python _corpus/verify_apply_contract.py
"""
from __future__ import annotations
import glob
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LATIN = "outout_latin"
_RU7 = re.compile(r"^[а-яё]{7,}$")


def zone_text(doc):
    parts = []

    def walk(ss):
        for s in ss:
            parts.append((s.get("title") or "") + " " + (s.get("text") or ""))
            walk(s.get("children", []))
    walk(doc.get("sections", []))
    for t in doc.get("tables", []) or []:
        parts.append((t.get("caption") or "") + " " + (t.get("raw_text") or ""))
    for it in (doc.get("excluded", {}) or {}).get("appendices", []) or []:
        parts.append((it.get("title") or "") + " " + (it.get("text") or ""))
    return "\n".join(parts)


def main():
    n_doc = 0
    dec = Counter()
    applied_flag_bad = 0
    nr_leaked = []              # needs_review, чей источник ИСЧЕЗ из текста (=применён, БАГ)
    auto_hit = 0
    auto_tot = 0
    crit_docs = 0
    for p in sorted(glob.glob(os.path.join(LATIN, "*.json"))):
        base = os.path.splitext(os.path.basename(p))[0]
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        n_doc += 1
        lr = doc.get("latin_recovery") or {}
        corr = lr.get("corrections") or []
        if lr.get("unresolved_critical"):
            crit_docs += 1
        zone = zone_text(doc)
        for c in corr:
            d = c.get("decision")
            dec[d] += 1
            # applied-флаг соответствует decision
            if c.get("applied") != (d == "auto"):
                applied_flag_bad += 1
            src = c.get("source_text") or ""
            if d == "needs_review":
                # источник ОБЯЗАН остаться в тексте (не применён)
                if src and src not in zone:
                    nr_leaked.append((base, src, c.get("resolved_text")))
            elif d == "auto":
                auto_tot += 1
                if (c.get("resolved_text") or "") in zone:
                    auto_hit += 1

    print("=" * 68)
    print("КОНТРАКТ ПРИМЕНЕНИЯ (после фикса)")
    print("=" * 68)
    print("документов: %d" % n_doc)
    print("коррекций по decision: %s" % dict(dec))
    print()
    print("provenance.applied != (decision==auto): %d  (обязан 0)" % applied_flag_bad)
    print("needs_review, чей ИСТОЧНИК ИСЧЕЗ из текста (=применён, БАГ): %d  (обязан 0)"
          % len(nr_leaked))
    for r in nr_leaked[:10]:
        print("     ПРОВАЛ %s: %s -> %s" % r)
    print("auto-результатов реально в тексте: %d / %d (применены)" % (auto_hit, auto_tot))
    print()
    print("документов с unresolved_critical (-> LATIN_UNRESOLVED REVIEW): %d" % crit_docs)
    ok = (applied_flag_bad == 0 and not nr_leaked)
    print()
    print("ВЕРДИКТ КОНТРАКТА: %s" % ("ЧИСТО" if ok else "НАРУШЕН (см. выше)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
