#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR-восстановление битого текстового слоя (изолированный модуль).

ВЕСЬ OCR-код живёт здесь. Основной конвейер (pdf_reader/parser/segmenter/stats)
трогает OCR только через `OcrRecoverer` и ТОЛЬКО для файлов-кандидатов — чистые
документы идут прежним путём, без рендера/Tesseract/замедления.

Два режима:

  ГИБРИД (`hybrid_recover`) — кириллица в слое ЧИСТАЯ, битая только латиница,
    отрендеренная кириллицей-двойником (BCLC->ВСЬС, Liver->Ыуег, Hepatitis->Нерабб,
    virus->У1гиз, HBsAg->НВзА§, Child-Pugh->Ри§Ь). Для кандидата OCR-ятся ВСЕ
    страницы (rus+eng), каждая строка слоя сливается со своей OCR-строкой ТОКЕН-
    УРОВНЕВО: нативная кириллица — АВТОРИТЕТНАЯ база (её OCR по-русски portит ё/е,
    переносы, пунктуацию), OCR-латиница подставляется ТОЛЬКО на «битые двойники»
    (токен с §/мешанина, либо подтверждённый глиф-двойник из карты документа).
    Реальные русские слова, аббревиатуры (ТАХЭ/УЗИ/СНВС), фамилии — не трогаются.
    Меняется только Line.text; bbox/size/bold/структура не трогаются.

  ПОЛНЫЙ (`full_ocr`) — годного текста нет (скан или тотальная порча). Все
    страницы рендерятся, Tesseract `rus+eng --psm 6 tsv` даёт строки с боксами;
    из них собираются Page/Line, которые дальше идут в штатный Segmenter/Stats.

Карта «кривое->чистое» строится из СЛОВАРЯ «Список сокращений» (регион OCR-ится и
сопоставляется по позиции) + статистики двойников по телу + КРОСС-ДОК базы пинов
(ocr_pins). Новые двойники документа пишутся в шард для пополнения базы.

Обнаружение — ТОЛЬКО существующие детекторы `textnorm` (не дублируем эвристику):
битый токен = `_is_section_sign_glyph_token` ИЛИ `_RE_MIXED`; документ-кандидат
по `section_sign_glyph_tokens` (§-плотность) ИЛИ по якорям базы пинов.

Путь к бинарю и TESSDATA_PREFIX — из окружения (`TESSERACT_CMD`/`TESSERACT_PATH`,
`TESSDATA_PREFIX`), с fallback на PATH и стандартные каталоги установки. Падение
Tesseract (нет бинаря / нет rus / ошибка запуска) НЕ роняет парсинг: тихо
пропускаем — файл разбирается как без OCR.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF (уже зависимость движка)

from crparser.engine.jsonio import JsonWriter
from crparser.engine.models import Line, Page
from crparser.engine.textnorm import (
    _RE_MIXED,
    _is_section_sign_glyph_token,
    section_sign_counts,
    section_sign_glyph_tokens,
)

# Рендер DPI. Поднят с 300 до 400: плотный курсивный глоссарий («Список
# сокращений»/«Термины и определения») на 300 dpi читался Tesseract-ом мусорно.
# Порог env OCR_DPI ниже 300 не опускается. Ключ дискового кэша OCR включает DPI и
# версию препроцессинга — их смена автоматически инвалидирует кэш.
_DEFAULT_DPI = 400
# Версия растрового препроцессинга (grayscale + autocontrast + unsharp + опц.
# апскейл). Инкрементировать при ЛЮБОЙ правке `_prep_png`/`_render`, иначе кэш
# отдаст растр, снятый старым препроцессингом.
_PREPROC_VERSION = 1
_CLIP_PAD_PT = 3.0
_TESS_TIMEOUT = 120

# Дисковый кэш сырого результата Tesseract (TSV) по (id+страница+dpi+langs+psm+
# версия препроцессинга). Каталог рабочий — в .gitignore. Радикально ускоряет
# повторные прогоны на тех же файлах (прошлый полный прогон шёл ~2 часа).
# Путь кэша параметризуем через env CR_OCR_CACHE (промпт 05, правка 7): два агента
# в разных worktree не должны делить один каталог кэша. По умолчанию — прежний путь.
_OCR_CACHE_DIR = os.environ.get("CR_OCR_CACHE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ocr_cache")

# --------------------------------------------------------------------------- #
# Отпечаток стека для content-addressed кэша (промпт 02, ПРАВКА 2).            #
# Ключ дискового кэша ОБЯЗАН зависеть от ВСЕГО, что влияет на результат: версии #
# Tesseract, хеша traineddata, версии Pillow, dpi, версии препроцессинга, langs.#
# Раньше ключ был basename+page+dpi+langs+preproc+psm — без хеша PDF и без       #
# версии стека: замена PDF при том же имени или апгрейд Tesseract молча отдавали #
# старый TSV. Теперь имя файла = <pdf_sha12>_p<page>_<stack_sha12>_psm<N>.json.  #
# --------------------------------------------------------------------------- #
_stack_sig_cache: Dict[tuple, str] = {}


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def _traineddata_dirs(cmd: Optional[str], tessdata: Optional[str]) -> list:
    dirs = []
    if tessdata:
        dirs.append(tessdata)
    if cmd:
        bindir = os.path.dirname(cmd)
        dirs.append(os.path.join(bindir, "tessdata"))
        dirs.append(os.path.join(os.path.dirname(bindir), "share", "tessdata"))
    return dirs


def _stack_signature(cmd: Optional[str], tessdata: Optional[str], langs: str,
                     dpi: int, preproc: int) -> str:
    """Короткий (12 hex) отпечаток стека OCR: версия Tesseract + sha используемых
    traineddata + версия Pillow + dpi + langs + версия препроцессинга. Кешируется
    на процесс. При смене любого компонента ключ кэша меняется -> пересчёт."""
    key = (cmd, tessdata, langs, dpi, preproc)
    cached = _stack_sig_cache.get(key)
    if cached is not None:
        return cached
    parts = ["dpi=%d" % dpi, "langs=%s" % langs, "preproc=%d" % preproc]
    # версия Tesseract
    tver = None
    if cmd:
        try:
            out = subprocess.run([cmd, "--version"], capture_output=True, text=True,
                                 timeout=_TESS_TIMEOUT)
            lines = (out.stdout or out.stderr or "").splitlines()
            tver = lines[0].strip() if lines else None
        except Exception:  # noqa: BLE001
            tver = None
    parts.append("tess=%s" % tver)
    # версия Pillow
    try:
        import PIL
        parts.append("pillow=%s" % getattr(PIL, "__version__", None))
    except Exception:  # noqa: BLE001
        parts.append("pillow=None")
    # sha256 используемых traineddata
    dirs = _traineddata_dirs(cmd, tessdata)
    for lang in sorted({p for p in langs.split("+") if p}):
        sha = None
        for d in dirs:
            p = os.path.join(d, lang + ".traineddata")
            if os.path.isfile(p):
                sha = _file_sha256(p)
                break
        parts.append("%s=%s" % (lang, sha))
    sig = _sha256_hex("|".join(parts).encode("utf-8"))[:12]
    _stack_sig_cache[key] = sig
    return sig

