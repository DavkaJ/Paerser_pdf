# -*- coding: utf-8 -*-
"""GROUP B — канал C5 не латинизирует РУССКИЙ. Стражи гейта 2 (языковой арбитр).

ПОЧЕМУ КР1000_1 (байт-в-байт) НЕ доказывает безопасность: он здоровый born-digital, все
шрифты честные -> гейт 1 отсекает документ целиком, канал C5 там вообще не запускается.
Ноль правок на файле, где детектор не работает, не доказывает ничего. Поэтому основные
стражи ниже смотрят на БИТЫЕ документы, где канал реально работает.

ЧТО ЗДЕСЬ ЦИРКУЛЯРНО, А ЧТО НЕТ (читать перед правкой — на этом уже обжигались).
`test_c5_never_touches_long_lowercase_cyrillic` совпадает с правилом 4 гейта 2 -> он
проходит ПО ПОСТРОЕНИЮ и НЕ является эмпирическим доказательством; его роль — зафиксировать
правило, чтобы его не сняли молча. Эмпирическую нагрузку несёт
`test_c5_did_not_touch_corpus_russian_vocabulary`: корпусный словарь русского (слово, чисто
встречающееся в >=3 документах baseline) НЕ совпадает ни с одним гейтом (гейты смотрят
частоту ВНУТРИ документа, словарь OpenCorpora и форму токена) -> этот тест МОЖЕТ упасть и
поймает регресс, которого правило 4 не видит (короткие слова, слова с заглавной, `рока`).

История (I21): предыдущая редакция этого файла содержала ДВА теста, которые сравнивали
`_translit(w)` сам с собой (`_similar(_translit(w), _translit(w))` == 1.0 ВСЕГДА) —
они проходили при ЛЮБОМ состоянии кода и «зеленели», когда канал латинизировал 196 рус.
словоформ. Удалены. Тест, который не может упасть, хуже отсутствующего.
"""
import glob
import json
import os
import re
from collections import Counter

import pytest

from crparser.engine import rumorph
from crparser.engine.latinrecovery import LatinRecoverer, c5_active

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATIN = os.path.join(ROOT, "outout_latin")
BASE = os.path.join(ROOT, "outout")
# КРИТЕРИЙ БЕЗ .lower()! Токен ОБЯЗАН уже состоять из строчных кириллических. Приведение
# к нижнему регистру уничтожает ровно тот признак, по которому порча отличается от рус.
# слова (заглавная ВНУТРИ токена): `оезорЬадиз`.lower() -> «10 строчных кириллических» ->
# ложное срабатывание на КОРРЕКТНОЙ починке. Прежняя редакция теста этим и болела.
_RU7 = re.compile(r"^[а-яё]{7,}$")
_W = re.compile(r"[^\s]+")
_MIN_DOCS = 3
_VOCAB_CACHE = None


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


def _corpus_russian_vocab():
    """Корпусный словарь РУССКОГО: слово реально, если ЧИСТО встречается в >=3 ДОВЕРЕННЫХ
    документах baseline (born-digital + честные шрифты). Собирается
    `_corpus/build_latin_vocab.py` в `_corpus/corpus_ru_vocab.json`.

    НЕЗАВИСИМ ОТ ВСЕХ ГЕЙТОВ (в этом весь смысл): гейты смотрят частоту ВНУТРИ документа,
    словарь OpenCorpora и форму токена — межкорпусную частоту не смотрит НИКТО. Поэтому
    этот тест МОЖЕТ упасть. Он же покрывает то, чего не может pymorphy: `гепатоцеллюлярный`
    в этом словаре ЕСТЬ (>=3 доверенных док.), а в OpenCorpora — нет.

    ИСТОЧНИК ОБЯЗАН БЫТЬ ДОВЕРЕННЫМ (I23): словарь по ВСЕМУ baseline содержал `уегзиз`
    (=versus), `рпшагу`(=primary), `йозе`(=dose) — битые шрифты рендерят латиницу
    кириллицей, и «словарь русского» начинал считать КОРРЕКТНЫЕ починки нарушениями."""
    global _VOCAB_CACHE
    if _VOCAB_CACHE is None:
        try:
            p = os.path.join(ROOT, "_corpus", "corpus_ru_vocab.json")
            _VOCAB_CACHE = set(json.load(open(p, encoding="utf-8")).get("words", []))
        except Exception:
            _VOCAB_CACHE = set()
    return _VOCAB_CACHE


