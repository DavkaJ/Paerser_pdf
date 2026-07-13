# -*- coding: utf-8 -*-
"""
Карантин по причинам + release-экспортёр с контрактом обучающего корпуса (промпт 06).

FAIL/REVIEW/SKIP физически НЕ МОГУТ попасть в обучающий корпус. Экспорт для обучения —
отдельный артефакт с явным машиночитаемым контрактом и собственным манифестом. Экспортёр
НЕ «чинит» данные — он только фильтрует по статусу и физически ВЫРЕЗАЕТ запрещённые зоны.

Директория до промпта 14 (gold set) называется candidate_release/ — не release/:
ни один документ не аттестован, и называть его релизом = повторить ошибку, из-за которой
596 непроверенных файлов уехали к коллегам как готовые.

    python release.py --run <run_id>     # экспорт из манифеста прогона (строгая проверка)
    python release.py --current          # из текущего report.json + outout/ (без свежего прогона)
    python release.py --sample <id>      # стратифицированная выборка на ручную проверку
    python release.py --recall-notice    # RECALL_NOTICE.md по уже отгруженному
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTOUT = os.environ.get("CR_OUTOUT", os.path.join(ROOT, "outout"))
REPORT = os.environ.get("CR_REPORT", os.path.join(ROOT, "_corpus", "report.json"))
RUNS_DIR = os.path.join(ROOT, "_runs")
CONTRACT = os.path.join(ROOT, "crparser", "data", "training_contract.json")
CAND_DIR = os.path.join(ROOT, "candidate_release")
QUAR_DIR = os.path.join(ROOT, "quarantine")

# Приоритет причины для раскладки по одной папке (severity: FAIL > REVIEW). Файл с
# несколькими причинами кладётся в папку ПЕРВОЙ по этому списку; UNVERIFIABLE_OCR —
# особая обязательная категория выше всех (данные, о качестве которых судить нельзя).
_KIND_PRIORITY = [
    "UNVERIFIABLE_OCR",
    # FAIL-уровень
    "CRASH", "WRITE_FAILED", "TIMEOUT", "SCHEMA", "STATS_MISMATCH", "COVERAGE_INVALID",
    "CANONICAL_RECALL", "MISSING", "HIERARCHY_BROKEN", "TITLE_BLEED", "DUPLICATE",
    "PHANTOM_SECTION", "COVERAGE",
    # REVIEW-уровень
    "OCR_REQUIRED", "COLLAPSE", "RESIDUAL_PIN", "TABLES_MISSING", "OVERCOUNT",
    "TOC_NONE", "NO_SECTIONS", "EMPTY_OUTPUT", "CORRUPTION",
]


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_contract():
    return json.load(open(CONTRACT, encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Источник статусов: run_manifest ИЛИ текущий report.json                      #
# --------------------------------------------------------------------------- #
def _kinds_of(item):
    fk = list(item.get("fail_kinds", []))
    rk = sorted({x.split(":", 1)[0] for x in item.get("reviews", [])})
    return fk + rk


def _is_unverifiable_ocr(item):
    """Скан, восстановленный ПОЛНЫМ OCR, чей parse_toc не извлекает оглавление
    (TOC_NONE): парсер и валидатор смотрят на разные документы — структура не
    проверена против источника ни разу (промпт 06 ЧАСТЬ 1)."""
    kinds = _kinds_of(item)
    return item.get("cls") == "SKIPPED_SCAN" and "TOC_NONE" in kinds


def load_source(run_id, use_current):
    """Вернуть (label, items, integrity_ok, integrity_note, run_meta).
    items: {base: {status, cls, kinds, unverifiable}}."""
    if use_current:
        report = json.load(open(REPORT, encoding="utf-8"))
        items = {}
        for r in report:
            items[r["file"]] = {
                "status": r["status"], "cls": r.get("cls", "OK"),
                "kinds": _kinds_of(r), "unverifiable": _is_unverifiable_ocr(r)}
        note = ("источник — ТЕКУЩИЙ published outout/ + report.json, не свежий "
                "верифицированный прогон (run_integrity формально не проверялся)")
        return "current", items, True, note, None
    # --- run_manifest ---
    rm_path = os.path.join(RUNS_DIR, run_id, "run_manifest.json")
    if not os.path.isfile(rm_path):
        print("ОТКАЗ: нет run_manifest %s" % rm_path)
        raise SystemExit(2)
    rm = json.load(open(rm_path, encoding="utf-8"))
    if not rm.get("run_integrity_ok"):
        print("ОТКАЗ: run_integrity_ok=false — прогон не целостен, экспорта нет.")
        print("  причины:", rm.get("integrity_failures"))
        raise SystemExit(2)
    if rm.get("require_ocr") is False:
        print("ОТКАЗ: прогон сделан с --no-require-ocr — OCR не гарантирован, "
              "release такой прогон не пускает.")
        raise SystemExit(2)
    items = {}
    for base, d in rm.get("documents", {}).items():
        # для kinds берём из report.json (в манифесте только сводка)
        items[base] = {"status": d.get("status"), "cls": "OK",
                       "kinds": d.get("kinds", []), "unverifiable": False}
    # обогатить cls/unverifiable из report.json, если он есть
    if os.path.isfile(REPORT):
        by = {r["file"]: r for r in json.load(open(REPORT, encoding="utf-8"))}
        for base in items:
            r = by.get(base)
            if r:
                items[base]["cls"] = r.get("cls", "OK")
                items[base]["kinds"] = _kinds_of(r)
                items[base]["unverifiable"] = _is_unverifiable_ocr(r)
    return run_id, items, True, None, rm


# --------------------------------------------------------------------------- #
# Карантин                                                                    #
# --------------------------------------------------------------------------- #
def _primary_kind(item):
    if item.get("unverifiable"):
        return "UNVERIFIABLE_OCR"
    kinds = item.get("kinds", [])
    for k in _KIND_PRIORITY:
        if k in kinds:
            return k
    if item["status"] == "SKIP":
        return "SKIP"
    return "OTHER"


def layout_quarantine(label, items, run_id):
    if os.path.isdir(QUAR_DIR):
        shutil.rmtree(QUAR_DIR)
    os.makedirs(QUAR_DIR, exist_ok=True)
    index = {}
    counts = {}
    for base, item in sorted(items.items()):
        if item["status"] == "PASS" and not item.get("unverifiable"):
            continue
        kind = _primary_kind(item)
        dst_dir = os.path.join(QUAR_DIR, kind)
        os.makedirs(dst_dir, exist_ok=True)
        src = os.path.join(OUTOUT, base + ".json")
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(dst_dir, base + ".json"))
        index[base] = {"status": item["status"], "kinds": item.get("kinds", []),
                       "primary": kind, "unverifiable_ocr": item.get("unverifiable", False),
                       "first_seen_run": run_id}
        counts[kind] = counts.get(kind, 0) + 1
    with open(os.path.join(QUAR_DIR, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)
    print("КАРАНТИН: %d не-PASS документов по папкам-причинам:" % len(index))
    for k in sorted(counts, key=lambda k: -counts[k]):
        print("   %-18s %d" % (k, counts[k]))
    return index, counts


# --------------------------------------------------------------------------- #
# Экспорт по контракту                                                        #
# --------------------------------------------------------------------------- #
def cut_by_contract(doc, contract=None):
    """Физически вырезать запрещённые зоны. Остаётся metadata/sections/tables +
    excluded.appendices (клинически ценные шкалы/критерии/алгоритмы).

    ПОЛЯ провенанса промпта 08 (span_uids/bbox/page/section_id/claimed_span_uids/
    source) и top-level `provenance` — ДИАГНОСТИКА, не обучающий контент: их
    вырезаем по contract['exclude_fields'] (top-level provenance выпадает сам —
    в выход он не копируется)."""
    ef = (contract or _load_contract()).get("exclude_fields", {})
    sec_drop = set(ef.get("section", []))
    tbl_drop = set(ef.get("table", []))
    exc_drop = set(ef.get("excluded_item", []))

    def cut_section(s):
        o = {k: v for k, v in s.items() if k not in sec_drop}
        if "children" in o:
            o["children"] = [cut_section(c) for c in o.get("children") or []]
        return o

    out = {"metadata": doc.get("metadata", {}),
           "sections": [cut_section(s) for s in doc.get("sections", [])],
           "tables": [{k: v for k, v in t.items() if k not in tbl_drop}
                      for t in doc.get("tables", [])]}
    app = (doc.get("excluded", {}) or {}).get("appendices")
    if app:
        out["excluded"] = {"appendices": [
            {k: v for k, v in i.items() if k not in exc_drop} for i in app]}
    return out


def _corruption_zero(doc):
    cor = (doc.get("stats", {}) or {}).get("corruption", {}) or {}
    return (int(cor.get("pseudo_ascii_tokens", 0)) == 0
            and int(cor.get("glyph_tokens", 0)) == 0)


def export(label, items, run_meta, as_release):
    contract = _load_contract()
    attested = bool(contract.get("require_thresholds", {}).get("passed"))  # False до 14
    dir_name = "release" if as_release else "candidate_release"
    if as_release and not attested:
        print("ОТКАЗ: назвать директорию release/ нельзя — корпус НЕ аттестован против "
              "gold set (промпт 14 не выполнен). Используйте candidate_release/.")
        raise SystemExit(2)
    out_root = os.path.join(ROOT, dir_name, label)
    if os.path.isdir(out_root):
        shutil.rmtree(out_root)
    os.makedirs(out_root, exist_ok=True)

    exported, skipped_corruption, skipped_unverifiable = [], [], []
    file_sha = {}
    for base, item in sorted(items.items()):
        if item["status"] not in contract["require_status"]:
            continue
        if item.get("unverifiable"):          # никогда не в обучение (промпт 06)
            skipped_unverifiable.append(base)
            continue
        src = os.path.join(OUTOUT, base + ".json")
        if not os.path.exists(src):
            continue
        doc = json.load(open(src, encoding="utf-8"))
        if not _corruption_zero(doc):         # двойная страховка поверх гейта 04
            skipped_corruption.append(base)
            continue
        cut = cut_by_contract(doc, contract)
        dst = os.path.join(out_root, base + ".json")
        with open(dst, "w", encoding="utf-8") as fh:
            json.dump(cut, fh, ensure_ascii=False, indent=1)
        exported.append(base)
        file_sha[base] = _sha256_file(dst)

    from_current = run_meta is None
    manifest = {
        "run_id": label, "generated_from": ("current_state" if from_current else "run_manifest"),
        "not_for_distribution": from_current,   # --current: незамороженное состояние
        "contract_version": contract["version"], "attested": attested,
        "disclaimer": (("НЕ ДЛЯ ОТГРУЗКИ. Собрано из НЕЗАМОРОЖЕННОГО состояния "
                        "(--current), не из верифицированного прогона. Только оценка "
                        "готовности. " if from_current else "")
                       + "Корпус НЕ аттестован против gold set (промпт 14 не выполнен). "
                       "Числовых порогов качества нет. Использовать как КАНДИДАТ, не релиз."),
        "count": len(exported),
        "excluded_zones": contract["exclude"],
        "included_zones": contract["include"],
        "files": {b: file_sha[b] for b in exported},
        "skipped_corruption": sorted(skipped_corruption),
        "skipped_unverifiable_ocr": sorted(skipped_unverifiable),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if run_meta:
        manifest["code"] = run_meta.get("code")
        manifest["env"] = run_meta.get("env")
    with open(os.path.join(out_root, "RELEASE_MANIFEST.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    _write_readme(out_root, contract, manifest)
    print("\nЭКСПОРТ (%s): %d документов -> %s/" % (dir_name, len(exported), out_root))
    print("  пропущено из-за порчи (require_zero): %d" % len(skipped_corruption))
    print("  пропущено UNVERIFIABLE_OCR: %d" % len(skipped_unverifiable))
    print("  ПЕРВОЕ ЧЕСТНОЕ ЧИСЛО ГОТОВНОСТИ: %d документов (attested=%s)"
          % (len(exported), attested))
    return manifest


def _write_readme(out_root, contract, manifest):
    lines = [
        "# Обучающий корпус КР — %s (attested: %s)" % (manifest["run_id"], manifest["attested"]),
        "",
        "**ВНИМАНИЕ.** %s" % manifest["disclaimer"],
        "",
        "## Что внутри",
        "%d документов со статусом PASS, у каждого вырезаны запрещённые контрактом зоны."
        % manifest["count"],
        "",
        "## Что ВКЛЮЧЕНО (%s)" % ", ".join(contract["include"]),
        "- metadata — реестровые метаданные",
        "- sections — дерево разделов",
        "- tables — извлечённые таблицы",
        "- excluded.appendices — приложения (шкалы Child-Pugh/ECOG, критерии, алгоритмы)",
        "",
        "## Что ВЫРЕЗАНО и почему",
    ]
    for zone, why in contract["rationale"].items():
        if zone in contract["exclude"]:
            lines.append("- **%s** — %s" % (zone, why))
    lines += [
        "",
        "## Чего НЕЛЬЗЯ делать с этими данными",
        "- Обучать на них как на аттестованном корпусе — он НЕ проверен против gold set.",
        "- Использовать вырезанные зоны (references/toc/front_matter/other) — их тут нет.",
        "- Считать отсутствие ошибок доказанным — числовых порогов качества пока нет.",
        "",
        "## Известные остаточные дефекты",
        "- Таблицы могут быть усечены (см. гейт TABLE_STUB/TABLES_MISSING).",
        "- Возможна остаточная кирилло-латинская порча в теле (гейт RESIDUAL_PIN ловит явные).",
    ]
    with open(os.path.join(out_root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Ретро-уведомление по уже отгруженному                                       #
# --------------------------------------------------------------------------- #
def recall_notice():
    report = json.load(open(REPORT, encoding="utf-8"))
    status_by = {r["file"]: r for r in report}
    pass_now = {r["file"] for r in report if r["status"] == "PASS"}
    shipped_pass = {os.path.splitext(os.path.basename(p))[0]
                    for p in glob.glob(os.path.join(ROOT, "outout_pass", "*.json"))}
    shipped_extra = {os.path.splitext(os.path.basename(p))[0]
                     for p in glob.glob(os.path.join(ROOT, "outout_extra_usable", "*.json"))}
    no_longer_pass = sorted(b for b in shipped_pass
                            if status_by.get(b, {}).get("status") != "PASS")
    pass_missing = sorted(pass_now - shipped_pass)
    shipped_not_pass = sorted(b for b in shipped_pass
                              if status_by.get(b, {}).get("status") not in (None, "PASS"))
    L = ["# RECALL NOTICE — отгруженный корпус требует пересмотра",
         "",
         "Сформировано по ФАКТУ (чтение outout_pass/ + report.json), не по памяти. "
         "Дата: %s." % datetime.now(timezone.utc).strftime("%Y-%m-%d"),
         "",
         "## Факты по выгрузке",
         "- `outout_pass/`: **%d** файлов (не 616 — не равно множеству PASS)." % len(shipped_pass),
         "- `outout_extra_usable/`: **%d** файлов." % len(shipped_extra),
         "- Текущее множество PASS в report.json: **%d**." % len(pass_now),
         "",
         "## 1. Отгруженные файлы, БОЛЬШЕ не PASS по новым гейтам (03+04)",
         "Всего: **%d**. Эти файлы нельзя использовать как готовые:" % len(no_longer_pass),
         ""]
    for b in no_longer_pass:
        r = status_by.get(b, {})
        L.append("  - %s -> %s (%s)" % (b, r.get("status"),
                                        ", ".join(_kinds_of(r)) or "—"))
    # Рассинхрон, ЗАМОРОЖЕННЫЙ на baseline (промпт 01): 21 PASS-файл, которого не было
    # в outout_pass/ на момент 616 PASS (КР401_2/205_2/408_2/571_2/…). Это исторический
    # факт неизвестного прогона; часть из них с тех пор перестала быть PASS.
    frozen_missing = []
    bm = os.path.join(ROOT, "_corpus", "baseline_manifest.json")
    if os.path.isfile(bm):
        try:
            sh = json.load(open(bm, encoding="utf-8")).get("shipped", {})
            frozen_missing = sh.get("pass_missing_from_shipped", [])
        except Exception:  # noqa: BLE001
            pass
    L += ["",
          "## 2. Рассинхрон самой выгрузки (доказательство неизвестного прогона)",
          "- PASS-файлов (на момент baseline, 616 PASS), которых НЕ было в outout_pass/: "
          "**%d** — %s" % (len(frozen_missing), ", ".join(sorted(frozen_missing)[:40])),
          "- PASS-файлов (текущих, 572 PASS), которых НЕТ в outout_pass/: **%d** — %s"
          % (len(pass_missing), ", ".join(pass_missing[:40]) or "—"),
          "- В outout_pass/ лежат файлы со статусом НЕ PASS: **%d** — %s"
          % (len(shipped_not_pass), ", ".join(shipped_not_pass) or "—"),
          "  (в т.ч. КР875_1 при статусе REVIEW — выгрузка сделана от ДРУГОГО прогона).",
          "",
          "## 3. excluded.references использовать НЕЛЬЗЯ",
          "Отгруженные JSON содержат `excluded.references` с exact-token attestation "
          "43-50% (замер аудита). По новому контракту обучения (training_contract.json) "
          "эта зона ИСКЛЮЧЕНА. Коллеги не должны обучать на ней.",
          "",
          "## Что делать",
          "Пересобрать корпус детерминированным экспортёром `release.py --run <run_id>` "
          "от верифицированного прогона (промпт 05) и раздать `candidate_release/`, не "
          "ручную выгрузку. Директория остаётся `candidate_release/` до аттестации на "
          "gold set (промпт 14).",
          ""]
    dst = os.path.join(ROOT, "_corpus", "RECALL_NOTICE.md")
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("RECALL_NOTICE.md -> %s" % os.path.relpath(dst, ROOT))
    print("  отгружено PASS-папкой: %d; больше не PASS: %d; PASS без выгрузки: %d; "
          "не-PASS в выгрузке: %d"
          % (len(shipped_pass), len(no_longer_pass), len(pass_missing), len(shipped_not_pass)))


# --------------------------------------------------------------------------- #
# Ручная выборка (гейт 2-ter)                                                 #
# --------------------------------------------------------------------------- #
def build_sample(label, items, out_root):
    """Стратифицированная выборка PASS (30) + все boundary cases -> _manual_review/."""
    review_dir = os.path.join(out_root, "_manual_review")
    os.makedirs(review_dir, exist_ok=True)
    passing = sorted(b for b, it in items.items()
                     if it["status"] == "PASS" and not it.get("unverifiable"))
    # детерминированная выборка 30 (шаг по отсортированному списку)
    n = min(30, len(passing))
    step = max(1, len(passing) // n) if n else 1
    sample = passing[::step][:n]
    checklist = [
        "# REVIEW_CHECKLIST — ручная проверка выборки (гейт §6.4 п.6 аудита)",
        "",
        "Проверить КАЖДЫЙ документ выборки по пунктам. Найденный дефект = гейт пропустил "
        "класс ошибок => чинить ГЕЙТ, а не выкидывать документ.",
        "",
        "## Пункты проверки",
        "- [ ] структура разделов соответствует оглавлению PDF",
        "- [ ] таблицы: числа в ячейках верны, ничего не усечено",
        "- [ ] коды МКБ / TNM / ATC — точная форма (D37.6 не 037.6, T1b не Т1Ь)",
        "- [ ] дозировки и единицы — точная форма (6 мес. не б мес.)",
        "- [ ] нет кирилло-латинской порчи в теле разделов",
        "",
        "## Документы выборки (%d): %s" % (len(sample), ", ".join(sample)),
        "",
        "Вердикт писать в `_manual_review/verdict.json`:",
        '`{"reviewed_by":"...","reviewed_at":"...","documents":{"КРxxx":"ok"|"defect: ..."}}`',
        "",
    ]
    with open(os.path.join(review_dir, "REVIEW_CHECKLIST.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(checklist) + "\n")
    for b in sample:
        src = os.path.join(OUTOUT, b + ".json")
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(review_dir, b + ".json"))
    print("ВЫБОРКА НА РУЧНУЮ ПРОВЕРКУ: %d документов -> %s"
          % (len(sample), os.path.relpath(review_dir, ROOT)))
    print("  экспортёр ОТКАЖЕТСЯ собрать релиз без _manual_review/verdict.json без дефектов.")
    return sample


def check_verdict(out_root, sample):
    """Финальный релиз запрещён без чистого verdict.json, покрывающего всю выборку."""
    vp = os.path.join(out_root, "_manual_review", "verdict.json")
    if not os.path.isfile(vp):
        return False, "нет _manual_review/verdict.json"
    v = json.load(open(vp, encoding="utf-8"))
    docs = v.get("documents", {})
    missing = [b for b in sample if b not in docs]
    if missing:
        return False, "вердикт не покрывает: %s" % ", ".join(missing[:10])
    defects = {b: r for b, r in docs.items() if str(r).lower().startswith("defect")}
    if defects:
        return False, "найдены дефекты (гейт пропустил класс ошибок): %s" % defects
    return True, "ok"


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Карантин + release-экспортёр КР.")
    ap.add_argument("--run", help="run_id из _runs/")
    ap.add_argument("--current", action="store_true",
                    help="из НЕЗАМОРОЖЕННОГО report.json + outout/ (только оценка, НЕ отгрузка). "
                         "Требует --i-know-this-is-unfrozen.")
    ap.add_argument("--i-know-this-is-unfrozen", dest="unfrozen_ack", action="store_true",
                    help="подтвердить, что --current собран из незамороженного состояния и "
                         "НЕПРИГОДЕН к отгрузке (именно так родился рассинхрон 596 файлов)")
    ap.add_argument("--sample", action="store_true", help="сформировать выборку на ручную проверку")
    ap.add_argument("--recall-notice", action="store_true")
    ap.add_argument("--as-release", action="store_true",
                    help="назвать директорию release/ (откажет до аттестации на gold set)")
    ap.add_argument("--finalize", action="store_true",
                    help="проверить verdict.json перед финальным релизом")
    args = ap.parse_args()

    if args.recall_notice:
        recall_notice()
        return 0
    if not args.run and not args.current:
        print("нужен --run <run_id> или --current (или --recall-notice)")
        return 2
    if args.current and not args.unfrozen_ack:
        print("ОТКАЗ: --current собирает из НЕЗАМОРОЖЕННОГО состояния (не свежий "
              "верифицированный прогон) — это механизм, породивший рассинхрон 596 "
              "файлов. Для оценки готовности добавьте --i-know-this-is-unfrozen; для "
              "отгрузки используйте --run <run_id> от полного прогона (промпт 05).")
        return 2

    label, items, integrity_ok, note, run_meta = load_source(args.run, args.current)
    if args.current:
        label = "current_UNFROZEN"      # имя кричит: это не отгружаемый релиз
    if note:
        print("ПРИМЕЧАНИЕ: %s" % note)
    layout_quarantine(label, items, label)
    manifest = export(label, items, run_meta, args.as_release)
    out_root = os.path.join(ROOT, "candidate_release", label)
    if args.sample:
        sample = build_sample(label, items, out_root)
        if args.finalize:
            ok, why = check_verdict(out_root, sample)
            print("ФИНАЛИЗАЦИЯ: %s (%s)" % ("разрешена" if ok else "ОТКАЗАНА", why))
            if not ok:
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