_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
_UPCYR = re.compile(r"[А-ЯЁ]")
# Точки/подчёркивания-лидеры оглавления: строки-ToC OCR читает мусорно — их слияние
# с OCR не ведём (только пред-разрешение подтверждённых двойников безопасно).
_LEAD = re.compile(r"\.{3,}|_{3,}")
# Якорь региона «Список сокращений»; конец — «Термины и определения» ИЛИ первый
# нумерованный раздел (те же структурные маркеры, что у toc._BODY_MARKERS).
_ABBR_ANCHOR = re.compile(r"^\s*список\s+сокращ", re.IGNORECASE)
_TERM_ANCHOR = re.compile(r"^\s*(термин\w*\s+и\s+определ|\d)", re.IGNORECASE)
# Якорь начала библиографии: с него и до конца документа (references + приложения)
# — ВНЕ ФОКУСА (тело/глоссарий). Двойники оттуда НЕ учим (многоязычная библиография
# даёт мусорные пары), чтобы не засорять карту/базу и не «дочищать» библиографию.
_REFS_ANCHOR = re.compile(r"^\s*список\s+литератур", re.IGNORECASE)
# Запись словаря «КЛЮЧ - определение»/«КЛЮЧ — определение».
_DASH = re.compile(r"\s[-–—]\s")
_TOK_PUNCT = ".,;:()[]«»\"'`-—%<>±*"
# Чистая латинская форма-восстановление: буква в начале, дальше латиница/цифры/. - /
# (мусор с апострофами/кавычками — русская ошибка OCR, не латинский двойник).
_CLEAN_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9./-]*$")
# Минимум наблюдений «кривое->чистое», чтобы принять двойник из статистики тела.
_MIN_DOUBLE_HITS = 2

# Сид доверенных ЧИСТЫХ форм тела (аналог base_values): двойник, чью доминантную
# чистую форму OCR дал в теле/глоссарии И она входит в сид, принимаем даже при
# ОДНОМ вхождении (закрывает hapax-термины тела: 8ТКШЕ->STRIDE, УЕСР->VEGF,
# УЕСРК->VEGFR, РЭ-Ы->PD-L1). glyph_signal при этом сохраняется (не разблокируем
# короткие/строчные — те чиним только пином).
_SEED_CLEAN = frozenset({
    "PD-L1", "PD-1", "PD1", "VEGF", "VEGFR", "CTLA4", "HBsAg", "HBs", "TNM",
    "BCLC", "ECOG", "RECIST", "mRECIST", "vs", "mTOR", "STRIDE", "IMbrave150",
})

# Одиночные кириллические буквы-двойники латиницы: в «Hepatitis С virus» буква «С»
# кириллическая, а по смыслу — латинская «C». Меняем ТОЛЬКО в зоне-замене (OCR
# прочитал позицию латиницей) и ТОЛЬКО когда OCR дал ровно этот латинский аналог, И
# в зоне есть латинский контекст (многобуквенный лат. токен). Русские предлоги
# «в/с/о/к/у» читаются кириллицей -> зона equal -> не затрагиваются. Строчную «в»
# в карту НЕ включаем (частый предлог, слабое сходство глифа).
_CYR2LAT = {
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M",
    "О": "O", "Р": "P", "Т": "T", "У": "Y", "Х": "X",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
}
# Только прописные двойники — для контекстной латинизации зажатой буквы (см.
# `_latinize_flanked`): строчные предлоги в/с/о/к/у из неё исключены.
_CYR2LAT_UP = {k: v for k, v in _CYR2LAT.items() if k.isupper()}


# ============================================================================
# Резолвинг Tesseract + предикаты токенов
# ============================================================================

def _resolve_tesseract() -> Optional[str]:
    """Путь к бинарю tesseract: ТОЛЬКО env (TESSERACT_CMD/TESSERACT_PATH) или PATH.

    Промпт 05, правка 3-bis: молчаливый фолбэк на захардкоженные домашние каталоги
    УБРАН. Он превращал доступность OCR в «удачу среды»: на машине без Tesseract на
    PATH, но с ~/Tesseract-OCR/, батч молча включал OCR и производил ДРУГОЙ корпус,
    завершаясь с exit 0. Теперь OCR — ОБЪЯВЛЕННОЕ предусловие прогона: не найден по
    env/PATH -> None, и batch_report с --require-ocr останавливается (exit 2), а не
    угадывает домашнюю директорию."""
    for var in ("TESSERACT_CMD", "TESSERACT_PATH"):
        cand = os.environ.get(var)
        if cand and os.path.isfile(cand):
            return cand
    return shutil.which("tesseract")


def _tok_core(tok: str) -> str:
    return tok.strip(_TOK_PUNCT)


def _has_lat(s: str) -> bool:
    return bool(_LAT.search(s))


def _has_cyr(s: str) -> bool:
    return bool(_CYR.search(s))


def _has_digit(s: str) -> bool:
    return any(ch.isdigit() for ch in s)


def _cyr_letters(s: str) -> int:
    return sum(1 for ch in s if _CYR.match(ch))


def _is_clean_latin(s: str) -> bool:
    return bool(_CLEAN_LATIN.match(s))


def _is_compound(tok: str) -> bool:
    """Токен склеен из компонентов через дефис/тире (Child—Pugh, анти-СТЬА4)."""
    return any(ch in _tok_core(tok) for ch in "-—–")


# Разделитель компонентов склеенного токена — ТОЛЬКО дефис (как в прежнем
# _hyphen_resolve). Слэш НЕ разделяем: слэш-склейки (Р01/РБ-Ы) чинятся ПИНОМ по
# полной кир-норме токена («р01/рбы»->«PD1/PD-L1»), а не покомпонентно, — иначе
# слэш-токены списка литературы («саге/Сапсег», «У/ап§») правились бы как двойники.
_COMPOUND_SEP = re.compile(r"(-)")


