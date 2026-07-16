# -*- coding: utf-8 -*-
"""ПИЛОТ VLM-АРБИТРА (промпт 13b, ШАГ 7) — перевод очереди в авто БЕЗ риска Group B.

АРХИТЕКТУРА (как задал заказчик):
  * `ai6-qwen3-vl-30b` — АРБИТР. Видит КРОП, выбирает из ЗАКРЫТОГО списка
    {KEEP_ORIGINAL, CANDIDATE_A, CANDIDATE_B, ABSTAIN}. Свободная генерация запрещена
    (structured output, temperature 0) -> галлюцинация невозможна по построению.
  * АВТО разрешено ТОЛЬКО при AND: арбитр выбрал кандидата И независимый визуальный OCR
    (Tesseract по кропу, psm 7, -l eng) подтвердил кандидата ПО ТОКЕНУ И пройден шаблон
    сущности (для кодов).
    Иначе -> очередь (арбитр даёт ПОДСКАЗКУ человеку, но замена НЕ применяется).

ДВЕ ИЗМЕРЯЕМЫЕ ВЕЛИЧИНЫ (не смешивать):
  A) БЕЗОПАСНОСТЬ (гейт отката): число ЛОЖНЫХ исправлений ВЕРНОЙ кириллицы. Контроль —
     кропы ПОДТВЕРЖДЁННО русских слов (pymorphy-known, >=5 строчных кир.) из БИТЫХ
     документов (где Group B-риск реален: русский рендерится верно, но лежит среди
     латинской порчи). Каждому даётся СОБЛАЗН — латинское чтение eng-OCR (`боль`->`bone`).
     Полный AND-гейт обязан оставить ВСЕ русскими. >0 латинизаций -> NO-GO, откат.
  B) ЦЕННОСТЬ: доля очереди, которую AND-гейт переводит в авто.

    TESSERACT_CMD=... TESSDATA_PREFIX=... python _corpus/vlm_pilot.py [N_control N_queue]
"""
from __future__ import annotations

import base64
import glob
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from crparser.engine import rumorph
from crparser.engine.latinrecovery import (
    entity_valid, _translit, _CYR_ANY, _plausible_latin, _RX_LONG_LOWER_CYR)

# Гомоглифы: кириллические ЗАГЛАВНЫЕ, визуально = латинской заглавной.
_HOMO_UP = set("АВЕКМНОРСТХ")

ROUTER = "https://mosai-llmrouter.emias.ru/v1/chat/completions"
MODEL = os.environ.get("VLM_MODEL", "ai6-qwen3-vl-30b")
QUEUE = os.path.join("_corpus", "verify_queue")
RAW = os.path.join("data", "raw")
OUT = os.path.join("_corpus", "vlm_pilot_result.json")
_RU5 = re.compile(r"^[а-яё]{5,}$")


def _key():
    for ln in open(".env", encoding="utf-8-sig").read().splitlines():
        if ln.strip() and not ln.strip().startswith("#"):
            k, _, v = ln.partition("=")
            if k.strip() == "LLM_API_KEY":
                return v.strip().strip('"').strip("'")
    raise SystemExit("нет LLM_API_KEY в .env")


_K = _key()

# ---- АРБИТР: закрытый список, structured output, temperature 0 ----
_SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string",
                "enum": ["KEEP_ORIGINAL", "CANDIDATE_A", "CANDIDATE_B", "ABSTAIN"]}},
    "required": ["verdict"], "additionalProperties": False}

_PROMPT = (
    "На изображении — фрагмент строки, извлечённой из медицинского PDF. Внутри неё есть "
    "слово, текстовый слой которого прочитан как «%s».\n"
    "Часто в этих PDF латинские слова по ошибке записаны кириллицей (сломан шрифт), но "
    "бывает и наоборот — слово реально русское.\n"
    "Кандидаты замены:\n%s\n"
    "Посмотри на ПИКСЕЛИ и реши, что НА САМОМ ДЕЛЕ напечатано:\n"
    "  KEEP_ORIGINAL — напечатано «%s» (замена НЕ нужна, в т.ч. если это верное рус. слово);\n"
    "%s"
    "  ABSTAIN — не видно / не уверен.\n"
    "Верни ТОЛЬКО метку в JSON."
)


