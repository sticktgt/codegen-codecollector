# codecollector

`codecollector` — оркестратор управляемого dry-run процесса для точечных изменений кода по пользовательскому запросу. Проект строит техническое представление целевого проекта, помогает выбрать точку изменения, собирает контекст, вызывает внешний `codegenerator`, применяет результат во временный workspace, запускает проверки и сохраняет артефакты запуска для анализа.

Текущая реализация ориентирована на Python-проекты, локальные или совместимые с Ollama LLM-сервисы, CLI-контракт и ручное решение о переносе изменений из staging workspace в основной проект.

---

## Назначение

`codecollector` не генерирует код самостоятельно. Его задача — подготовить условия для безопасной генерации и проверки изменения:

- проиндексировать проект;
- найти релевантные символы;
- оценить качество пользовательского запроса;
- определить или принять от пользователя операцию изменения;
- выбрать target для замены или anchor для вставки нового символа;
- собрать `context pack` вокруг выбранной точки;
- подобрать reference-артефакты;
- сформировать структурированный запрос для внешнего `codegenerator`;
- применить результат в staging workspace;
- выполнить структурные и runtime-проверки;
- сформировать diff, impact summary, dry-run merge plan и итоговый run report.

`codecollector` отвечает за структурный отбор контекста. Низкоуровневое сжатие prompt, budget strategy, вызов LLM и разбор ответа модели выполняет `codegenerator`.

---

## Основной dry-run сценарий

Рабочий сценарий состоит из последовательных этапов:

1. обновление индекса проекта;
2. обновление search documents и vector index, если проект изменился;
3. анализ change request;
4. определение операции изменения;
5. поиск и ранжирование кандидатов;
6. выбор target или anchor;
7. сбор context pack;
8. подбор reference-артефактов;
9. формирование `GenerationRequest`;
10. вызов `codegenerator generate`;
11. применение production-артефакта в staging workspace;
12. проверка результата применения;
13. вызов `codegenerator generate-test`;
14. применение сгенерированного теста, если он получен и прошел статические проверки;
15. запуск verification;
16. выполнение `repair`, если ошибка считается исправимой;
17. формирование dry-run merge plan;
18. сохранение run artifacts.

Пайплайн всегда применяет изменения во временный workspace. Основной проект не изменяется автоматически.

---

## Операции изменения

Операция задает способ применения generated artifact к выбранной точке кода.

Поддерживаются две операции:

- `replace_symbol`;
- `insert_after_symbol`.

### `replace_symbol`

`replace_symbol` используется для замены существующего символа. Выбранный target является изменяемым символом.

Типичный сценарий: изменить реализацию функции, метода или класса, сохранив внешний контракт.

### `insert_after_symbol`

`insert_after_symbol` используется для вставки нового символа после существующего anchor. Выбранный target является не объектом изменения, а точкой вставки.

Типичные сценарии:

- добавить новую функцию после существующей функции в том же модуле;
- добавить новый класс после существующего класса;
- добавить новый метод внутрь существующего класса, если target указывает на класс.

Для `insert_after_symbol` важно показывать пользователю, что выбранный symbol — это anchor.

---

## Analyze и LLM-assisted выбор

Команда `sessions analyze` создает session, оценивает запрос, определяет operation, собирает recall-набор кандидатов и возвращает результат для UI или CLI-пользователя.

Операцию можно передать явно через `--operation`. Если operation не передана, analyze пытается определить ее автоматически.

```bash
python -m codecollector sessions analyze \
  --project-id <project_id> \
  --title "Изменить текст уведомления о назначении тикета" \
  --description "Сделать уведомление на русском языке и использовать формулировку успешно назначен сотруднику." \
  --constraint "Не менять внешний контракт API"
```

```bash
python -m codecollector sessions analyze \
  --project-id <project_id> \
  --title "Добавить Dataclass модели адреса" \
  --description "Добавить Dataclass модели адреса с минимальным количеством полей" \
  --operation insert_after_symbol
```

