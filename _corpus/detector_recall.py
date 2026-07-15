# -*- coding: utf-8 -*-
"""ЗАМЕР RECALL ДЕТЕКТОРА латиницы (13b) — СЛЕПОЙ eng-OCR по всем спанам обучаемой зоны.

Проблема: очередь верификации содержит только то, что нашёл ДЕТЕКТОР. Если детектор слеп к
классу порчи, этот класс не попадёт ни в очередь, ни в отчёт — и останется в корпусе навсегда.
Замер отвечает: какую долю РЕАЛЬНЫХ латинских дефектов детектор видит.

МЕТОД (слепой, без детектора):
  1. Для 10 док. прогнать eng-OCR ПО КРОПУ КАЖДОЙ строки обучаемой зоны (не только подозрительных).
  2. Выровнять native-токены <-> eng-OCR-токены по позиции.
  3. Каждое расхождение (nat != ocr) классифицировать:
       found_fixed  — детектор нашёл и починил (nat в latin_recovery, decision=auto);
       found_queued — детектор нашёл, спан в очереди (decision=needs_review / unresolved);
       MISS         — детектор НЕ нашёл, а OCR видит РЕАЛЬНОЕ латинское слово  <- искомое;
       ocr_fp       — OCR ошибся (nat — верный рус. текст, OCR выдал транслит-мусор).
  4. recall = found / (found + MISS).

КЛЮЧЕВОЕ: «реальное латинское слово vs OCR-мусор» решается ОБЪЕКТИВНО и НЕЗАВИСИМО от детектора —
корпусным словарём латиницы, построенным по BASELINE `outout/` (ДО восстановления, не загрязнён
нашими правками): слово считается реальным, если оно ЧИСТО встречается в >=MIN_DOCS документах.
OCR-мусор («кровотечения»->«kpoeomeyenus») в извлечённом тексте не встречается НИКОГДА (там
кириллица) -> в словарь не попадает -> корректно классифицируется как ocr_fp.

    TESSERACT_CMD=... TESSDATA_PREFIX=... python _corpus/detector_recall.py
"""
from __future__ import annotations

import glob
import json
import os
import random
import re
import sys
import time
import warnings
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LATIN_OUT = "outout_latin"
BASE_OUT = "outout"
REPORT = os.path.join("_corpus", "detector_recall.md")
RAW = os.path.join("data", "raw")

MIN_DOCS_VOCAB = 3        # слово реально, если чисто встречается в >=3 документах baseline
MIN_LEN_VOCAB = 4         # короче — слишком шумно (of, in, mg)
ZONE_HIT_FRAC = 0.5       # доля токенов строки, попавших в обучаемую зону -> строка зоны

_RX_LAT_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9]{%d,}$" % (MIN_LEN_VOCAB - 1))
_RX_VOWEL = re.compile(r"[aeiouyAEIOUY]")
_CYR = re.compile(r"[А-Яа-яЁё]")
_WORD = re.compile(r"[^\s]+")

# ВИЗУАЛЬНАЯ транслитерация кириллицы в латиницу — то, что eng-OCR ВСЕГДА выдаёт, читая
# кириллические глифы латинским алфавитом («могут»->«MOryT», «кровотечения»->«kpoeomeyenus»).
# Если OCR-вывод ≈ этой транслитерации, значит OCR просто прочитал кириллицу как латиницу —
# это OCR-АРТЕФАКТ, а НЕ дефект текста. Если OCR-вывод ОТЛИЧАЕТСЯ от транслитерации, значит
# на странице нарисованы РЕАЛЬНО латинские глифы («ШСС» -> транслит «WCC», а OCR видит «UICC»)
# -> это настоящая порча. Ключевой дискриминатор замера, независимый от детектора.
_TRANSLIT = {
    "А": "A", "Б": "b", "В": "B", "Г": "r", "Д": "A", "Е": "E", "Ё": "E", "Ж": "x",
    "З": "3", "И": "u", "Й": "u", "К": "K", "Л": "n", "М": "M", "Н": "H", "О": "O",
    "П": "n", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Ф": "o", "Х": "X", "Ц": "u",
    "Ч": "4", "Ш": "w", "Щ": "w", "Ъ": "b", "Ы": "bl", "Ь": "b", "Э": "3", "Ю": "10",
    "Я": "R",
    "а": "a", "б": "6", "в": "b", "г": "r", "д": "a", "е": "e", "ё": "e", "ж": "x",
    "з": "3", "и": "u", "й": "u", "к": "k", "л": "n", "м": "m", "н": "h", "о": "o",
    "п": "n", "р": "p", "с": "c", "т": "t", "у": "y", "ф": "o", "х": "x", "ц": "u",
    "ч": "4", "ш": "w", "щ": "w", "ъ": "b", "ы": "bl", "ь": "b", "э": "3", "ю": "10",
    "я": "r",
}
TRANSLIT_SIM = 0.62       # OCR ≈ транслит -> артефакт чтения кириллицы (вкл. слепоту к A2)
ALIGN_MIN_SIM = 0.30      # OCR вообще не похож на чтение native -> пара срослась ошибочно