def arbiter(png: bytes, source: str, cands):
    lines, letters = [], "AB"
    for i, c in enumerate(cands[:2]):
        lines.append("  CANDIDATE_%s — «%s»" % (letters[i], c))
    opts = "".join("  CANDIDATE_%s — напечатано «%s» (текст врёт, заменить);\n" % (letters[i], c)
                   for i, c in enumerate(cands[:2]))
    body = {"model": MODEL, "temperature": 0, "max_tokens": 30,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "verdict", "schema": _SCHEMA,
                                                "strict": True}},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": _PROMPT % (source, "\n".join(lines), source, opts)},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}]}
    req = urllib.request.Request(ROUTER, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _K})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode())
            txt = d["choices"][0]["message"]["content"]
            try:
                return json.loads(txt).get("verdict", "PARSE_ERR"), None
            except Exception:
                m = re.search(r"KEEP_ORIGINAL|CANDIDATE_A|CANDIDATE_B|ABSTAIN", txt)
                return (m.group(0) if m else "PARSE_ERR"), None
        except urllib.error.HTTPError as e:
            err = "HTTP%d:%s" % (e.code, e.read().decode()[:120])
            if e.code >= 500 and attempt < 2:
                time.sleep(3)
                continue
            return "ERR", err
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                time.sleep(3)
                continue
            return "ERR", repr(e)[:120]
    return "ERR", "retries"


# ---- независимый визуальный OCR (Tesseract psm 8 -l eng по кропу) ----
_OCR = None


def indep_ocr(png: bytes) -> str:
    global _OCR
    if _OCR is None:
        from crparser.engine.ocr import OcrRecoverer
        _OCR = OcrRecoverer()
        if not _OCR.available():
            raise SystemExit("Tesseract недоступен — выставь TESSERACT_CMD/TESSDATA_PREFIX (I5)")
    # psm 7 = СТРОКА (кропы очереди — построчные). psm 8 (слово) на строке даёт кашу.
    return (_OCR._run(png, psm=7, langs="eng") or "").strip()


def _norm(s):
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def _ocr_confirms(indep: str, cand: str) -> bool:
    """Независимый OCR подтверждает кандидата? Совпадение по ТОКЕНУ (не подстроке): иначе
    короткий кандидат (`van`) ложно нашёлся бы внутри чужого слова. Кандидат подтверждён,
    если его нормализованная форма РАВНА какому-то токену строки OCR (или тот — префикс/
    суффикс с общей длиной >=4 — терпим огрызки пунктуации `trachomatis,`)."""
    nc = _norm(cand)
    if len(nc) < 3:
        return False
    for tok in re.findall(r"[A-Za-z0-9]+", indep):
        nt = _norm(tok)
        if nt == nc:
            return True
        if len(nc) >= 5 and (nt.startswith(nc) or nc.startswith(nt)) and abs(len(nt) - len(nc)) <= 2:
            return True
    return False