Если `analysis.llm_assist.enabled=true`, analyze использует два LLM-assisted этапа.

Первый этап, `search_plan`, получает пользовательский запрос и compact project map. Он определяет качество запроса, вероятную operation, ожидаемые новые symbols и поисковый план.

Второй этап, `candidate_rerank`, получает candidate cards. В карточки входят excerpt-код, описания, связи, соседние symbols и related tests. Этот этап выбирает target для `replace_symbol` или anchor для `insert_after_symbol`.

Если запрос признан недостаточным и search plan не содержит полезных поисковых подсказок, дорогой `candidate_rerank` может быть пропущен. В этом случае результат содержит warning и `analysis_usage.calls=1`.

---

## Качество запроса

Analyze возвращает блок `request_quality`.

```json
{
  "request_quality": {
    "status": "processable",
    "reason": "Запрос достаточно конкретен.",
    "missing_information": []
  }
}
```

Возможные значения `request_quality.status`:

| Статус | Значение |
|---|---|
| `processable` | Запрос достаточно конкретен для выбора target или anchor. |
| `uncertain` | В запросе есть неопределенность, но система может попытаться выбрать target или anchor. |
| `insufficient` | Запрос слишком общий для генерации. Нужно переписать запрос и заново выполнить analyze. |

Если статус равен `insufficient`, UI должен показать `reason` и `missing_information`, заблокировать генерацию и предложить пользователю переписать исходный запрос.

Ручной выбор target через `select-target` не используется как способ исправить недостаточный запрос. Правильный сценарий для `insufficient` — изменить title, description или constraints и заново вызвать `sessions analyze`.

---

## Источник operation

Итоговая operation возвращается в поле `requested_operation`.

```json
{
  "requested_operation": "replace_symbol",
  "operation_source": "llm_rerank",
  "operation_confidence": 0.95,
  "operation_reason": "Запрос требует заменить существующую реализацию."
}
```

Возможные значения `operation_source`:

| Значение | Значение |
|---|---|
| `user` | Operation явно передана пользователем. |
| `llm_search_plan` | Operation определена на этапе search planning. |
| `llm_rerank` | Operation уточнена после rerank кандидатов. |
| `fallback` | Уверенно определить operation не удалось; использовано техническое значение по умолчанию. |

Если `operation_source=fallback`, operation не считается надежно выбранной.

---

## Рекомендация target или anchor

Analyze возвращает блок `target_recommendation`.

```json
{
  "target_recommendation": {
    "recommended_operation": "replace_symbol",
    "operation_confidence": 0.95,
    "operation_reason": "...",
    "recommended_candidate_id": "c2",
    "recommended_target": "support_app.services.notification_service.build_assignment_message",
    "target_role": "target",
    "target_confidence": 0.98,
    "target_reason": "...",
    "manual_review_required": false,
    "warnings": [],
    "ranked_candidates": []
  }
}
```

Основные поля:

| Поле | Значение |
|---|---|
| `recommended_operation` | Operation, рекомендованная analyze. |
| `recommended_target` | Рекомендуемый qualname. |
| `target_role` | Роль выбранного qualname. |
| `target_confidence` | Уверенность выбора target или anchor. |
| `target_reason` | Причина выбора. |
| `manual_review_required` | Признак необходимости ручного решения. |
| `warnings` | Предупреждения analyze. |
| `ranked_candidates` | Ранжирование кандидатов, которое вернула LLM. |

Возможные значения `target_role`:

| Значение | Значение |
|---|---|
| `target` | Символ является объектом замены. |
| `anchor` | Символ является точкой вставки нового кода. |
| `unknown` | Target или anchor не удалось определить. |

---

## Кандидаты analyze

Analyze возвращает список кандидатов для выбора пользователем.

