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

def test_D8_kr396_ocr_required(validate_base):
    """ЗАКРЫТ промптом 04 (гейт OCR_REQUIRED): КР396_4 (glyph_tokens=1) больше не PASS."""
    rep = validate_base("КР396_4")
    assert rep.status != "PASS"
    assert any("OCR_REQUIRED" in r for r in rep.reviews)


def test_D9_kr1_4_residual_pin_fixed_by_09(outout_doc):
    """ЗАКРЫТ иначе, чем ждали: остаточные пины (Вагсе1опа/ВСЬС) сидели в OVER-CAPTURE-
    кропе Table 1 (pdfplumber-дамп битого слоя). Промпт 09 (ПРАВКА 4: raw_text из IR)
    берёт ВОССТАНОВЛЕННЫЙ гибрид-OCR текст тела (Barcelona/BCLC), а не битый крап —
    остаточных пинов в теле больше НЕТ, КР1_4 -> PASS. Это и есть цель ПРАВКИ 4."""
    doc = outout_doc("КР1_4")

    def _walk(secs):
        for s in secs:
            yield s
            yield from _walk(s.get("children", []))
    body = " ".join(
        [(t.get("raw_text") or "") for t in doc["tables"]]
        + [(s.get("text") or "") for s in _walk(doc["sections"])])
    # битые формы ушли, восстановленные — на месте (взяты из IR, а не из крапа)
    assert "Вагсе1опа" not in body and "ВСЬС" not in body, "битые пины остались"
    assert "Barcelona" in body and "BCLC" in body, "восстановленных форм нет"


# ============================================================================
# ГРУППА B — coverage v2 (промпт 10): D10 xfail
# ============================================================================

def test_D10_structured_coverage(make_doc):
    """ЗАКРЫТ промптом 10: 1 символ в sections + 99 в excluded: coverage_percent=100
    выдавал провал за успех; теперь structured_coverage ~1% обнажает дефект, а
    source_retention=100% подтверждает, что физически ничего не потеряно."""
    doc = make_doc(
        sections=[{"number": "1", "title": "", "text": "x", "level": 1, "children": []}],
        excluded={"other": [{"title": "", "text": "y" * 99}]},
        total_chars=100)
    v2 = doc["stats"].get("coverage_v2")
    assert v2 is not None
    assert v2["structured_coverage"] < 0.1
    assert v2["source_retention"] >= 0.99


# ============================================================================
# ГРУППА C — таблицы (промпт 09): xfail (репродукция по структуре кода)
# ============================================================================

def test_D12_table_fallback_is_document_level():
    """ЗАКРЫТ 09: whitespace-детектор больше НЕ под document-level `if not tables:` —
    он пер-страничный (ансамбль), безрамочные не теряются рядом с рамочными."""
    src = _src(os.path.join("crparser", "engine", "tables.py"))
    assert "if not tables:" not in src, "fallback всё ещё document-level"


def test_D14_subtraction_before_reconcile():
    """ЗАКРЫТ 09: вычитание (subtraction_map) — ТОЛЬКО после reconcile (score>=порог),
    не по факту эмиссии."""
    src = _src(os.path.join("crparser", "engine", "tables.py"))
    assert "reconcil" in src.lower(), "нет reconcile-гейта перед вычитанием"


def test_D13_multipage_table_continuation():
    """ЗАКРЫТ 09: строки продолжения многостраничной таблицы связаны через
    continues_table (КР1_4 Table 2 стр.63-64)."""
    src = _src(os.path.join("crparser", "engine", "tables.py"))
    assert "continues_table" in src


# ============================================================================
# ГРУППА D — provenance (промпт 08): D15 xfail
# ============================================================================

def test_D15_section_has_provenance(outout_doc):
    """ЗАКРЫТ промптом 08: Section несёт section_id/page/bbox/span_uids (провенанс).
    Редакция 2 промпта переименовала line_ids -> span_uids (геометрический якорь)."""
    doc = outout_doc("КР1000_1")
    s = doc["sections"][0]
    assert "section_id" in s and "page" in s and "bbox" in s and "span_uids" in s


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

class _FakeWriter:
    def __init__(self, dump=None):
        self._dump = dump
    def to_dict(self, result):
        return {"ok": 1}
    def _atomic_dump(self, doc, path):
        if self._dump:
            self._dump(doc, path)


def test_D19_failed_write_breaks_integrity(monkeypatch, tmp_path):
    """ЗАКРЫТ промптом 05 (in-process, БЕЗ env-хуков в боевом воркере): сбой записи ->
    статус FAIL(WRITE_FAILED), write_ok=False, и compute_integrity даёт нарушение
    целостности (=> run_integrity_ok=False, публикации нет, exit 2)."""
    import batch_report as B

    def _raise(doc, path):
        raise OSError("disk full (тест)")
    monkeypatch.setattr(B, "_PARSER", type("P", (), {"parse": lambda s, p: object()})())
    monkeypatch.setattr(B, "_WRITER", _FakeWriter(dump=_raise))
    monkeypatch.setattr(B, "_CLASS", {})
    monkeypatch.setattr(B, "_STAGING", str(tmp_path))
    monkeypatch.setattr(B, "RAW", str(tmp_path))
    r = B.work("КР_X")
    assert r["status"] == "FAIL" and r["write_ok"] is False
    assert any("WRITE_FAILED" in f for f in r["fails"])
    fails = B.compute_integrity({"КР_X": r}, ["КР_X"], "a", "a", [])
    assert any("WRITE_FAILED" in f for f in fails)


def test_D20_crash_removes_stale(monkeypatch, tmp_path):
    """ЗАКРЫТ промптом 05: CRASH -> статус FAIL(CRASH), и publish удаляет устаревший
    JSON упавшего документа (staging-выхода нет)."""
    import batch_report as B
    monkeypatch.setattr(B, "_PARSER",
                        type("P", (), {"parse": lambda s, p: (_ for _ in ()).throw(ValueError("bad"))})())
    monkeypatch.setattr(B, "_WRITER", _FakeWriter())
    monkeypatch.setattr(B, "_CLASS", {})
    monkeypatch.setattr(B, "_STAGING", str(tmp_path / "staging"))
    monkeypatch.setattr(B, "RAW", str(tmp_path))
    r = B.work("КР_C")
    assert r["crash"] is True and r["status"] == "FAIL"
    out = tmp_path / "out"
    out.mkdir()
    (out / "КР_C.json").write_text('{"stale":1}', encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    monkeypatch.setattr(B, "OUTOUT", str(out))
    B.publish(str(staging), ["КР_C"], {"КР_C": r}, full_run=False)
    assert not (out / "КР_C.json").exists()          # устаревший удалён при публикации


def test_D_pins_change_breaks_integrity():
    """Изменение ocr_pins.json в ходе прогона (before != after) -> нарушение целостности
    (тестируется чистой функцией, без порчи боевой базы)."""
    import batch_report as B
    fails = B.compute_integrity({"КР_X": {"write_ok": True}}, ["КР_X"], "sha_before", "sha_AFTER", [])
    assert any("PINS_CHANGED" in f for f in fails)


# ============================================================================
# ГРУППА G — скрытая связанность (промпт 11): D21 xfail
# ============================================================================

def test_D21_heading_order_valid_is_called():
    """ЗАКРЫТ промптом 11b: сегментер вызывает NumberingPolicy.heading_order_valid
    (монотонность номеров). Нарушение не отбрасывает раздел молча — пишет warning."""
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