def _translit(s: str) -> str:
    return "".join(_TRANSLIT.get(c, c) for c in s).lower()

_VOCAB = set()
_DOCS = {}


# ---------------------------------------------------------------- vocab
_RX_RU_WORD = re.compile(r"^[А-Яа-яЁё][А-Яа-яЁё\-]{1,}$")


def build_vocab():
    """Два корпусных словаря по BASELINE outout/ (ДО восстановления, не загрязнён правками):
      * LAT — чистая латиница (реальное лат. слово, если чисто в >=MIN_DOCS док.);
      * RU  — чистая кириллица (реальное РУС. слово).
    RU-словарь — ГЛАВНЫЙ фильтр артефактов: если native-токен есть реальное рус. слово, то
    любая латиница, которую «увидел» eng-OCR, — это ЧТЕНИЕ КИРИЛЛИЦЫ ЛАТИНСКИМ АЛФАВИТОМ
    («АЛТ»->«AJIT», «печени»->«Child» при сбое выравнивания), а НЕ дефект текста. Симметричен
    LAT-словарю и так же независим от детектора."""
    df_lat, df_ru = Counter(), Counter()
    for p in glob.glob(os.path.join(BASE_OUT, "*.json")):
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        lat, ru = set(), set()
        for w in _WORD.findall(zone_text(doc)):
            core = w.strip('".,:;()[]«»/\\*!?')
            if _RX_LAT_WORD.match(core) and _RX_VOWEL.search(core):
                lat.add(core.lower())
            elif _RX_RU_WORD.match(core):
                ru.add(core.lower())
        for w in lat:
            df_lat[w] += 1
        for w in ru:
            df_ru[w] += 1
    return ({w for w, n in df_lat.items() if n >= MIN_DOCS_VOCAB},
            {w for w, n in df_ru.items() if n >= MIN_DOCS_VOCAB})


def zone_text(doc) -> str:
    """Обучаемая зона: sections + tables + metadata + excluded.appendices."""
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
    parts.append(json.dumps(doc.get("metadata", {}), ensure_ascii=False))
    return "\n".join(parts)


# ---------------------------------------------------------------- worker
_STATE = {}


_RU_VOCAB = set()


def _init(vocab, ru_vocab):
    global _VOCAB, _RU_VOCAB
    _VOCAB = vocab
    _RU_VOCAB = ru_vocab


def _doc_state(base):
    if base in _STATE:
        return _STATE[base]
    import fitz
    from crparser.engine.ocr import OcrRecoverer
    from crparser.engine.pdf_reader import _pdf_sha256
    doc = json.load(open(os.path.join(LATIN_OUT, base + ".json"), encoding="utf-8"))
    zt = zone_text(doc)
    zone_tokens = set()
    for w in _WORD.findall(zt):
        c = w.strip('".,:;()[]«»/\\*!?')
        if c:
            zone_tokens.add(c)
    corr = {}
    for c in (doc.get("latin_recovery", {}) or {}).get("corrections", []):
        corr[c.get("source_text")] = c.get("decision")
    for u in (doc.get("latin_recovery", {}) or {}).get("unresolved_critical", []):
        corr.setdefault(u.get("source_text"), "unresolved")
    pdf = fitz.open(os.path.join(RAW, base + ".pdf"))
    rec = OcrRecoverer()
    ok = rec.available()
    if ok:
        rec._doc_id = base
        try:
            rec._pdf_sha = _pdf_sha256(os.path.join(RAW, base + ".pdf"))
        except Exception:  # noqa: BLE001
            rec._pdf_sha = "recall_" + base
    _STATE[base] = (pdf, rec, ok, zone_tokens, corr, {w.lower() for w in zone_tokens})
    return _STATE[base]