```json
{
  "qualname": "support_app.services.notification_service.build_assignment_message",
  "name": "build_assignment_message",
  "kind": "function",
  "file_path": "support_app/services/notification_service.py",
  "score": 35.75,
  "confidence": 1.0,
  "relevance_category": "высокая",
  "reasons": ["..."],
  "docstring": "Формирует короткое уведомление после назначения тикета агенту.",
  "knowledge_title": "Текст уведомления о назначении",
  "requirements": ["REQ-DEMO-001"],
  "ranked_by_llm": true,
  "llm_recommended": true,
  "llm_rank": 1,
  "llm_reason": "Прямая реализация текста уведомления"
}
```

Основные поля кандидата:

| Поле | Значение |
|---|---|
| `qualname` | Полное имя символа. |
| `name` | Короткое имя. |
| `kind` | Тип символа: `module`, `class`, `function`, `method`. |
| `file_path` | Файл символа. |
| `score` | Поисковый score. |
| `confidence` | Нормализованная уверенность. |
| `relevance_category` | Категория релевантности: `высокая`, `средняя`, `низкая`. |
| `reasons` | Причины попадания в список. |
| `docstring` | Docstring из кода. |
| `knowledge_title` | Заголовок из `knowledge.yaml`, если есть. |
| `requirements` | Связанные требования. |
| `ranked_by_llm` | Кандидат участвовал в LLM rerank. |
| `llm_recommended` | Кандидат выбран LLM как основной. |
| `llm_rank` | Ранг кандидата по LLM. |
| `llm_reason` | Причина ранга по LLM. |

Список кандидатов, возвращаемый через API для выбора пользователем, ограничивается настройкой `analysis.result.max_candidates`. Внутренний recall-набор может быть шире и ограничивается `analysis.recall.max_recall_candidates`. В LLM rerank передается до `analysis.candidate_context.max_candidate_cards` расширенных карточек.

Через API обычно возвращается LLM-оцененный top-N список. Если LLM rerank пропущен или не дал результата, кандидаты могут иметь `ranked_by_llm=false`.

---

## Статусы analyze

Итоговый статус analyze находится в `result_summary.status`.

```json
{
  "result_summary": {
    "status": "analyzed",
    "manual_review_required": false,
    "request_quality_status": "processable"
  }
}
```

Основные значения:

| Статус | Значение |
|---|---|
| `analyzed` | Analyze завершился с рабочей рекомендацией. |
| `needs_user_decision` | Требуется решение пользователя. |

`needs_user_decision` требует дополнительной трактовки по соседним полям.

Если `request_quality_status=insufficient`, нужно переписать запрос и заново выполнить analyze.

Если `request_quality_status=processable` или `uncertain`, а `manual_review_required=true`, пользователь может выбрать target или anchor вручную через `sessions select-target`.

---

## LLM usage, prompt budget и тайминги

Analyze возвращает суммарные метрики LLM-вызовов в `analysis_usage`.

```json
{
  "analysis_usage": {
    "calls": 2,
    "prompt_tokens": 5004,
    "output_tokens": 837,
    "total_tokens": 5841,
    "prompt_chars": 19482,
    "duration_sec": 28.56,
    "steps": {
      "search_plan": {
        "prompt_tokens": 1430,
        "output_tokens": 352,
        "total_tokens": 1782,
        "prompt_chars": 5438,
        "duration_sec": 8.19,
        "total_duration_sec": 7.90
      },
      "candidate_rerank": {
        "prompt_tokens": 3574,
        "output_tokens": 485,
        "total_tokens": 4059,
        "prompt_chars": 14044,
        "duration_sec": 9.19,
        "total_duration_sec": 8.95
      }
    }
  }
}
```

Отдельные LLM-блоки также содержат `llm_usage` и `prompt_budget`.

```json
{
  "prompt_budget": {
    "max_prompt_chars": 15000,
    "prompt_chars": 14044,
    "trim_steps": [
      "candidate_cards:7->5",
      "candidate_source_chars->120"
    ]
  }
}
```

UI и диагностические инструменты должны показывать:

- количество LLM-вызовов;
- prompt tokens;
- output tokens;
- total tokens;
- prompt chars;
- duration по analyze целиком;
- duration по каждому LLM-этапу;
- max prompt chars;
- фактический prompt size;
- trim steps.

