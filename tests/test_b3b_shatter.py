# -*- coding: utf-8 -*-
"""ШАГ 2 (промпт 17): B3b — прецизионный гейт рассыпанных ЛАТИНСКИХ слов.

Сигнал `shattered_latin` скринера на 96% ШУМ (табличные числа, РАЗРЯДКА русского).
Гейт `_is_genuine_latin_shatter` обязан отсечь шум и НИКОГДА не пускать разрядку
русского в латинизацию (Group B). Тест исполняет гейт напрямую (быстрый, без OCR).
"""
import re

from crparser.engine.latinrecovery import LatinRecoverer, _strip_edges

_TOK = re.compile(r"[^\s]+")


def _toks(text):
    out = []
    for m in _TOK.finditer(text):
        lead, core, _ = _strip_edges(m.group())
        cs = m.start() + len(lead)
        out.append((cs, cs + len(core), core))
    return out


def _run_span(text, run_str):
    """Индексы [i,j) токенов, образующих run_str в тексте."""
    toks = _toks(text)
    parts = run_str.split()
    for i in range(len(toks)):
        if [t[2] for t in toks[i:i + len(parts)]] == parts:
            return toks, i, i + len(parts)
    raise AssertionError("run not found")


def _gate(text, run_str):
    r = LatinRecoverer.__new__(LatinRecoverer)
    toks, i, j = _run_span(text, run_str)
    return r._is_genuine_latin_shatter(toks, i, j)


def test_latin_flanked_shatter_qualifies():
    assert _gate("cause Aspergillus у 1 ш з fungus grows", "у 1 ш з") is True


def test_letterspaced_russian_rejected():
    """Разрядка русского (`трансфераз ы в крови`) — НЕ латинский осколок (Group B)."""
    assert _gate("аминотрансфераз ы в крови повышена", "ы в") is False


def test_table_numbers_rejected():
    assert _gate("results 0 1 2 3 4 columns", "0 1 2 3 4") is False


def test_russian_micro_phrase_rejected():
    """`1 и 2` (= «типов 1 и 2») — одна кир-буква, не рассыпанное слово."""
    assert _gate("простого герпеса 1 и 2 типов", "1 и 2") is False


def test_no_latin_neighbor_rejected():
    """Осколок без чистого лат. соседа -> не наш случай."""
    assert _gate("температура во д ы измерялась", "д ы") is False
