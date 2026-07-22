# Дельта-отчёт: применение проверенных правок к тексту корпуса (ROADMAP шаг 1)

Механизм: движковый оверлей `self._g` (decision=auto, source=human_verified) + перепарс
с OCR. **Аудит airtight**: OFF и ON спарены в одном процессе (Tesseract не детерминирован
между процессами); токен-диф выравнивания OFF/ON — каждое отличающееся ON-ядро обязано
быть проверенным corrected-значением (оверлей внёс РОВНО проверенный набор, не тронул
прочее). Оверлей ПЕРЕКРЫВАЕТ ошибочные канальные авто-решения (напр. `Н2О`->`H20`(канал) ->
`H2O`(человек)).

## Гейт аудита
- док. чистых: **97 / 97** (грязных 0, crash 0)
- контрольная I1: OFF==ON **ДА** (оверлей инертен на здоровом; 6 док.)

## Итог
- затронуто док.: **97**
- форм применено: **129 / 132** (не применено: **3**)
- всего замен (occurrences): **854**

## По провенансу (occurrences)
- `human_verified_5a`: 505
- `ls_verified_full`: 245
- `reverified_homoglyph_norm`: 59
- `reverified_human_verified+homoglyph_norm`: 33
- `reverified_crop_arbitrated`: 12

## Применённые формы (source -> corrected | occ | #docs)
- `оГ` -> `of` | occ=347 | docs=32
- `8са1е` -> `Scale` | occ=48 | docs=13
- `Н2О` -> `H2O` | occ=48 | docs=8
- `гезропзе` -> `response` | occ=29 | docs=13
- `Herpes	С
	simplex virus` -> `Herpes simplex virus` | occ=14 | docs=6
