#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Точка входа CLI единого парсера КР.

Примеры:
    python run.py data\\КР100_2.pdf --profile cr --registry reestr.xlsx --out out
    python run.py data\\ --profile cr --registry reestr.xlsx --out out
"""

from __future__ import annotations

import sys

from crparser.interface.cli import main

if __name__ == "__main__":
    sys.exit(main())
