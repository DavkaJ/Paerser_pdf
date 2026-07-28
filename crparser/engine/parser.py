#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Оркестратор движка.

`DocumentParser` связывает все компоненты движка и работает ТОЛЬКО через
абстракцию `DocumentProfile`. Он не знает, КР перед ним или иной документ:
весь доменный смысл инкапсулирован в профиле.

Конвейер:
    PDF --PdfReader--> страницы(layout)
        --TableExtractor--> таблицы + карта вычитания
        --profile.extract_metadata--> метаданные
        --Segmenter(profile)--> разделы + исключения
        --StatsCalculator--> покрытие
        --> ParseResult
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Dict, List

from crparser.engine.models import (
    BBox, ExcludedItem, MetadataContext, PageIR, ParseResult, Page, Section, Table)
from crparser.engine.pdf_reader import PdfReader, build_page_ir
from crparser.engine.segmenter import Segmenter
from crparser.engine.stats import StatsCalculator
from crparser.engine.tables import TableExtractor
from crparser.engine.textnorm import (
    control_char_count, glyph_suspect_count, pseudo_ascii_counts,
    pseudo_ascii_glyph_tokens)
from crparser.profiles.base import DocumentProfile


def _bbox_overlaps(a: BBox, b: BBox, pad: float = 1.0) -> bool:
    """Прямоугольники строки и таблицы пересекаются (с малым допуском pad).

    Пересечение (а не «центр внутри») выбрано намеренно: строки, чей центр лёг ВНЕ
    bbox таблицы (потому и не вычтены сегментером), но которые физически заходят в
    её область, честно оказываются заявлены И разделом, И таблицей — так инвариант
    владения ДЕЛАЕТ ВИДИМЫМ двойной учёт (аудит §4.2 про КР628_2). Их чинят 09/10."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 - pad or ax0 > bx1 + pad
                or ay1 < by0 - pad or ay0 > by1 + pad)


def _walk_sections(sections: List[Section]):
    """Рекурсивный обход дерева разделов (узел + все потомки)."""
    for s in sections:
        yield s
        yield from _walk_sections(s.children)


class DocumentParser:
    """Парсит один PDF выбранным профилем в ParseResult."""

    def __init__(self, profile: DocumentProfile, latin_recovery: bool = False,
                 latin_queue_dir: str = None) -> None:
        self._profile = profile
        self._stats = StatsCalculator()
        # промпт 13b: восстановление латиницы за флагом. По умолчанию ВЫКЛ -> вывод
        # байт-в-байт baseline (резолвер не вызывается).
        self._latin_recovery = latin_recovery
        self._latin_queue_dir = latin_queue_dir

    def parse(self, pdf_path: str) -> ParseResult:
        warnings_list: List[str] = []
        source_file = os.path.basename(pdf_path)

        # 1. layout-чтение (внутри — ГИБРИД-OCR для §-кандидатов, если доступен)
        reader = PdfReader(pdf_path)
        try:
            pages = reader.read()
            norm_stats = dict(reader.norm_stats)
            full_text = reader.full_text(pages)
            # ПОЛНЫЙ OCR: годного текста НЕТ (скан) ИЛИ тотальная «обратная» порча
            # (кириллица->ASCII). Пересобираем документ из полного OCR, ПОКА
            # reader._doc открыт; при недоступном OCR оставляем прежнее поведение.
            pseudo_ascii = pseudo_ascii_glyph_tokens(
                norm_stats.get("sig", 0), norm_stats.get("pseudo", 0))
            if not full_text.strip() or pseudo_ascii > 0:
                ocr_pages = self._full_ocr(reader, warnings_list)
                if ocr_pages:
                    pages = ocr_pages
                    full_text = reader.full_text(pages)
                    norm_stats = self._recount_corruption(pages)
                    pseudo_ascii = pseudo_ascii_glyph_tokens(
                        norm_stats.get("sig", 0), norm_stats.get("pseudo", 0))
            body_size = reader.body_size(pages)
            first_page_text = reader.first_page_text()
        finally:
            reader.close()

        if not full_text.strip():
            warnings_list.append(
                "PDF без текстового слоя (вероятно скан) — разделы и таблицы не "
                "извлечены; метаданные взяты только из реестра")

        # 2. таблицы по bbox + карта вычитания
        extractor = TableExtractor(pdf_path)
        tables = extractor.extract(pages, warnings_list)

        # 3. метаданные (через профиль)
        ctx = MetadataContext(
            pdf_path=pdf_path,
            source_file=source_file,
            full_text=full_text,
            first_page_text=first_page_text,
            pages=pages,
            registry=self._profile.registry,   # часть контракта профиля (промпт 11)
        )
        # явный контракт вместо ключа-призрака metadata["_warnings"] (промпт 11)
        meta_res = self._profile.metadata_result(ctx)
        metadata = meta_res.metadata
        metadata.setdefault("source_file", source_file)
        metadata.setdefault("document_type", self._profile.document_type)
        warnings_list.extend(meta_res.warnings)

        # 4. нарезка на разделы/исключения (через профиль)
        segmenter = Segmenter(self._profile, body_size)
        segmented = segmenter.segment(pages, extractor.subtraction_map, warnings_list)
        sections = segmented["sections"]
        excluded = segmented["excluded"]

        # 4b. provenance (промпт 08/09): PageIR по ИТОГОВЫМ страницам; заявку таблиц
        # на спаны (claimed_span_uids) теперь ставит САМ экстрактор — только у
        # reconciled-таблиц (промпт 09), поэтому геометрический _assign_table_claims
        # больше не вызывается. Инвариант владения (дубли/сироты -> warnings, НЕ падаем).
        page_ir = build_page_ir(pages)
        self._ownership_check(sections, tables, excluded, page_ir, warnings_list)

        # 4c. latin recovery (промпт 13b, за флагом): чинит латиницу в обучаемой зоне
        # (sections/tables/metadata/excluded.appendices) с провенансом. Мутирует текст
        # ПО МЕСТУ — ДО подсчёта stats, иначе included_chars разойдётся с фактом текста
        # (STATS_MISMATCH). При выключенном флаге не вызывается -> вывод байт-в-байт baseline.
        latin_recovery: List[Dict] = []
        latin_unresolved: List[Dict] = []
        latin_queue: List[Dict] = []
        if self._latin_recovery:
            latin_recovery, latin_unresolved, latin_queue = self._run_latin_recovery(
                pdf_path, metadata, sections, tables, excluded, warnings_list)

        # 5. статистика покрытия (coverage_v2 по span-union — из page_ir). Считается
        # ПОСЛЕ latin recovery, чтобы char-бухгалтерия соответствовала итоговому тексту.
        stats = self._stats.compute(full_text, sections, excluded, tables, page_ir)
        # счётчики порчи текста: разрядка/удвоение (починены) + глиф-токены
        # (обнаружены, на OCR) — валидатор по ним поднимает статус CORRUPTION.
        glyph_regions = getattr(extractor, "corrupt_count", 0)
        # «обратная» глифовая порча (кириллица->ASCII): засчитываем токены на OCR
        # только при ТОТАЛЬНОЙ порче по документу (плотностной порог), иначе 0 —
        # единичные лаб-токены (>50%, p<0.05) в здоровом файле не считаются.
        pseudo_ascii = pseudo_ascii_glyph_tokens(
            norm_stats.get("sig", 0), norm_stats.get("pseudo", 0))
        stats["corruption"] = {
            "spacing_fixed": norm_stats.get("spacing", 0),
            "doubling_fixed": norm_stats.get("doubling", 0),
            "glyph_tokens": norm_stats.get("glyph", 0),
            "glyph_regions": glyph_regions,
            "pseudo_ascii_tokens": pseudo_ascii,
            "control_chars": norm_stats.get("control", 0),
        }
        if stats["coverage_percent"] < 50:
            warnings_list.append(
                f"низкое покрытие ({stats['coverage_percent']}%) — текст мог уйти "
                f"в исключения или не распознались заголовки")

        return ParseResult(
            metadata=metadata,
            sections=sections,
            tables=tables,
            excluded=excluded,
            stats=stats,
            warnings=warnings_list,
            page_ir=page_ir,
            latin_recovery=latin_recovery,
            latin_unresolved_critical=latin_unresolved,
            latin_queue=latin_queue,
        )

    def _run_latin_recovery(self, pdf_path, metadata, sections, tables, excluded,
                            warnings_list):
        """Прогнать резолвер латиницы (промпт 13b). Изолировано; любая ошибка -> warning,
        документ не падает (deградируем мягко, как остальной конвейер)."""
        from crparser.engine.latinrecovery import LatinRecoverer
        try:
            doc_id = os.path.splitext(os.path.basename(pdf_path))[0]
            rec = LatinRecoverer(pdf_path, doc_id, queue_dir=self._latin_queue_dir)
            rec.recover(sections, tables, excluded, metadata)
            rec.close()
            if rec.unresolved_critical:
                warnings_list.append(
                    "latin: %d неразрешённых КРИТИЧЕСКИХ латинских сущностей "
                    "(документ на карантин, промпт 13b)" % len(rec.unresolved_critical))
            return rec.prov, rec.unresolved_critical, rec.queue
        except Exception as exc:  # noqa: BLE001
            warnings_list.append(f"latin recovery не выполнен ({exc!r})")
            return [], [], []

    # ---- provenance: заявка таблиц и инвариант владения (промпт 08) -----------

    @staticmethod
    def _assign_table_claims(tables: List[Table], pages: List[Page]) -> None:
        """Проставить каждой таблице span_uids строк, чьи bbox пересекают её область.
        Источник дампа — pdfplumber/нативный слой, поэтому source='native'."""
        by_page: Dict[int, List] = defaultdict(list)
        for page in pages:
            for ln in page.lines:
                by_page[page.number].append(ln)
        for t in tables:
            claimed: List[str] = []
            seen: set = set()
            for ln in by_page.get(t.page, []):
                if _bbox_overlaps(ln.bbox, t.bbox) and ln.span_uid not in seen:
                    seen.add(ln.span_uid)
                    claimed.append(ln.span_uid)
            t.claimed_span_uids = claimed
            t.source = "native"

    @staticmethod
    def _ownership_check(sections: List[Section], tables: List[Table],
                         excluded: Dict[str, List[ExcludedItem]],
                         page_ir: List[PageIR], warnings: List[str]) -> None:
        """Инвариант владения: каждый span_uid имеет НЕ БОЛЕЕ одного владельца среди
        sections ∪ tables ∪ excluded. Дубли ОЖИДАЕМЫ (их источник чинят 09 и 10) —
        НЕ падаем, а делаем видимыми: пишем в warnings число и первые 10. Спаны без
        владельца — тоже в warnings. Формулировки БЕЗ подстроки 'ocr' (иначе
        сработал бы гейт OCR_REQUIRED валидатора)."""
        owners: Dict[str, set] = defaultdict(set)   # span_uid -> множество владельцев
        for s in _walk_sections(sections):
            key = ("section", id(s))
            for uid in set(s.span_uids):
                owners[uid].add(key)
        for t in tables:
            key = ("table", id(t))
            for uid in set(t.claimed_span_uids):
                owners[uid].add(key)
        for bucket in excluded.values():
            for item in bucket:
                key = ("excluded", id(item))
                for uid in set(item.span_uids):
                    owners[uid].add(key)

        dup = sorted(uid for uid, ow in owners.items() if len(ow) > 1)
        all_uids: set = set()
        for pir in page_ir:
            for sp in pir.spans:
                all_uids.add(sp.span_uid)
        orphans = sorted(all_uids - set(owners))

        if dup:
            warnings.append(
                "provenance: %d спанов заявлены более чем одним владельцем "
                "(дубли; источник чинят промпты 09/10): %s"
                % (len(dup), ", ".join(dup[:10])))
        if orphans:
            warnings.append(
                "provenance: %d спанов без владельца: %s"
                % (len(orphans), ", ".join(orphans[:10])))

    # ---- OCR-восстановление (изолировано в crparser.engine.ocr) --------------

    def _full_ocr(self, reader: PdfReader, warnings_list: List[str]):
        """Собрать страницы из ПОЛНОГО OCR (скан/тотальная порча) до close()
        reader. Пустой список -> прежнее поведение (OCR недоступен/пусто)."""
        from crparser.engine.ocr import OcrRecoverer
        try:
            recoverer = OcrRecoverer()
            if not recoverer.available():
                return []      # OCR не настроен — тихо, поведение как без OCR
            doc_id = os.path.splitext(os.path.basename(reader._path))[0]
            from crparser.engine.pdf_reader import _pdf_sha256
            pages = recoverer.full_ocr(reader.doc, doc_id=doc_id,
                                       pdf_sha=_pdf_sha256(reader._path))
        except Exception as exc:  # noqa: BLE001
            warnings_list.append(f"полный OCR не выполнен ({exc!r})")
            return []
        if pages and any(page.lines for page in pages):
            warnings_list.append(
                "документ восстановлен ПОЛНЫМ OCR (скан/тотальная глифовая порча)")
            return pages
        return []

    @staticmethod
    def _recount_corruption(pages) -> dict:
        """Счётчики порчи на ВОССТАНОВЛЕННОМ полным OCR тексте (он чистый)."""
        glyph = sig = pseudo = control = 0
        for page in pages:
            for line in page.lines:
                glyph += glyph_suspect_count(line.text)
                s_here, p_here = pseudo_ascii_counts(line.text)
                sig += s_here
                pseudo += p_here
                control += control_char_count(line.text)
        return {"spacing": 0, "doubling": 0, "glyph": glyph, "sig": sig,
                "pseudo": pseudo, "control": control}
