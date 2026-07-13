# -*- coding: utf-8 -*-
"""
Пины провенанса (промпт 08): аддитивность, иммутабельность, геометрический якорь,
инвариант владения, расхождения каналов.

OCR отключён на уровне conftest, поэтому:
  * аддитивность и владение проверяются на OCR-АГНОСТИЧНЫХ файлах (КР1000_1) и по
    геометрии (КР628_2) — они одинаковы с OCR и без;
  * расхождения native/OCR (КР1_4) требуют OCR-провенанса — берём из outout/
    (перепарсен батчем с OCR); при отсутствии — skip.
"""
import glob
import json
import os

import pytest

from conftest import ROOT, RAW, has_fixture, load_outout

REG = None
for _c in glob.glob(os.path.join(ROOT, "*.xlsx")):
    REG = _c
    break


# ---- strip_new_fields: обратный срез всех полей промпта 08 -------------------

def _strip_section(s):
    return {"number": s["number"], "title": s["title"], "level": s["level"],
            "text": s["text"], "children": [_strip_section(c) for c in s["children"]]}


def _strip_table(t):
    out = {"page": t["page"], "number": t["number"], "caption": t["caption"],
           "raw_text": t["raw_text"], "bbox": t["bbox"]}
    if t.get("low_confidence"):
        out["low_confidence"] = True
    return out


def strip_new_fields(doc):
    """Убрать поля 08 (section_id/page/bbox/span_uids, claimed_span_uids/source,
    excluded.span_uids), блок provenance и новые provenance-warnings."""
    return {
        "metadata": doc["metadata"],
        "sections": [_strip_section(s) for s in doc["sections"]],
        "tables": [_strip_table(t) for t in doc["tables"]],
        "excluded": {b: [{"title": i["title"], "text": i["text"]} for i in items]
                     for b, items in doc["excluded"].items()},
        "stats": doc["stats"],
        "warnings": [w for w in doc["warnings"]
                     if not w.startswith("provenance:")],
    }


def _dumps(o):
    return json.dumps(o, ensure_ascii=False, indent=2)


@pytest.fixture(scope="module")
def cr_parser():
    from crparser.engine.parser import DocumentParser
    from crparser.profiles import create_profile
    return DocumentParser(create_profile("cr", REG))


def _parse_doc(cr_parser, base):
    from crparser.engine.jsonio import JsonWriter
    return JsonWriter().to_dict(cr_parser.parse(os.path.join(RAW, base + ".pdf")))


# ---- геометрический якорь ----------------------------------------------------

def test_span_uid_geometric_stable():
    """span_uid детерминирован, зависит ТОЛЬКО от (page, bbox) и несёт номер страницы."""
    from crparser.engine.models import span_uid
    a = span_uid(3, (10.0, 20.0, 30.0, 40.0))
    assert a == span_uid(3, (10.0, 20.0, 30.0, 40.0))   # стабилен
    assert a.startswith("p3_")                          # номер страницы в id
    assert a != span_uid(3, (10.1, 20.0, 30.0, 40.0))   # иная геометрия — иной id
    assert a != span_uid(4, (10.0, 20.0, 30.0, 40.0))   # иная страница — иной id


# ---- глубокая иммутабельность PageIR ----------------------------------------

def test_pageir_deep_immutable(cr_parser):
    """frozen=True сам вложенное не морозит — проверяем proxy/tuple явно."""
    if not has_fixture("КР1000_1"):
        pytest.skip("нет фикстуры КР1000_1")
    res = cr_parser.parse(os.path.join(RAW, "КР1000_1.pdf"))
    assert res.page_ir, "PageIR не построен"
    pir = next(p for p in res.page_ir if p.spans)
    with pytest.raises(Exception):
        pir.diagnostics["k"] = 1              # MappingProxyType
    with pytest.raises(Exception):
        pir.spans[0].candidates["native"] = "x"
    with pytest.raises(Exception):
        pir.spans = ()                        # frozen dataclass
    with pytest.raises(Exception):
        pir.spans[0].selected = "ocr"


# ---- аддитивность: strip == baseline (форма без полей 08) --------------------

def test_additive_strip_matches_outout(cr_parser):
    """strip_new_fields(перепарс) совпадает со strip_new_fields(outout) БАЙТ-В-БАЙТ.
    КР1000_1 OCR-агностичен, поэтому OCR-off перепарс равен OCR-on выгрузке."""
    if not has_fixture("КР1000_1"):
        pytest.skip("нет фикстуры КР1000_1")
    doc = _parse_doc(cr_parser, "КР1000_1")
    baseline = strip_new_fields(load_outout("КР1000_1"))
    assert _dumps(strip_new_fields(doc)) == _dumps(baseline)
    # и провенанс реально присутствует (не пустой срез)
    s0 = doc["sections"][0]
    assert {"section_id", "page", "bbox", "span_uids"} <= set(s0)
    assert "provenance" in doc and doc["provenance"]["spans_total"] > 0


# ---- инвариант владения: КР628_2 обязан иметь дубли (аудит §4.2) -------------

def test_kr628_2_ownership_duplicates(cr_parser):
    """«Критерии качества» лежат и в sections[6].text, и в tables[1]/[2] — один и
    тот же span заявлен дважды. Геометрия одинакова с OCR и без -> проверяем перепарс."""
    if not has_fixture("КР628_2"):
        pytest.skip("нет фикстуры КР628_2")
    doc = _parse_doc(cr_parser, "КР628_2")
    dup = [w for w in doc["warnings"]
           if w.startswith("provenance:") and "заявлены более" in w]
    assert dup, "КР628_2 обязан иметь дубли владения — иначе span_uids собраны неверно"
    # инвариант не должен бросать исключение (документ распарсился целиком)
    assert doc["sections"] and doc["provenance"]["spans_total"] > 0


def test_ownership_warnings_never_trigger_ocr_gate(cr_parser):
    """provenance-warnings НЕ содержат подстроки 'ocr' (иначе валидатор поднимет
    OCR_REQUIRED и сдвинет статус)."""
    if not has_fixture("КР628_2"):
        pytest.skip("нет фикстуры КР628_2")
    doc = _parse_doc(cr_parser, "КР628_2")
    bad = [w for w in doc["warnings"]
           if w.startswith("provenance:") and "ocr" in w.lower()]
    assert not bad, "provenance-warning с подстрокой 'ocr': %s" % bad


# ---- расхождения каналов: КР1_4 native/OCR (нужен OCR-провенанс) -------------

def test_kr1_4_disagreements_present():
    """provenance.disagreements для КР1_4 непуст и содержит пару «Вагсе1опа»/«Barcelona».
    Требует OCR-провенанса — берём из выгрузки outout/ (батч парсит с OCR)."""
    if not has_fixture("КР1_4"):
        pytest.skip("нет фикстуры КР1_4")
    doc = load_outout("КР1_4")
    prov = doc.get("provenance")
    if not prov:
        pytest.skip("outout/КР1_4.json без провенанса — требуется перепарс батчем с OCR")
    dis = prov.get("disagreements", [])
    assert dis, "КР1_4 disagreements пусты — модель кандидатов не работает"
    assert any("arce" in (d.get("ocr") or "") for d in dis), \
        "нет расхождения Barcelona/Вагсе1опа — сбор кандидатов неверен"
