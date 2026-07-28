# -*- coding: utf-8 -*-
"""Детерминированный гомоглиф-декодер (без моделей) — фолбэк для задач без LLM-предсказания
(spend-limit оборвал часть батчей) + intra-doc self-repair (B3a). НИЧЕГО в текст корпуса.

Для каждой uncovered-задачи: 1:1 замена кириллических гомоглифов на латиницу; если декод
отличается и (валиден как код / встречается чистым в ТОМ ЖЕ документе / совпал с кандидатом)
-> heuristic_suggestion (action=fix); если source уже латиница/валиден -> source_ok. Иначе
low-confidence. Пишет tasks_min_v3.json на месте (добавляет llm_suggestion со source=heuristic).
"""
import json, os, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
QDIR = os.path.join("_corpus", "verify_queue")
LATIN = "outout_latin"

# кир -> лат гомоглифы (верх/низ) + Ь->b (мягкий знак в коде)
CYR2LAT = {'А':'A','В':'B','Е':'E','К':'K','М':'M','Н':'H','О':'O','Р':'P','С':'C','Т':'T',
           'У':'Y','Х':'X','І':'I','Ѕ':'S','Ј':'J','а':'a','е':'e','о':'o','р':'p','с':'c',
           'у':'y','х':'x','к':'k','м':'m','т':'t','в':'b','н':'h','Ь':'b','ь':'b'}
_LAT = re.compile(r"[A-Za-z]")
_HAS_CYR = re.compile(r"[А-Яа-яЁё]")
# валидные формы кодов после декода
_ATC = re.compile(r"^[A-Z]\d{2}[A-Z]{2}\d{2}$")
_TNM = re.compile(r"^[TNMcp]{1,2}\d[a-c]?\d?$")
_ICD = re.compile(r"^[A-Z]\d{2}(\.\d+)?$")


def decode(s):
    return "".join(CYR2LAT.get(ch, ch) for ch in (s or ""))


def code_valid(s):
    return bool(_ATC.match(s) or _TNM.match(s) or _ICD.match(s))


def doc_text(doc, cache):
    if doc in cache: return cache[doc]
    txt = ""; p = os.path.join(LATIN, doc + ".json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8")); parts = []
            def w(ss):
                for x in ss:
                    if x.get("text"): parts.append(x["text"])
                    w(x.get("children", []))
            w(d.get("sections", []))
            for t in d.get("tables", []):
                if t.get("raw_text"): parts.append(t["raw_text"])
            txt = "\n".join(parts)
        except Exception: pass
    cache[doc] = txt; return txt


def main():
    tasks = json.load(open(os.path.join(QDIR, "tasks_min_v3.json"), encoding="utf-8"))
    cache = {}
    filled = 0; by_action = {"fix": 0, "source_ok": 0, "low": 0}
    for t in tasks:
        if t.get("llm_suggestion"):
            continue
        src = (t.get("source_text") or "").strip()
        if not src:
            continue
        dec = decode(src)
        cands = [c for c in (t.get("candidates") or []) if c]
        if not _HAS_CYR.search(src):
            sug = {"predicted": "SOURCE_OK", "action": "source_ok", "confidence": 0.5,
                   "reason": "уже латиница/без гомоглифов"}
        elif dec != src and (code_valid(dec) or dec in cands
                             or (dec in doc_text(t.get("doc", ""), cache) and _LAT.search(dec))):
            conf = 0.75 if (code_valid(dec) or dec in cands) else 0.65
            sug = {"predicted": dec, "action": "fix", "confidence": conf,
                   "reason": "гомоглиф-декод (детерм.)"
                             + (" +intra-doc" if dec in cache.get(t.get("doc", ""), "") else "")}
        else:
            sug = {"predicted": dec if dec != src else "SOURCE_OK",
                   "action": "fix" if dec != src else "source_ok", "confidence": 0.4,
                   "reason": "декод без подтверждения — на человека"}
        sug["model_version"] = "homoglyph-heuristic"
        t["llm_suggestion"] = sug
        filled += 1
        by_action["fix" if sug["action"] == "fix" and sug["confidence"] >= 0.6 else
                  ("source_ok" if sug["action"] == "source_ok" else "low")] += 1
    json.dump(tasks, open(os.path.join(QDIR, "tasks_min_v3.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("гомоглиф-фолбэк заполнил: %d задач %s" % (filled, by_action))
    covered = sum(1 for t in tasks if t.get("llm_suggestion"))
    print("итоговое покрытие пре-аннотациями: %d / %d" % (covered, len(tasks)))


if __name__ == "__main__":
    main()
