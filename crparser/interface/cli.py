#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI-интерфейс.

    python run.py <pdf-или-папка> --profile cr --registry <excel> --out <папка>

Поддерживает один файл и обработку всей папки в цикле. Путь вывода — параметром.
Профиль выбирается по ключу (--profile). Реестр (--registry) нужен профилю КР.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from typing import List, Optional

from crparser.engine.parser import DocumentParser
from crparser.engine.jsonio import JsonWriter
from crparser.engine.models import ParseResult
from crparser.profiles import available_profiles, create_profile


def _iter_pdfs(path: str) -> List[str]:
    """Вернуть список PDF: либо один файл, либо все *.pdf из папки (не рекурсивно)."""
    if os.path.isfile(path):
        return [path] if path.lower().endswith(".pdf") else []
    if os.path.isdir(path):
        out = [os.path.join(path, n) for n in sorted(os.listdir(path))
               if n.lower().endswith(".pdf")]
        return out
    return []


def _print_summary(result: ParseResult, out_path: str) -> None:
    """Краткая сводка по файлу (для глаза человека)."""
    md = result.metadata
    st = result.stats
    mkb = ", ".join(md.get("mkb_codes") or []) or "—"
    print(f"✓ {md.get('source_file')}  [id={md.get('id')}]")
    print(f"  Название : {md.get('title')}")
    print(f"  Год / возраст : {md.get('year')} / {md.get('age_group')}")
    print(f"  МКБ : {mkb}")
    print(f"  Разделов : {st['sections_found']}   Таблиц : {st['tables_found']}")
    print(f"  Покрытие : {st['coverage_percent']}%   "
          f"(учтено {st['accounted_chars']}/{st['total_chars']} симв.)")
    if result.warnings:
        print(f"  Предупреждения ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"    - {w}")
    print(f"  -> {out_path}")


def _out_path_for(pdf_path: str, out_dir: Optional[str]) -> str:
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        return os.path.join(out_dir, stem + ".json")
    return os.path.splitext(pdf_path)[0] + ".json"


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="run.py",
        description="Парсинг клинических рекомендаций (PDF) в структурированный JSON.")
    ap.add_argument("input", help="PDF-файл или папка с PDF")
    ap.add_argument("--profile", default="cr", choices=available_profiles(),
                    help="профиль документа (по умолчанию: cr)")
    ap.add_argument("--registry", default=None,
                    help="путь к Excel-реестру КР (источник метаданных по ID)")
    ap.add_argument("--out", default=None,
                    help="папка для JSON (по умолчанию — рядом с PDF)")
    ap.add_argument("--quiet", action="store_true", help="не печатать сводку по файлам")
    ap.add_argument("--no-provenance", dest="provenance", action="store_false",
                    default=True,
                    help="компактный выход: без provenance-полей и блока provenance "
                         "(совпадает с прежней формой JSON)")
    return ap


def _force_utf8_console() -> None:
    """Windows-консоль по умолчанию cp1251 и падает на «✓»/кириллице — чиним."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_console()
    args = build_arg_parser().parse_args(argv)

    pdfs = _iter_pdfs(args.input)
    if not pdfs:
        print(f"Не найдено PDF по пути: {args.input}", file=sys.stderr)
        return 2

    try:
        profile = create_profile(args.profile, args.registry)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    parser = DocumentParser(profile)
    writer = JsonWriter(include_provenance=args.provenance)

    ok, failed = 0, 0
    for pdf_path in pdfs:
        out_path = _out_path_for(pdf_path, args.out)
        try:
            result = parser.parse(pdf_path)
            writer.write(result, out_path)
            ok += 1
            if not args.quiet:
                _print_summary(result, out_path)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"✗ ОШИБКА на {os.path.basename(pdf_path)}: {exc}", file=sys.stderr)
            if os.environ.get("CRPARSER_DEBUG"):
                traceback.print_exc()

    print(f"\nГотово: успешно {ok}, с ошибкой {failed}, всего {len(pdfs)}.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
