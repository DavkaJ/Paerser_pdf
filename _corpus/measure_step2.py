# -*- coding: utf-8 -*-
"""Замер blast-radius ШАГА 2 (START_IN_TOC, tail-based TOC). Перепарс OK-набора,
побайтовое сравнение с ЗАМОРОЖЕННЫМ staging прогона d6eb3906 (Jul-14 = до правки).

Правка — чистый elif (срабатывает ТОЛЬКО когда лидерный путь не сработал), поэтому
документы на лидерном пути обязаны быть БАЙТ-В-БАЙТ. Изменившиеся = кандидаты tail-ветки
(+ возможный дрейф Jul-14->Jul-20; сегментер/toc — единственное, что тронуто, latin off).

    python _corpus/measure_step2.py
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW = os.path.join("data", "raw")
RUN = "20260714T013305Z_d6eb3906"
I1 = {"КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"}
_PARSER = None
_WRITER = None


def _init(registry):
    global _PARSER, _WRITER
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    _PARSER = DocumentParser(create_profile("cr", registry))
    _WRITER = JsonWriter()


def work(base):
    from validate import validate_doc, parse_toc, _canonical_recall
    pdf = os.path.join(RAW, base + ".pdf")
    try:
        doc = _WRITER.to_dict(_PARSER.parse(pdf))
        raw = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")  # == _atomic_dump
        rep = validate_doc(base + ".pdf", doc, parse_toc(pdf))
        return base, {"sha": hashlib.sha256(raw).hexdigest(), "status": rep.status,
                      "recall": _canonical_recall(doc),
                      "ntop": len(doc.get("sections", []))}
    except Exception as exc:  # noqa: BLE001
        return base, {"error": repr(exc)}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    registry = glob.glob("*.xlsx")[0]
    # baseline sha по staging, ЧИТАННОМУ КАК ТЕКСТ (LF): _atomic_dump пишет в текстовом
    # режиме Windows -> на диске CRLF, а манифестный output_sha256 = sha CRLF-байт. Наш
    # reparse-sha = sha от json.dumps(...).encode() (LF). Сравнивать надо LF-с-LF, иначе
    # ВСЁ ложно «изменилось» (проверено: КР1000_1 content идентичен, но CRLF!=LF sha).
    stg = os.path.join("_runs", RUN, "staging")
    base_sha = {}
    for jf in glob.glob(os.path.join(stg, "*.json")):
        b = os.path.splitext(os.path.basename(jf))[0]
        base_sha[b] = hashlib.sha256(
            open(jf, encoding="utf-8").read().encode("utf-8")).hexdigest()
    rep_old = {r["file"]: r for r in json.load(open("_corpus/report.json", encoding="utf-8"))}

    classmap = {}
    for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8")):
        classmap[os.path.splitext(r["file"])[0]] = r["class"]
    bases = sorted(b for b, c in classmap.items() if c in ("OK", "LOW_TEXT_REVIEW"))

    # ВНИМАНИЕ: sha считаем от json.dumps(indent=1) — тот же формат, что _atomic_dump.
    # Сверим формат на одном неизменном файле, иначе «все изменились» ложно.
    print("перепарс %d док…" % len(bases))
    t0 = time.time()
    res = {}
    with ProcessPoolExecutor(max_workers=14, initializer=_init, initargs=(registry,)) as ex:
        for i, (b, d) in enumerate(ex.map(work, bases), 1):
            res[b] = d
            if i % 150 == 0:
                print("  ...%d/%d (%.0fs)" % (i, len(bases), time.time() - t0))

    errs = {b: v["error"] for b, v in res.items() if "error" in v}
    if errs:
        print("ОШИБКИ (%d): %s" % (len(errs), list(errs.items())[:3]))

    changed, status_delta = [], []
    for b in bases:
        v = res.get(b, {})
        if "error" in v:
            continue
        if base_sha.get(b) and v["sha"] != base_sha[b]:
            changed.append(b)
            old = rep_old.get(b, {}).get("status")
            if old != v["status"]:
                status_delta.append((b, old, v["status"], v["recall"], v["ntop"]))

    print("\n== ИЗМЕНИЛОСЬ vs Jul-14 staging: %d / %d ==" % (len(changed), len(bases)))
    print("   (правка — чистый elif; лидерный путь -> байт-в-байт. Порог промпта: <=~30)")
    print("   список:", ", ".join(sorted(changed)[:40]))
    print("\n== СТАТУС изменился (%d) ==" % len(status_delta))
    for b, o, n, rc, nt in sorted(status_delta):
        print("   %-10s %s -> %s  recall=%d ntop=%d %s" % (b, o, n, rc, nt, "I1!" if b in I1 else ""))

    print("\n== контрольная I1 (обязаны быть БАЙТ-В-БАЙТ = не изменились) ==")
    for b in sorted(I1):
        v = res.get(b, {})
        same = base_sha.get(b) and v.get("sha") == base_sha[b]
        print("   %-10s изменился=%s статус=%s" % (b, (not same), v.get("status")))

    kr = res.get("КР715_2", {})
    print("\n== пин КР715_2: status=%s recall=%s ntop=%s ==" % (kr.get("status"), kr.get("recall"), kr.get("ntop")))
    print("(%.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    main()