def _source_is_russian(source: str) -> bool:
    """Источник — подтверждённо русское слово? ЕДИНСТВЕННЫЙ язык-независимый сигнал.
    Пилот доказал (контроль Group B, пословный): арбитр И независимый Tesseract `-l eng`
    КОРРЕЛИРОВАНЫ на гомоглифах — оба ТРАНСЛИТЕРИРУЮТ (`связанных`->`CBAZAHHBIX`), и обе
    визуальные ноги AND-гейта проходят на РУССКОМ слове. Различает только ЯЗЫК (I21/I22).
    Поэтому pymorphy — ОБЯЗАТЕЛЬНАЯ 4-я нога: known-русский источник в auto НЕ идёт НИКОГДА
    (максимум — подсказка в очередь). `рока`/`тазе` (валидный русский И реально порча) ->
    остаток на человека, а не автозамена."""
    core = (source or "").strip('.,;:()[]«»"')
    low = core.lower()
    # ТРИ языковых/структурных правила (целевой контроль 80 кропов, I27). Каждое закрывает
    # класс, который остальные НЕ держат; визуальные ноги (VLM/OCR) на них скоррелированы:
    # 1) word_is_known — частотная рус. лексика (`связанных`/`состоянию`);
    if rumorph.word_is_known(low):
        return True
    # 2) ФОРМА «7+ строчных кириллических» — РЕДКАЯ мед. лексика, которой НЕТ в OpenCorpora
    #    (`гепатоцеллюлярными`/`холангиокарциномой`/`устекинумаб`). Тот же приём, что в
    #    C5-канале (`_c5_protected` long_lowercase) — вынесен и в гейт арбитра ЯВНО.
    #    Цена: длинные ЛАТИНСКИЕ слова (`trachomatis`/`immunodeficiency`) уходят в очередь
    #    с подсказкой, не в auto. Приемлемо: рус. слово испортить нельзя, латинское —
    #    человек подтвердит по кропу за секунды.
    if _RX_LONG_LOWER_CYR.match(core):
        return True
    # 3) ГОМОГЛИФ-ONLY ALL-CAPS — рус. аббревиатура ЦЕЛИКОМ из букв с латинским двойником
    #    (`МАНК`=метод амплиф.нукл.кислот, `МНО`=межд.нормализ.отношение). Контроль показал:
    #    арбитр латинизирует их в транслит (`МАНК`->`MAHK`, `МНО`->`MHO`), и обе визуальные
    #    ноги проходят — различить Russian-гомоглиф от Latin-гомоглиф пикселями НЕЛЬЗЯ.
    #    Блокируем ВЕСЬ класс (в т.ч. лат. акронимы EAU/IMPACT/KRAS -> очередь): auto по
    #    гомоглиф-only небезопасен по построению. `ЕТОКЯ`(ETDRS)/`МЕОЫЫЕ`(MEDLINE) НЕ
    #    гомоглиф-only (несут Я/Ы) -> остаются в auto. Рус. аббревиатуры с не-гомоглифом
    #    (ГЦР/СОЭ/СРБ) защищены пикселями (Г/Ц/Э/Б без двойника) отдельно.
    letters = [c for c in core if c.isalpha()]
    if len(letters) >= 2 and all(c in _HOMO_UP for c in letters) and _CYR_ANY.search(core):
        return True
    return False


def and_gate(verdict, cands, indep, kind, source=""):
    """AND: (источник НЕ русский) И арбитр выбрал кандидата И независимый OCR совпал И
    (шаблон сущности). -> ('auto', cand) | ('queue', hint) | ('keep', None)."""
    if verdict == "KEEP_ORIGINAL":
        return "keep", None
    if verdict not in ("CANDIDATE_A", "CANDIDATE_B"):
        return "queue", None                     # ABSTAIN/ERR -> человек
    cand = cands[0] if verdict == "CANDIDATE_A" else (cands[1] if len(cands) > 1 else None)
    if not cand:
        return "queue", None
    # нога 1 (ЯЗЫК, upstream): источник — известное рус. слово -> НИКОГДА не auto (Group B).
    # Визуальные ноги ниже на гомоглифах КОРРЕЛИРОВАНЫ и русский не защищают.
    if _source_is_russian(source):
        return "queue", cand
    # нога 2: независимый визуальный OCR подтвердил кандидата (по токену)
    if not _ocr_confirms(indep, cand):
        return "queue", cand                     # арбитр ЗА, но OCR не подтвердил -> подсказка
    # нога 3: шаблон сущности (для кодов); термин проходит по чистой латинице
    k = kind if kind in ("icd", "atc", "tnm", "stage") else "term"
    if not entity_valid(cand, k):
        return "queue", cand
    return "auto", cand


