# -*- coding: utf-8 -*-
"""
Манифест прогона: хеши входов/выходов/кода/окружения + сверка дрейфа.

Инструмент делает состояние корпуса ВОСПРОИЗВОДИМО ИЗМЕРИМЫМ. Он НЕ участвует в
парсинге и не влияет на выход: не импортируется из parse(), ничего не перезапускает,
снимает состояние КАК ЕСТЬ вместе со всеми дефектами.

    python -m crparser.engine.manifest --snapshot   # снять baseline_manifest.json
    python -m crparser.engine.manifest --verify      # сверить текущее состояние
    python -m crparser.engine.manifest --drift        # отчёт «выход новее отчёта»

--verify: exit 0 только при полном совпадении с манифестом, иначе 1.

Манифест — источник истины о том, каким кодом получены 722 JSON в outout/. Пока его
нет, любая правка неизмерима: нельзя отличить «мой фикс изменил файл» от «файл и так
был другой».
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

# Пути корпуса (относительно корня проекта). Совпадают с batch_report.py.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW_DIR = os.path.join(_ROOT, "data", "raw")
OUT_DIR = os.path.join(_ROOT, "outout")
REPORT = os.path.join(_ROOT, "_corpus", "report.json")
PINS = os.path.join(_ROOT, "crparser", "data", "ocr_pins.json")
MANIFEST = os.path.join(_ROOT, "_corpus", "baseline_manifest.json")

_CHUNK = 1 << 20


# --------------------------------------------------------------------------- #
# Базовые хеши                                                                 #
# --------------------------------------------------------------------------- #
def sha256_file(path: str) -> str:
    """sha256 файла по частям (не грузим весь файл в память)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Отпечаток кода                                                              #