def _is_native_ru(tok: str) -> bool:
    """Компонент — НОРМАЛЬНАЯ русская морфема, а не глиф-двойник латиницы.

    Жёсткий инвариант против латинизации русского: такой компонент берётся из
    НАТИВНОГО слоя ДОСЛОВНО и никогда не подменяется чтением OCR. Признак: чистая
    кириллица (без латиницы/цифр/«§»), длина >=3 и есть СТРОЧНАЯ кириллическая
    буква — т.е. слово/морфема («терапии», «положительного», «ингибитора»), а не
    прописная аббревиатура-двойник (ВСЬС/СЫЫ) и не битый токен (Ьпд§е)."""
    core = _tok_core(tok)
    if len(core) < 3 or _has_lat(core) or _has_digit(core) or "§" in core:
        return False
    if not _has_cyr(core):
        return False
    return any(("а" <= ch <= "я") or ch == "ё" for ch in core)


def _latinize_flanked(text: str) -> str:
    """Одиночная ПРОПИСНАЯ кириллица-двойник латиницы, зажатая между двумя чистыми
    многобуквенными латинскими токенами («Hepatitis В virus» -> «Hepatitis B virus»),
    латинизируется. OCR сам читает такую «В/С» кириллицей (в мед-тексте «гепатит В»
    двусмыслен), поэтому чиним пост-проходом по КОНТЕКСТУ. Только ПРОПИСНЫЕ двойники
    (_CYR2LAT_UP) и только с ЛАТ. соседями с ОБЕИХ сторон — строчные предлоги
    (в/с/о/к/у) и одиночные буквы среди кириллицы не затрагиваются."""
    toks = text.split()
    if len(toks) < 3:
        return text
    changed = False
    for i in range(1, len(toks) - 1):
        core = _tok_core(toks[i])
        if len(core) == 1 and core in _CYR2LAT_UP:
            prev, nxt = _tok_core(toks[i - 1]), _tok_core(toks[i + 1])
            if (len(prev) >= 2 and _is_clean_latin(prev)
                    and len(nxt) >= 2 and _is_clean_latin(nxt)):
                toks[i] = _sub(toks[i], _CYR2LAT_UP[core])
                changed = True
    return " ".join(toks) if changed else text


def _is_broken_token(tok: str) -> bool:
    """Битый токен: впаянный «§» ИЛИ смешение кириллицы и латиницы в слове."""
    return _is_section_sign_glyph_token(tok) or bool(_RE_MIXED.search(tok))


def _cyr_norm(tok: str) -> str:
    """Нормализованное «ядро» кириллического токена: буквы/цифры/«/»/«§», нижний
    регистр, ё->е. Ключ карты двойников (совпадает с формой ключей базы пинов).
    Цифры, «/» и «§» сохраняем: «у1гиз», «кт/мрт», «ри§ь», «нвза§» — различимы и
    «§» держит длину ключа (иначе глиф-сигнал длины срезал бы §-двойники)."""
    core = _tok_core(tok).lower().replace("ё", "е")
    return "".join(ch for ch in core if _CYR.match(ch) or ch.isdigit() or ch in "/§")


def _numberish(value: str) -> bool:
    """OCR-значение — почти число (для случая «Ю0»->«100»)."""
    core = _tok_core(value)
    return bool(core) and sum(1 for ch in core if ch.isdigit()) / len(core) >= 0.6


def _glyph_signal(key_core: str, value: str, has_upper: bool) -> bool:
    """Структурный признак: кириллический токен — это латинский глиф-двойник, а не
    русское слово. Слэш-совмещённые (КТ/МРТ) — русские сокращения, не двойники;
    с цифрой внутри — двойник при >=2 кир. буквах ИЛИ числовом значении (Ю0->100);
    без цифры — ПРОПИСНОЙ токен длиной >=3 (ВСЬС/Ыуег/СЫЫ->Child/Риф->Pugh).
    Порог длины опущен с 4 до 3, чтобы ловить короткие прописные двойники (СЫЫ/Риф);
    это безопасно, потому что реальные рус. сокращения (ГЦР/ДНК/УЗИ) OCR читает
    кириллицей -> eq>0 -> отсекаются выше по стеку, а в latc вообще не попадают
    (нет чистой латиницы в паре). Короткие СТРОЧНЫЕ служебные слова — нет has_upper."""
    if "/" in key_core and not _has_digit(key_core):
        return False
    if _has_digit(key_core):
        return _cyr_letters(key_core) >= 2 or _numberish(value)
    return len(key_core) >= 3 and has_upper


def is_hybrid_candidate(text: str, base: Optional[dict] = None) -> bool:
    """Документ — кандидат на кирилло-латинскую глиф-порчу.

    Триггеры: (1) плотность «§-в-токене» выше порога (section_sign_glyph_tokens);
    (2) в тексте есть кривые якоря из кросс-док базы пинов (двойники ВСЬС/Ыуег и
    без «§»). `looks_glyph_corrupted` как гейт НЕ используем: он True и на ЧИСТЫХ
    файлах с легит-латиницей (IgG/TNM — смешение скриптов), это сломало бы
    регрессию чистых. Чистый файл без «§» и без якорей базы -> False, работы ноль."""
    if "§" in text:
        sig, glyph = section_sign_counts(text)
        if section_sign_glyph_tokens(sig, glyph) > 0:
            return True
    from crparser.engine import ocr_pins
    return ocr_pins.anchor_hit(text, base)


# ============================================================================
# Выравнивание строк слой<->OCR и слияние (нативная кириллица авторитетна)
# ============================================================================

def _cyr_key(tok: str, side: str, idx: int):
    """Ключ выравнивания: только кириллические буквы токена (ё->е). Токен без
    кириллицы (латиница/цифры/«§») получает УНИКАЛЬНЫЙ сентинел — он ни с чем не
    совпадёт, значит попадёт в зону замены (там берём OCR-латиницу)."""
    cyr = "".join(ch for ch in tok.lower() if ("а" <= ch <= "я") or ch == "ё")
    cyr = cyr.replace("ё", "е")
    return cyr if cyr else ("\x00" + side, idx)


def _align_ops(orig: str, ocr: str):
    ot, ct = orig.split(), ocr.split()
    ka = [_cyr_key(t, "o", i) for i, t in enumerate(ot)]
    kb = [_cyr_key(t, "c", i) for i, t in enumerate(ct)]
    return ot, ct, SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes()


