# -*- coding: utf-8 -*-
"""ЭТАП 1: классификация входа по плотности текстового слоя.

Открывает каждый PDF (fitz), считает «значимые» символы (буквы/цифры) на странице
и разносит файлы на OK / SKIPPED_SCAN / LOW_TEXT_REVIEW. Пишет scan_candidates.csv.

    python _corpus/classify.py
"""
from __future__ import annotations

import csv
import glob
import os
import re
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")

RAW = os.path.join("data", "raw")
OUT_CSV = "scan_candidates.csv"

_SIG = re.compile(r"[0-9A-Za-zА-Яа-яЁё]")

# Пороги классификации (откалиброваны по распределению корпуса: реальные КР дают
# >800 знач.символов/стр при <15% пустых страниц; «чистые» сканы — 0; единичный
# частичный скан КР875_1 — ~97 cpp при 76% пустых страниц).
SCAN_AVG = 30         # < ~30 значимых символов/стр -> текста по сути нет (скан)
LOW_AVG = 450         # разреженный текст -> на ручную проверку
LOW_EMPTY_FRAC = 0.30  # >30% страниц почти без текста -> частичный скан/OCR-мусор


def analyze(path: str):
    import fitz
    base = os.path.basename(path)
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        return {"file": base, "pages": 0, "chars": 0, "per_page": 0.0,
                "empty_frac": 1.0, "cls": "SKIPPED_SCAN", "note": "open_error:%r" % exc}
    try:
        pages = doc.page_count
        sig_total = 0
        empty = 0
        for page in doc:
            try:
                txt = page.get_text()
            except Exception:
                txt = ""
            s = len(_SIG.findall(txt))
            sig_total += s
            if s < 50:
                empty += 1
    finally:
        doc.close()

    per_page = sig_total / pages if pages else 0.0
    empty_frac = empty / pages if pages else 1.0

    if per_page < SCAN_AVG:
        cls = "SKIPPED_SCAN"        # текста по сути нет — не парсить, не валидировать
    elif per_page < LOW_AVG or empty_frac > LOW_EMPTY_FRAC:
        cls = "LOW_TEXT_REVIEW"     # частичный скan/разреженный текст — парсить + флаг
    else:
        cls = "OK"
    return {"file": base, "pages": pages, "chars": sig_total,
            "per_page": round(per_page, 1), "empty_frac": round(empty_frac, 3),
            "cls": cls, "note": ""}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pdfs = sorted(p for p in glob.glob(os.path.join(RAW, "*.pdf")))
    print("PDF к анализу: %d" % len(pdfs))
    rows = []
    with ProcessPoolExecutor(max_workers=14) as ex:
        for i, r in enumerate(ex.map(analyze, pdfs), 1):
            rows.append(r)
            if i % 100 == 0:
                print("  ...%d/%d" % (i, len(pdfs)))

    rows.sort(key=lambda r: r["per_page"])
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "pages", "chars", "chars_per_page", "empty_frac", "class", "note"])
        for r in rows:
            w.writerow([r["file"], r["pages"], r["chars"], r["per_page"],
                        r["empty_frac"], r["cls"], r["note"]])

    from collections import Counter
    cnt = Counter(r["cls"] for r in rows)
    print("\nКЛАССЫ:", dict(cnt))
    print("scan_candidates.csv записан (%d строк)" % len(rows))
    # распределение per_page для калибровки
    pp = sorted(r["per_page"] for r in rows)
    qs = [pp[int(len(pp) * q)] for q in (0.0, 0.02, 0.05, 0.1, 0.25, 0.5, 0.9)]
    print("per_page квантили [0,2,5,10,25,50,90%%]:", qs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
