# -*- coding: utf-8 -*-
"""
Пакетная проверка качества парсера на множестве КР (валидация Этапа 2).

Прогоняет пакет PDF параллельно, пишет JSON в out/, собирает по каждому файлу
сводку и агрегаты. Флагует подозрительные случаи (нет раздела 4, ToC в разделах,
низкое покрытие, метаданные только из титула, ошибки).

    python batch_report.py [N] [WORKERS] [head|spread]

  N        — сколько файлов обработать (по умолчанию 150)
  WORKERS  — число процессов (по умолчанию 8)
  head     — первые N файлов; spread — равномерная выборка по всему корпусу
"""

import sys, os, glob, json, time, re, warnings
warnings.filterwarnings("ignore")
from concurrent.futures import ProcessPoolExecutor

DATA = os.path.join("data", "текст_после_чистки")
OUT = "out"
_PARSER = None  # глобальный парсер на воркер


def _init(registry_path):
    global _PARSER
    from crparser.engine.parser import DocumentParser
    from crparser.profiles import create_profile
    _PARSER = DocumentParser(create_profile("cr", registry_path))


def _top_numbers(sections):
    return [s.get("number") for s in sections]


def _has_section4(sections):
    for s in sections:
        n = s.get("number")
        t = (s.get("title") or "").lower()
        if n == "4" or t.startswith("медицинская реабилитация"):
            return True
    return False


def _toc_leak(sections):
    """Эвристика: пункт ToC в разделах (точки-лидеры или дикий номер уровня 1)."""
    for s in sections:
        if re.search(r"\.{3,}", s.get("title") or ""):
            return True
    return False


def _flatten(sections):
    for s in sections:
        yield s
        yield from _flatten(s.get("children", []))


