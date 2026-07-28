# -*- coding: utf-8 -*-
"""СБОРКА LAT-СЛОВАРЯ КОРПУСА (`crparser/data/latin_vocab.json`) ИЗ ДОВЕРЕННОГО ИСТОЧНИКА.

ЗАЧЕМ. Словарь — precision-гейт (гейт 3) каналов eng-OCR: замена принимается, только если
результат — НАСТОЯЩЕЕ латинское слово. Прежний словарь (5005 слов) собирался по ВСЕМУ
baseline и оказался ЗАГРЯЗНЁН транслит-мусором: в нём лежали `nayuenmoe`(=пациентов),
`ypobehb`(=Уровень), `ajit`(=АЛТ), `mozo`(=того) — РОВНО те пины, на которых канал уже
портил корпус. Гейт, построенный по порченому корпусу, пропускает порчу: `пациентов`->
`nayuenmoe` проходил гейт 3, потому что `nayuenmoe` в словаре БЫЛ.

ОТКУДА МУСОР (ЗАМЕРЕНО, а не предположено).
Гипотеза «источник = документы с обратной глиф-порчей, помеченные
`stats.corruption.pseudo_ascii_tokens > 0`» ПРОВЕРЕНА и ОТВЕРГНУТА: этот флаг равен **0 у
ВСЕХ 722** документов — `parser.py` засчитывает pseudo-ascii токены только при ТОТАЛЬНОЙ
порче (плотностной порог), а здесь порча ЛОКАЛЬНАЯ. Фильтр по нему — no-op.
`stats.corruption.glyph_tokens` — тоже 0 у всех проверенных источников мусора.

РЕАЛЬНЫЙ ИСТОЧНИК — документы, чей ТЕКСТ изначально не заслуживает доверия. Замер по
документам-носителям мусора (`nayuenmoe`/`ypobehb`/`ajit`/`mozo`):
  * БОЛЬШИНСТВО имеют НОЛЬ анализируемых шрифтов -> это СКАНЫ, восстановленные полным OCR
    (I6). Их текст — вывод Tesseract, а не текстовый слой: OCR читает кириллицу латиницей
    и его же артефакты попадают в baseline (`пациентов` -> `nayuenmoe`);
  * остальные несут БИТЫЕ шрифты (КР518_3: 240/240 битых, КР68_2: 280/320, КР627_3: 87/229)
    -> /ToUnicode врёт, текст недостоверен по построению.
Контрольная группа I1 (born-digital, здоровые): 0 битых из 101-179 шрифтов.

ФИЛЬТР ИСТОЧНИКА (то же, что гейт 1, — проверенный механизм проекта, I17/I21: битые
шрифты 0.00-0.01 agreement, здоровые 0.97-1.0, промежутка нет):
  ДОВЕРЕННЫЙ документ = есть >=1 анализируемый шрифт И НИ ОДИН не битый.
  Иначе (скан без шрифтов / хоть один битый шрифт) -> в словарь НЕ идёт.
Это «чистый источник -> чистый словарь», а НЕ пост-фильтрация эвристиками: фильтровать
готовый словарь по виду слова НЕЛЬЗЯ — `portal`/`bone` неотличимы от «мусора» по форме
(`портал`/`боль` — тоже валидные русские), снесли бы нужное.

    python _corpus/build_latin_vocab.py [--dry-run]
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import warnings
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_OUT = "outout"
RAW = os.path.join("data", "raw")
OUT_JSON = os.path.join("crparser", "data", "latin_vocab.json")
# RU-словарь + список доверенных документов — ВЕРИФИКАЦИОННЫЙ артефакт (не продуктовый):
# на нём стоит НЕциркулярный тест Group B. В гейты его вносить ЗАПРЕЩЕНО (I22).
OUT_RU_JSON = os.path.join("_corpus", "corpus_ru_vocab.json")

MIN_DOCS = 3          # слово реально, если встречается в >=3 ДОВЕРЕННЫХ документах
MIN_LEN = 4           # короче — шумно (of, in, mg)
_WORD = re.compile(r"[^\s]+")
# ТОЛЬКО буквы: цифро-содержащие токены (`cba3h`, `S01EC`) в термин-словаре не нужны —
# коды валидируются шаблоном сущности (`entity_valid`), а не словарём.
_RX_LAT_WORD = re.compile(r"^[A-Za-z][A-Za-z\-]{%d,}$" % (MIN_LEN - 1))
_RX_VOWEL = re.compile(r"[aeiouyAEIOUY]")
_RX_RU_WORD = re.compile(r"^[а-яё][а-яё-]{2,}$")


def zone_text(doc) -> str:
    parts = []

    def walk(ss):
        for s in ss:
            parts.append((s.get("title") or "") + " " + (s.get("text") or ""))
            walk(s.get("children", []))
    walk(doc.get("sections", []))
    for t in doc.get("tables", []) or []:
        parts.append((t.get("caption") or "") + " " + (t.get("raw_text") or ""))
    for it in (doc.get("excluded", {}) or {}).get("appendices", []) or []:
        parts.append((it.get("title") or "") + " " + (it.get("text") or ""))
    return "\n".join(parts)


def font_trust(base: str):
    """(trusted, n_corrupt, n_total). Доверяем ТОЛЬКО born-digital с честными шрифтами."""
    import fitz
    from crparser.engine.fontrepair import FontRepairer
    p = os.path.join(RAW, base + ".pdf")
    if not os.path.exists(p):
        return False, 0, 0
    doc = fitz.open(p)
    fr = FontRepairer(doc)
    bad = tot = 0
    try:
        for pno in range(len(doc)):
            for f in doc[pno].get_fonts(full=True):
                try:
                    r = fr.font_report(f[0])
                except Exception:  # noqa: BLE001
                    continue
                if r is None:
                    continue
                tot += 1
                if r["corrupt"]:
                    bad += 1
    finally:
        doc.close()
    return (tot > 0 and bad == 0), bad, tot


def work(base: str):
    """-> (base, trusted, n_corrupt, n_total, {лат. слова}, {рус. слова})"""
    try:
        trusted, bad, tot = font_trust(base)
    except Exception:  # noqa: BLE001
        return base, False, -1, -1, set(), set()
    if not trusted:
        return base, False, bad, tot, set(), set()
    try:
        doc = json.load(open(os.path.join(BASE_OUT, base + ".json"), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return base, False, bad, tot, set(), set()
    lat, ru = set(), set()
    for w in _WORD.findall(zone_text(doc)):
        core = w.strip('".,:;()[]«»/\\*!?')
        if _RX_LAT_WORD.match(core) and _RX_VOWEL.search(core):
            lat.add(core.lower())
        elif _RX_RU_WORD.match(core.lower()):
            ru.add(core.lower())
    return base, True, bad, tot, lat, ru


def main():
    dry = "--dry-run" in sys.argv
    bases = sorted(os.path.splitext(os.path.basename(p))[0]
                   for p in glob.glob(os.path.join(BASE_OUT, "*.json")))
    print("документов baseline: %d — классифицирую источник по шрифтам…" % len(bases))
    df, df_ru = Counter(), Counter()
    trusted_docs, scans, corrupt_docs = [], [], []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for base, trusted, bad, tot, words, ru in ex.map(work, bases, chunksize=4):
            if trusted:
                trusted_docs.append(base)
                for w in words:
                    df[w] += 1
                for w in ru:
                    df_ru[w] += 1
            elif tot == 0:
                scans.append(base)
            else:
                corrupt_docs.append(base)

    vocab = sorted(w for w, n in df.items() if n >= MIN_DOCS)
    ru_vocab = sorted(w for w, n in df_ru.items() if n >= MIN_DOCS)
    print("\n=== ИСТОЧНИК ===")
    print("  ДОВЕРЕННЫХ (born-digital, все шрифты честные): %d" % len(trusted_docs))
    print("  отвергнуто — сканы/без анализируемых шрифтов:  %d" % len(scans))
    print("  отвергнуто — есть битые шрифты:                %d" % len(corrupt_docs))
    print("\n=== СЛОВАРЬ ===")
    print("  уникальных лат. слов в доверенных: %d" % len(df))
    print("  прошли порог >=%d док.:            %d" % (MIN_DOCS, len(vocab)))

    old = set()
    if os.path.exists(OUT_JSON):
        try:
            old = set(json.load(open(OUT_JSON, encoding="utf-8")).get("words", []))
        except Exception:  # noqa: BLE001
            pass
    if old:
        gone = old - set(vocab)
        added = set(vocab) - old
        print("\n=== ДЕЛЬТА со старым словарём (%d слов) ===" % len(old))
        print("  убрано: %d | добавлено: %d" % (len(gone), len(added)))
        probes = ["nayuenmoe", "ypobehb", "ajit", "mozo", "heit", "cba3h"]
        print("  ПИНЫ-МУСОР (обязаны УЙТИ):")
        for w in probes:
            st = ("УБРАН" if w in gone else ("ОСТАЛСЯ!!" if w in vocab
                  else "не было"))
            print("     %-11s %s" % (w, st))
        probes_ok = ["bone", "portal", "liver", "varices", "network", "assessment",
                     "oesophagus", "hypertension", "disease"]
        print("  ПИНЫ-НАСТОЯЩИЕ (обязаны ОСТАТЬСЯ):")
        for w in probes_ok:
            st = "цел" if w in vocab else ("ПОТЕРЯН!!" if w in old else "не было")
            print("     %-13s %s" % (w, st))

    print("\n=== RU-СЛОВАРЬ (верификация, НЕ гейт) ===")
    print("  русских слов из доверенных, >=%d док.: %d" % (MIN_DOCS, len(ru_vocab)))
    ru_set = set(ru_vocab)
    # Пины: порча латиницы, отрендеренная кириллицей, НЕ должна попасть в «словарь русского»
    # (иначе тест Group B объявит КОРРЕКТНУЮ починку `уегзиз`->versus нарушением).
    print("  ПИНЫ-ПОРЧА (в RU-словаре быть НЕ должны):")
    for w in ("уегзиз", "рпшагу", "йозе", "уап", "снп", "уегзюп"):
        print("     %-10s %s" % (w, "ЕСТЬ!!" if w in ru_set else "нет"))
    print("  ПИНЫ-РУССКИЕ (обязаны быть):")
    for w in ("боль", "пациентов", "печени", "рекомендуется", "гепатоцеллюлярный"):
        print("     %-18s %s" % (w, "есть" if w in ru_set else "НЕТ!!"))

    if dry:
        print("\n--dry-run: файлы не тронуты")
        return 0
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    payload = {
        "version": 2,
        "built_from": "baseline outout/, ТОЛЬКО born-digital с честными шрифтами",
        "min_docs": MIN_DOCS,
        "min_len": MIN_LEN,
        "source_docs_trusted": len(trusted_docs),
        "source_docs_rejected_scan": len(scans),
        "source_docs_rejected_corrupt_font": len(corrupt_docs),
        "words": vocab,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("\nзаписано: %s (%d слов)" % (OUT_JSON, len(vocab)))

    os.makedirs(os.path.dirname(OUT_RU_JSON), exist_ok=True)
    with open(OUT_RU_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "version": 1,
            "built_from": "baseline outout/, ТОЛЬКО born-digital с честными шрифтами",
            "purpose": ("НЕЗАВИСИМЫЙ дискриминатор Group B для тестов/верификации. "
                        "В ГЕЙТЫ НЕ ВНОСИТЬ (I22): на нём стоит единственный "
                        "НЕциркулярный тест канала C5."),
            "min_docs": MIN_DOCS,
            "trusted_docs": sorted(trusted_docs),
            "words": ru_vocab,
        }, f, ensure_ascii=False, indent=1)
    print("записано: %s (%d рус. слов, %d доверенных док.)"
          % (OUT_RU_JSON, len(ru_vocab), len(trusted_docs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