def merge_line(orig: str, ocr: str) -> str:
    """Слить чистую кириллицу СЛОЯ и восстановленную латиницу OCR (зонно).

    Выравниваем по кириллическим якорям: совпавшие (genuine Cyrillic) участки
    берём ИЗ СЛОЯ (orig), несовпавшие — из OCR (там восстановленная латиница).
    Пустой OCR -> возвращаем исходную строку. НЕ ослаблять: используется как
    точечный fallback (_recover_line)."""
    ot = orig.split()
    ct = ocr.split()
    if not ct:
        return orig
    _, _, ops = _align_ops(orig, ocr)
    out: List[str] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            out.extend(ot[i1:i2])
            continue
        lz, cz = ot[i1:i2], ct[j1:j2]
        if len(lz) == len(cz):
            # инвариант: нативную русскую морфему в зоне-замене НЕ латинизируем —
            # берём её из слоя, OCR-латиницу ставим только на не-русские токены.
            out.extend(lk if _is_native_ru(lk) else ck for lk, ck in zip(lz, cz))
        else:
            out.extend(cz)
    merged = " ".join(out).strip()
    return merged or orig


def _sub(tok: str, val: str) -> str:
    """Подставить чистое значение вместо «ядра» токена, сохранив внешнюю пунктуацию."""
    core = _tok_core(tok)
    return tok.replace(core, val, 1) if core else val


def _resolve_line(orig: str, ocr: str, doc_map: Dict[str, str],
                  genuine: frozenset = frozenset()) -> str:
    """ТОКЕН-УРОВНЕВОЕ восстановление строки. Заменяем ТОЛЬКО: (а) подтверждённый
    глиф-двойник из doc_map (в любой зоне); (б) собственно битый токен (§/мешанина)
    — по doc_map или позиционной OCR-латинице внутри своей зоны замены. Все прочие
    токены (нативная кириллица, реальные слова) — из слоя, авторитетно. `genuine` —
    кир-нормы генуинно-русских токенов (eq>0), которые в битых склейках не латинизируем."""
    if not ocr.split():
        return _pre_resolve(orig, doc_map)
    ot, ct, ops = _align_ops(orig, ocr)
    out: List[str] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            out.extend(ot[i1:i2])
            continue
        lz, cz = ot[i1:i2], ct[j1:j2]
        if len(lz) == len(cz):                       # позиционное сопоставление
            for lk, ck in zip(lz, cz):
                low = _cyr_norm(lk)
                if _has_cyr(lk) and not _has_lat(lk) and low in doc_map:
                    out.append(_sub(lk, doc_map[low]))
                elif _is_broken_token(lk):
                    if low in doc_map:
                        out.append(_sub(lk, doc_map[low]))
                    else:
                        # СКЛЕЙКА (Ьпд§е-терапии) — покомпонентно, русское из слоя;
                        # None -> одиночный битый токен -> чтение OCR целиком.
                        comp = _resolve_broken_compound(lk, ck, doc_map, genuine)
                        out.append(comp if comp is not None
                                   else (ck if _is_clean_latin(_tok_core(ck)) else lk))
                else:
                    out.append(lk)                   # нативный токен — из слоя
        else:                                        # разной длины: латиница по порядку
            lat = [c for c in cz if _is_clean_latin(_tok_core(c))]
            li = 0
            for lk in lz:
                low = _cyr_norm(lk)
                if _has_cyr(lk) and not _has_lat(lk) and low in doc_map:
                    out.append(_sub(lk, doc_map[low]))
                elif _is_broken_token(lk):
                    if low in doc_map:
                        out.append(_sub(lk, doc_map[low]))
                    else:
                        # СКЛЕЙКА -> покомпонентно (без позиц. OCR: зоны разной
                        # длины), русское из слоя; None (одиночный) -> латиница OCR.
                        comp = _resolve_broken_compound(lk, "", doc_map, genuine)
                        if comp is not None:
                            out.append(comp)
                        elif li < len(lat):
                            out.append(lat[li]); li += 1
                        # иначе битый токен без соответствия — отбрасываем
                else:
                    out.append(lk)                   # нативный токен — из слоя
    return _pre_resolve(" ".join(out), doc_map)


def _reassemble(pieces: List[Tuple[int, str]], seps: List[str]) -> str:
    """Собрать компоненты обратно, ставя ИСХОДНЫЙ разделитель перед каждым (кроме
    первого). `pieces` — список (индекс_первого_компонента, текст); разделитель
    перед пробегом, начинающимся с компонента a, — это seps[a-1]."""
    s = ""
    for idx, (a, text) in enumerate(pieces):
        if idx > 0:
            s += seps[a - 1]
        s += text
    return s


def _hyphen_resolve(tok: str, doc_map: Dict[str, str]) -> str:
    """Двойник, склеенный с рус. морфемами через дефис в ЛЮБОЙ позиции (начало,
    середина, хвост): «анти-СТЬА4»->«анти-CTLA4», «РБ-Ы-ингибитора»->
    «PD-L1-ингибитора», «апб-НСУ-определение»->«anti-HCV-определение». Идём слева
    направо, на каждой позиции берём МАКСИМАЛЬНЫЙ ПРОБЕГ дефис-компонентов, чья
    кир-норма — ПОЛНЫЙ ключ карты, заменяем его чистой формой, русские компоненты
    оставляем как есть. Замена по дефис-ГРАНИЦЕ (компонент целиком), не произвольная
    подстрока: ключ карты (eq==0 глиф-двойник) не совпадёт с рус. морфемой. Слэш-
    склейки (Р01/РБ-Ы) сюда не расщепляются — их чинит ПИН по полной кир-норме."""
    core = _tok_core(tok)
    toks = _COMPOUND_SEP.split(core)
    comps, seps = toks[0::2], toks[1::2]
    if len(comps) < 2:
        return tok
    pieces: List[Tuple[int, str]] = []
    i, n, changed = 0, len(comps), False
    while i < n:
        matched = False
        for k in range(n, i, -1):                     # пробег comps[i:k], длиннейший
            join = _cyr_norm("".join(comps[i:k]))
            if join and join in doc_map:
                pieces.append((i, doc_map[join])); i = k; matched = changed = True
                break
        if not matched:
            pieces.append((i, comps[i])); i += 1
    return _sub(tok, _reassemble(pieces, seps)) if changed else tok


