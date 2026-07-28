# -*- coding: utf-8 -*-
"""Демо латин-восстановления (13b) за флагом на ТРЁХ документах: A2-шаблон + eng-OCR
по кропу. До/после по конкретным местам + провенанс на каждую правку + замер
неразрешённого (критические vs прочие). НЕ прогоняет корпус."""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
import fitz  # noqa: E402
from crparser.engine.ocr import OcrRecoverer  # noqa: E402
from crparser.engine.latinnorm import normalize_code_token  # noqa: E402

_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
_PUN = ".,:;!?»«\"'()[]{}"
# критические сущности: коды/дозы/препараты/гены/стадии
_CRIT_RX = re.compile(
    r"(^[A-ZА-Я]\d{2}|^\d{2}[A-ZА-Я]|[TNMGТНМГ][0-4xXхХ]|мг|мл|мкг|ЕД|"
    r"PD|CTLA|VEGF|HBs?|HCV|HBV|HDV|IgG|IgM|УЕСР|СНшс|Вагсе|варикоз|"
    r"^[IVXДПШУ]{1,4}$|BCLC|UICC|TNM|ВСЬС|ШСС)", re.I)


def _seg_mixed(core):
    for seg in re.split(r"[-/.—–\s]", core):
        if len(seg) >= 2 and _CYR.search(seg) and _LAT.search(seg):
            return True
    return False


def suspicious(tok):
    core = tok.strip(_PUN)
    if len(core) < 2:
        return False
    if "§" in core or _seg_mixed(core):
        return True
    if re.match(r"^\d\d[A-ZА-Я]", core):        # 037, 801, 501
        return True
    # кириллический токен, стоящий как латинская сущность (Вагсе1опа, ШСС, СНшс)
    if _CYR.search(core) and re.search(r"\d", core) and not re.search(r"[а-я]{4,}", core):
        return True
    return False


def critical(tok):
    return bool(_CRIT_RX.search(tok.strip(_PUN)))


def lines_of(doc):
    for pno in range(len(doc)):
        for blk in doc[pno].get_text("dict").get("blocks", []):
            for ln in blk.get("lines", []):
                txt = "".join(sp["text"] for sp in ln.get("spans", []))
                if txt.strip():
                    yield pno, txt, tuple(ln["bbox"])


def recover(path, targets):
    doc = fitz.open(path)
    rec = OcrRecoverer()
    if not rec.available():                       # РАЗРЕШАЕТ self._cmd (иначе OCR = "")
        print("  OCR НЕДОСТУПЕН")
    rec._pdf_sha = "demo13b_" + path.split("/")[-1]
    fixes, prov = [], []
    resolved_crit = unresolved_crit = resolved_oth = unresolved_oth = 0
    shown = set()
    all_lines = list(lines_of(doc))
    # 1) целевые места (демо до/после)
    for label in targets:
        hit = next((l for l in all_lines if targets[label] in l[1].replace(" ", "")
                    or targets[label] in l[1]), None)
        if not hit:
            prov.append("   [%s] НЕ НАЙДЕНО в native" % label)
            continue
        pno, txt, bbox = hit
        eng = rec._clip_text(doc[pno], pno, bbox, psm=7, scale=2.0, langs="eng")
        # A2 по токенам
        a2parts = []
        for t in txt.split():
            r = normalize_code_token(t)
            a2parts.append(r[0] if r else t)
        prov.append("[%s] p%d" % (label, pno + 1))
        prov.append("   native : %r" % txt.strip()[:70])
        prov.append("   a2     : %r" % " ".join(a2parts)[:70])
        prov.append("   ocr_eng: %r" % eng[:70])
    # 2) замер остатка по этим 3 докам: все suspicious токены, resolved A2/OCR vs нет
    for pno, txt, bbox in all_lines:
        susp = [t for t in txt.split() if suspicious(t)]
        if not susp:
            continue
        eng = rec._clip_text(doc[pno], pno, bbox, psm=7, scale=2.0, langs="eng") \
            if any(critical(t) for t in susp) or susp else ""
        engset = set(re.findall(r"[A-Za-z][A-Za-z0-9/.-]+", eng))
        for t in susp:
            core = t.strip(_PUN)
            if core in shown:
                continue
            shown.add(core)
            a2 = normalize_code_token(t)
            latinized = "".join({"А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
                                 "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
                                 "Х": "X"}.get(c, c) for c in core)
            ok = bool(a2) or any(latinized in e or e in latinized for e in engset) \
                or (latinized in engset)
            if critical(t):
                resolved_crit += ok
                unresolved_crit += (not ok)
                if not ok:
                    fixes.append(("CRIT-unresolved", core[:22]))
            else:
                resolved_oth += ok
                unresolved_oth += (not ok)
    return prov, (resolved_crit, unresolved_crit, resolved_oth, unresolved_oth), fixes


DOCS = {
    "data/raw/КР1_4.pdf": {
        "Barcelona": "Вагсе1опа", "037.6": "037.6", "ШСС": "ШСС", "TNM Т1а": "Т1а",
    },
    "data/raw/КР628_2.pdf": {"H40.06": "Н40.0", "S01EC": "501ЕС", "II-IV": "П-1У"},
    "data/raw/КР876_1.pdf": {"C10AC": "C10AС", "N06AA": "N06AА"},   # контроль: коды
}

for path, targets in DOCS.items():
    print("\n" + "=" * 60)
    print(path)
    prov, counts, fixes = recover(path, targets)
    print("\n".join(prov))
    rc, uc, ro, uo = counts
    print("  -- остаток (native-слой): КРИТ resolved=%d unresolved=%d | прочие resolved=%d unresolved=%d"
          % (rc, uc, ro, uo))
    if fixes:
        print("  неразрешённые критические (первые):", [f[1] for f in fixes[:18]])