Настройки prompt budget для analyze задаются в `config.yaml`:

- `analysis.llm_assist.max_prompt_chars`;
- `analysis.llm_assist.search_plan_max_prompt_chars`;
- `analysis.llm_assist.rerank_max_prompt_chars`.

Специализированный лимит этапа имеет приоритет над общим `max_prompt_chars`.

---

## Select target

Команда `sessions select-target` фиксирует ручной выбор target или anchor.

```bash
python -m codecollector sessions select-target \
  --session-id <session_id> \
  --selected-qualname <qualname> \
  --operation insert_after_symbol
```

Параметр `--operation` позволяет явно зафиксировать операцию при ручном выборе. Это полезно, если analyze вернул несколько вариантов или UI предоставляет пользователю выбор операции.

`select-target` не переписывает смысл исходного запроса. Если analyze вернул `request_quality.status=insufficient`, пользователь должен изменить запрос и заново выполнить analyze.

---

## Generate и блокировка генерации

Команда `sessions generate` запускает генерацию по выбранной session.

```bash
python -m codecollector sessions generate --session-id <session_id>
```

Если session не готова к генерации из-за недостаточного запроса, команда возвращает JSON с признаком блокировки, а не запускает pipeline.

```json
{
  "pipeline_result": null,
  "result_summary": {
    "status": "needs_user_decision",
    "generation_blocked": true,
    "block_reason": "insufficient_request",
    "message": "Request is insufficient for generation. Rewrite the request and run analyze again.",
    "request_quality_status": "insufficient",
    "missing_information": [
      "конкретное улучшаемое поведение",
      "желаемый результат",
      "где именно применить изменения"
    ],
    "recommended_action": "rewrite_request_and_run_analyze_again",
    "selected_target": "support_app.services.notification_service.build_assignment_message",
    "requested_operation": "insert_after_symbol"
  }
}
```

Возможные поля блокировки:

| Поле | Значение |
|---|---|
| `generation_blocked` | Генерация не запущена. |
| `block_reason` | Причина блокировки. Сейчас используется `insufficient_request`. |
| `message` | Человекочитаемое описание причины. |
| `request_quality_status` | Статус качества запроса. |
| `missing_information` | Чего не хватает в запросе. |
| `recommended_action` | Рекомендуемое действие. Сейчас используется `rewrite_request_and_run_analyze_again`. |

Для UI это нормальный бизнес-ответ. Кнопка генерации должна быть заблокирована, пока пользователь не перепишет запрос и не выполнит analyze заново.

---

## Сессии, запуски и workspace

### Session

`session` — логическая единица работы по одному change request или набору связанных требований.

Session хранит:

- входные требования;
- `requested_operation`;
- текущий `status`;
- `recommended_target`;
- `selected_target`;
- анализ запроса;
- историю run ids;
- историю workspace ids;
- последний run id;
- последний workspace id.

Основные статусы session:

| Статус | Значение |
|---|---|
| `analyzed` | Analyze завершен, есть результат анализа. |
| `needs_user_decision` | Нужно действие пользователя: уточнить запрос или выбрать target. |
| `target_selected` | Пользователь выбрал target или anchor. |
| `generated` | Генерация выполнена. |
| `ready_for_merge_review` | Проверки прошли, workspace готов к ручному review. |
| `verification_failed` | Verification не пройден. |
| `generated_test_verification_failed` | Production-код корректен, но сгенерированный тест не прошел verification. |
| `repair_verification_failed` | Repair выполнен, но verification не пройден. |
| `repair_no_effective_change` | Repair не дал полезного изменения. |
| `repaired` | Repair выполнен успешно. |
| `finalized` | Работа по session завершена. |

### Run

`run` — один запуск pipeline внутри session. Run сохраняет шаги pipeline, результаты внешних вызовов, примененный artifact, verification report, merge plan и ссылки на workspace.

### Workspace

