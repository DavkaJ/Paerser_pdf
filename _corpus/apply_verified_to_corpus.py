# -*- coding: utf-8 -*-
"""ROADMAP шаг 1: применить 72 проверенные правки (_verified_corrections.json) к ТЕКСТУ
корпуса через движковый оверлей (decision=auto, source=human_verified) + перепарс с OCR +
АУДИТ каждой замены против источника. НЕ строковой заменой off-pipeline.

Схема (без веерных Опус-прогонов; CPU-параллелизм):
  1. Перепарс ЗАТРОНУТЫХ докумантов (list из _affected_docs.json) с latin_recovery+OCR и
     CR_VERIFIED_OVERLAY=1 -> staging-каталог (_overlay_staging/). tasks.json НЕ трогаем.
  2. АУДИТ каждого дока: reconstruct(old_zone, verified_map) == new_zone для ВСЕХ
     редактируемых зон (никаких необъяснимых изменений); каждая human_verified-правка
     присутствует в карте 72, applied=True, decision=auto; source-форма исчезла, corrected есть.
  3. КОНТРОЛЬНАЯ ГРУППА I1: перепарс 6 здоровых докумантов с оверлеем -> обязаны быть
     байт-в-байт равны диску (оверлей инертен на здоровом).
  4. Дельта-отчёт по причинам (provenance): что применено, где, сколько; что НЕ применено.

Промоушен (копирование staging -> outout_latin) — ОТДЕЛЬНЫМ шагом после зелёного аудита,
здесь НЕ делается (безопасность: битый аудит не портит корпус).

    TESSERACT_CMD=.. TESSDATA_PREFIX=.. python _corpus/apply_verified_to_corpus.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor

_WORD = "0-9A-Za-zА-Яа-яЁё"

warnings.filterwarnings("ignore")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

OUT = os.path.join(_ROOT, "outout_latin")
STAGING = os.path.join(_ROOT, "_corpus", "_overlay_staging")
VERIFIED = os.path.join(_ROOT, "_corpus", "verify_queue", "_verified_corrections.json")
AFFECTED = os.path.join(_ROOT, "_corpus", "_affected_docs.json")
REPORT_MD = os.path.join(_ROOT, "_corpus", "_overlay_delta_report.md")
REPORT_JSON = os.path.join(_ROOT, "_corpus", "_overlay_audit.json")
I1 = ["КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"]

# сегментные помощники движка — тем же кодом, что и _apply (иначе аудит разошёлся бы с движком)
from crparser.engine.latinrecovery import _SEG_RE, _strip_edges  # noqa: E402

_PARSER = None
_WRITER = None


def _init(registry):
    global _PARSER, _WRITER
    os.environ["CR_VERIFIED_OVERLAY"] = "1"          # оверлей ВКЛ явно
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    # queue_dir=None: кропы/очередь НЕ пишем (step 1 меняет только текст+провенанс)
    _PARSER = DocumentParser(create_profile("cr", registry),
                             latin_recovery=True, latin_queue_dir=None)
    _WRITER = JsonWriter()


def work(base):
    """Перепарс одного дока с оверлеем -> staging. Возврат: (base, ok, n_hv, secs)."""
    t0 = time.time()
    try:
        res = _PARSER.parse(os.path.join(_ROOT, "data", "raw", base + ".pdf"))
        doc = _WRITER.to_dict(res)
        _WRITER._atomic_dump(doc, os.path.join(STAGING, base + ".json"))
        corr = (doc.get("latin_recovery") or {}).get("corrections", []) or []
        n_hv = sum(1 for c in corr if c.get("source") == "human_verified")
        return (base, True, n_hv, round(time.time() - t0, 1))
    except Exception as exc:  # noqa: BLE001
        return (base, False, repr(exc), round(time.time() - t0, 1))


# --------------------------- АУДИТ ---------------------------------------- #
def _struct_sig(doc):
    """Структурная сигнатура (кол-во узлов на каждом уровне) — оверлей структуру НЕ меняет.
    Расхождение => сравнивать зоны позиционно нельзя, это само по себе необъяснимо."""
    n_sec = [0]

    def walk(secs):
        for s in secs:
            n_sec[0] += 1
            walk(s.get("children", []) or [])
    walk(doc.get("sections", []))
    return (n_sec[0], len(doc.get("tables", []) or []),
            len(((doc.get("excluded") or {}).get("appendices") or [])))


def zone_pairs(old, new):
    """ПОЗИЦИОННО выровненные (where, old_text, new_text) редактируемых зон, ровно те, что
    мутирует _gather_zones. Позиционно (а не по section_id — он часто None и коллидирует)."""
    pairs = []

    def walk(os_, ns_):
        for so, sn in zip(os_, ns_):
            w = "sec[%s]" % (so.get("number") or so.get("section_id") or "?")
            pairs.append((w + "#title", so.get("title") or "", sn.get("title") or ""))
            pairs.append((w + "#text", so.get("text") or "", sn.get("text") or ""))
            walk(so.get("children", []) or [], sn.get("children", []) or [])
    walk(old.get("sections", []), new.get("sections", []))
    ot, nt = old.get("tables", []) or [], new.get("tables", []) or []
    for to, tn in zip(ot, nt):
        w = "table[%s]" % to.get("number", "")
        pairs.append((w + "#raw", to.get("raw_text") or "", tn.get("raw_text") or ""))
        pairs.append((w + "#cap", to.get("caption") or "", tn.get("caption") or ""))
    oa = (old.get("excluded") or {}).get("appendices") or []
    na = (new.get("excluded") or {}).get("appendices") or []
    for io, inw in zip(oa, na):
        if isinstance(io, dict) and isinstance(inw, dict):
            pairs.append(("appendix#text", io.get("text") or "", inw.get("text") or ""))
            pairs.append(("appendix#title", io.get("title") or "", inw.get("title") or ""))
    omd, nmd = old.get("metadata") or {}, new.get("metadata") or {}
    pairs.append(("metadata:title", omd.get("title") or "", nmd.get("title") or ""))
    return pairs


def reconstruct(old_text, tok_map, phrase_map):
    """Применить проверенные правки к old_text ТЕМ ЖЕ сегментным контрактом, что и движок:
    фразы (строковая замена) -> сегментный self._g-проход. Результат ОБЯЗАН совпасть с
    выходом движка (new_text). Любое расхождение = необъяснимое изменение."""
    text = old_text
    for src, dst in phrase_map.items():
        if src in text:
            text = text.replace(src, dst)
    parts = _SEG_RE.split(text)
    for i in range(0, len(parts), 2):
        word = parts[i]
        if not word:
            continue
        lead, core, trail = _strip_edges(word)
        if not core:
            continue
        dst = tok_map.get(core)
        if dst is not None and dst != core:
            parts[i] = lead + dst + trail
    return "".join(parts)


def audit_doc(base, verified):
    """Вернуть dict-результат аудита одного дока."""
    old = json.load(open(os.path.join(OUT, base + ".json"), encoding="utf-8"))
    new = json.load(open(os.path.join(STAGING, base + ".json"), encoding="utf-8"))
    tok_map = {s: e["corrected"] for s, e in verified.items() if " " not in s}
    phrase_map = {s: e["corrected"] for s, e in verified.items() if " " in s}

    r = {"doc": base, "struct_mismatch": None, "unexplained_zones": [], "prov_bad": [],
         "applied": [], "source_leaks": [], "corrected_missing": []}

    # (0) СТРУКТУРА идентична (оверлей не меняет число узлов) — иначе позиц. сверка неверна
    so, sn = _struct_sig(old), _struct_sig(new)
    if so != sn:
        r["struct_mismatch"] = {"old": so, "new": sn}

    pairs = zone_pairs(old, new)
    blob_parts = []
    # (1) ТЕКСТ: reconstruct(old)==new для каждой зоны (позиционно)
    for where, otext, ntext in pairs:
        blob_parts.append(ntext)
        if reconstruct(otext, tok_map, phrase_map) != ntext:
            r["unexplained_zones"].append(where)

    # (2) ПРОВЕНАНС human_verified: пара в карте, applied=True, decision=auto
    corr = (new.get("latin_recovery") or {}).get("corrections", []) or []
    for c in corr:
        if c.get("source") != "human_verified":
            continue
        st, rt = c.get("source_text"), c.get("resolved_text")
        ok = (st in verified and verified[st]["corrected"] == rt
              and c.get("applied") is True and c.get("decision") == "auto")
        if not ok:
            r["prov_bad"].append({"source_text": st, "resolved_text": rt,
                                  "applied": c.get("applied"), "decision": c.get("decision")})
        else:
            r["applied"].append({"source_text": st, "resolved_text": rt,
                                 "provenance": c.get("rule"),
                                 "occurrences": c.get("occurrences")})

    # (3) ПРИСУТСТВИЕ: для применённых форм source исчез, corrected есть (по всему тексту зон)
    blob = "\n".join(blob_parts)
    for a in r["applied"]:
        st, rt = a["source_text"], a["resolved_text"]
        # source не должен оставаться КАК ТОКЕН (word-boundary; подстрока была бы шумом)
        if re.search(r"(?<![%s])%s(?![%s])" % (_WORD, re.escape(st), _WORD), blob):
            r["source_leaks"].append(st)
        if rt not in blob:
            r["corrected_missing"].append(rt)
    # дедуп (source_leaks/corrected_missing могут повторяться по нескольким записям)
    r["source_leaks"] = sorted(set(r["source_leaks"]))
    r["corrected_missing"] = sorted(set(r["corrected_missing"]))

    r["clean"] = not (r["struct_mismatch"] or r["unexplained_zones"] or r["prov_bad"]
                      or r["source_leaks"] or r["corrected_missing"])
    return r


def control_i1(registry):
    """Перепарс I1 с оверлеем -> сравнить с диском байт-в-байт (оверлей инертен)."""
    os.environ["CR_VERIFIED_OVERLAY"] = "1"
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    p = DocumentParser(create_profile("cr", registry), latin_recovery=True,
                       latin_queue_dir=None)
    w = JsonWriter()
    res = []
    for base in I1:
        disk_p = os.path.join(OUT, base + ".json")
        if not os.path.isfile(disk_p):
            res.append((base, None, "нет на диске"))
            continue
        got = json.dumps(w.to_dict(p.parse(os.path.join(_ROOT, "data", "raw", base + ".pdf"))),
                         ensure_ascii=False, sort_keys=True)
        disk = json.dumps(json.load(open(disk_p, encoding="utf-8")),
                          ensure_ascii=False, sort_keys=True)
        res.append((base, got == disk, None if got == disk else "РАСХОЖДЕНИЕ"))
    return res


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    registry = glob.glob(os.path.join(_ROOT, "*.xlsx"))[0]
    verified = json.load(open(VERIFIED, encoding="utf-8"))
    affected = json.load(open(AFFECTED, encoding="utf-8"))
    os.makedirs(STAGING, exist_ok=True)

    from crparser.engine.ocr import ocr_precondition
    ok, msg = ocr_precondition()
    if not ok:
        print("ОСТАНОВ: OCR недоступен — %s. Задайте TESSERACT_CMD/TESSDATA_PREFIX." % msg)
        return 2
    print("OCR: доступен. Затронуто докумантов: %d. Оверлей форм: %d." % (len(affected), len(verified)))

    # --- 1. перепарс -> staging (параллельно) ---
    t0 = time.time()
    workers = int(os.environ.get("CR_LATIN_WORKERS", "8"))
    results = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                             initargs=(registry,)) as ex:
        for i, r in enumerate(ex.map(work, affected), 1):
            results.append(r)
            if i % 20 == 0:
                print("  перепарс ...%d/%d (%.0fs)" % (i, len(affected), time.time() - t0))
    crashed = [r for r in results if not r[1]]
    print("перепарс готов: %d ок, %d ошибок (%.0fs)"
          % (len(results) - len(crashed), len(crashed), time.time() - t0))
    for r in crashed:
        print("   CRASH %s: %s" % (r[0], r[2]))

    # --- 2. аудит ---
    audits = [audit_doc(b, verified) for b in affected
              if os.path.isfile(os.path.join(STAGING, b + ".json"))]
    dirty = [a for a in audits if not a["clean"]]
    print("\n=== АУДИТ (%d докумантов) ===" % len(audits))
    print("чистых: %d | грязных: %d" % (len(audits) - len(dirty), len(dirty)))
    for a in dirty:
        print("  ГРЯЗНЫЙ %s: unexplained_zones=%s prov_bad=%s source_leaks=%s corrected_missing=%s"
              % (a["doc"], a["unexplained_zones"][:5], a["prov_bad"][:3],
                 a["source_leaks"][:5], a["corrected_missing"][:5]))

    # --- 3. контрольная группа I1 ---
    print("\n=== КОНТРОЛЬНАЯ ГРУППА I1 (байт-в-байт) ===")
    i1 = control_i1(registry)
    for base, same, note in i1:
        print("  %-10s %s" % (base, "БАЙТ-В-БАЙТ ✓" if same else ("ПРОПУСК: %s" % note)))
    i1_ok = all(s for _, s, _ in i1)

    # --- 4. дельта-отчёт по причинам ---
    from collections import Counter, defaultdict
    per_form_docs = defaultdict(set)
    per_form_occ = Counter()
    per_prov = Counter()
    for a in audits:
        for x in a["applied"]:
            per_form_docs[x["source_text"]].add(a["doc"])
            per_form_occ[x["source_text"]] += (x["occurrences"] or 1)
            per_prov[x["provenance"]] += (x["occurrences"] or 1)
    applied_forms = set(per_form_docs)
    not_applied = [s for s in verified if s not in applied_forms]

    lines = ["# Дельта-отчёт: применение проверенных правок к тексту корпуса (ROADMAP шаг 1)",
             "",
             "Механизм: движковый оверлей `self._g` (decision=auto, source=human_verified) +",
             "перепарс с OCR. Аудит: reconstruct(old_zone, verified_map)==new_zone для каждой",
             "редактируемой зоны — необъяснимых изменений нет.", "",
             "## Итог",
             "- затронуто докумантов (вход): **%d**" % len(affected),
             "- форм в карте: **%d** (применено: **%d**, не применено: **%d**)"
             % (len(verified), len(applied_forms), len(not_applied)),
             "- всего замен (occurrences): **%d**" % sum(per_form_occ.values()),
             "- аудит: **%s** (чистых %d / грязных %d)"
             % ("ЧИСТО" if not dirty else "ГРЯЗНО", len(audits) - len(dirty), len(dirty)),
             "- контрольная группа I1 байт-в-байт: **%s**" % ("ДА" if i1_ok else "НЕТ"),
             "",
             "## По провенансу (occurrences)"]
    for prov, n in per_prov.most_common():
        lines.append("- `%s`: %d замен" % (prov, n))
    lines += ["", "## Применённые формы (source -> corrected | occ | docs)"]
    for s in sorted(applied_forms, key=lambda x: -per_form_occ[x]):
        lines.append("- `%s` -> `%s` | occ=%d | docs=%d %s"
                     % (s, verified[s]["corrected"], per_form_occ[s],
                        len(per_form_docs[s]), sorted(per_form_docs[s])[:8]))
    lines += ["", "## НЕ применённые формы (в тексте не встретились дословно)"]
    for s in not_applied:
        lines.append("- `%s` -> `%s`  [%s] — 0 вхождений в затронутых зонах"
                     % (s, verified[s]["corrected"], verified[s].get("provenance")))
    if dirty:
        lines += ["", "## ГРЯЗНЫЕ документы (требуют разбора)"]
        for a in dirty:
            lines.append("- %s: unexplained_zones=%s prov_bad=%s source_leaks=%s corrected_missing=%s"
                         % (a["doc"], a["unexplained_zones"], a["prov_bad"],
                            a["source_leaks"], a["corrected_missing"]))
    open(REPORT_MD, "w", encoding="utf-8").write("\n".join(lines))
    json.dump({"audits": audits, "i1": [(b, s) for b, s, _ in i1],
               "not_applied": not_applied,
               "per_prov": dict(per_prov), "per_form_occ": dict(per_form_occ)},
              open(REPORT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    verdict = (not dirty) and i1_ok and not crashed
    print("\nотчёт: %s" % os.path.relpath(REPORT_MD, _ROOT))
    print("применено форм: %d / %d, замен: %d, не применено: %d"
          % (len(applied_forms), len(verified), sum(per_form_occ.values()), len(not_applied)))
    print("ВЕРДИКТ: %s" % ("ЧИСТО — можно промоутить staging -> outout_latin"
                           if verdict else "ЕСТЬ ПРОБЛЕМЫ — промоушен ЗАПРЕЩЁН"))
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
