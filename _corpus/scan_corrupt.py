# -*- coding: utf-8 -*-
"""Скан корпуса на 3 типа порчи по outout/*.json (быстро, текст уже извлечён).
Строит подкорпус порчи и ищет удвоение (тип 2).

    python _corpus/scan_corrupt.py            # сводка по всем
    python _corpus/scan_corrupt.py КР115_2    # детально по файлу
"""
import os, re, sys, json, glob
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_LET = r"[А-Яа-яЁёA-Za-z]"
_SPACING = re.compile(r"(?:%s ){3,}%s" % (_LET, _LET))          # >=4 одиночных
_DOUBLED = re.compile(r"\b(?:([А-Яа-яЁё])\1){3,}[А-Яа-яЁё]{0,1}\b")
_MIXED = re.compile(r"[A-Za-z][А-Яа-яЁё]|[А-Яа-яЁё][A-Za-z]")
_BAD_BG = ("пб", "гб", "бг", "уо", "шуо", "апб", "агб", "пбаг", "оаг", "уог")


def texts(doc):
    def walk(secs):
        for s in secs:
            yield s.get("title", ""); yield s.get("text", "")
            yield from walk(s.get("children", []))
    yield from walk(doc.get("sections", []))
    for t in doc.get("tables", []):
        yield t.get("raw_text", ""); yield t.get("caption", "") or ""


def profile(doc):
    sp = db = mx = bg = 0
    for t in texts(doc):
        sp += len(_SPACING.findall(t))
        db += len(_DOUBLED.findall(t))
        mx += len(_MIXED.findall(t))
        for tok in t.split():
            core = tok.strip(".,;:()[]«»\"'-—%<>")
            if re.fullmatch(r"[А-Яа-яЁё]{4,}", core) and any(b in core.lower() for b in _BAD_BG):
                bg += 1
    return sp, db, mx, bg


def main():
    if len(sys.argv) > 1:
        for base in sys.argv[1:]:
            doc = json.load(open(os.path.join("outout", base + ".json"), encoding="utf-8"))
            print("=" * 70, "\n", base)
            for t in texts(doc):
                if _DOUBLED.search(t):
                    print("  [DBL] ", t[:90])
            print("  profile spacing/doubled/mixed/badbg:", profile(doc))
        return
    rows = []
    for jf in glob.glob(os.path.join("outout", "*.json")):
        base = os.path.splitext(os.path.basename(jf))[0]
        try:
            doc = json.load(open(jf, encoding="utf-8"))
        except Exception:
            continue
        sp, db, mx, bg = profile(doc)
        if sp + db + bg > 0:
            rows.append((base, sp, db, mx, bg))
    rows.sort(key=lambda r: -(r[1] + r[2] + r[4]))
    print("файлов с признаками порчи: %d" % len(rows))
    print("%-12s %6s %6s %6s %6s" % ("file", "space", "dbl", "mixed", "badbg"))
    for base, sp, db, mx, bg in rows[:45]:
        print("%-12s %6d %6d %6d %6d" % (base, sp, db, mx, bg))


if __name__ == "__main__":
    main()
