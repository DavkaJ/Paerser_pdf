#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гейт и экспорт корпуса под ГЕНЕРАЦИЮ СИНТЕТИКИ.

Отдельная ось качества: валидатор отвечает на вопрос «структура сверена с
оглавлением источника» (PASS/REVIEW/FAIL), а здесь — на вопрос «этот текст можно
давать модели». Это разные вопросы, и смешивать их нельзя:

  * документ с потерянным подразделом 3.3.2 — FAIL у валидатора, но весь текст на
    месте (coverage 100%), для синтетики он пригоден;
  * документ КР153_2 — REVIEW только по OVERCOUNT, но в тексте 10 855 управляющих
    символов вместо букв: для синтетики НЕПРИГОДЕН.

Поэтому статус валидатора здесь — один из входов, а не вердикт. Инвариант 6
(«в candidate_release только PASS») не нарушается: этот экспорт пишет в ОТДЕЛЬНЫЙ
каталог candidate_synthetic/ и своим тиром помечает каждый документ.

Тиры:
  A  READY          — чистый: 7/7 канонических глав, нет неразрешённых критических
                      кодов, порча ниже шума;
  B  READY_FLAGGED  — пригоден, но с флагами качества (неполная каноника, overcount,
                      огрызки таблиц, needs_review-спаны, критические коды);
  C  QUARANTINE     — не отгружается (потеря текста, развал структуры, порча слоя).

Использование::

    python synthready.py --report _corpus/report_latin_v2.json --corpus outout_latin_v2
    python synthready.py ... --export candidate_synthetic
    python synthready.py ... --jsonl candidate_synthetic/corpus.jsonl
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Пороги. Каждый — с замером, на котором получен (INVARIANTS §8).              #
# --------------------------------------------------------------------------- #

# Каноническая полнота: сколько глав 1..7 распознано. Замер по корпусу 701:
# 7/7 у 656 док., 6/7 у 34, <5 всего у 8. Ниже 5 глав документ структурно неполон.
SYNTH_RECALL_MIN = 5
SYNTH_RECALL_CLEAN = 7

# Покрытие текста источника. Ниже 99% значит, что часть текста не попала никуда.
SYNTH_COVERAGE_MIN = 99.0

# Доля подозрительных токенов в ОБУЧАЕМОЙ зоне (sections+tables+appendices).
# Замер по корпусу 701: медиана 0.02%, 202 документа ровно 0, хвост >0.5% — 6 док.
# (у них порча реальная и массовая). Порог карантина 0.5%, флага — 0.15%.
SYNTH_GARBLE_QUARANTINE = 0.005
SYNTH_GARBLE_FLAG = 0.0015

# Управляющие символы вместо букв (битый cmap отдаёт код глифа). Порог общий с
# валидатором: 20 при замере «один поражённый документ на корпус, 10 855 штук».
SYNTH_CONTROL_MAX = 20

# Переучёт спанов (текст посчитан дважды: раздел + таблица). Не порча, но раздувает
# объём и даёт модели дубли — флаг, не карантин.
SYNTH_OVERCOUNT_FLAG = 1.02

# Таблица короче этого — почти наверняка вырезана одна шапка, тело потеряно.
# Порог общий с валидатором (TABLE_STUB_MAX_CHARS).
SYNTH_TABLE_STUB_CHARS = 200

# Минимальный объём текста, ниже которого документ бессмысленен как источник.
SYNTH_MIN_TOKENS = 500

CANONICAL_TITLES = {
    1: "краткая информация", 2: "диагностика", 3: "лечение",
    4: "медицинская реабилитация", 5: "профилактика",
    6: "организация медицинской помощи", 7: "дополнительная информация",
}

# --------------------------------------------------------------------------- #
# Детектор порчи в обучаемой зоне                                              #
# --------------------------------------------------------------------------- #
_TOK = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
# Цифра ВНУТРИ кириллического слова: ATC-код с кириллической буквой («М01А»
# вместо «M01A»), латинское слово с цифрами-гомоглифами («апа1уз1з»=analysis).
# Цифра на конце («Т2», «тип1») легитимна и НЕ ловится — нужна буква с обеих сторон.
_DIGIT_IN_CYR = re.compile(r"^[А-Яа-яЁё0-9]*[А-Яа-яЁё][0-9]{1,3}"
                           r"[А-Яа-яЁё][А-Яа-яЁё0-9]*$")