- `Hepatitis В` -> `Hepatitis B` | occ=12 | docs=11
- `JO1XХ` -> `J01XX` | occ=12 | docs=6
- `М1а` -> `M1a` | occ=12 | docs=8
- `1Ш88СО` -> `RUSSCO` | occ=12 | docs=5
- `Нераййз` -> `Hepatitis` | occ=10 | docs=5
- `СНп` -> `Clin` | occ=9 | docs=8
- `МсОШ` -> `McGill` | occ=8 | docs=4
- `8ирр1` -> `Suppl` | occ=8 | docs=8
- `смН2О` -> `см H2O` | occ=7 | docs=5
- `уегзюп` -> `version` | occ=7 | docs=6
- `Ьйрз` -> `https` | occ=7 | docs=6
- `Т1Ь` -> `T1b` | occ=6 | docs=6
- `рО2` -> `pO2` | occ=6 | docs=3
- `зеуегйу` -> `severity` | occ=6 | docs=5
- `аси!е` -> `acute` | occ=5 | docs=4
- `СагЬопе` -> `Carbone` | occ=5 | docs=4
- `сЬготс` -> `chronic` | occ=5 | docs=4
- `Аззау` -> `Assay` | occ=4 | docs=2
- `Т4Ь` -> `T4b` | occ=4 | docs=4
- `аегщтоза` -> `aeruginosa` | occ=4 | docs=3
- `Огабе` -> `Grade` | occ=4 | docs=4
- `М1с` -> `M1c` | occ=4 | docs=3
- `сНзеазе` -> `disease` | occ=4 | docs=2
- `зоНб` -> `solid` | occ=4 | docs=3
- `ЕКА8` -> `ERAS` | occ=4 | docs=3
- `Рпшагу` -> `Primary` | occ=4 | docs=3
- `N1а` -> `N1a` | occ=4 | docs=2
- `сйзеазе` -> `disease` | occ=4 | docs=3
- `Зсоге` -> `Score` | occ=4 | docs=3
- `гапёогшгеё` -> `randomized` | occ=4 | docs=2
- `1МКТ` -> `IMRT` | occ=4 | docs=4
- `ВАР1` -> `BAP1` | occ=4 | docs=2
- `Е8РЕЫ` -> `ESPEN` | occ=3 | docs=3
- `рпшагу` -> `primary` | occ=3 | docs=3
- `раШбит` -> `pallidum` | occ=3 | docs=3
- `Штогз` -> `tumors` | occ=3 | docs=3
- `ууотеп` -> `women` | occ=3 | docs=2
- `Тохюку` -> `Toxicity` | occ=3 | docs=3
- `Кезрн` -> `Respir` | occ=3 | docs=2
- `асбуйу` -> `activity` | occ=3 | docs=2
- `йозе` -> `dose` | occ=3 | docs=3
- `81ейепз` -> `Steffens` | occ=3 | docs=3
- `тГесйоп` -> `infection` | occ=3 | docs=2
- `801РВ01` -> `S01FB01` | occ=3 | docs=1
- `МО4` -> `MO4` | occ=3 | docs=1
- `МесНса1` -> `Medical` | occ=3 | docs=3
- `гедгеззюп` -> `regression` | occ=3 | docs=3
- `Ьир://огс1с1.ог` -> `http://orcid.org/0000-0001-7098-4584` | occ=3 | docs=2
- `Тохюйу` -> `Toxicity` | occ=3 | docs=3
- `МЕОЫЫЕ` -> `MEDLINE` | occ=3 | docs=3
- `801АА` -> `S01AA` | occ=3 | docs=2
- `Ггош` -> `from` | occ=3 | docs=2
- `сПшса1` -> `clinical` | occ=3 | docs=3
- `aeruginosa, В. cepacia comрlex` -> `aeruginosa, B. cepacia complex` | occ=3 | docs=2
- `райеп!з` -> `patients` | occ=3 | docs=3
- `Кезрп` -> `Respir` | occ=3 | docs=3
- `51апс1аП5` -> `standarts` | occ=3 | docs=3
- `сопзегуайуе` -> `conservative` | occ=3 | docs=2
- `1итог` -> `tumor` | occ=3 | docs=2
- `Райеп` -> `Patient` | occ=3 | docs=2
- `еу1с1епсе` -> `evidence-based` | occ=2 | docs=2
- `М1b` -> `M1b` | occ=2 | docs=2
- `ОеуеЬртеп` -> `Development` | occ=2 | docs=2
- `НЬзАд` -> `HbsAg` | occ=2 | docs=2
- `Т2в` -> `T2b` | occ=2 | docs=1
- `7ЧЕС` -> `NEC` | occ=2 | docs=1
- `Шзеазе` -> `disease` | occ=2 | docs=2
- `МРА8А` -> `RIPASA` | occ=2 | docs=1
- `бейшепсу` -> `deficiency` | occ=2 | docs=2
- `РКАХ` -> `FRAX` | occ=2 | docs=2
- `810РЕЬ` -> `SIOPEL-3` | occ=2 | docs=1
- `01зеазе` -> `Disease` | occ=2 | docs=2
- `Мес1` -> `Med` | occ=2 | docs=2
- `Ншпап` -> `Human` | occ=2 | docs=2
- `Ь04АВ02` -> `L04AB02` | occ=2 | docs=1
- `уапсез` -> `varices` | occ=2 | docs=2
- `N2а` -> `N2a` | occ=2 | docs=1
- `ЕТ1Ш8` -> `ETDRS` | occ=2 | docs=1
- `РO2` -> `pO2` | occ=2 | docs=1
- `Уо1.54` -> `Vol.54` | occ=2 | docs=2
- `T2в` -> `T2b` | occ=2 | docs=1
- `К.1188СО` -> `RUSSCO` | occ=2 | docs=2
- `зишшагу` -> `summary` | occ=2 | docs=2
- `N1с` -> `N1c` | occ=1 | docs=1
- `КЕС15Т` -> `RECIST` | occ=1 | docs=1
- `VО2` -> `VO2` | occ=1 | docs=1
- `АО5` -> `A05` | occ=1 | docs=1
- `С04ЕЮ` -> `G04BD` | occ=1 | docs=1
- `Уо1.22` -> `Vol. 224` | occ=1 | docs=1
- `8ЮРЕЬ` -> `SIOPEL-3` | occ=1 | docs=1
- `Т04ААЗЗ` -> `L04AA33` | occ=1 | docs=1
- `Б04ААЗЗ` -> `L04AA33` | occ=1 | docs=1
- `О04ВБ` -> `G04BD` | occ=1 | docs=1
- `Е8НКЕ` -> `ESHRE` | occ=1 | docs=1
- `1А1НС` -> `IAIHG` | occ=1 | docs=1
- `801ЕС` -> `S01EC` | occ=1 | docs=1
- `гЦТ11Ш1с!еПпе` -> `rUTIguideline` | occ=1 | docs=1
- `М1a` -> `M1a` | occ=1 | docs=1
- `А8КМ` -> `ASRM` | occ=1 | docs=1
- `Уо1.1` -> `Vol. 15` | occ=1 | docs=1
- `Ю4ААЗЗ` -> `L04AA33` | occ=1 | docs=1
- `301СЯ` -> `J01CR` | occ=1 | docs=1
- `ШРА8А` -> `RIPASA` | occ=1 | docs=1
- `801АЕ` -> `S01AE` | occ=1 | docs=1
- `N2с` -> `N2c` | occ=1 | docs=1
- `CHА2DS2` -> `CHA2DS2-VASc` | occ=1 | docs=1
- `511ТЗ` -> `5HT3` | occ=1 | docs=1
- `РО2` -> `pO2` | occ=1 | docs=1
- `М1с2` -> `M1c2` | occ=1 | docs=1
- `ЮЙ8` -> `Kids` | occ=1 | docs=1
- `СА19.9` -> `CA19.9` | occ=1 | docs=1
- `АС55` -> `ACSS` | occ=1 | docs=1
- `рМ1а` -> `pN1a` | occ=1 | docs=1
- `Н2O` -> `H2O` | occ=1 | docs=1
- `1Ш8БСО` -> `RUSSCO` | occ=1 | docs=1
- `801РА` -> `S01FA` | occ=1 | docs=1
- `М1с1` -> `M1c1` | occ=1 | docs=1
- `М1Ь` -> `M1b` | occ=1 | docs=1
- `8МАКСВ1` -> `SMARCB1` | occ=1 | docs=1
- `Р01РОХ1Ш` -> `FOLFOXIRI` | occ=1 | docs=1
- `ШЛ88СО` -> `RUSSCO` | occ=1 | docs=1
- `ТН1М` -> `THIM` | occ=1 | docs=1
- `ЯЕС18Т` -> `RECIST` | occ=1 | docs=1
- `ЯА8` -> `RAS` | occ=1 | docs=1

