# Инвентаризация скрытой связанности engine ↔ КР (промпт 11, ШАГ 0)

Проверено по `crparser/engine/*.py`. **Ключевой факт: классификация заголовков УЖЕ в
профиле** (`base.py`: `classify_heading`, `excluded_regions`, `is_title_continuation`,
`can_attach_title`, `heading_breaks_before`, `main_title_canonical`, `title_is_body_label`,
`split_inline_headings`, `heading_order_valid`). Движок зовёт эти хуки. Остаточная
связанность — КОНЦЕНТРИРОВАННАЯ: доменные ЯКОРЯ регионов, пара порогов и два магических
контракта. Ниже — каждое место, куда должно уехать, и оценка риска байт-в-байт.

| # | Файл:строка | Что | Куда (политика) | Риск | Действие |
|---|---|---|---|---|---|
| 1 | `parser.py:107` | `getattr(profile,"registry",None)` — магический доступ | контракт `DocumentProfile.registry` (может быть None) | низкий | объявить `registry` в base.py, убрать getattr |
| 2 | `parser.py:113` | `metadata.pop("_warnings")` — ключ-призрак | `MetadataResult(metadata, warnings)` | средний (трогает clinical.extract_metadata) | явный dataclass-контракт |
| 3 | `toc.py:122-124` | `_BODY_MARKERS` (список сокращ/термины/1.краткая) — якоря тела КР | `RegionPolicy.body_start_markers` | **высокий** | toc.py — общий модуль, зовётся и валидатором; см. решение ниже |
| 4 | `ocr.py:170-175` | `_ABBR_ANCHOR/_TERM_ANCHOR/_REFS_ANCHOR` — якоря регионов для словаря сокращений | `OcrPolicy.region_anchors` | **высокий** | ocr.py — 1000 строк медицинской починки; anchors вплетены в `_build_doc_map` |
| 5 | `tables.py:230` | `_RE_TABLE_CAPTION` («Таблица N») | `TablePolicy.caption_pattern` | средний | вынести паттерн в политику |
| 6 | `tables.py:1017/1026` | `_RE_SECTION_HEAD/_RE_SUBSEC_HEAD` (дотированный номер + кириллица) | `NumberingPolicy.subsection_head` | средний | вынести паттерн |
| 7 | `segmenter.py:45` | `_MAX_SUB_TITLE=200` — порог длины подзаголовка (КР-калибровка) | `NumberingPolicy.max_subtitle_len` | низкий | перенести константу |
| 8 | `segmenter.py:714 _roman_chapters_safe` + `_roman_ok` | правило «римские главы + локальные арабские подпункты → отключить римские» | `NumberingPolicy` (механизм) | средний | политика решает; сегментер применяет |
| 9 | `textnorm.py:31-164`, `ocr.py:162` | `_CYR/_UPCYR/_RE_MIXED/_RE_AFFIX` — классы кириллицы | — (уровень ЯЗЫКА, не документа) | — | **оставить**: clinical-Russian engine всегда обрабатывает кириллицу; вынос = ложная нейтральность |
| 10 | `segmenter.py:heading_order_valid` | НЕ вызывается (счётчик=0) | `NumberingPolicy` (неактивный) | — | перенести как неиспользуемый; **подключение — 11b** |

## Осознанные решения по scope (Р1 регламента: часть связанности дороже в разборе, чем в жизни)

- **#9 (кириллица в textnorm/ocr) — ОСТАВИТЬ.** Это уровень естественного языка, не типа
  документа. «Нейтральный» движок, не умеющий кириллицу, — фикция: домен проекта —
  русские медицинские КР. Выносить `_CYR`/`_RE_MIXED` в политику = обещать
  латиница-переносимость, которой в textnorm нет. Записано как сознательный останов.