def suspicious_token(tok: str) -> bool:
    """Токен со смешением скриптов или с цифрой-гомоглифом внутри кириллицы.

    Намеренно НЕ ловит «латинское слово, целиком набранное кириллицей»
    (`ТЬегару`=Therapy): без словарного гейта на ДЕКОДИРОВАННОЙ форме такой
    детектор даёт >90% ложных (`типа`->tuna, `того`->toro, `ними`->humu) —
    замерено на корпусе. Эта порча живёт в основном в references, а их контракт
    режет."""
    has_cyr, has_lat = bool(_CYR.search(tok)), bool(_LAT.search(tok))
    if has_cyr and has_lat:
        return True
    return has_cyr and bool(_DIGIT_IN_CYR.match(tok))


def _control_chars(text: str) -> int:
    return sum(1 for ch in text
               if (ch < " " or "\x7f" <= ch <= "\x9f") and ch not in "\n\t\r")


# --------------------------------------------------------------------------- #
# Разбор документа                                                             #
# --------------------------------------------------------------------------- #

def walk_sections(sections: List[dict]):
    for s in sections or []:
        yield s
        yield from walk_sections(s.get("children") or [])


def trainable_texts(doc: dict) -> List[str]:
    """Тексты, которые реально уедут в обучение (то, что оставляет контракт)."""
    out: List[str] = []
    for s in walk_sections(doc.get("sections") or []):
        out.append(s.get("title") or "")
        out.append(s.get("text") or "")
    for t in doc.get("tables") or []:
        out.append(t.get("caption") or "")
        out.append(t.get("raw_text") or "")
    for item in (doc.get("excluded") or {}).get("appendices") or []:
        out.append(item.get("title") or "")
        out.append(item.get("text") or "")
    return out


def canonical_recall(doc: dict) -> int:
    tops = set()
    for s in doc.get("sections") or []:
        num = (s.get("number") or "").split(".")[0]
        if num.isdigit() and 1 <= int(num) <= 7:
            tops.add(int(num))
    return len(tops)


def measure(doc: dict) -> dict:
    stats = doc.get("stats") or {}
    cv2 = stats.get("coverage_v2") or {}
    corruption = stats.get("corruption") or {}
    latin = doc.get("latin_recovery") or {}
    corrections = latin.get("corrections") or []
    texts = trainable_texts(doc)

    tokens = suspicious = controls = 0
    for t in texts:
        controls += _control_chars(t)
        for tok in _TOK.findall(t):
            tokens += 1
            if suspicious_token(tok):
                suspicious += 1

    tables = doc.get("tables") or []
    return {
        "recall": canonical_recall(doc),
        "coverage": float(stats.get("coverage_percent") or 0.0),
        "lost_spans": int(cv2.get("lost_spans") or 0),
        "overcount": float(cv2.get("overcount_ratio") or 1.0),
        "structured": float(cv2.get("structured_coverage") or 0.0),
        "glyph_tokens": int(corruption.get("glyph_tokens") or 0),
        "pseudo_ascii": int(corruption.get("pseudo_ascii_tokens") or 0),
        "control_chars": max(controls, int(corruption.get("control_chars") or 0)),
        "tokens": tokens,
        "suspicious": suspicious,
        "garble_rate": suspicious / tokens if tokens else 0.0,
        "unresolved_critical": [c.get("source_text") if isinstance(c, dict) else str(c)
                                for c in (latin.get("unresolved_critical") or [])],
        "needs_review_spans": sum(1 for c in corrections
                                  if c.get("decision") == "needs_review"),
        "tables_total": len(tables),
        "tables_stub": sum(1 for t in tables
                           if len(t.get("raw_text") or "") < SYNTH_TABLE_STUB_CHARS),
        "sections": sum(1 for _ in walk_sections(doc.get("sections") or [])),
    }


