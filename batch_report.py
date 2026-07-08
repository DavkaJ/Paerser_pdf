# -*- coding: utf-8 -*-
"""
Пакетный прогон парсера по корпусу КР с записью поставляемого артефакта.

Читает PDF из data/raw (только классы OK + LOW_TEXT_REVIEW по scan_candidates.csv,
сканы без текста пропускаются), парсит каждый и АТОМАРНО пишет JSON в outout/ —
ту самую папку, которую читает validate.py. По каждому файлу прогоняет валидатор
и собирает отчёт _corpus/report.json со статусами PASS/REVIEW/FAIL. После прогона
делает проверку целостности: каждый outout/*.json обязан читаться json.load; при
любом битом файле печатает список и завершается НЕНУЛЕВЫМ кодом.

    python batch_report.py            # весь корпус (OK + LOW_TEXT_REVIEW)
    python batch_report.py F.pdf …    # только указанные файлы (регресс-подвыборка)

Запись атомарна (JsonWriter.write: temp + fsync + os.replace), поэтому прерывание
батча не оставляет оборванных/NUL-паддинговых JSON. Содержимое и форматирование
не меняются — незатронутые файлы остаются байт-в-байт прежними.
"""

import csv
import glob
import json
import os
import sys
import time
import warnings
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")

# воркеры (spawn на Windows) переимпортируют модуль — добавляем корень проекта в
# sys.path, иначе `import crparser` падает в дочернем процессе.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RAW = os.path.join("data", "raw")
OUTOUT = "outout"
REPORT = os.path.join("_corpus", "report.json")

_PARSER = None
_WRITER = None
_CLASS = {}


def _init(registry, classmap):
    global _PARSER, _WRITER, _CLASS
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    _PARSER = DocumentParser(create_profile("cr", registry))
    _WRITER = JsonWriter()
    _CLASS = classmap


def work(base):
    pdf = os.path.join(RAW, base + ".pdf")
    cls = _CLASS.get(base, "OK")
    t0 = time.time()
    from validate import validate_doc, parse_toc, Report
    write_ok = True
    try:
        doc = _WRITER.to_dict(_PARSER.parse(pdf))
        try:
            # атомарная запись в поставляемую папку outout/
            _WRITER._atomic_dump(doc, os.path.join(OUTOUT, base + ".json"))
        except Exception:  # noqa: BLE001
            write_ok = False
        rep = validate_doc(base + ".pdf", doc, parse_toc(pdf))
        st = doc.get("stats", {})
        cov = st.get("coverage_percent", 0.0)
        sec = st.get("sections_found", 0)
        tab = st.get("tables_found", 0)
    except Exception as exc:  # noqa: BLE001
        rep = Report(base + ".pdf")
        rep.fail("CRASH", repr(exc))
        cov = sec = tab = 0

    return {
        "file": base, "cls": cls, "status": rep.status,
        "coverage": cov, "sections": sec, "tables": tab,
        "fails": rep.fails, "fail_kinds": sorted({f.split(":", 1)[0] for f in rep.fails}),
        "warns": rep.warns, "warn_kinds": sorted({w.split(":", 1)[0] for w in rep.warns}),
        "reviews": rep.reviews, "corruption": rep.corruption,
        "skipped": rep.skipped, "write_ok": write_ok,
        "secs": round(time.time() - t0, 2),
    }


def _integrity_check() -> int:
    """json.load по каждому outout/*.json; при битом — список и код возврата 1."""
    broken = []
    files = sorted(glob.glob(os.path.join(OUTOUT, "*.json")))
    for jf in files:
        try:
            with open(jf, encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:  # noqa: BLE001
            broken.append("%s (%s)" % (os.path.basename(jf), type(exc).__name__))
    if broken:
        print("\nБИТЫЕ JSON (%d из %d): %s" % (len(broken), len(files), broken))
        return 1
    print("\nцелостность: все %d JSON валидны" % len(files))
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.makedirs(OUTOUT, exist_ok=True)
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    registry = glob.glob("*.xlsx")[0]

    classmap = {os.path.splitext(r["file"])[0]: r["class"]
                for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8"))}

    if len(sys.argv) > 1:                       # явный список (регресс-подвыборка)
        bases = [os.path.splitext(os.path.basename(a))[0] for a in sys.argv[1:]]
    else:
        # Весь корпус. SKIPPED_SCAN включаем ОТДЕЛЬНОЙ веткой: при доступном
        # Tesseract парсер восстановит их ПОЛНЫМ OCR (непустые sections); без
        # Tesseract они как и раньше дают пустой текст -> статус SKIP (быстро,
        # поведение прежнее). Так полный OCR охватывает и сканы, а чистые файлы
        # остаются нетронутыми.
        bases = sorted(b for b, c in classmap.items()
                       if c in ("OK", "LOW_TEXT_REVIEW", "SKIPPED_SCAN"))
    print("К обработке: %d файлов -> %s/" % (len(bases), OUTOUT))

    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=14, initializer=_init,
                             initargs=(registry, classmap)) as ex:
        for i, r in enumerate(ex.map(work, bases), 1):
            results.append(r)
            if i % 100 == 0:
                print("  ...%d/%d  (%.0fs)" % (i, len(bases), time.time() - t0))

    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)

    ok = [r for r in results if r["cls"] == "OK"]
    print("\nOK-группа: %s" % dict(Counter(r["status"] for r in ok)))
    print("LOW_TEXT_REVIEW: %s" %
          [r["status"] for r in results if r["cls"] == "LOW_TEXT_REVIEW"])
    failed_write = [r["file"] for r in results if not r.get("write_ok", True)]
    if failed_write:
        print("НЕ ЗАПИСАНЫ (%d): %s" % (len(failed_write), ", ".join(failed_write)))
    print("отчёт: %s  (%.0fs)" % (REPORT, time.time() - t0))

    return _integrity_check()


if __name__ == "__main__":
    raise SystemExit(main())
