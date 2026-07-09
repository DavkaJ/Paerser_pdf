#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Кросс-документная база пинов OCR-восстановления {кривое -> чистое}.

Персистентный словарь глиф-двойников (ВСЬС->BCLC, Ыуег->Liver, Нерабб->Hepatitis),
накопленный по всем обработанным КР. Используется OCR-путём для:
  * гейта кандидата — если в тексте есть кривой якорь из базы (даже без «§»);
  * консистентности — «примагничивания» восстановленных токенов к каноничной форме.

БЕЗ ГОНКИ ПРИ БАТЧЕ (ProcessPoolExecutor, spawn): общий `ocr_pins.json` грузится
ТОЛЬКО НА ЧТЕНИЕ; карта каждого документа пишется в ОТДЕЛЬНЫЙ шард
`data/pins/<id>.json` атомарно (temp+fsync+os.replace через JsonWriter). Слияние
шардов в общий файл — единичным процессом в хвосте batch_report.main.

Все операции отказоустойчивы: любой сбой I/O не роняет парсинг (база — кэш-подсказка).
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, Optional

from crparser.engine.jsonio import JsonWriter

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_BASE_FILE = os.path.join(_DATA_DIR, "ocr_pins.json")
_SHARD_DIR = os.path.join(_DATA_DIR, "pins")

# кэш загруженной базы на процесс (воркеры-spawn грузят каждый свой — без гонки)
_base_cache: Optional[Dict[str, str]] = None
_PUNCT = ".,;:()[]«»\"'-—%<>±*"


def _clean_map(obj) -> Dict[str, str]:
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in obj.items():
        if isinstance(k, str) and isinstance(v, str) and k and v and k != v:
            out[k] = v
    return out


def load_base() -> Dict[str, str]:
    """База {кривое->чистое} из ocr_pins.json (лениво, кэш на процесс). {} при сбое."""
    global _base_cache
    if _base_cache is not None:
        return _base_cache
    data: Dict[str, str] = {}
    try:
        if os.path.isfile(_BASE_FILE):
            with open(_BASE_FILE, encoding="utf-8") as fh:
                data = _clean_map(json.load(fh))
    except Exception:  # noqa: BLE001 — база необязательна
        data = {}
    _base_cache = data
    return data


def anchor_hit(text: str, base: Optional[Dict[str, str]] = None) -> bool:
    """В тексте есть кривые якоря из базы (токены == кривая форма). Ключи базы —
    в нижнем регистре (нормализованное «ядро» двойника), поэтому токен тоже берём
    в нижнем регистре. Требуем >= 2 попаданий: единичное случайное совпадение не
    делает чистый документ кандидатом. Для чистого файла (двойников нет) — False."""
    base = load_base() if base is None else base
    if not base:
        return False
    keys = base.keys()
    hits = 0
    for tok in text.split():
        core = tok.strip(_PUNCT).lower()
        if len(core) >= 3 and core in keys:
            hits += 1
            if hits >= 2:
                return True
    return False


def _shard_path(doc_id: str) -> str:
    safe = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_.-]", "_", str(doc_id)) or "doc"
    return os.path.join(_SHARD_DIR, safe + ".json")


def write_shard(doc_id: str, mapping: Dict[str, str]) -> None:
    """Атомарно записать карту документа в свой шард (рабочий кэш). Пусто -> no-op."""
    mapping = _clean_map(mapping)
    if not mapping:
        return
    try:
        os.makedirs(_SHARD_DIR, exist_ok=True)
        JsonWriter._atomic_dump(mapping, _shard_path(doc_id))
    except Exception:  # noqa: BLE001 — шард необязателен
        pass


def merge_shards() -> int:
    """Свести все шарды в общий ocr_pins.json (единичный процесс, хвост батча).

    Существующие записи базы не перетираем (первое чистое значение выигрывает —
    стабильность). Возвращает число НОВЫХ ключей. Отказоустойчиво."""
    base: Dict[str, str] = {}
    try:
        if os.path.isfile(_BASE_FILE):
            with open(_BASE_FILE, encoding="utf-8") as fh:
                base = _clean_map(json.load(fh))
    except Exception:  # noqa: BLE001
        base = {}
    added = 0
    if os.path.isdir(_SHARD_DIR):
        for name in sorted(os.listdir(_SHARD_DIR)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(_SHARD_DIR, name), encoding="utf-8") as fh:
                    shard = _clean_map(json.load(fh))
            except Exception:  # noqa: BLE001
                continue
            for k, v in shard.items():
                if k not in base:
                    base[k] = v
                    added += 1
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        JsonWriter._atomic_dump(base, _BASE_FILE)
    except Exception:  # noqa: BLE001
        pass
    return added
