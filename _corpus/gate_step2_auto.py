# -*- coding: utf-8 -*-
"""ПОДГОТОВКА применения auto-предсказаний шага 2 к тексту — через ТОТ ЖЕ гейт безопасности,
что в шаге 1 (НЕ «по форме» вслепую), + оценка per-occurrence экспозиции. НИЧЕГО НЕ ПРИМЕНЯЕТ:
только считает, сколько форм гейт отсеет и какова экспозиция.

Кандидаты на применение = v4-предсказания gate=="auto" (VLM CANDIDATE + независимый OCR +
шаблон сущности). Каждая задача v4 дедуплицирована ПО ФОРМЕ, но несёт ОДИН кроп -> VLM проверил
ОДНО вхождение, а применение по форме затронуло бы ВСЕ (occurrences/docs). Отсюда два фильтра:
  1. Гейт безопасности шага 1 (is_dangerous): инициалы / известное рус. слово / короткий
     кир. фрагмент / фраза с рус. предлогом.
  2. Экспозиция: сколько вхождений/докумантов у формы (проверено VLM = 1). Много вхождений при
     коротком/неоднозначном источнике = риск, даже если is_dangerous пропустил.

    python _corpus/gate_step2_auto.py
"""
import json, os, re, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from _corpus.safety_filter_verified import is_dangerous   # тот же гейт, что в шаге 1
from crparser.engine.latinrecovery import latin_vocab

# МЕД+АНГЛ СЛОВАРЬ = latin_vocab() (born-digital мед. корпус, 4016 слов): Pseudomonas/
# Helicobacter/pallidum ЕСТЬ, фамилии (bepren/Isupova) — НЕТ (ловит транслит рус. имён).
# Плюс: исключаем ALL-CAPS (акронимы ANCA/NYHA — в словаре есть, но по решению -> человеку) и
# многословные/дефисные/суффиксные (`follow-`,`NETTER-1`) -> человеку.
_VOCAB = latin_vocab()


def in_med_dict(dst):
    d = (dst or "").strip()
    if " " in d:
        return False
    core = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", d)
    if not core or "-" in core:
        return False
    if core.isupper():                # акроним -> человеку
        return False
    return core.lower() in _VOCAB

# ТРАНСЛИТ-МУСОР: VLM+OCR на РУССКОМ источнике даёт чистую латиницу, которая проходит
# entity_valid, но это транслитерация, а не слово (`СпецЛит`->`CnenJIut`, `ТхаМ`->`TlaNOMO`).
# Сигнатура: >=2 строчных подряд, затем ЗАГЛАВНАЯ (камел в СЕРЕДИНЕ) — у настоящих слов/имён
# собственных такого нет; легит биохим (`pO2`,`mRNA`) — ОДНА строчная перед заглавной (не ловим).
_TRANSLIT = re.compile(r"[a-z]{2}[A-Z]")


def looks_translit(dst):
    return bool(_TRANSLIT.search(dst or ""))

QDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_corpus", "verify_queue")
V4 = os.path.join(QDIR, "_v4_predictions.jsonl")
V4Q = os.path.join(QDIR, "tasks_min_v4.json")
APPLIED = os.path.join(QDIR, "_verified_corrections.json")
DEFERRED = os.path.join(QDIR, "_deferred_ambiguous.json")
OUT = os.path.join(QDIR, "_step2_apply_candidates.json")


