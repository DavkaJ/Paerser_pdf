# Предсказания в Label Studio силами Claude Code (Opus 4.8, ultracode) — СЕЙЧАС

Цель: Opus проходит по задачам очереди как разметчик (контекст документа + картинка кропа) и
пишет пред-аннотации (`predictions`) в Label Studio. Человек потом ПОДТВЕРЖДАЕТ/правит. В текст
корпуса ничего не применяется — предсказания живут в слое ревью, поэтому это безопасно.

## Что делать
1. Загрузить `_corpus/verify_queue/tasks_min.json` (3237 задач; критические первыми).
2. Для КАЖДОЙ задачи собрать вход:
   - `source_text`;
   - окно контекста ±~200 симв. вокруг спана из `outout_latin/<doc>.json` (найти вхождение);
   - картинку кропа `crop_word`/`crop_line`/`crop_page` (файлы в `_corpus/verify_queue/crops_min/`)
     — ЧИТАТЬ изображением (у Claude есть зрение). У ~33 задач кропа нет (multiline) → только текст.
3. **Ultracode:** разложить задачи по под-агентам пачками (напр. по 60-100), каждый агент —
   свой батч; собрать результаты. Дедуп по (source_text, entity_kind) можно, чтобы одинаковые
   формы не гонять повторно.
4. Opus выдаёт на задачу: `decision` ∈ {accept, pick_candidate, enter_own, source_ok, illegible,
   need_expert}, `corrected_text` (правильная форма, если fix), `entity_kind` (icd/atc/tnm/gene/
   drug/dose/term/not_entity), `confidence`, короткая причина.
   - URL/ссылки → decision=source_ok, entity_kind=not_entity (не чинить по кусочкам).
   - Несуществующая форма (напр. «Herpes C simplex virus») НЕ предлагать — правильное «Herpes
     simplex virus» (убрать лишнюю букву).
   - Валидное русское (иПТГ, микроРНК) → source_ok (не латинизировать).

## Формат LS-предсказаний (точно под текущий labeling_config.xml)
Контролы конфига: decision (Choices, toName=crop), corrected_text (TextArea, toName=crop),
entity_kind_fix (Choices, toName=crop). В каждую задачу добавить блок:
```json
"predictions": [{
  "model_version": "opus48-context",
  "score": 0.9,
  "result": [
    {"from_name":"decision","to_name":"crop","type":"choices","value":{"choices":["enter_own"]}},
    {"from_name":"corrected_text","to_name":"crop","type":"textarea","value":{"text":["Vol.54"]}},
    {"from_name":"entity_kind_fix","to_name":"crop","type":"choices","value":{"choices":["not_entity"]}}
  ]
}]
```
(Если decision=accept/source_ok — corrected_text можно опустить.) Картинки — как в
LAUNCH_LABEL_STUDIO.md: поля crop_* переписать в `/data/local-files/?d=...` (прямые слеши),
env `LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED=true` + DOCUMENT_ROOT на verify_queue.

## Импорт
- Если проект ещё пустой (разметки почти нет) — пересоздать проект и импортировать
  `tasks_min_pred.json` (с блоками predictions). Предсказания появятся пред-заполненными.
- Если человек уже разметил заметную часть — НЕ пересоздавать; добавить предсказания к
  существующим задачам через LS API (`POST /api/predictions`, по task id), чтобы не потерять
  ручную работу.

## Дисциплина
- Предсказания — ПОДСКАЗКА, не истина. Человек подтверждает. КРИТИЧЕСКИЕ (icd/atc/tnm/доза/ген)
  человек сверяет по кропу сам, даже при высокой уверенности LLM.
- В текст корпуса НИЧЕГО не применять на этом шаге. Применение решений человека — отдельный
  экспортный шаг позже.
- Провенанс: model_version в predictions; какие задачи размечены Opus — фиксировать.
- Замерить/доложить: сколько задач с предсказанием, распределение по decision, средняя уверенность.