def verdict(m: dict, status: Optional[str] = None) -> Tuple[str, List[str]]:
    """(tier, flags). Тир C — карантин, причина лежит первой во flags."""
    if m["pseudo_ascii"] or m["glyph_tokens"] >= 4:
        return "C", ["corrupt_layer"]
    if m["control_chars"] >= SYNTH_CONTROL_MAX:
        return "C", ["control_chars:%d" % m["control_chars"]]
    if m["tokens"] < SYNTH_MIN_TOKENS:
        return "C", ["too_short:%d" % m["tokens"]]
    if m["recall"] < SYNTH_RECALL_MIN:
        return "C", ["structure:recall=%d" % m["recall"]]
    if m["coverage"] < SYNTH_COVERAGE_MIN or m["lost_spans"] > 0:
        return "C", ["lost_text:cov=%.2f,lost=%d" % (m["coverage"], m["lost_spans"])]
    if m["garble_rate"] > SYNTH_GARBLE_QUARANTINE:
        return "C", ["garble:%.3f%%" % (m["garble_rate"] * 100)]

    flags: List[str] = []
    if m["recall"] < SYNTH_RECALL_CLEAN:
        flags.append("recall=%d" % m["recall"])
    if m["unresolved_critical"]:
        flags.append("unresolved_critical=%d" % len(m["unresolved_critical"]))
    if m["overcount"] > SYNTH_OVERCOUNT_FLAG:
        flags.append("overcount=%.3f" % m["overcount"])
    if m["garble_rate"] > SYNTH_GARBLE_FLAG:
        flags.append("garble=%.3f%%" % (m["garble_rate"] * 100))
    if m["tables_stub"]:
        flags.append("table_stubs=%d" % m["tables_stub"])
    if m["needs_review_spans"]:
        flags.append("needs_review_spans=%d" % m["needs_review_spans"])
    if status in ("FAIL", "REVIEW"):
        flags.append("validator=%s" % status)
    return ("A" if not flags else "B"), flags


# --------------------------------------------------------------------------- #
# Экспорт по контракту                                                         #
# --------------------------------------------------------------------------- #

def cut_section(s: dict) -> dict:
    return {
        "number": s.get("number"),
        "title": s.get("title"),
        "level": s.get("level"),
        "text": s.get("text"),
        "children": [cut_section(c) for c in (s.get("children") or [])],
    }


def cut_document(doc: dict, tier: str, flags: List[str], m: dict) -> dict:
    """Форма под обучение: без references/toc/front_matter/other, без provenance
    и stats; каждый документ несёт свой блок quality."""
    return {
        "metadata": doc.get("metadata"),
        "sections": [cut_section(s) for s in (doc.get("sections") or [])],
        "tables": [{"page": t.get("page"), "number": t.get("number"),
                    "caption": t.get("caption"), "raw_text": t.get("raw_text"),
                    "low_confidence": bool(t.get("low_confidence"))
                    or len(t.get("raw_text") or "") < SYNTH_TABLE_STUB_CHARS}
                   for t in (doc.get("tables") or [])],
        "appendices": [{"title": i.get("title"), "text": i.get("text")}
                       for i in ((doc.get("excluded") or {}).get("appendices") or [])],
        "quality": {
            "tier": tier, "flags": flags,
            "canonical_recall": m["recall"], "coverage_percent": m["coverage"],
            "garble_rate": round(m["garble_rate"], 6),
            "unresolved_critical": m["unresolved_critical"],
            "needs_review_spans": m["needs_review_spans"],
            "tables_total": m["tables_total"], "tables_stub": m["tables_stub"],
        },
    }


