# -*- coding: utf-8 -*-
"""ВЕРИФИКАЦИЯ канала C5 (гейт 2 = pymorphy) по всему корпусу — Group B и Group C.

Group B (не испортить здоровое):
  * ни одной C5-правки на документах контрольной группы I1 (там честные шрифты -> гейт 1);
  * ни одной латинизации русского: (а) правило формы «7+ строчных кириллических»,
    (б) НЕЗАВИСИМЫЙ корпусный словарь русского (слово чисто встречается в >=3 док. baseline).

Group C (не потерять и не солгать):
  * owned_before ⊆ owned_after — спан, имевший владельца, обязан его сохранить (E2);
  * КАЖДАЯ замена имеет запись в провенансе latin_recovery (молчаливых замен нет);
  * ссылки [150, 151] и список литературы ЦЕЛЫ (решение команды: библиографию НЕ трогаем).

    python _corpus/verify_c5.py [outout_latin] [outout]
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LATIN = sys.argv[1] if len(sys.argv) > 1 else "outout_latin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "outout"
I1 = ["КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"]
_RU7 = re.compile(r"^[а-яё]{7,}$")
_W = re.compile(r"[^\s]+")
_MIN_DOCS = 3


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


def owned_spans(doc):
    """Спаны, имеющие владельца: sections ∪ excluded ∪ claimed таблиц (I13: E2 по
    СОХРАННОСТИ). C5 меняет ТЕКСТ, не структуру -> множество обязано совпасть."""
    out = set()

    def walk(ss):
        for s in ss:
            out.update(s.get("span_uids", []) or [])
            walk(s.get("children", []))
    walk(doc.get("sections", []))
    for t in doc.get("tables", []) or []:
        out.update(t.get("claimed_span_uids", []) or [])
    exc = doc.get("excluded", {}) or {}
    for key in ("appendices", "references", "toc", "front_matter", "other"):
        for it in (exc.get(key) or []):
            if isinstance(it, dict):
                out.update(it.get("span_uids", []) or [])
    return out


RU_VOCAB_JSON = os.path.join("_corpus", "corpus_ru_vocab.json")


def corpus_ru_vocab():
    """НЕЗАВИСИМЫЙ дискриминатор Group B: русское слово = чисто встречается в >=3
    ДОВЕРЕННЫХ документах (born-digital, честные шрифты; `_corpus/build_latin_vocab.py`).

    ИСТОЧНИК ОБЯЗАН БЫТЬ ДОВЕРЕННЫМ — ровно та же болезнь, что у LAT-словаря (I23).
    Словарь по ВСЕМУ baseline содержал `уегзиз`(=versus), `рпшагу`(=primary), `йозе`(=dose),
    `уап`(=Wang): битые шрифты рендерят ЛАТИНИЦУ КИРИЛЛИЦЕЙ, это повторяется в >=3 док. и
    становится «русским словом». Такой словарь объявлял КОРРЕКТНЫЕ починки (`уегзиз`->
    `versus`) нарушениями Group B — 99 ложных провалов. Из доверенных: 74390 -> 63716 слов,
    все пины-порчи ушли, `боль`/`пациентов`/`гепатоцеллюлярный` на месте."""
    try:
        d = json.load(open(RU_VOCAB_JSON, encoding="utf-8"))
        return set(d.get("words", []))
    except Exception as e:  # noqa: BLE001
        print("  (!) нет %s (%r) — собери: python _corpus/build_latin_vocab.py"
              % (RU_VOCAB_JSON, e))
        return set()


def main():
    print("Строю корпусный словарь русского по baseline (%s)…" % BASE)
    ru_vocab = corpus_ru_vocab()
    print("  словарь: %d слов (>=%d док.)\n" % (len(ru_vocab), _MIN_DOCS))

    n_doc = n_c5 = n_all = 0
    c5_docs = set()
    viol_form, viol_vocab, viol_i1 = [], [], []
    no_prov, lost_owner, ref_broken = [], [], []
    kinds = Counter()

    for p in sorted(glob.glob(os.path.join(LATIN, "*.json"))):
        base = os.path.splitext(os.path.basename(p))[0]
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print("  ! не читается %s: %r" % (base, e))
            continue
        n_doc += 1
        corr = (doc.get("latin_recovery", {}) or {}).get("corrections", [])
        n_all += len(corr)
        c5 = [c for c in corr if "c5_corrupt_font" in (c.get("why_suspect") or [])]
        n_c5 += len(c5)
        if c5:
            c5_docs.add(base)
        for c in c5:
            src, dst = c.get("source_text") or "", c.get("resolved_text") or ""
            kinds[c.get("decision")] += 1
            if _RU7.match(src):
                viol_form.append((base, src, dst))
            if src.lower() in ru_vocab:
                viol_vocab.append((base, src, dst))
            if not c.get("rule") or not c.get("method"):
                no_prov.append((base, src, dst))
        if base in I1 and c5:
            viol_i1.append((base, len(c5)))

        # --- Group C: owned_before ⊆ owned_after ---
        bp = os.path.join(BASE, base + ".json")
        if os.path.exists(bp):
            try:
                bdoc = json.load(open(bp, encoding="utf-8"))
            except Exception:
                bdoc = None
            if bdoc is not None:
                lost = owned_spans(bdoc) - owned_spans(doc)
                if lost:
                    lost_owner.append((base, len(lost)))
                # --- ссылки и библиография ЦЕЛЫ ---
                b_refs = (bdoc.get("excluded", {}) or {}).get("references") or []
                a_refs = (doc.get("excluded", {}) or {}).get("references") or []
                bl = sum(len((r or {}).get("text", "")) for r in b_refs)
                al = sum(len((r or {}).get("text", "")) for r in a_refs)
                if bl != al:
                    ref_broken.append((base, bl, al))

    def head(t):
        print("\n" + "=" * 72 + "\n%s\n" % t + "=" * 72)

    head("ОБЪЁМ")
    print("документов: %d | правок всего: %d | из них C5: %d | док. с C5: %d"
          % (n_doc, n_all, n_c5, len(c5_docs)))
    print("решения C5: %s" % dict(kinds))

    head("GROUP B — ЛАТИНИЗАЦИЯ РУССКОГО (обязан быть НОЛЬ)")
    print("(а) правило формы «7+ строчных кириллических»: %d" % len(viol_form))
    for v in viol_form[:10]:
        print("      ПРОВАЛ %s: %s -> %s" % v)
    print("(б) корпусный словарь русского (>=%d док., НЕЗАВИСИМ от гейтов): %d"
          % (_MIN_DOCS, len(viol_vocab)))
    for v in viol_vocab[:10]:
        print("      ПРОВАЛ %s: %s -> %s" % v)
    print("(в) C5-правки на контрольной группе I1 (гейт 1 обязан молчать): %d" % len(viol_i1))
    for v in viol_i1[:10]:
        print("      ПРОВАЛ %s: %d правок" % v)

    head("GROUP C — ПОТЕРЯ/ПРОВЕНАНС/ССЫЛКИ")
    print("owned_before ⊄ owned_after (E2, потеря владельца): %d док." % len(lost_owner))
    for v in lost_owner[:10]:
        print("      ПРОВАЛ %s: %d спанов осиротело" % v)
    print("замен без провенанса (rule/method): %d" % len(no_prov))
    print("документов с изменённой библиографией (обязан быть 0): %d" % len(ref_broken))
    for v in ref_broken[:10]:
        print("      ПРОВАЛ %s: references %d -> %d симв." % v)

    ok = not (viol_form or viol_vocab or viol_i1 or lost_owner or no_prov or ref_broken)
    head("ВЕРДИКТ")
    print("GROUP B/C: %s" % ("ЧИСТО" if ok else "ЕСТЬ НАРУШЕНИЯ (см. выше)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