# --------------------------------------------------------------------------- #
def _git(*args: str):
    try:
        out = subprocess.run(["git", "-C", _ROOT, *args],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 — git может отсутствовать
        return None


def _code_files() -> list:
    """Отсортированный список путей: все *.py в crparser/ + validate.py + batch_report.py."""
    files = sorted(glob.glob(os.path.join(_ROOT, "crparser", "**", "*.py"),
                             recursive=True))
    for extra in ("validate.py", "batch_report.py"):
        p = os.path.join(_ROOT, extra)
        if os.path.exists(p):
            files.append(p)
    return files


def code_fingerprint() -> dict:
    """{git_sha, git_dirty, tree_sha256}.

    tree_sha256 фиксирует код ДАЖЕ при грязном рабочем дереве: он считается от
    отсортированного списка (относительный путь, sha256) исходников, а не от git.
    """
    dirty = _git("status", "--porcelain")
    parts = []
    for path in _code_files():
        rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
        parts.append("%s:%s" % (rel, sha256_file(path)))
    tree = _sha256_bytes("\n".join(parts).encode("utf-8"))
    return {
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": bool(dirty) if dirty is not None else None,
        "tree_sha256": tree,
    }


# --------------------------------------------------------------------------- #
# Отпечаток окружения                                                        #
# --------------------------------------------------------------------------- #
def _pkg_version(mod_name: str, attr: str = "__version__"):
    try:
        mod = __import__(mod_name)
        return getattr(mod, attr, None)
    except Exception:  # noqa: BLE001
        return None


def _fitz_version():
    try:
        import fitz
        vb = getattr(fitz, "VersionBind", None)
        return vb or _pkg_version("fitz")
    except Exception:  # noqa: BLE001
        return None


def _tesseract_info() -> dict:
    """Первая строка `tesseract --version` + sha256 rus/eng traineddata, если найдены."""
    info = {"version": None, "traineddata": {}}
    try:
        from crparser.engine.ocr import _resolve_tesseract
        cmd = _resolve_tesseract()
    except Exception:  # noqa: BLE001
        cmd = None
    if cmd:
        try:
            out = subprocess.run([cmd, "--version"], capture_output=True,
                                 text=True, timeout=30)
            first = (out.stdout or out.stderr or "").splitlines()
            info["version"] = first[0].strip() if first else None
        except Exception:  # noqa: BLE001
            pass
    # traineddata: TESSDATA_PREFIX -> рядом с бинарём -> ../share/tessdata
    dirs = []
    if os.environ.get("TESSDATA_PREFIX"):
        dirs.append(os.environ["TESSDATA_PREFIX"])
    if cmd:
        bindir = os.path.dirname(cmd)
        dirs.append(os.path.join(bindir, "tessdata"))
        dirs.append(os.path.join(os.path.dirname(bindir), "share", "tessdata"))
    for lang in ("rus", "eng"):
        for d in dirs:
            p = os.path.join(d, lang + ".traineddata")
            if os.path.exists(p):
                info["traineddata"][lang] = {
                    "path": p, "sha256": sha256_file(p),
                    "bytes": os.path.getsize(p)}
                break
    return info


def env_fingerprint() -> dict:
    """Версии инструментов стека. Недоступное — None, не падать."""
    return {
        "python": sys.version.split()[0],
        "pymupdf": _fitz_version(),
        "pdfplumber": _pkg_version("pdfplumber"),
        "pillow": _pkg_version("PIL", "__version__"),
        "pytesseract": _pkg_version("pytesseract"),
        "fonttools": _pkg_version("fontTools", "version"),
        "tesseract": _tesseract_info(),
    }


# --------------------------------------------------------------------------- #
# Конфиг пайплайна (читается из модулей, без дублирования значений)          #
# --------------------------------------------------------------------------- #
def _config() -> dict:
    cfg = {"ocr_dpi": None, "max_workers": None, "cov_fail": None, "cov_warn": None}
    try:
        from crparser.engine import ocr
        cfg["ocr_dpi"] = getattr(ocr, "_DEFAULT_DPI", None)
    except Exception:  # noqa: BLE001
        pass
    try:
        import validate
        cfg["cov_fail"] = getattr(validate, "COV_FAIL", None)
        cfg["cov_warn"] = getattr(validate, "COV_WARN", None)
    except Exception:  # noqa: BLE001
        pass
    # max_workers — теперь CLI-параметр --workers (промпт 05), не константа. Читаем
    # его default из исходника; форма default=min(os.cpu_count() or N, M).
    try:
        import re
        src = open(os.path.join(_ROOT, "batch_report.py"), encoding="utf-8").read()
        m = re.search(r"--workers.*?default=min\(os\.cpu_count\(\)\s*or\s*\d+,\s*(\d+)\)",
                      src, re.DOTALL)
        if m:
            cfg["max_workers"] = "min(cpu,%s)" % m.group(1)
        else:
            m2 = re.search(r"max_workers\s*=\s*(\d+)", src)
            if m2:
                cfg["max_workers"] = int(m2.group(1))
    except Exception:  # noqa: BLE001
        pass
    return cfg


# --------------------------------------------------------------------------- #
# Сборка манифеста                                                           #
# --------------------------------------------------------------------------- #
def _page_count(path: str):
    try:
        import fitz
        with fitz.open(path) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001
        return None


def _report_counts() -> dict:
    """PASS/FAIL/REVIEW из report.json (если есть)."""
    counts = {}
    if os.path.exists(REPORT):
        try:
            data = json.load(open(REPORT, encoding="utf-8"))
            if isinstance(data, list):
                from collections import Counter
                c = Counter(r.get("status") for r in data)
                counts = {"pass": c.get("PASS", 0), "fail": c.get("FAIL", 0),
                          "review": c.get("REVIEW", 0)}
        except Exception:  # noqa: BLE001
            pass
    return counts


def build_manifest(raw_dir: str = RAW_DIR, out_dir: str = OUT_DIR) -> dict:
    """Снять снимок текущего состояния корпуса (без перезапуска парсинга)."""
    inputs = {}
    total_pages = 0
    pages_known = True
    for pdf in sorted(glob.glob(os.path.join(raw_dir, "*.pdf"))):
        base = os.path.splitext(os.path.basename(pdf))[0]
        pages = _page_count(pdf)
        if pages is None:
            pages_known = False
        else:
            total_pages += pages
        inputs[base] = {"sha256": sha256_file(pdf),
                        "bytes": os.path.getsize(pdf), "pages": pages}

    outputs = {}
    for jf in sorted(glob.glob(os.path.join(out_dir, "*.json"))):
        base = os.path.splitext(os.path.basename(jf))[0]
        st = os.stat(jf)
        outputs[base] = {
            "sha256": sha256_file(jf), "bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()}

    rc = _report_counts()
    counts = {"inputs": len(inputs), "outputs": len(outputs),
              "pages_total": total_pages if pages_known else None, **rc}

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "code": code_fingerprint(),
        "env": env_fingerprint(),
        "config": _config(),
        "pins_sha256": sha256_file(PINS) if os.path.exists(PINS) else None,
        "registry_sha256": _registry_sha256(),
        "inputs": inputs,
        "outputs": outputs,
        "report_sha256": sha256_file(REPORT) if os.path.exists(REPORT) else None,
        "counts": counts,
        "shipped": _shipped_facts(),
    }
    return manifest


def _registry_sha256():
    xlsx = sorted(glob.glob(os.path.join(_ROOT, "*.xlsx")))
    return sha256_file(xlsx[0]) if xlsx else None


def _shipped_facts() -> dict:
    """Факт по уже отгруженным папкам — находка, не ошибка инструмента.

    Сколько файлов реально лежит в outout_pass/ и outout_extra_usable/ и совпадает
    ли множество outout_pass/ с множеством PASS из report.json.
    """
    facts = {}
    pass_dir = os.path.join(_ROOT, "outout_pass")
    extra_dir = os.path.join(_ROOT, "outout_extra_usable")
    pass_files = {os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(pass_dir, "*.json"))}
    extra_files = {os.path.splitext(os.path.basename(p))[0]
                   for p in glob.glob(os.path.join(extra_dir, "*.json"))}
    facts["outout_pass_count"] = len(pass_files)
    facts["outout_extra_usable_count"] = len(extra_files)
    # сверка с множеством PASS
    report_pass = set()
    review_in_shipped = []
    if os.path.exists(REPORT):
        try:
            data = json.load(open(REPORT, encoding="utf-8"))
            report_pass = {r["file"] for r in data if r.get("status") == "PASS"}
            status_by = {r["file"]: r.get("status") for r in data}
            review_in_shipped = sorted(f for f in pass_files
                                       if status_by.get(f) and status_by[f] != "PASS")
        except Exception:  # noqa: BLE001
            pass
    facts["pass_in_report"] = len(report_pass)
    facts["pass_missing_from_shipped"] = sorted(report_pass - pass_files)
    facts["shipped_not_pass"] = review_in_shipped  # напр. КР875_1 при статусе REVIEW
    facts["shipped_matches_pass_set"] = (pass_files == report_pass)
    return facts


