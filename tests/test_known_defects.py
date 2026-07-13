# -*- coding: utf-8 -*-
"""
Репродукция известных дефектов аудита (промпт 07a) + анти-регресс.

Правило: закрытый дефект -> зелёный регресс-страж; открытый -> xfail(strict=True)
с номером промпта-владельца. strict=True: если открытый дефект внезапно исчезнет,
тест станет XPASS и упадёт — заметим.

СОСТОЯНИЕ НА МОМЕНТ НАПИСАНИЯ: выполнены промпты 02 (пины/кэш) и 03 (валидатор
fail-closed: схема/exit/stats/римское оглавление). Поэтому D1-D7, D11, D16-D18
уже ЗАКРЫТЫ (зелёные); D8-D10, D12-D15, D19-D21 ещё открыты (xfail).
Продуктовый код этим промптом НЕ трогается.
"""
import json
import os

import pytest

import validate as V
from crparser.engine import ocr_pins

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = None
for _c in (os.path.join(ROOT, "Список_утвержденных_клинических_рекомендаций_2.xlsx"),):
    if os.path.exists(_c):
        REG = _c


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ============================================================================
# ГРУППА A — валидатор (промпт 03): ЗАКРЫТЫ, зелёные
# ============================================================================

def test_D1_empty_doc_is_schema_fail():
    """Пустой {} -> SCHEMA FAIL (было: SKIP + exit 0)."""
    rep = V.validate_doc("empty.pdf", {}, None)
    assert rep.status == "FAIL"
    assert any(f.startswith("SCHEMA") for f in rep.fails)


def test_D2_report_ok_excludes_reviews():
    """Report.ok игнорировал reviews. Теперь REVIEW -> ok=False."""
    rep = V.Report("x")
    rep.review("SOME", "порча")
    assert rep.ok is False
    assert rep.status == "REVIEW"


def test_D3_cli_exit_nonzero_on_review(validate_base):
    """CLI: файл со статусом REVIEW -> ненулевой exit (было: 0)."""
    if not os.path.exists(os.path.join(ROOT, "outout", "КР401_2.json")):
        pytest.skip("нет фикстуры КР401_2")
    code = V.main([os.path.join(ROOT, "data", "raw", "КР401_2.pdf"),
                   "--json", os.path.join(ROOT, "outout", "КР401_2.json"),
                   "--registry", REG or ""])
    assert code != 0


def test_D4_forged_stats_detected(make_doc):
    """Подделанный coverage_percent=100 при фактических ~84 -> STATS_MISMATCH FAIL."""
    doc = make_doc(total_chars=500)            # факт: 418/500 = 83.6%
    assert doc["stats"]["coverage_percent"] < 90
    doc["stats"]["coverage_percent"] = 100.0   # враньё
    rep = V.validate_doc("forged.pdf", doc, None)
    assert rep.status == "FAIL"
    assert any("STATS_MISMATCH" in f for f in rep.fails)


def test_D5_roman_toc_numbered():
    """Оглавление с римскими номерами: numbered() их видит (было: [])."""
    toc = V.Toc([(None, "XII. Критерии оценки качества"),
                 (None, "XIII. Список литературы"),
                 ("1.1", "Определение")], body_norm="")
    nums = [n for n, _ in toc.numbered()]
    assert "XII" in nums and "XIII" in nums and "1.1" in nums


def test_D6_kr848_not_pass(validate_base):
    """КР848_1: развалившаяся структура больше не PASS (римское MISSING + OVERCOUNT)."""
    rep = validate_base("КР848_1")
    assert rep.status != "PASS"


def test_D7_kr401_not_pass(validate_base):
    """КР401_2: 3 секции при total 127к больше не PASS."""
    rep = validate_base("КР401_2")
    assert rep.status != "PASS"


def test_D11_kr848_overcount_detected(outout_doc):
    """КР848_1: сумма учтённого > источника -> OVERCOUNT (было: обрезано min())."""
    doc = outout_doc("КР848_1")
    rep = V.Report("КР848_1")
    V._check_stats_and_coverage(rep, doc, doc.get("stats", {}))
    assert any("OVERCOUNT" in r for r in rep.reviews)


# ============================================================================
# ГРУППА A' — валидатор-гейты, ещё ОТКРЫТЫ (промпт 04): xfail
# ============================================================================

