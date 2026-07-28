# Независимый технический аудит парсера клинических рекомендаций

**Дата среза:** 11 июля 2026 года.  
**Роль:** независимый технический критик. Исходный код и результаты парсинга не изменялись.  
**Цель аудита:** определить, можно ли использовать 722 JSON как обучающий корпус для русскоязычной медицинской LLM и правильно ли выбрано техническое направление.

## Краткий вердикт

**Направление верно лишь на уровне базовых примитивов, но неверно на уровне архитектуры доверия и публикации корпуса.** PyMuPDF как быстрый native-text baseline, профиль клинических рекомендаций, page-level OCR для дефектных страниц и отдельное сохранение excluded-контента — разумные решения. Однако текущая реализация объединяет их в невоспроизводимый, разрушающий provenance pipeline с fail-open валидацией. Поэтому менять PyMuPDF «целиком на модную библиотеку» не нужно; менять нужно архитектуру извлечения, независимую проверку и release gate.

**JSON «как есть» для обучения медицинской LLM не годится.** Формальный отчёт показывает `616/722 = 85,3% PASS`, но `459/616 = 74,5%` этих PASS содержат предупреждения. Без предупреждений остаются `157/722 = 21,7%`, и даже в этой группе есть подтверждённый ложный PASS (`КР848_1`: один раздел при 29 пунктах оглавления). Независимо аттестованных относительно визуального оригинала файлов в корпусе нет: **готовность к выпуску на обучение в текущем виде — 0%**. Это оценка готовности release-пакета, а не утверждение, что 0% текста верно.

Для двух основных отладочных документов измеренный независимый proxy точного совпадения токенов с OCR визуального рендера составляет:

| Документ | `sections`: exact tokens | `sections`: char alignment | Весь JSON: exact tokens | Весь JSON: char alignment |
|---|---:|---:|---:|---:|
| КР1_4 | 92,62% | 93,56% | 78,15% | 80,73% |
| КР628_2 | 92,46% | 87,17% | 76,40% | 77,59% |

Это **не CER и не доказанная доля правильных символов**: второй OCR сам ошибается. Метрика служит воспроизводимым сигналом расхождения; клинически значимые ошибки и потерянные таблицы дополнительно подтверждены визуально.

## Объём и методика

Проверены:

- `X:\parser\crparser\engine\*`, `X:\parser\crparser\profiles\clinical.py`, `X:\parser\validate.py`, `X:\parser\batch_report.py`;
- `X:\parser\_corpus\report.json`, `X:\parser\_corpus\FINAL_REPORT.txt` и все 722 JSON в `X:\parser\outout`;
- `X:\parser\data\raw\КР1_4.pdf` против `X:\parser\outout\КР1_4.json`;
- `X:\parser\data\raw\КР628_2.pdf` против `X:\parser\outout\КР628_2.json`;
- текущий `X:\parser\crparser\data\ocr_pins.json`;
- официальные материалы по Docling, Marker, LiteParse/LlamaParse, GROBID, PaddleOCR, EasyOCR, Surya, Tesseract, fontTools и PyMuPDF.

Все 149 страниц были отрендерены PyMuPDF в grayscale при 2,2× (около 158 dpi) и независимо распознаны Tesseract 5.4.0 `rus+eng --psm 6`; спорные фрагменты сверялись на рендерах 2,5–3×. Нумерация страниц ниже — физическая PDF page, начиная с 1. Нативный PDF-текст использовался для навигации и проверки кодовых точек, но не как ground truth.

`Exact-token attestation`: Unicode-слова `\w+`, `casefold`, без пунктуации; мультимножественное пересечение JSON с OCR соответствующего диапазона страниц, делённое на число токенов JSON. `Char alignment`: `casefold`, `ё→е`, пунктуация→пробел, пробелы схлопнуты; normalized Levenshtein similarity, а для whole JSON — взвешенное по длине полей среднее. Token multiset не проверяет порядок и способен завышать качество; OCR также ошибается. Поэтому эти числа — proxy расхождения, не CER.

Корпус содержит 57 681 страницу в 722 PDF; среднее — 79,9 страницы, медиана — 66, 90-й процентиль — 140. Значит, выводы по двум отладочным документам не могут аттестовать корпус.

Ограничения аудита:

- нет вручную транскрибированного и медицински верифицированного gold set;
- `_corpus/report.json` датирован 9 июля 2026 года, а `КР1_4.json`, `КР628_2.json`, `ocr.py` и `ocr_pins.json` изменялись позже; report не хранит hashes входов/выходов и версию кода;
- машинное page-aligned сравнение охватывает все 149 страниц, а визуальная проверка — спорные фрагменты и таблицы; полной ручной транскрипции 149 страниц нет;
- оценки стоимости ниже — инженерные person-days при условии одного senior Python engineer, знакомого с проектом; медицинская разметка считается отдельно.

## 1. Архитектура решения

### 1.1. Что сломается первым

Первый содержательный отказ на новом типе документа — **таблицы и границы регионов**, а не PyMuPDF. Первый операционный отказ на масштабе — **OCR oversubscription и невоспроизводимое состояние кэша/пинов**.

#### P0 — batch может успешно закончиться с отсутствующим или устаревшим JSON

- `X:\parser\batch_report.py:62-68`: ошибка записи только ставит `write_ok=False`; валидация продолжается по объекту в памяти.
- `X:\parser\batch_report.py:73-76`: после crash старый JSON не инвалидируется.
- `X:\parser\batch_report.py:89-103`: integrity-check проверяет лишь, что существующие JSON синтаксически читаются; не сверяет ожидаемое множество, hash или свежесть.
- `X:\parser\batch_report.py:143-156`: failed writes печатаются, но не определяют exit code.

