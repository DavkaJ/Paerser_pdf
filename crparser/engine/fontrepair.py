#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Font/cmap repair (промпт 13): чинит НЕВЕРНЫЙ МАППИНГ /ToUnicode по КОНТУРАМ глифов.

Идея (уровень ШРИФТА, не спана): встроенный субсет — это Times/Arial, поэтому контур
глифа идентичен эталонному. Для каждого встроенного шрифта ОДИН РАЗ:
  1. хешируем контур каждого глифа (с декомпозицией композитов);
  2. сопоставляем с эталонами (times/timesbd/timesi/timesbi/arial*) -> GID -> истинный
     символ (множество при гомографах: латинская C и кириллическая С — один контур);
  3. сравниваем с картой /ToUnicode этого шрифта -> ДОЛЯ СОГЛАСИЯ.

**Гейт false_substitution <= 0.001 держится ПО ПОСТРОЕНИЮ:** у здорового шрифта карты
совпадают (agreement ~1.0) -> ремонт НЕ применяется -> испортить здоровый текст структурно
невозможно. Замер: КР1000_1 (здоров) 0.97-1.0, КР1_4 (битый) 0.00-0.01 — разрыв огромен.

Модуль НЕ трогает segmenter/tables/validate/схему. Даёт декодер, применяемый к тексту
БИТОГО шрифта вместо лживого /ToUnicode. Гомографы разрешаются по алфавиту строки.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from typing import Dict, FrozenSet, List, Optional, Tuple

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import DecomposingRecordingPen

# Эталонные шрифты Windows (реально используемые в КР): Times + Arial, все начертания.
_REF_FONT_FILES = ("times", "timesbd", "timesi", "timesbi",
                   "arial", "arialbd", "ariali", "arialbi")
_WIN_FONTS = os.environ.get("WINDIR", r"C:\Windows") + os.sep + "Fonts"

# Порог согласия карт: ниже — шрифт битый, применяем контурную карту; выше — здоров,
# НЕ трогаем. Замер (КР1000_1 vs КР1_4): здоровые >=0.97, битые <=0.01 — разрыв огромный,
# 0.85 лежит в пустой зоне. MIN_SAMPLE — минимум глифов с эталоном, чтобы доверять доле.
AGREEMENT_HEALTHY = 0.85
MIN_SAMPLE = 12

_ref_cache: Optional[Dict[str, FrozenSet[str]]] = None


def _ohash(glyphset, gname: str, upm: int) -> Optional[str]:
    """sha1 нормализованного контура глифа (масштаб к 1000/upm, позиция к минимуму).
    Композиты ДЕКОМПОЗИРУЮТСЯ (кириллица в Times часто ссылается на латинский двойник)."""
    if gname not in glyphset:
        return None
    pen = DecomposingRecordingPen(glyphset)
    try:
        glyphset[gname].draw(pen)
    except Exception:  # noqa: BLE001
        return None
    pts = [a for _, args in pen.value for a in (args or []) if isinstance(a, tuple)]
    if not pts:
        return None
    minx = min(p[0] for p in pts)
    miny = min(p[1] for p in pts)
    s = 1000.0 / upm
    norm = [(cmd, tuple((round((a[0] - minx) * s), round((a[1] - miny) * s))
                        if isinstance(a, tuple) else a for a in args))
            for cmd, args in pen.value]
    return hashlib.sha1(repr(norm).encode()).hexdigest()[:16]


def _reference_table() -> Dict[str, FrozenSet[str]]:
    """{контур-хеш: множество символов} по всем эталонным начертаниям. Множество>1 =
    гомограф (латиница/кириллица с одинаковым контуром)."""
    global _ref_cache
    if _ref_cache is not None:
        return _ref_cache
    acc: Dict[str, set] = {}
    for name in _REF_FONT_FILES:
        path = os.path.join(_WIN_FONTS, name + ".ttf")
        if not os.path.exists(path):
            continue
        try:
            tt = TTFont(path)
            upm = tt["head"].unitsPerEm
            gs = tt.getGlyphSet()
            for uni, gname in (tt.getBestCmap() or {}).items():
                if 0x20 <= uni < 0x500 or 0x2160 <= uni < 0x2190:  # latin+cyr + roman num
                    h = _ohash(gs, gname, upm)
                    if h:
                        acc.setdefault(h, set()).add(chr(uni))
        except Exception:  # noqa: BLE001
            continue
    _ref_cache = {h: frozenset(cs) for h, cs in acc.items()}
    return _ref_cache


def _parse_tounicode(data: bytes) -> Dict[int, str]:
    """Разобрать /ToUnicode CMap: code(=GID для Identity-H) -> unicode-строка."""
    t = data.decode("latin-1")
    m: Dict[int, str] = {}
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", t, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            m[int(src, 16)] = "".join(chr(int(dst[i:i + 4], 16))
                                      for i in range(0, len(dst), 4))
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", t, re.S):
        for s, e, d in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            si, ei, base = int(s, 16), int(e, 16), int(d, 16)
            for i in range(ei - si + 1):
                m[si + i] = chr(base + i)
    return m


