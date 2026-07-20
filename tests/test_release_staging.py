# -*- coding: utf-8 -*-
"""Release-экспорт из ЗАМОРОЖЕННОГО staging прогона со сверкой output_sha256 (I30 #7).

Дефект: `release.py --run` проверял run_integrity_ok/require_ocr в манифесте, НО читал
контент из ИЗМЕНЯЕМОГО outout/ и не сверял output_sha256 каждого файла — остаточная форма
«публикации от неизвестного прогона» (промпт 05, I9). Здесь: экспорт берёт файлы из
_runs/<run_id>/staging и сверяет их sha с манифестом; дрейф -> отказ (exit 2).
"""
import hashlib
import json
import os

import pytest

import release as R


CONTRACT = {
    "version": "test-contract-1",
    "require_status": ["PASS"],
    "require_thresholds": {"passed": False},        # не аттестован -> candidate_release/
    "exclude_fields": {"section": ["span_uids"], "table": [], "excluded_item": []},
    "exclude": ["references"],
    "include": ["metadata", "sections", "tables"],
    "rationale": {"references": "attestation 43-50%"},
}


def _doc(marker):
    """Минимальный PASS-JSON: проходит cut_by_contract + _corruption_zero."""
    return {
        "metadata": {"source_file": "x.pdf", "document_type": "КР", "marker": marker},
        "sections": [{"number": "1", "title": "Раздел", "text": marker,
                      "level": 1, "children": [], "span_uids": ["p1_abc"]}],
        "tables": [], "excluded": {},
        "stats": {"corruption": {"pseudo_ascii_tokens": 0, "glyph_tokens": 0}},
    }


def _sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _make_run(tmp, rid, docs):
    """docs: {base: doc}. Пишет staging/*.json + run_manifest.json (sha по staging)."""
    staging = os.path.join(str(tmp), "_runs", rid, "staging")
    os.makedirs(staging, exist_ok=True)
    mdocs = {}
    for base, doc in docs.items():
        raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        with open(os.path.join(staging, base + ".json"), "wb") as fh:
            fh.write(raw)
        mdocs[base] = {"status": "PASS", "output_sha256": _sha_bytes(raw), "kinds": []}
    rm = {"run_id": rid, "run_integrity_ok": True, "require_ocr": True, "documents": mdocs}
    with open(os.path.join(str(tmp), "_runs", rid, "run_manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(rm, fh, ensure_ascii=False)
    return staging


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Перенаправить release на tmp: ROOT/RUNS_DIR/OUTOUT/REPORT + контракт."""
    monkeypatch.setattr(R, "ROOT", str(tmp_path))
    monkeypatch.setattr(R, "RUNS_DIR", os.path.join(str(tmp_path), "_runs"))
    monkeypatch.setattr(R, "OUTOUT", os.path.join(str(tmp_path), "outout"))
    monkeypatch.setattr(R, "REPORT", os.path.join(str(tmp_path), "nope_report.json"))
    monkeypatch.setattr(R, "_load_contract", lambda: CONTRACT)
    os.makedirs(os.path.join(str(tmp_path), "outout"), exist_ok=True)
    return tmp_path


def test_run_export_reads_staging_not_outout(wired):
    """Ключевой пин: под --run контент берётся из staging, а НЕ из изменяемого outout/.
    outout/ намеренно испорчен (другое содержимое) — экспорт обязан взять staging."""
    rid = "20260720T000000Z_test"
    staging = _make_run(wired, rid, {"КР_X": _doc("STAGING_TRUTH")})
    # outout/ дрейфанул после прогона — НЕ должен попасть в экспорт
    with open(os.path.join(str(wired), "outout", "КР_X.json"), "w", encoding="utf-8") as fh:
        json.dump(_doc("OUTOUT_DRIFT"), fh, ensure_ascii=False)

    label, items, ok, note, run_meta = R.load_source(rid, False)
    R.export(label, items, run_meta, as_release=False)

    out = os.path.join(str(wired), "candidate_release", rid, "КР_X.json")
    assert os.path.exists(out)
    got = json.load(open(out, encoding="utf-8"))
    # взято из staging (STAGING_TRUTH), не из outout (OUTOUT_DRIFT)
    assert got["metadata"]["marker"] == "STAGING_TRUTH"
    assert got["sections"][0]["text"] == "STAGING_TRUTH"
    # provenance-поле вырезано контрактом
    assert "span_uids" not in got["sections"][0]
    assert staging  # staging реально построен


def test_run_export_refuses_on_staging_drift(wired):
    """Подмена одного байта в staging-файле -> output_sha256 не сходится -> отказ (exit 2)."""
    rid = "20260720T000001Z_test"
    staging = _make_run(wired, rid, {"КР_X": _doc("ok")})
    label, items, ok, note, run_meta = R.load_source(rid, False)
    # дрейф: дописать байт в staging после снятия манифеста
    with open(os.path.join(staging, "КР_X.json"), "ab") as fh:
        fh.write(b" ")

    with pytest.raises(SystemExit) as e:
        R.export(label, items, run_meta, as_release=False)
    assert e.value.code == 2
    # экспорт НЕ создан
    assert not os.path.exists(os.path.join(str(wired), "candidate_release", rid, "КР_X.json"))


def test_run_export_refuses_missing_staging_file(wired):
    """Манифест числит PASS-док, которого нет в staging -> прогон неполон -> отказ (exit 2)."""
    rid = "20260720T000002Z_test"
    _make_run(wired, rid, {"КР_X": _doc("ok")})
    os.remove(os.path.join(str(wired), "_runs", rid, "staging", "КР_X.json"))
    label, items, ok, note, run_meta = R.load_source(rid, False)
    with pytest.raises(SystemExit) as e:
        R.export(label, items, run_meta, as_release=False)
    assert e.value.code == 2