def _c5_corrections():
    """Все C5-правки реального выхода (outout_latin) -> (файл, src, dst)."""
    out = []
    for p in glob.glob(os.path.join(LATIN, "*.json")):
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for c in (doc.get("latin_recovery", {}) or {}).get("corrections", []):
            if "c5_corrupt_font" in (c.get("why_suspect") or []):
                out.append((os.path.basename(p), c.get("source_text") or "",
                            c.get("resolved_text") or ""))
    return out


# ----------------------- ГЕЙТ 2: юнит, на пинах катастрофы -----------------------
def _gate():
    r = LatinRecoverer.__new__(LatinRecoverer)
    r._freq = {}
    return r


@pytest.mark.skipif(not rumorph.available(), reason="pymorphy3 недоступен")
def test_gate2_protects_catastrophe_pins():
    """ЯВНЫЕ ПИНЫ двух откатов (I21). Каждый уже стоил корпусу порчи:
    `боль`->`bone`, `пациентов`->`nayuenmoe`, `Уровень`->`ypoBeHb`.
    Гейт 2 ОБЯЗАН признать их русскими -> замена невозможна В ПРИНЦИПЕ."""
    g = _gate()
    for w in ("боль", "пациентов", "Уровень", "Клинические", "печени", "стационар",
              "исследования", "рекомендуется", "зависимый", "СОСТАВ"):
        assert g._c5_protected(w), "гейт 2 НЕ защитил рус. слово %r -> оно будет латинизировано" % w


@pytest.mark.skipif(not rumorph.available(), reason="pymorphy3 недоступен")
def test_gate2_protects_russian_abbreviations():
    """ALL-CAPS рус. аббревиатуры. Их НЕ знает ни pymorphy (`ГЦР`/`СНВС` -> known=False),
    ни LAT-словарь (eng-OCR даёт РЕАЛЬНОЕ лат. слово: `СОД`->COD, `ВГД`->BID) — держит
    ТОЛЬКО правило all-caps. Проверяем БЕЗ частотной подпорки (_freq пуст)."""
    g = _gate()
    for w in ("ГЦР", "СНВС", "СОД", "ВГД", "РФ", "УЗИ", "ШСС"):
        assert g._c5_protected(w) == "all_caps", (
            "all-caps рус. аббревиатура %r не защищена по построению" % w)


@pytest.mark.skipif(not rumorph.available(), reason="pymorphy3 недоступен")
def test_gate2_protects_medical_russian_beyond_dictionary():
    """ГРАНИЦА ГЕЙТА, явно зафиксированная: OpenCorpora — словарь ОБЩЕГО языка, мед.
    лексики в нём НЕТ (`гепатоцеллюлярном` -> word_is_known=False). Эти формы держит
    ПРАВИЛО ФОРМЫ (7+ строчных кириллических), и оно обязано их держать БЕЗ частоты."""
    g = _gate()
    for w in ("гепатоцеллюлярном", "стеатогепатита", "мультикиназными",
              "холангиокарцинома", "иммуногистохимического"):
        assert not rumorph.word_is_known(w), (
            "%r внезапно в словаре — пересмотреть комментарии о границе гейта" % w)
        assert g._c5_protected(w) == "long_lowercase", (
            "мед. словоформа %r не защищена -> риск латинизации" % w)


