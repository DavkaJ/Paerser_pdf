# Дельта-отчёт: применение проверенных правок к тексту корпуса (ROADMAP шаг 1)

Механизм: движковый оверлей `self._g` (decision=auto, source=human_verified) + перепарс
с OCR. **Аудит airtight**: OFF и ON спарены в одном процессе (Tesseract не детерминирован
между процессами); токен-диф выравнивания OFF/ON — каждое отличающееся ON-ядро обязано
быть проверенным corrected-значением (оверлей внёс РОВНО проверенный набор, не тронул
прочее). Оверлей ПЕРЕКРЫВАЕТ ошибочные канальные авто-решения (напр. `Н2О`->`H20`(канал) ->
`H2O`(человек)).

## Гейт аудита
- док. чистых: **81 / 81** (грязных 0, crash 0)
- контрольная I1: OFF==ON **ДА** (оверлей инертен на здоровом; 6 док.)

## Итог
- затронуто док.: **81**
- форм применено: **71 / 72** (не применено: **1**)
- всего замен (occurrences): **579**

## По провенансу (occurrences)
- `human_verified_5a`: 507
- `reverified_human_verified+homoglyph_norm`: 39
- `reverified_homoglyph_norm`: 26
- `reverified_crop_arbitrated`: 7

## Применённые формы (source -> corrected | occ | #docs)
- `оГ` -> `of` | occ=349 | docs=34
- `8са1е` -> `Scale` | occ=48 | docs=13
- `Н2О` -> `H2O` | occ=14 | docs=4
- `М1а` -> `M1a` | occ=12 | docs=8
- `1Ш88СО` -> `RUSSCO` | occ=12 | docs=5
- `СНп` -> `Clin` | occ=9 | docs=8
- `МсОШ` -> `McGill` | occ=8 | docs=4
- `JO1XХ` -> `J01XX` | occ=7 | docs=3
- `Ьйрз` -> `https` | occ=7 | docs=6
- `уегзюп` -> `version` | occ=7 | docs=6
- `Т1Ь` -> `T1b` | occ=6 | docs=6
- `рО2` -> `pO2` | occ=6 | docs=3
- `N1а` -> `N1a` | occ=6 | docs=4
- `ВАР1` -> `BAP1` | occ=4 | docs=2
- `Т4Ь` -> `T4b` | occ=4 | docs=4
- `М1с` -> `M1c` | occ=4 | docs=3
- `М1a` -> `M1a` | occ=3 | docs=2
- `N2а` -> `N2a` | occ=3 | docs=2
- `801АА` -> `S01AA` | occ=3 | docs=2
- `801РВ01` -> `S01FB01` | occ=3 | docs=1
- `Т2в` -> `T2b` | occ=2 | docs=1
- `К.1188СО` -> `RUSSCO` | occ=2 | docs=2
- `7ЧЕС` -> `NEC` | occ=2 | docs=1
- `М1b` -> `M1b` | occ=2 | docs=2
- `РО2` -> `pO2` | occ=2 | docs=2
- `ЕТ1Ш8` -> `ETDRS` | occ=2 | docs=1
- `Ь04АВ02` -> `L04AB02` | occ=2 | docs=1
- `T2в` -> `T2b` | occ=2 | docs=1
- `810РЕЬ` -> `SIOPEL-3` | occ=2 | docs=1
- `N2с` -> `N2c` | occ=2 | docs=2
- `Уо1.54` -> `Vol.54` | occ=2 | docs=2
- `РO2` -> `pO2` | occ=2 | docs=1
- `МРА8А` -> `RIPASA` | occ=2 | docs=1
- `CHА2DS2` -> `CHA2DS2-VASc` | occ=1 | docs=1
- `ТН1М` -> `THIM` | occ=1 | docs=1
- `801АЕ` -> `S01AE` | occ=1 | docs=1
- `301СЯ` -> `J01CR` | occ=1 | docs=1
- `8МАКСВ1` -> `SMARCB1` | occ=1 | docs=1
- `N1с` -> `N1c` | occ=1 | docs=1
- `Б04ААЗЗ` -> `L04AA33` | occ=1 | docs=1
- `1А1НС` -> `IAIHG` | occ=1 | docs=1
- `АС55` -> `ACSS` | occ=1 | docs=1
- `Уо1.1` -> `Vol. 15` | occ=1 | docs=1
- `СА19.9` -> `CA19.9` | occ=1 | docs=1
- `А8КМ` -> `ASRM` | occ=1 | docs=1
- `1Ш8БСО` -> `RUSSCO` | occ=1 | docs=1
- `АО5` -> `A05` | occ=1 | docs=1
- `ЯА8` -> `RAS` | occ=1 | docs=1
- `Ю4ААЗЗ` -> `L04AA33` | occ=1 | docs=1
- `801ЕС` -> `S01EC` | occ=1 | docs=1
- `С04ЕЮ` -> `G04BD` | occ=1 | docs=1
- `8ЮРЕЬ` -> `SIOPEL-3` | occ=1 | docs=1
- `Е8НКЕ` -> `ESHRE` | occ=1 | docs=1
- `801РА` -> `S01FA` | occ=1 | docs=1
- `Уо1.22` -> `Vol. 224` | occ=1 | docs=1
- `рМ1а` -> `pN1a` | occ=1 | docs=1
- `М1с1` -> `M1c1` | occ=1 | docs=1
- `КЕС15Т` -> `RECIST` | occ=1 | docs=1
- `ЮЙ8` -> `Kids` | occ=1 | docs=1
- `511ТЗ` -> `5HT3` | occ=1 | docs=1
- `Р01РОХ1Ш` -> `FOLFOXIRI` | occ=1 | docs=1
- `ШЛ88СО` -> `RUSSCO` | occ=1 | docs=1
- `гЦТ11Ш1с!еПпе` -> `rUTIguideline` | occ=1 | docs=1
- `Т04ААЗЗ` -> `L04AA33` | occ=1 | docs=1
- `ЯЕС18Т` -> `RECIST` | occ=1 | docs=1
- `О04ВБ` -> `G04BD` | occ=1 | docs=1
- `М1Ь` -> `M1b` | occ=1 | docs=1
- `VО2` -> `VO2` | occ=1 | docs=1
- `Н2O` -> `H2O` | occ=1 | docs=1
- `ШРА8А` -> `RIPASA` | occ=1 | docs=1
- `М1с2` -> `M1c2` | occ=1 | docs=1

