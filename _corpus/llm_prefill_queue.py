# -*- coding: utf-8 -*-
"""ROADMAP шаг 2 — ОБЪЁМНЫЙ LLM-проход по очереди на СЕРВЕРНЫХ моделях (внутренний роутер
emias, НЕ Опус-агенты). Пред-заполняет предсказания для v4 (кропы -> VLM-арбитр) и mixcase
(текст+контекст -> текстовый арбитр). В LS НЕ импортирует, в текст корпуса НЕ пишет —
предсказания живут в слое ревью (безопасно).

Метод (доказан пилотом, [[llm-router-access]]): АРБИТР выбирает из ЗАКРЫТОГО списка
{KEEP_ORIGINAL, CANDIDATE_A/B, ABSTAIN}, structured output, temperature 0 -> галлюцинация
невозможна. AND-гейт (не-русский + арбитр + независимый OCR + шаблон сущности) -> auto/queue/keep.
Свободная генерация запрещена: задачи БЕЗ кандидатов -> human (без ненадёжного free-read).

Резюмируемо: чекпойнт в JSONL, повторный запуск пропускает готовые task-ключи. Хост GPU
до ~22:00 — при HTTP500 конкретный таск помечается ERR и разберётся при повторном запуске.

    TESSERACT_CMD=.. TESSDATA_PREFIX=.. python _corpus/llm_prefill_queue.py [--limit N] [--mixcase-only|--v4-only]
"""
from __future__ import annotations
import json, os, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)   # vlm_pilot использует относительные пути (.env, crops)

# проверенные функции из пилота (арбитр + независимый OCR + языковой/гейтовый пайплайн)
from _corpus.vlm_pilot import (arbiter, indep_ocr, and_gate, MODEL, _load_png)  # noqa: E402

QDIR = os.path.join(_ROOT, "_corpus", "verify_queue")
V4 = os.path.join(QDIR, "tasks_min_v4.json")
MIX = os.path.join(QDIR, "_mixcase_queue.jsonl")
VERIFIED = os.path.join(QDIR, "_verified_corrections.json")
DEFERRED = os.path.join(QDIR, "_deferred_ambiguous.json")
OUT_V4 = os.path.join(QDIR, "_v4_predictions.jsonl")
OUT_MIX = os.path.join(QDIR, "_mixcase_predictions.jsonl")
TEXT_MODEL = os.environ.get("TEXT_MODEL", "ai2-gpt120b-oss")   # текстовый арбитр для mixcase

_LOCK = threading.Lock()
_CONF = {"auto": 0.9, "keep": 0.85, "queue": 0.5}


def _done_keys(path):
    keys = set()
    if os.path.exists(path):
        for ln in open(path, encoding="utf-8"):
            try:
                keys.add(json.loads(ln)["key"])
            except Exception:
                pass
    return keys


def _emit(path, rec):
    with _LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _map_decision(gate, cand, source, resolved):
    """('auto'/'keep'/'queue', cand) -> (decision, corrected, uncertain)."""
    if gate == "keep":
        return ("принять" if resolved == source else "ошибка"), source, False
    if gate == "auto" and cand:
        return ("принять" if cand == resolved else "ошибка"), cand, False
    # queue: неуверенно -> подсказка есть, но флаг uncertain (человек смотрит)
    return ("ошибка" if (cand and cand != source) else "принять"), (cand or resolved), True


# ---------- v4: VLM-арбитр по кропу ----------
def _pred_v4(t):
    src = t.get("source_text") or ""
    cands = t.get("candidates") or []
    resolved = t.get("resolved_text") or src
    kind = t.get("entity_kind")
    key = "v4:%s" % (t.get("task_id") or (t.get("doc"), src))
    if not cands:
        return {"key": key, "queue": "v4", "source_text": src, "doc": t.get("doc"),
                "verdict": "NO_CANDIDATE", "gate": "human", "decision": None,
                "corrected": None, "confidence": 0.0, "note": "нет кандидатов -> эксперт"}
    png = _load_png(t.get("crop_word") or t.get("crop_line") or t.get("crop_page"))
    if not png:
        return {"key": key, "queue": "v4", "source_text": src, "doc": t.get("doc"),
                "verdict": "NO_CROP", "gate": "human", "decision": None,
                "corrected": None, "confidence": 0.0}
    verdict, err = arbiter(png, src, cands)
    indep = ""
    if verdict in ("CANDIDATE_A", "CANDIDATE_B"):
        try:
            indep = indep_ocr(png)
        except Exception:
            indep = ""
    gate, cand = and_gate(verdict, cands, indep, kind, src)
    dec, corr, unc = _map_decision(gate, cand, src, resolved)
    return {"key": key, "queue": "v4", "source_text": src, "doc": t.get("doc"),
            "entity_kind": kind, "verdict": verdict, "gate": gate, "decision": dec,
            "corrected": corr, "uncertain": unc, "confidence": _CONF.get(gate, 0.4),
            "ocr_indep": indep[:60], "err": err}


# ---------- mixcase: текстовый арбитр по контексту ----------
_TXT_SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["KEEP_ORIGINAL", "CANDIDATE_A", "CANDIDATE_B", "ABSTAIN"]}},
    "required": ["verdict"], "additionalProperties": False}


