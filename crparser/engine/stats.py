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

# Служебная обвязка страницы (Р1 промпта 10, ГЕОМЕТРИЯ — Phase 0.1, I30 #6): осиротевший
# span, являющийся номером страницы, колонкой номеров оглавления, номером источника в
# списке литературы, маркером нумерованного списка или повторяющимся колонтитулом.
#
# ПРЕЖНЯЯ версия списывала в обвязку по ДЛИНЕ (`len<=2`) — а длина не признак обвязки:
# стадия «II», уровень «5A», ячейка «2.5» тем же порогом молча уходили из lost (дефект
# I30 #6). Перепроверка геометрией (`_corpus/diag_furniture_geometry.py`, выборка 18 док.
# по стратам, вкл. контрольную I1) показала: ВСЕ центральные числовые сироты — это голые
# номера (`bare=1`): TOC-колонка (КР931_1 «6/14/18» рядом с «1.1 Определение…»), номера
# источников (КР845_1 «283», КР848_1 «400»), маркеры списка (КР895_1 «2./3./4.» при
# owned-тексте пункта). Реального КОРОТКОГО ТЕЛА, списанного по длине, в выборке НЕТ
# (furn_central_word=0) — вывод I12 «потерянного тела нет» ПОДТВЕРЖДЁН геометрией.
#
# Отсюда принципиальное (не по длине) правило: голый номер — всегда обвязка; короткий
# спан — обвязка ТОЛЬКО если он у края страницы (колонтитул) или повторяется из страницы
# в страницу; повторяющийся заголовок у края (напр. «1.1 Определение…» вверху каждой
# страницы раздела) — тоже обвязка. Иначе (центральная полоса, не голый номер, не повтор)
# — это НЕ обвязка, а потенциально короткое тело -> lost (surface, не списывать молча).
_RE_FURNITURE = re.compile(r"^\W*\d{1,4}(?:[.\-/]\d{1,4})*[.)]?\W*$")
_FURNITURE_MAX_LEN = 2       # порог «короткого» маркера («•», «§», буквы, римские «V.»)
_FURNITURE_EDGE_FRAC = 0.08  # полоса высоты набора у верх/низ края = зона колонтитула
_FURNITURE_REPEAT_PAGES = 3  # один и тот же спан на >=N страницах = повтор-обвязка


def _is_furniture(text: str, at_edge: bool = False, repeats: bool = False) -> bool:
    """Обвязка ли осиротевший span. Геометрия (`at_edge`/`repeats`) приходит из page_ir;
    без неё (синтетика/тесты без provenance) остаётся только голый-номер признак — но там
    сирот и нет (S == owned), так что порог длины не нужен."""
    t = (text or "").strip()
    if not t:
        return True
    # 1. голый (дотированный) номер — обвязка ВНЕ зависимости от позиции: номер страницы,
    #    колонка номеров оглавления, номер источника, маркер списка «2.» (все они bare).
    if _RE_FURNITURE.match(t):
        return True
    # 2. у края страницы: короткий маркер-колонтитул ИЛИ повторяющийся заголовок-колонтитул.
    if at_edge and (repeats or len(t) <= _FURNITURE_MAX_LEN):
        return True
    # 3. короткий маркер, повторяющийся из страницы в страницу (напр. буллет «•»).
    if repeats and len(t) <= _FURNITURE_MAX_LEN:
        return True
    return False


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


def _span_weight_and_text(page_ir: List[PageIR]) -> Tuple[
        Dict[str, int], Dict[str, str],
        Dict[str, Tuple[int, Any]], Dict[int, Tuple[float, float]]]:
    """span_uid -> (число символов выбранного канала, его текст, геометрия) + полоса
    набора каждой страницы. Источник веса — ИТОГОВЫЙ page_ir (промпт 08): вес span'а =
    длина текста ВЫБРАННОГО канала. При коллизии uid (клоны inline-разбиения делят один
    span_uid) берём наибольший вес; геометрия у клонов одна (span_uid = f(page, bbox)).
    Полоса набора страницы (min y0 .. max y1 по ВСЕМ её спанам) нужна классификатору
    обвязки, чтобы определить «у края» без размеров страницы (их page_ir не несёт)."""
    weight: Dict[str, int] = {}
    text: Dict[str, str] = {}
    geom: Dict[str, Tuple[int, Any]] = {}                 # uid -> (page, bbox)
    page_band: Dict[int, Tuple[float, float]] = {}        # page -> (y0_min, y1_max)
    for pir in page_ir:
        for sp in pir.spans:
            t = sp.candidates.get(sp.selected, "") or ""
            if len(t) >= weight.get(sp.span_uid, -1):
                weight[sp.span_uid] = len(t)
                text[sp.span_uid] = t
            geom.setdefault(sp.span_uid, (sp.page, sp.bbox))
            y0, y1 = sp.bbox[1], sp.bbox[3]
            lo, hi = page_band.get(sp.page, (float("inf"), float("-inf")))
            page_band[sp.page] = (min(lo, y0), max(hi, y1))
    return weight, text, geom, page_band


def coverage_v2_from_objects(sections: List[Section], tables: List[Table],
                             excluded: Dict[str, List], page_ir: List[PageIR],
                             accounted_raw: int, total_chars: int) -> Dict[str, Any]:
    """coverage_v2 из датаклассов движка + page_ir (авторитетные веса span'ов)."""
    weight, text, geom, page_band = _span_weight_and_text(page_ir)
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

    return _coverage_v2_core(units, weight, text, ir_uids, accounted_raw, total_chars,
                             geom=geom, page_band=page_band)


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
    return _coverage_v2_core(units, {}, {}, set(), accounted_raw, total,
                             geom={}, page_band={})


def _coverage_v2_core(units: List[Dict[str, Any]], weight: Dict[str, int],
                      text: Dict[str, str], ir_uids: set,
                      accounted_raw: int, total_chars: int,
                      geom: Optional[Dict[str, Tuple[int, Any]]] = None,
                      page_band: Optional[Dict[int, Tuple[float, float]]] = None,
                      ) -> Dict[str, Any]:
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
    # обвязка по ГЕОМЕТРИИ (Phase 0.1, I30 #6): «у края» — из полосы набора страницы,
    # «повтор» — один и тот же текст-сирота на >=N страницах. Без geom (синтетика) обе
    # величины False -> остаётся только голый-номер признак.
    geom = geom or {}
    page_band = page_band or {}
    orphan_pages: Dict[str, set] = {}
    for u in lost_all:
        orphan_pages.setdefault((text.get(u, "") or "").strip(), set())
        pg = geom.get(u)
        if pg is not None:
            orphan_pages[(text.get(u, "") or "").strip()].add(pg[0])

    def _at_edge(u: str) -> bool:
        pg = geom.get(u)
        if pg is None:
            return False
        page, bbox = pg
        band = page_band.get(page)
        if not band:
            return False
        lo, hi = band
        h = (hi - lo) or 1.0
        y0, y1 = bbox[1], bbox[3]
        return (y0 - lo) / h < _FURNITURE_EDGE_FRAC or (hi - y1) / h < _FURNITURE_EDGE_FRAC

    lost, furniture = [], 0
    for u in lost_all:
        t = text.get(u, "")
        repeats = len(orphan_pages.get((t or "").strip(), ())) >= _FURNITURE_REPEAT_PAGES
        if _is_furniture(t, at_edge=_at_edge(u), repeats=repeats):
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
