# -*- coding: utf-8 -*-
"""Проверка аддитивности промпта 10: удалить coverage_v2 из свежих outout/*.json ->
результат обязан БАЙТ-В-БАЙТ совпасть с бэкапом до промпта 10.

    python _corpus/check_additive_10.py <backup_dir>
"""
import glob
import json
import os
import sys


def strip(path):
    d = json.load(open(path, encoding="utf-8"))
    (d.get("stats", {}) or {}).pop("coverage_v2", None)
    return json.dumps(d, ensure_ascii=False, indent=2)


def main():
    backup = sys.argv[1]
    diffs, missing, ok = [], [], 0
    for jf in sorted(glob.glob(os.path.join("outout", "*.json"))):
        b = os.path.basename(jf)
        bk = os.path.join(backup, b)
        if not os.path.exists(bk):
            missing.append(b)
            continue
        new_stripped = strip(jf)
        old = open(bk, encoding="utf-8").read()
        if new_stripped == old:
            ok += 1
        else:
            diffs.append(b)
    print("байт-в-байт после strip(coverage_v2): %d" % ok)
    print("РАСХОЖДЕНИЯ (не аддитивно!): %d %s" % (len(diffs), diffs[:20]))
    print("нет в бэкапе: %d %s" % (len(missing), missing[:20]))
    # доп.: проверить, что coverage_v2 реально есть во всех свежих
    no_v2 = [os.path.basename(jf) for jf in glob.glob(os.path.join("outout", "*.json"))
             if "coverage_v2" not in (json.load(open(jf, encoding="utf-8"))
                                      .get("stats", {}) or {})]
    print("без coverage_v2 в свежих: %d %s" % (len(no_v2), no_v2[:20]))


if __name__ == "__main__":
    main()
