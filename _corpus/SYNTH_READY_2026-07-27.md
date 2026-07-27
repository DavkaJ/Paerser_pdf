# Канонический прогон под синтетику — 27.07.2026

Ветка `audit-fix-2026-07`. Цель: получить корпус, который собирается ОДНОЙ штатной
командой, имеет манифест прогона и отгружается по контракту «пригодно для генерации».

## 1. Что было не так

| # | Проблема | Факт |
|---|---|---|
| B1 | Продуктовый путь публиковал грязный корпус | ни `batch_report`, ни `run.py` не включали `latin_recovery`; в `outout/` 36 973 PUA-глифа в 397 док. Чистый `outout_latin` собирался скриптами `_corpus/` и штатной командой не воспроизводился |
| B2 | Управляющие символы вместо букв | 10 855 шт. в КР153_2 (`Chlamydia` → `Hhl\x12m\x1adi\x12`). Документ держался REVIEW только по OVERCOUNT, т.е. при допуске benign-REVIEW уехал бы в обучение |
| B3 | `report_latin.json` отставал на 3 мутации текста | 66 `LATIN_UNRESOLVED` против 37 реальных → 29 документов держались зря; `revalidate.py` был захардкожен на `outout` |
| B4 | Нет run-манифеста для отгружаемого корпуса | `release.py --run` невозможен, оставался только `--current` со штампом `not_for_distribution` |
| B5 | Контракт `require_status:[PASS]` | 502 из 722 при том, что пригодны 671 |

## 2. Сделано (5 коммитов)

| Коммит | Что |
|---|---|
| `af88c8e` | `revalidate.py`: пути из `CR_OUTOUT`/`CR_REPORT` + блок `latin` в строках отчёта |
| `4edd212` | Детектор управляющих C0/C1 (`stats.corruption.control_chars`, гейт CORRUPTION ≥20) + снятие невидимых форматных символов |
| `df55ce2` | `--latin-recovery` в `batch_report` и `run.py`, флаг пишется в `run_manifest` |
| `17222e3` | `synthready.py` — гейт и экспорт под синтетику (тиры A/B/C) |
| `e0601ad` | PUA-нормализация доходит до `excluded` (приложения ехали с сырыми глифами) |

Регресс-набор: **167 passed / 1 xfailed**.

## 3. Находки, которые вскрыл сам прогон

**PUA в приложениях.** Обход `_pua_normalize_all` умел dict/list, а `ExcludedItem` —
датакласс. Движок нормализовал `sections` и `tables`, но не `excluded`: первый штатный
прогон дал 6 524 PUA против 1 579 у патч-скрипта — разница 4 945, из них 4 078 маркеров
списка `U+F0B7` в `.excluded.appendices`, а приложения входят в обучающий контракт.
После фикса — 1 579 в 47 док., ровно как у скрипта (остаток — кодпоинты вне таблицы
Adobe Symbol, они помечены `needs_review` и НЕ дропнуты).

**Патченный корпус нёс устаревший анализ.** 4 документа (КР963_1, КР726_2, КР88_5,
КР1016_1) перешли PASS→REVIEW: текст разделов байт-в-байт тот же, но пере-выведенный
`unresolved_critical` нашёл `СО2`/`р27`. Скрипты правили текст, не пересчитывая анализ.

**Недетерминизм OCR-каналов.** 43 документа из 701 отличаются от патченного корпуса
единичными токенами. Часть — улучшения (`P02`→`pO2`, `V02`→`VO2`), часть — потери
(`8атта1когр1`→`Sammalkorpi` стал `needs_review` вместо `auto`: кроп прочитался
Tesseract-ом иначе, уверенность ниже гейта). Новое поведение контрактно-верное
(needs_review в текст не пишется), но байт-в-байт повтор прогона на OCR-документах
не гарантирован — это свойство канала, а не дефект правок.

## 4. Итог прогона

```
run_id 20260727T183303Z_c3107d6c   722 док.   1439s   crash=0 timeout=0
run_integrity_ok=True   latin_recovery=true   require_ocr=true
СТАТУСЫ валидатора: PASS 502 / REVIEW 158 / FAIL 62
```

Гейт `synthready` (тиры по пригодности для генерации, ось отдельная от валидатора):

```
A (чистые)      156
B (с флагами)   515      флаги: table_stubs 355, needs_review_spans 205,
C (карантин)     51              validator 185, overcount 68, garble 61,
                                 unresolved_critical 41, recall 36
карантин: structure 21, lost_text 16, garble 11, control_chars 1,
          corrupt_layer 1, too_short 1
```

**Выгружено: 671 документ, 15 736 текстовых юнитов, 51.4 млн символов** →
`candidate_synthetic/` (по документу на файл + `MANIFEST.json` с sha256) и
`candidate_synthetic/corpus.jsonl` (плоские юниты: `doc_id`, `url`, `mkb_codes`,
`age_group`, путь по главам, текст, тир, флаги).

Контракт режет `excluded.references`, `toc`, `front_matter`, `other`, `stats`,
`provenance`. Инвариант 6 не нарушен: `candidate_release/` остаётся PASS-only,
это отдельная директория.

## 5. Воспроизведение

```
set TESSERACT_CMD=...\tesseract.exe & set TESSDATA_PREFIX=...\tessdata
set CR_OUTOUT=outout_latin_v2 & set CR_REPORT=_corpus\report_latin_v2.json
python batch_report.py --latin-recovery --keep-runs 3
python synthready.py --corpus outout_latin_v2 --report _corpus\report_latin_v2.json ^
    --export candidate_synthetic --jsonl candidate_synthetic\corpus.jsonl
```

## 6. Что осталось

- `СО2` (углекислый газ) детектится как критический ICD-код `C02` — ложное срабатывание,
  раздувает флаг `unresolved_critical` (41 док.). Нужен химический белый список.
- Огрызки таблиц: 1 016 из 11 597 короче 200 символов (вырезана шапка, тело потеряно) —
  помечены `low_confidence` в выгрузке, но не восстановлены.
- Остаток PUA 1 579 в 47 док. — 8 кодпоинтов вне таблицы Adobe Symbol (чекбоксы анкет,
  маркеры списков); нужен per-font разбор или ремап в `•`.
- Хвост «латинское слово кириллицей» живёт в references (0.92% против 0.08% в разделах)
  — контракт их режет, чинить под синтетику не требуется.