Следствие проверяемо из кода: report способен описывать объект, который не был опубликован, а старый JSON способен пройти финальную проверку.

**Безопасная альтернатива:** run-scoped staging, manifest `{input_sha256, output_sha256, parser_version, config_hash, status}`, проверка точного множества результатов и атомарная публикация только allowlist. Цена: 5–8 инженерных дней; временно почти двойной объём диска.

#### P0 — coverage измеряет бухгалтерию символов, а не сохранность структуры

- `X:\parser\crparser\engine\stats.py:28-43` складывает длину `sections`, всех `excluded` и `tables`.
- `X:\parser\crparser\engine\stats.py:41-49` обрезает переполнение через `min(accounted_raw, total_chars)`.

Текст, попавший не в тот раздел, в `excluded.other` или продублированный между buckets, считается покрытым. Синтетическая проверка «один символ в section + 99 в excluded при 100 исходных» дала `coverage=100%`.

Живые примеры:

- `X:\parser\_corpus\report.json:11868-11878`: КР675_2 — coverage 100%, при этом MISSING 3.3–3.4.2;
- `X:\parser\_corpus\report.json:4827-4837`: КР284_2 — coverage 100%, потеря четырёх разделов;
- `X:\parser\_corpus\report.json:17211-17219`: КР839_1 — coverage 100%, потеря глав 4 и 5.

**Альтернатива:** stable line/span IDs и три отдельные метрики: source-retention union, correctly-structured union и excluded union; отдельно считать overlap/duplication. Цена: 10–15 дней вместе с provenance migration; JSON станет больше.

#### P0 — таблицы удаляются из текста до доказательства успешного извлечения

- `X:\parser\crparser\engine\tables.py:288-292`: whitespace fallback запускается только если рамочный детектор не нашёл ни одной таблицы во всём документе. Документ со смешанными рамочными и безрамочными таблицами детерминированно теряет вторые.
- `X:\parser\crparser\engine\tables.py:273-283`: bbox включается в `subtraction_map` независимо от качества `raw_text`.
- `X:\parser\crparser\engine\tables.py:344-352`: при ошибке dump возвращается пустая строка, но bbox уже заявлен.
- `X:\parser\crparser\engine\parser.py:74-76,93-100`: Segmenter получает destructive subtraction.

Табличный текст повторно берётся из native pdfplumber-слоя, а не из восстановленного OCR view. Поэтому body можно починить OCR, а таблицу — одновременно удалить из body и сохранить битой. Именно это наблюдается на КР1_4 и КР628_2.

**Альтернатива:** page-level ансамбль table candidates, `TableExtractionResult(tables, claimed_line_ids, diagnostics)` и вычитание только после непустого reconciled результата. Цена: 5–10 дней для адаптера и дедупликации; выше CPU.

#### P0 — OCR cache и pins зависят от истории корпуса

- `X:\parser\crparser\engine\parser.py:140-141` и `pdf_reader.py:189-191`: `doc_id` — basename без content hash.
- `X:\parser\crparser\engine\ocr.py:811-840,853-879`: cache key не содержит hash PDF, Tesseract/traineddata/Pillow version.
- `X:\parser\crparser\engine\ocr_pins.py:48-80`: база кэшируется процессом, а накопленные ключи сами запускают OCR на следующих документах.
- `X:\parser\crparser\engine\ocr.py:699-702`: глобальная база подмешивается в текущую карту замен.
- `X:\parser\crparser\engine\ocr_pins.py:100-130`: старые shards не удаляются, первое значение выигрывает, I/O-ошибки подавляются.

В текущем `ocr_pins.json` есть конфликтно-опасные соответствия: `"рб1": "PDI"` (`X:\parser\crparser\data\ocr_pins.json:24`), хотя в контексте checkpoint-рецептора правильная форма — `PD1`; параллельно есть `"рб1/рви": "PD1/PD-L1"` (`:170`). В КР1_4 реально остаётся `PDI` в клиническом тексте (`X:\parser\outout\КР1_4.json:187,346`).

**Альтернатива:** content-addressed cache по hash PDF/page render и полному fingerprint OCR stack; pins — версионированный, reviewable, immutable input конкретного run. Цена: 4–7 дней плюс миграция кэша.

### 1.2. Скрытая связанность

Заявленная абстракция `DocumentProfile` не совпадает с реальной границей системы:

- hook `heading_order_valid` объявлен, но не вызывается (`X:\parser\crparser\profiles\base.py:138-149`);
- `segmenter.py` содержит КР-специфические константы и политику Roman/local numbering (`X:\parser\crparser\engine\segmenter.py:40-44,439-446,659-686`);
- engine hardcode-ит русские названия регионов и таблиц (`ocr.py:96-106`, `tables.py:44-50`);
- parser использует `getattr(profile, "registry")` и магический `_warnings` (`parser.py:79-91`).

Новый профиль не может переопределить numbering, content boundary, table/OCR policy и порядок регионов. Поэтому либо модуль надо честно назвать `ClinicalRecommendationEngine`, либо вынести `NumberingPolicy`, `RegionPolicy`, `ContentBoundaryPolicy`, `OcrPolicy`, `TablePolicy`.

### 1.3. Provenance вычисляется и теряется

