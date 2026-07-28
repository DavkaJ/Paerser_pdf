# -*- coding: utf-8 -*-
"""ИЗОЛИРОВАННЫЙ A/B ШАГА 4 (numbering model select) — БЕЗ дрейфа.

Каждый док парсится дважды: выбор модели ON vs OFF (segmenter._ROMAN_MODEL_SELECT).
Второй парс — ТОЛЬКО если док неоднозначный (uses_roman & _roman_chapters_safe==False),
иначе OFF==ON. Разница = чистый эффект ШАГА 4.

    python _corpus/measure_step4_ab.py
"""
from __future__ import annotations

import csv, glob, hashlib, json, os, sys, time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW = os.path.join("data", "raw")
I1 = {"КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"}
_P = _W = None
_AMBIG = {"v": False}


def _init(registry):
    global _P, _W
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    from crparser.engine import segmenter as SEG
    _P = DocumentParser(create_profile("cr", registry))
    _W = JsonWriter()
    orig = SEG.Segmenter._roman_chapters_safe

    def wrap(self, lines):
        r = orig(self, lines)
        _AMBIG["v"] = bool(self._numbering.uses_roman_chapters and not r)
        return r
    SEG.Segmenter._roman_chapters_safe = wrap


def _sha(doc):
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")).hexdigest()


def work(base):
    from crparser.engine import segmenter as SEG
    from validate import validate_doc, parse_toc, _canonical_recall
    pdf = os.path.join(RAW, base + ".pdf")
    try:
        toc = parse_toc(pdf)
        SEG._ROMAN_MODEL_SELECT = True
        _AMBIG["v"] = False
        d_on = _W.to_dict(_P.parse(pdf))
        amb = _AMBIG["v"]
        r = {"amb": amb, "sha_on": _sha(d_on),
             "st_on": validate_doc(base + ".pdf", d_on, toc).status,
             "rec_on": _canonical_recall(d_on), "ntop_on": len(d_on.get("sections", []))}
        if amb:
            SEG._ROMAN_MODEL_SELECT = False
            d_off = _W.to_dict(_P.parse(pdf))
            SEG._ROMAN_MODEL_SELECT = True
            r.update(sha_off=_sha(d_off), st_off=validate_doc(base + ".pdf", d_off, toc).status,
                     rec_off=_canonical_recall(d_off), ntop_off=len(d_off.get("sections", [])))
        else:
            r.update(sha_off=r["sha_on"], st_off=r["st_on"], rec_off=r["rec_on"], ntop_off=r["ntop_on"])
        return base, r
    except Exception as exc:  # noqa: BLE001
        return base, {"error": repr(exc)}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    registry = glob.glob("*.xlsx")[0]
    classmap = {}
    for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8")):
        classmap[os.path.splitext(r["file"])[0]] = r["class"]
    bases = sorted(b for b, c in classmap.items() if c in ("OK", "LOW_TEXT_REVIEW"))
    print("A/B перепарс %d док (roman model-select ON vs OFF)…" % len(bases))
    t0 = time.time()
    res = {}
    with ProcessPoolExecutor(max_workers=14, initializer=_init, initargs=(registry,)) as ex:
        for i, (b, d) in enumerate(ex.map(work, bases), 1):
            res[b] = d
            if i % 150 == 0:
                print("  ...%d/%d (%.0fs)" % (i, len(bases), time.time() - t0))

    ok = {b: d for b, d in res.items() if "error" not in d}
    amb = [b for b, d in ok.items() if d["amb"]]
    changed = [b for b, d in ok.items() if d["sha_on"] != d["sha_off"]]
    print("\n== ЧИСТЫЙ эффект ШАГА 4 (ON vs OFF, без дрейфа) ==")
    print("  неоднозначных (uses_roman & not safe): %d док" % len(amb))
    print("  реально изменилось (sha ON!=OFF): %d" % len(changed))
    print("\n  изменения (recall/status off->on):")
    for b in sorted(changed):
        d = ok[b]
        arrow = ("УЛУЧШ recall" if d["rec_on"] > d["rec_off"] else
                 ("ХУЖЕ recall" if d["rec_on"] < d["rec_off"] else "="))
        print("     %-10s recall %d->%d  ntop %d->%d  status %s->%s  [%s]%s"
              % (b, d["rec_off"], d["rec_on"], d["ntop_off"], d["ntop_on"],
                 d["st_off"], d["st_on"], arrow, "  I1!!" if b in I1 else ""))
    print("\n  I1 среди изменившихся:", [b for b in changed if b in I1] or "нет (верно)")
    regress = [b for b in changed if ok[b]["rec_on"] < ok[b]["rec_off"]]
    print("  РЕГРЕСС recall (on<off):", regress or "нет")
    errs = [b for b, d in res.items() if "error" in d]
    if errs:
        print("  ОШИБКИ:", errs[:5])
    print("(%.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    main()
