# -*- coding: utf-8 -*-
"""ROADMAP шаг 2 — ПРИМЕНЕНИЕ 589 step2_auto правок к тексту (VLM+OCR+словарь, occ==1) через
ТОТ ЖЕ механизм, что шаг 1: изолированный OFF/ON аудит в одном процессе + I1.

БАЗА (OFF) = текущее промо-состояние: human_verified ВКЛ, step2 ВЫКЛ.
ON            = human_verified ВКЛ + step2 ВКЛ. Дифф OFF->ON = ТОЛЬКО step2-правки.
Аудит: мультимнож. токен-диф (net-added = проверенное corrected step2-ядро; net-removed по
скелету) + провенанс source==step2_auto ∈ карте. Контроль I1: OFF==ON байт (step2 инертен).

    TESSERACT_CMD=.. TESSDATA_PREFIX=.. python _corpus/apply_and_audit_step2.py
"""
from __future__ import annotations
import glob, json, os, sys, time, warnings
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
OUT = os.path.join(_ROOT, "outout_latin")
STAGING = os.path.join(_ROOT, "_corpus", "_step2_staging")
MAP = os.path.join(_ROOT, "_corpus", "verify_queue", "_step2_auto_apply.json")
AFFECTED = os.path.join(_ROOT, "_corpus", "_affected_docs_step2.json")
REPORT_MD = os.path.join(_ROOT, "_corpus", "_step2_apply_report.md")
REPORT_JSON = os.path.join(_ROOT, "_corpus", "_step2_apply_audit.json")
I1 = ["КР802_1", "КР845_1", "КР901_1", "КР1000_1", "КР66_4", "КР876_1"]

from _corpus.apply_verified_to_corpus import zone_pairs, _struct_sig  # noqa: E402
from _corpus.apply_and_audit_final import _cores, _homnorm  # noqa: E402

_MAP = json.load(open(MAP, encoding="utf-8"))
_CORRECTED = set()
for _e in _MAP.values():
    _CORRECTED.update(_cores(_e["corrected"]))


def _skel(s):
    s = _homnorm(s).upper()
    return s.replace("0", "O").replace("1", "I").replace("L", "I")


_REMOVED_SKEL = set()
for _s, _e in _MAP.items():
    for _rc in (set(_cores(_s)) - set(_cores(_e["corrected"]))):
        _REMOVED_SKEL.add(_skel(_rc))

_P_ON = _P_OFF = _W = None


def _init(registry):
    global _P_ON, _P_OFF, _W
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    prof = create_profile("cr", registry)
    _P_OFF = DocumentParser(prof, latin_recovery=True, latin_queue_dir=None)
    _P_ON = DocumentParser(prof, latin_recovery=True, latin_queue_dir=None)
    _W = JsonWriter()


def _audit(base, off, on):
    r = {"doc": base, "struct": None, "unexplained": [], "prov_bad": [], "applied": 0, "forms": {}}
    if _struct_sig(off) != _struct_sig(on):
        r["struct"] = {"off": _struct_sig(off), "on": _struct_sig(on)}
    for where, o, n in zone_pairs(off, on):
        if o == n:
            continue
        oc, nc = Counter(_cores(o)), Counter(_cores(n))
        for c in (nc - oc):
            if c not in _CORRECTED:
                r["unexplained"].append((where, "on+", c))
        for c in (oc - nc):
            if _skel(c) not in _REMOVED_SKEL:
                r["unexplained"].append((where, "off-", c))
    for c in (on.get("latin_recovery") or {}).get("corrections", []) or []:
        if c.get("source") != "step2_auto":
            continue
        st, rt = c.get("source_text"), c.get("resolved_text")
        if st in _MAP and _MAP[st]["corrected"] == rt and c.get("applied") and c.get("decision") == "auto":
            r["applied"] += 1
            r["forms"][st] = r["forms"].get(st, 0) + 1
        else:
            r["prov_bad"].append((st, rt))
    r["clean"] = not (r["struct"] or r["unexplained"] or r["prov_bad"])
    return r


def work(base):
    t0 = time.time()
    try:
        pdf = os.path.join(_ROOT, "data", "raw", base + ".pdf")
        os.environ["CR_VERIFIED_OVERLAY"] = "1"; os.environ["CR_STEP2_AUTO"] = "0"
        off = _W.to_dict(_P_OFF.parse(pdf))
        os.environ["CR_STEP2_AUTO"] = "1"
        on = _W.to_dict(_P_ON.parse(pdf))
        _W._atomic_dump(on, os.path.join(STAGING, base + ".json"))
        r = _audit(base, off, on); r["secs"] = round(time.time() - t0, 1)
        return r
    except Exception as exc:  # noqa: BLE001
        return {"doc": base, "clean": False, "crash": repr(exc), "forms": {}}