@pytest.mark.xfail(strict=True, reason="дефект аудита §3.3; гейт OCR_REQUIRED — промпт 04")
def test_D8_kr396_ocr_required(validate_base):
    """КР396_4 PASS при glyph_tokens=1 — должен стать REVIEW (OCR_REQUIRED)."""
    rep = validate_base("КР396_4")
    assert rep.status != "PASS"


@pytest.mark.xfail(strict=True, reason="дефект аудита §3.5 P0-5; гейт RESIDUAL_PIN — промпт 04")
def test_D9_kr1_4_residual_pin(validate_base, outout_doc):
    """КР1_4 PASS при остаточных пинах в ТЕЛЕ (tables: Вагсе1опа/ВСЬС/1луег)."""
    doc = outout_doc("КР1_4")
    base = ocr_pins.load_base()
    punct = ".,;:()[]«»\"'-—%<>±*"
    tbl = " ".join((t.get("raw_text") or "") + " " + (t.get("caption") or "")
                   for t in doc["tables"])
    hits = [tok.strip(punct).lower() for tok in tbl.split()
            if len(tok.strip(punct)) >= 3 and tok.strip(punct).lower() in base]
    assert hits, "предусловие: у КР1_4 есть остаточные пины в таблицах"
    rep = validate_base("КР1_4")
    assert rep.status != "PASS"    # ждём REVIEW после гейта 04


# ============================================================================
# ГРУППА B — coverage v2 (промпт 10): D10 xfail
# ============================================================================

@pytest.mark.xfail(strict=True, reason="дефект аудита §1.1 P0-2; coverage v2 — промпт 10")
def test_D10_structured_coverage_absent(make_doc):
    """1 символ в sections + 99 в excluded: source_retention=100%, но structured_coverage
    ~1% — метрики пока НЕТ (единственная coverage_percent=100 выдаёт провал за успех)."""
    doc = make_doc(
        sections=[{"number": "1", "title": "", "text": "x", "level": 1, "children": []}],
        excluded={"other": [{"title": "", "text": "y" * 99}]},
        total_chars=100)
    assert "coverage_v2" in doc["stats"] and \
        doc["stats"]["coverage_v2"]["structured_coverage"] < 0.1


# ============================================================================
# ГРУППА C — таблицы (промпт 09): xfail (репродукция по структуре кода)
# ============================================================================

@pytest.mark.xfail(strict=True, reason="дефект аудита §1.1 P0-3; постраничный fallback — промпт 09")
def test_D12_table_fallback_is_document_level():
    """whitespace-fallback стоит под document-level `if not tables:` -> безрамочные
    таблицы теряются в документах с хоть одной рамочной. Фикс 09 делает его постраничным."""
    src = _src(os.path.join("crparser", "engine", "tables.py"))
    assert "if not tables:" not in src, "fallback всё ещё document-level"


@pytest.mark.xfail(strict=True, reason="дефект аудита §1.1 P0-3; reconcile перед вычитанием — промпт 09")
def test_D14_subtraction_before_reconcile():
    """bbox таблицы попадает в subtraction_map независимо от того, что вернул дамп ->
    текст исчезает из тела. Фикс 09 вводит reconcile перед вычитанием."""
    src = _src(os.path.join("crparser", "engine", "tables.py"))
    assert "reconcil" in src.lower(), "нет reconcile-гейта перед вычитанием"


@pytest.mark.xfail(strict=True, reason="дефект аудита §4; многостраничные продолжения — промпт 09")
def test_D13_multipage_table_continuation():
    """Строки продолжения многостраничной таблицы теряются: нет связи continues_table."""
    src = _src(os.path.join("crparser", "engine", "tables.py"))
    assert "continues_table" in src


# ============================================================================
# ГРУППА D — provenance (промпт 08): D15 xfail
# ============================================================================

@pytest.mark.xfail(strict=True, reason="дефект аудита §1.3; provenance — промпт 08")
def test_D15_section_has_provenance(outout_doc):
    """Section не имеет section_id/page/bbox/line_ids, хотя профиль их вычисляет."""
    doc = outout_doc("КР1000_1")
    s = doc["sections"][0]
    assert "section_id" in s and "page" in s and "line_ids" in s


# ============================================================================
# ГРУППА E — пины/кэш (промпт 02): ЗАКРЫТЫ, зелёные
# ============================================================================

