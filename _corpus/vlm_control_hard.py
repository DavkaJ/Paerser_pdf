# -*- coding: utf-8 -*-
"""ЦЕЛЕВОЙ КОНТРОЛЬ GROUP B — два класса, которые НОГА 4 (word_is_known) НЕ держит.

Замечание заказчика: «Group B = 0» на первом контроле мог значить не «гейт держит», а
«контроль не содержал класса, который гейт не держит». Все 3 прошлых провала — частотная
лексика (word_is_known=True), её ловит нога 4. Здесь — ДВА НЕПРОВЕРЕННЫХ класса:

  КЛАСС 1 — редкая медкириллица, word_is_known=False (`гепатоцеллюлярными`,
    `холангиокарциномой`, `устекинумаб`, `месалазин`...). Ногу 4 НЕ проходят -> держать
    может только ПРАВИЛО ФОРМЫ «7+ строчных кириллических».
  КЛАСС 2 — рус. аббревиатуры ЦЕЛИКОМ из гомоглифов (`СНВС`, `ВСС`). `ГЦР` защищён
    пикселями (Г/Ц без лат. двойника), а эти — нет; pymorphy их не знает; all-caps-блок снят.
    Чем держатся? — вопрос замера.

Один VLM-проход (verdict+indep_ocr сохраняются), затем оценка гейта под ВАРИАНТАМИ правил
БЕЗ повторных вызовов. Правило отката прежнее: латинизация верной кириллицы > 0 -> NO-GO.

    TESSERACT_CMD=... TESSDATA_PREFIX=... python _corpus/vlm_control_hard.py [N1 N2]
"""
from __future__ import annotations

import glob
import importlib.util as _ilu
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from crparser.engine import rumorph
from crparser.engine.latinrecovery import (
    _CYR_ANY, _plausible_latin, _translit, _RX_LONG_LOWER_CYR)

_spec = _ilu.spec_from_file_location(
    "vlm_pilot", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vlm_pilot.py"))
_vp = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_vp)
arbiter, indep_ocr, _ocr_confirms, entity_valid, RAW = (
    _vp.arbiter, _vp.indep_ocr, _vp._ocr_confirms, _vp.entity_valid, _vp.RAW)

OUT = os.path.join("_corpus", "vlm_control_hard_result.json")

# гомоглифы: кириллические заглавные, визуально = латинской заглавной
_HOMO_UP = set("АВЕКМНОРСТХ")
_RU_LOWER5 = re.compile(r"^[а-яё]{5,}$")
# КЛАСС 2 — гомоглиф-only all-caps кириллица (>=2 буквы, все буквы — гомоглифы)
def _is_homoglyph_allcaps(w: str) -> bool:
    letters = [c for c in w if c.isalpha()]
    return (len(letters) >= 2 and all(c in _HOMO_UP for c in letters)
            and _CYR_ANY.search(w) is not None)


def _corrupt_docs(limit=60):
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


def _collect(n1, n2):
    """Пословные кропы: class1 (редкая медкириллица) и class2 (гомоглиф-only all-caps)."""
    import fitz
    c1, c2, seen = [], [], set()
    for base in _corrupt_docs():
        pdf = os.path.join(RAW, base + ".pdf")
        if not os.path.exists(pdf) or (len(c1) >= n1 and len(c2) >= n2):
            continue
        doc = fitz.open(pdf)
        for pno in range(len(doc)):
            page = doc[pno]
            for w in page.get_text("words"):
                x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
                core = word.strip('.,;:()[]«»"')
                low = core.lower()
                is1 = (len(c1) < n1 and _RU_LOWER5.match(core)
                       and bool(_RX_LONG_LOWER_CYR.match(core))
                       and not rumorph.word_is_known(low))
                is2 = len(c2) < n2 and _is_homoglyph_allcaps(core)
                if not (is1 or is2) or core in seen:
                    continue
                pad = 2.0
                clip = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
                png = page.get_pixmap(matrix=fitz.Matrix(400 / 72.0, 400 / 72.0),
                                      clip=clip).tobytes("png")
                eng = indep_ocr(png)
                cand = None
                for t in re.findall(r"[A-Za-z]{3,}", eng):
                    if _plausible_latin(t) and not _CYR_ANY.search(t):
                        cand = t
                        break
                if not cand:
                    cand = _translit(core) or "the"
                seen.add(core)
                rec = {"doc": base, "page": pno + 1, "source": core, "cand": cand, "png": png}
                (c1 if is1 else c2).append(rec)
        doc.close()
    return c1, c2


# ---- варианты ноги 1 (языковой/формальной защиты источника) ----
def source_blocked(source, variant):
    core = (source or "").strip('.,;:()[]«»"')
    low = core.lower()
    if variant == "known":                     # текущий гейт: только word_is_known
        return rumorph.word_is_known(low)
    if variant == "known+form":                # + правило формы «7+ строчных»
        return rumorph.word_is_known(low) or bool(_RX_LONG_LOWER_CYR.match(core))
    if variant == "known+form+homo":           # + гомоглиф-only all-caps
        return (rumorph.word_is_known(low) or bool(_RX_LONG_LOWER_CYR.match(core))
                or _is_homoglyph_allcaps(core))
    return False


def gate(verdict, cand, indep, source, variant):
    if verdict == "KEEP_ORIGINAL":
        return "keep"
    if verdict not in ("CANDIDATE_A", "CANDIDATE_B") or not cand:
        return "queue"
    if source_blocked(source, variant):
        return "queue"
    if not _ocr_confirms(indep, cand):
        return "queue"
    if not entity_valid(cand, "term"):
        return "queue"
    return "auto"


def main():
    n1 = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    n2 = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    print("собираю кропы: класс1(редкая медкириллица)=%d, класс2(гомоглиф-only allcaps)=%d\n"
          % (n1, n2))
    c1, c2 = _collect(n1, n2)
    print("собрано: класс1=%d, класс2=%d\n" % (len(c1), len(c2)))
    res = []
    for cls, cases in (("class1_rare_med", c1), ("class2_homoglyph_allcaps", c2)):
        for i, c in enumerate(cases):
            v, err = arbiter(c["png"], c["source"], [c["cand"]])
            indep = indep_ocr(c["png"])
            res.append({"cls": cls, "doc": c["doc"], "source": c["source"],
                        "cand": c["cand"], "verdict": v, "indep_ocr": indep[:50]})
            if (i + 1) % 20 == 0:
                print("  %s ...%d/%d" % (cls, i + 1, len(cases)))
            json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n" + "=" * 70)
    print("ОЦЕНКА ГЕЙТА ПОД ВАРИАНТАМИ (латинизация верной кириллицы обязана быть 0)")
    print("=" * 70)
    for variant in ("known", "known+form", "known+form+homo"):
        print("\n--- нога источника = %s ---" % variant)
        for cls in ("class1_rare_med", "class2_homoglyph_allcaps"):
            rows = [r for r in res if r["cls"] == cls]
            autos = [r for r in rows if gate(r["verdict"], r["cand"], r["indep_ocr"],
                                             r["source"], variant) == "auto"]
            print("  %-26s n=%-3d АВТО-ЛАТИНИЗАЦИЙ: %d" % (cls, len(rows), len(autos)))
            for r in autos[:8]:
                print("       %s -> %s (verdict=%s, ocr=%r)"
                      % (r["source"], r["cand"], r["verdict"], r["indep_ocr"][:26]))
    print("\nсырьё: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
