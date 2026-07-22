# -*- coding: utf-8 -*-
"""Собрать финальную очередь шага 2 в LS-формате и ДОБАВИТЬ в проект latin_verify (id=2)
БЕЗ пересоздания (сохраняя 170 аннотаций человека). Три канала — все add-only:

  1. v4 (2038 preds, gate auto/queue/keep) -> LS PREDICTIONS на СУЩЕСТВУЮЩИЕ таски (по task_id).
     Предсказания не трогают аннотации/таски (add-only слой подсказок).
  2. mixcase (588) -> НОВЫЕ таски (в LS их нет) + встроенное prediction. Импорт /import.
  3. deferred (22) -> flag-prediction на существующие таски (safety-deferred, per-occurrence).

Идемпотентно по model_version: повторный запуск НЕ дублирует (пропускает таски, где уже есть
prediction нашего model_version). Аннотации (170) не трогаются НИКОГДА.

    python _corpus/ls_import_queue.py --test     # 2 preds, проверить механику
    python _corpus/ls_import_queue.py --go        # полный прогон
"""
from __future__ import annotations
import json, os, sys, sqlite3, collections, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QDIR = os.path.join(_ROOT, "_corpus", "verify_queue")
DB = "file:///C:/Users/David/ls_latin_verify/label_studio.sqlite3?mode=ro&immutable=1"
API = "http://localhost:8080"
PROJ = 2
SCRATCH = os.environ.get("CR_SCRATCH", os.path.join(_ROOT, "_corpus"))
MV_V4 = "step2-vlm-qwen3vl"
MV_MIX = "step2-medgemma"
MV_DEF = "step2-safety-deferred"


def _tok():
    con = sqlite3.connect(DB, uri=True)
    t = con.execute("SELECT key FROM authtoken_token WHERE user_id=1").fetchone()[0]
    con.close()
    return t


TOK = _tok()


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "Token " + TOK,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        b = r.read().decode()
        return json.loads(b) if b.strip() else {}


def _ls_task_maps():
    con = sqlite3.connect(DB, uri=True)
    byid, bysrc, has_mv = {}, collections.defaultdict(list), collections.defaultdict(set)
    for tid, data in con.execute("SELECT id,data FROM task WHERE project_id=?", (PROJ,)):
        d = json.loads(data)
        if d.get("task_id"):
            byid[d["task_id"]] = tid
        if d.get("source_text") is not None:
            bysrc[d["source_text"]].append(tid)
    # какие таски уже имеют наш prediction (идемпотентность)
    for tid, mv in con.execute(
            "SELECT task_id, model_version FROM prediction WHERE project_id=?", (PROJ,)):
        has_mv[mv].add(tid)
    con.close()
    return byid, bysrc, has_mv


def _result(decision, corrected, source, kind):
    """LS-result из предсказания (формат existing opus48-context)."""
    res = []
    if decision in ("принять", "ошибка"):
        res.append({"from_name": "decision", "to_name": "crop", "type": "choices",
                    "value": {"choices": [decision]}})
    if decision == "ошибка" and corrected and corrected != source:
        res.append({"from_name": "corrected_text", "to_name": "crop", "type": "textarea",
                    "value": {"text": [corrected]}})
    if kind in ("icd", "atc", "tnm", "stage", "gene", "drug", "dose", "term"):
        res.append({"from_name": "entity_kind_fix", "to_name": "crop", "type": "choices",
                    "value": {"choices": [kind]}})
    return res


def _post_prediction(ls_task, decision, corrected, source, kind, score, mv):
    body = {"task": ls_task, "project": PROJ, "model_version": mv,
            "score": round(float(score), 3),
            "result": _result(decision, corrected, source, kind)}
    return api("/api/predictions/", "POST", body)


def build_v4(byid, has_mv):
    out = []
    for p in (json.loads(l) for l in open(os.path.join(QDIR, "_v4_predictions.jsonl"), encoding="utf-8")):
        if p.get("gate") not in ("auto", "queue", "keep"):
            continue
        tid = byid.get(p["key"].split("v4:")[-1])
        if tid is None or tid in has_mv.get(MV_V4, set()):
            continue
        out.append((tid, p.get("decision"), p.get("corrected"), p.get("source_text"),
                    p.get("entity_kind"), p.get("confidence"), MV_V4))
    return out


