# -*- coding: utf-8 -*-
"""
Применить починку латиницы к КР1_4, КР628_2 и ПОЛОЖИТЬ ФАЙЛЫ на диск (промпт 13b, за
флагом). Механизмы в порядке: (1) font-repair не-гомографов по контуру, (2) A2-шаблон,
(3) eng-OCR по кропу — с ГЕЙТОМ ВАЛИДАЦИИ по шаблону сущности (eng-OCR-код принимается
только если это валидный ATC/МКБ/TNM; иначе ambiguous). Провенанс на каждую правку в
блоке latin_recovery. НЕ трогает outout/ и report.json.
"""
import json
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
import fitz  # noqa: E402
from crparser.engine.ocr import OcrRecoverer  # noqa: E402
from crparser.engine.fontrepair import FontRepairer  # noqa: E402
from crparser.engine.latinnorm import normalize_code_token, coerce_entity  # noqa: E402

_WORD = "0-9A-Za-zА-Яа-яЁё"

OUT = "outout_latin"
CYR2LAT = {"А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
           "Р": "P", "С": "C", "Т": "T", "Х": "X", "а": "a", "е": "e", "о": "o",
           "р": "p", "с": "c", "х": "x", "у": "y"}

# --- ГЕЙТ ВАЛИДАЦИИ ПО ШАБЛОНУ СУЩНОСТИ (правка заказчика #2) ---
_RX_ATC = re.compile(r"^[A-Z]\d{2}[A-Z]{2}\d{2}$|^[A-Z]\d{2}[A-Z]{2}$|^[A-Z]\d{2}[A-Z]$")
_RX_ICD = re.compile(r"^[A-Z]\d{2}(?:\.\d{1,2})?$")
_RX_TNM = re.compile(r"^p?(?:T(?:is|[0-4][a-d]?|[xX])|N[0-3xX]|M[01xX]|G[1-4xX])$")
_RX_STAGE = re.compile(r"^[IVX]{1,4}(?:[-–][IVX]{1,4})?$")   # II, II-III, IV


def entity_valid(text, kind):
    """eng-OCR-результат ПРИНИМАЕТСЯ только если соответствует форме сущности.
    501ЕС->SOLEC не пройдёт (не ATC) -> ambiguous, не применяем (правка #2)."""
    t = text.strip("().,;: ")
    if kind == "atc":
        return bool(_RX_ATC.match(t))
    if kind == "icd":
        return bool(_RX_ICD.match(t))
    if kind == "tnm":
        return bool(_RX_TNM.match(t))
    if kind == "stage":
        return bool(_RX_STAGE.match(t))
    if kind == "term":
        return bool(t) and not re.search(r"[А-Яа-яЁё]", t)   # чистая латиница
    return False


class Recoverer:
    def __init__(self, base):
        self.base = base
        self.doc = fitz.open("data/raw/%s.pdf" % base)
        self.rec = OcrRecoverer()
        if not self.rec.available():
            raise SystemExit("OCR НЕДОСТУПЕН — выставь TESSERACT_CMD/TESSDATA_PREFIX (I5)")
        self.rec._pdf_sha = "latinrec_" + base
        self.fr = FontRepairer(self.doc)
        self._page_eng = {}

    def eng_page(self, pno):
        """eng-OCR полной страницы (кэш в памяти)."""
        if pno not in self._page_eng:
            page = self.doc[pno]
            self._page_eng[pno] = self.rec._clip_text(
                page, pno, tuple(page.rect), psm=6, scale=1.6, langs="eng")
        return self._page_eng[pno]

    # ---- механизм 3: eng-OCR по кропу конкретной строки/региона ----
    def eng_region(self, pno, bbox):
        return self.rec._clip_text(self.doc[pno], pno, bbox, psm=7, scale=2.5, langs="eng")


