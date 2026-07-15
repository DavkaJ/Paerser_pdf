# -*- coding: utf-8 -*-
"""Ночной прогон корпуса ЗА ФЛАГОМ --latin-recovery (промпт 13b).

Парсит каждый PDF с включённым латинским восстановлением, пишет JSON в outout_latin/
(baseline outout/ НЕ трогаем — 13b: recovery за флагом, не в релиз до gold; I8: outout не
в git). Собирает очередь верификации (_corpus/verify_queue/tasks.json + crops/) и отчёт
_corpus/report_latin.json. Статус считает validate_doc (гейт LATIN_UNRESOLVED).

    TESSERACT_CMD=... TESSDATA_PREFIX=... python _corpus/batch_latin.py            # все
    python _corpus/batch_latin.py КР1_4.pdf КР628_2.pdf КР1000_1.pdf               # выборка
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW = os.path.join("data", "raw")
OUT = os.environ.get("CR_LATIN_OUT", "outout_latin")
REPORT = os.path.join("_corpus", "report_latin.json")
QUEUE_DIR = os.path.join("_corpus", "verify_queue")
# верхняя граница eng-OCR-строк на документ (без тихой потери — логируется, см. NIGHT_RUN).
MAX_OCR_LINES = int(os.environ.get("CR_LATIN_MAX_OCR", "600"))

_PARSER = None
_WRITER = None
_CLASS = {}


def _init(registry, classmap):
    global _PARSER, _WRITER, _CLASS
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    _PARSER = DocumentParser(create_profile("cr", registry),
                             latin_recovery=True, latin_queue_dir=QUEUE_DIR)
    _WRITER = JsonWriter()
    _CLASS = classmap


def work(base):
    pdf = os.path.join(RAW, base + ".pdf")
    cls = _CLASS.get(base, "OK")
    t0 = time.time()
    from validate import validate_doc, parse_toc, Report
    write_ok = True
    queue = []
    lr = {"corrections": 0, "needs_review": 0, "unresolved_critical": 0, "auto": 0}
    try:
        res = _PARSER.parse(pdf)
        doc = _WRITER.to_dict(res)
        queue = list(getattr(res, "latin_queue", []) or [])
        block = doc.get("latin_recovery") or {}
        corr = block.get("corrections", []) or []
        lr = {
            "corrections": len(corr),
            "needs_review": sum(1 for c in corr if c.get("decision") == "needs_review"),
            "auto": sum(1 for c in corr if c.get("decision") == "auto"),
            "unresolved_critical": len(block.get("unresolved_critical", []) or []),
        }
        try:
            _WRITER._atomic_dump(doc, os.path.join(OUT, base + ".json"))
        except Exception:  # noqa: BLE001
            write_ok = False
        rep = validate_doc(base + ".pdf", doc, parse_toc(pdf))
        st = doc.get("stats", {})
        cov, sec, tab = st.get("coverage_percent", 0.0), st.get("sections_found", 0), st.get("tables_found", 0)
    except Exception as exc:  # noqa: BLE001
        rep = Report(base + ".pdf")
        rep.fail("CRASH", repr(exc))
        cov = sec = tab = 0
    return {
        "file": base, "cls": cls, "status": rep.status,
        "coverage": cov, "sections": sec, "tables": tab,
        "fail_kinds": sorted({f.split(":", 1)[0] for f in rep.fails}),
        "warn_kinds": sorted({w.split(":", 1)[0] for w in rep.warns}),
        "reviews": rep.reviews, "fails": rep.fails,
        "latin": lr, "queue": queue, "write_ok": write_ok,
        "secs": round(time.time() - t0, 2),
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(QUEUE_DIR, exist_ok=True)
    registry = glob.glob("*.xlsx")[0]

    classmap = {}
    for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8")):
        classmap[os.path.splitext(r["file"])[0]] = r["class"]

    if len(sys.argv) > 1:
        bases = [os.path.splitext(os.path.basename(a))[0] for a in sys.argv[1:]]
    else:
        bases = sorted(b for b, c in classmap.items() if c in ("OK", "LOW_TEXT_REVIEW"))
    print("К обработке: %d файлов (OUT=%s)" % (len(bases), OUT))

    t0 = time.time()
    results = []
    workers = int(os.environ.get("CR_LATIN_WORKERS", "12"))
    with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                             initargs=(registry, classmap)) as ex:
        for i, r in enumerate(ex.map(work, bases), 1):
            results.append(r)
            if i % 50 == 0:
                print("  ...%d/%d  (%.0fs)" % (i, len(bases), time.time() - t0))

    # отчёт
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)

    # очередь верификации -> tasks.json (Label Studio)
    tasks = []
    for r in results:
        for q in r.get("queue", []):
            tasks.append(q)
    with open(os.path.join(QUEUE_DIR, "tasks.json"), "w", encoding="utf-8") as fh:
        json.dump(tasks, fh, ensure_ascii=False, indent=1)

    # сводка
    from collections import Counter
    st = Counter(r["status"] for r in results)
    tot_corr = sum(r["latin"]["corrections"] for r in results)
    tot_auto = sum(r["latin"]["auto"] for r in results)
    tot_nr = sum(r["latin"]["needs_review"] for r in results)
    tot_unres = sum(r["latin"]["unresolved_critical"] for r in results)
    docs_touched = sum(1 for r in results if r["latin"]["corrections"] > 0)
    docs_blocked = sum(1 for r in results if r["latin"]["unresolved_critical"] > 0)
    print("\n=== LATIN RECOVERY (весь прогон) ===")
    print("статусы:", dict(st))
    print("документов с правками: %d / %d" % (docs_touched, len(results)))
    print("замен всего: %d  (auto=%d, needs_review=%d)" % (tot_corr, tot_auto, tot_nr))
    print("неразрешённых КРИТИЧЕСКИХ (release-block): %d в %d док." % (tot_unres, docs_blocked))
    print("очередь верификации: %d задач -> %s/tasks.json" % (len(tasks), QUEUE_DIR))
    print("отчёт: %s  (%.0fs)" % (REPORT, time.time() - t0))

    broken = []
    for jf in glob.glob(os.path.join(OUT, "*.json")):
        try:
            json.load(open(jf, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            broken.append(os.path.basename(jf))
    if broken:
        print("БИТЫЕ JSON (%d): %s" % (len(broken), ", ".join(broken[:20])))
        return 1
    print("целостность: все %d JSON валидны" % len(glob.glob(os.path.join(OUT, "*.json"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