- **#3/#4 (toc.py `_BODY_MARKERS`, ocr.py anchors) — ВЫНЕСТИ ЧЕРЕЗ ПОЛИТИКУ, НО ОСТОРОЖНО.**
  `toc.py.parse_entries`/`ocr._build_doc_map` вызываются вне сегментера (валидатор,
  reader). Политику НЕ протаскивать сквозь все сигнатуры: дать модулям ПАРАМЕТР-умолчание
  (текущие паттерны как default), а профиль передаёт свои. Так engine перестаёт ХАРДКОДИТЬ
  якоря (они приходят параметром), но общие модули остаются вызываемыми без профиля.

## Итог: 5 политик (ШАГ 1)

- `NumberingPolicy` — max_subtitle_len (#7), subsection_head (#6), roman-правило (#8),
  heading_order_valid (#10, неактивный). Дефолт: арабские номера, латиница.
- `RegionPolicy` — excluded_regions (уже есть в ExcludedSpec) + body_start_markers (#3).
- `ContentBoundaryPolicy` — дом для `_find_content_start` (промпт 12 будет чинить ЕГО).
- `OcrPolicy` — region_anchors (#4) + конфиг (DPI/langs/когда включать/пины).
- `TablePolicy` — caption_pattern (#5) + пороги reconcile (09).

Дефолтные реализации в base.py доменно-нейтральны; КР-специфика — в clinical.py.
Плюс контракты #1 (registry) и #2 (MetadataResult). Плюс `profiles/minimal.py` +
англоязычный тест (ШАГ 3) как доказательство расширяемости.

## РЕЗУЛЬТАТ 11 (прагматичный субсет — решение заказчика)

**Ключевая рамка:** рефакторинг разрывает КР-**ТИП-ДОКУМЕНТА**-связанность, но НЕ
русско-**ЯЗЫКОВУЮ**. Движок честно РУССКО-ЯЗЫЧНЫЙ (классы кириллицы в textnorm/ocr,
соглашения «Таблица»/«Список литературы») — это не КР-специфика (русская монография
тоже так), а язык корпуса. Выносить язык = фиктивная нейтральность (#9).

**СДЕЛАНО (byte-identical, проверено):**
- base.py: 5 политик (NumberingPolicy/RegionPolicy/ContentBoundaryPolicy/OcrPolicy/
  TablePolicy) с нейтральными дефолтами; `registry` в контракте; `MetadataResult`.
- clinical.py: `numbering` (max_subtitle_len=200, uses_roman_chapters), `regions`
  (excluded_spec); `metadata_result` строит MetadataResult БЕЗ ключа-призрака.
- Роутинг: `_MAX_SUB_TITLE`→`numbering.max_subtitle_len` (#7); `excluded_regions`→
  `regions.excluded_spec` (сегментер); римские главы гейтятся `uses_roman_chapters` (#8);
  `heading_order_valid`→NumberingPolicy (#10, НЕ подключён — 11b); registry (#1) +
  MetadataResult (#2) в parser.
- profiles/minimal.py + tests/test_minimal_profile.py: движок парсит англ. «1./2./3.»
  документ нейтральным профилем в дерево разделов — расширяемость ДОКАЗАНА.

**ОСТАВЛЕНО в движке (Р1, русско-языковой слой, не КР-тип):**
- tables.py `_RE_TABLE_CAPTION` («Таблица N») (#5) — язык, +роутинг invasive в 8 мест
  только что закрытого 09.
- ocr.py `_ABBR/_TERM/_REFS_ANCHOR` (#4) — вплетены в 1000-строчный алгоритм починки.
- toc.py `_BODY_MARKERS` (#3) — общий с валидатором модуль.
- `_find_content_start` — остаётся в сегментере; **ContentBoundaryPolicy заведён как
  типизированный дом для промпта 12**, сам перенос метода — не в прагматичном объёме.
- textnorm/ocr классы кириллицы (#9).

**Следствие для extensibility-теста:** minimal-профиль доказывает профиль-агностичность
СТРУКТУРНОГО конвейера (reader→segmenter→sections). Полная НЕ-русская локализация
(таблицы/OCR) — отдельный языковой слой, честно записан здесь.
