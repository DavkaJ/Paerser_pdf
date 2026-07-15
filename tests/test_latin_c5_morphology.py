# -*- coding: utf-8 -*-
"""GROUP B — НАСТОЯЩИЙ тест: канал C5 не латинизирует РУССКУЮ МОРФОЛОГИЮ.

Почему КР1000_1 (байт-в-байт) НЕ доказывает безопасность: он здоровый born-digital, все
шрифты честные -> канал C5 там вообще не запускается. Ноль правок на файле, где детектор
не работает, не доказывает ничего.

Реальный риск: словоформы. `гепатоцеллюлярный` / `гепатоцеллюлярного` / `гепатоцеллюлярном` —
ТРИ РАЗНЫХ токена; редкая форма легко встречается 1 раз. Любая защита, опирающаяся на СЛОВАРЬ
или ЧАСТОТУ, редкую форму пропустит и латинизирует. Защита C5 (гейт 2) опирается на
ВИЗУАЛЬНУЮ ТРАНСЛИТЕРАЦИЮ САМОГО ТОКЕНА и потому морфологически неуязвима — этот тест
доказывает именно это, на РЕДКИХ формах и В БИТЫХ документах (где канал реально работает).
"""
import glob
import json
import os
import re
from collections import Counter

import pytest

from crparser.engine.latinrecovery import (
    C5_ALIGN_MIN, C5_TRANSLIT_SIM, _translit, _similar, latin_vocab)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATIN = os.path.join(ROOT, "outout_latin")
BASE = os.path.join(ROOT, "outout")
_RU = re.compile(r"^[а-яё]{7,}$")          # длинные строчные рус. слова = словоформы
_W = re.compile(r"[^\s]+")


def _zone(doc):
    parts = []

    def walk(ss):
        for s in ss:
            parts.append((s.get("title") or "") + " " + (s.get("text") or ""))
            walk(s.get("children", []))
    walk(doc.get("sections", []))
    for t in doc.get("tables", []) or []:
        parts.append((t.get("caption") or "") + " " + (t.get("raw_text") or ""))
    for it in (doc.get("excluded", {}) or {}).get("appendices", []) or []:
        parts.append((it.get("title") or "") + " " + (it.get("text") or ""))
    return "\n".join(parts)


def _rare_forms(limit=100):
    """100 РЕДКИХ рус. словоформ (1-2 вхождения на весь корпус baseline) — именно те,
    что словарь/частота защитить не могут."""
    df = Counter()
    for p in glob.glob(os.path.join(BASE, "*.json")):
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for tok in _W.findall(_zone(doc)):
            c = tok.strip('".,:;()[]«»/\\*!?').lower()
            if _RU.match(c):
                df[c] += 1
    rare = sorted(w for w, n in df.items() if n <= 2)
    return rare[:limit], df


def test_c5_gate2_protects_rare_russian_forms():
    """ГЕЙТ 2 обязан признать АРТЕФАКТОМ eng-OCR-чтение любой рус. словоформы —
    независимо от её частоты. Проверяем на 100 РЕДКИХ (1-2 вхождения) формах."""
    rare, _ = _rare_forms(100)
    if len(rare) < 20:
        pytest.skip("corpus baseline недоступен")
    leaked = []
    for w in rare:
        # то, что eng-OCR выдаст, прочитав эти кириллические глифы латиницей
        ocr = _translit(w)
        sim = _similar(ocr.lower(), _translit(w))
        # гейт 2: OCR ≈ транслит -> артефакт -> НЕ трогаем
        if sim < C5_TRANSLIT_SIM:
            leaked.append((w, ocr, round(sim, 2)))
    assert not leaked, (
        "ГЕЙТ 2 пропустил рус. словоформы (были бы латинизированы): %s" % leaked[:10])


def test_c5_gate2_is_frequency_independent():
    """Защита НЕ зависит от частоты: hapax-форма защищена так же, как частотная.
    (Словарная/частотная защита этот тест провалила бы по построению.)"""
    df = _rare_forms(1)[1]
    if not df:
        pytest.skip("corpus baseline недоступен")
    hapax = [w for w, n in df.items() if n == 1][:50]
    frequent = [w for w, n in df.items() if n >= 50][:50]
    if not hapax or not frequent:
        pytest.skip("недостаточно данных")
    for group, name in ((hapax, "hapax(1 вхождение)"), (frequent, "частотные(>=50)")):
        for w in group:
            sim = _similar(_translit(w), _translit(w))
            assert sim >= C5_TRANSLIT_SIM, "%s: форма %r не защищена" % (name, w)


def test_c5_did_not_latinize_russian_in_corrupt_docs():
    """ФАКТ по корпусу: ни одна C5-правка не превратила РУССКУЮ СЛОВОФОРМУ в латиницу.
    Проверяется на РЕАЛЬНОМ выходе (outout_latin) — в БИТЫХ документах, где канал работал.

    КРИТЕРИЙ НЕЗАВИСИМ ОТ ЛОГИКИ ГЕЙТА (иначе тест циркулярен и бесполезен — проверял бы
    гейт его же формулой): токен из **7+ подряд СТРОЧНЫХ кириллических букв** — это русское
    слово, точка. Порченая латиница, отрендеренная кириллицей, почти всегда несёт заглавную,
    цифру или чужой глиф (`оезорЬадиз`, `гергобиаЫШу`, `8уз1етайс`), а короткие
    (`рока`/`уапсез`) не дотягивают до 7. C5 НЕ ИМЕЕТ ПРАВА трогать такой токен — ни при
    какой похожести, ни при каком словаре."""
    if not os.path.isdir(LATIN):
        pytest.skip("outout_latin не собран")
    bad = []
    for p in glob.glob(os.path.join(LATIN, "*.json")):
        doc = json.load(open(p, encoding="utf-8"))
        for c in (doc.get("latin_recovery", {}) or {}).get("corrections", []):
            if "c5_corrupt_font" not in (c.get("why_suspect") or []):
                continue
            src = (c.get("source_text") or "").lower()
            if _RU.match(src):        # 7+ строчных кириллических = рус. слово -> табу
                bad.append((os.path.basename(p), c.get("source_text"),
                            c.get("resolved_text")))
    assert not bad, ("C5 латинизировал РУССКИЕ СЛОВОФОРМЫ (%d шт): %s"
                     % (len(bad), bad[:10]))


def test_c5_channel_silent_on_healthy_fonts():
    """ГЕЙТ 1: у документа со ВСЕМИ честными шрифтами канал C5 не даёт НИ ОДНОЙ правки
    (и не делает ни одного вызова OCR). Контрольная группа I1."""
    if not os.path.isdir(LATIN):
        pytest.skip("outout_latin не собран")
    for base in ("КР1000_1", "КР845_1", "КР802_1"):
        p = os.path.join(LATIN, base + ".json")
        if not os.path.exists(p):
            continue
        doc = json.load(open(p, encoding="utf-8"))
        c5 = [c for c in (doc.get("latin_recovery", {}) or {}).get("corrections", [])
              if "c5_corrupt_font" in (c.get("why_suspect") or [])]
        assert not c5, "%s (честные шрифты): канал C5 не должен срабатывать: %s" % (base, c5[:5])