def _resolve_broken_compound(lk: str, ck: str, doc_map: Dict[str, str],
                             genuine: frozenset) -> Optional[str]:
    """Разрешить БИТЫЙ склеенный токен (Ьпд§е-терапии), НЕ латинизируя русские
    морфемы. Возвращает None, если токен НЕ склейка (тогда — обычная ветвь).

    lk — токен слоя, ck — его OCR-прочтение («bridge-Tepanuu»); `genuine` —
    множество кир-норм ГЕНУИННО-РУССКИХ токенов документа (OCR хоть раз прочитал их
    кириллицей: eq>0). Компоненты слева направо:
      (1) максимальный пробег с кир-нормой = ключ карты -> чистая форма (Ри§Ь->Pugh);
      (2) компонент из `genuine` -> ДОСЛОВНО из СЛОЯ (жёсткий инвариант: генуинно-
          русское НЕ латинизируем, что бы ни прочитал OCR: «терапии», не «Tepanuu»);
      (3) прочий битый компонент -> позиционная чистая латиница OCR (СЫЫ->Child,
          Ьпд§е->bridge), если OCR-токен разбит на столько же компонентов; иначе — слой.
    Разделитель («-») сохраняется на исходном месте.

    `genuine`, а не морфологический признак, — потому что битая латиница часто
    выглядит как русское слово («поп»=non, «пзк»=risk в англоязычной библиографии):
    её OCR читает чистой латиницей (eq==0), и она ДОЛЖНА замениться. Ключевое отличие
    от прежнего поведения: битый СКЛЕЕННЫЙ токен НИКОГДА не заменяется чтением OCR
    целиком — латиница ставится только в позицию НЕ-генуинного битого компонента."""
    core = _tok_core(lk)
    toks = _COMPOUND_SEP.split(core)
    comps, seps = toks[0::2], toks[1::2]
    if len(comps) < 2:
        return None
    # Покомпонентная обработка нужна ТОЛЬКО чтобы уберечь генуинно-русскую МОРФЕМУ в
    # склейке (Ьпд§е-ТЕРАПИИ). Если защищать нечего (вся склейка — латиница/двойники,
    # как в англоязычной библиографии «Ying-Hui», «Direct-Acting»), возвращаем None ->
    # обычная ветвь берёт чтение OCR ЦЕЛИКОМ (чище, чем слой). Порог длины >=3: одно-
    # /двухбуквенные генуинные токены (предлоги «у/в/с/о/к», инициалы) слишком
    # неоднозначны — по ним склейку не защищаем (иначе «У-…» ловилось бы ложно).
    if not any(len(_cyr_norm(c)) >= 3 and _cyr_norm(c) in genuine for c in comps):
        return None
    ccomps = _COMPOUND_SEP.split(_tok_core(ck))[0::2] if ck else []
    positional = ccomps if len(ccomps) == len(comps) else None
    pieces: List[Tuple[int, str]] = []
    i, n, changed = 0, len(comps), False
    while i < n:
        matched = False
        for k in range(n, i, -1):
            join = _cyr_norm("".join(comps[i:k]))
            if join and join in doc_map:
                pieces.append((i, doc_map[join])); i = k; matched = changed = True
                break
        if matched:
            continue
        comp = comps[i]
        if _cyr_norm(comp) in genuine:                # генуинно-русский — из слоя (инвариант)
            pieces.append((i, comp))
        elif positional and _is_clean_latin(_tok_core(positional[i])):
            pieces.append((i, _tok_core(positional[i]))); changed = True   # чистая латиница OCR
        else:
            pieces.append((i, comp))                  # нет чистого чтения — из слоя
        i += 1
    return _sub(lk, _reassemble(pieces, seps)) if changed else lk


def _pre_resolve(text: str, doc_map: Dict[str, str]) -> str:
    """Пред-разрешение (ФИНАЛЬНЫЙ полный проход по границе токена): заменить в тексте
    подтверждённые глиф-двойники из doc_map (кириллический токен без латиницы), в т.ч.
    слипшиеся через дефис (`_hyphen_resolve`). Нативную кириллицу не трогает."""
    if not doc_map:
        return text
    out: List[str] = []
    for tok in text.split():
        low = _cyr_norm(tok)
        if low and low in doc_map and _has_cyr(tok) and not _has_lat(tok):
            out.append(_sub(tok, doc_map[low]))
        elif "-" in tok and _has_cyr(tok) and not _has_lat(tok):
            out.append(_hyphen_resolve(tok, doc_map))
        else:
            out.append(tok)
    return " ".join(out)


# ============================================================================
# Карта «кривое->чистое»: словарь сокращений + статистика тела + база пинов
# ============================================================================

def _band_text(bbox, words) -> str:
    """Текст OCR-слов, чей вертикальный центр попал в полосу строки слоя bbox."""
    x0, y0, x1, y1 = bbox
    r = [(wx0, w) for cy, wx0, wx1, w in words
         if y0 <= cy < y1 and wx1 > x0 - 2 and wx0 < x1 + 2]
    r.sort()
    return " ".join(w for _, w in r)


def _abbr_region_ids(flat: List[Line]) -> set:
    """id строк региона «Список сокращений»..«Термины и определения»/первый раздел.
    Берём ТЕЛОвое (не оглавление) вхождение якоря — без точек-лидеров."""
    ai = next((i for i, ln in enumerate(flat)
               if _ABBR_ANCHOR.search(ln.text) and not _LEAD.search(ln.text)), None)
    if ai is None:
        return set()
    ti = next((i for i, ln in enumerate(flat)
               if i > ai and _TERM_ANCHOR.search(ln.text) and not _LEAD.search(ln.text)),
              min(ai + 60, len(flat)))
    return {id(flat[i]) for i in range(ai, ti)}


def _out_of_scope_ids(flat: List[Line]) -> set:
    """id строк ВНЕ фокуса (библиография + приложения): от якоря «Список литературы»
    до конца документа. Учим двойники только в теле+глоссарии; из этих строк —
    не учим (мусор многоязычной библиографии) и не пополняем базу."""
    ri = next((i for i, ln in enumerate(flat)
               if _REFS_ANCHOR.search(ln.text) and not _LEAD.search(ln.text)), None)
    if ri is None:
        return set()
    return {id(flat[i]) for i in range(ri, len(flat))}