# --------------------------------------------------------------------------- #
# Команды CLI                                                                #
# --------------------------------------------------------------------------- #
def _atomic_write_json(obj, path: str) -> None:
    """Атомарная запись (temp+fsync+os.replace): без торн/«Extra data» при прерывании
    или конкурентной записи — тот же класс, что уже починен в batch_report/revalidate."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def cmd_snapshot() -> int:
    manifest = build_manifest()
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    _atomic_write_json(manifest, MANIFEST)
    c = manifest["counts"]
    print("baseline_manifest.json записан -> %s" % os.path.relpath(MANIFEST, _ROOT))
    print("  входов: %d, выходов: %d" % (c["inputs"], c["outputs"]))
    print("  страниц по корпусу: %s" % c.get("pages_total"))
    print("  PASS/FAIL/REVIEW: %s/%s/%s"
          % (c.get("pass"), c.get("fail"), c.get("review")))
    sh = manifest["shipped"]
    print("  outout_pass/: %d файлов (PASS в report.json: %d, совпадает: %s)"
          % (sh["outout_pass_count"], sh["pass_in_report"],
             sh["shipped_matches_pass_set"]))
    if sh["shipped_not_pass"]:
        print("  ФАКТ: в outout_pass/ лежат не-PASS файлы: %s"
              % ", ".join(sh["shipped_not_pass"]))
    if sh["pass_missing_from_shipped"]:
        print("  ФАКТ: %d PASS-файлов отсутствуют в outout_pass/"
              % len(sh["pass_missing_from_shipped"]))
    return 0


def _load_manifest() -> dict:
    if not os.path.exists(MANIFEST):
        print("манифест не найден: %s (сначала --snapshot)" % MANIFEST)
        raise SystemExit(2)
    return json.load(open(MANIFEST, encoding="utf-8"))


def _diff_group(name: str, old: dict, new: dict, field: str = "sha256"):
    """Возвращает (changed, missing, added) по группе файлов."""
    old_keys, new_keys = set(old), set(new)
    changed = sorted(k for k in old_keys & new_keys
                     if old[k].get(field) != new[k].get(field))
    missing = sorted(old_keys - new_keys)
    added = sorted(new_keys - old_keys)
    return changed, missing, added


def cmd_verify() -> int:
    base = _load_manifest()
    cur = build_manifest()
    diverged = False

    for grp in ("inputs", "outputs"):
        changed, missing, added = _diff_group(grp, base[grp], cur[grp])
        if changed or missing or added:
            diverged = True
            print("[%s] изменились: %d, пропали: %d, добавились: %d"
                  % (grp, len(changed), len(missing), len(added)))
            for k in changed[:50]:
                print("    ~ %s" % k)
            for k in missing[:50]:
                print("    - %s" % k)
            for k in added[:50]:
                print("    + %s" % k)
        else:
            print("[%s] расхождений нет (%d файлов)" % (grp, len(cur[grp])))

    # код
    if base["code"].get("tree_sha256") != cur["code"].get("tree_sha256"):
        diverged = True
        print("[код] tree_sha256 изменился: %s -> %s"
              % (base["code"].get("tree_sha256", "")[:12],
                 cur["code"].get("tree_sha256", "")[:12]))
    if base["code"].get("git_sha") != cur["code"].get("git_sha"):
        print("[код] git_sha изменился: %s -> %s"
              % (base["code"].get("git_sha"), cur["code"].get("git_sha")))

    # pins / registry
    for key, label in (("pins_sha256", "pins"), ("registry_sha256", "registry")):
        if base.get(key) != cur.get(key):
            diverged = True
            print("[%s] изменился: %s -> %s" % (label, base.get(key), cur.get(key)))

    # env — поэлементно. Смена версии Tesseract/traineddata/Pillow/PyMuPDF ломает
    # воспроизводимость OCR, ради которой манифест и делался, поэтому это FAIL сверки.
    # ВАЖНО: путь traineddata (path) машинозависим — сравниваем ТОЛЬКО sha256 содержимого.
    if _diff_env(base.get("env", {}), cur.get("env", {})):
        diverged = True

    # config — ocr_dpi, max_workers, cov_fail, cov_warn.
    b_cfg, c_cfg = base.get("config", {}) or {}, cur.get("config", {}) or {}
    for k in ("ocr_dpi", "max_workers", "cov_fail", "cov_warn"):
        if b_cfg.get(k) != c_cfg.get(k):
            diverged = True
            print("[config] %s: %s -> %s" % (k, b_cfg.get(k), c_cfg.get(k)))

    # report_sha256
    if base.get("report_sha256") != cur.get("report_sha256"):
        diverged = True
        print("[report] report_sha256: %s -> %s"
              % (base.get("report_sha256"), cur.get("report_sha256")))

    # counts — inputs, outputs, pages_total, pass, fail, review.
    b_cnt, c_cnt = base.get("counts", {}) or {}, cur.get("counts", {}) or {}
    for k in ("inputs", "outputs", "pages_total", "pass", "fail", "review"):
        if b_cnt.get(k) != c_cnt.get(k):
            diverged = True
            print("[counts] %s: %s -> %s" % (k, b_cnt.get(k), c_cnt.get(k)))

    if diverged:
        print("\nРАСХОЖДЕНИЯ ЕСТЬ.")
        return 1
    print("\nрасхождений нет.")
    return 0


def _diff_env(base_env: dict, cur_env: dict) -> bool:
    """Поэлементная сверка окружения. traineddata сравнивается по sha256, не по пути."""
    diverged = False
    for k in ("python", "pymupdf", "pdfplumber", "pillow", "pytesseract", "fonttools"):
        if base_env.get(k) != cur_env.get(k):
            diverged = True
            print("[env] %s: %s -> %s" % (k, base_env.get(k), cur_env.get(k)))
    b_t = base_env.get("tesseract", {}) or {}
    c_t = cur_env.get("tesseract", {}) or {}
    if b_t.get("version") != c_t.get("version"):
        diverged = True
        print("[env] tesseract.version: %s -> %s"
              % (b_t.get("version"), c_t.get("version")))
    b_td = b_t.get("traineddata", {}) or {}
    c_td = c_t.get("traineddata", {}) or {}
    for lang in sorted(set(b_td) | set(c_td)):
        b_sha = (b_td.get(lang) or {}).get("sha256")
        c_sha = (c_td.get(lang) or {}).get("sha256")
        if b_sha != c_sha:
            diverged = True
            print("[env] tesseract.traineddata.%s.sha256: %s -> %s"
                  % (lang, b_sha, c_sha))
    return diverged


def cmd_drift() -> int:
    """Выход новее отчёта — ровно тот дефект, что нашёл аудит (КР1_4, КР628_2)."""
    if not os.path.exists(REPORT):
        print("report.json не найден: %s" % REPORT)
        return 2
    report_mtime = os.path.getmtime(REPORT)
    data = json.load(open(REPORT, encoding="utf-8"))
    files = [r["file"] for r in data] if isinstance(data, list) else []
    newer, absent = [], []
    for base in files:
        jf = os.path.join(OUT_DIR, base + ".json")
        if not os.path.exists(jf):
            absent.append(base)
            continue
        if os.path.getmtime(jf) > report_mtime:
            newer.append(base)
    print("report.json mtime: %s"
          % datetime.fromtimestamp(report_mtime, timezone.utc).isoformat())
    print("\nвыход новее отчёта (%d):" % len(newer))
    for b in sorted(newer):
        print("    %s" % b)
    if absent:
        print("\nв отчёте есть, файла в outout/ нет (%d): %s"
              % (len(absent), ", ".join(sorted(absent)[:50])))
    return 0


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    if "--snapshot" in argv:
        return cmd_snapshot()
    if "--verify" in argv:
        return cmd_verify()
    if "--drift" in argv:
        return cmd_drift()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