@pytest.mark.skipif(not rumorph.available(), reason="pymorphy3 недоступен")
def test_gate2_lets_c5_corruption_through():
    """Обратная сторона: гейт 2 не должен глушить РЕАЛЬНУЮ порчу — иначе recall=0
    и канал бесполезен. Эти токены обязаны дойти до гейта 3."""
    g = _gate()
    for w in ("йуег", "уапсез", "КеПгогк", "оезорЬадиз", "Амап", "Какона", "РоНа1"):
        assert not g._c5_protected(w), "гейт 2 заглушил ПОРЧУ %r -> recall падает" % w


def test_c5_is_fail_closed_without_arbiter(monkeypatch):
    """FAIL-CLOSED (I5): нет языкового арбитра -> канал ВЫКЛЮЧЕН ЦЕЛИКОМ. Трактовать
    «пакета нет» как «слово неизвестно» = латинизировать русский на машине без
    зависимости и молча произвести ДРУГОЙ корпус (ровно дыра I5)."""
    monkeypatch.setattr(rumorph, "available", lambda: False)
    assert not c5_active(), "без pymorphy канал C5 обязан молчать, а не латинизировать всё"
    # и сам арбитр при недоступности обязан отвечать «русский» (защита), а не «неизвестно»
    monkeypatch.setattr(rumorph, "_TRIED", True)
    monkeypatch.setattr(rumorph, "_ANALYZER", None)
    assert rumorph.word_is_known("боль") is True
    assert rumorph.word_is_known("уапсез") is True


# ----------------------- ФАКТ ПО КОРПУСУ (outout_latin) -----------------------
@pytest.mark.skipif(not os.path.isdir(LATIN), reason="outout_latin не собран")
def test_c5_never_touches_long_lowercase_cyrillic():
    """ПО ПОСТРОЕНИЮ (правило 4 гейта 2), не эмпирика — см. шапку модуля.
    Токен из 7+ подряд СТРОЧНЫХ кириллических = русское слово (в т.ч. медицинское,
    которого нет ни в OpenCorpora, ни в частотной защите). C5 не имеет права его
    трогать — ни при какой похожести, ни при каком словаре."""
    bad = [(d, s, t) for d, s, t in _c5_corrections() if _RU7.match(s)]
    assert not bad, ("C5 латинизировал токены из 7+ строчных кириллических (%d): %s"
                     % (len(bad), bad[:10]))


@pytest.mark.skipif(not os.path.isdir(LATIN), reason="outout_latin не собран")
def test_c5_did_not_touch_corpus_russian_vocabulary():
    """ГЛАВНЫЙ ЭМПИРИЧЕСКИЙ СТРАЖ (НЕ циркулярен, МОЖЕТ упасть).
    Критерий независим от всех гейтов: слово, ЧИСТО встречающееся в >=3 документах
    baseline, — реальное русское слово корпуса. C5 не имеет права его трогать.
    Ловит то, чего правило формы не видит: короткие слова (`боль`, `рока`), слова с
    заглавной (`Уровень`), аббревиатуры."""
    vocab = _corpus_russian_vocab()
    if len(vocab) < 1000:
        pytest.skip("baseline-корпус недоступен (словарь пуст)")
    bad = [(d, s, t) for d, s, t in _c5_corrections() if s.lower() in vocab]
    assert not bad, (
        "C5 латинизировал РУССКИЕ слова корпусного словаря (>=%d док.) — %d шт: %s"
        % (_MIN_DOCS, len(bad), bad[:10]))


@pytest.mark.skipif(not os.path.isdir(LATIN), reason="outout_latin не собран")
def test_c5_channel_silent_on_healthy_fonts():
    """ГЕЙТ 1: у документа со ВСЕМИ честными шрифтами канал C5 не даёт НИ ОДНОЙ правки
    (и не делает ни одного вызова OCR). Контрольная группа I1."""
    for base in ("КР1000_1", "КР845_1", "КР802_1", "КР901_1", "КР66_4", "КР876_1"):
        p = os.path.join(LATIN, base + ".json")
        if not os.path.exists(p):
            continue
        doc = json.load(open(p, encoding="utf-8"))
        c5 = [c for c in (doc.get("latin_recovery", {}) or {}).get("corrections", [])
              if "c5_corrupt_font" in (c.get("why_suspect") or [])]
        assert not c5, "%s (честные шрифты): канал C5 не должен срабатывать: %s" % (base, c5[:5])


