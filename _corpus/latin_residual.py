# -*- coding: utf-8 -*-
"""ШАГ 0 промпта 13b: замер ОСТАТКА латинской/mixed-script порчи по всему корпусу
(после 13 — font-repair NO-GO, поэтому остаток = текущее состояние). Считаем ТОЛЬКО
обучаемые зоны (sections/tables/excluded.appendices/metadata) — их гейтит release.
Классифицируем критические сущности. Отчёт -> _corpus/latin_residual_baseline.md.
"""
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crparser.engine import ocr_pins  # noqa: E402

PINS = ocr_pins.load_base()
_PUNCT = ".,;:()[]«»\"'-—%<>±*/§"

_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
_SIGN = re.compile(r"§")
# критические сущности (даже в порченой форме): ICD, ATC, TNM, стадии, дозы, гены/белки
_RE_ICD = re.compile(r"^[A-ZА-Я]\d{2}", re.I)                    # C22, D37, Н40…
_RE_ATC = re.compile(r"^[A-ZА-Я0-9]\d{2}[A-ZА-Я0-9]{2}", re.I)   # S01EC, 801ЕС…
_RE_TNM = re.compile(r"^[TNMGtnmgТНМГ][0-9xXхХ]", re.I)          # T1a, N0, Mx, Т1а…
_RE_STAGE = re.compile(r"^(I{1,3}V?|IV|VI{0,3}|[1Ш][АВA-CВ]?)$")  # II, IIIA, IVB, ШВ…
_RE_DOSE = re.compile(r"\d+\s*(мг|мл|мкг|г|мг/м|мг/кг|ЕД|IU|mg|ml)", re.I)
_RE_GENE = re.compile(r"(PD-?L?\d|CTLA|VEGF|HBs?|HCV|HBV|HDV|IgG|IgM|BRAF|KRAS|EGFR)", re.I)
_CRIT = [_RE_ICD, _RE_ATC, _RE_TNM, _RE_GENE]


_SEG = re.compile(r"[-/.\s—–]")


def is_suspicious(tok):
    core = tok.strip(_PUNCT)
    if len(core) < 2:
        return None
    if _SIGN.search(tok):
        return "C1_sign"
    # ВНУТРИ-СЕГМЕНТНОЕ смешение скриптов = порча (кир-гомограф в лат-коде: L04АA).
    # Легитимные составные «T2-тип», «HBsAg-положительный» разбиваются сепаратором на
    # односкриптовые сегменты и НЕ флагаются (иначе 100% ложных на здоровых файлах).
    for seg in _SEG.split(core):
        if len(seg) < 2:
            continue
        if _CYR.search(seg) and _LAT.search(seg):
            return "C4_mixed"
    low = core.lower()
    if len(low) >= 3 and low in PINS:
        return "pin_hit"
    # код-шаблон ATC/ICD, начинающийся ЦИФРОЙ вместо буквы (801ЕС, 037.6) — буква
    # подменена похожей цифрой (S→8, O→0). Требуем код-подобную длину, не «5А».
    if re.match(r"^\d\d[A-ZА-Я0-9]{2,}$", core) and _LAT.search(core[2:] or "x"):
        return "C3_code_alpha"
    return None


def is_critical(tok):
    core = tok.strip(_PUNCT)
    # нормализуем кир-гомографы к латинице для проверки шаблона
    norm = core.translate(str.maketrans("АВСЕНКМОРТХаосехр", "ABCEHKMOPTXaocexp"))
    if _RE_STAGE.match(core):
        return True
    for rx in _CRIT:
        if rx.match(norm) or rx.search(norm):
            return True
    if _RE_DOSE.search(core):
        return True
    return False


def training_text(doc):
    parts = []

    def walk(secs):
        for s in secs:
            yield s
            yield from walk(s.get("children", []))
    for s in walk(doc.get("sections", [])):
        parts.append(s.get("title") or "")
        parts.append(s.get("text") or "")
    for t in doc.get("tables", []) or []:
        parts.append(t.get("raw_text") or "")
        parts.append(t.get("caption") or "")
    for item in (doc.get("excluded", {}) or {}).get("appendices", []):
        parts.append(item.get("title", ""))
        parts.append(item.get("text", ""))
    for v in (doc.get("metadata") or {}).values():
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts)


def main():
    import collections
    cls = collections.Counter()
    docs_susp = docs_crit = 0
    total_susp = total_crit = 0
    crit_examples = []
    per_doc_crit = []
    files = glob.glob(os.path.join("outout", "*.json"))
    for jf in files:
        doc = json.load(open(jf, encoding="utf-8"))
        text = training_text(doc)
        susp = crit = 0
        for tok in text.split():
            k = is_suspicious(tok)
            if not k:
                continue
            susp += 1
            cls[k] += 1
            if is_critical(tok):
                crit += 1
                if len(crit_examples) < 40:
                    crit_examples.append((os.path.basename(jf)[:-5], tok.strip(_PUNCT)[:20]))
        total_susp += susp
        total_crit += crit
        if susp:
            docs_susp += 1
        if crit:
            docs_crit += 1
            per_doc_crit.append((os.path.basename(jf)[:-5], crit))

    print("документов всего: %d" % len(files))
    print("документов с подозрительными span'ами (обучаемая зона): %d (%.0f%%)"
          % (docs_susp, 100 * docs_susp / len(files)))
    print("документов с КРИТИЧЕСКИМИ подозрительными (блокируют release): %d (%.0f%%)"
          % (docs_crit, 100 * docs_crit / len(files)))
    print("всего подозрительных токенов: %d (критических %d)" % (total_susp, total_crit))
    print("\nразбивка по классам:")
    for k, n in cls.most_common():
        print("  %-16s %d" % (k, n))
    per_doc_crit.sort(key=lambda x: -x[1])
    print("\nтоп-15 документов по критическим:")
    for b, n in per_doc_crit[:15]:
        print("  %-9s %d" % (b, n))
    print("\nпримеры критических токенов:")
    for b, t in crit_examples[:25]:
        print("  %-9s %r" % (b, t))


if __name__ == "__main__":
    main()
