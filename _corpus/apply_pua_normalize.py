# -*- coding: utf-8 -*-
"""Применить PUA-нормализацию (символьный шрифт -> Unicode, Adobe Symbol) к тексту корпуса
outout_latin. Детерминированный char-map (все Symbol-значения — 1 символ -> длина сохраняется ->
stats не плывут, OCR-дрейфа НЕТ, не-PUA контент байт-в-байт цел — сильнее reparse шага 1).

Аудит (airtight): для КАЖДОГО дока char-by-char — всякое отличие old->new = покрытая PUA ->
её Symbol-символ; не-PUA позиции не тронуты. Непокрытые PUA (вне Symbol) ОСТАЮТСЯ + needs_review
провенанс (не дропаем молча). Идемпотентно: пропускает доки с уже проставленным pua_normalize.
Провенанс -> latin_recovery.corrections (source=pua_normalize). I1 — печатается отдельно.

    python _corpus/apply_pua_normalize.py [--go]   (без --go: только замер, ничего не пишет)
"""
import json, os, sys, collections, copy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from crparser.engine.latinrecovery import _pua_fix
from crparser.engine.pua_symbol import symbol_char

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAT = os.path.join(ROOT, "outout_latin")
I1 = {"КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"}
TEXT_KEYS = ("sections", "tables", "excluded", "metadata")


def fix_tree(o, cov, unc):
    """Рекурсивно чинит все строки в поддереве (dict/list). Возврат: новое дерево."""
    if isinstance(o, str):
        nv, c, u = _pua_fix(o)
        cov.update(c); unc.update(u)
        return nv
    if isinstance(o, dict):
        return {k: fix_tree(v, cov, unc) for k, v in o.items()}
    if isinstance(o, list):
        return [fix_tree(v, cov, unc) for v in o]
    return o


def verify(old, new):
    """char-by-char: всякое отличие = покрытая-PUA -> её Symbol-символ; иначе список нарушений."""
    bad = []

    def w(a, b, path):
        if isinstance(a, str):
            if a == b:
                return
            if len(a) != len(b):
                bad.append((path, "len", a[:20], b[:20])); return
            for i, (ca, cb) in enumerate(zip(a, b)):
                if ca != cb:
                    if not (0xE000 <= ord(ca) <= 0xF8FF and symbol_char(ord(ca)) == cb):
                        bad.append((path, "char", hex(ord(ca)), repr(cb)))
        elif isinstance(a, dict):
            for k in a:
                w(a[k], b.get(k), path + "." + str(k))
        elif isinstance(a, list):
            for i, (x, y) in enumerate(zip(a, b)):
                w(x, y, path + "[%d]" % i)
    w(old, new, "")
    return bad


def main():
    go = "--go" in sys.argv
    tot_cov = collections.Counter(); tot_unc = collections.Counter()
    docs_changed = 0; docs_skipped = 0; bad_docs = []
    i1_report = {}
    files = sorted(__import__("glob").glob(os.path.join(LAT, "КР*.json")))
    for fp in files:
        base = os.path.basename(fp)[:-5]
        doc = json.load(open(fp, encoding="utf-8"))
        corr = (doc.get("latin_recovery") or {}).get("corrections", []) or []
        if any(c.get("source") == "pua_normalize" for c in corr):
            docs_skipped += 1; continue                 # идемпотентность
        cov, unc = collections.Counter(), collections.Counter()
        subtree = {k: doc.get(k) for k in TEXT_KEYS}
        fixed = fix_tree(subtree, cov, unc)
        if not cov and not unc:
            continue
        b = verify(subtree, fixed)
        if b:
            bad_docs.append((base, b[:5])); continue    # НЕ писать грязный
        tot_cov.update(cov); tot_unc.update(unc)
        docs_changed += 1
        if base in I1:
            i1_report[base] = {"covered": sum(cov.values()), "unresolved": sum(unc.values())}
        if go and cov:                                   # пишем только если есть покрытые замены
            for k in TEXT_KEYS:
                if k in fixed:
                    doc[k] = fixed[k]
            block = doc.get("latin_recovery") or {"corrections": [], "unresolved_critical": []}
            for o, n in sorted(cov.items()):
                block["corrections"].append({
                    "source_text": "U+%04X" % o, "resolved_text": symbol_char(o),
                    "method": "pua_normalize", "rule": "adobe_symbol", "confidence": 1.0,
                    "entity_kind": "symbol", "is_critical": False, "decision": "auto",
                    "why_suspect": ["pua_glyph"], "source": "pua_normalize",
                    "applied": True, "occurrences": n})
            for o, n in sorted(unc.items()):
                block["corrections"].append({
                    "source_text": "U+%04X" % o, "resolved_text": None,
                    "method": "pua_normalize", "rule": "not_in_symbol_table", "confidence": 0.0,
                    "entity_kind": "symbol", "is_critical": False, "decision": "needs_review",
                    "why_suspect": ["pua_glyph", "outside_symbol_table"], "source": "pua_normalize",
                    "applied": False, "occurrences": n})
            doc["latin_recovery"] = block
            tmp = fp + ".tmp"
            json.dump(doc, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            os.replace(tmp, fp)

    print("=" * 70)
    print("PUA-НОРМАЛИЗАЦИЯ %s" % ("(ПРИМЕНЕНО)" if go else "(ЗАМЕР, не писал)"))
    print("=" * 70)
    print("докумантов затронуто: %d (пропущено уже-нормализованных: %d)" % (docs_changed, docs_skipped))
    print("НОРМАЛИЗОВАНО (Symbol): %d вхождений, %d кодпоинтов" % (sum(tot_cov.values()), len(tot_cov)))
    print("НЕПОКРЫТО (needs_review, оставлено): %d вхождений, %d кодпоинтов"
          % (sum(tot_unc.values()), len(tot_unc)))
    print("\nтоп нормализованных:")
    for o, n in tot_cov.most_common(12):
        print("   U+%04X x%-6d -> %r" % (o, n, symbol_char(o)))
    print("\nОСТАТОК непокрытых кодпоинтов (нужен per-font/human):")
    for o, n in tot_unc.most_common():
        print("   U+%04X x%-5d" % (o, n))
    print("\nконтрольная I1:", i1_report if i1_report else "(в I1 нет PUA — байт-в-байт)")
    if bad_docs:
        print("\n!!! ГРЯЗНЫЕ (не-PUA изменение — НЕ записаны): %d" % len(bad_docs))
        for b in bad_docs[:5]:
            print("   ", b)
        return 1
    print("\nАУДИТ: все изменения = PUA->Symbol, не-PUA контент цел.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