class _FontInfo:
    """Разбор одного встроенного шрифта: контурная карта, tu-карта, доля согласия."""

    __slots__ = ("contour", "tu", "agreement", "sample", "corrupt")

    def __init__(self, contour: Dict[int, FrozenSet[str]], tu: Dict[int, str]) -> None:
        self.contour = contour
        self.tu = tu
        agree = dis = 0
        for gid, cand in contour.items():
            tuc = tu.get(gid)
            if tuc is None:
                continue
            if tuc in cand:
                agree += 1
            else:
                dis += 1
        self.sample = agree + dis
        self.agreement = (agree / self.sample) if self.sample else 1.0
        # битый = достаточно доказательств И низкое согласие
        self.corrupt = self.sample >= MIN_SAMPLE and self.agreement < AGREEMENT_HEALTHY


class FontRepairer:
    """Анализирует шрифты документа и декодирует текст БИТЫХ по контурам."""

    def __init__(self, doc) -> None:
        self._doc = doc
        self._fonts: Dict[int, _FontInfo] = {}
        self._ref = _reference_table()

    def _font(self, xref: int) -> Optional[_FontInfo]:
        if xref in self._fonts:
            return self._fonts[xref]
        info = self._build(xref)
        self._fonts[xref] = info
        return info

    def _build(self, xref: int) -> Optional[_FontInfo]:
        try:
            _, ext, _, buf = self._doc.extract_font(xref)
            if not buf or ext != "ttf":
                return None
            sub = TTFont(io.BytesIO(buf))
            upm = sub["head"].unitsPerEm
            gs = sub.getGlyphSet()
            go = sub.getGlyphOrder()
            contour: Dict[int, FrozenSet[str]] = {}
            for gid, gname in enumerate(go):
                cand = self._ref.get(_ohash(gs, gname, upm))
                if cand:
                    contour[gid] = cand
            tk = self._doc.xref_get_key(xref, "ToUnicode")
            tu: Dict[int, str] = {}
            if tk and tk[0] == "xref":
                tu = _parse_tounicode(self._doc.xref_stream(int(tk[1].split()[0])))
            return _FontInfo(contour, tu)
        except Exception:  # noqa: BLE001
            return None

    def font_report(self, xref: int) -> Optional[Dict]:
        info = self._font(xref)
        if info is None:
            return None
        return {"agreement": round(info.agreement, 4), "sample": info.sample,
                "corrupt": info.corrupt}

    def decode_span(self, xref: int, gids: List[int], tu_text: str) -> Tuple[str, bool]:
        """Вернуть (текст, применён_ли_ремонт) для последовательности глифов ОДНОГО
        шрифта. Здоровый/неизвестный шрифт -> tu_text без изменений (ремонт НЕ применён —
        так гейт false_substitution держится по построению). Битый -> контурная карта,
        гомографы разрешаются по доминантному алфавиту неоднозначно-однозначных символов."""
        info = self._font(xref)
        if info is None or not info.corrupt:
            return tu_text, False
        # 1-й проход: однозначные символы -> доминантный алфавит. Битый шрифт в этих
        # документах несёт ЛАТИНСКИЙ контент (термины/коды, отрендеренные кир-двойником),
        # поэтому при СПОРНОМ балансе и при равенстве предпочитаем латиницу (bias),
        # но явное преобладание кириллицы уважаем (не над-латинизируем).
        lat = cyr = 0
        for gid in gids:
            cand = info.contour.get(gid)
            if cand and len(cand) == 1:
                ch = next(iter(cand))
                if "a" <= ch.lower() <= "z":
                    lat += 1
                elif "а" <= ch.lower() <= "я" or ch in "ёЁ":
                    cyr += 1
        prefer_lat = lat * 2 >= cyr
        # 2-й проход: собрать строку, гомографы -> предпочтительный алфавит
        out: List[str] = []
        for i, gid in enumerate(gids):
            cand = info.contour.get(gid)
            if not cand:
                # нет контура -> оставить символ ToUnicode (не угадываем)
                out.append(tu_text[i] if i < len(tu_text) else "")
                continue
            if len(cand) == 1:
                out.append(next(iter(cand)))
            else:
                out.append(_resolve_homograph(cand, prefer_lat))
        return "".join(out), True


def _resolve_homograph(cand: FrozenSet[str], prefer_lat: bool) -> str:
    """Из набора гомографов выбрать по алфавиту строки. Латиница приоритетна для
    латинских сущностей (TNM/МКБ/ATC); при равенстве — латиница (чаще именно она
    отрендерена кириллическим двойником в этих документах)."""
    lat = [c for c in cand if ("a" <= c.lower() <= "z") or c.isdigit()]
    cyr = [c for c in cand if "а" <= c.lower() <= "я" or c in "ёЁ"]
    pool = (lat or cyr) if prefer_lat else (cyr or lat)
    if not pool:
        pool = sorted(cand)
    # детерминированно: короче код -> раньше (стабильный выбор)
    return sorted(pool)[0]
