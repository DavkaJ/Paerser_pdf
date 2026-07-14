#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Статистика покрытия.

Движок-уровень. Две метрики, отвечающие на РАЗНЫЕ вопросы:

  * `coverage_percent` (прежняя) — «бухгалтерия символов»: доля исходного текста,
    учтённого хоть где-то (разделы+исключения+таблицы), с обрезкой `min()` до 100%.
    Текст, попавший НЕ в тот раздел, свалившийся в `excluded.other` или
    ПРОДУБЛИРОВАННЫЙ между buckets, тоже считается «покрытым». Поле сохранено
    байт-в-байт (downstream его читает), но в схеме помечено deprecated.

  * `coverage_v2` (промпт 10) — сохранность СТРУКТУРЫ по span-union (промпт 08),
    а не по длинам строк. Каждый исходный span учитывается РОВНО ОДИН раз (union),
    поэтому дубли между buckets не могут дать >100%, а провал структуры («1 символ
    в разделе + 99 в excluded») виден там, где старая метрика показывает 100%.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from crparser.engine.models import PageIR, Section, Table

# Служебная обвязка страницы (Р1 промпта 10): осиротевший span, чей текст — номер
# страницы, ОДИНОКИЙ номер раздела оглавления/заголовка («1.2.», «2.3.1.», «05.9»)
# или короткий колонтитульный маркер. Замер корпуса: сироты = 0.102% символов, 98.11%
# — номера страниц (медиана 2 симв), сирот >100 симв — ноль; остаток — колонка номеров
# оглавления, оторванная от заголовка. Такие span'ы НЕ «потерянный контент»: их
# отделяем в класс page_furniture, а не в lost_spans. Голый номер (без текста после)
# — обвязка; «1.6 Классификация…» (номер + заголовок) — уже НЕ обвязка (реальная строка).
_RE_FURNITURE = re.compile(r"^\W*\d{1,4}(?:[.\-/]\d{1,4})*[.)]?\W*$")
_FURNITURE_MAX_LEN = 2   # очень короткие сироты (маркеры «•», «§», буквы) — тоже обвязка