- profile обещает стабильный `section_id` (`X:\parser\crparser\profiles\base.py:73-79`) и создаёт его (`clinical.py:395-405`);
- `Section` не имеет этого поля (`models.py:98-106`);
- Segmenter и serializer его отбрасывают (`segmenter.py:557-564`, `jsonio.py:62-69`);
- page/bbox заголовка также теряются (`models.py:79-80`, `segmenter.py:374-375`).

В результате нельзя доказать, из какой страницы/строки получен section, обнаружить duplication или безопасно отменить ошибочное вычитание таблицы.

### 1.4. Heading/numbering переобучены на один формат КР

- одиночный арабский номер всегда level 1 (`clinical.py:281-326`), а при локальных `1,2,3` после римской главы Roman chapters отключаются целиком (`segmenter.py:659-686`);
- inline split допускает только главы 1–7 (`clinical.py:171-176`);
- каноничность использует `startswith`, а слабые visual/uppercase/lexical сигналы способны промотировать heading (`clinical.py:433-448,577-620`);
- неприклеившийся `NUMBER_ONLY` уже потреблён и теряется (`segmenter.py:463-485`).

Это объясняет, почему известные «~16 обвалов `_find_content_start`» на самом деле не один bug class. Read-only трассировка показала минимум три причины:

1. start внутри TOC: КР715_2 (`X:\parser\outout\КР715_2.json:26-32`) и КР675_2 (`X:\parser\outout\КР675_2.json:19-25`);
2. start в glossary/front matter: КР891_1, КР115_2, КР467_3;
3. несовместимая numbering model при корректном старте: КР284_2 (`X:\parser\outout\КР284_2.json:25-70`) и КР750_1.

Не исправлять один глобальный threshold — разумно. Оставлять эти файлы в общей публикуемой директории — неразумно. Сейчас hard quarantine отсутствует.

### 1.5. Операционная масштабируемость

- 14 процессов зафиксированы в `X:\parser\batch_report.py:129-131`;
- рендер OCR — 400 DPI (`ocr.py:63-78`);
- hybrid candidate может OCR-иться на всех страницах (`ocr.py:910-946`);
- PDF повторно читается валидатором для TOC (`batch_report.py:68`, `validate.py:116-132`).

На 57 681 странице это создаёт RAM/I/O pressure и повторную работу. Нужна отдельная OCR queue с memory budget, динамическим worker count, per-document timeout и reuse одного immutable `DocumentView`.

## 2. Технологии и альтернативы

### 2.1. Оценка текущего стека

**PyMuPDF — оставить.** Для born-digital PDF это быстрый и управляемый baseline. Он не создаёт дефектный ToUnicode; он честно извлекает то, что закодировано в PDF. Ошибка проекта — считать native layer авторитетом там, где он доказанно битый, и затем разрушающе смешивать его с OCR.

**Tesseract rus+eng — оставить как baseline/challenger, не как единственный oracle.** Официальная документация прямо связывает качество с preprocessing, page segmentation и словарями; для таблиц требуются специальные стратегии ([Tesseract quality guide](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)). Текущая проблема — не сам Tesseract, а merge, скрытое состояние pins и отсутствие независимого gold benchmark.

**Собственный Cyrillic-anchor merge и §-детектор — годятся только как triage.** Они полезны для поиска подозрительных страниц, но не доказывают корректность строки. На двух target PDF не найден переход «чистый native → плохой final»; зато многократно подтверждён отказ «грязный native признан авторитетным и не заменён правильным visual/OCR». § density имеет нулевой recall на текущем PASS-корпусе при действующем threshold.

**Собственный table detector — заменить первым.** Реальные target PDF уже демонстрируют overcapture, truncation и пропущенные multi-page таблицы.

### 2.2. Сравнение альтернатив

| Альтернатива | Что даст лучше | Что даст хуже / риск | Стоимость перехода | Вывод |
|---|---|---|---:|---|
| **Docling standard/hybrid** | Детерминированный parse, OCR, layout и neural table structure в одном IR; hybrid умеет сохранять backend text и направлять images/tables в VLM | Не знает канонической структуры российских КР; всё равно нужен profile и local gold | POC 3–5 дней; production 2–3 недели | Лучший общий challenger/orchestrator, не wholesale replacement |
| **Marker** | JSON/Markdown, layout, таблицы, формы; force OCR и optional LLM | GPU/PyTorch footprint; GPL-код и отдельные условия model weights; клиническая схема не встроена | POC 3–5 дней; production 3–4 недели + license review | Сильный challenger, но не default без legal/GPU решения |
| **LiteParse** | Быстрый local PDFium parse, bbox, complexity routing, подключаемый OCR | Heuristic Markdown; сложные плотные/multi-page tables остаются слабым местом | POC 2–4 дня | Хороший fast baseline/router, не замена клинического профиля |
| **LlamaParse** | Agentic OCR, сложные layout и multi-page tables, schema extraction | Cloud/vendor dependency, конфиденциальность, повторяющаяся стоимость; 57 681 страниц сильно выше free allowance | POC 1–3 дня; mapping 1–2 недели; затем usage cost | Использовать как exception challenger, не основной batch |
| **GROBID** | Научные статьи, библиография, TEI | Его FAQ прямо ограничивает section hierarchy и extraction строк/ячеек таблиц | POC 2–3 дня | Отклонить для основного pipeline; возможен только для references |
| **PaddleOCR PP-StructureV3** | Layout, reading order, table/formula recognition, Markdown; CPU/GPU; обучаемость | Тяжёлый runtime; лучший throughput требует GPU и отдельной русской проверки | Adapter 3–5 дней; benchmark/tuning 1–2 недели | Лучший OCR/table challenger |
| **EasyOCR** | Простая замена OCR, Cyrillic, CPU/GPU | Нет полноценного table/layout pipeline | 1–2 дня | Только OCR baseline; переход без benchmark не обоснован |
| **Surya** | OCR 90+ языков, layout, reading order, table recognition | GPU footprint и лицензионные условия weights; нужна русская доменная проверка | Adapter 3–5 дней; benchmark 1–2 недели | Перспективный второй challenger |
| **VLM по рендеру** | Понимание сложного layout/table, восстановление exception pages | Hallucination, недетерминизм, cost/privacy; нельзя использовать как неоспоримый source | Exception prototype 2–4 дня | Только adjudication/exception route |
| **cmap/glyph repair + fontTools** | Сохраняет геометрию, числа и стиль без OCR; потенциально дешевле на CID-defect pages | Не решает визуально одинаковые Latin/Cyrillic homographs без контекста; не все fonts извлекаемы | Pilot 3–7 дней; robust 2–3 недели | Обязательный пилот перед OCR на повреждённых subset fonts |