## НЕ применённые (0 дословных вхождений)
- `Ш-1У` -> `III-IV` [ls_verified_full] — фраза только в разрыве табличных ячеек, requeue
- `Н-Ш` -> `II-III` [ls_verified_full] — фраза только в разрыве табличных ячеек, requeue
- `Herpes С simplex virus` -> `Herpes simplex virus` [human_verified_5a] — фраза только в разрыве табличных ячеек, requeue
## Обновление из ПОЛНОГО набора LS (латин_verify, 170 аннотаций)
Пере-собран из 170 аннотаций LS (121 принять + 49 ошибка) через `rebuild_verified_from_ls.py`:
new-derived 151 ∪ старые 72 (старое побеждает — сохранена пере-сверка: pO2-регистр, T2в->T2b,
crop-арбитраж JO1XX) = **154 формы**. 3 невалидных ATC (СО2АВО2/СО2АВО1/СО8САО2) НЕ применены —
в `_requeue_critical.json`.

### Гейт безопасности (`safety_filter_verified.py`) — 22 формы ОТЛОЖЕНЫ, не применены глобально
Очередь дедуплицировала по форме, смешав латинские и РУССКИЕ вхождения — правка одного кропа
человеком не должна латинизировать ВСЕ вхождения. Отложено в `_deferred_ambiguous.json`
(разберёт per-occurrence LLM-проход шага 2):
- инициалы: `Е.А`->E.A (167 вхождений — «Вишнёва Е.А.» рус. инициалы!), `Р.А`, `ЕА`;
- короткие неоднозначные кир. фрагменты (<=3): `ап`->an («Снижение ап-» рус.), `апб`/`апс`->and
  («апб-ЗМА»=anti-SMA!), `ех`, `рат`, `Ше`, `Йге`, `Ьйр`, `ЫК`, `Ыо`, `СПп`, `йз`, `уап`, `Рат`, `Раш`;
- фразы с одиночным СТРОЧНЫМ кир. токеном = рус. предлог/маркер: `difficile с`/`virus с`/`AL-A с`
  (=«с» with), `AOSpine: а` (=пункт «а»).
Итог применяемого набора: **132 формы** (72 старых + 60 новых безопасных), 97 затронутых док.

## Скринер (corruption_screen) — прецизионная порча
- digit_in_cyrword: 2025 -> **1962 (-63)** (М1а->M1a, 801АА->S01AA, коды)
- adjacent_mixed: 4835 -> **4820 (-15)**
- impossible_start: 534 -> **531 (-3)** (Ьйрз->https)
- shattered_latin: 2758 -> 2759 (+1); homoglyph_in_latin: 10 -> 15 (+5)
Основные каналы порчи УПАЛИ. Рост homoglyph_in_latin (+5) — НЕ оверлей (токен-диф доказал:
оверлей только УБИРАЕТ гомоглифы): это `Hepatitis А/С/Е virus` (кир. гласная), которых НЕТ в
проверенном наборе (только `Hepatitis В`->B) — предсуществующая порча, рендерящаяся текущим
движком при перепарсе; кандидаты в очередь шага 2.

## manifest --verify
Базовый `outout/` шагом 1 НЕ затронут (mtime 2026-07-20, оверлей только при latin_recovery=True).
Инструменты: `rebuild_verified_from_ls.py`, `safety_filter_verified.py`, `apply_and_audit_final.py`.
