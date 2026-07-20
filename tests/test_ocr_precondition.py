# -*- coding: utf-8 -*-
"""ШАГ 0 (промпт 17): OCR-предусловие корпусных прогонов — гейт fail-closed.

Ловит дыру I5/I34 (инцидент ac5d03a): `_corpus/batch.py` / `_corpus/batch_latin.py`
публиковали ДРУГОЙ корпус с молча выключенным OCR и завершались exit 0. Правило I30:
«гейт работает» = есть ВЫЗОВ + тест ИСПОЛНЯЕТ путь. Здесь:
  (1) юнит-проверка предиката `ocr_precondition()` на ОБЕИХ ветках отказа (нет бинаря;
      нет rus.traineddata) и на успехе;
  (2) интеграционный тест — РЕАЛЬНЫЙ запуск batch_latin.py подпроцессом с недоступным
      OCR: exit 2 и НУЛЕВАЯ публикация (не читаем артефакт — исполняем main()).
"""
import os
import subprocess
import sys

import pytest

from crparser.engine import ocr as _ocr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------- (1) предикат: обе ветки отказа + успех ----------------
def test_precondition_false_when_no_binary(monkeypatch):
    """Нет бинаря (resolver->None) -> (False, message). conftest уже зануляет резолвер."""
    monkeypatch.setattr(_ocr, "_resolve_tesseract", lambda: None)
    available, msg = _ocr.ocr_precondition()
    assert available is False
    assert msg and "не найден" in msg


def test_precondition_false_when_rus_missing(monkeypatch):
    """Бинарь есть, но нет rus.traineddata (ветка Cowork-машины) -> (False, msg про rus)."""
    monkeypatch.setattr(_ocr, "_resolve_tesseract", lambda: "/fake/tesseract")
    monkeypatch.setattr(_ocr.OcrRecoverer, "_list_langs", lambda self: {"eng", "osd"})
    available, msg = _ocr.ocr_precondition()  # по умолчанию нужен rus+eng
    assert available is False
    assert "rus" in msg


def test_precondition_true_when_rus_and_eng(monkeypatch):
    """Бинарь + rus+eng -> (True, "")."""
    monkeypatch.setattr(_ocr, "_resolve_tesseract", lambda: "/fake/tesseract")
    monkeypatch.setattr(_ocr.OcrRecoverer, "_list_langs",
                        lambda self: {"eng", "rus", "osd"})
    available, msg = _ocr.ocr_precondition()
    assert available is True
    assert msg == ""


# ---------------- (2) интеграция: batch_latin.py exit 2, без публикации ----------------
def test_batch_latin_gate_refuses_when_ocr_unavailable(tmp_path):
    """РЕАЛЬНЫЙ прогон main(): недоступный OCR -> exit 2 и НИ ОДНОГО файла в OUT.

    OCR глушим машино-независимо: OCR_LANGS с несуществующим языком -> available()==False
    и при наличии бинаря (языка нет), и при его отсутствии (resolver None). Так путь
    гейта исполняется на любой машине CI, без Tesseract-зависимости."""
    if not os.path.isfile(os.path.join(ROOT, "scan_candidates.csv")):
        pytest.skip("нет scan_candidates.csv — интеграционный тест требует корпус-окружение")
    out = tmp_path / "out_gate"
    env = dict(os.environ)
    env["OCR_LANGS"] = "rus+eng+__nonexistent_lang__"  # заведомо недоступен -> gate off
    env["CR_LATIN_OUT"] = str(out)
    env.pop("TESSERACT_CMD", None)
    proc = subprocess.run(
        [sys.executable, os.path.join("_corpus", "batch_latin.py"), "КР1000_1.pdf"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
    assert proc.returncode == 2, (
        "batch_latin с недоступным OCR дал exit %d (ожидался 2 — гейт не сработал)\n%s"
        % (proc.returncode, proc.stdout[-2000:]))
    published = list(out.glob("*.json")) if out.exists() else []
    assert not published, "публикация при недоступном OCR: %s" % [p.name for p in published]