Факты по возможностям взяты из первичных источников:

- Docling описывает standard pipeline как deterministic parse + optional OCR + neural table structure и отдельно hybrid `force_backend_text=True` ([Docling pipelines](https://docling-project.github.io/docling/examples/agent_skill/docling-document-intelligence/pipelines/)).
- Marker поддерживает PDF→JSON/Markdown, tables/forms/equations и optional LLM; README также фиксирует GPL-код и modified model license ([Marker README](https://github.com/datalab-to/marker/blob/master/README.md)).
- LiteParse заявляет local parsing, bbox и pluggable OCR ([LiteParse](https://github.com/run-llama/liteparse)).
- LlamaParse даёт 10 000 free credits, около 1 000 страниц/месяц; это 1,7% текущего корпуса ([LlamaIndex/LlamaParse](https://www.llamaindex.ai/)).
- GROBID ориентирован на technical/scientific publications, а FAQ говорит, что hierarchy разделов надёжно не различается и строки/ячейки таблиц не извлекаются ([GROBID introduction](https://grobid.readthedocs.io/en/latest/Introduction/), [GROBID FAQ](https://grobid.readthedocs.io/en/latest/Frequently-asked-questions/)).
- PP-StructureV3 включает layout, table/formula recognition, reading order и Markdown; CPU поддержан, но официальный benchmark показывает существенный выигрыш GPU ([PP-StructureV3](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-StructureV3/PP-StructureV3.html)).
- EasyOCR поддерживает Cyrillic ([EasyOCR](https://github.com/JaidedAI/EasyOCR)); Surya заявляет OCR/layout/order/table для 90+ языков ([Surya](https://github.com/datalab-to/surya)).
- `TTFont` предоставляет доступ к TrueType/OpenType tables и glyph contours ([fontTools TTFont](https://fonttools.readthedocs.io/en/latest/ttLib/ttFont.html), [fontTools tables](https://fonttools.readthedocs.io/en/latest/ttLib/tables.html)).

Оба target PDF содержат subset TrueType Type0/Identity-H fonts с `/ToUnicode` и `/CIDToGIDMap`. Это делает font-level pilot технически обоснованным: сопоставлять code→CID→GID и outline с эталонным шрифтом, а неоднозначные гомографы отдавать контексту/OCR. Он не является полной заменой OCR.

### 2.3. Рекомендуемый курс

Не заменять весь pipeline одним продуктом. Целевой порядок:

1. **Immutable page IR:** line/span ID, page, bbox, source channel (`native`, `font-repair`, `ocr`, `vlm`), confidence, исходный и нормализованный текст.
2. **Page router:** хороший native layer → PyMuPDF; broken CID → font repair + OCR comparison; scan → OCR; table pages → dedicated table model; VLM → только exception/adjudication.
3. **Clinical profile поверх IR:** канонические разделы и metadata остаются доменной логикой, но не удаляют source evidence.
4. **Независимый release gate:** schema, source-span coverage/overlap, gold regression и hard quarantine.

POC: current vs Docling standard/hybrid vs PaddleOCR/Surya на 30–50 стратифицированных документах (около 1 000 страниц). Сравнивать не общий OCR score, а section recall/precision, heading assignment, table cell F1, numeric/unit exactness, mixed-script error rate и unsupported-content rate.

## 3. Валидация и доверие к PASS

### 3.1. Что PASS реально гарантирует

PASS гарантирует только отсутствие сообщений в текущих `fails`, `reviews` и `skipped` при конкретном наборе эвристик. Он **не гарантирует**:

- соответствие JSON schema;
- наличие большинства разделов, если TOC не распознан или использует Roman numbering;
- правильное назначение текста разделу;
- отсутствие дубликатов между sections/excluded/tables;
- полноту таблиц;
- точность чисел, единиц и Latin terms;
- отсутствие остаточной OCR/CID-порчи;
- соответствие записанного файла валидированному объекту;
- воспроизводимость другим запуском.

Кодовая причина:

- `X:\parser\validate.py:170-182`: status PASS определяется отсутствием skipped/fails/reviews, а warnings игнорируются;
- там же `Report.ok` игнорирует reviews;
- `X:\parser\validate.py:552-564`: CLI считает REVIEW успешным и возвращает exit 0;
- `X:\parser\crparser\engine\jsonio.py:22-29,62-83`: сериализация формы не является schema validation;
- пустой `{}` интерпретируется как scan/SKIP, а не schema failure (`validate.py:185-210`).

Patch «0 sections → REVIEW» (`validate.py:203-225`) закрывает один точный симптом. Он не закрывает один мусорный section, Roman TOC, TOC=None, ложные stats или схлопывание всех глав внутрь одного section.

### 3.2. Состояние report.json

Текущий `_corpus/report.json`:

| Статус | Файлов | Доля |
|---|---:|---:|
| PASS | 616 | 85,3% |
| FAIL | 58 | 8,0% |
| REVIEW | 48 | 6,6% |
| Всего | 722 | 100% |

Из 616 PASS:

- 459 имеют warnings (`74,5%` PASS; `63,6%` всего корпуса);
- 157 не имеют warnings (`25,5%` PASS; `21,7%` всего корпуса);
- категории PASS warnings, с пересечениями: EXTRA 329, SOURCE_DEFECT 162, COVERAGE 137, TITLE_MISMATCH 102, TOC 23, TABLES 10.

`report.json` — bare array без generated_at, git SHA, parser/config version, input/output hashes и thresholds (`X:\parser\batch_report.py:78-85,136-137`). `_corpus/FINAL_REPORT.txt` уже противоречит текущему report: строки 4–9 говорят 623/700 PASS и 77 FAIL, строки 19–20 считают 21 scan необработанными, строка 23 считает КР875_1 PASS; текущий status КР875_1 — REVIEW (`X:\parser\_corpus\report.json:18229-18240`).

На момент аудита `report.json` имел mtime 9 июля, а оба target JSON — более поздний. Повторная валидация текущих КР1_4 и КР628_2 дала те же статусы, но report не содержит hashes, поэтому идентичность байтов историческому run недоказуема.

### 3.3. Подтверждённые ложные PASS

1. **КР848_1 — чистый PASS без warnings, одна секция вместо структуры документа.** `X:\parser\_corpus\report.json:17488-17505`: sections=1, coverage=100. В `X:\parser\outout\КР848_1.json:27-35` единственная секция `XII`, а её текст начинается с `XIII. Список литературы`; `excluded.other` содержит 834 элемента. PDF TOC содержит 29 Roman entries, но validator проверяет только Arabic numeric regex (`X:\parser\validate.py:63,100-101`). Сумма included/excluded/tables превышает source total и обрезается до 100% (`КР848_1.json:3447-3456`).
2. **КР401_2 — PASS с тремя секциями.** `X:\parser\_corpus\report.json:6759-6787`; `X:\parser\outout\КР401_2.json:26-45` содержит только 1.1, 1.3, 1.4, а внутри текста 1.4 лежат headings 1.5, 1.6 и глава 2 далее. Coverage 99,83% это не обнаруживает.
3. **КР396_4 — PASS при `needs_ocr=1`.** `X:\parser\_corpus\report.json:6604-6632`; `X:\parser\outout\КР396_4.json:104,118,361` содержит `АтзЮгбат`, разрушенные gene labels и `Мапбагб`.

### 3.4. Корпусный residual screen

По текущему `ocr_pins.json` выполнен точный token scan всех 722 JSON с той же нормализацией core-token, что использует `ocr_pins.anchor_hit`: trim `_PUNCT`, lowercase, длина ≥3, exact key match.

Для PASS:

- 11 442 438 whitespace tokens;
- 1 156 exact pin hits, или 10,103 на 100 000 tokens;
- 21 PASS-файл содержит минимум два разных pin keys;
- 1 074 hits находятся в references, но 39 — в sections/tables девяти PASS-файлов.

Подтверждённые примеры в основном контенте:

- КР1_4: `Вагсе1опа`, `СНшс`, `1луег`, `Сапсег`, `ВСЬС` (`X:\parser\outout\КР1_4.json:286`) при mappings к `Barcelona`, `Clinic`, `Liver`, `Cancer`, `BCLC` (`ocr_pins.json:5-14`);
- КР720_2: `Еигореап`, `ОшбеНпез` (`X:\parser\outout\КР720_2.json:132`);
- КР502_2: `Сгоир`, `А1СС`, `КЕС18Т` (`X:\parser\outout\КР502_2.json:66,131`);
- КР957_1: `УЕСР` вместо VEGF (`X:\parser\outout\КР957_1.json:138,145`);
- КР609_2: `погта1` вместо normal (`X:\parser\outout\КР609_2.json:59`).

Отдельная узкая mixed-script эвристика нашла 304 слова в 142 PASS, например `цитомегаловируcа`, `cтадии`, `cтепень`, `диcфункции`, `вxодит` (`КР757_1.json:108`, `КР501_2.json:157`, `КР687_3.json:99`, `КР912_1.json:590`, `КР9_3.json:364`). Это screening signal, не ground truth; broad mixed-script detector слишком шумен для автоматического FAIL.

### 3.5. Обязательные отсутствующие gates

P0 до публикации:

1. JSON Schema fail-closed; stats пересчитываются, а не принимаются из output.
2. PASS только при usable TOC или независимом structural skeleton; Roman TOC обязателен; TOC none/empty → REVIEW.
3. Collapse checks: largest-section share, headings-inside-text, excluded.other ratio, minimum canonical chapter recall.
4. Source span/line-ID union coverage плюс overlap/duplication; не character accounting.
5. `needs_ocr > 0` никогда не corpus-ready; mixed-script/confusable detector по sections, tables, appendices, metadata.
6. Table recall/completeness: ожидаемые captions, continuation pages, rows/cells и nonempty raw evidence.
7. Строгие exit semantics: REVIEW и failed write не равны успеху.
8. Run manifest и publish allowlist; FAIL/REVIEW физически не попадают в training directory.
9. Regression tests: empty JSON, forged stats, one garbage node, Roman TOC, TOC none, coverage 0/NaN/>100, REVIEW exit, failed batch write, mixed ruled/unruled tables.

P1: стратифицированный gold set 30–50 документов/~1 000 страниц; двойная разметка clinically material fields; отдельные метрики sections, tables, numeric/unit/Latin exactness; threshold калибруется на holdout, а не на КР1_4/КР628_2.

## 4. Сверка КР1_4 и КР628_2 с оригиналами

### 4.1. КР1_4

**Формальный status:** PASS, coverage 99,66%, 36 sections, 2 tables; warnings `COVERAGE`, `SOURCE_DEFECT`, `TITLE_MISMATCH` (`X:\parser\_corpus\report.json:3585-3615`). Все 36 узлов основной иерархии 1–6 и «Критерии оценки качества» присутствуют; потери целой нумерованной главы не обнаружено. Однако качество критических сущностей и таблиц недостаточно для training-ready medical text.

Подтверждённые дефекты:

- **Неверная граница excluded:** `$.excluded.toc[0].text` объединяет оглавление, список сокращений и термины/определения с PDF pp3–7 (`X:\parser\outout\КР1_4.json:318`).
- **Метаданные/body расходятся:** metadata сохраняет `D37.6`, но на PDF p9 body превращает код МКБ в `037.6`; та же ошибка повторена во front matter (`X:\parser\outout\КР1_4.json:13,52,312`).
- **TNM/UICC/BCLC разрушены на PDF pp10–11:** `Tx/T0/T1a/T1b/T3/Nx/Mx/M0` → `Тх/ТО/Т1а/Т1Ь/ТЗ/Ых/Мх/МО`; `pT/pN/pM` → `рТ/р14/рМ`; `Gx/G1/G2/G3/G4` → `Ох/0 1/0 2/03/0 4`; `IA/IB/IVA/IVB` → `1А/1В/1УА/1УВ`; BCLC `D` → `Э` (`X:\parser\outout\КР1_4.json:72`).
- **Вирусологические маркеры меняют смысл:** на PDF p14 `M и G (anti-HCV IgG и anti-HCV IgM) … ДНК HBV … РНК-HDV … Hepatitis C virus` превращено в `М и С (anti-HCV (anti-HCV и anti-HCV anti-HCV) … ДНК НВУ … РНК-ВГД … Hepatitis С у 1 ш з` (`X:\parser\outout\КР1_4.json:110`).
- **Числа и названия рекомендаций повреждены:** на PDF p18 `6 мес.` → `б мес.`, а AASLD/LI-RADS/EASL/APASL/NCCN превращены в псевдолатиницу (`X:\parser\outout\КР1_4.json:117`). Около p30 `HBs-положительного` остаётся `НВз-положительного` (`:174`).
- **Заголовки повреждены:** PDF p13 `Физикальное обследование` → `Фнзикалыюе обследование.` (`:101`); p26 `Трансартериальная химиоэмболизация` → `Тпансартериальная хнмиоэмболизация` (`:151`); p29 `радиоэмболизация` → `радиоэмболизацня` (`:158`); p38 `Предреабилитация` → `Предпеабилитация` (`:216`). TITLE_MISMATCH остаётся warning, поэтому status — PASS.
- **Русские слова также остаются повреждёнными:** PDF p9 `стеатогепатитный` → `стетогепатитный`, `макротрабекулярный` → `мактротрабекулярный` (`:65`); p13 `ГЦР развивается на фоне` → `ГЦРразвивается па фоне`, `мультидисциплинарной` → `мулътидисциплинарной` (`:90`). Сверка кодовых точек показала, что эти формы уже есть в native extraction. Прямой сценарий «чистый native → плохой final» не подтверждён; подтверждён другой отказ — merge доверяет грязной нативной кириллице и не заменяет её правильным визуальным/OCR-текстом.
- **PD1/PD-L1/CTLA4 не восстановлены:** `PDI`, `РИ1/РИ-1Л`, `Р01/Р0-ГЛ`, `СТЪА4` присутствуют в clinical/patient text (`X:\parser\outout\КР1_4.json:187,346`). Сам pin `"рб1": "PDI"` институционализирует неверную форму (`X:\parser\crparser\data\ocr_pins.json:24`).
- **Table 1, PDF p11:** все 7 строк найдены, но caption `(UICC)` → `(ШСС)`, `IA/IIIA/IVB` → `1А/ША/1УВ`, `T1b` → `Т1Ъ`, `Любая N` → `ЛюбаяК`; `Barcelona Clinic Liver Cancer (BCLC)` превращено в `Вагсе1опа СНшс 1луег Сапсег (ВСЬС)` (`X:\parser\outout\КР1_4.json:285-286`). Raw text также захватывает следующий prose.
- **Table 2, PDF pp63–64:** сохранены 7 из 13 режимов. Полностью отсутствуют nivolumab+ipilimumab, ramucirumab, tremelimumab+durvalumab, durvalumab, gemcitabine+cisplatin и gemcitabine+oxaliplatin; ссылки `[87]/[91]/[92]` превращены в `[871/[911/[921` (`X:\parser\outout\КР1_4.json:299`). Итого row recall двух именованных таблиц — `14/20 = 70%`.
- **Структурный table recall ещё хуже:** визуально есть не менее восьми логических таблиц/шкал — Table 1, критерии качества, шкалы уровней доказательности/убедительности, Table 2, Child-Pugh, ECOG, Karnofsky. В `tables` представлены две: recall не выше 25%.
- **Excluded не безопасен как training text:** references с PDF p48+ имеют exact-token attestation 43,01%; например `Akinyemiju T. et al. The Burden...` превращено в `Актуетуи Т. е1 а1. ТЬе Вигбеп...`. В definitions `CD152` → `СБ152`, `Modified` → `МосНПей`, `prehabilitation` → `ргеЬаЫШа(юп)` (`X:\parser\outout\КР1_4.json:318,324`).

Содержательные metadata — title, age group, основные MKB codes, developer, approval, `year=2025` — в основном соответствуют обложке. PDF XMP title/author пусты; `url` и точный `publication_date` из PDF не верифицируются.

### 4.2. КР628_2

**Формальный status:** PASS, coverage 99,73%, 22 sections, 5 tables; warnings `COVERAGE`, `EXTRA` (`X:\parser\_corpus\report.json:10506-10531`). Все 22 нумерованных узла 1–7 найдены, но после главы 7 нарушена граница следующего региона.

Подтверждённые дефекты:

- **Критерии качества приписаны разделу 7:** заголовок, пояснение и фрагменты таблиц с PDF pp42–44 находятся в `$.sections[6].text`, заканчиваются дублированным хвостом и одновременно частично повторяются в `$.tables[1]`/`[2]` (`X:\parser\outout\КР628_2.json:174,193-199`). Это одновременно misassignment и duplication; coverage считает его успехом.
- **Неверные excluded boundaries:** `$.excluded.toc[0].text` объединяет TOC, сокращения и определения с PDF pp2–6 (`X:\parser\outout\КР628_2.json:250`); административные письма/заключения pp72–75 приписаны appendices.
- **Коды и числа повреждены:** PDF p10 `H40.06` → `Н40.0б` (`X:\parser\outout\КР628_2.json:51`); PDF pp32/34 ATC `S01EC` получает три формы `501ЕС`, `801 ЕС`, `801ЕС` (`:123`); PDF p36 диапазон `II–IV` → `(П-1У)` (`:130`).
- **Названия классификаций повреждены:** около PDF p17 `van Beuningen, C. Spaeth или K. Shaffer` превращено в `хап Beuningen, С. 8рае1И или К. 8Иа//ег`; рядом появляется `периметрия^125...` (`X:\parser\outout\КР628_2.json:101`). В glossary PDF p6 `MD, mean deviation` → `ЛИ), шеап с!еУ1абоп`, `GHT, Glaucoma Hemifield Test` → `СНТ, Glaucoma НетШеМ Тез*` (`:250`).
- **Русские слова в основном тексте повреждены:** PDF p38 `офтальмологом` → `офталъмологом` (`:160`); PDF pp39–40 `и/или` → `ши`/`и/ши`, `консилиумом` → `консшиумом`, `профилю` → `профшю`, `профильную` → `профтьную` (`:167`); заголовок получает апостроф `Организация оказания медицинской'помощи` (`:165`). Эти же формы присутствуют в native extraction: чистый native не был испорчен merge, но грязный native не был заменён правильным OCR/визуальным вариантом.
- **Table 1, PDF pp24–30:** `0/9` лекарственных строк представлены как структурированные rows; JSON сохраняет только header, 194 символа (`X:\parser\outout\КР628_2.json:181-183`). Часть данных сплющена в section 3.1 с интерливированными колонками.
- **Table 2, PDF pp32–33:** `0/9`, table object отсутствует.
- **Table 3.1, pp42–43:** `2/7`; Table 3.2, pp43–44: `5/7` (`X:\parser\outout\КР628_2.json:193-199`).
- **Appendix tables:** П1 сохраняет `6/10`; П2 — `3/3`, но захватывает текст `Порядок обновления клинических рекомендаций` и номер страницы 62 (`X:\parser\outout\КР628_2.json:201-207`). В сумме table records сохраняют `16/45 = 35,6%` видимых строк. Caption recall `5/6 = 83,3%` скрывает этот провал.
- **References:** exact-token attestation с PDF p44+ — 49,73%.

Содержательные metadata в основном корректны. Обложка визуально содержит обрезанное `Год утверждения: 20`, но документы pp72–73 датированы 2024 годом, поэтому `year=2024` не противоречит PDF; `end_year=null` честнее автоматического дополнения.

### 4.3. Численная оценка двух документов

| Критерий | КР1_4 | КР628_2 | Вывод |
|---|---:|---:|---|
| Exact-token attestation, sections | 9 385/10 133 = 92,62% | 6 646/7 188 = 92,46% | Token multiset не проверяет порядок и способен завышать качество |
| Character alignment, sections | 93,56% | 87,17% | У КР628 структурная перестановка/duplication сильнее бьёт по порядку |
| Exact-token attestation, whole JSON | 15 455/19 777 = 78,15% | 14 121/18 483 = 76,40% | JSON целиком непригоден |
| Character alignment, whole JSON | 80,73% | 77,59% | Excluded/tables сильно загрязнены |
| Формальный validator | PASS | PASS | PASS даёт ложную уверенность |
| Table row recall | 14/20 = 70% именованных строк; ≤25% логических таблиц/шкал | 16/45 = 35,6% | Блокер |
| Clinically material errors | D37.6, TNM/UICC/BCLC, HBV/HCV/HDV, режимы терапии | H40.06, S01EC, II–IV, структура критериев | Блокер |
| Чистый native → плохой final | Не обнаружено | Не обнаружено | Причинность не приписывается merge |
| Грязный native оставлен вместо visual/OCR | Подтверждено | Подтверждено | Блокер выбранной политики авторитета |

Честная оценка sections — **около 92–94% строгой визуальной согласованности для КР1_4 и 87–92,5% для КР628_2** по двум независимым proxy. Whole JSON — около 78–81% и 76–78% соответственно. Эти интервалы не заменяют ground truth: точные ошибки попадают в коды, классификации, лекарства, числа и строки таблиц, поэтому средний процент не делает документы пригодными для medical training.

## 5. Оценка принятых known issues

### Не следовало принимать

1. **~16 обвалов глав без hard quarantine.** Не менять общий threshold разумно; публиковать заведомо collapsed JSON вместе с PASS-кандидатами — нет. Причины неоднородны, их надо разделить и физически исключить из release.
2. **Кириллическое удвоение/склейку в основном medical text.** Для чисел, единиц, диагнозов, препаратов и рекомендаций это не косметика. Нельзя агрессивно автокорректировать; надо детектировать, маркировать span confidence и отправлять файл в REVIEW.
3. **Неочищенную иностранную bibliography при передаче JSON «как есть».** Это допустимо только если schema contract запрещает использовать `excluded.references` для обучения и release exporter физически его не включает. Сейчас такого enforceable контракта нет.
4. **Итеративную отладку на двух документах как доказательство качества.** Два файла полезны для unit/regression fixtures, но не оценивают 722 PDF и несколько root-cause классов.

### Принято верно

- PyMuPDF как native baseline;
- отдельная обработка сканов;
- доменный clinical profile для metadata и канонической иерархии;
- идея сохранять excluded/raw evidence;
- selective OCR вместо full OCR каждого born-digital PDF;
- решение не менять глобально `_find_content_start` без regression set.

Эти решения верны по направлению, но текущие destructive subtraction, потеря provenance, global pins и fail-open release gate обесценивают их.

## 6. Обязательный итоговый вердикт

### 6.1. Верно ли направление в целом?

**Частично да для extraction stack; нет для production corpus pipeline.** Оставить PyMuPDF, доменный профиль и selective OCR. Сменить курс с «наращивания эвристик и пинов до высокого PASS» на provenance-first routed extraction + независимый gold-based gate. Без этой смены дальнейшие локальные fixes будут повышать формальный PASS быстрее, чем реальное доверие.

### 6.2. Топ-3 самых серьёзных проблемы

1. **Fail-open PASS и stale publication дают ложную уверенность.** Доказательства: REVIEW имеет `ok=True`/exit 0 (`validate.py:170-182,552-564`); batch не делает hash/set gate (`batch_report.py:62-103,143-156`); 616 PASS включают КР848_1, КР401_2, КР396_4.
2. **Destructive structure/table pipeline теряет и неверно размещает контент, а coverage показывает около 100%.** Доказательства: `stats.py:28-49`, `tables.py:273-292,344-352`, живые КР1_4/КР628_2 и collapsed PASS.
3. **Stateful OCR/pins/cache оставляют медицинские ошибки, закрепляют неверные замены и разрушают воспроизводимость.** Доказательства: basename cache без input/tool hash (`ocr.py:811-879`), global pins (`ocr_pins.py:48-130`), неверный pin `PDI`, 21 exact-pin-positive PASS и подтверждённый приоритет грязной нативной кириллицы над правильным visual/OCR в обоих target JSON.

### 6.3. Топ-3 что сделать иначе

1. **Fail-closed release pipeline:** schema, strict status/exit, run manifest/hashes, staging, physical quarantine, independent structural gates. Цена: 5–8 engineer-days; самый высокий ROI.
2. **Immutable provenance IR и non-destructive reconciliation:** line/span IDs, page/bbox/source/confidence, coverage по source union, tables вычитаются только после успешного reconcile. Цена: 10–15 engineer-days плюс schema migration.
3. **Routed challenger extraction:** Docling standard/hybrid + PaddleOCR или Surya на gold benchmark; fontTools/glyph pilot для CID; VLM только exception pages. POC — 1 неделя; production adapter/tuning — ещё 2–4 недели; медицинская разметка 1–2 reviewer-weeks.

### 6.4. Готовность корпуса

- **85,3%** — доля формальных PASS, не качество.
- **21,7%** — максимальный пул PASS без warnings, но не аттестованный и уже содержащий ложный PASS.
- **0%** — доля корпуса, которую на текущих доказательствах можно ответственно выпустить «как есть» для обучения медицинской LLM.

Условия ненулевой release readiness:

1. заморозить exact run и hashes;
2. внедрить P0 gates и quarantine;
3. разметить стратифицированный gold set;
4. установить и выполнить thresholds: section recall/precision, structured coverage/overlap, numeric/unit/Latin exactness, table cell recall/precision, zero clinically material corruption in release sample;
5. повторно прогнать весь корпус с immutable pins/cache;
6. вручную проверить стратифицированную выборку PASS и все boundary cases.

Только после этого процент готовности следует считать как долю файлов, прошедших один замороженный reproducible run и независимый gate. Текущий `616 PASS` для этой цели использовать нельзя.