`workspace` — staging-копия проекта для конкретного run. Внутри run могут создаваться промежуточные workspace, но наружу возвращается финальный workspace, по которому строятся diff, impact summary и dry-run merge plan.

---

## Verification и итоговые статусы

После применения generated artifact запускаются проверки.

Сейчас используются:

- `runtime_ast_parse` — синтаксический разбор Python-файлов;
- `runtime_py_compile` — запуск `python -m compileall .`;
- `runtime_pytest_recommended` — запуск рекомендованных тестов;
- статические проверки production artifact;
- статические проверки generated test;
- проверка релевантности generated test изменению.

Итог verification отделяется от причины ошибки.

Если production-код или основные проверки проекта не проходят, используется статус `verification_failed`.

Если production-код проходит, но проблема подтверждена только в generated test, используется статус `generated_test_verification_failed`. В этом случае основной production-код не описывается как некорректный.

Если внешний `generate-test` завершился ошибкой и не вернул тестовый артефакт, это фиксируется в `generated_test_apply` и `warnings`. Такой случай не маскируется под обычное отсутствие сгенерированных тестов.

Если ошибка считается исправимой, pipeline может вызвать `repair`. Для repair сохраняется исходная operation, включая `insert_after_symbol`, чтобы исправление не превращало вставку нового символа в замену anchor.

Различие между ошибкой production-кода и ошибкой generated test отражается в:

- `result_summary.status`;
- `session.status`;
- итоговом payload run;
- `merge_plan.summary_lines`;
- `apply_result.impact.notes`.

---

## Контекст, reference artifacts и budget

`context pack` содержит структурированный контекст выбранного target или anchor:

- target symbol со source-кодом;
- `module_outline`;
- соседние symbols;
- inbound и outbound relations;
- связанные требования;
- `recommended_tests`;
- `related_tests`;
- reference summary;
- reference artifacts.

`reference_library/` содержит дополнительные материалы, которые используются как вспомогательный контекст: шаблоны, паттерны и snippets.

`codecollector` структурно ограничивает контекст:

- не передает полный файл, когда достаточно target symbol;
- ограничивает число related tests;
- ограничивает число reference artifacts;
- передает ограниченный reference-контекст в `generate-test`, если это разрешено настройкой;
- включает полный файл для `generate-test`, когда это нужно для импортов и окружения теста.

В логах и request payload сохраняются метрики контекста:

- `request_chars`;
- `target_source_chars`;
- `related_test_chars`;
- `reference_chars`;
- `estimated_context_chars`;
- `full_file_included`.

---

## Контракт с внешним codegenerator

`codecollector` вызывает внешний `codegenerator` через файловый JSON request и получает JSON result.

Используются три режима:

- `generate` — генерация production-кода;
- `generate-test` — генерация тестового файла;
- `repair` — исправление ранее сгенерированного артефакта.

### GenerationRequest

Используется для `generate` и `generate-test`.

Основные поля:

- `request_id`;
- `mode`;
- `change_request`;
- `target`;
- `project_context`;
- `reference_context`;
- `generated_code_artifact`;
- `options`;
- `context_metrics`.

`change_request` содержит `title`, `description`, `constraints` и `notes`.

`target` содержит `qualname`, `file_path` и `operation`.

`project_context` содержит `module_outline`, `full_file_source`, `target_symbol`, `related_tests` и `recommended_tests`.

`reference_context` содержит `reference_summary` и `reference_artifacts`.

`generated_code_artifact` используется, когда следующий шаг строится по уже сгенерированному production-коду. Для `insert_after_symbol` сгенерированный symbol является объектом тестирования, а выбранный target остается anchor.

### RepairRequest

Используется для `repair`.

Основные поля:

- `request_id`;
- `mode`;
- `previous_generation_request_id`;
- `change_request`;
- `error_context`;
- `previous_artifact`;
- `project_context`;
- `reference_context`;
- `options`.

В `previous_artifact.operation` сохраняется исходная operation.

### GenerationResult