def _text_arbiter(context, source, cands):
    import urllib.request, urllib.error, re
    from _corpus.vlm_pilot import ROUTER, _K
    letters = "AB"
    opts = "".join("  CANDIDATE_%s — «%s» (латиница, заменить);\n" % (letters[i], c)
                   for i, c in enumerate(cands[:2]))
    prompt = (
        "В медицинском тексте встречается токен «%s». Часто латинские слова записаны кириллицей "
        "(сломан шрифт), но бывает и реальное русское слово/аббревиатура.\n"
        "Контекст: …%s…\n"
        "Кандидаты замены:\n%s"
        "Реши по КОНТЕКСТУ, что это на самом деле:\n"
        "  KEEP_ORIGINAL — «%s» верно как есть (в т.ч. рус. аббревиатура);\n%s"
        "  ABSTAIN — не уверен.\nВерни ТОЛЬКО метку в JSON."
    ) % (source, (context or "")[:300], "\n".join("  CANDIDATE_%s — «%s»" % (letters[i], c)
         for i, c in enumerate(cands[:2])), source, opts)
    body = {"model": TEXT_MODEL, "temperature": 0, "max_tokens": 30,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "verdict", "schema": _TXT_SCHEMA, "strict": True}},
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(ROUTER, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + _K})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode())
            txt = d["choices"][0]["message"]["content"]
            try:
                return json.loads(txt).get("verdict", "PARSE_ERR"), None
            except Exception:
                m = re.search(r"KEEP_ORIGINAL|CANDIDATE_A|CANDIDATE_B|ABSTAIN", txt)
                return (m.group(0) if m else "PARSE_ERR"), None
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < 2:
                time.sleep(3); continue
            return "ERR", "HTTP%d" % e.code
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                time.sleep(3); continue
            return "ERR", repr(e)[:80]
    return "ERR", "retries"


def _pred_mix(t):
    src = t.get("source_text") or ""
    cands = t.get("candidates") or []
    resolved = t.get("resolved_text") or src
    kind = t.get("entity_kind")
    key = "mix:%s" % src
    if not cands:
        return {"key": key, "queue": "mixcase", "source_text": src, "verdict": "NO_CANDIDATE",
                "gate": "human", "decision": None, "corrected": None, "confidence": 0.0}
    verdict, err = _text_arbiter(t.get("context"), src, cands)
    # текстовый арбитр без независимого визуального OCR -> auto НЕ выдаём, только подсказка
    if verdict == "KEEP_ORIGINAL":
        gate, cand = "keep", None
    elif verdict in ("CANDIDATE_A", "CANDIDATE_B"):
        gate = "queue"; cand = cands[0] if verdict == "CANDIDATE_A" else (cands[1] if len(cands) > 1 else cands[0])
    else:
        gate, cand = "queue", None
    dec, corr, unc = _map_decision(gate, cand, src, resolved)
    return {"key": key, "queue": "mixcase", "source_text": src, "entity_kind": kind,
            "verdict": verdict, "gate": gate, "decision": dec, "corrected": corr,
            "uncertain": True, "confidence": min(_CONF.get(gate, 0.4), 0.7), "err": err}


def _run(tasks, fn, out_path, label, workers):
    done = _done_keys(out_path)
    t0 = time.time(); n = 0; gates = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        for t in tasks:
            src = t.get("source_text") or ""
            key = ("v4:%s" % (t.get("task_id") or (t.get("doc"), src))) if label == "v4" else ("mix:%s" % src)
            if key in done:
                continue
            futs[ex.submit(fn, t)] = key
        total = len(futs)
        for fut in as_completed(futs):
            rec = fut.result()
            _emit(out_path, rec)
            gates[rec.get("gate")] = gates.get(rec.get("gate"), 0) + 1
            n += 1
            if n % 100 == 0:
                print("  [%s] %d/%d (%.0fs) gates=%s" % (label, n, total, time.time() - t0, gates))
    print("[%s] готово: %d предсказано (%.0fs). gates=%s" % (label, n, time.time() - t0, gates))
    return gates


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    applied = set(json.load(open(VERIFIED, encoding="utf-8")))
    deferred = set(json.load(open(DEFERRED, encoding="utf-8"))) if os.path.exists(DEFERRED) else set()
    resolved = applied | deferred
    workers = int(os.environ.get("LLM_WORKERS", "10"))

    from _corpus.vlm_pilot import _K  # noqa
    print("роутер: VLM=%s TEXT=%s workers=%d" % (MODEL, TEXT_MODEL, workers))

    if "--mixcase-only" not in args:
        v4 = json.load(open(V4, encoding="utf-8"))
        v4 = [t for t in v4 if t.get("source_text") not in resolved]
        if limit:
            v4 = v4[:limit]
        print("v4 к предсказанию: %d (пропущено применённых/отложенных)" % len(v4))
        _run(v4, _pred_v4, OUT_V4, "v4", workers)

    if "--v4-only" not in args:
        mix = [json.loads(l) for l in open(MIX, encoding="utf-8") if l.strip()]
        mix = [t for t in mix if t.get("source_text") not in resolved]
        if limit:
            mix = mix[:limit]
        print("mixcase к предсказанию: %d" % len(mix))
        _run(mix, _pred_mix, OUT_MIX, "mixcase", workers)

    print("\nпредсказания: %s | %s (в LS НЕ импортировано)" %
          (os.path.relpath(OUT_V4, _ROOT), os.path.relpath(OUT_MIX, _ROOT)))


if __name__ == "__main__":
    raise SystemExit(main())