def _is_furniture(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if len(t) <= _FURNITURE_MAX_LEN:
        return True
    return bool(_RE_FURNITURE.match(t))


class StatsCalculator:
    """Считает счётчики, coverage_percent (прежний) и coverage_v2 (span-union)."""

    def compute(
        self,
        full_text: str,
        sections: List[Section],
        excluded: Dict[str, List[Dict[str, str]]],
        tables: List[Table],
        page_ir: Optional[List[PageIR]] = None,
    ) -> Dict[str, Any]:
        total_chars = len(full_text)
        total_words = len(full_text.split())

        flat = list(self._iter_sections(sections))
        included_chars = sum(len(s.title) + len(s.text) for s in flat)

        excluded_chars = 0
        for bucket in excluded.values():
            for item in bucket:
                excluded_chars += len(item.get("title", "")) + len(item.get("text", ""))

        table_chars = sum(len(t.raw_text) + len(t.caption or "") for t in tables)

        accounted_raw = included_chars + excluded_chars + table_chars
        # дубли подписей/таблиц не должны давать >100%
        accounted_chars = min(accounted_raw, total_chars)
        # Вырожденный случай: в разделы не попало НИЧЕГО (included_chars==0), но
        # excluded/таблицы добирают accounted до ~100% -> фиктивное «покрытие».
        # Такой файл покрытым не считаем: coverage=0. Для нормальных файлов
        # (included_chars>0) формула прежняя — вывод байт-в-байт не меняется.
        if total_chars and included_chars:
            coverage = round(accounted_chars / total_chars * 100, 2)
        else:
            coverage = 0.0

        stats = {
            "total_chars": total_chars,
            "total_words": total_words,
            "sections_found": len(flat),
            "tables_found": len(tables),
            "included_chars": included_chars,
            "excluded_chars": excluded_chars,
            "table_chars": table_chars,
            "accounted_chars": accounted_chars,
            "coverage_percent": coverage,
        }
        stats["coverage_v2"] = coverage_v2_from_objects(
            sections, tables, excluded, page_ir or [],
            accounted_raw=accounted_raw, total_chars=total_chars)
        return stats

    @classmethod
    def _iter_sections(cls, sections: List[Section]):
        for section in sections:
            yield section
            yield from cls._iter_sections(section.children)


# --------------------------------------------------------------------------- #
# coverage_v2: span-union по классам TRAINING / NON_TRAINING / GARBAGE          #
# --------------------------------------------------------------------------- #
#
# Классы span'ов (промпт 10):
#   TRAINING (T)     = sections ∪ tables ∪ excluded.appendices  — ради чего всё
#   NON_TRAINING (N) = excluded.{toc,front_matter,references}   — законно вне обучения
#   GARBAGE (G)      = excluded.other                           — корзина сегментера
# S — все span'ы документа (из page_ir); D = S \ N — что ДОЛЖНО было лечь в структуру.

# excluded-бакет -> класс span'ов
_BUCKET_CLASS = {
    "appendices": "T",
    "toc": "N", "front_matter": "N", "references": "N",
    "other": "G",
}


def _span_weight_and_text(page_ir: List[PageIR]) -> Tuple[Dict[str, int], Dict[str, str]]:
    """span_uid -> (число символов выбранного канала, его текст). Источник веса —
    ИТОГОВЫЙ page_ir (промпт 08): вес span'а = длина текста ВЫБРАННОГО канала. При
    коллизии uid (клоны inline-разбиения делят один span_uid) берём наибольший вес."""
    weight: Dict[str, int] = {}
    text: Dict[str, str] = {}
    for pir in page_ir:
        for sp in pir.spans:
            t = sp.candidates.get(sp.selected, "") or ""
            if len(t) >= weight.get(sp.span_uid, -1):
                weight[sp.span_uid] = len(t)
                text[sp.span_uid] = t
    return weight, text


def coverage_v2_from_objects(sections: List[Section], tables: List[Table],
                             excluded: Dict[str, List], page_ir: List[PageIR],
                             accounted_raw: int, total_chars: int) -> Dict[str, Any]:
    """coverage_v2 из датаклассов движка + page_ir (авторитетные веса span'ов)."""
    weight, text = _span_weight_and_text(page_ir)
    ir_uids = set(weight)

    units: List[Dict[str, Any]] = []
    for s in _walk_sections(sections):
        units.append({"klass": "T", "uids": list(s.span_uids),
                      "chars": len(s.title or "") + len(s.text or "")})
    for t in tables:
        units.append({"klass": "T", "uids": list(t.claimed_span_uids),
                      "chars": len(t.raw_text or "") + len(t.caption or "")})
    for bucket, items in excluded.items():
        klass = _BUCKET_CLASS.get(bucket, "G")
        for item in items:
            units.append({"klass": klass, "uids": list(item.get("span_uids", []) or []),
                          "chars": len(item.get("title", "") or "")
                          + len(item.get("text", "") or "")})

    return _coverage_v2_core(units, weight, text, ir_uids, accounted_raw, total_chars)


def coverage_v2_from_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """coverage_v2 из JSON-дока БЕЗ page_ir (синтетика/тесты). Без provenance
    у span'ов нет весов и множество S неизвестно, поэтому каждый unit становится
    ОДНИМ псевдо-span'ом весом в свои символы (путь pseudo-span в ядре). Для реальных
    документов авторитетна coverage_v2_from_objects (её пишет парсер в stats)."""
    stats = doc.get("stats", {}) or {}
    total = stats.get("total_chars") or 0
    inc = stats.get("included_chars")
    exc = stats.get("excluded_chars")
    tab = stats.get("table_chars")
    if inc is None or exc is None or tab is None:
        inc = sum(len(s.get("title") or "") + len(s.get("text") or "")
                  for s in _walk_dict(doc.get("sections", [])))
        exc = sum(len(i.get("title", "") or "") + len(i.get("text", "") or "")
                  for b in (doc.get("excluded", {}) or {}).values() for i in b)
        tab = sum(len(t.get("raw_text") or "") + len(t.get("caption") or "")
                  for t in (doc.get("tables", []) or []))
    accounted_raw = inc + exc + tab

    units: List[Dict[str, Any]] = []
    for s in _walk_dict(doc.get("sections", [])):
        units.append({"klass": "T", "uids": list(s.get("span_uids", []) or []),
                      "chars": len(s.get("title") or "") + len(s.get("text") or "")})
    for t in (doc.get("tables", []) or []):
        units.append({"klass": "T", "uids": list(t.get("claimed_span_uids", []) or []),
                      "chars": len(t.get("raw_text") or "") + len(t.get("caption") or "")})
    for bucket, items in (doc.get("excluded", {}) or {}).items():
        klass = _BUCKET_CLASS.get(bucket, "G")
        for item in items:
            units.append({"klass": klass, "uids": list(item.get("span_uids", []) or []),
                          "chars": len(item.get("title", "") or "")
                          + len(item.get("text", "") or "")})
    return _coverage_v2_core(units, {}, {}, set(), accounted_raw, total)


def _coverage_v2_core(units: List[Dict[str, Any]], weight: Dict[str, int],
                      text: Dict[str, str], ir_uids: set,
                      accounted_raw: int, total_chars: int) -> Dict[str, Any]:
    """Ядро coverage_v2. Работает по span_uid; вес span'а — число символов.

    Два режима:
      * есть provenance (page_ir непуст ИЛИ у любого unit есть span_uids) — считаем
        по реальным span_uid; unit без span_uid (нереконсилированная таблица) НЕ
        добавляет span'ов (его текст уже принадлежит секциям через их span'ы —
        второй раз считать нельзя);
      * нет provenance (синтетика) — каждый unit = ОДИН псевдо-span весом в свои
        символы (иначе метрику не на чём считать)."""
    has_prov = bool(ir_uids) or any(u["uids"] for u in units)

    weight = dict(weight)
    owner_count: Dict[str, int] = {}
    class_uids: Dict[str, set] = {"T": set(), "N": set(), "G": set()}

    for i, u in enumerate(units):
        uids = u["uids"]
        if not uids:
            if has_prov:
                continue                     # реальный unit без span'ов — не считаем
            uid = "_pseudo_%d" % i           # синтетика: один псевдо-span
            weight[uid] = u["chars"]
            uids = [uid]
        seen = set()
        for uid in uids:
            class_uids[u["klass"]].add(uid)
            if uid not in seen:
                seen.add(uid)
                owner_count[uid] = owner_count.get(uid, 0) + 1

    T, N, G = class_uids["T"], class_uids["N"], class_uids["G"]
    owned = T | N | G
    S = ir_uids | owned
    D = S - N

    def w(uids: set) -> int:
        return sum(weight.get(u, 0) for u in uids)

    wS, wD = w(S), w(D)
    source_retention = (w(owned & S) / wS) if wS else 1.0
    structured_coverage = (w(T & D) / wD) if wD else 0.0
    garbage_ratio = (w(G) / wS) if wS else 0.0

    overlap = {u for u, c in owner_count.items() if c > 1}
    overlap_chars = w(overlap)

    lost_all = S - owned
    lost, furniture = [], 0
    for u in lost_all:
        if _is_furniture(text.get(u, "")):
            furniture += 1
        else:
            lost.append(u)

    overcount_ratio = (accounted_raw / total_chars) if total_chars else 0.0

    return {
        "source_retention": round(source_retention, 4),
        "structured_coverage": round(structured_coverage, 4),
        "garbage_ratio": round(garbage_ratio, 4),
        "overlap_spans": len(overlap),
        "overlap_chars": overlap_chars,
        "lost_spans": len(lost),
        "page_furniture": furniture,
        "overcount_ratio": round(overcount_ratio, 4),
        "total_spans": len(S),
        # первые 20 реально потерянных (не-обвязка) span_uid — доказуемый список
        "lost_span_uids": sorted(lost)[:20],
    }


def _walk_sections(sections: List[Section]):
    for s in sections:
        yield s
        yield from _walk_sections(s.children)


def _walk_dict(sections: List[Dict[str, Any]]):
    for s in sections:
        yield s
        yield from _walk_dict(s.get("children", []) or [])
