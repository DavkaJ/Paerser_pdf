#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Кросс-документная база пинов OCR-восстановления {кривое -> чистое}.

Персистентный словарь глиф-двойников (ВСЬС->BCLC, Ыуег->Liver, Нерабб->Hepatitis),
накопленный по всем обработанным КР. Используется OCR-путём для:
  * гейта кандидата — если в тексте есть кривой якорь из базы (даже без «§»);
  * консистентности — «примагничивания» восстановленных токенов к каноничной форме.

НЕИЗМЕНЯЕМЫЙ ВХОД ПРОГОНА (промпт 02, ПРАВКА 1). Раньше воркеры дописывали накопленные
пары в общий `ocr_pins.json` в хвосте батча — из-за чего результат парсинга документа N
зависел от того, что парсилось до него. Теперь база — версионированный, ревьюируемый,
read-only вход: `load_base()` отдаёт неизменяемый MappingProxyType, а накопленные за прогон
пары уходят в ПРЕДЛОЖЕНИЕ `_corpus/pins_proposed_<runid>.json` (требует ручного ревью),
а не в базу.

Формат файла базы понимается в двух вариантах:
  * v2: {"version": 2, "reviewed_at": "...", "map": {кривое: чистое}}
  * v1: плоский {кривое: чистое}  (обратная совместимость)

БЕЗ ГОНКИ ПРИ БАТЧЕ (ProcessPoolExecutor, spawn): общий `ocr_pins.json` грузится ТОЛЬКО
НА ЧТЕНИЕ; карта каждого документа пишется в ОТДЕЛЬНЫЙ шард `data/pins/<id>.json` атомарно.

Отказоустойчивость: ОТСУТСТВИЕ файла базы — норма (пустая база). Но сбой чтения
СУЩЕСТВУЮЩЕГО файла базы — это FAIL прогона (исключение), а не молчаливый пустой словарь:
парсить корпус неизвестно каким словарём нельзя.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Dict, Optional

from crparser.engine.jsonio import JsonWriter

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_BASE_FILE = os.path.join(_DATA_DIR, "ocr_pins.json")
_SHARD_DIR = os.path.join(_DATA_DIR, "pins")
_PROPOSAL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "_corpus")

# кэш загруженной базы на процесс (воркеры-spawn грузят каждый свой — без гонки)
_base_cache: Optional["MappingProxyType[str, str]"] = None
_PUNCT = ".,;:()[]«»\"'-—%<>±*"


def _clean_map(obj) -> Dict[str, str]:
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in obj.items():
        if isinstance(k, str) and isinstance(v, str) and k and v and k != v:
            out[k] = v
    return out


def _extract_map(obj) -> Dict[str, str]:
    """Достать словарь замен из v2-объекта или из плоского v1-словаря."""
    if isinstance(obj, dict) and "map" in obj and "version" in obj:
        return _clean_map(obj.get("map"))
    return _clean_map(obj)


def load_base() -> "MappingProxyType[str, str]":
    """База {кривое->чистое} (лениво, кэш на процесс), ТОЛЬКО НА ЧТЕНИЕ.

    Возвращает MappingProxyType — попытка мутации падает (база — immutable вход).
    Отсутствие файла -> пустая база. Сбой чтения существующего файла -> исключение
    (FAIL прогона): молча парсить пустым словарём при битой базе нельзя."""
    global _base_cache
    if _base_cache is not None:
        return _base_cache
    data: Dict[str, str] = {}
    if os.path.isfile(_BASE_FILE):
        # НЕ подавляем ошибку: битая существующая база — это провал прогона.
        with open(_BASE_FILE, encoding="utf-8") as fh:
            data = _extract_map(json.load(fh))
    _base_cache = MappingProxyType(dict(data))
    return _base_cache


def anchor_hit(text: str, base=None) -> bool:
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


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_proposal() -> Dict[str, dict]:
    """Свести шарды в ПРЕДЛОЖЕНИЕ новых пар (не в базу).

    Пара попадает в предложение, только если она встретилась МИНИМУМ в 2 разных
    документах (шарды несут присутствие ключа в документе; per-doc частота в шарде
    не хранится, поэтому одиночные случайные пары одного документа не предлагаются).
    Уже присутствующие в базе ключи пропускаются. Значение — доминирующая чистая
    форма среди документов."""
    base = load_base()
    from collections import Counter, defaultdict
    docs_by_key = defaultdict(list)                 # key -> [doc_id, ...]
    vals_by_key = defaultdict(Counter)              # key -> Counter(clean_value)
    if os.path.isdir(_SHARD_DIR):
        for name in sorted(os.listdir(_SHARD_DIR)):
            if not name.endswith(".json"):
                continue
            doc_id = name[:-5]
            try:
                with open(os.path.join(_SHARD_DIR, name), encoding="utf-8") as fh:
                    shard = _clean_map(json.load(fh))
            except Exception:  # noqa: BLE001
                continue
            for k, v in shard.items():
                if k in base:
                    continue
                docs_by_key[k].append(doc_id)
                vals_by_key[k][v] += 1
    proposal: Dict[str, dict] = {}
    for k, docs in docs_by_key.items():
        if len(set(docs)) < 2:                      # порог: >= 2 разных документа
            continue
        clean, _n = vals_by_key[k].most_common(1)[0]
        proposal[k] = {"clean": clean, "docs": sorted(set(docs)), "hits": len(docs)}
    return proposal


def merge_shards(run_id: Optional[str] = None) -> int:
    """НЕ пишет в ocr_pins.json. Собирает предложение новых пар и пишет его в
    `_corpus/pins_proposed_<runid>.json` для ручного ревью. Возвращает число
    ПРЕДЛОЖЕННЫХ пар. Имя сохранено ради обратной совместимости вызова из батча."""
    run_id = run_id or _run_id()
    proposal = build_proposal()
    try:
        os.makedirs(_PROPOSAL_DIR, exist_ok=True)
        path = os.path.join(_PROPOSAL_DIR, "pins_proposed_%s.json" % run_id)
        JsonWriter._atomic_dump(
            {"run_id": run_id, "count": len(proposal), "proposals": proposal}, path)
    except Exception:  # noqa: BLE001 — предложение необязательно для успеха прогона
        pass
    return len(proposal)
