# -*- coding: utf-8 -*-
"""ЭТАП 2: батч-парсинг + валидация всего корпуса (OK + LOW_TEXT_REVIEW).

Парсит каждый PDF в JSON (outout/) и прогоняет validate_doc. Сохраняет
машино-читаемый отчёт _corpus/report.json. Код парсера НЕ трогает.

    python _corpus/batch.py            # все OK + LOW_TEXT_REVIEW из scan_candidates.csv
    python _corpus/batch.py FILE.pdf … # только указанные (для регресс-прогона)
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

# воркеры (spawn на Windows) переимпортируют модуль — добавляем корень проекта
# (родитель _corpus) в sys.path, иначе `import crparser` падает в дочернем процессе.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

    fail_kinds = sorted({f.split(":", 1)[0] for f in rep.fails})
    warn_kinds = sorted({w.split(":", 1)[0] for w in rep.warns})
    return {
        "file": base, "cls": cls, "status": rep.status,
        "coverage": cov, "sections": sec, "tables": tab,
        "fails": rep.fails, "fail_kinds": fail_kinds,
        "warns": rep.warns, "warn_kinds": warn_kinds,
        "reviews": rep.reviews, "corruption": rep.corruption,
        "skipped": rep.skipped, "write_ok": write_ok,
        "secs": round(time.time() - t0, 2),
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.makedirs(OUTOUT, exist_ok=True)
    registry = glob.glob("*.xlsx")[0]

    classmap = {}
    for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8")):
        classmap[os.path.splitext(r["file"])[0]] = r["class"]

    # OCR-предусловие (ШАГ 0, fail-closed): этот инструмент — корень инцидента
    # ac5d03a (регенерация baseline с молча выключенным OCR, exit 0). Гибрид-проход
    # требует rus+eng; без гейта прогон публикует ДРУГОЙ корпус — дыра I5/I34. Гейт
    # по факту доступности (бинарь И rus+eng); --no-require-ocr снимает ОСОЗНАННО.
    require_ocr = "--no-require-ocr" not in sys.argv
    argv_files = [a for a in sys.argv[1:] if not a.startswith("--")]
    from crparser.engine.ocr import ocr_precondition
    available, ocr_msg = ocr_precondition()
    if require_ocr and not available:
        print("ОСТАНОВ (--require-ocr): OCR недоступен — %s" % ocr_msg)
        print("Публикации в %s/ НЕТ. Задайте TESSERACT_CMD/TESSDATA_PREFIX (нужен "
              "rus+eng) либо --no-require-ocr (прогон невалиден)." % OUTOUT)
        return 2
    print("OCR-предусловие: available=%s%s" % (
        available, "" if available else " (--no-require-ocr: прогон невалиден)"))

    if argv_files:                            # явный список (регресс-прогон)
        bases = [os.path.splitext(os.path.basename(a))[0] for a in argv_files]
    else:                                     # все OK + LOW_TEXT_REVIEW
        bases = sorted(b for b, c in classmap.items()
                       if c in ("OK", "LOW_TEXT_REVIEW"))
    print("К обработке: %d файлов" % len(bases))

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

    from collections import Counter
    ok = [r for r in results if r["cls"] == "OK"]
    st = Counter(r["status"] for r in ok)
    print("\nOK-группа: %s" % dict(st))
    print("LOW_TEXT_REVIEW: %s" % [r["status"] for r in results if r["cls"] == "LOW_TEXT_REVIEW"])
    fk = Counter()
    for r in ok:
        if r["status"] == "FAIL":
            for k in r["fail_kinds"]:
                fk[k] += 1
    print("FAIL-сигнатуры (OK):", dict(fk.most_common()))
    print("отчёт: %s  (%.0fs)" % (REPORT, time.time() - t0))

    # файлы, которые не удалось записать (исключение при atomic-dump)
    failed_write = [r["file"] for r in results if not r.get("write_ok", True)]
    if failed_write:
        print("НЕ ЗАПИСАНЫ (%d): %s" % (len(failed_write), ", ".join(failed_write)))

    # финальный чек целостности: все out/*.json обязаны читаться json.load
    broken = []
    for jf in glob.glob(os.path.join(OUTOUT, "*.json")):
        try:
            with open(jf, encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:  # noqa: BLE001
            broken.append("%s (%s)" % (os.path.basename(jf), type(exc).__name__))
    if broken:
        print("БИТЫЕ JSON (%d): %s" % (len(broken), ", ".join(broken)))
        return 1
    print("целостность: все %d JSON валидны" % len(glob.glob(os.path.join(OUTOUT, "*.json"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
