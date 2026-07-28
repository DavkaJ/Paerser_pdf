# -*- coding: utf-8 -*-
"""ИЗОЛИРОВАННЫЙ A/B ШАГА 2 (START_IN_TOC) — БЕЗ дрейфа Jul-14->Jul-20.

Для каждого дока парсит ДВАЖДЫ в одном процессе: с tail-путём (ON) и без (OFF, через
segmenter._TAIL_TOC_ENABLED). Второй парс — ТОЛЬКО если tail-ветка была eligible (лидерный
путь провалился + маркер + >=4 хвостов), иначе OFF==ON (правка инертна). Разница ON vs OFF —
ЧИСТЫЙ эффект ШАГА 2. Регрессии drift (все 8 PASS->REVIEW имели лидеры -> лидерный путь ->
elif недостижим) сюда НЕ попадают.

    python _corpus/measure_step2_ab.py
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW = os.path.join("data", "raw")
I1 = {"КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"}
_P = _W = None
_ELIG = {"v": False}


def _init(registry):
    global _P, _W
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    from crparser.engine import segmenter as SEG
    _P = DocumentParser(create_profile("cr", registry))
    _W = JsonWriter()
    orig = SEG.Segmenter._find_content_start

    def wrap(self, lines):
        n = len(lines)
        dot = [k for k in range(n) if SEG._RE_LEADER.search(lines[k].text)]
        anc = next((k for k in range(n) if self._spec.toc.match(lines[k].text.strip())), None)
        near = ([k for k in dot if 0 <= k - anc <= 60] if anc is not None
                else [k for k in dot if k <= 400])
        elig = False
        if len(near) < 4 and anc is not None:
            tail = [k for k in range(n)
                    if SEG._RE_TOC_TAIL.search(lines[k].text.strip())
                    and re.search(r"[А-Яа-яA-Za-z]", lines[k].text)]
            elig = len([k for k in tail if 0 <= k - anc <= 60]) >= 4
        _ELIG["v"] = elig
        return orig(self, lines)

    SEG.Segmenter._find_content_start = wrap


def _sha(doc):
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")).hexdigest()


def work(base):
    from crparser.engine import segmenter as SEG
    from validate import validate_doc, parse_toc, _canonical_recall
    pdf = os.path.join(RAW, base + ".pdf")
    try:
        toc = parse_toc(pdf)
        SEG._TAIL_TOC_ENABLED = True
        _ELIG["v"] = False
        d_on = _W.to_dict(_P.parse(pdf))
        elig = _ELIG["v"]
        r = {"elig": elig, "sha_on": _sha(d_on),
             "st_on": validate_doc(base + ".pdf", d_on, toc).status,
             "rec_on": _canonical_recall(d_on), "ntop_on": len(d_on.get("sections", []))}
        if elig:
            SEG._TAIL_TOC_ENABLED = False
            d_off = _W.to_dict(_P.parse(pdf))
            SEG._TAIL_TOC_ENABLED = True
            r.update(sha_off=_sha(d_off), st_off=validate_doc(base + ".pdf", d_off, toc).status,
                     rec_off=_canonical_recall(d_off), ntop_off=len(d_off.get("sections", [])))
        else:
            r.update(sha_off=r["sha_on"], st_off=r["st_on"], rec_off=r["rec_on"],
                     ntop_off=r["ntop_on"])
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
    print("A/B перепарс %d док (tail-TOC ON vs OFF)…" % len(bases))
    t0 = time.time()
    res = {}
    with ProcessPoolExecutor(max_workers=14, initializer=_init, initargs=(registry,)) as ex:
        for i, (b, d) in enumerate(ex.map(work, bases), 1):
            res[b] = d
            if i % 150 == 0:
                print("  ...%d/%d (%.0fs)" % (i, len(bases), time.time() - t0))

    ok = {b: d for b, d in res.items() if "error" not in d}
    elig = [b for b, d in ok.items() if d["elig"]]
    changed = [b for b, d in ok.items() if d["sha_on"] != d["sha_off"]]
    st_ch = [(b, ok[b]["st_off"], ok[b]["st_on"], ok[b]["rec_off"], ok[b]["rec_on"])
             for b in changed if ok[b]["st_off"] != ok[b]["st_on"]]
    print("\n== ЧИСТЫЙ эффект ШАГА 2 (ON vs OFF, без дрейфа) ==")
    print("  tail-ветка eligible: %d док" % len(elig))
    print("  реально изменилось (sha ON!=OFF): %d" % len(changed))
    print("  список изменившихся:", ", ".join(sorted(changed)))
    print("\n  статус ИЗМЕНИЛСЯ ШАГОМ 2 (%d):" % len(st_ch))
    for b, o, n, ro, rn in sorted(st_ch):
        arrow = "УЛУЧШ" if (o in ("FAIL", "REVIEW") and n == "PASS") else \
                ("РЕГРЕСС" if (o == "PASS" and n != "PASS") else "сдвиг")
        print("     %-10s %s -> %s  recall %d->%d  [%s]%s"
              % (b, o, n, ro, rn, arrow, "  I1!!" if b in I1 else ""))
    print("\n  I1 среди изменившихся:", [b for b in changed if b in I1] or "нет (верно)")
    errs = [b for b, d in res.items() if "error" in d]
    if errs:
        print("  ОШИБКИ:", errs[:5])
    print("(%.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    main()
