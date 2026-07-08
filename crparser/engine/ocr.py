#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR-восстановление битого текстового слоя (изолированный модуль).

ВЕСЬ OCR-код живёт здесь. Основной конвейер (pdf_reader/parser/segmenter/stats)
трогает OCR только через `OcrRecoverer` и ТОЛЬКО для файлов-кандидатов — чистые
документы идут прежним путём, без рендера/Tesseract/замедления.

Два режима:

  ГИБРИД (`hybrid_recover`) — кириллица в слое чистая, битая только латиница
    (HBsAg->НВзА§, BCLC->ВСЬС, IgM/IgG->1§М/1§С, Child-Pugh->Ри§Ь). Строки с
    битыми токенами рендерятся по bbox в растр, Tesseract `rus+eng --psm 7`
    восстанавливает латиницу; чистая кириллица берётся ИЗ СЛОЯ (выравнивание по
    кириллическим якорям), восстановленная латиница — из OCR. Меняется только
    Line.text битых строк; bbox/size/bold/структура не трогаются.

  ПОЛНЫЙ (`full_ocr`) — годного текста нет (скан или тотальная порча). Все
    страницы рендерятся, Tesseract `rus+eng --psm 6 tsv` даёт строки с боксами;
    из них собираются Page/Line, которые дальше идут в штатный Segmenter/Stats.

Обнаружение — ТОЛЬКО существующие детекторы `textnorm` (не дублируем эвристику):
битый токен = `_is_section_sign_glyph_token` ИЛИ `_RE_MIXED`; документ-кандидат
по плотностному порогу `section_sign_glyph_tokens`.

Путь к бинарю и TESSDATA_PREFIX — из окружения (`TESSERACT_CMD`/`TESSERACT_PATH`,
`TESSDATA_PREFIX`), с fallback на PATH и стандартные каталоги установки. Падение
Tesseract (нет бинаря / нет rus / ошибка запуска) НЕ роняет парсинг: пишем
предупреждение и возвращаем исходные строки — файл разбирается как прежде.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

import fitz  # PyMuPDF (уже зависимость движка)

from crparser.engine.models import Line, Page
from crparser.engine.textnorm import (
    _RE_MIXED,
    _is_section_sign_glyph_token,
    section_sign_counts,
    section_sign_glyph_tokens,
)

# Рендер: DPI (>=300 по ТЗ), паддинг клипа строки (в пунктах PDF), таймаут вызова.
_DEFAULT_DPI = 300
_CLIP_PAD_PT = 3.0
_TESS_TIMEOUT = 60

# Стандартные места установки Tesseract на Windows/Unix (fallback к env/PATH —
# это НЕ хардкод конкретного файла, а типовые каталоги пакета).
_COMMON_TESS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expanduser(r"~\Tesseract-OCR\tesseract.exe"),
    os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
)

_CYR = re.compile(r"[А-Яа-яЁё]")


def _resolve_tesseract() -> Optional[str]:
    """Путь к бинарю tesseract: env -> PATH -> типовые каталоги. None — нет."""
    for var in ("TESSERACT_CMD", "TESSERACT_PATH"):
        cand = os.environ.get(var)
        if cand and os.path.isfile(cand):
            return cand
    found = shutil.which("tesseract")
    if found:
        return found
    for cand in _COMMON_TESS_PATHS:
        if os.path.isfile(cand):
            return cand
    return None


def _is_broken_token(tok: str) -> bool:
    """Битый токен: впаянный «§» ИЛИ смешение кириллицы и латиницы в слове."""
    return _is_section_sign_glyph_token(tok) or bool(_RE_MIXED.search(tok))


def is_hybrid_candidate(text: str) -> bool:
    """Документ — кандидат на ГИБРИД: плотность «§-в-токене» выше порога.

    Быстрый выход для чистых файлов: без «§» в тексте работы ноль."""
    if "§" not in text:
        return False
    sig, glyph = section_sign_counts(text)
    return section_sign_glyph_tokens(sig, glyph) > 0


