# -*- coding: utf-8 -*-
"""КОНТРОЛЬ GROUP B (исправленный) — ПОСЛОВНЫЕ кропы.

Прошлый контроль (`vlm_pilot.py` Stage A) был НЕВАЛИДЕН: кроп — вся СТРОКА, а кандидат
брался как первый латинский токен eng-OCR строки. На строке `МАНК - метод амплификации…`
русскому слову `метод` доставался кандидат `MAHK` — мисридинг ДРУГОГО слова (аббревиатуры
МАНК). Арбитр видел `MAHK` в пикселях строки и выбирал его. Это дефект ВОПРОСА, не Group B.

Здесь: кроп — БBOX ОДНОГО СЛОВА (`page.get_text('words')`), кандидат — eng-OCR ЭТОГО же
слова (его собственный соблазн-мисридинг). Арбитр видит только целевое слово. Это валидный
тест: подтверждённо-русское слово + его латинский соблазн -> обязан KEEP_ORIGINAL.

Урок для ПРОДА: арбитру нужен ПОСЛОВНЫЙ фокус. Построчный кроп с несколькими
кандидато-подобными токенами двусмыслен («какое из слов ты оцениваешь?»).

    TESSERACT_CMD=... TESSDATA_PREFIX=... python _corpus/vlm_control_b.py [N]
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import importlib.util as _ilu

from crparser.engine import rumorph
from crparser.engine.latinrecovery import _CYR_ANY, _plausible_latin, _translit

# vlm_pilot.py лежит рядом, но _corpus не пакет (нет __init__) -> грузим по пути
_spec = _ilu.spec_from_file_location(
    "vlm_pilot", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vlm_pilot.py"))
_vp = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_vp)
arbiter, indep_ocr, and_gate, RAW = _vp.arbiter, _vp.indep_ocr, _vp.and_gate, _vp.RAW

OUT = os.path.join("_corpus", "vlm_control_b_result.json")
_RU5 = re.compile(r"^[а-яё]{5,}$")


def _corrupt_docs(limit=30):
    docs = []
    for p in glob.glob(os.path.join("outout_latin", "*.json")):
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if any("c5_corrupt_font" in (c.get("why_suspect") or [])
               for c in (doc.get("latin_recovery", {}) or {}).get("corrections", [])):
            docs.append(os.path.splitext(os.path.basename(p))[0])
        if len(docs) >= limit:
            break
    return docs


def _word_cases(n_target):
    """ПОСЛОВНЫЕ кропы подтверждённо-русских слов + их СОБСТВЕННЫЙ латинский соблазн."""
    import fitz
    cases, seen = [], set()
    for base in _corrupt_docs():
        pdf = os.path.join(RAW, base + ".pdf")
        if not os.path.exists(pdf):
            continue
        doc = fitz.open(pdf)
        for pno in range(len(doc)):
            page = doc[pno]
            for w in page.get_text("words"):
                x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
                core = word.strip('.,;:()[]«»"')
                if not _RU5.match(core) or not rumorph.word_is_known(core):
                    continue
                if core in seen:
                    continue
                pad = 2.0
                clip = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
                png = page.get_pixmap(matrix=fitz.Matrix(400 / 72.0, 400 / 72.0),
                                      clip=clip).tobytes("png")
                eng = indep_ocr(png)               # соблазн: eng-OCR ЭТОГО слова
                cand = None
                for t in re.findall(r"[A-Za-z]{3,}", eng):
                    if _plausible_latin(t) and not _CYR_ANY.search(t):
                        cand = t
                        break
                if not cand:
                    cand = _translit(core) or "the"
                seen.add(core)
                cases.append({"doc": base, "page": pno + 1, "source": core,
                              "cand": cand, "png": png})
                if len(cases) >= n_target:
                    doc.close()
                    return cases
        doc.close()
    return cases


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    print("КОНТРОЛЬ GROUP B (пословный), цель: %d\n" % n)
    cases = _word_cases(n)
    print("сгенерировано пословных кропов: %d\n" % len(cases))
    res, false_latin, arb_cand = [], [], []
    for i, c in enumerate(cases):
        v, err = arbiter(c["png"], c["source"], [c["cand"]])
        indep = indep_ocr(c["png"])
        decision, applied = and_gate(v, [c["cand"]], indep, "term", c["source"])
        rec = {"doc": c["doc"], "source": c["source"], "cand": c["cand"],
               "verdict": v, "indep_ocr": indep[:60], "decision": decision,
               "applied": applied}
        res.append(rec)
        if decision == "auto":
            false_latin.append(rec)
        if v in ("CANDIDATE_A", "CANDIDATE_B"):
            arb_cand.append(rec)
        if (i + 1) % 20 == 0:
            print("  ...%d/%d (ложных латинизаций: %d)" % (i + 1, len(cases), len(false_latin)))
        json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n" + "=" * 68)
    print("КОНТРОЛЬ GROUP B (пословный)")
    print("=" * 68)
    from collections import Counter
    print("слов: %d | решения: %s | вердикты: %s"
          % (len(res), dict(Counter(r["decision"] for r in res)),
             dict(Counter(r["verdict"] for r in res))))
    print("\nАВТО-ЛАТИНИЗАЦИЙ РУССКОГО (метрика отката): %d" % len(false_latin))
    for r in false_latin:
        print("   %s: %s -> %s (ocr=%r)" % (r["doc"], r["source"], r["applied"], r["indep_ocr"]))
    print("\nарбитр выбрал CANDIDATE на верном русском (AND-гейт отсёк или нет): %d" % len(arb_cand))
    for r in arb_cand[:15]:
        print("   %-16s -> %-10s verdict=%s ocr=%r decision=%s"
              % (r["source"], r["cand"], r["verdict"], r["indep_ocr"][:22], r["decision"]))
    print("\nВЕРДИКТ: %s" % ("GO (Group B цела)" if not false_latin
                             else "NO-GO -> ОТКАТ (латинизация русского > 0)"))
    print("сырьё: %s" % OUT)
    return 0 if not false_latin else 1


if __name__ == "__main__":
    sys.exit(main())