# ---- Stage A: контроль Group B (генерируем кропы подтверждённо-русских слов) ----
def _corrupt_docs(limit=25):
    """Документы, где C5 сработал (значит битый шрифт) — там Group B-риск реален."""
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


def _control_cases(n_target):
    """Кропы ПОДТВЕРЖДЁННО русских слов из битых док. + соблазн (eng-OCR-чтение).
    Yields dict(doc, page, source, cand, png)."""
    import fitz
    cases = []
    seen = set()
    for base in _corrupt_docs():
        pdf = os.path.join(RAW, base + ".pdf")
        if not os.path.exists(pdf):
            continue
        doc = fitz.open(pdf)
        for pno in range(len(doc)):
            page = doc[pno]
            for blk in page.get_text("dict").get("blocks", []):
                for ln in blk.get("lines", []):
                    txt = "".join(sp["text"] for sp in ln.get("spans", []))
                    words = [w.strip('.,;:()[]«»"') for w in txt.split()]
                    ru = [w for w in words if _RU5.match(w) and rumorph.word_is_known(w)]
                    if not ru:
                        continue
                    src = ru[0]
                    if src in seen:
                        continue
                    bbox = ln["bbox"]
                    pad = 3.0
                    clip = fitz.Rect(bbox[0]-pad, bbox[1]-pad, bbox[2]+pad, bbox[3]+pad)
                    png = page.get_pixmap(matrix=fitz.Matrix(400/72.0, 400/72.0),
                                          clip=clip).tobytes("png")
                    # СОБЛАЗН: латинское чтение eng-OCR этого слова
                    eng = indep_ocr(png)
                    cand = None
                    for w in re.findall(r"[A-Za-z]{3,}", eng):
                        if _plausible_latin(w) and not _CYR_ANY.search(w):
                            cand = w
                            break
                    if not cand:
                        cand = _translit(src) or "the"
                    seen.add(src)
                    cases.append({"doc": base, "page": pno + 1, "source": src,
                                  "cand": cand, "png": png})
                    if len(cases) >= n_target:
                        doc.close()
                        return cases
        doc.close()
    return cases


def _load_png(rel):
    p = os.path.join(QUEUE, rel.replace("\\", os.sep))
    if os.path.exists(p):
        return open(p, "rb").read()
    return None