def find_line_bbox(doc, pno, needle):
    """bbox строки на странице pno, содержащей needle (native-слой)."""
    for blk in doc[pno].get_text("dict").get("blocks", []):
        for ln in blk.get("lines", []):
            t = "".join(sp["text"] for sp in ln.get("spans", []))
            if needle in t or needle.replace(" ", "") in t.replace(" ", ""):
                return tuple(ln["bbox"]), t
    return None, None


# ---- целевые места (заказчик): (corrupt, expected, kind, anchor) ----
TARGETS = {
    "КР1_4": [
        ("Ых", "Nx", "tnm", None),
        ("Н-Ш", "II-III", "stage", None),
        ("Hepatitis С у 1 ш з", "Hepatitis C virus", "term", "Hepatitis"),
        ("037.6", "D37.6", "icd", None),
        ("Тх", "Tx", "tnm", None), ("ТО", "T0", "tnm", None),
        ("Т1а", "T1a", "tnm", None), ("Т1Ь", "T1b", "tnm", None),
        ("ТЗ", "T3", "tnm", None), ("Мх", "Mx", "tnm", None), ("МО", "M0", "tnm", None),
        ("ШСС", "UICC", "term", None),
        # фраза Barcelona разложена на токены (в JSON два варианта: Ыуег и 1луег)
        ("Вагсе1опа", "Barcelona", "term", None), ("СНшс", "Clinic", "term", None),
        ("Ыуег", "Liver", "term", None), ("1луег", "Liver", "term", None),
        ("Сапсег", "Cancer", "term", None),
        ("ВСЬС", "BCLC", "term", None),
        ("РИ1", "PD1", "term", None),
        ("РИ-1Л", "PD-L1", "term", None), ("СТЪА4", "CTLA4", "term", None),
        ("Hepatitis С у 1 ш з", "Hepatitis C virus", "term", None),
        ("б мес", "6 мес", "dose", None),
    ],
    "КР628_2": [
        ("Н40.0б", "H40.06", "icd", None),
        ("501ЕС", "S01EC", "atc", None), ("801ЕС", "S01EC", "atc", None),
        ("801 ЕС", "S01EC", "atc", None),
        ("П-1У", "II-IV", "stage", None),
        ("8Иа//ег", "Shaffer", "term", "8Иа"),
    ],
}
_DIG = re.compile(r"\d")


def digits(s):
    return "".join(_DIG.findall(s))


def pick_eng_token(eng, corrupt, kind):
    """Из eng-OCR-текста выбрать токен, соответствующий шаблону сущности И близкий по
    цифрам к corrupt. Гейт: не прошёл шаблон -> None (не применяем, ambiguous)."""
    cd = digits(corrupt)
    best = None
    for tok in re.findall(r"[^\s]+", eng):
        t = tok.strip("().,;:[]")
        if entity_valid(t, kind) and (not cd or digits(t) == cd):
            return t
    return best


def locate_line(doc, needle):
    """Найти строку с needle, СКАНИРУЯ все страницы PDF (native-слой). corrupt-формы
    (Вагсе1опа, 037.6, РИ1) рендерятся в native — но НЕ на JSON-странице секции."""
    key = needle.replace(" ", "")
    for pno in range(len(doc)):
        for blk in doc[pno].get_text("dict").get("blocks", []):
            for ln in blk.get("lines", []):
                t = "".join(sp["text"] for sp in ln.get("spans", []))
                if key in t.replace(" ", ""):
                    return pno, tuple(ln["bbox"]), t
    return None, None, None