Основные поля результата:

- `request_id`;
- `status`;
- `code_artifact`;
- `test_artifact`;
- `planner_result`;
- `warnings`;
- `trace_path`;
- `llm_usage`;
- `error_type`;
- `message`.

---

## Run artifacts

Каждый запуск сохраняется в `.runs/`.

В run artifacts входят:

- request и result внешних вызовов;
- stderr внешнего генератора;
- итоговый `pipeline_run_*.json`;
- метаданные шагов pipeline;
- usage и timing внешних LLM-вызовов;
- diff и verification report;
- generated test diagnostics;
- merge plan.

Run artifacts используются для отладки, сравнения запусков и анализа качества генерации.

---

## CLI

### Анализ без явной operation

```bash
python -m codecollector sessions analyze \
  --project-id <project_id> \
  --title "Изменить текст уведомления о назначении тикета" \
  --description "Сделать уведомление на русском языке и использовать формулировку успешно назначен сотруднику." \
  --constraint "Не менять внешний контракт API"
```

### Анализ с явной operation

```bash
python -m codecollector sessions analyze \
  --project-id <project_id> \
  --title "Добавить Dataclass модели адреса" \
  --description "Добавить Dataclass модели адреса с минимальным количеством полей" \
  --constraint "Добавляем только Dataclass описания модели" \
  --operation insert_after_symbol
```

### Ручной выбор target или anchor

```bash
python -m codecollector sessions select-target \
  --session-id <session_id> \
  --selected-qualname support_app.domain.models.AgentSummary \
  --operation insert_after_symbol
```

### Генерация по session

```bash
python -m codecollector sessions generate \
  --session-id <session_id>
```

### Низкоуровневый pipeline generate

```bash
python -m codecollector pipeline generate \
  --project demo_projects/sample_python_app \
  --title "Изменить текст уведомления о назначении тикета" \
  --description "Сделать уведомление полностью русскоязычным и пригодным для UI." \
  --constraint "Не менять внешний контракт API" \
  --constraint "Не менять сигнатуру функции" \
  --selected-qualname support_app.services.notification_service.build_assignment_message \
  --operation replace_symbol
```

### Доступные группы команд

В текущем CLI доступны команды для:

- управления проектами: `projects register`, `projects onboard`, `projects list`, `projects get`, `projects delete`;
- работы с session: `sessions analyze`, `sessions select-target`, `sessions generate`, `sessions repair`, `sessions finalize`, `sessions get`, `sessions list`, `sessions delete`;
- работы с workspace: `workspaces get`, `workspaces diff`, `workspaces apply`;
- запуска pipeline: `pipeline generate`, `pipeline replay`;
- низкоуровневой диагностики: `search search`, `context context`, `index build`, `apply apply`.

---

## Конфигурация

Основная конфигурация хранится в `config.yaml`.

В конфигурации находятся:

- параметры индексации;
- параметры semantic search;
- настройки graph storage;
- настройки vector storage;
- настройки внешнего вызова `codegenerator`;
- настройки verification;
- настройки trace и логирования;
- правила структурного отбора контекста;
- настройки LLM-assisted analyze.

Параметры analyze находятся в `analysis.llm_assist` и связанных секциях. Подключение к Ollama-compatible endpoint задается локально в конфигурации `codecollector`; настройки LLM не наследуются из `codegenerator`.

Числовые лимиты, prompt budget и threshold должны задаваться через конфигурацию. Prompt-тексты для analyze хранятся в `codecollector/prompts`.

---

## Структура проекта

Основные директории и файлы:

