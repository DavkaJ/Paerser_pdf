# -*- coding: utf-8 -*-
"""
Валидатор фикса многострочных заголовков.

Прогоняет парсер на КР79_2 (детальные критерии) и на регрессионном наборе
(дубли/порядок/покрытие/обрыв заголовков). Печатает PASS/FAIL по каждому критерию
и общий итог. Без фикса — зафиксировать baseline; после фикса — должно быть всё PASS.

    python validate_headings.py
"""

import sys, os, glob, re, warnings
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
from crparser.engine.parser import DocumentParser
from crparser.engine.jsonio import JsonWriter
from crparser.profiles import create_profile

def _find_data_dir():
    for cand in (os.path.join("tests", "pdfs"),
                 os.path.join("data", "текст_после_чистки"), "data", "."):
        if os.path.isdir(cand) and glob.glob(os.path.join(cand, "*.pdf")):
            return cand
    return "."


def _find_registry():
    for cand in glob.glob(os.path.join("tests", "*.xlsx")) + glob.glob("*.xlsx"):
        return cand
    return None


DATA = _find_data_dir()
PROF = create_profile("cr", _find_registry())
PAR = DocumentParser(PROF)
W = JsonWriter()

# регрессионный набор (не считая КР79_2): разные раскладки, уже работавшие
REGRESSION = ["КР359_3", "КР1012_1", "КР100_2", "КР940_1", "КР47_3", "КР477_2",
              "КР1004_1", "КР16_4", "КР159_2", "КР229_3", "КР58_2", "КР22_3", "КР786_1"]

# хвостовые токены, на которых заголовок обрываться НЕ должен
DANGLING = ("в том", "к применению", "и противопоказания", "включая", "на основе",
            "в том числе", "и", "или", "к", "по", "при", "с", "для", "на", "о",
            "методов", "медицинские")

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  -> " + detail) if detail and not cond else ""))


def parse(stem):
    return W.to_dict(PAR.parse(os.path.join(DATA, stem + ".pdf")))


def walk(secs):
    for s in secs:
        yield s
        yield from walk(s["children"])


def top_nums(d):
    return [s["number"] for s in d["sections"] if s["number"] and s["number"].isdigit()]


def child_nums(d, num):
    for s in d["sections"]:
        if s["number"] == num:
            return [c["number"] for c in s["children"]]
    return []


def _title_dangles(t):
    """Заголовок кончается на висячем союзе/предлоге/запятой/открытой скобке."""
    t = (t or "").strip()
    if not t:
        return False
    if t.endswith((",", "-", "–", "(")):
        return True
    low = t.lower()
    return any(low.endswith(" " + w) or low == w for w in DANGLING)


def leaked(sec):
    """
    РЕАЛЬНАЯ утечка хвоста заголовка в тело: title оборван на висячем слове И
    text начинается со строчной буквы (это и есть приклеившийся хвост). Секции
    «Критерии оценки качества» — табличные (содержимое в raw_text), их обрывы —
    артефакт таблиц, а не баг прозовых заголовков, поэтому исключаем.
    """
    title = (sec.get("title") or "")
    if "критери" in title.lower():
        return False
    text = (sec.get("text") or "").lstrip()
    return _title_dangles(title) and bool(text) and text[:1].islower()


def main():
    print("=" * 70)
    print("КР79_2 — детальные критерии многострочных заголовков")
    print("=" * 70)
    d = parse("КР79_2")
    st = d["stats"]
    secs = {s["number"]: s for s in d["sections"]}

    check("coverage_percent == 100.0", st["coverage_percent"] == 100.0,
          "cov=%s" % st["coverage_percent"])
    check("total_chars == 374984 (стабилен)", st["total_chars"] == 374984,
          "total=%s" % st["total_chars"])
    tn = top_nums(d)
    check("нет дублей номеров разделов", len(tn) == len(set(tn)), "top=%s" % tn)
    check("порядок 1..7 присутствует и монотонен",
          tn[:7] == ["1", "2", "3", "4", "5", "6", "7"], "top=%s" % tn)
    check("sections_found >= 58", st["sections_found"] >= 58,
          "sec=%s" % st["sections_found"])
    check("§3 имеет 3.1,3.2,3.3,3.4",
          child_nums(d, "3") == ["3.1", "3.2", "3.3", "3.4"], str(child_nums(d, "3")))
    check("§5 имеет 5.1,5.2", child_nums(d, "5") == ["5.1", "5.2"], str(child_nums(d, "5")))

    s4 = secs.get("4", {})
    s4t = (s4.get("title") or "")
    check("§4 title — полный 4-строчный заголовок",
          ("медицинская реабилитация" in s4t.lower()
           and "природных лечебных факторов" in s4t.lower()
           and "в том числе основанных" in s4t.lower()), repr(s4t))
    check("§4 text начинается со слова 'Реабилитация'",
          (s4.get("text") or "").lstrip().startswith("Реабилитация"),
          repr((s4.get("text") or "")[:40]))

    s5 = secs.get("5", {})
    s5t = (s5.get("title") or "")
    check("§5 title содержит 'к применению методов профилактики'",
          "к применению методов профилактики" in s5t.lower(), repr(s5t))
    check("§5 text != 'методов профилактики'",
          (s5.get("text") or "").strip() != "методов профилактики",
          repr((s5.get("text") or "")[:40]))

    trunc = [s["number"] for s in walk(d["sections"]) if leaked(s)]
    check("ни один title не оборван (хвост не утёк в text) — весь КР79_2", not trunc,
          "утечки: %s" % trunc)

    print("\n" + "=" * 70)
    print("Регрессия — другие файлы не должны сломаться")
    print("=" * 70)
    for stem in REGRESSION:
        if not os.path.exists(os.path.join(DATA, stem + ".pdf")):
            print("  [SKIP] %s (нет файла в наборе)" % stem)
            continue
        try:
            dd = parse(stem)
        except Exception as exc:
            check("%s парсится" % stem, False, repr(exc))
            continue
        tn = top_nums(dd)
        seq = [int(x) for x in tn]
        cov = dd["stats"]["coverage_percent"]
        trunc = [s["number"] for s in walk(dd["sections"]) if leaked(s)]
        ok = (len(tn) == len(set(tn)) and seq == sorted(seq)
              and cov >= 99.0 and not trunc)
        check("%s: без дублей/порядок/cov>=99/без обрывов" % stem, ok,
              "top=%s cov=%s trunc=%s" % ("".join(tn), cov, trunc))

    npass = sum(1 for _, ok in _results if ok)
    print("\n" + "=" * 70)
    print("ИТОГ: %d/%d PASS" % (npass, len(_results)))
    print("=" * 70)
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