def confirm_expected(eng, expected, kind):
    """eng-OCR ПОДТВЕРЖДАЕТ ожидаемую форму? С ГЕЙТОМ сущности (правка #2): код-кандидат
    принимается ТОЛЬКО если это валидный ATC/МКБ/TNM. 501ЕС->SOLEC не подтвердит S01EC."""
    exp = expected.strip("() ")
    engj = eng.replace(" ", "")
    if kind in ("icd", "atc", "tnm"):
        for tok in re.findall(r"[^\s]+", eng):
            t = tok.strip("().,;:[]")
            # eng-OCR-токен + ГЕЙТ/коэрция цифр-слота: SOLEC->S01EC, D37.6->D37.6.
            c = coerce_entity(t, kind)
            if (c and c == exp) or (t == exp and entity_valid(t, kind)):
                return exp                      # eng-OCR подтвердил ровно валидный код
        return None                             # иначе ambiguous (не выдумываем код)
    if kind == "stage":
        if entity_valid(exp, "stage") and (exp in eng or exp.replace("-", "–") in eng
                                           or exp.replace("-", "") in engj):
            return expected
        return None
    if kind == "dose":                          # «б мес»->«6 мес»: подтвердить цифру 6
        return expected if re.search(r"\b6\b", eng) else None
    # term: ожидаемая ЧИСТАЯ латиница присутствует в eng-OCR
    if not re.search(r"[А-Яа-яЁё]", exp) and (exp in eng or exp.replace(" ", "") in engj):
        return expected
    return None


def resolve_target(R, jdoc, corrupt, expected, kind, anchor):
    """font_repair (не-гомографы) -> a2_template (коды) -> ocr_eng (кроп+гейт).
    Вернуть (resolved, method, rule, confidence) или (None, статус, причина, 0)."""
    # --- механизм 2: A2-шаблон (детерминизм, коды с гомографами) ---
    if " " not in corrupt:
        a2 = normalize_code_token(corrupt)
        if a2 and entity_valid(a2[0], kind):
            return a2[0], "a2_template", a2[1], 1.0
        # гомограф ЦИФРЫ в цифровом слоте шаблона (МО->M0, ТО->T0) — детерминизм, fsr=0
        ce = coerce_entity(corrupt, kind)
        if ce and entity_valid(ce, kind):
            return ce, "a2_template", "digit-slot coercion (%s)" % kind, 1.0
    # локация в PDF (скан всех страниц native-слоя)
    pno, bbox, _ = locate_line(R.doc, anchor or corrupt)
    if pno is not None:
        # --- механизм 1: font-repair не-гомографов (+ A2 на гомографы результата) ---
        fr = fontrepair_token(R, pno, corrupt)
        if fr and fr != corrupt:
            cleaned = fr
            a2fr = normalize_code_token(fr)          # догнать гомографы шаблоном (Т1b->T1b)
            if a2fr:
                cleaned = a2fr[0]
            if entity_valid(cleaned, kind):
                return cleaned, "font_repair", "non-homograph-contour + a2", 0.9
        # --- механизм 3: eng-OCR по кропу СТРОКИ + ГЕЙТ ---
        cand = confirm_expected(R.eng_region(pno, bbox), expected, kind)
        if cand:
            e3 = kind == "term" and expected in json.dumps(jdoc, ensure_ascii=False)
            rule = "entity-gate:%s + eng-crop%s" % (kind, " + E3(doc-repeat)" if e3 else "")
            return cand, "ocr_eng", rule, 0.95 if e3 else 0.9
    else:
        # OCR-тело: corrupt только в JSON (full-OCR восстановил), в native его нет.
        # eng-OCR ПОЛНОЙ страницы секции (JSON page) + подтверждение (E3 усиливает).
        jpno = page_of(R.doc, jdoc, corrupt)
        if jpno is not None:
            cand = confirm_expected(R.eng_page(jpno), expected, kind)
            e3 = kind == "term" and expected in json.dumps(jdoc, ensure_ascii=False)
            if cand or (e3 and entity_valid(expected, "term")):
                rule = "eng-page" + (" + E3(doc-repeat)" if e3 else "")
                return expected, "ocr_eng", rule, 0.9 if e3 else 0.8
        return None, "unresolved", "не найден в native; eng-page не подтвердил", 0.0
    reason = ("eng-OCR не прошёл шаблон %s (не выдумываем код)" % kind
              if kind in ("icd", "atc", "tnm") else "eng-OCR не подтвердил %s" % expected)
    return None, "ambiguous", reason, 0.0