def main():
    applied = set(json.load(open(APPLIED, encoding="utf-8")))
    deferred = set(json.load(open(DEFERRED, encoding="utf-8")))
    already = applied | deferred
    # экспозиция из v4-очереди: source_text -> (occurrences, #docs)
    occ = {}
    for t in json.load(open(V4Q, encoding="utf-8")):
        occ[t.get("source_text")] = (t.get("occurrences") or len(t.get("docs") or []) or 1,
                                     len(t.get("docs") or []))

    preds = [json.loads(l) for l in open(V4, encoding="utf-8")]
    auto = [p for p in preds if p.get("gate") == "auto"]
    # distinct формы среди auto, не тронутые шагом 1
    forms = {}
    for p in auto:
        s = p.get("source_text")
        if s in already or not p.get("corrected"):
            continue
        forms[s] = p["corrected"]

    filtered = {}      # form -> reason (гейт безопасности шага 1 отсеял)
    translit = {}      # form -> reason (транслит-мусор на рус. источнике)
    exposure = {}      # form -> reason (occ>1: VLM проверил 1 кроп, применение затронет все)
    not_dict = {}      # form -> corrected НЕ в мед+англ словаре (имена/акронимы/суффиксы -> человеку)
    safe = {}          # occ==1 + corrected в словаре: by-form == per-occurrence, реальное слово
    for s, dst in forms.items():
        r = is_dangerous(s)
        if r:
            filtered[s] = r
            continue
        if looks_translit(dst):
            translit[s] = "translit-garbage"
            continue
        n_occ, n_docs = occ.get(s, (1, 1))
        # occ>1 -> by-form генерализует одну VLM-проверку на ВСЕ вхождения (разный контекст) ->
        # на per-occurrence (шаг 2 применяет по локации кропа, не по форме).
        if n_occ > 1 or n_docs > 1:
            exposure[s] = "occ=%d docs=%d" % (n_occ, n_docs)
            continue
        # ФИНАЛЬНЫЙ гейт (уточнение пользователя): corrected обязан быть реальным словом
        # мед+англ словаря — иначе имя/акроним/транслит -> НЕ в текст, остаётся предсказанием в LS.
        if not in_med_dict(dst):
            not_dict[s] = dst
            continue
        safe[s] = dst

    print("=" * 70)
    print("ГЕЙТ ДЛЯ ПРИМЕНЕНИЯ AUTO-ПРЕДСКАЗАНИЙ ШАГА 2 (ничего не применено)")
    print("=" * 70)
    print("auto-предсказаний всего: %d" % len(auto))
    print("distinct форм (не из шага 1): %d" % len(forms))
    print()
    print("(1) ОТСЕЯНО гейтом безопасности шага 1 (is_dangerous): %d форм" % len(filtered))
    for r, n in collections.Counter(filtered.values()).most_common():
        print("      %-34s %d" % (r, n))
    for s in list(filtered)[:6]:
        print("        %r -> %r  [%s]" % (s, forms[s], filtered[s]))
    print()
    print("(2) ОТСЕЯНО как ТРАНСЛИТ-МУСОР (рус. источник): %d форм" % len(translit))
    for s in list(translit)[:10]:
        print("        %r -> %r" % (s, forms[s]))
    print()
    print("(3) ОТСЕЯНО по ЭКСПОЗИЦИИ occ>1 (нужен per-occurrence, не by-form): %d форм" % len(exposure))
    for s in list(exposure)[:6]:
        print("        %r -> %r  [%s]" % (s, forms[s], exposure[s]))
    print()
    print("(4) ОТСЕЯНО: corrected НЕ в мед+англ словаре (имена/акронимы/суффиксы -> человеку в LS): %d" % len(not_dict))
    for s in list(not_dict)[:12]:
        print("        %r -> %r" % (s, not_dict[s]))
    print()
    print("ПРОШЛИ ВСЁ (occ==1 + реальное слово мед+англ словаря) -> К ПРИМЕНЕНИЮ:")
    print("   **%d форм**" % len(safe))
    for s in list(safe)[:15]:
        print("        %r -> %r" % (s, safe[s]))

    json.dump({"safe": safe, "filtered": filtered, "translit": translit, "exposure": exposure,
               "not_dict": not_dict, "n_auto": len(auto), "n_forms": len(forms)},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print()
    print("=" * 70)
    print("ИТОГ по %d новым auto-формам:" % len(forms))
    print("  отсеяно: is_dangerous=%d + translit=%d + occ>1=%d + не-в-словаре=%d = %d"
          % (len(filtered), len(translit), len(exposure), len(not_dict),
             len(filtered) + len(translit) + len(exposure) + len(not_dict)))
    print("  К ПРИМЕНЕНИЮ (occ==1 + мед+англ словарь): **%d форм**" % len(safe))
    print("  Отсеянные НЕ потеряны — остаются auto-предсказаниями в LS на ревью человеку.")
    print("  Кандидаты -> %s. НИЧЕГО НЕ ПРИМЕНЕНО." % os.path.relpath(OUT))


if __name__ == "__main__":
    main()
