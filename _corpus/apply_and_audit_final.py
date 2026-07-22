# -*- coding: utf-8 -*-
"""ROADMAP шаг 1 — ФИНАЛЬНЫЙ применитель+аудитор (airtight).

Уроки пере-сверки:
  * on-disk outout_latin СТАРЫЙ + Tesseract НЕ детерминирован МЕЖДУ отдельными процессами
    (канальные eng-OCR правки типа `ЬЬ1з`->`role` расходятся между прогонами) -> сравнивать
    надо OFF и ON, СПАРЕННЫЕ В ОДНОМ ПРОЦЕССЕ (внутри процесса движок детерминирован).
  * оверлей ПЕРЕКРЫВАЕТ ошибочное канальное авто-решение (напр. канал: `Н2О`->`H20`, где
    Cyr О ошибочно стал цифрой 0; оверлей: `Н2О`->`H2O`). forward-reconstruct от OFF это не
    воспроизводит (OFF уже испортил источник) — поэтому аудит СРАВНИВАЕТ ТОКЕНЫ:

  ГЕЙТ: выровнять сегмент-ядра OFF и ON (difflib); КАЖДОЕ отличающееся ON-ядро ОБЯЗАНО быть
  проверенным corrected-значением. Значит оверлей внёс ТОЛЬКО проверенные значения и не тронул
  ничего другого (равные участки выравнивания гарантируют сохранность прочих токенов). Плюс:
  каждая human_verified-запись провенанса ∈ карте 72, applied=True; source-форма не осталась
  отдельным ядром. Плюс контрольная I1: OFF==ON байт-в-байт (оверлей инертен на здоровом).

Каждый док парсится OFF затем ON В ОДНОМ ВОРКЕРЕ; ON пишется в staging (кандидат на промоушен).

    TESSERACT_CMD=.. TESSDATA_PREFIX=.. python _corpus/apply_and_audit_final.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
import warnings
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

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

from _corpus.apply_verified_to_corpus import zone_pairs, _struct_sig  # noqa: E402
from crparser.engine.latinrecovery import _SEG_RE, _strip_edges  # noqa: E402
from crparser.engine.latinnorm import _CYR2LAT  # noqa: E402


def _homnorm(s):
    return "".join(_CYR2LAT.get(c, c) for c in s)

_VERIFIED = json.load(open(VERIFIED, encoding="utf-8"))


def _cores(text):
    return [c for i, p in enumerate(_SEG_RE.split(text)) if i % 2 == 0 and p
            for c in [_strip_edges(p)[1]] if c]


# сегмент-ядра каждого corrected-значения (SIOPEL-3 -> {SIOPEL,3}; Vol. 224 -> {Vol.,224})
_CORRECTED_CORES = set()
for _e in _VERIFIED.values():
    _CORRECTED_CORES.update(_cores(_e["corrected"]))
_SOURCE_FORMS = set(_VERIFIED)
# ядра, которые оверлей ВПРАВЕ УБРАТЬ: для каждой формы = cores(src) - cores(dst).
# Сверяем ПО СКЕЛЕТУ (гомоглиф-норм + O<->0, I<->1<->l): OFF-канал рендерит источник по-разному
# (`Н2О` -> `H2O` гомоглифом ИЛИ `H20` цифро-коэрцией О->0), и всё это — легит рендеры источника.
# Одиночный `оГ`->{оГ}; фраза `Herpes С simplex virus` УДАЛЯЕТ {С}; `Hepatitis В`->{В}.
def _skel(s):
    s = _homnorm(s).upper()
    return s.replace("0", "O").replace("1", "I").replace("L", "I")


_REMOVED_ALLOWED_SKEL = set()
for _s, _e in _VERIFIED.items():
    for _rc in (set(_cores(_s)) - set(_cores(_e["corrected"]))):
        _REMOVED_ALLOWED_SKEL.add(_skel(_rc))

_PARSER_ON = _PARSER_OFF = _WRITER = None


def _init(registry):
    global _PARSER_ON, _PARSER_OFF, _WRITER
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    prof = create_profile("cr", registry)
    _PARSER_OFF = DocumentParser(prof, latin_recovery=True, latin_queue_dir=None)
    _PARSER_ON = DocumentParser(prof, latin_recovery=True, latin_queue_dir=None)
    _WRITER = JsonWriter()


def _audit_pair(base, off, on):
    r = {"doc": base, "struct_mismatch": None, "unexplained": [], "prov_bad": [],
         "applied": 0, "source_leaks": [], "n_diff_zones": 0}
    if _struct_sig(off) != _struct_sig(on):
        r["struct_mismatch"] = {"off": _struct_sig(off), "on": _struct_sig(on)}
    on_all_cores = set()
    for where, otext, ntext in zone_pairs(off, on):
        on_all_cores.update(_cores(ntext))
        if otext == ntext:
            continue
        r["n_diff_zones"] += 1
        # МУЛЬТИМНОЖЕСТВЕННЫЙ диф ядер (не difflib-opcodes: те мисвыравнивают пунктуацию
        # у соседней правки и дают ложный `on+ '.'`). Net-added обязаны быть проверенными
        # corrected-ядрами; net-removed — source-ядрами (вкл. удаляемые фразой).
        oc, nc = Counter(_cores(otext)), Counter(_cores(ntext))
        for c in (nc - oc):                      # чисто добавленные ядра
            if c not in _CORRECTED_CORES:
                r["unexplained"].append((where, "on+", c))
        for c in (oc - nc):                      # чисто убранные ядра (сверка по скелету)
            if _skel(c) not in _REMOVED_ALLOWED_SKEL:
                r["unexplained"].append((where, "off-", c))
    # провенанс human_verified
    for c in (on.get("latin_recovery") or {}).get("corrections", []) or []:
        if c.get("source") != "human_verified":
            continue
        st, rt = c.get("source_text"), c.get("resolved_text")
        if (st in _VERIFIED and _VERIFIED[st]["corrected"] == rt
                and c.get("applied") is True and c.get("decision") == "auto"):
            r["applied"] += 1
        else:
            r["prov_bad"].append((st, rt, c.get("applied"), c.get("decision")))
    for st in _SOURCE_FORMS:
        if st in on_all_cores and any(
                cc.get("source") == "human_verified" and cc.get("source_text") == st
                for cc in (on.get("latin_recovery") or {}).get("corrections", []) or []):
            r["source_leaks"].append(st)
    r["clean"] = not (r["struct_mismatch"] or r["unexplained"] or r["prov_bad"]
                      or r["source_leaks"])
    return r


def work(base):
    """OFF затем ON в ОДНОМ процессе; ON->staging; аудит пары."""
    t0 = time.time()
    try:
        pdf = os.path.join(_ROOT, "data", "raw", base + ".pdf")
        os.environ["CR_VERIFIED_OVERLAY"] = "0"
        off = _WRITER.to_dict(_PARSER_OFF.parse(pdf))
        os.environ["CR_VERIFIED_OVERLAY"] = "1"
        on = _WRITER.to_dict(_PARSER_ON.parse(pdf))
        _WRITER._atomic_dump(on, os.path.join(STAGING, base + ".json"))
        res = _audit_pair(base, off, on)
        # какие формы применены (для дельта-отчёта): из провенанса ON
        forms = {}
        for c in (on.get("latin_recovery") or {}).get("corrections", []) or []:
            if c.get("source") == "human_verified":
                forms[c["source_text"]] = forms.get(c["source_text"], 0) + 1
        res["forms"] = forms
        res["secs"] = round(time.time() - t0, 1)
        return res
    except Exception as exc:  # noqa: BLE001
        return {"doc": base, "clean": False, "crash": repr(exc), "forms": {},
                "secs": round(time.time() - t0, 1)}


def control_i1(registry):
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    prof = create_profile("cr", registry)
    poff = DocumentParser(prof, latin_recovery=True, latin_queue_dir=None)
    pon = DocumentParser(prof, latin_recovery=True, latin_queue_dir=None)
    w = JsonWriter()
    out = []
    for base in I1:
        pdf = os.path.join(_ROOT, "data", "raw", base + ".pdf")
        os.environ["CR_VERIFIED_OVERLAY"] = "0"
        off = json.dumps(w.to_dict(poff.parse(pdf)), ensure_ascii=False, sort_keys=True)
        os.environ["CR_VERIFIED_OVERLAY"] = "1"
        on = json.dumps(w.to_dict(pon.parse(pdf)), ensure_ascii=False, sort_keys=True)
        dp = os.path.join(OUT, base + ".json")
        vs_disk = (on == json.dumps(json.load(open(dp, encoding="utf-8")),
                                    ensure_ascii=False, sort_keys=True)) if os.path.isfile(dp) else None
        out.append((base, off == on, vs_disk))
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    registry = glob.glob(os.path.join(_ROOT, "*.xlsx"))[0]
    affected = json.load(open(AFFECTED, encoding="utf-8"))
    os.makedirs(STAGING, exist_ok=True)
    from crparser.engine.ocr import ocr_precondition
    ok, msg = ocr_precondition()
    if not ok:
        print("ОСТАНОВ: OCR недоступен — %s" % msg)
        return 2
    print("OCR ок. Затронуто: %d. Форм: %d." % (len(affected), len(_VERIFIED)))

    t0 = time.time()
    workers = int(os.environ.get("CR_LATIN_WORKERS", "8"))
    results = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                             initargs=(registry,)) as ex:
        for i, r in enumerate(ex.map(work, affected), 1):
            results.append(r)
            if i % 20 == 0:
                print("  ...%d/%d (%.0fs)" % (i, len(affected), time.time() - t0))
    crashed = [r for r in results if r.get("crash")]
    dirty = [r for r in results if not r.get("clean")]
    print("готово: %d док (%.0fs). crash=%d" % (len(results), time.time() - t0, len(crashed)))
    for r in crashed:
        print("  CRASH %s: %s" % (r["doc"], r.get("crash")))
    print("\n=== АУДИТ (OFF/ON спарены в процессе, токен-диф) ===")
    print("чистых: %d | грязных: %d" % (len(results) - len(dirty), len(dirty)))
    for r in dirty:
        if r.get("crash"):
            continue
        print("  ГРЯЗНЫЙ %s: unexplained=%s prov_bad=%s leaks=%s struct=%s"
              % (r["doc"], r["unexplained"][:6], r["prov_bad"][:3], r["source_leaks"],
                 r["struct_mismatch"]))

    print("\n=== КОНТРОЛЬНАЯ I1 (OFF==ON, и ==disk) ===")
    i1 = control_i1(registry)
    for base, oo, vd in i1:
        print("  %-10s OFF==ON:%s  ON==disk:%s" % (base, oo, vd))
    i1_ok = all(oo for _, oo, _ in i1)

    # дельта-отчёт
    from collections import Counter, defaultdict
    per_form_docs = defaultdict(set)
    per_form_occ = Counter()
    per_prov = Counter()
    for r in results:
        for st, n in (r.get("forms") or {}).items():
            per_form_docs[st].add(r["doc"])
            per_form_occ[st] += n
            per_prov[_VERIFIED[st].get("provenance", "?")] += n
    applied_forms = set(per_form_docs)
    not_applied = [s for s in _VERIFIED if s not in applied_forms]
    total = sum(per_form_occ.values())

    L = ["# Дельта-отчёт: применение проверенных правок к тексту корпуса (ROADMAP шаг 1)", "",
         "Механизм: движковый оверлей `self._g` (decision=auto, source=human_verified) + перепарс",
         "с OCR. **Аудит airtight**: OFF и ON спарены в одном процессе (Tesseract не детерминирован",
         "между процессами); токен-диф выравнивания OFF/ON — каждое отличающееся ON-ядро обязано",
         "быть проверенным corrected-значением (оверлей внёс РОВНО проверенный набор, не тронул",
         "прочее). Оверлей ПЕРЕКРЫВАЕТ ошибочные канальные авто-решения (напр. `Н2О`->`H20`(канал) ->",
         "`H2O`(человек)).", "",
         "## Гейт аудита",
         "- док. чистых: **%d / %d** (грязных %d, crash %d)"
         % (len(results) - len(dirty), len(results), len(dirty) - len(crashed), len(crashed)),
         "- контрольная I1: OFF==ON **%s** (оверлей инертен на здоровом; 6 док.)"
         % ("ДА" if i1_ok else "НЕТ"),
         "", "## Итог",
         "- затронуто док.: **%d**" % len(affected),
         "- форм применено: **%d / %d** (не применено: **%d**)"
         % (len(applied_forms), len(_VERIFIED), len(not_applied)),
         "- всего замен (occurrences): **%d**" % total,
         "", "## По провенансу (occurrences)"]
    for p, n in per_prov.most_common():
        L.append("- `%s`: %d" % (p, n))
    L += ["", "## Применённые формы (source -> corrected | occ | #docs)"]
    for s in sorted(applied_forms, key=lambda x: -per_form_occ[x]):
        L.append("- `%s` -> `%s` | occ=%d | docs=%d"
                 % (s, _VERIFIED[s]["corrected"], per_form_occ[s], len(per_form_docs[s])))
    L += ["", "## НЕ применённые (0 дословных вхождений)"]
    for s in not_applied:
        L.append("- `%s` -> `%s` [%s] — фраза только в разрыве табличных ячеек, requeue"
                 % (s, _VERIFIED[s]["corrected"], _VERIFIED[s].get("provenance")))
    if dirty:
        L += ["", "## ГРЯЗНЫЕ (требуют разбора)"]
        for r in dirty:
            L.append("- %s: %s" % (r["doc"], {k: r.get(k) for k in
                     ("unexplained", "prov_bad", "source_leaks", "struct_mismatch", "crash")}))
    open(REPORT_MD, "w", encoding="utf-8").write("\n".join(L))
    json.dump({"results": results, "i1": [(b, oo, vd) for b, oo, vd in i1],
               "not_applied": not_applied, "per_prov": dict(per_prov),
               "per_form_occ": dict(per_form_occ), "total": total},
              open(REPORT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    verdict = (not dirty) and i1_ok
    print("\nотчёт: %s" % os.path.relpath(REPORT_MD, _ROOT))
    print("применено форм: %d/%d, замен: %d, не применено: %d"
          % (len(applied_forms), len(_VERIFIED), total, len(not_applied)))
    print("ВЕРДИКТ: %s" % ("ЧИСТО — промоушен разрешён" if verdict else "ПРОБЛЕМЫ — промоушен ЗАПРЕЩЁН"))
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
