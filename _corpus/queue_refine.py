# -*- coding: utf-8 -*-
"""Доработка очереди (Cowork-ревью 4, model-free): п.2 URL-фильтр (остаток) + п.7 реклассификация.

Причина, почему утренний morning_apply оставил ~230 URL-задач и мало патогенов:
  - URL-фильтр искал source_text через txt.find() в outout_latin — но source_text это ПОРЧЕННАЯ
    форма, а текст уже ЛАТИНИЗИРОВАН => 1409/2857 задач не находились вовсе (детектор слеп).
    Фикс: гомоглиф-фолд обеих строк + strip не-alnum + ВКЛЮЧИТЬ excluded (references/toc, где URL).
    Локатор — по фолду, но маркеры URL проверяются на ОРИГИНАЛЬНОМ окне (., /, : не переживают фолд).
    Точность: удаляем только задачи, где ВСЕ вхождения формы в URL-контексте (0 критических).
  - Реклассификация бежала по source_text (порчен) => пропускала патогены. Фикс: гонять детекторы
    по ДЕКОДИРОВАННОЙ форме (candidates + llm_suggestion.predicted + resolved_text). Короткие
    акронимы/виды (HIV, coli, ...) — со \\b, иначе 'Archives'~hiv, 'scoliosis'~coli (ложные).

Вход : verify_queue/tasks_min_v3.json (2857, живая очередь с llm_suggestion).
Выход: verify_queue/tasks_min_v4.json (URL убраны, priority проставлен, пересортировано; task_id СТАБИЛЕН),
       дозапись в verify_queue/_url_noise.jsonl (_filter=url_context_v2). Текст корпуса НЕ трогается.
    python _corpus/queue_refine.py
"""
from __future__ import annotations
import json, os, re, sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # -> X:\parser
QDIR = os.path.join("_corpus", "verify_queue")
LATIN = "outout_latin"

TW = {'А':'A','В':'B','С':'C','Е':'E','Н':'H','К':'K','М':'M','О':'O','Р':'P','Т':'T','Х':'X','У':'Y',
      'а':'a','е':'e','о':'o','с':'c','р':'p','х':'x','у':'y','к':'k','м':'m','н':'h','в':'b','т':'t',
      'Ь':'b','ь':'b','З':'3','з':'3','Ч':'4','Ш':'W','Ъ':'b','і':'i','І':'I','Ј':'J','ј':'j','Ё':'E',
      'Я':'R','Б':'B','Г':'r','Д':'D','Л':'L','П':'n','Ф':'F','Ц':'U','Щ':'W','Ю':'IO','Й':'N'}
def foldchar(c): return TW.get(c, c).lower()
def foldkey(s): return "".join(ch for c in str(s) for ch in foldchar(c) if ch.isalnum())

_URL = re.compile(
    r"(https?://|ЬЦ?[рt]?[рp]?[зs]?://|www\.|\bdoi\b|10\.\d{4,}/|\.ru/|\.ги/|\.com\b|\.org\b|"
    r"\.pdf|rosoncoweb|elibrary|pubmed|medline|/standards/|/upload/|/view-cr/|minzdrav|\.gov|"
    r"cyberleninka|scholar|researchgate|\.html?\b|nih\.gov|ncbi)", re.I)

# п.7 реклассификация — приоритет (средний тир). Короткие токены со \b.
_PATHO = re.compile(
    r"\b(ЦМВ|ВПГ|ВЭБ|ВИЧ|ВГВ|ВГС|ВПЧ|CMV|HSV|EBV|HIV|HBV|HCV|HPV|coli|pylori|aeruginosa|"
    r"albicans|tuberculosis)\b|"
    r"(Chlamyd|Salmonell|Herpes|Aspergill|Candida|Mycobacter|Escherichia|Klebsiel|"
    r"Staphyloc|Streptoc|Pseudomon|Helicobacter|Treponema|Toxoplasm|Legionell|"
    r"Enterobacter|Enterococc|Neisseri|Bacteroid|Clostrid|Acinetobacter|"
    r"Borrelia|Rickettsia|Bordetella)", re.I)
_STAGE = re.compile(r"(стади|\b[IVXШНУ1]{1,3}\s*[-–]\s*[IVXШНУ1]{1,3}\b)", re.I)
_CELL = re.compile(r"(\bNK\b|\bЫК\b|\bCD\d{1,3}\b|[TBТВ]-?(клет|лимфоц|cell|helper|reg))", re.I)
_DRUGCLASS = re.compile(r"(ингибитор|блокатор|антагонист|моноклонал|-mab\b|-ib\b|"
                        r"статин|сартан|инкретин|глифлозин|таксан)", re.I)
_BIBLI = re.compile(r"(Archive|Journal|Volume|Proceedings|Bulletin|Review\b)", re.I)  # не патоген

def decoded(t):
    parts = [t.get('source_text', '')]
    parts += [c for c in (t.get('candidates') or []) if c]
    ls = t.get('llm_suggestion') or {}
    if ls.get('predicted') and ls.get('predicted') not in ('SOURCE_OK', 'ILLEGIBLE'):
        parts.append(str(ls['predicted']))
    if t.get('resolved_text'):
        parts.append(t['resolved_text'])
    return " ".join(parts)