def control_i1(registry):
    from crparser.engine.parser import DocumentParser
    from crparser.engine.jsonio import JsonWriter
    from crparser.profiles import create_profile
    prof = create_profile("cr", registry)
    poff = DocumentParser(prof, latin_recovery=True, latin_queue_dir=None)
    pon = DocumentParser(prof, latin_recovery=True, latin_queue_dir=None)
    w = JsonWriter(); out = []
    for base in I1:
        pdf = os.path.join(_ROOT, "data", "raw", base + ".pdf")
        os.environ["CR_VERIFIED_OVERLAY"] = "1"; os.environ["CR_STEP2_AUTO"] = "0"
        off = json.dumps(w.to_dict(poff.parse(pdf)), ensure_ascii=False, sort_keys=True)
        os.environ["CR_STEP2_AUTO"] = "1"
        on = json.dumps(w.to_dict(pon.parse(pdf)), ensure_ascii=False, sort_keys=True)
        out.append((base, off == on))
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    reg = glob.glob(os.path.join(_ROOT, "*.xlsx"))[0]
    affected = json.load(open(AFFECTED, encoding="utf-8"))
    os.makedirs(STAGING, exist_ok=True)
    from crparser.engine.ocr import ocr_precondition
    ok, msg = ocr_precondition()
    if not ok:
        print("ОСТАНОВ: OCR — %s" % msg); return 2
    print("step2 форм: %d, затронуто док: %d" % (len(_MAP), len(affected)))
    t0 = time.time(); res = []
    with ProcessPoolExecutor(max_workers=int(os.environ.get("CR_LATIN_WORKERS", "8")),
                             initializer=_init, initargs=(reg,)) as ex:
        for i, r in enumerate(ex.map(work, affected), 1):
            res.append(r)
            if i % 20 == 0:
                print("  ...%d/%d (%.0fs)" % (i, len(affected), time.time() - t0))
    crash = [r for r in res if r.get("crash")]
    dirty = [r for r in res if not r.get("clean")]
    print("готово: %d док (%.0fs). crash=%d чистых=%d грязных=%d"
          % (len(res), time.time() - t0, len(crash), len(res) - len(dirty), len(dirty)))
    for r in dirty:
        print("  ГРЯЗНЫЙ %s: %s" % (r["doc"], {k: r.get(k) for k in ("unexplained", "prov_bad", "struct", "crash")}))
    i1 = control_i1(reg)
    print("I1 (OFF==ON, step2 инертен):")
    for b, same in i1:
        print("   %-10s %s" % (b, same))
    i1_ok = all(s for _, s in i1)
    applied_forms = {}
    for r in res:
        for f, n in (r.get("forms") or {}).items():
            applied_forms[f] = applied_forms.get(f, 0) + n
    L = ["# step2_auto — применение к тексту (589 форм, occ==1, VLM+OCR+мед.словарь)", "",
         "База OFF = human_verified (текущее промо); ON = human+step2. Дифф = только step2.",
         "Аудит: мультимнож. токен-диф (net-added ∈ step2 corrected-ядрам) + провенанс. ",
         "", "## Гейт",
         "- чистых: **%d / %d**, crash %d" % (len(res) - len(dirty), len(res), len(crash)),
         "- I1 OFF==ON байт: **%s**" % ("ДА" if i1_ok else "НЕТ"),
         "- форм применено: **%d / %d**, затронуто док: **%d**" % (len(applied_forms), len(_MAP), len(affected)),
         "", "## Применённые формы (source -> corrected)"]
    for f in sorted(applied_forms):
        L.append("- `%s` -> `%s`" % (f, _MAP[f]["corrected"]))
    if dirty:
        L += ["", "## ГРЯЗНЫЕ"] + ["- %s: %s" % (r["doc"], r.get("unexplained") or r.get("crash")) for r in dirty]
    open(REPORT_MD, "w", encoding="utf-8").write("\n".join(L))
    json.dump({"res": res, "i1": i1, "applied_forms": applied_forms}, open(REPORT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    verdict = (not dirty) and i1_ok
    print("\nприменено форм: %d/%d. ВЕРДИКТ: %s"
          % (len(applied_forms), len(_MAP), "ЧИСТО — промоушен разрешён" if verdict else "ПРОБЛЕМЫ"))
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