def build_deferred(bysrc, has_mv):
    deferred = json.load(open(os.path.join(QDIR, "_deferred_ambiguous.json"), encoding="utf-8"))
    forms = deferred if isinstance(deferred, list) else list(deferred)
    meta = deferred if isinstance(deferred, dict) else {}
    out = []
    for f in forms:
        for tid in bysrc.get(f, []):
            if tid in has_mv.get(MV_DEF, set()):
                continue
            corr = (meta.get(f) or {}).get("corrected") if isinstance(meta.get(f), dict) else None
            # deferred: НЕ проставляем decision (человек решает per-occurrence); только подсказка+kind
            out.append((tid, None, corr, f, None, 0.3, MV_DEF))
    return out


def build_mixcase_tasks(has_src):
    """mixcase таски НЕ в LS -> новые таски с встроенным prediction. Кропов нет (контекст)."""
    tasks = []
    preds = {p["source_text"]: p for p in
             (json.loads(l) for l in open(os.path.join(QDIR, "_mixcase_predictions.jsonl"), encoding="utf-8"))}
    for t in (json.loads(l) for l in open(os.path.join(QDIR, "_mixcase_queue.jsonl"), encoding="utf-8")):
        src = t.get("source_text")
        if src in has_src:                      # уже есть в LS -> не дублируем
            continue
        p = preds.get(src)
        data = {"doc": (t.get("docs") or ["mixcase"])[0], "page": "", "source_text": src,
                "resolved_text": t.get("resolved_text"), "candidates": ", ".join(t.get("candidates") or []),
                "entity_kind": t.get("entity_kind"), "is_critical": t.get("is_critical"),
                "why_uncertain": (t.get("context") or "")[:200], "method": t.get("method"),
                "confidence": t.get("confidence"), "crop_word": "", "crop_line": "", "crop_page": "",
                "task_id": "mix_" + src}
        item = {"data": data}
        if p and p.get("gate") in ("queue", "keep"):
            item["predictions"] = [{"model_version": MV_MIX, "score": round(float(p.get("confidence", 0.5)), 3),
                                    "result": _result(p.get("decision"), p.get("corrected"), src, t.get("entity_kind"))}]
        tasks.append(item)
    return tasks


def _run_posts(jobs, label, workers=8):
    ok = err = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_post_prediction, *j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                f.result(); ok += 1
            except Exception as e:  # noqa: BLE001
                err += 1
                if err <= 3:
                    print("   POST err:", repr(e)[:120])
            if i % 300 == 0:
                print("  [%s] %d/%d (%.0fs)" % (label, i, len(jobs), time.time() - t0))
    print("[%s] POST готово: ok=%d err=%d (%.0fs)" % (label, ok, err, time.time() - t0))
    return ok, err


def _counts():
    d = api("/api/projects/%d" % PROJ)
    return d.get("task_number"), d.get("total_annotations_number"), d.get("total_predictions_number")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    mode = "--go" if "--go" in sys.argv else ("--test" if "--test" in sys.argv else None)
    if not mode:
        print(__doc__); return 2
    byid, bysrc, has_mv = _ls_task_maps()
    has_src = set(bysrc)
    t0, a0, p0 = _counts()
    print("ДО: tasks=%s annotations=%s predictions=%s" % (t0, a0, p0))

    v4 = build_v4(byid, has_mv)
    deferred = build_deferred(bysrc, has_mv)
    mix = build_mixcase_tasks(has_src)
    print("к добавлению: v4-preds=%d, deferred-flags=%d, mixcase-НОВЫХ тасков=%d"
          % (len(v4), len(deferred), len(mix)))

    if mode == "--test":
        _run_posts(v4[:2], "v4-TEST")
        t1, a1, p1 = _counts()
        print("ПОСЛЕ теста: tasks=%s annotations=%s(было %s) predictions=%s(+%s)"
              % (t1, a1, a0, p1, p1 - p0))
        assert a1 == a0, "АННОТАЦИИ ИЗМЕНИЛИСЬ — СТОП"
        print("OK: аннотации целы, prediction добавляется. Для полного прогона: --go")
        return 0

    # --go: полный add-only
    _run_posts(v4, "v4")
    _run_posts(deferred, "deferred")
    if mix:
        print("импорт %d новых mixcase-тасков..." % len(mix))
        r = api("/api/projects/%d/import" % PROJ, "POST", mix)
        print("  import result:", json.dumps({k: r.get(k) for k in ("task_count", "annotation_count", "prediction_count", "duration")}, ensure_ascii=False))
    t1, a1, p1 = _counts()
    print("\nПОСЛЕ: tasks=%s(+%s) annotations=%s(было %s) predictions=%s(+%s)"
          % (t1, t1 - t0, a1, a0, p1, p1 - p0))
    if a1 != a0:
        print("!!! АННОТАЦИИ ИЗМЕНИЛИСЬ (%s->%s) — РАЗБИРАТЬСЯ" % (a0, a1))
        return 1
    print("аннотации целы (170).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
