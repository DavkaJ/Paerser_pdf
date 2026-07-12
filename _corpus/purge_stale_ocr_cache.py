# -*- coding: utf-8 -*-
"""
Утилита очистки устаревшего OCR-кэша (промпт 02, ПРАВКА 2).

После смены компонента стека (Tesseract/traineddata/Pillow/dpi/preproc) или
изменения PDF старые content-addressed файлы кэша просто не находятся — это
ПРАВИЛЬНОЕ поведение (пересчёт), а не ошибка. Старый кэш не удаляется автоматически;
эта утилита чистит его вручную.

    python _corpus/purge_stale_ocr_cache.py --dry-run   # показать, что удалит
    python _corpus/purge_stale_ocr_cache.py --purge-stale

«Устаревший» = файл кэша, чей отпечаток стека (stack12 в имени) не совпадает с
текущим для его langs/dpi/preproc. Имя актуального кэша:
  <pdf_sha12>_p<page>_<stack12>_psm<N>.json  и  <pdf_sha12>_clip_...
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CACHE = os.path.join(ROOT, "crparser", "data", "ocr_cache")


def current_stack12() -> str:
    from crparser.engine.ocr import (
        _stack_signature, _resolve_tesseract, _DEFAULT_DPI, _PREPROC_VERSION)
    cmd = _resolve_tesseract()
    tessdata = os.environ.get("TESSDATA_PREFIX") or None
    langs = os.environ.get("OCR_LANGS", "rus+eng")
    dpi = max(int(os.environ.get("OCR_DPI", _DEFAULT_DPI)), 300)
    return _stack_signature(cmd, tessdata, langs, dpi, _PREPROC_VERSION)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--purge-stale", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not os.path.isdir(CACHE):
        print("кэш пуст:", CACHE)
        return 0
    stack = current_stack12()
    print("текущий stack12 =", stack)
    stale, keep = [], 0
    for name in sorted(os.listdir(CACHE)):
        if not name.endswith(".json"):
            continue
        # актуальные имена содержат текущий stack12; всё остальное — устаревшее
        if stack in name:
            keep += 1
        else:
            stale.append(name)
    print("актуальных: %d, устаревших: %d" % (keep, len(stale)))
    if args.dry_run or not args.purge_stale:
        for n in stale[:50]:
            print("  устарел:", n)
        if not args.purge_stale:
            print("\n(dry-run по умолчанию; для удаления передайте --purge-stale)")
        return 0
    removed = 0
    for n in stale:
        try:
            os.remove(os.path.join(CACHE, n))
            removed += 1
        except OSError:
            pass
    print("удалено:", removed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