def _abbrev_langs(flat: List[Line], region_ids: set,
                  line_ocr: Dict[int, str]) -> Tuple[Dict[str, str], set]:
    """Разобрать «Список сокращений»: для каждой записи «КЛЮЧ - определение» решить
    по OCR определения, латинская это аббревиатура или русская.
      * определение латинское (Barcelona Clinic Liver Cancer) -> карта КЛЮЧ->чистый
        первый OCR-токен (ВСЬС->BCLC) — структурный высокодостоверный двойник;
      * определение русское (компьютерная томография) -> КЛЮЧ в защиту (abbr_rus):
        русское сокращение (ТАХЭ/УЗИ/СНВС) никогда не латинизируем."""
    pairs: Dict[str, str] = {}
    rus: set = set()
    for ln in flat:
        if id(ln) not in region_ids:
            continue
        layer = ln.text.strip()
        ocr = line_ocr.get(id(ln), "")
        if not _DASH.search(layer):
            continue
        key_layer = layer.split()[0]
        kk = _cyr_norm(key_layer)
        if not kk or _has_lat(key_layer):        # латинский КЛЮЧ ловит статистика тела
            continue
        oc = ocr.split()[1:] if ocr else []
        lat = sum(1 for w in oc if _is_clean_latin(_tok_core(w)))
        cyr = sum(1 for w in oc if _has_cyr(w) and not _has_lat(w) and len(_tok_core(w)) >= 3)
        if lat > cyr and ocr:
            first = _tok_core(ocr.split()[0])
            if _is_clean_latin(first):
                pairs[kk] = first
        else:
            rus.add(kk)
    return pairs, rus


def _record_double(oo: str, cc: str, latc: Dict[str, Counter], kup: Dict[str, bool],
                   glossc: set, in_gloss: bool) -> None:
    """Записать кандидат-двойник «кириллический токен слоя oo -> чистая латиница OCR
    cc» в статистику latc (кириллический токен без латиницы, латиница — чистая
    форма-восстановление, ядро >=2)."""
    core = _tok_core(oo)
    if not (_has_cyr(oo) and not _has_lat(oo) and len(core) >= 2):
        return
    ccc = _tok_core(cc)
    if not _is_clean_latin(ccc):
        return
    low = _cyr_norm(oo)
    latc[low][ccc] += 1
    kup[low] = kup.get(low, False) or bool(_UPCYR.search(core))
    if in_gloss:
        glossc.add(low)


def _dominant(cc: Counter) -> Optional[str]:
    """Доминирующее значение двойника, если оно явно преобладает (>=60% наблюдений).
    Неоднозначные (of/or 47/5 -> ок; 6/5 -> нет) отсекаем: риск ложной замены."""
    if not cc:
        return None
    (value, hits), total = cc.most_common(1)[0], sum(cc.values())
    return value if hits * 5 >= total * 3 else None


def _base_value_set(base: Dict[str, str]) -> set:
    """Множество чистых форм базы + их компоненты (Child-Pugh -> Child, Pugh) —
    для корроборации одиночных двойников (сыш->Child, ридь->Pugh)."""
    vals: set = set()
    for v in base.values():
        vals.add(v)
        for part in re.split(r"[-/ ]", v):
            if len(part) >= 3:
                vals.add(part)
    return vals