def test_c5_defer_reasons_cover_ambiguous_not_frequent():
    """КОНТРАКТ ОЧЕРЕДИ. Гейт 2 запрещает автозамену — но там, где запрет опирается на
    ОБЩЕЕ знание, не знающее латыни этого корпуса, токен обязан уйти ЧЕЛОВЕКУ, а не
    исчезнуть: `рока`(morphology) = И рус. слово, И реально `portal`; `ШСС`(all_caps) =
    И похож на рус. аббревиатуру, И реально `UICC`; `аззеззшеп`(long_lowercase) = И 7+
    строчных, И реально `assessment`. А `freq`/`stop` — сильная улика ВНУТРИ документа
    (`пациентов`/`может`), там человеку смотреть нечего и очередь бы утонула."""
    from crparser.engine.latinrecovery import LatinRecoverer as LRec
    assert LRec._C5_DEFER_REASONS == {"morphology", "all_caps", "long_lowercase"}
    for r in ("freq", "stop"):
        assert r not in LRec._C5_DEFER_REASONS, (
            "%r попал в очередь -> она утонет в реальных рус. словах" % r)


@pytest.mark.skipif(not os.path.isdir(LATIN), reason="outout_latin не собран")
def test_c5_ambiguous_go_to_human_queue():
    """ОСТАТОК ПРИНЯТ ОСОЗНАННО И ВИДИМО. Токен, валидный как русский И реально битая
    латиница, автозамене не подлежит — но обязан лежать в очереди С КРОПОМ. Проверяем на
    `рока`->portal (родительный от «рок»): именно он назван эталоном остатка."""
    qp = os.path.join(ROOT, "_corpus", "verify_queue", "tasks.json")
    if not os.path.exists(qp):
        pytest.skip("очередь не собрана")
    q = json.load(open(qp, encoding="utf-8"))
    tasks = q if isinstance(q, list) else q.get("tasks", [])
    kr = [t for t in tasks if t.get("doc") == "КР1_4"]
    if not kr:
        pytest.skip("КР1_4 не в очереди")
    by_src = {t.get("source_text"): t for t in kr}
    for src in ("рока", "ШСС"):
        t = by_src.get(src)
        assert t is not None, (
            "%r не попал в очередь: остаток обязан быть ВИДИМЫМ человеку, а не молча "
            "осесть в корпусе" % src)
        assert t.get("crop_png"), "%r в очереди БЕЗ кропа — задача бесполезна человеку" % src


@pytest.mark.skipif(not os.path.isdir(LATIN), reason="outout_latin не собран")
def test_term_auto_requires_strong_evidence_not_e3():
    """МОЛЧАЛИВАЯ замена термина — только при СИЛЬНОЙ улике на САМОМ токене (sim>=0.8).

    Регресс, который этот тест ловит (замерен на корпусе): правило было
    `not (sim >= 0.8 or e3)`, т.е. E3 сам по себе разрешал auto ПРИ ЛЮБОЙ похожести. E3 =
    «слово есть где-то в этом документе», а `the`/`and`/`for` есть везде -> сбой
    выравнивания давал МОЛЧАЛИВУЮ порчу: `Ыуег`->`the`, `НаетоггЬаде`->`Guideline`.
    Чистый LAT-словарь это НЕ ловит (`the` — настоящее слово), поэтому нужен именно тест."""
    from crparser.engine.latinrecovery import C5_AUTO_SIM
    bad = []
    for p in glob.glob(os.path.join(LATIN, "*.json")):
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for c in (doc.get("latin_recovery", {}) or {}).get("corrections", []):
            if c.get("decision") != "auto":
                continue
            m = re.search(r"eng-crop\+align\(sim=([0-9.]+)\)", c.get("rule") or "")
            if m and float(m.group(1)) < C5_AUTO_SIM:
                bad.append((os.path.basename(p), c.get("source_text"),
                            c.get("resolved_text"), m.group(1)))
    assert not bad, (
        "МОЛЧАЛИВЫЕ замены термина при sim<%.1f (%d шт) — вернулся E3-путь: %s"
        % (C5_AUTO_SIM, len(bad), bad[:10]))