def test_D16_run_does_not_mutate_pins(tmp_path):
    """Сбор предложения новых пар НЕ пишет в ocr_pins.json (было: merge дописывал)."""
    import hashlib
    p = os.path.join(ROOT, "crparser", "data", "ocr_pins.json")
    before = hashlib.sha256(open(p, "rb").read()).hexdigest()
    ocr_pins.build_proposal()          # читает шарды, базу НЕ трогает
    after = hashlib.sha256(open(p, "rb").read()).hexdigest()
    assert before == after


def test_D17_cache_key_depends_on_pdf_content():
    """Ключ OCR-кэша зависит от содержимого PDF: разные sha -> разные пути."""
    from crparser.engine import ocr
    r = ocr.OcrRecoverer()
    r._cmd = None
    r._pdf_sha = "a" * 64
    pa = r._cache_path(1, 6)
    r._pdf_sha = "b" * 64
    pb = r._cache_path(1, 6)
    assert pa is not None and pa != pb


def test_D18_no_PDI_target_in_base():
    """В активной базе пинов нет цели `PDI` (был конфликт с PD1/PD-L1)."""
    m = ocr_pins.load_base()
    assert not any("PDI" in v for v in m.values())
    raw = _src(os.path.join("crparser", "data", "ocr_pins.json"))
    assert '"PDI"' not in raw


# ============================================================================
# ГРУППА F — батч-транзакционность (промпт 05): xfail
# ============================================================================

@pytest.mark.xfail(strict=True, reason="дефект аудита §1.1 P0-1; staging+allowlist — промпт 05")
def test_D19_failed_write_forces_fail():
    """Сбой записи только ставит write_ok=False, статус не форсится в FAIL. Фикс 05 — WRITE_FAILED."""
    src = _src("batch_report.py")
    assert "WRITE_FAILED" in src


@pytest.mark.xfail(strict=True, reason="дефект аудита §1.1 P0-1; транзакционная публикация — промпт 05")
def test_D20_crash_does_not_survive_publish():
    """CRASH не инвалидирует старый JSON. Фикс 05 — staging + публикация по allowlist."""
    src = _src("batch_report.py")
    assert "staging" in src


# ============================================================================
# ГРУППА G — скрытая связанность (промпт 11): D21 xfail
# ============================================================================

@pytest.mark.xfail(strict=True, reason="дефект аудита §1.2; heading_order_valid не вызывается — промпт 11")
def test_D21_heading_order_valid_is_called():
    """heading_order_valid объявлен в base.py, но не вызывается из движка."""
    import glob
    calls = 0
    for f in glob.glob(os.path.join(ROOT, "crparser", "engine", "*.py")):
        src = open(f, encoding="utf-8").read()
        calls += src.count("heading_order_valid(")
    assert calls > 0


# ============================================================================
# АНТИ-РЕГРЕСС G1-G3 — обязаны быть зелёными НАВСЕГДА
# ============================================================================

@pytest.mark.parametrize("base", ["КР802_1", "КР845_1", "КР901_1", "КР1000_1"])
def test_G1_healthy_stay_pass(validate_base, base):
    """Здоровые эталоны остаются PASS. Гейты не топят здоровое."""
    rep = validate_base(base)
    assert rep.status == "PASS", "%s: %s / %s" % (base, rep.fails, rep.reviews)


def test_G2_kr1000_clean_latin_not_flagged(outout_doc, validate_base):
    """КР1000_1: легитимная латиница, 0 §-токенов — ни один детектор порчи не трогает."""
    doc = outout_doc("КР1000_1")
    cor = doc["stats"].get("corruption", {})
    assert cor.get("glyph_tokens", 0) == 0
    assert cor.get("pseudo_ascii_tokens", 0) == 0
    rep = validate_base("КР1000_1")
    assert rep.status == "PASS"


def test_G3_all_outout_valid_json():
    """Все JSON в outout/ синтаксически валидны и несут обязательные ключи."""
    import glob
    files = glob.glob(os.path.join(ROOT, "outout", "*.json"))
    if not files:
        pytest.skip("нет outout/")
    need = {"metadata", "sections", "tables", "excluded", "stats"}
    bad = []
    for jf in files:
        try:
            d = json.load(open(jf, encoding="utf-8"))
            if not need <= set(d):
                bad.append(os.path.basename(jf))
        except Exception as exc:  # noqa: BLE001
            bad.append("%s (%s)" % (os.path.basename(jf), exc))
    assert not bad, "невалидные/неполные: %s" % bad[:10]