| Путь | Назначение |
|---|---|
| `config.yaml` | Основная конфигурация. |
| `.runs/` | Артефакты запусков pipeline. |
| `.workspaces/` | Временные staging workspace. |
| `api/` | CLI и пользовательские точки входа. |
| `analysis/` | LLM-assisted analyze, search planning и rerank candidates. |
| `prompts/` | Prompt-тексты для analyze. |
| `orchestration/` | Основная orchestration-логика pipeline. |
| `indexing/` | Построение и обновление индекса проекта. |
| `search/` | Поиск по проекту и shortlist кандидатов. |
| `context/` | Сбор context pack. |
| `external_codegen/` | Подготовка request и вызовы внешнего `codegenerator`. |
| `patching/` | Применение артефактов к staging workspace. |
| `workspace/` | Управление workspace, diff и impact summary. |
| `reference_library/` | Reference-артефакты. |
| `vector_search/` | Embedding и semantic search. |
| `verification/` | Проверки после применения изменений. |
| `sessions/` | Логика session-based flow. |
| `codecollector_ui/` | Streamlit-приложение для просмотра запусков. |
| `demo_projects/sample_python_app/` | Демонстрационный Python-проект. |

---

## Демонстрационный проект

Демонстрационный проект находится в `demo_projects/sample_python_app` и содержит приложение `support_app`.

Основные части demo-проекта:

- `support_app/api/controllers.py` — API-слой и точки входа use-case;
- `support_app/services/ticket_service.py` — бизнес-логика по тикетам;
- `support_app/services/notification_service.py` — формирование уведомлений;
- `support_app/services/report_service.py` — отчетные функции и сводки;
- `support_app/storage/ticket_repository.py` — in-memory репозиторий;
- `support_app/domain/models.py` — модели предметной области;
- `tests/` — тесты, используемые для проверки и как связанный контекст.

Рядом с demo-кодом находится `.codecollector/knowledge.yaml`. Он содержит описания модулей, символов, требований и архитектурных слоев, которые используются при поиске и выборе target.

Дополнительные демонстрационные материалы:

- `demo_change_requests/` — примеры structured change request;
- `demo_artifacts/` — вспомогательные артефакты;
- `tests/golden/` — проверочные сценарии.

---

## Текущие ограничения

Актуальные ограничения проекта:

- основная поддержка — Python;
- основной runtime-сценарий — локальные или совместимые с Ollama модели;
- storage/vector stack ориентирован на PostgreSQL и pgvector;
- внешний контракт проекта — CLI и JSON-файлы, не HTTP API;
- merge выполняется как dry-run;
- автоматический перенос изменений в основной проект не выполняется;
- часть сценариев `generate` может требовать `repair`;
- качество `generate-test` зависит от выбранной модели и доступного контекста;
- `generate-test` может привести к статусу `generated_test_verification_failed`, даже если production-код корректен;
- onboarding, повторный onboarding после apply и полноценный lifecycle проекта остаются отдельными незавершенными областями;
- поддержка других языков требует отдельных адаптеров.

---

## Актуальные направления доработки

Для `codecollector` актуальны следующие задачи:

- оформить HTTP API поверх текущего CLI-контракта;
- завершить lifecycle для проектов, session, run и workspace;
- оформить apply в основной проект как отдельную операцию;
- добавить повторный onboarding после успешного apply;
- улучшить onboarding нового проекта;
- улучшить автоматическую генерацию `knowledge.yaml`;
- расширить поддержку языков через адаптеры;
- отделить language-agnostic orchestration core от Python-специфики;
- повышать стабильность `generate` и `generate-test`.

Для связки с `codegenerator` актуальны следующие задачи:

- сохранять текущий JSON-контракт `GenerationRequest`, `RepairRequest` и `GenerationResult`;
- при переходе с CLI на локальный сервис сохранять семантику текущего контракта;
- улучшать качество генерации тестов;
- дорабатывать работу с дополнительными библиотеками и reference-контекстом.

---

## Итоговое состояние

`codecollector` в текущем состоянии — это orchestration-слой для controlled dry-run изменения кода. Он обеспечивает анализ запроса, выбор target или anchor, структурный отбор контекста, вызов внешнего `codegenerator`, применение результата в staging workspace, verification и подготовку результата к ручному review.

Основная ценность проекта — сделать LLM-assisted изменение кода управляемым, воспроизводимым и пригодным для диагностики.