def flatten(doc_cut: dict) -> List[dict]:
    """Плоские текстовые юниты под генерацию: один раздел — одна запись с путём."""
    meta = doc_cut.get("metadata") or {}
    q = doc_cut.get("quality") or {}
    units: List[dict] = []

    def rec(sections, path):
        for s in sections:
            title = s.get("title") or ""
            here = path + [("%s %s" % (s.get("number") or "", title)).strip()]
            text = (s.get("text") or "").strip()
            if len(text) >= 200:
                units.append({
                    "doc_id": meta.get("id"),
                    "url": meta.get("url"),
                    "title": meta.get("title"),
                    "mkb_codes": meta.get("mkb_codes"),
                    "age_group": meta.get("age_group"),
                    "year": meta.get("year"),
                    "path": here,
                    "section_number": s.get("number"),
                    "section_title": title,
                    "level": s.get("level"),
                    "text": text,
                    "tier": q.get("tier"),
                    "flags": q.get("flags"),
                })
            rec(s.get("children") or [], here)

    rec(doc_cut.get("sections") or [], [])
    return units


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", default=os.environ.get("CR_OUTOUT", "outout_latin"))
    ap.add_argument("--report", default=os.environ.get("CR_REPORT",
                                                       "_corpus/report_latin.json"))
    ap.add_argument("--export", default=None, help="каталог выгрузки (тиры A+B)")
    ap.add_argument("--tiers", default="AB", help="какие тиры выгружать (A / AB)")
    ap.add_argument("--jsonl", default=None, help="плоский JSONL под генерацию")
    ap.add_argument("--run-id", default=None, help="id прогона для манифеста")
    args = ap.parse_args()

    statuses: Dict[str, str] = {}
    if os.path.exists(args.report):
        for row in json.load(open(args.report, encoding="utf-8")):
            statuses[row["file"]] = row["status"]

    files = sorted(f for f in os.listdir(args.corpus) if f.endswith(".json"))
    rows, tiers = [], collections.Counter()
    quarantine_reasons = collections.Counter()
    for name in files:
        base = os.path.splitext(name)[0]
        doc = json.load(open(os.path.join(args.corpus, name), encoding="utf-8"))
        m = measure(doc)
        tier, flags = verdict(m, statuses.get(base))
        tiers[tier] += 1
        if tier == "C":
            quarantine_reasons[flags[0].split(":")[0]] += 1
        rows.append((base, doc, m, tier, flags))

    print("корпус: %s (%d док.)  отчёт: %s" % (args.corpus, len(files), args.report))
    print("ТИРЫ: A(чистые)=%d  B(с флагами)=%d  C(карантин)=%d"
          % (tiers["A"], tiers["B"], tiers["C"]))
    print("карантин по причинам: %s" % dict(quarantine_reasons))
    flag_hist = collections.Counter()
    for _b, _d, _m, tier, flags in rows:
        if tier == "B":
            for f in flags:
                flag_hist[f.split("=")[0].split(":")[0]] += 1
    print("флаги тира B: %s" % dict(flag_hist.most_common()))

    want = set(args.tiers.upper())
    picked = [r for r in rows if r[3] in want]
    total_chars = sum(len(u["text"]) for _b, d, m, t, f in picked
                      for u in flatten(cut_document(d, t, f, m)))
    print("к выгрузке: %d док., %d символов обучаемого текста" % (len(picked), total_chars))

    if args.export:
        if os.path.isdir(args.export):
            shutil.rmtree(args.export)
        os.makedirs(args.export, exist_ok=True)
        manifest_docs = {}
        for base, doc, m, tier, flags in picked:
            cut = cut_document(doc, tier, flags, m)
            blob = json.dumps(cut, ensure_ascii=False, indent=2).encode("utf-8")
            with open(os.path.join(args.export, base + ".json"), "wb") as fh:
                fh.write(blob)
            manifest_docs[base] = {"tier": tier, "flags": flags,
                                   "sha256": hashlib.sha256(blob).hexdigest()}
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_from": {"corpus": args.corpus, "report": args.report,
                               "run_id": args.run_id},
            "purpose": "генерация синтетики",
            "tiers_exported": sorted(want),
            "counts": dict(tiers), "count_exported": len(picked),
            "excluded_zones": ["excluded.references", "excluded.toc",
                               "excluded.front_matter", "excluded.other",
                               "stats", "warnings", "provenance"],
            "attested": False,
            "disclaimer": "Кандидатный набор: пороги — triage, не gold set. "
                          "Документы тира B несут флаги качества в поле quality.",
            "documents": manifest_docs,
        }
        with open(os.path.join(args.export, "MANIFEST.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        print("выгружено -> %s" % args.export)

    if args.jsonl:
        os.makedirs(os.path.dirname(args.jsonl) or ".", exist_ok=True)
        n = 0
        with open(args.jsonl, "w", encoding="utf-8") as fh:
            for base, doc, m, tier, flags in picked:
                for unit in flatten(cut_document(doc, tier, flags, m)):
                    fh.write(json.dumps(unit, ensure_ascii=False) + "\n")
                    n += 1
        print("JSONL: %d юнитов -> %s" % (n, args.jsonl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