def _strip(t):
    m = re.match(r"^(\W*)(.*?)(\W*)$", t, re.S)
    return m.group(2) if m and m.group(2) else ""


def _align(nat, eng):
    """Позиционное выравнивание (как в резолвере): равные длины -> по индексу."""
    out = {}
    if not nat or not eng:
        return out
    if len(nat) == len(eng):
        for i in range(len(nat)):
            out[i] = eng[i]
        return out
    j = 0
    for i in range(len(nat)):
        best, bj, bs = None, j, -1.0
        for jj in range(j, min(len(eng), j + 3)):
            s = _sim(nat[i].lower(), eng[jj].lower())
            if s > bs:
                bs, bj, best = s, jj, eng[jj]
        out[i] = best
        j = min(len(eng) - 1, bj + 1)
    return out


def _sim(a, b):
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    dp = [0] * (lb + 1)
    for i in range(1, la + 1):
        prev = 0
        for j in range(1, lb + 1):
            tmp = dp[j]
            dp[j] = prev + 1 if a[i - 1] == b[j - 1] else max(dp[j], dp[j - 1])
            prev = tmp
    return dp[lb] / max(la, lb)


def work(task):
    """OCR всех строк ОДНОЙ страницы, классификация расхождений."""
    base, pno = task
    try:
        pdf, rec, ok, zone_tokens, corr, zone_lower = _doc_state(base)
    except Exception as exc:  # noqa: BLE001
        return {"base": base, "err": repr(exc)}
    res = Counter()
    misses = []
    unaligned = []
    if not ok:
        return {"base": base, "err": "OCR unavailable"}
    page = pdf[pno]
    for blk in page.get_text("dict").get("blocks", []):
        for ln in blk.get("lines", []):
            native = "".join(s["text"] for s in ln.get("spans", []))
            if not native.strip():
                continue
            nat_words = [w for w in _WORD.findall(native)]
            nat_cores = [_strip(w) for w in nat_words]
            nat_cores = [c for c in nat_cores if c]
            if not nat_cores:
                continue
            # --- фильтр ОБУЧАЕМОЙ ЗОНЫ: строка считается зонной, если >=50% её токенов
            #     присутствуют в зоне ИЛИ были правлены детектором (тогда их в зоне уже нет)
            hit = sum(1 for c in nat_cores if c in zone_tokens or c in corr)
            if hit / len(nat_cores) < ZONE_HIT_FRAC:
                continue
            res["zone_lines"] += 1
            try:
                eng = rec._clip_text(page, pno, tuple(ln["bbox"]), psm=7, scale=2.5,
                                     langs="eng")
            except Exception:  # noqa: BLE001
                eng = ""
            if not eng:
                res["ocr_empty"] += 1
                continue
            eng_cores = [_strip(w) for w in _WORD.findall(eng)]
            eng_cores = [c for c in eng_cores if c]
            al = _align(nat_cores, eng_cores)
            for i, nat_c in enumerate(nat_cores):
                oc = al.get(i)
                if not oc:
                    continue
                if nat_c.lower() == oc.lower():
                    res["match"] += 1
                    continue
                res["diff"] += 1
                # (0) односимвольный native — доказательной силы не несёт (шум выравнивания)
                if len(nat_c) < 2:
                    res["skip_short"] += 1
                    continue
                tr_sim = _sim(oc.lower(), _translit(nat_c))
                # (1) САНИТИ ВЫРАВНИВАНИЯ: OCR-слово вообще НЕ похоже на чтение этого native-
                #     токена («т»->«Transcatheter», «С»->«Hepatitis») -> пара срослась по ошибке,
                #     это НЕ улика ни в одну сторону.
                if tr_sim < ALIGN_MIN_SIM:
                    res["misalign"] += 1
                    continue
                # (2) native — РЕАЛЬНОЕ РУССКОЕ СЛОВО (корпусный RU-словарь) -> текст верен,
                #     eng-OCR просто прочитал кириллицу латиницей («АЛТ»->«AJIT»).
                if nat_c.lower() in _RU_VOCAB:
                    res["ocr_fp"] += 1
                    res["ocr_fp_ruword"] += 1
                    continue
                # (3) OCR-вывод ≈ ВИЗУАЛЬНАЯ транслитерация native -> OCR прочитал кириллицу
                #     латиницей. Сюда же по построению попадает класс A2 (чистые гомографы:
                #     `Т1а`/`С18`) — слепой OCR его РАЗЛИЧИТЬ НЕ МОЖЕТ, поэтому он честно
                #     исключён из ЗНАМЕНАТЕЛЯ (см. «границы замера»), а не записан в пропуски.
                if tr_sim >= TRANSLIT_SIM:
                    res["ocr_fp"] += 1
                    res["ocr_fp_translit"] += 1
                    continue
                # (4) остаток: OCR видит нечто СВЯЗАННОЕ, но ОТЛИЧНОЕ от чтения кириллицы ->
                #     на странице реально латинские глифы. Нашёл ли это детектор?
                dec = corr.get(nat_c)
                if dec == "auto":
                    res["found_fixed"] += 1
                elif dec in ("needs_review", "unresolved", "ambiguous"):
                    res["found_queued"] += 1
                elif (oc.lower() in _VOCAB and _RX_LAT_WORD.match(oc)
                        and not _CYR.search(oc) and nat_c in zone_tokens):
                    res["miss"] += 1
                    if len(misses) < 400:
                        misses.append((nat_c, oc, pno + 1))
                else:
                    res["ocr_fp"] += 1
            # --- НЕвыровненное: реальные лат. слова, которых нет НИ в строке, НИ в зоне
            #     (ловит склейки/утрату пробелов: `Hepatitis С у 1 ш з` -> `virus`).
            #     Тот же анти-транслит-фильтр: слово, являющееся транслитерацией ЛЮБОГО
            #     native-токена строки, — артефакт чтения кириллицы, не утрата.
            nl = native.lower()
            for oc in eng_cores:
                if len(oc) < 5:
                    continue
                if not (oc.lower() in _VOCAB and oc.lower() not in nl
                        and oc.lower() not in zone_lower):
                    continue
                if any(_sim(oc.lower(), _translit(nc)) >= TRANSLIT_SIM for nc in nat_cores):
                    continue
                if all(nc.lower() in _RU_VOCAB or not _CYR.search(nc) for nc in nat_cores):
                    continue
                res["unaligned_missing"] += 1
                if len(unaligned) < 200:
                    unaligned.append((oc, native[:60], pno + 1))
    return {"base": base, "counts": dict(res), "misses": misses, "unaligned": unaligned}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t0 = time.time()
    bases_all = sorted(os.path.splitext(os.path.basename(p))[0]
                       for p in glob.glob(os.path.join(LATIN_OUT, "*.json")))
    fixed = ["КР1_4", "КР628_2", "КР1000_1", "КР876_1"]
    pool = [b for b in bases_all if b not in fixed]
    sel = fixed + random.Random(13).sample(pool, 6)
    print("Документы (10):", sel)
    print("Строю корпусный словарь латиницы по baseline outout/ ...")
    vocab, ru_vocab = build_vocab()
    print("  LAT-словарь: %d слов; RU-словарь: %d слов (>=%d док.)"
          % (len(vocab), len(ru_vocab), MIN_DOCS_VOCAB))

    import fitz
    tasks = []
    for b in sel:
        p = os.path.join(RAW, b + ".pdf")
        if not os.path.exists(p):
            continue
        d = fitz.open(p)
        tasks += [(b, i) for i in range(len(d))]
        d.close()
    print("Страниц к слепому OCR: %d" % len(tasks))

    agg = defaultdict(Counter)
    misses = defaultdict(list)
    unaligned = defaultdict(list)
    workers = int(os.environ.get("CR_RECALL_WORKERS", "12"))
    done = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                             initargs=(vocab, ru_vocab)) as ex:
        for r in ex.map(work, tasks):
            done += 1
            if done % 100 == 0:
                print("  ...%d/%d стр (%.0fs)" % (done, len(tasks), time.time() - t0))
            if r.get("err"):
                continue
            b = r["base"]
            agg[b].update(r["counts"])
            misses[b] += r["misses"]
            unaligned[b] += r["unaligned"]

    write_report(sel, agg, misses, unaligned, vocab, ru_vocab, time.time() - t0)


