# -*- coding: utf-8 -*-
"""Убрать из _verified_corrections.json ОПАСНЫЕ для ГЛОБАЛЬНОГО применения формы (очередь
дедуплицировала по форме, смешав латинские и русские вхождения — правка одного кропа
человеком не должна латинизировать ВСЕ вхождения). Опасно, если ИСТОЧНИК — правдоподобный
рус. токен: инициалы (Е.А/Р.А/ЕА), известное рус. слово, или короткий кир. фрагмент (<=3),
совпадающий с рус. обрывком (ап-, апб-=anti-). Такие формы -> _deferred_ambiguous.json,
их разберёт per-occurrence LLM-проход шага 2 (контекст кропа/строки).

СТАРЫЕ 72 (уже применены и проверены токен-дифом) НЕ трогаем — фильтр только для НОВЫХ форм.
"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from crparser.engine import rumorph

QDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "_corpus", "verify_queue")
VERIFIED = os.path.join(QDIR, "_verified_corrections.json")
OLD = os.path.join(QDIR, "_verified_corrections.json.bak")   # старые 72 (baseline)
DEFERRED = os.path.join(QDIR, "_deferred_ambiguous.json")

INIT = re.compile(r"^[А-ЯЁ]\.?[А-ЯЁ]\.?$")
CYRPURE = re.compile(r"^[А-Яа-яЁё]+$")
LOWER_CYR1 = re.compile(r"^[а-яё]$")             # одиночная СТРОЧНАЯ кир. буква = предлог/маркер


def known_ru(f):
    try:
        return rumorph.word_is_known(f.strip(".,;:()[]"))
    except Exception:
        return False


def is_dangerous(f):
    core = f.strip(".")
    if " " in f or "\t" in f or "\n" in f:
        # ФРАЗА: опасна, если содержит ОТДЕЛЬНЫЙ одиночный СТРОЧНЫЙ кир. токен —
        # это рус. предлог (с/у/а/о) или маркер перечисления (а/б/в), а НЕ гомоглиф.
        # (`difficile с`=with, `AOSpine: а`=пункт «а»). Заглавная (`Hepatitis В`) — гомоглиф B, ок.
        for tok in re.split(r"[\s]+", f):
            if LOWER_CYR1.match(tok.strip(".,;:()[]-")):
                return "phrase-ru-preposition/marker"
        return None
    if INIT.match(f) and f[0] not in "ЫЬЪ":
        return "initials"
    if known_ru(f):
        return "known-ru"
    if CYRPURE.match(f) and len(core) <= 3:
        return "short<=3-cyr"
    return None


def main():
    V = json.load(open(VERIFIED, encoding="utf-8"))
    old = set(json.load(open(OLD, encoding="utf-8"))) if os.path.exists(OLD) else set()
    # МЕРЖ в существующий deferred (не терять ранее отложенные)
    deferred = json.load(open(DEFERRED, encoding="utf-8")) if os.path.exists(DEFERRED) else {}
    for f in list(V):
        if f in old:                       # старые 72 — проверены, не трогаем
            continue
        reason = is_dangerous(f)
        if reason:
            deferred[f] = {**V.pop(f), "deferred_reason": reason}
    json.dump(V, open(VERIFIED, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(deferred, open(DEFERRED, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("applied set (safe): %d форм" % len(V))
    print("deferred (ambiguous, -> step 2 per-occurrence): %d форм" % len(deferred))
    for f, e in deferred.items():
        print("   %r -> %r  [%s]" % (f, e["corrected"], e["deferred_reason"]))


if __name__ == "__main__":
    main()
