# -*- coding: utf-8 -*-
"""Перевалидировать существующие JSON корпуса (без перепарсинга) — для проверки
правок ТОЛЬКО валидатора либо после точечных правок текста. Обновляет отчёт.

    python _corpus/revalidate.py                                  # outout -> _corpus/report.json
    CR_OUTOUT=outout_latin CR_REPORT=_corpus/report_latin.json \
        python _corpus/revalidate.py                              # любой корпус

Каталог и отчёт берутся из CR_OUTOUT/CR_REPORT (I2: боевые пути перекрываются env,
не правкой кода). Блок `latin` в строках отчёта считается из самого JSON, поэтому
отчёт сопоставим с тем, что пишет batch_latin.py.
"""
from __future__ import annotations

import csv, glob, json, os, sys, time, warnings
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW = os.path.join("data", "raw")
OUTOUT = os.environ.get("CR_OUTOUT", "outout")
REPORT = os.environ.get("CR_REPORT", os.path.join("_corpus", "report.json"))
_CLASS = {}


def _init(classmap):
    global _CLASS
    _CLASS = classmap


def work(base):
    from validate import validate_doc, parse_toc, Report
    cls = _CLASS.get(base, "OK")
    jf = os.path.join(OUTOUT, base + ".json")
    lr = {"corrections": 0, "needs_review": 0, "auto": 0, "unresolved_critical": 0}
    try:
        doc = json.load(open(jf, encoding="utf-8"))
        rep = validate_doc(base + ".pdf", doc, parse_toc(os.path.join(RAW, base + ".pdf")))
        st = doc.get("stats", {})
        cov, sec, tab = st.get("coverage_percent", 0.0), st.get("sections_found", 0), st.get("tables_found", 0)
        block = doc.get("latin_recovery") or {}
        corr = block.get("corrections", []) or []
        lr = {"corrections": len(corr),
              "needs_review": sum(1 for c in corr if c.get("decision") == "needs_review"),
              "auto": sum(1 for c in corr if c.get("decision") == "auto"),
              "unresolved_critical": len(block.get("unresolved_critical", []) or [])}
    except Exception as exc:  # noqa: BLE001
        rep = Report(base + ".pdf"); rep.fail("CRASH", repr(exc)); cov = sec = tab = 0
    return {"file": base, "cls": cls, "status": rep.status, "coverage": cov,
            "sections": sec, "tables": tab, "latin": lr, "fails": rep.fails,
            "fail_kinds": sorted({f.split(':', 1)[0] for f in rep.fails}),
            "warns": rep.warns, "warn_kinds": sorted({w.split(':', 1)[0] for w in rep.warns}),
            "reviews": rep.reviews, "corruption": rep.corruption,
            "skipped": rep.skipped, "secs": 0}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    classmap = {os.path.splitext(r["file"])[0]: r["class"]
                for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8"))}
    bases = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(OUTOUT, "*.json")))
    t0 = time.time(); results = []
    with ProcessPoolExecutor(max_workers=14, initializer=_init, initargs=(classmap,)) as ex:
        for r in ex.map(work, bases):
            results.append(r)
    from crparser.engine.jsonio import JsonWriter
    JsonWriter._atomic_dump(results, REPORT)   # атомарно: без торн/обрыва при прерывании
    from collections import Counter
    ok = [r for r in results if r["cls"] == "OK"]
    print("%s -> %s" % (OUTOUT, REPORT))
    print("ВСЕ:", dict(Counter(r["status"] for r in results)))
    print("OK:", dict(Counter(r["status"] for r in ok)), " (%.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    main()