def reclass(t):
    txt = decoded(t)
    if _PATHO.search(txt) and not _BIBLI.search(txt): return 'pathogen'
    if _STAGE.search(txt) or t.get('entity_kind') == 'stage': return 'stage'
    if _CELL.search(txt): return 'cell'
    if _DRUGCLASS.search(txt): return 'drug_class'
    return None

# ---- doc text (latin + excluded) with fold->orig position map ----
_cache = {}
def doc_texts(doc):
    if doc in _cache: return _cache[doc]
    parts = []
    p = os.path.join(LATIN, doc + ".json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            def w(secs):
                for x in secs:
                    if x.get("text"): parts.append(x["text"])
                    w(x.get("children", []))
            w(d.get("sections", []))
            for tb in d.get("tables", []):
                if tb.get("raw_text"): parts.append(tb["raw_text"])
            for cat in (d.get("excluded", {}) or {}).values():
                for x in (cat or []):
                    if isinstance(x, dict) and x.get("text"): parts.append(x["text"])
        except Exception:
            pass
    orig = "\n".join(parts)
    folded = []; origpos = []
    for i, c in enumerate(orig):
        for ch in foldchar(c):
            if ch.isalnum():
                folded.append(ch); origpos.append(i)
    res = (orig, "".join(folded), origpos)
    _cache[doc] = res
    return res

def url_all_occurrences(task, W=90):
    """True только если source_text найдено И КАЖДОЕ вхождение в URL-контексте (точный фильтр)."""
    fs = foldkey(task.get('source_text', ''))
    if len(fs) < 4: return False
    orig, folded, origpos = doc_texts(task.get('doc', ''))
    if not folded: return False
    hits = 0; total = 0; a = folded.find(fs)
    while a != -1:
        total += 1
        b = a + len(fs)
        oa = origpos[max(0, a - W)]; ob = origpos[min(len(origpos) - 1, b + W - 1)]
        if _URL.search(orig[oa:ob]): hits += 1
        a = folded.find(fs, a + 1)
    return total > 0 and hits == total

def main():
    v3 = json.load(open(os.path.join(QDIR, "tasks_min_v3.json"), encoding="utf-8"))
    CRIT = {"icd", "atc", "tnm", "dose", "drug", "gene"}

    url_removed, active = [], []
    for t in v3:
        t.pop("priority_tier", None); t.pop("priority_kind", None)  # пересчёт с нуля
        if not t.get("is_critical") and url_all_occurrences(t):     # п.2 (крит. не трогаем)
            t["_filter"] = "url_context_v2"
            url_removed.append(t)
            continue
        active.append(t)

    prio_new = Counter()
    for t in active:                                                # п.7
        if t.get("entity_kind") in CRIT or t.get("is_critical"):
            continue
        nk = reclass(t)
        if nk:
            t["priority_tier"] = "priority"; t["priority_kind"] = nk
            prio_new[nk] += 1

    def tier_of(t):
        if t.get("entity_kind") in CRIT or t.get("is_critical"): return 0
        if t.get("priority_tier") == "priority": return 1
        return 2
    def has_cand(t):
        c = [x for x in (t.get("candidates") or []) if x and x != t.get("source_text")]
        return bool(c) or (t.get("resolved_text") and t.get("resolved_text") != t.get("source_text"))
    active.sort(key=lambda t: (tier_of(t), 0 if has_cand(t) else 1,
                               -t.get("occurrences", 1), -len(t.get("docs", []))))
    for i, t in enumerate(active):
        t["queue_rank"] = i          # порядок для показа; task_id СТАБИЛЕН (реконсиляция с LS по id)

    json.dump(active, open(os.path.join(QDIR, "tasks_min_v4.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    # отдельный файл (overwrite -> идемпотентно); полный URL-шум = _url_noise.jsonl(299) + этот
    with open(os.path.join(QDIR, "_url_noise_v2.jsonl"), "w", encoding="utf-8") as fh:
        for t in url_removed:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")

    crit = [t for t in active if tier_of(t) == 0]
    prio = [t for t in active if tier_of(t) == 1]
    term = [t for t in active if tier_of(t) == 2]
    print("=" * 64)
    print("ДОРАБОТКА ОЧЕРЕДИ (ревью 4): URL-фильтр + реклассификация")
    print("=" * 64)
    print("вход tasks_min_v3:                    %d" % len(v3))
    print("п.2 URL-контекст (v2, ВСЕ вхожд.) -> _url_noise: %d (0 критических)" % len(url_removed))
    print("-" * 64)
    print("АКТИВНАЯ ОЧЕРЕДЬ v4:                   %d" % len(active))
    print("  критические (icd/atc/tnm/dose/gene): %d" % len(crit))
    print("  ПРИОРИТЕТ (п.7 патоген/стадия/клетка/класс): %d  %s" % (len(prio), dict(prio_new)))
    print("  обычные термины:                     %d" % len(term))
    print("-" * 64)
    print("файлы: tasks_min_v4.json | _url_noise_v2.jsonl (%d) | всего URL-шум: %d"
          % (len(url_removed), 299 + len(url_removed)))

if __name__ == "__main__":
    main()