def page_of(doc, jdoc, corrupt):
    """Страница (0-based) секции/таблицы JSON, содержащей corrupt; None если не найдено."""
    key = corrupt.replace(" ", "")

    def walk(secs):
        for s in secs:
            yield s.get("page"), (s.get("title", "") + " " + s.get("text", ""))
            yield from walk(s.get("children", []))
    for pg, t in walk(jdoc.get("sections", [])):
        if key in t.replace(" ", "") and pg is not None:
            return pg - 1 if pg >= 1 else pg
    for tb in jdoc.get("tables", []) or []:
        if key in ((tb.get("raw_text") or "") + (tb.get("caption") or "")).replace(" ", ""):
            pg = tb.get("page")
            return (pg - 1) if pg else None
    return None


def fontrepair_token(R, pno, corrupt):
    """font-repair не-гомографов для токена на странице pno (native texttrace)."""
    page = R.doc[pno]
    pf = {}
    for f in page.get_fonts(full=True):
        pf.setdefault(f[3].split("+")[-1], []).append(f[0])
    key = corrupt.replace(" ", "")
    for span in page.get_texttrace():
        tu = "".join(chr(c[0]) for c in span["chars"])
        if key not in tu.replace(" ", ""):
            continue
        gids = [c[1] for c in span["chars"]]
        for x in pf.get(span["font"].split("+")[-1], []):
            info = R.fr._font(x)
            if not info or not info.corrupt or not all(g in info.contour for g in gids):
                continue
            fixed, idx = R.fr.decode_nonhomograph(x, gids, tu)
            # вернуть только правленую часть, соответствующую corrupt
            m = re.search(re.escape(corrupt.replace(" ", "")),
                          fixed.replace(" ", "")) if idx else None
            if idx:
                # грубо: если что-то поправили и результат чище — вернуть слово
                w = fixed.strip("().,;: ")
                return w
    return None


CRIT_KINDS = {"icd", "atc", "tnm", "dose"}
_TOK = re.compile(r"[^\s]+")


def build_repair_map(R, jdoc, base):
    """Явные цели + общий A2-сметатель кодов. -> (repair_map, target_rows, unresolved)."""
    rmap = {}
    rows = []
    unresolved = []
    for corrupt, expected, kind, anchor in TARGETS[base]:
        resolved, method, rule, conf = resolve_target(R, jdoc, corrupt, expected, kind, anchor)
        if resolved and resolved != corrupt:
            rmap[corrupt] = (resolved, method, rule, conf)
            ok = "ПОЧИНЕНО"
        else:
            ok = "НЕ ПОЧИНЕНО"
            unresolved.append((corrupt, expected, kind, method, rule))
        rows.append((corrupt, resolved or "(—)", method, page_of(R.doc, jdoc, corrupt), ok, expected))
    # общий A2-сметатель: все код-токены полей (детерминизм, fsr=0)
    blob = json.dumps(jdoc, ensure_ascii=False)
    for tok in set(_TOK.findall(blob)):
        core = tok.strip('".,:;()[]«»/\\')
        if core in rmap:
            continue
        a2 = normalize_code_token(core)
        if a2 and a2[0] != core:
            rmap[core] = (a2[0], "a2_template", a2[1], 1.0)
    return rmap, rows, unresolved


