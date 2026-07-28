# -*- coding: utf-8 -*-
"""ОЦЕНКА УЩЕРБА: сколько term-замен в `outout_latin/` НЕ проходят ЧИСТЫЙ LAT-словарь.

ЗАЧЕМ. Гейт 3 («результат обязан быть настоящим латинским словом») сверялся со словарём,
собранным по ВСЕМУ baseline — включая сканы (текст = вывод full-OCR) и документы с битыми
шрифтами. Их артефакты (`nayuenmoe`=пациентов, `ypobehb`=Уровень, `ajit`=АЛТ) попали в
словарь и СТАЛИ «настоящими латинскими словами» для гейта. Значит гейт мог пропустить
замену РУССКОГО слова на транслит-мусор — молча, с decision=auto.

Замер отвечает: сколько таких замен УЖЕ лежит в outout_latin (ущерб ночного прогона), и
сколько из них auto (молча применены) против needs_review (человек бы поймал).

    python _corpus/audit_term_damage.py [outout_latin]
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

LATIN = sys.argv[1] if len(sys.argv) > 1 else "outout_latin"
VOCAB_JSON = os.path.join("crparser", "data", "latin_vocab.json")
_CYR = re.compile(r"[А-Яа-яЁё]")


def load_vocab():
    d = json.load(open(VOCAB_JSON, encoding="utf-8"))
    return set(d.get("words", [])), d.get("built_from", "(старый словарь, без метаданных)")


def main():
    vocab, origin = load_vocab()
    print("LAT-словарь: %d слов" % len(vocab))
    print("источник:    %s\n" % origin)

    n_docs = n_corr = 0
    term_total = 0
    by_decision = Counter()
    fails = []          # term-замены, чей результат НЕ в чистом словаре
    fails_auto = []
    for p in sorted(glob.glob(os.path.join(LATIN, "*.json"))):
        base = os.path.splitext(os.path.basename(p))[0]
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        n_docs += 1
        for c in (doc.get("latin_recovery", {}) or {}).get("corrections", []):
            n_corr += 1
            # term-канал = eng-OCR по кропу для ТЕРМИНА (не код: у кодов гейт — шаблон
            # сущности `entity_valid`, словарь там не при чём; не A2/контур — они
            # детерминированы и словаря не спрашивают).
            if c.get("method") != "ocr_eng":
                continue
            kind = c.get("entity_kind")
            if kind not in (None, "term"):
                continue
            rule = c.get("rule") or ""
            if "entity-gate" in rule or "roman-stage" in rule:
                continue
            dst = (c.get("resolved_text") or "").strip()
            if not dst or _CYR.search(dst):
                continue
            term_total += 1
            by_decision[c.get("decision")] += 1
            if dst.lower() not in vocab:
                rec = (base, c.get("source_text"), dst, c.get("decision"), rule[:34])
                fails.append(rec)
                if c.get("decision") == "auto":
                    fails_auto.append(rec)

    print("=" * 74)
    print("документов: %d | всего замен: %d | из них term-канал (гейт=словарь): %d"
          % (n_docs, n_corr, term_total))
    print("term по решениям: %s" % dict(by_decision))
    print("=" * 74)
    pct = (100.0 * len(fails) / term_total) if term_total else 0.0
    print("\nTERM-ЗАМЕН, НЕ ПРОХОДЯЩИХ ЧИСТЫЙ СЛОВАРЬ: %d  (%.2f%% от term-замен)"
          % (len(fails), pct))
    print("  из них decision=auto (применены МОЛЧА):   %d" % len(fails_auto))
    print("  из них needs_review (человек бы поймал):  %d" % (len(fails) - len(fails_auto)))

    print("\n--- ПРИМЕРЫ auto-замен, отвергаемых чистым словарём (до 40) ---")
    for r in fails_auto[:40]:
        print("   %-11s %-20s -> %-18s [%s] %s" % r)
    if not fails_auto:
        print("   НЕТ — ни одна МОЛЧАЛИВАЯ замена не отвергается чистым словарём")

    print("\n--- ПРИМЕРЫ needs_review, отвергаемых чистым словарём (до 20) ---")
    for r in [f for f in fails if f[3] != "auto"][:20]:
        print("   %-11s %-20s -> %-18s [%s] %s" % r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