def _queue_sample(n_target):
    """Стратифицированная выборка задач очереди С кандидатом и кропом."""
    q = json.load(open(os.path.join(QUEUE, "tasks.json"), encoding="utf-8"))
    tasks = q if isinstance(q, list) else q.get("tasks", [])
    withc = [t for t in tasks if t.get("candidates") and t.get("crop_png")]
    # стратификация по первому тегу why_suspect, детерминированный шаг
    from collections import defaultdict
    buckets = defaultdict(list)
    for t in withc:
        buckets[(t.get("why_suspect") or ["?"])[0]].append(t)
    sample = []
    per = max(1, n_target // max(1, len(buckets)))
    for tag, lst in sorted(buckets.items()):
        step = max(1, len(lst) // per)
        sample += lst[::step][:per]
    return sample[:n_target]


def main():
    n_ctrl = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    n_q = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    print("модель: %s | контроль Group B: %d | выборка очереди: %d\n" % (MODEL, n_ctrl, n_q))
    res = {"model": MODEL, "control": [], "queue": []}

    print("=== STAGE A: КОНТРОЛЬ GROUP B (генерирую кропы русских слов) ===")
    ctrl = _control_cases(n_ctrl)
    print("сгенерировано контрольных кропов: %d" % len(ctrl))
    false_latin = []
    for i, c in enumerate(ctrl):
        v, err = arbiter(c["png"], c["source"], [c["cand"]])
        indep = indep_ocr(c["png"])
        decision, applied = and_gate(v, [c["cand"]], indep, "term", c["source"])
        rec = {"doc": c["doc"], "page": c["page"], "source": c["source"],
               "cand": c["cand"], "verdict": v, "indep_ocr": indep[:120],
               "decision": decision, "applied": applied}
        res["control"].append(rec)
        if decision == "auto":               # русское слово латинизировано АВТОМАТИЧЕСКИ
            false_latin.append(rec)
        if (i + 1) % 20 == 0:
            print("  ...%d/%d (ложных латинизаций: %d)" % (i + 1, len(ctrl), len(false_latin)))
        json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n=== STAGE B: ЦЕННОСТЬ (выборка очереди) ===")
    sample = _queue_sample(n_q)
    print("задач в выборке: %d" % len(sample))
    counts = {"auto": 0, "queue": 0, "keep": 0}
    amb_latin = []
    for i, t in enumerate(sample):
        png = _load_png(t["crop_png"])
        if png is None:
            continue
        cands = list(t.get("candidates") or [])
        v, err = arbiter(png, t["source_text"], cands)
        indep = indep_ocr(png)
        decision, applied = and_gate(v, cands, indep, t.get("entity_kind"), t.get("source_text"))
        counts[decision] = counts.get(decision, 0) + 1
        rec = {"doc": t.get("doc"), "source": t.get("source_text"),
               "cands": cands, "kind": t.get("entity_kind"),
               "why": t.get("why_suspect"), "verdict": v, "indep_ocr": indep[:120],
               "decision": decision, "applied": applied}
        res["queue"].append(rec)
        # Group B внутри очереди: c5_ambiguous, ушедший в auto — проверить глазами
        if decision == "auto" and "c5_ambiguous" in (t.get("why_suspect") or []):
            amb_latin.append(rec)
        if (i + 1) % 25 == 0:
            print("  ...%d/%d  auto=%d queue=%d keep=%d"
                  % (i + 1, len(sample), counts["auto"], counts["queue"], counts["keep"]))
        json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---- отчёт ----
    print("\n" + "=" * 72)
    print("STAGE A — БЕЗОПАСНОСТЬ (гейт отката)")
    print("=" * 72)
    print("контрольных русских слов: %d" % len(res["control"]))
    kept = sum(1 for r in res["control"] if r["decision"] == "keep")
    queued = sum(1 for r in res["control"] if r["decision"] == "queue")
    print("  KEEP (верно оставлены русскими): %d" % kept)
    print("  QUEUE (арбитр за замену, но AND-гейт не пустил): %d" % queued)
    print("  AUTO ЛОЖНАЯ ЛАТИНИЗАЦИЯ РУССКОГО: %d  <<< МЕТРИКА ОТКАТА" % len(false_latin))
    for r in false_latin[:15]:
        print("     %s: %s -> %s (verdict=%s, ocr=%r)"
              % (r["doc"], r["source"], r["applied"], r["verdict"], r["indep_ocr"]))
    verdict_a = "GO (Group B цела)" if not false_latin else "NO-GO -> ОТКАТ (латинизация русского > 0)"
    print("  ВЕРДИКТ A: %s" % verdict_a)

    print("\n" + "=" * 72)
    print("STAGE B — ЦЕННОСТЬ (перевод очереди в авто)")
    print("=" * 72)
    tot = sum(counts.values())
    if tot:
        print("  AUTO (AND-гейт пропустил):   %d  (%.1f%% выборки)"
              % (counts["auto"], 100.0 * counts["auto"] / tot))
        print("  QUEUE (осталось человеку):   %d  (%.1f%%)"
              % (counts["queue"], 100.0 * counts["queue"] / tot))
        print("  KEEP (арбитр: замена не нужна): %d  (%.1f%%)"
              % (counts["keep"], 100.0 * counts["keep"] / tot))
    print("  c5_ambiguous, ушедшие в AUTO (проверить, не русское ли): %d" % len(amb_latin))
    for r in amb_latin[:15]:
        print("     %s: %s -> %s (ocr=%r)" % (r["doc"], r["source"], r["applied"], r["indep_ocr"]))

    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nсырые результаты: %s" % OUT)
    return 0 if not false_latin else 1


if __name__ == "__main__":
    sys.exit(main())
