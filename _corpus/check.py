# -*- coding: utf-8 -*-
"""Быстрая проверка отдельных файлов (не трогает report.json/outout).

    python _corpus/check.py КР1039_1 КР1021_1 ...
"""
import glob, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from crparser.engine.parser import DocumentParser
from crparser.engine.jsonio import JsonWriter
from crparser.profiles import create_profile
from validate import validate_doc, parse_toc

reg = glob.glob("*.xlsx")[0]
p = DocumentParser(create_profile("cr", reg))
w = JsonWriter()
for a in sys.argv[1:]:
    b = os.path.splitext(os.path.basename(a))[0]
    pdf = os.path.join("data", "raw", b + ".pdf")
    d = w.to_dict(p.parse(pdf))
    r = validate_doc(b + ".pdf", d, parse_toc(pdf))
    status = "SKIP" if r.skipped else ("PASS" if r.ok else "FAIL")
    print("[%s] %-12s cov=%s sec=%s tab=%s" %
          (status, b, d["stats"]["coverage_percent"], d["stats"]["sections_found"],
           d["stats"]["tables_found"]))
    for f in r.fails:
        print("    X", f[:130])