## НЕ применённые (0 дословных вхождений)
- `Herpes С simplex virus` -> `Herpes simplex virus` [human_verified_5a] — фраза только в разрыве табличных ячеек, requeue

## Скринер (corruption_screen) — корпус до/после промоушена
Прогон `corruption_screen.py outout_latin --baseline <preoverlay>`:
- adjacent_mixed  4835 -> 4824 (-11)
- digit_in_cyrword 2025 -> 1970 (-55)
- impossible_start  534 ->  529 (-5)
- homoglyph_in_latin  10 ->   10 (0)
- shattered_latin  2758 -> 2759 (+1)

Прецизионная порча УПАЛА (digit_in_cyrword -55: М1а->M1a, 801АА->S01AA; impossible_start -5:
Ьйрз->https; adjacent_mixed -11). shattered_latin +1 (по 81 док. локально +8) — НЕ порча
оверлея (токен-диф доказал: оверлей вносит только чистые corrected-значения): это (а) кросс-
процессная НЕдетерминированность Tesseract в библиографических OCR-зонах и (б) шумный детектор
(96% шум по замеру) НОВО замечает ПРЕДсуществующие одиночные кир-раны, чей сосед стал чистой
латиницей после проверенной правки. Новой порчи оверлей не внёс.

## manifest --verify
Базовый корпус outout/ (batch_report, БЕЗ latin_recovery) шагом 1 НЕ затронут: изменение
движка работает только при latin_recovery=True, а outout/ строит batch_report без флага.
Доказательство: mtime всех outout/*.json = 2026-07-20 14:59 и старше (сессия шага 1 —
2026-07-22), т.е. в этой сессии outout/ не переписывался.

manifest --verify показывает расхождения, НО baseline_manifest.json устарел (2026-07-14,
до ветки audit-fix): «722 outputs изменились», counts/report сдвинулись — это накопленная
история ветки (последняя регенерация outout/ 2026-07-20), НЕ шаг 1. inputs — без расхождений
(722). code.tree_sha256 изменился ожидаемо (моя правка latinrecovery.py). Пере-снапшот
манифеста вне scope шага 1 (отдельная задача управления baseline).