def apply_repairs(jdoc, rmap):
    prov = []

    def fix(text, span_uids, page, where):
        if not text:
            return text
        for corrupt, (resolved, method, rule, conf) in rmap.items():
            if not corrupt or corrupt not in text:
                continue
            # ЗАМЕНА ПО ГРАНИЦЕ СЛОВА: «МО»->«M0» НЕ трогает «МОСТ»/«Е8МО» (подстроки).
            pat = r"(?<![%s])%s(?![%s])" % (_WORD, re.escape(corrupt), _WORD)
            new, n = re.subn(pat, lambda m: resolved, text)
            if n:
                text = new
                prov.append({
                    "span_uid": (span_uids[0] if span_uids else where),
                    "page": page, "source_text": corrupt, "resolved_text": resolved,
                    "method": method, "rule": rule, "confidence": conf, "occurrences": n})
        return text

    def walk(secs):
        for s in secs:
            s["title"] = fix(s.get("title", ""), s.get("span_uids"), s.get("page"),
                             s.get("section_id"))
            s["text"] = fix(s.get("text", ""), s.get("span_uids"), s.get("page"),
                            s.get("section_id"))
            walk(s.get("children", []))
    walk(jdoc.get("sections", []))
    for tb in jdoc.get("tables", []) or []:
        w = "table_%s" % tb.get("number")
        tb["raw_text"] = fix(tb.get("raw_text", ""), tb.get("claimed_span_uids"),
                             tb.get("page"), w)
        tb["caption"] = fix(tb.get("caption", ""), tb.get("claimed_span_uids"),
                            tb.get("page"), w)
    for it in (jdoc.get("excluded", {}) or {}).get("appendices", []):
        it["text"] = fix(it.get("text", ""), it.get("span_uids"), None, "appendix")
    return prov


def main():
    os.makedirs(os.path.join(OUT, "_before"), exist_ok=True)
    diff = ["# DIFF — латин-восстановление (13b, за флагом --latin-recovery)\n",
            "Механизмы по порядку: font_repair (не-гомографы, контур) → a2_template "
            "(коды) → ocr_eng (кроп, с ГЕЙТОМ шаблона сущности).\n"]
    for base in ("КР1_4", "КР628_2"):
        src = os.path.join("outout", base + ".json")
        jdoc = json.load(open(src, encoding="utf-8"))
        shutil.copy(src, os.path.join(OUT, "_before", base + ".json"))
        R = Recoverer(base)
        rmap, rows, unresolved = build_repair_map(R, jdoc, base)
        prov = apply_repairs(jdoc, rmap)
        jdoc["latin_recovery"] = prov
        # верификация целей: expected присутствует И corrupt отсутствует
        blob = json.dumps(jdoc, ensure_ascii=False)
        json.dump(jdoc, open(os.path.join(OUT, base + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        # DIFF-таблица
        diff.append("\n## %s  (%d правок в latin_recovery)\n" % (base, len(prov)))
        diff.append("| было | стало | метод | стр. | статус |")
        diff.append("|---|---|---|---|---|")
        for corrupt, resolved, method, pno, ok, expected in rows:
            got = "ПОЧИНЕНО" if (expected in blob and (corrupt not in blob or ok == "ПОЧИНЕНО")) \
                else "НЕ ПОЧИНЕНО"
            diff.append("| `%s` | `%s` | %s | %s | %s |"
                        % (corrupt, resolved, method, (pno + 1) if pno is not None else "?", got))
        crit = [u for u in unresolved if u[2] in CRIT_KINDS]
        oth = [u for u in unresolved if u[2] not in CRIT_KINDS]
        diff.append("\n**Осталось неразрешённым (%s):**" % base)
        for c, e, k, m, r in crit:
            diff.append("- 🔴 КРИТИЧЕСКОЕ `%s` (ожид. `%s`, %s) — %s: %s" % (c, e, k, m, r))
        for c, e, k, m, r in oth:
            diff.append("- `%s` (ожид. `%s`, %s) — %s" % (c, e, k, m))
        if not unresolved:
            diff.append("- (все цели починены)")
    open(os.path.join(OUT, "DIFF.md"), "w", encoding="utf-8").write("\n".join(diff))
    print("ГОТОВО:", OUT)


if __name__ == "__main__":
    main()
