# RECALL NOTICE — отгруженный корпус требует пересмотра

Сформировано по ФАКТУ (чтение outout_pass/ + report.json), не по памяти. Дата: 2026-07-13.

## Факты по выгрузке
- `outout_pass/`: **596** файлов (не 616 — не равно множеству PASS).
- `outout_extra_usable/`: **83** файлов.
- Текущее множество PASS в report.json: **582**.

## 1. Отгруженные файлы, БОЛЬШЕ не PASS по новым гейтам (03+04)
Всего: **14**. Эти файлы нельзя использовать как готовые:

  - КР1021_1 -> FAIL (MISSING, CANONICAL_RECALL)
  - КР1_4 -> REVIEW (RESIDUAL_PIN)
  - КР246_3 -> REVIEW (COLLAPSE)
  - КР396_4 -> REVIEW (OCR_REQUIRED)
  - КР400_2 -> REVIEW (TOC_NONE)
  - КР469_3 -> REVIEW (CANONICAL_RECALL, TOC_NONE)
  - КР502_2 -> REVIEW (RESIDUAL_PIN)
  - КР600_2 -> REVIEW (TABLES_MISSING)
  - КР654_2 -> REVIEW (TABLES_MISSING)
  - КР717_2 -> REVIEW (TOC_NONE)
  - КР809_1 -> REVIEW (OVERCOUNT)
  - КР811_1 -> REVIEW (CANONICAL_RECALL)
  - КР848_1 -> FAIL (CANONICAL_RECALL, MISSING, COLLAPSE, OVERCOUNT)
  - КР875_1 -> REVIEW (NO_SECTIONS, OVERCOUNT)

## 2. Рассинхрон самой выгрузки (доказательство неизвестного прогона)
- PASS-файлов (на момент baseline, 616 PASS), которых НЕ было в outout_pass/: **0** — 
- PASS-файлов (текущих, 572 PASS), которых НЕТ в outout_pass/: **0** — —
- В outout_pass/ лежат файлы со статусом НЕ PASS: **14** — КР1021_1, КР1_4, КР246_3, КР396_4, КР400_2, КР469_3, КР502_2, КР600_2, КР654_2, КР717_2, КР809_1, КР811_1, КР848_1, КР875_1
  (в т.ч. КР875_1 при статусе REVIEW — выгрузка сделана от ДРУГОГО прогона).

## 3. excluded.references использовать НЕЛЬЗЯ
Отгруженные JSON содержат `excluded.references` с exact-token attestation 43-50% (замер аудита). По новому контракту обучения (training_contract.json) эта зона ИСКЛЮЧЕНА. Коллеги не должны обучать на ней.

## Что делать
Пересобрать корпус детерминированным экспортёром `release.py --run <run_id>` от верифицированного прогона (промпт 05) и раздать `candidate_release/`, не ручную выгрузку. Директория остаётся `candidate_release/` до аттестации на gold set (промпт 14).

