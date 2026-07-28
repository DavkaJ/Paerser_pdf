# -*- coding: utf-8 -*-
"""
Кэш разбора оглавления по содержимому PDF (промпт 00).

Временная мера до единого DocumentView (промпт 16). Сейчас каждый прогон
report.json читает КАЖДЫЙ PDF ДВАЖДЫ: один раз парсер, второй — parse_toc в
валидаторе. Оглавление зависит от содержимого PDF, кода его разбора И состояния
OCR-стека (parse_toc гибрид-OCR-ит кандидатов), поэтому кэшируется по всем трём:

  crparser/data/toc_cache/<pdf_sha12>_<toc_code_sha8>_<ocr_stack_sha8>.json

(параметр code_sha8 несёт склейку toc_code+ocr_stack — см. validate._toc_cache_key)

Ключ включает хеш кода разбора оглавления (toc.py + parse_toc + якоря) И отпечаток
OCR-стека (версия Tesseract/traineddata/Pillow/DPI/доступность). Кэш АВТОМАТИЧЕСКИ
инвалидируется при смене правил разбора (напр. римские номера) ИЛИ смене OCR-стека —
устаревшее/иначе-восстановленное оглавление молча не подсунется. Это тот же класс
защиты, что у content-addressed OCR-кэша (промпт 02).

Содержимое: {"entries": [[number, title], ...] | null, "body_norm": "..."}.
`entries: null` кэширует случай «оглавление не извлечено» (parse_toc вернул None),
чтобы такие PDF тоже не открывались повторно.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from crparser.engine.jsonio import JsonWriter

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "toc_cache")

_MISSING = object()   # «нет кэш-файла» — отличать от кэшированного None-оглавления


def _path(pdf_sha12: str, code_sha8: str) -> str:
    return os.path.join(_CACHE_DIR, "%s_%s.json" % (pdf_sha12, code_sha8))


def load(pdf_sha12: str, code_sha8: str):
    """Вернуть {"entries":..., "body_norm":...} из кэша, либо _MISSING, если файла нет.
    Сбой чтения трактуем как промах (пересчёт), не как ошибку."""
    path = _path(pdf_sha12, code_sha8)
    if not os.path.isfile(path):
        return _MISSING
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — битый кэш = промах
        return _MISSING


def store(pdf_sha12: str, code_sha8: str,
          entries: Optional[list], body_norm: str) -> None:
    """Атомарно записать разбор оглавления в кэш. Сбой I/O не роняет валидацию."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        JsonWriter._atomic_dump(
            {"entries": entries, "body_norm": body_norm},
            _path(pdf_sha12, code_sha8))
    except Exception:  # noqa: BLE001 — кэш необязателен
        pass
