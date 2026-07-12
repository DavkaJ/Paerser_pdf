# -*- coding: utf-8 -*-
"""
Аудит активной базы пинов ocr_pins.json (промпт 02, ПРАВКА 3).

Находит класс ошибок, а не отдельные случаи, глазами:
  (а) пары ключей, где один — префикс/подстрока другого, с НЕСОВМЕСТИМЫМИ целями
      (как "рб1"->PDI против "рб1/рви"->PD1/PD-L1);
  (б) цели с подозрительными гомографами: латинская I/l рядом с цифрами,
      «0» vs «O», «1» vs «l» — типовые OCR-ловушки;
  (в) цели, сами содержащие кириллицу (пин, «чинящий» кириллицу в кириллицу — мусор);
  (г) ключи короче 3 символов.

Печатает полный список подозрительных пар. Ничего не меняет — решение принимается
человеком; однозначные фиксы вносятся в ocr_pins.json, спорные — в pins_disputed.json.

    python _corpus/audit_pins.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "crparser", "data", "ocr_pins.json")

_CYR = re.compile(r"[А-Яа-яЁё]")


def load_map(path):
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict) and "map" in d and "version" in d:
        return d["map"]
    return d


def homograph_suspects(value):
    """Латинские I/l/O рядом с цифрами внутри одного «слова» — вероятная OCR-ловушка."""
    flags = []
    for tok in re.findall(r"\S+", value):
        has_digit = any(c.isdigit() for c in tok)
        # латинская I или l или O внутри токена, где ЕСТЬ цифры -> подозрительно
        if has_digit and re.search(r"[IlO]", tok):
            flags.append(tok)
    return flags


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    m = load_map(BASE)
    keys = list(m)
    print("активная база: %d пар\n" % len(m))

    # (а) префиксные/подстрочные пары с несовместимыми целями
    print("=== (а) ключ — подстрока другого, цели несовместимы ===")
    a_hits = []
    for i, k1 in enumerate(keys):
        for k2 in keys:
            if k1 == k2 or len(k1) < 3:
                continue
            if k1 in k2:                       # k1 — подстрока k2
                v1, v2 = m[k1], m[k2]
                # совместимо, если цель k1 — подстрока цели k2 (без учёта регистра/пробелов)
                n1 = v1.replace(" ", "").lower()
                n2 = v2.replace(" ", "").lower()
                if n1 not in n2 and n2 not in n1:
                    a_hits.append((k1, v1, k2, v2))
    for k1, v1, k2, v2 in sorted(set(a_hits)):
        print("  %-14r -> %-14r  ⊂  %-14r -> %r" % (k1, v1, k2, v2))
    if not a_hits:
        print("  нет")

    # (б) гомографы в целях
    print("\n=== (б) гомографы в целях (латиница I/l/O рядом с цифрами) ===")
    b_hits = [(k, v, homograph_suspects(v)) for k, v in m.items() if homograph_suspects(v)]
    for k, v, toks in sorted(b_hits):
        print("  %-14r -> %-20r  подозрительно: %s" % (k, v, toks))
    if not b_hits:
        print("  нет")

    # (в) кириллица в цели
    print("\n=== (в) цель содержит кириллицу (кир->кир, вероятно мусор) ===")
    c_hits = [(k, v) for k, v in m.items() if _CYR.search(v)]
    for k, v in sorted(c_hits):
        print("  %-14r -> %r" % (k, v))
    if not c_hits:
        print("  нет")

    # (г) короткие ключи
    print("\n=== (г) ключи короче 3 символов ===")
    g_hits = [(k, v) for k, v in m.items() if len(k.strip()) < 3]
    for k, v in sorted(g_hits):
        print("  %-14r -> %r" % (k, v))
    if not g_hits:
        print("  нет")

    print("\nИТОГО подозрительных: а=%d б=%d в=%d г=%d"
          % (len(set(a_hits)), len(b_hits), len(c_hits), len(g_hits)))


if __name__ == "__main__":
    main()
