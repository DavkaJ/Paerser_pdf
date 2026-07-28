# Запуск Label Studio на очереди верификации — задача для Claude Code

Цель: поднять Label Studio локально на текущей очереди `_corpus/verify_queue/tasks_min.json`
(3237 задач, 73 критических, критические первыми), чтобы человек начал разметку СЕЙЧАС.
Арбитр mixcase-латиницы (Cowork-ревью) — отдельно, УТРОМ (модели ночью не работают). Не
блокирует эту разметку.

## Ловушка, которую надо обойти (иначе картинки не покажутся)
Поля `crop_word/crop_line/crop_page` в задачах — относительные Windows-пути с бэкслешами
(`crops_min\__10_5_p36_0_word.png`). Label Studio их так не отдаст. Нужно: (1) включить сервинг
локальных файлов, (2) переписать пути в URL вида `/data/local-files/?d=crops_min/...` с прямыми
слешами. Кропы лежат в `_corpus/verify_queue/crops_min/` (9612 PNG).

## Шаги
1. **Установить:** `pip install label-studio` (в том же Python-окружении, где будешь запускать).

2. **Переписать пути под LS-сервинг** — сгенерировать `tasks_min_ls.json`:
   ```python
   import json
   base=r"X:\parser\_corpus\verify_queue"
   import os
   t=json.load(open(os.path.join(base,"tasks_min.json"),encoding="utf-8"))
   for x in t:
       for f in ("crop_word","crop_line","crop_page"):
           v=x.get(f)
           if v: x[f]="/data/local-files/?d="+v.replace("\\","/")
   json.dump(t,open(os.path.join(base,"tasks_min_ls.json"),"w",encoding="utf-8"),ensure_ascii=False)
   print("ok",len(t))
   ```

3. **Включить сервинг локальных файлов** (переменные окружения ДО запуска). Windows:
   ```
   set LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED=true
   set LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT=X:\parser\_corpus\verify_queue
   ```

4. **Запустить сервер:** `label-studio start` (откроется http://localhost:8080). При первом
   запуске создать аккаунт (локальный, любой email/пароль).

5. **Создать проект и импортировать** (через UI надёжнее, чем старый CLI `--init`):
   - Create Project → имя `latin_verify`.
   - Labeling Setup → вкладка Custom → вставить содержимое
     `_corpus/verify_queue/labeling_config.xml` целиком → Save.
   - Import → файл `_corpus/verify_queue/tasks_min_ls.json` → Import.
   - (Если версия LS поддерживает CLI-импорт — можно `label-studio start latin_verify --init
     --input-path _corpus/verify_queue/tasks_min_ls.json --label-config
     _corpus/verify_queue/labeling_config.xml --input-format json-task`, но UI-путь надёжнее.)

6. **Проверить, что картинки грузятся:** открыть первую задачу — должны быть видны три кропа
   (слово/строка/страница). Если картинки битые — проверить, что env-переменные выставлены В ТОМ
   ЖЕ процессе, что запускает сервер, и что DOCUMENT_ROOT указывает на `verify_queue`.

7. **Доложить человеку:** URL (http://localhost:8080), проект `latin_verify`, всего 3237 задач,
   73 критических идут первыми. Разметка: кнопки accept / pick_candidate / enter_own /
   source_ok / illegible / need_expert.

## Важно (координация с утренней работой)
- **Не пересоздавать и не --init заново** проект утром — иначе ночная разметка человека
  потеряется. Утренний арбитр (mixcase-латиница) должен ДОБАВЛЯТЬ задачи (импорт нового батча в
  ТОТ ЖЕ проект или отдельный проект), а не перегенерировать очередь под ним.
- Если утром планируется re-regen корпуса — сначала ЭКСПОРТ аннотаций из LS (JSON), потом
  регенерация. Разметка человека — ценные данные, не терять.
- gene-triage (1186) и noise (1083) — отдельные файлы, в эту разметку НЕ импортировать.