def _cyr_key(tok: str, side: str, idx: int):
    """Ключ выравнивания: только кириллические буквы токена (ё->е). Токен без
    кириллицы (латиница/цифры/«§») получает УНИКАЛЬНЫЙ сентинел — он ни с чем не
    совпадёт, значит попадёт в зону замены (там берём OCR-латиницу)."""
    cyr = "".join(ch for ch in tok.lower() if ("а" <= ch <= "я") or ch == "ё")
    cyr = cyr.replace("ё", "е")
    return cyr if cyr else ("\x00" + side, idx)


def merge_line(orig: str, ocr: str) -> str:
    """Слить чистую кириллицу СЛОЯ и восстановленную латиницу OCR.

    Выравниваем по кириллическим якорям: совпавшие (genuine Cyrillic) участки
    берём ИЗ СЛОЯ (orig), несовпавшие — из OCR (там восстановленная латиница).
    Битые латинские токени слоя кириллице OCR не соответствуют -> заменяются;
    настоящая кириллица (её OCR читает как кириллицу) -> совпадает -> сохраняется.
    Пустой OCR -> возвращаем исходную строку (без потерь)."""
    ot = orig.split()
    ct = ocr.split()
    if not ct:
        return orig
    ka = [_cyr_key(t, "o", i) for i, t in enumerate(ot)]
    kb = [_cyr_key(t, "c", i) for i, t in enumerate(ct)]
    sm = SequenceMatcher(None, ka, kb, autojunk=False)
    out: List[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        out.extend(ot[i1:i2] if tag == "equal" else ct[j1:j2])
    merged = " ".join(out).strip()
    return merged or orig


class OcrRecoverer:
    """Восстановление битого текстового слоя через Tesseract (оба режима).

    Экземпляр лёгкий; доступность Tesseract проверяется ЛЕНИВО и кешируется —
    для чистых файлов (не кандидатов) recoverer вообще не трогается."""

    def __init__(self, langs: str = "rus+eng", dpi: int = _DEFAULT_DPI) -> None:
        self._langs = os.environ.get("OCR_LANGS", langs)
        self._dpi = max(int(os.environ.get("OCR_DPI", dpi)), 300)
        self._cmd: Optional[str] = None
        self._checked = False
        self._available = False
        self._tessdata = os.environ.get("TESSDATA_PREFIX") or None
        self.warnings: List[str] = []

    # ---- доступность --------------------------------------------------------

    def available(self) -> bool:
        """Есть ли рабочий Tesseract с нужными языками (rus и eng). Кешируется."""
        if self._checked:
            return self._available
        self._checked = True
        self._cmd = _resolve_tesseract()
        if not self._cmd:
            self.warnings.append(
                "OCR пропущен: tesseract не найден (задайте TESSERACT_CMD)")
            return False
        try:
            langs = self._list_langs()
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"OCR пропущен: ошибка запуска tesseract ({exc!r})")
            return False
        need = {p for p in self._langs.split("+") if p}
        missing = need - langs
        if missing:
            self.warnings.append(
                "OCR пропущен: в tesseract нет traineddata %s "
                "(установите tesseract-ocr-%s)"
                % (sorted(missing), "/".join(sorted(missing))))
            return False
        self._available = True
        return True

    def _list_langs(self) -> set:
        res = subprocess.run(
            [self._cmd, "--list-langs"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=self._env(), timeout=_TESS_TIMEOUT)
        out = (res.stdout or "") + "\n" + (res.stderr or "")
        return {ln.strip() for ln in out.splitlines() if ln.strip() and " " not in ln.strip()}

    def _env(self) -> dict:
        env = dict(os.environ)
        if self._tessdata:
            env["TESSDATA_PREFIX"] = self._tessdata
        return env

    # ---- низкоуровневый вызов Tesseract ------------------------------------

    def _run(self, png: bytes, psm: int, tsv: bool = False) -> str:
        """Прогнать растр (PNG-байты) через Tesseract со stdin. «» при ошибке."""
        args = [self._cmd, "-", "stdout", "-l", self._langs, "--psm", str(psm)]
        if self._tessdata:
            args += ["--tessdata-dir", self._tessdata]
        if tsv:
            args.append("tsv")
        try:
            res = subprocess.run(
                args, input=png, capture_output=True, env=self._env(),
                timeout=_TESS_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"OCR: вызов tesseract не удался ({exc!r})")
            return ""
        return (res.stdout or b"").decode("utf-8", "replace")

    def _render(self, page: "fitz.Page", clip: Optional["fitz.Rect"]) -> bytes:
        """Растр области (или всей страницы) в PNG-байтах при заданном DPI."""
        mat = fitz.Matrix(self._dpi / 72.0, self._dpi / 72.0)
        pix = page.get_pixmap(matrix=mat, clip=clip)
        return pix.tobytes("png")

    # ---- ГИБРИД -------------------------------------------------------------

    def hybrid_recover(self, doc: "fitz.Document", pages: List[Page]) -> int:
        """Починить битые строки на месте (меняем только Line.text). Возвращает
        число восстановленных строк. При недоступном OCR — 0 без изменений.

        Быстрый путь: клипы ВСЕХ битых строк страницы складываются в ОДИН узкий
        растр (вертикальная лента с белыми промежутками) и OCR-ятся одним вызовом
        `--psm 6 tsv`; результат раскладывается обратно по строкам через
        вертикальные полосы-«band». Так на страницу — один запуск Tesseract (а не
        по одному на строку), причём картинка маленькая (только битые строки, не
        вся страница) — на порядок быстрее полностраничного OCR при той же
        точности. Fallback (нет ленты/после слияния осталась «§») — клип строки
        `--psm 7`, гарантирующий уход битых токенов."""
        if not self.available():
            return 0
        fixed = 0
        for page in pages:
            broken = [ln for ln in page.lines
                      if any(_is_broken_token(t) for t in ln.text.split())]
            if not broken:
                continue
            fpage = doc[page.number - 1]
            texts = self._strip_ocr(fpage, broken)
            for line in broken:
                cand = texts.get(id(line), "")
                new_text = merge_line(line.text, cand) if cand else line.text
                if (not cand) or any(_is_broken_token(t) for t in new_text.split()):
                    alt = self._recover_line(fpage, line)   # точечный fallback
                    if alt and not any(_is_broken_token(t) for t in alt.split()):
                        new_text = alt
                if new_text and new_text != line.text:
                    line.text = new_text
                    fixed += 1
            page.text = "\n".join(ln.text for ln in page.lines)
        return fixed

    def _strip_ocr(self, fpage: "fitz.Page", lines: List[Line]) -> dict:
        """OCR ленты из клипов битых строк за один вызов -> {id(line): текст}.

        Клипы складываем вертикально с белым промежутком; по TSV раскидываем слова
        обратно в исходные строки по вертикальной полосе (band). Пустой результат
        при любой ошибке (вызывающий сделает точечный fallback)."""
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001 — без PIL остаётся точечный путь
            return {}
        import io as _io
        gap, scale = 40, self._dpi / 72.0
        imgs, bands, y = [], [], 0
        for line in lines:
            x0, y0, x1, y1 = line.bbox
            if x1 <= x0 or y1 <= y0:
                continue
            clip = fitz.Rect(x0 - _CLIP_PAD_PT, y0 - _CLIP_PAD_PT,
                             x1 + _CLIP_PAD_PT, y1 + _CLIP_PAD_PT)
            try:
                pix = fpage.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip)
                img = Image.open(_io.BytesIO(pix.tobytes("png"))).convert("L")
            except Exception:  # noqa: BLE001
                continue
            imgs.append((y, img, line))
            bands.append((y, y + img.height, line))
            y += img.height + gap
        if not imgs:
            return {}
        width = max(img.width for _, img, _ in imgs)
        canvas = Image.new("L", (width, y), 255)
        for top, img, _ in imgs:
            canvas.paste(img, (0, top))
        buf = _io.BytesIO()
        canvas.save(buf, format="PNG")
        tsv = self._run(buf.getvalue(), psm=6, tsv=True)
        return self._words_to_bands(tsv, bands)

    @staticmethod
    def _words_to_bands(tsv: str, bands: List[Tuple]) -> dict:
        """Разложить слова TSV по полосам bands (по вертикальному центру слова)."""
        acc: dict = {id(ln): [] for _, _, ln in bands}
        for row in tsv.splitlines():
            cols = row.split("\t")
            if len(cols) < 12 or not cols[0].isdigit() or int(cols[0]) != 5:
                continue
            word = cols[11].strip()
            if not word:
                continue
            try:
                top, height = int(cols[7]), int(cols[9])
            except ValueError:
                continue
            cy = top + height / 2.0
            for y0b, y1b, ln in bands:
                if y0b <= cy < y1b:
                    acc[id(ln)].append(word)
                    break
        return {k: " ".join(v) for k, v in acc.items() if v}

    def _recover_line(self, fpage: "fitz.Page", line: Line) -> str:
        x0, y0, x1, y1 = line.bbox
        if x1 <= x0 or y1 <= y0:
            return line.text
        clip = fitz.Rect(x0 - _CLIP_PAD_PT, y0 - _CLIP_PAD_PT,
                         x1 + _CLIP_PAD_PT, y1 + _CLIP_PAD_PT)
        try:
            png = self._render(fpage, clip)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"OCR: рендер строки не удался ({exc!r})")
            return line.text
        ocr = self._run(png, psm=7)
        # OCR может отдать несколько визуальных строк — склеиваем в одну.
        ocr = " ".join(ocr.split())
        if not ocr:
            return line.text
        return merge_line(line.text, ocr)

    # ---- ПОЛНЫЙ -------------------------------------------------------------

    def full_ocr(self, doc: "fitz.Document") -> List[Page]:
        """Собрать страницы из ПОЛНОГО OCR (скан/тотальная порча). При недоступном
        OCR — пустой список (вызывающий сохраняет прежнее поведение)."""
        if not self.available():
            return []
        pages: List[Page] = []
        for index in range(doc.page_count):
            fpage = doc[index]
            try:
                png = self._render(fpage, None)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"OCR: рендер страницы {index + 1} не удался ({exc!r})")
                pages.append(Page(number=index + 1, width=float(fpage.rect.width),
                                  height=float(fpage.rect.height), lines=[], text=""))
                continue
            tsv = self._run(png, psm=6, tsv=True)
            lines = self._lines_from_tsv(tsv, index + 1)
            for i in range(1, len(lines)):
                lines[i].gap_before = lines[i].bbox[1] - lines[i - 1].bbox[1]
            pages.append(Page(
                number=index + 1,
                width=float(fpage.rect.width),
                height=float(fpage.rect.height),
                lines=lines,
                text="\n".join(ln.text for ln in lines)))
        return pages

    def _lines_from_tsv(self, tsv: str, page_no: int) -> List[Line]:
        """Собрать строки из Tesseract TSV: слова группируем по (block,par,line),
        bbox — объединение слов, координаты переводим из пикселей в пункты PDF."""
        scale = 72.0 / self._dpi
        groups: dict = {}
        order: List[Tuple] = []
        for row in tsv.splitlines():
            cols = row.split("\t")
            if len(cols) < 12 or cols[0] == "level" or not cols[0].isdigit():
                continue
            if int(cols[0]) != 5:  # level 5 — слово
                continue
            word = cols[11].strip()
            if not word:
                continue
            key = (cols[2], cols[3], cols[4])  # block, par, line
            try:
                left, top, w, h = (int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]))
            except ValueError:
                continue
            if key not in groups:
                groups[key] = {"words": [], "x0": left, "y0": top,
                               "x1": left + w, "y1": top + h}
                order.append(key)
            g = groups[key]
            g["words"].append(word)
            g["x0"] = min(g["x0"], left)
            g["y0"] = min(g["y0"], top)
            g["x1"] = max(g["x1"], left + w)
            g["y1"] = max(g["y1"], top + h)
        lines: List[Line] = []
        for key in order:
            g = groups[key]
            text = " ".join(g["words"]).strip()
            if not text:
                continue
            size = round((g["y1"] - g["y0"]) * scale, 1)
            lines.append(Line(
                page=page_no,
                text=text,
                bbox=(g["x0"] * scale, g["y0"] * scale, g["x1"] * scale, g["y1"] * scale),
                size=size if size > 0 else 12.0,
                bold=False))
        return lines