@pytest.mark.skipif(not os.path.isdir(LATIN), reason="outout_latin не собран")
def test_latin_vocab_is_built_from_trusted_source():
    """LAT-словарь (гейт 3) обязан быть собран ТОЛЬКО по born-digital с честными шрифтами.
    Иначе он содержит артефакты full-OCR сканов и битых шрифтов — и гейт «результат обязан
    быть настоящим лат. словом» начинает ПРОПУСКАТЬ порчу: `пациентов`->`nayuenmoe`
    проходил, потому что `nayuenmoe` в словаре БЫЛ (замерено)."""
    from crparser.engine.latinrecovery import latin_vocab
    v = latin_vocab()
    assert len(v) > 1000, "словарь подозрительно мал: %d" % len(v)
    for w in ("nayuenmoe", "ypobehb", "ajit", "mozo"):
        assert w not in v, (
            "транслит-мусор %r снова в LAT-словаре -> гейт 3 пропустит латинизацию "
            "русского. Пересобрать: python _corpus/build_latin_vocab.py" % w)
    for w in ("bone", "portal", "liver", "varices", "assessment"):
        assert w in v, "настоящее лат. слово %r потеряно -> recall упадёт" % w


@pytest.mark.skipif(not os.path.isdir(LATIN), reason="outout_latin не собран")
def test_c5_recovers_known_class_positives():
    """ПОЗИТИВ: класс C5 (88% пропусков детектора, замер recall) реально чинится.
    Без этого теста «безопасность» достигается тривиально — выключением канала."""
    p = os.path.join(LATIN, "КР1_4.json")
    if not os.path.exists(p):
        pytest.skip("КР1_4 не собран")
    fixed = {s: t for d, s, t in _c5_corrections() if d == "КР1_4.json"}
    for src, dst in (("уапсез", "varices"), ("КеПгогк", "Network"),
                     ("оезорЬадиз", "oesophagus"), ("Огдашгайоп", "Organization")):
        assert fixed.get(src) == dst, (
            "C5 не восстановил %r->%r (получено %r) — канал деградировал"
            % (src, dst, fixed.get(src)))
    # ЧЕСТНАЯ ГРАНИЦА (замерено, `_corpus/detector_recall.md`): `йуег`->liver, `то<Зе1`->
    # model, `сйзеазе`->disease на стр.69 НЕ чинятся — и это НЕ гейт 2. Гейт 1 пропускает
    # строку, eng-OCR читает её ИДЕАЛЬНО («...The model for end-stage liver disease...»),
    # но `_align_line` (жадное окно 3) на длинной библиографической строке уводит
    # выравнивание: nat[10]=`йуег` -> eng=`it`, sim=0.00 -> кандидат отброшен как шум.
    # Информация ЕСТЬ в кропе, её теряет ВЫРАВНИВАНИЕ. Чинится глобальным выравниванием
    # (Needleman-Wunsch) — отдельная работа: `_align_line` общий с term-каналом (4801
    # замена), правка требует своего цикла верификации. Пин НЕ засчитан, см. отчёт.
    assert "йуег" not in fixed, (
        "`йуег` внезапно чинится — значит выравнивание переписали: перепроверить term-канал "
        "(4801 замена на том же `_align_line`) и обновить этот комментарий")