def _build_doc_map(flat: List[Line], line_ocr: Dict[int, str], region_ids: set,
                   base: Dict[str, str]
                   ) -> Tuple[Dict[str, str], Dict[str, str], frozenset]:
    """Карта документа {кривое->чистое}, НОВЫЕ (не из базы) записи для шарда и
    множество ГЕНУИННО-РУССКИХ кир-норм (eq>0) — их битые склейки не латинизируют.

    Двойник принимается, если он: (1) НЕ защищённое рус.-сокращение; (2) never-
    trusted (OCR ни разу не прочитал токен кириллицей, eq==0); (3) прошёл
    структурный глиф-сигнал; (4) значение — чистая латиница И явно доминирует; и
    хотя бы одно из: наблюдался >=2 раз В ТЕЛЕ ДОКУМЕНТА, ИЛИ встретился в регионе
    глоссария (там определения английские — одного вхождения достаточно), ИЛИ его
    чистая форма подтверждена базой/другими двойниками того же документа
    (value_support). Так дочищаются одиночные глоссарные (Аззосгабоп->Association) и
    «рваные» телесные (нвзад->HBsAg, апбнсу->anti-HCV) двойники, пропущенные раньше.

    Русские слова/фамилии (eq>0) и защищённые сокращения в карту не попадают —
    деградация кириллицы близка к нулю (сильнейший гейт — eq==0)."""
    oos_ids = _out_of_scope_ids(flat)          # библиография+приложения — вне фокуса
    base_values = _base_value_set(base)
    eq = Counter()
    latc: Dict[str, Counter] = defaultdict(Counter)
    kup: Dict[str, bool] = {}
    glossc: set = set()
    for ln in flat:
        ocr = line_ocr.get(id(ln), "")
        if not ocr or _LEAD.search(ln.text):
            continue
        in_gloss = id(ln) in region_ids
        out_of_scope = id(ln) in oos_ids
        o, c, ops = _align_ops(ln.text, ocr)
        for tag, i1, i2, j1, j2 in ops:
            if tag == "equal":                              # eq (защита) — по ВСЕМ строкам
                for t in o[i1:i2]:
                    if _has_cyr(t) and not _has_lat(t):
                        eq[_cyr_norm(t)] += 1
            elif out_of_scope:                              # библиографию не учим
                continue
            elif (i2 - i1) == (j2 - j1):                    # позиционное сопоставление
                for oo, cc in zip(o[i1:i2], c[j1:j2]):
                    _record_double(oo, cc, latc, kup, glossc, in_gloss)
            else:                                           # разной длины: латиница по порядку
                lat = [x for x in c[j1:j2] if _is_clean_latin(_tok_core(x))]
                # кандидаты для СКЛЕЙКИ — только ПРОПИСНЫЕ двойники (строчные рус.
                # предлоги «по/на/с», которые OCR принял за латиницу «no», исключаем).
                cands = [oo for oo in o[i1:i2]
                         if _has_cyr(oo) and not _has_lat(oo)
                         and len(_tok_core(oo)) >= 2 and _UPCYR.search(oo)]
                # атомы-компоненты ИЗВЕСТНОЙ составной формы (Child, Pugh); латиница-
                # дистрактор от рус. предлога («no») в base_values не входит -> отсеётся.
                known = [_tok_core(x) for x in lat
                         if _tok_core(x) in base_values and "-" not in _tok_core(x)]
                joined = "-".join(known)
                if (len(cands) == 1 and len(known) >= 2 and _is_compound(cands[0])
                        and joined in base_values):
                    # склеенный source-двойник (Child—Pugh одним токеном), OCR разбил
                    # на атомы -> собираем известную составную форму, НЕ обрезаем.
                    _record_double(cands[0], joined, latc, kup, glossc, in_gloss)
                else:
                    li = 0
                    for oo in o[i1:i2]:
                        if li >= len(lat):
                            break
                        if _has_cyr(oo) and not _has_lat(oo) and len(_tok_core(oo)) >= 2:
                            _record_double(oo, lat[li], latc, kup, glossc, in_gloss)
                            li += 1
    abbr_pairs, abbr_rus = _abbrev_langs(flat, region_ids, line_ocr)

    def protected(low: str) -> bool:
        return low in abbr_rus or any(_cyr_norm(p) in abbr_rus for p in low.split("/"))

    # кандидаты, прошедшие базовые гейты (eq==0, не защита, глиф-сигнал, доминантное
    # значение), — и суммарная поддержка каждой ЧИСТОЙ формы по всем ключам документа
    cand: Dict[str, str] = {}
    value_support: Counter = Counter()
    for low, cc in latc.items():
        if protected(low) or eq.get(low, 0) > 0:
            continue
        value = _dominant(cc)
        if value and _glyph_signal(low, value, kup.get(low, False)):
            cand[low] = value
            value_support[value] += sum(cc.values())

    doc_map: Dict[str, str] = {}
    new_map: Dict[str, str] = {}
    for low, value in cand.items():
        if (sum(latc[low].values()) >= _MIN_DOUBLE_HITS      # >=2 набл. в теле
                or low in glossc                             # регион глоссария
                or value in base_values                      # форма известна базе
                or value in _SEED_CLEAN                       # чистая форма из сида (hapax)
                or value_support[value] >= _MIN_DOUBLE_HITS):  # форму дают >=2 двойника
            doc_map[low] = value
            if low not in base:
                new_map[low] = value
    for low, value in abbr_pairs.items():             # словарь — структурный, приоритетнее
        if not protected(low):
            doc_map[low] = value
            new_map[low] = value
    # база пинов — только чтение: досыпаем известные двойники, не перетирая документ
    for low, value in base.items():
        if low not in doc_map and not protected(low):
            doc_map[low] = value
    # генуинно-русские кир-нормы: OCR хоть раз прочитал их кириллицей (eq>0). Их НИ
    # в какой битой склейке не латинизируем (см. _resolve_broken_compound).
    genuine = frozenset(k for k, c in eq.items() if c > 0)
    return doc_map, new_map, genuine


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
        #: человекочитаемая метка документа (пишется ВНУТРЬ файла кэша, НЕ в ключ).
        self._doc_id: Optional[str] = None
        #: sha256 исходного PDF — часть ключа кэша (content-addressed). None -> кэш
        #: не используется (нет отпечатка содержимого), только рендер.
        self._pdf_sha: Optional[str] = None
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

    def _render(self, page: "fitz.Page", clip: Optional["fitz.Rect"],
                scale: float = 1.0) -> bytes:
        """Растр области (или всей страницы) в PNG-байтах при заданном DPI, с
        лёгким препроцессингом для Tesseract (`_prep_png`). `scale` (>1) даёт
        дополнительный апскейл — для мелкого/плотного кегля (клипы глоссария)."""
        z = self._dpi / 72.0 * scale
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z), clip=clip)
        return self._prep_png(pix)

    @staticmethod
    def _prep_png(pix: "fitz.Pixmap") -> bytes:
        """Препроцессинг растра под OCR: оттенки серого + автоконтраст + лёгкий
        unsharp. Чинит плотный курсивный глоссарий. Через PIL; если PIL нет —
        возвращаем сырой PNG (graceful fallback, поведение как раньше)."""
        raw = pix.tobytes("png")
        try:
            from PIL import Image, ImageFilter, ImageOps
            img = Image.open(io.BytesIO(raw)).convert("L")
            img = ImageOps.autocontrast(img)
            img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=150, threshold=2))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:  # noqa: BLE001 — PIL необязателен
            return raw

    # ---- дисковый кэш сырого TSV -------------------------------------------

    def _stack12(self) -> str:
        """Отпечаток стека OCR (Tesseract/traineddata/Pillow/dpi/langs/preproc)."""
        return _stack_signature(self._cmd, self._tessdata, self._langs,
                                self._dpi, _PREPROC_VERSION)

    def _cache_path(self, page_no: int, psm: int) -> Optional[str]:
        """Путь к content-addressed кэш-файлу TSV: <pdf_sha12>_p<page>_<stack12>_psm<N>.
        Ключ зависит от содержимого PDF и всего стека, а не от basename. None -> нет
        отпечатка PDF (кэшировать нечем)."""
        if not self._pdf_sha:
            return None
        name = "%s_p%d_%s_psm%d.json" % (
            self._pdf_sha[:12], page_no, self._stack12(), psm)
        return os.path.join(_OCR_CACHE_DIR, name)

    def _cached_tsv(self, fpage: "fitz.Page", page_no: int, psm: int) -> str:
        """TSV страницы: из кэша при попадании (без рендера/Tesseract), иначе
        рендер+OCR и атомарная запись в кэш. Рендер может бросить — обрабатывает
        вызывающий (как раньше). Пустой TSV не кэшируем (мог быть транзиентный сбой)."""
        path = self._cache_path(page_no, psm)
        if path:
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as fh:
                        return json.load(fh).get("tsv", "")
            except Exception:  # noqa: BLE001 — кэш необязателен
                pass
        png = self._render(fpage, None)
        tsv = self._run(png, psm=psm, tsv=True)
        if path and tsv:
            try:
                os.makedirs(_OCR_CACHE_DIR, exist_ok=True)
                JsonWriter._atomic_dump(
                    {"tsv": tsv, "dpi": self._dpi, "preproc": _PREPROC_VERSION,
                     "doc_id": self._doc_id}, path)
            except Exception:  # noqa: BLE001
                pass
        return tsv

    def _clip_text(self, fpage: "fitz.Page", page_no: int, bbox,
                   psm: int = 7, scale: float = 1.5) -> str:
        """Построчный OCR клипа строки (`--psm 7`, апскейл) с дисковым кэшем по bbox.
        Для плотного/курсивного глоссария page-`--psm 6` читает мусорно (ТNМ->ТММ),
        клип-`--psm 7` — чисто (ТNМ->TNM). «» при сбое/недоступном PIL/рендере."""
        x0, y0, x1, y1 = bbox
        if x1 <= x0 or y1 <= y0:
            return ""
        path = None
        if self._pdf_sha:
            key = "%s_clip_p%d_%d_%d_%d_%d_s%s_%s_psm%d.json" % (
                self._pdf_sha[:12], page_no, round(x0), round(y0), round(x1), round(y1),
                str(scale).replace(".", "-"), self._stack12(), psm)
            path = os.path.join(
                _OCR_CACHE_DIR, re.sub(r"[^0-9A-Za-z_.-]", "_", key))
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as fh:
                        return json.load(fh).get("text", "")
            except Exception:  # noqa: BLE001 — кэш необязателен
                pass
        clip = fitz.Rect(x0 - _CLIP_PAD_PT, y0 - _CLIP_PAD_PT,
                         x1 + _CLIP_PAD_PT, y1 + _CLIP_PAD_PT)
        try:
            png = self._render(fpage, clip, scale=scale)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"OCR: рендер клипа не удался ({exc!r})")
            return ""
        text = " ".join(self._run(png, psm=psm).split())
        if path and text:
            try:
                os.makedirs(_OCR_CACHE_DIR, exist_ok=True)
                JsonWriter._atomic_dump(
                    {"text": text, "dpi": self._dpi, "scale": scale,
                     "preproc": _PREPROC_VERSION}, path)
            except Exception:  # noqa: BLE001
                pass
        return text

    def _page_words(self, fpage: "fitz.Page", page_no: int) -> list:
        """OCR всей страницы (`--psm 6 tsv`) -> список слов (cy, x0, x1, текст) в
        пунктах PDF. Пусто при сбое рендера/OCR. Результат TSV кэшируется на диск."""
        try:
            tsv = self._cached_tsv(fpage, page_no, psm=6)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"OCR: рендер страницы не удался ({exc!r})")
            return []
        scale = 72.0 / self._dpi
        words = []
        for row in tsv.splitlines():
            cols = row.split("\t")
            if len(cols) < 12 or not cols[0].isdigit() or int(cols[0]) != 5:
                continue
            word = cols[11].strip()
            if not word:
                continue
            try:
                left, top, w, h = (int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]))
            except ValueError:
                continue
            words.append(((top + h / 2.0) * scale, left * scale, (left + w) * scale, word))
        return words

    # ---- ГИБРИД -------------------------------------------------------------

    def hybrid_recover(self, doc: "fitz.Document", pages: List[Page],
                       base: Optional[Dict[str, str]] = None,
                       doc_id: Optional[str] = None,
                       pdf_sha: Optional[str] = None) -> Tuple[int, Dict[str, str]]:
        """Восстановить кирилло-латинскую глиф-порчу по всему документу-кандидату.

        OCR-ит ВСЕ страницы (полнота важнее скорости), строит карту двойников из
        словаря сокращений + статистики тела + базы пинов, затем ТОКЕН-УРОВНЕВО
        сливает каждую строку слоя с её OCR-строкой (нативная кириллица авторитетна).
        Меняет только Line.text. Возвращает (число_изменённых_строк, новые_двойники
        для шарда). При недоступном OCR — (0, {}) без изменений."""
        if not self.available():
            return 0, {}
        self._doc_id = doc_id
        self._pdf_sha = pdf_sha
        base = base or {}
        flat = [ln for page in pages for ln in page.lines]
        region_ids = _abbr_region_ids(flat)          # регион глоссария (по якорям)
        line_ocr: Dict[int, str] = {}
        for page in pages:
            fpage = doc[page.number - 1]
            words = self._page_words(fpage, page.number)
            for ln in page.lines:
                band = _band_text(ln.bbox, words) if words else ""
                # ГЛОССАРИЙ (Список сокращений/Термины): строки региона переснимаем
                # построчным клипом (psm 7 + апскейл) — плотные аббревиатуры-ключи
                # (ТNМ->TNM) и курсивные определения page-psm6 читает мусорно.
                if id(ln) in region_ids:
                    clip = self._clip_text(fpage, page.number, ln.bbox)
                    if clip:
                        band = clip
                elif not band and len(ln.text.strip()) >= 3:
                    # ТЕЛО: лента пуста/съехала (плотный абзац, page-psm6 не покрыл
                    # строку) — переснимаем построчным клипом для надёжного
                    # соответствия слой<->OCR. Кэш клипов есть; фолбэк редкий.
                    clip = self._clip_text(fpage, page.number, ln.bbox)
                    if clip:
                        band = clip
                line_ocr[id(ln)] = band
        doc_map, new_map, genuine = _build_doc_map(flat, line_ocr, region_ids, base)
        fixed = 0
        for page in pages:
            changed = False
            for ln in page.lines:
                before = ln.text
                if _LEAD.search(before):
                    after = _pre_resolve(before, doc_map)
                else:
                    after = _resolve_line(before, line_ocr.get(id(ln), ""),
                                          doc_map, genuine)
                    after = _latinize_flanked(after)  # «Hepatitis В virus»->«...B...»
                if after and after != before:
                    ln.text = after
                    fixed += 1
                    changed = True
            if changed:
                page.text = "\n".join(ln.text for ln in page.lines)
        return fixed, new_map

    def _recover_line(self, fpage: "fitz.Page", line: Line) -> str:
        """Точечный fallback: клип строки по bbox, `--psm 7`, слияние merge_line."""
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
        ocr = " ".join(self._run(png, psm=7).split())
        return merge_line(line.text, ocr) if ocr else line.text

    # ---- ПОЛНЫЙ -------------------------------------------------------------

    def full_ocr(self, doc: "fitz.Document",
                 doc_id: Optional[str] = None,
                 pdf_sha: Optional[str] = None) -> List[Page]:
        """Собрать страницы из ПОЛНОГО OCR (скан/тотальная порча). При недоступном
        OCR — пустой список (вызывающий сохраняет прежнее поведение)."""
        if not self.available():
            return []
        self._doc_id = doc_id
        self._pdf_sha = pdf_sha
        pages: List[Page] = []
        for index in range(doc.page_count):
            fpage = doc[index]
            try:
                tsv = self._cached_tsv(fpage, index + 1, psm=6)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"OCR: рендер страницы {index + 1} не удался ({exc!r})")
                pages.append(Page(number=index + 1, width=float(fpage.rect.width),
                                  height=float(fpage.rect.height), lines=[], text=""))
                continue
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