def write_report(sel, agg, misses, unaligned, vocab, ru_vocab, secs):
    tot = Counter()
    for b in sel:
        tot.update(agg[b])
    found = tot["found_fixed"] + tot["found_queued"]
    miss = tot["miss"]
    recall = (found / (found + miss)) if (found + miss) else 1.0
    all_miss = [m for b in sel for m in misses[b]]
    cls = classify_misses(all_miss)

    L = []
    L.append("# RECALL ДЕТЕКТОРА латиницы — слепой eng-OCR по всем спанам обучаемой зоны\n")
    L.append("**Зачем.** Очередь верификации содержит ТОЛЬКО то, что нашёл детектор. Если он "
             "слеп к классу порчи — класс не попадёт ни в очередь, ни в отчёт и останется в "
             "корпусе навсегда. Замер отвечает: какую долю РЕАЛЬНЫХ дефектов детектор видит.\n")
    L.append("## Метод")
    L.append("- 10 документов: 2 эталона (КР1_4, КР628_2), 2 из контрольной группы I1 "
             "(КР1000_1, КР876_1), 6 случайных (seed=13).")
    L.append("- eng-OCR **по кропу КАЖДОЙ строки** обучаемой зоны (sections/tables/metadata/"
             "appendices), БЕЗ участия детектора. Строка зоны: >=50% её токенов в зоне.")
    L.append("- native-токены выравниваются на eng-OCR-токены по позиции; каждое расхождение "
             "классифицируется.")
    L.append("- **«Реальное лат. слово» vs «OCR-мусор» решается НЕЗАВИСИМО от детектора**: "
             "корпусным словарём латиницы по **baseline `outout/`** (ДО восстановления, "
             "не загрязнён нашими правками) — слово реально, если ЧИСТО встречается в >=%d док. "
             "(len>=%d). LAT-словарь: **%d слов**. Транслит-мусор («кровотечения»->«kpoeomeyenus») "
             "в извлечённом тексте не встречается никогда -> в словарь не попадает.\n"
             % (MIN_DOCS_VOCAB, MIN_LEN_VOCAB, len(vocab)))
    L.append("## ИТОГ\n")
    L.append("| метрика | значение |")
    L.append("|---|---|")
    L.append("| строк зоны прогнано слепым OCR | **%d** |" % tot["zone_lines"])
    L.append("| токенов совпало (native==OCR) | %d |" % tot["match"])
    L.append("| расхождений всего | %d |" % tot["diff"])
    L.append("| детектор нашёл и починил (auto) | **%d** |" % tot["found_fixed"])
    L.append("| детектор нашёл, в очереди (needs_review/unresolved) | **%d** |" % tot["found_queued"])
    L.append("| **ПРОПУСК (детектор не нашёл, OCR видит реальную латиницу)** | **%d** |" % miss)
    L.append("| OCR ошибся (текст верен, транслит-мусор) | %d |" % tot["ocr_fp"])
    L.append("| несвязанная утрата лат. слов (склейки, `Hepatitis C virus`) | %d |"
             % tot["unaligned_missing"])
    L.append("")
    L.append("### RECALL ДЕТЕКТОРА = **%.1f%%**  (found %d / (found %d + miss %d))\n"
             % (recall * 100, found, found, miss))
    verdict = ("**>= 95% -> очередь РЕПРЕЗЕНТАТИВНА, валидируем.**" if recall >= 0.95
               else ("**< 80% -> СНАЧАЛА ЧИНИТЬ ДЕТЕКТОР по классам пропусков; валидация "
                     "преждевременна.**" if recall < 0.80
                     else "**80-95% -> серая зона: очередь неполна; см. классы пропусков.**"))
    L.append("### Вердикт по порогу: %s\n" % verdict)

    L.append("## По документам\n")
    L.append("| док | строк зоны | found_fixed | found_queued | MISS | ocr_fp | recall |")
    L.append("|---|---|---|---|---|---|---|")
    for b in sel:
        c = agg[b]
        f = c["found_fixed"] + c["found_queued"]
        m = c["miss"]
        r = (f / (f + m)) if (f + m) else 1.0
        L.append("| %s | %d | %d | %d | **%d** | %d | %.0f%% |"
                 % (b, c["zone_lines"], c["found_fixed"], c["found_queued"], m,
                    c["ocr_fp"], r * 100))
    L.append("")
    L.append("## ТОП-КЛАССЫ ПРОПУСКОВ\n")
    if not cls:
        L.append("_пропусков не найдено_")
    for name, items in cls:
        L.append("### %s — %d" % (name, len(items)))
        for nat, oc, pg in items[:12]:
            L.append("- `%s` -> OCR видит `%s`  (стр. %d)" % (nat, oc, pg))
        L.append("")
    ua = [u for b in sel for u in unaligned[b]]
    if ua:
        L.append("## Утрата лат. слов вне выравнивания (склейки/пробелы) — %d\n" % len(ua))
        for oc, ctx, pg in ua[:15]:
            L.append("- OCR видит `%s`, в тексте нет; строка: `%s` (стр. %d)"
                     % (oc, ctx.replace("`", "'"), pg))
        L.append("")
    L.append("## ГРАНИЦЫ ЗАМЕРА (что он НЕ измеряет — читать обязательно)\n")
    L.append("1. **Класс A2 (кир-гомографы) слепому OCR НЕ ВИДЕН ПО ПОСТРОЕНИЮ.** У токена, все "
             "символы которого имеют латинских двойников (`С18`, `Т1а`, `Вагсе1опа`), «OCR читает "
             "кириллицу латиницей» и «глифы реально латинские» дают ОДИН И ТОТ ЖЕ вывод — "
             "различить нельзя (I19: глиф РЕАЛЬНО кириллический, eng-OCR тут бессилен). Такие "
             "расхождения замер относит к OCR-артефактам. **Значит recall ниже считается для "
             "классов, которые OCR ВИДИТ (A1/C1/C5/C9 — реально латинские глифы), а не для A2.** "
             "A2 закрывает детерминированный шаблон (fsr=0, полнота по построению: находит ВСЕ "
             "код-токены валидной формы) — это не зона риска.")
    L.append("2. Замер не видит порчу, где OCR и текст согласованно неверны (символ утрачен ДО "
             "рендера), и не покрывает не-латинские дефекты (структура, таблицы).")
    L.append("3. Дискриминатор «артефакт vs дефект» — двойной: (а) OCR-вывод ≈ визуальная "
             "транслитерация native (порог %.2f) -> артефакт; (б) слово должно быть в корпусном "
             "словаре латиницы. Оба независимы от детектора.\n" % TRANSLIT_SIM)
    L.append("## Оценка остатка после валидации очереди\n")
    if found + miss:
        per_doc_miss = miss / max(1, len(sel))
        L.append("- В выборке 10 док.: детектор видит **%d**, пропускает **%d** дефектов "
                 "(recall %.1f%%)." % (found, miss, recall * 100))
        L.append("- После полной валидации очереди в корпусе останутся ИМЕННО пропуски: "
                 "~**%.0f** дефектов на документ -> экстраполяция на 701 док: "
                 "**~%.0f** невидимых дефектов." % (per_doc_miss, per_doc_miss * 701))
        L.append("- Это НИЖНЯЯ граница: слепой OCR сам не видит порчу, где OCR и текст "
                 "согласованно неверны (утрата символов до рендера), и не покрывает "
                 "не-латинские дефекты.")
    L.append("\n_замер: %.0f мин, %s_" % (secs / 60.0, time.strftime("%Y-%m-%d %H:%M")))
    open(REPORT, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n=== RECALL = %.1f%% (found %d / miss %d) ===" % (recall * 100, found, miss))
    print("отчёт:", REPORT)


def classify_misses(misses):
    """Разложить пропуски по классам порчи."""
    buckets = defaultdict(list)
    for nat, oc, pg in misses:
        if _CYR.search(nat) and not re.search(r"[A-Za-z]", nat):
            if nat[:1].isupper() and nat[1:].islower():
                buckets["C5 латинское слово ЦЕЛИКОМ кириллицей (Capitalized)"].append((nat, oc, pg))
            else:
                buckets["C5 латинское слово ЦЕЛИКОМ кириллицей"].append((nat, oc, pg))
        elif _CYR.search(nat) and re.search(r"[A-Za-z]", nat):
            buckets["C4 mixed-script внутри токена"].append((nat, oc, pg))
        elif re.search(r"\d", nat) and re.search(r"[A-Za-z]", nat):
            buckets["C3 цифро-буквенные confusables (I/1, O/0, S/5)"].append((nat, oc, pg))
        elif nat.isupper() or oc.isupper():
            buckets["C6/акронимы и регистр"].append((nat, oc, pg))
        else:
            buckets["C1/C9 прочее (битый маппинг / утрата символов)"].append((nat, oc, pg))
    return sorted(buckets.items(), key=lambda kv: -len(kv[1]))


if __name__ == "__main__":
    raise SystemExit(main() or 0)
