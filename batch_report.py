# -*- coding: utf-8 -*-
"""
Транзакционный пакетный прогон парсера КР (промпт 05, редакция 2).

Публикация результатов сделана транзакционной с ЯВНОЙ state machine и РАЗВЕДЕНИЕМ
двух независимых осей:

  run_integrity_ok  — прогон технически СОСТОЯЛСЯ: обработано ровно ожидаемое
                      множество, каждая запись на диск удалась, пины не менялись,
                      OCR-предусловие выполнено. Блокирует ПУБЛИКАЦИЮ сырого outout/.
  quality_gate_ok   — результат достаточно ХОРОШ: нет FAIL/REVIEW/CRASH. Блокирует
                      release-экспорт для обучения (промпт 06), НЕ публикацию.

Порядок на каждый документ: parse -> write to staging -> RE-READ from staging ->
validate прочитанное. Валидировать объект из памяти запрещено — так ловится и порча
при сериализации, и рассинхрон «отчёт про одно, файл про другое».

    python batch_report.py                 # весь корпус (OK+LOW+SKIPPED_SCAN)
    python batch_report.py F.pdf …         # частичный прогон (публикует только указанные)

Exit-коды (три состояния):
    0  — run_integrity_ok И quality_gate_ok
    1  — run_integrity_ok, но есть FAIL/REVIEW/CRASH (публикация СОСТОЯЛАСЬ)
    2  — run_integrity_ok == False (публикации НЕ БЫЛО, outout/ не тронут)
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import shutil
import sys
import time
import warnings
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RAW = os.path.join("data", "raw")
# Пути параметризуемы через env (правка 7) — по умолчанию прежние значения.
OUTOUT = os.environ.get("CR_OUTOUT", "outout")
REPORT = os.environ.get("CR_REPORT", os.path.join("_corpus", "report.json"))
REPORT_META = os.path.join(os.path.dirname(REPORT) or ".", "report_meta.json")
RUNS_DIR = "_runs"

# Оригинальный набор полей report.json (bare array) — downstream его читает,
# формат НЕ меняем; богатые per-doc данные уходят в run_manifest.
_REPORT_FIELDS = ("file", "cls", "status", "coverage", "sections", "tables",
                  "fails", "fail_kinds", "warns", "warn_kinds", "reviews",
                  "corruption", "skipped", "write_ok", "secs")

_PARSER = None
_WRITER = None
_CLASS = {}
_STAGING = None


def _sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Воркер                                                                       #
# --------------------------------------------------------------------------- #
def _init(registry, classmap, staging_dir):
    global _PARSER, _WRITER, _CLASS, _STAGING
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    _PARSER = DocumentParser(create_profile("cr", registry))
    _WRITER = JsonWriter()
    _CLASS = classmap
    _STAGING = staging_dir


def _mk(base, cls, rep, doc, input_sha, output_sha, t0,
        crash=False, write_failed=False, timeout=False):
    st = (doc or {}).get("stats", {})
    return {
        "file": base, "cls": cls, "status": rep.status,
        "coverage": st.get("coverage_percent", 0.0),
        "sections": st.get("sections_found", 0),
        "tables": st.get("tables_found", 0),
        "fails": rep.fails, "fail_kinds": sorted({f.split(":", 1)[0] for f in rep.fails}),
        "warns": rep.warns, "warn_kinds": sorted({w.split(":", 1)[0] for w in rep.warns}),
        "reviews": rep.reviews, "corruption": rep.corruption, "skipped": rep.skipped,
        "write_ok": not write_failed, "crash": crash, "timeout": timeout,
        "input_sha256": input_sha, "output_sha256": output_sha,
        "secs": round(time.time() - t0, 2),
    }


def work(base):
    """parse -> write staging -> re-read -> validate прочитанное."""
    from validate import validate_doc, parse_toc, Report
    cls = _CLASS.get(base, "OK")
    t0 = time.time()
    pdf = os.path.join(RAW, base + ".pdf")
    staging_path = os.path.join(_STAGING, base + ".json")
    input_sha = _sha256_file(pdf)
    # тест-хук: имитировать мутацию immutable-базы пинов В ХОДЕ прогона (после 02
    # такого быть не должно; проверяем, что детектор PINS_CHANGED это ловит).
    if os.environ.get("CR_TEST_TOUCH_PINS") == base:
        try:
            with open(os.path.join("crparser", "data", "ocr_pins.json"), "a",
                      encoding="utf-8") as fh:
                fh.write(" ")
        except Exception:  # noqa: BLE001
            pass
    # --- parse ---
    try:
        doc = _WRITER.to_dict(_PARSER.parse(pdf))
    except Exception as exc:  # noqa: BLE001 — CRASH: этот PDF не парсится
        rep = Report(base + ".pdf")
        rep.fail("CRASH", repr(exc))
        return _mk(base, cls, rep, None, input_sha, None, t0, crash=True)
    # --- write to staging (атомарно) ---
    # тест-хук (только для регресс-тестов транзакционности; в норме не задан):
    # CR_TEST_FAIL_WRITE=<base> имитирует сбой инфраструктуры записи.
    if os.environ.get("CR_TEST_FAIL_WRITE") == base:
        rep = Report(base + ".pdf")
        rep.fail("WRITE_FAILED", "имитация сбоя записи (CR_TEST_FAIL_WRITE)")
        return _mk(base, cls, rep, None, input_sha, None, t0, write_failed=True)
    try:
        _WRITER._atomic_dump(doc, staging_path)
    except Exception as exc:  # noqa: BLE001 — WRITE_FAILED: сломана инфраструктура
        rep = Report(base + ".pdf")
        rep.fail("WRITE_FAILED", repr(exc))
        return _mk(base, cls, rep, None, input_sha, None, t0, write_failed=True)
    # --- re-read from staging и валидируем ПРОЧИТАННОЕ ---
    try:
        with open(staging_path, encoding="utf-8") as fh:
            disk_doc = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        rep = Report(base + ".pdf")
        rep.fail("WRITE_FAILED", "re-read staging: %r" % exc)
        return _mk(base, cls, rep, None, input_sha, None, t0, write_failed=True)
    output_sha = _sha256_file(staging_path)
    rep = validate_doc(base + ".pdf", disk_doc, parse_toc(pdf))
    return _mk(base, cls, rep, disk_doc, input_sha, output_sha, t0)


# --------------------------------------------------------------------------- #
# OCR-предусловие (правка 3-bis)                                              #
# --------------------------------------------------------------------------- #
def _ocr_status():
    from crparser.engine.ocr import OcrRecoverer
    rec = OcrRecoverer()
    available = rec.available()
    info = {"available": available, "version": None, "traineddata_sha256": {}}
    if available:
        try:
            from crparser.engine.manifest import _tesseract_info
            ti = _tesseract_info()
            info["version"] = ti.get("version")
            info["traineddata_sha256"] = {
                k: v.get("sha256") for k, v in ti.get("traineddata", {}).items()}
        except Exception:  # noqa: BLE001
            pass
    return available, info


# --------------------------------------------------------------------------- #
# Пул с двумя проходами и per-doc timeout                                     #
# --------------------------------------------------------------------------- #
def _run_pool(bases, workers, registry, classmap, staging_dir, timeout):
    results = {}
    if not bases:
        return results
    with ProcessPoolExecutor(max_workers=max(1, workers), initializer=_init,
                             initargs=(registry, classmap, staging_dir)) as ex:
        futures = {ex.submit(work, b): b for b in bases}
        done = 0
        for fut, b in futures.items():
            try:
                results[b] = fut.result(timeout=timeout)
            except FutureTimeout:
                from validate import Report
                rep = Report(b + ".pdf")
                rep.fail("TIMEOUT", "превышен per-doc timeout %ss" % timeout)
                results[b] = _mk(b, classmap.get(b, "OK"), rep, None,
                                 _sha256_file(os.path.join(RAW, b + ".pdf")),
                                 None, time.time(), timeout=True)
            except Exception as exc:  # noqa: BLE001 — воркер умер
                from validate import Report
                rep = Report(b + ".pdf")
                rep.fail("CRASH", "worker died: %r" % exc)
                results[b] = _mk(b, classmap.get(b, "OK"), rep, None, None, None,
                                 time.time(), crash=True)
            done += 1
            if done % 100 == 0:
                print("  ...%d/%d" % (done, len(bases)))
        # per-doc timeout может оставить зависший нативный вызов — не ждём его вечно
        ex.shutdown(wait=False, cancel_futures=True)
    return results


# --------------------------------------------------------------------------- #
# Публикация                                                                  #
# --------------------------------------------------------------------------- #
def publish(staging_dir, expected, results, full_run):
    """Копирует staging -> outout; удаляет устаревшие JSON упавших документов;
    при полном прогоне удаляет outout-артефакты вне expected. Только при
    run_integrity_ok (вызывающий это гарантирует)."""
    os.makedirs(OUTOUT, exist_ok=True)
    published, removed_stale, removed_extra = 0, [], []
    for base in expected:
        sp = os.path.join(staging_dir, base + ".json")
        op = os.path.join(OUTOUT, base + ".json")
        if os.path.exists(sp):
            shutil.copyfile(sp, op)
            published += 1
        else:
            # CRASH/TIMEOUT: staging-выхода нет -> устаревший JSON не должен пережить
            if os.path.exists(op):
                os.remove(op)
                removed_stale.append(base)
    if full_run:
        keep = set(expected)
        for jf in glob.glob(os.path.join(OUTOUT, "*.json")):
            b = os.path.splitext(os.path.basename(jf))[0]
            if b not in keep:
                os.remove(jf)
                removed_extra.append(b)
    if removed_stale:
        print("ПУБЛИКАЦИЯ: удалены устаревшие JSON упавших документов (%d): %s"
              % (len(removed_stale), ", ".join(sorted(removed_stale)[:20])))
    if removed_extra:
        print("ПУБЛИКАЦИЯ: удалены артефакты прошлых прогонов (%d): %s"
              % (len(removed_extra), ", ".join(sorted(removed_extra)[:20])))
    print("ПУБЛИКАЦИЯ: %d файлов -> %s/" % (published, OUTOUT))


def _staging_integrity(staging_dir):
    """Все staging/*.json обязаны читаться. Возвращает список битых."""
    broken = []
    for jf in sorted(glob.glob(os.path.join(staging_dir, "*.json"))):
        try:
            with open(jf, encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:  # noqa: BLE001
            broken.append("%s (%s)" % (os.path.basename(jf), type(exc).__name__))
    return broken


def _cleanup_runs(keep_n):
    if not os.path.isdir(RUNS_DIR):
        return
    runs = sorted(d for d in os.listdir(RUNS_DIR)
                  if os.path.isdir(os.path.join(RUNS_DIR, d)))
    for old in runs[:-keep_n] if keep_n > 0 else []:
        shutil.rmtree(os.path.join(RUNS_DIR, old), ignore_errors=True)


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Транзакционный батч парсера КР.")
    ap.add_argument("files", nargs="*", help="PDF/база для частичного прогона")
    ap.add_argument("--workers", type=int, default=min(os.cpu_count() or 4, 8))
    ap.add_argument("--ocr-workers", type=int, default=3,
                    help="узкий пул для полного OCR (растры 400 DPI держат память)")
    ap.add_argument("--timeout", type=float, default=900.0, help="per-doc, сек")
    ap.add_argument("--keep-runs", type=int, default=2)
    ap.add_argument("--require-ocr", dest="require_ocr", action="store_true", default=True)
    ap.add_argument("--no-require-ocr", dest="require_ocr", action="store_false",
                    help="явно снять OCR-предусловие (записывается в манифест; "
                         "release.py такой прогон в релиз не пускает)")
    ap.add_argument("--allow-not-pass", action="store_true",
                    help="локальная отладка: exit 1 -> 0 (на код 2 не влияет)")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(REPORT) or ".", exist_ok=True)
    registry = glob.glob("*.xlsx")[0]
    classmap = {os.path.splitext(r["file"])[0]: r["class"]
                for r in csv.DictReader(open("scan_candidates.csv", encoding="utf-8"))}

    full_run = not args.files
    if full_run:
        bases = sorted(b for b, c in classmap.items()
                       if c in ("OK", "LOW_TEXT_REVIEW", "SKIPPED_SCAN"))
    else:
        bases = [os.path.splitext(os.path.basename(a))[0] for a in args.files]
    expected = list(bases)

    # --- run_id + staging ---
    from crparser.engine import manifest
    code = manifest.code_fingerprint()
    run_id = "%s_%s" % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                        (code.get("tree_sha256") or "nocode")[:8])
    run_dir = os.path.join(RUNS_DIR, run_id)
    staging_dir = os.path.join(run_dir, "staging")
    os.makedirs(staging_dir, exist_ok=True)
    print("run_id=%s  документов: %d  (%s прогон)"
          % (run_id, len(bases), "полный" if full_run else "частичный"))

    # --- OCR-предусловие (правка 3-bis) ---
    ocr_available, ocr_info = _ocr_status()
    ocr_info["required"] = args.require_ocr
    pins_path = os.path.join("crparser", "data", "ocr_pins.json")
    pins_before = _sha256_file(pins_path)

    integrity_failures = []
    if args.require_ocr and not ocr_available:
        integrity_failures.append(
            "OCR_UNAVAILABLE: OCR требуется (--require-ocr), но Tesseract не найден "
            "по TESSERACT_CMD/PATH. Задайте TESSERACT_CMD или --no-require-ocr.")
        print("\nОСТАНОВ: " + integrity_failures[-1])
        _write_manifests(run_dir, run_id, expected, {}, code, ocr_info,
                         pins_before, pins_before, integrity_failures, args)
        return 2

    # --- прогон: два прохода (не-OCR широким пулом, полный-OCR узким) ---
    t0 = time.time()
    scans = [b for b in bases if classmap.get(b) == "SKIPPED_SCAN"]
    rest = [b for b in bases if classmap.get(b) != "SKIPPED_SCAN"]
    results = {}
    results.update(_run_pool(rest, args.workers, registry, classmap, staging_dir, args.timeout))
    if scans:
        print("OCR-очередь (полный OCR): %d документов, %d воркеров"
              % (len(scans), args.ocr_workers))
        results.update(_run_pool(scans, args.ocr_workers, registry, classmap,
                                 staging_dir, args.timeout))
    print("прогон завершён за %.0fs" % (time.time() - t0))

    # --- run_integrity ---
    pins_after = _sha256_file(pins_path)
    result_keys = set(results)
    if result_keys != set(expected):
        integrity_failures.append(
            "SET_MISMATCH: результатов %d, ожидалось %d" % (len(result_keys), len(expected)))
    write_failed = [b for b, r in results.items() if not r["write_ok"]]
    if write_failed:
        integrity_failures.append("WRITE_FAILED: " + ", ".join(sorted(write_failed)))
    if pins_before != pins_after:
        integrity_failures.append("PINS_CHANGED: ocr_pins.json изменился в ходе прогона")
    broken = _staging_integrity(staging_dir)
    if broken:
        integrity_failures.append("STAGING_BROKEN: " + ", ".join(broken))

    run_integrity_ok = not integrity_failures

    counts = Counter(r["status"] for r in results.values())
    n_crash = sum(1 for r in results.values() if r.get("crash"))
    n_timeout = sum(1 for r in results.values() if r.get("timeout"))
    quality_gate_ok = (counts.get("FAIL", 0) == 0 and counts.get("REVIEW", 0) == 0
                       and n_crash == 0)

    # --- публикация (только при run_integrity_ok) ---
    if run_integrity_ok:
        publish(staging_dir, expected, results, full_run)
    else:
        print("\nRUN_INTEGRITY_OK=False — публикации НЕТ, outout/ не тронут:")
        for f in integrity_failures:
            print("   ✗ " + f)

    # --- report.json (bare array, прежний формат) + report_meta.json ---
    report_items = [{k: results[b][k] for k in _REPORT_FIELDS}
                    for b in sorted(results)]
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(report_items, fh, ensure_ascii=False, indent=1)
    _write_manifests(run_dir, run_id, expected, results, code, ocr_info,
                     pins_before, pins_after, integrity_failures, args,
                     counts=counts, n_crash=n_crash, n_timeout=n_timeout,
                     run_integrity_ok=run_integrity_ok, quality_gate_ok=quality_gate_ok,
                     published=run_integrity_ok)
    _cleanup_runs(args.keep_runs)

    print("\nСТАТУСЫ: %s  crash=%d timeout=%d" % (dict(counts), n_crash, n_timeout))
    print("run_integrity_ok=%s  quality_gate_ok=%s" % (run_integrity_ok, quality_gate_ok))

    # --- exit-коды ---
    if not run_integrity_ok:
        return 2
    if quality_gate_ok:
        return 0
    return 0 if args.allow_not_pass else 1


def _write_manifests(run_dir, run_id, expected, results, code, ocr_info,
                     pins_before, pins_after, integrity_failures, args,
                     counts=None, n_crash=0, n_timeout=0,
                     run_integrity_ok=False, quality_gate_ok=False, published=False):
    from crparser.engine import manifest
    os.makedirs(run_dir, exist_ok=True)
    documents = {}
    for b in sorted(results):
        r = results[b]
        documents[b] = {
            "input_sha256": r.get("input_sha256"),
            "output_sha256": r.get("output_sha256"),
            "status": r["status"], "kinds": r.get("fail_kinds", []) + [
                x.split(":", 1)[0] for x in r.get("reviews", [])],
            "crash": r.get("crash", False), "timeout": r.get("timeout", False),
            "secs": r.get("secs"),
        }
    counts = counts or Counter()
    run_manifest = {
        "run_id": run_id,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "code": code, "env": manifest.env_fingerprint(), "config": manifest._config(),
        "ocr": ocr_info,
        "pins_sha256_before": pins_before, "pins_sha256_after": pins_after,
        "expected": sorted(expected), "documents": documents,
        "run_integrity_ok": run_integrity_ok, "integrity_failures": integrity_failures,
        "quality_gate_ok": quality_gate_ok, "published": published,
        "require_ocr": args.require_ocr,
        "counts": {"pass": counts.get("PASS", 0), "review": counts.get("REVIEW", 0),
                   "fail": counts.get("FAIL", 0), "skip": counts.get("SKIP", 0),
                   "crash": n_crash, "write_failed": sum(
                       1 for r in results.values() if not r.get("write_ok", True))},
    }
    from crparser.engine.jsonio import JsonWriter
    JsonWriter._atomic_dump(run_manifest, os.path.join(run_dir, "run_manifest.json"))
    # report_meta.json — шапка отдельным файлом (report.json остаётся bare array)
    JsonWriter._atomic_dump(
        {"run_id": run_id, "run_integrity_ok": run_integrity_ok,
         "quality_gate_ok": quality_gate_ok, "counts": run_manifest["counts"],
         "ocr": ocr_info, "integrity_failures": integrity_failures}, REPORT_META)


if __name__ == "__main__":
    raise SystemExit(main())