def work(pdf_path):
    base = os.path.basename(pdf_path)
    t0 = time.time()
    try:
        res = _PARSER.parse(pdf_path)
        from crparser.engine.jsonio import JsonWriter
        w = JsonWriter()
        d = w.to_dict(res)
        out = os.path.join(OUT, os.path.splitext(base)[0] + ".json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
        md, st = d["metadata"], d["stats"]
        flat = list(_flatten(d["sections"]))
        warns = d.get("warnings", [])
        nums = [s["number"] for s in d["sections"] if s["number"] and s["number"].isdigit()]
        dup = sorted({n for n in nums if nums.count(n) > 1})
        disorder = [int(n) for n in nums] != sorted(int(n) for n in nums)
        from_registry = not any("из титульного листа" in w or "реестр недоступен" in w
                                 for w in warns)
        return {
            "file": base, "ok": True, "secs": time.time() - t0,
            "id": md.get("id"), "title_ok": bool(md.get("title")),
            "year": md.get("year"), "age": md.get("age_group"),
            "mkb_n": len(md.get("mkb_codes") or []),
            "from_registry": from_registry,
            "sections": st["sections_found"], "tables": st["tables_found"],
            "coverage": st["coverage_percent"],
            "top_numbers": _top_numbers(d["sections"]),
            "n_top": len(d["sections"]),
            "has4": _has_section4(d["sections"]),
            "toc_leak": _toc_leak(flat),
            "dup": dup,
            "disorder": disorder,
            "warns": len(warns),
        }
    except Exception as exc:
        return {"file": base, "ok": False, "secs": time.time() - t0, "error": repr(exc)}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    os.makedirs(OUT, exist_ok=True)
    registry = glob.glob("*.xlsx")[0]

    pdfs = [p for p in sorted(glob.glob(os.path.join(DATA, "*.pdf")))
            if os.path.basename(p).startswith("КР")]
    mode = sys.argv[3] if len(sys.argv) > 3 else "head"
    if mode == "spread" and len(pdfs) > n:
        step = len(pdfs) / n
        pdfs = [pdfs[int(i * step)] for i in range(n)]
    else:
        pdfs = pdfs[:n]
    print("Файлов к обработке: %d, воркеров: %d, реестр: %s" % (len(pdfs), workers, registry))

    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                             initargs=(registry,)) as ex:
        for i, r in enumerate(ex.map(work, pdfs), 1):
            results.append(r)
            if r["ok"]:
                flag = ""
                if not r["has4"]: flag += " NO_SEC4"
                if r["toc_leak"]: flag += " TOC_LEAK"
                if r["coverage"] < 80: flag += " LOWCOV"
                if not r["from_registry"]: flag += " FALLBACK"
                if not r["title_ok"]: flag += " NOTITLE"
                print("  [%3d/%3d] %-16s id=%-7s sec=%-3d tab=%-3d cov=%5.1f%% top=%s%s" % (
                    i, len(pdfs), r["file"], r["id"], r["sections"], r["tables"],
                    r["coverage"], "".join(str(x) for x in r["top_numbers"])[:24], flag))
            else:
                print("  [%3d/%3d] %-16s ОШИБКА: %s" % (i, len(pdfs), r["file"], r["error"]))

    dt = time.time() - t0
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]

    print("\n" + "="*70)
    print("ИТОГИ (%d файлов за %.1f c, %.2f c/файл)" % (len(results), dt, dt/max(1,len(results))))
    print("  успешно: %d, ошибок: %d" % (len(ok), len(bad)))
    if ok:
        import statistics as S
        covs = [r["coverage"] for r in ok]
        print("  покрытие: среднее %.1f%%, медиана %.1f%%, мин %.1f%%, <80%%: %d, <50%%: %d" % (
            S.mean(covs), S.median(covs), min(covs),
            sum(c < 80 for c in covs), sum(c < 50 for c in covs)))
        print("  метаданные из реестра: %d, из титула (fallback): %d" % (
            sum(r["from_registry"] for r in ok), sum(not r["from_registry"] for r in ok)))
        print("  название извлечено: %d/%d" % (sum(r["title_ok"] for r in ok), len(ok)))
        print("  МКБ извлечён (>=1): %d/%d" % (sum(r["mkb_n"] > 0 for r in ok), len(ok)))
        print("  есть раздел 4 (реабилитация): %d/%d" % (sum(r["has4"] for r in ok), len(ok)))
        print("  ToC-утечка в разделы: %d" % sum(r["toc_leak"] for r in ok))
        print("  ДУБЛИ номеров верхнего уровня: %d" % sum(1 for r in ok if r["dup"]))
        print("  НАРУШЕН порядок номеров: %d" % sum(1 for r in ok if r["disorder"]))
        print("  таблиц найдено всего: %d (файлов с таблицами: %d)" % (
            sum(r["tables"] for r in ok), sum(r["tables"] > 0 for r in ok)))
        print("  разделов: среднее %.1f, мин %d, макс %d" % (
            S.mean([r["sections"] for r in ok]),
            min(r["sections"] for r in ok), max(r["sections"] for r in ok)))

    # списки проблемных файлов
    no4 = [r["file"] for r in ok if not r["has4"]]
    leak = [r["file"] for r in ok if r["toc_leak"]]
    low = [(r["file"], r["coverage"]) for r in ok if r["coverage"] < 80]
    fb = [r["file"] for r in ok if not r["from_registry"]]
    if no4: print("\n  БЕЗ РАЗДЕЛА 4 (%d): %s" % (len(no4), no4[:25]))
    if leak: print("\n  TOC-УТЕЧКА (%d): %s" % (len(leak), leak[:25]))
    if low: print("\n  НИЗКОЕ ПОКРЫТИЕ <80%% (%d): %s" % (len(low), low[:25]))
    dups = [(r["file"], r["dup"]) for r in ok if r["dup"]]
    diso = [r["file"] for r in ok if r["disorder"]]
    if dups: print("\n  ДУБЛИ (%d): %s" % (len(dups), dups[:25]))
    if diso: print("\n  НАРУШЕН ПОРЯДОК (%d): %s" % (len(diso), diso[:25]))
    if fb: print("\n  FALLBACK на титул (%d): %s" % (len(fb), fb[:25]))
    if bad: print("\n  ОШИБКИ (%d): %s" % (len(bad), [(r["file"], r["error"]) for r in bad][:15]))

    # сохраним сырые результаты для последующего анализа
    with open("_stage2_results.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
