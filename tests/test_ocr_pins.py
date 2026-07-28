# -*- coding: utf-8 -*-
"""Пины и content-addressed кэш (промпт 07, группа 5)."""
import hashlib
import os

from crparser.engine import ocr, ocr_pins

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PINS = os.path.join(ROOT, "crparser", "data", "ocr_pins.json")


def test_24_proposal_does_not_mutate_base():
    """Сбор предложения новых пар НЕ пишет в ocr_pins.json (immutable вход)."""
    before = hashlib.sha256(open(PINS, "rb").read()).hexdigest()
    ocr_pins.build_proposal()
    after = hashlib.sha256(open(PINS, "rb").read()).hexdigest()
    assert before == after


def test_25_base_is_read_only():
    """load_base() отдаёт неизменяемый MappingProxyType — мутация падает."""
    base = ocr_pins.load_base()
    try:
        base["x"] = "y"
        assert False, "база должна быть read-only"
    except TypeError:
        pass


def test_26_cache_key_sensitive_to_stack_and_pdf():
    """Ключ кэша меняется при смене отпечатка PDF И при смене стека (Tesseract/
    traineddata/Pillow/DPI/langs/preproc)."""
    r = ocr.OcrRecoverer()
    r._cmd = None
    r._pdf_sha = "a" * 64
    p_a = r._cache_path(1, 6)
    r._pdf_sha = "b" * 64
    p_b = r._cache_path(1, 6)
    assert p_a != p_b                                    # PDF-содержимое в ключе
    # stack signature: разные dpi/langs -> разный отпечаток
    s1 = ocr._stack_signature(None, None, "rus+eng", 400, 1)
    s2 = ocr._stack_signature(None, None, "rus+eng", 300, 1)
    s3 = ocr._stack_signature(None, None, "rus", 400, 1)
    s4 = ocr._stack_signature(None, None, "rus+eng", 400, 2)
    assert len({s1, s2, s3, s4}) == 4                    # каждый компонент влияет


def test_27_no_PDI_target_in_active_base():
    """В активной базе нет цели PDI (был конфликт с PD1/PD-L1)."""
    base = ocr_pins.load_base()
    assert not any("PDI" in v for v in base.values())
