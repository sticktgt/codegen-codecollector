# codecollector

`codecollector` — оркестратор пайплайна подготовки и применения точечных изменений кода по change request.

Проект ориентирован на локальные LLM и ограниченный размер контекста. Его задача — не «генерировать код внутри себя», а:

- проиндексировать проект;
- найти или подтвердить target для изменения;
- собрать структурированный контекст;
- вызвать внешний `codegenerator`;
- применить результат в staging workspace;
- запустить проверки;
- сохранить артефакты запуска для анализа и отладки.

Текущая реализация ориентирована прежде всего на Python-проекты и dry-run сценарий с ручным решением о merge.

---

## Назначение проекта

`codecollector` нужен как внешний orchestration-слой вокруг LLM-генерации.

Он решает задачи, которые неудобно или опасно перекладывать на модель напрямую:

- подготовка технического представления проекта;
- поиск основного места изменения;
- выбор связанного контекста;
- подбор reference-артефактов;
- формирование структурированного request для внешнего генератора;
- применение результата не в master, а в отдельный staging workspace;
- запуск автоматических проверок;
- формирование отчета по шагам пайплайна.

`codecollector` **не занимается низкоуровневым prompt trimming по символам**. Его зона ответственности — структурный отбор контекста. Основное budget-ограничение prompt и runtime trimming выполняет `codegenerator`.

---

## Текущая общая схема работы

Типовой сценарий `pipeline generate` выглядит так:

1. обновление индекса проекта;
2. обновление search documents и vector index, если код изменился;
3. поиск shortlist кандидатов по change request;
4. выбор или подтверждение target-символа;
5. сбор context pack;
6. подбор reference-артефактов;
7. формирование `GenerationRequest`;
8. внешний вызов `codegenerator generate`;
9. применение полученного артефакта в staging workspace;
10. при необходимости внешний вызов `codegenerator repair` и повторное применение;
11. отдельный внешний вызов `codegenerator generate-test`;
12. применение сгенерированного теста в staging workspace;
13. verification (`ast_parse`, `py_compile`, `pytest_recommended`);
14. формирование dry-run merge plan и итогового run-report.

На практике пайплайн может идти по одной из веток:

- **без repair** — если `generate` сразу выдал валидный код;
- **с repair** — если сгенерированный артефакт не применился или не прошел проверки;
- **без generated test apply** — если тест не был сгенерирован или был пропущен;
- **с generated test apply** — если тестовый артефакт успешно получен и применяется вместе с кодом.

---

## Разделение ответственности между `codecollector` и `codegenerator`

Текущее разграничение такое.

### `codecollector` отвечает за

- индекс проекта и техническое представление кода;
- semantic search по проекту;
- использование `knowledge.yaml`;
- выбор target-символа;
- сбор `context pack`;
- структурный отбор контекста:
  - передавать ли `full_file_source`;
  - сколько related tests передавать;
  - сколько reference artifacts передавать;
  - передавать ли reference для `generate-test`;
- подготовку `GenerationRequest` и `RepairRequest`;
- вызов CLI внешнего генератора;
- применение результата и проверки;
- сохранение run-артефактов.

### `codegenerator` отвечает за

- prompt assembly;
- budget strategy;
- runtime trimming;
- вызов LLM;
- разбор ответа модели;
- нормализацию `code_artifact` / `test_artifact`;
- trace и метрики вызова модели.

Это текущее разделение является целевым и должно сохраняться при дальнейших изменениях.

---

## Основные функциональные блоки

### Индекс проекта

Индекс проекта хранит техническое представление кода.

Сейчас в индекс попадают:

- модули;
- классы;
- функции и методы;
- диапазоны строк;
- docstring;
- входящие и исходящие связи;
- search documents для semantic search.

Индекс используется для:

- поиска подходящего target по change request;
- построения графа связей;
- формирования `context pack`;
- impact analysis после применения изменения;
- выбора связанных тестов и соседнего контекста.

### `knowledge.yaml`

`knowledge.yaml` — человекочитаемый слой описания проекта рядом с кодом.

Он может содержать:

- названия и описания модулей;
- описания важных символов;
- keywords;
- связи символов с требованиями;
- архитектурные слои;
- формулировки, удобные для semantic search.

В текущем подходе `knowledge.yaml` рассматривается как полуавтоматический артефакт:

- он может быть сгенерирован автоматически;
- затем может быть уточнен аналитиком вручную.

### Semantic search и search documents

Для поиска target-символов по change request используются search documents — короткие текстовые представления символов.

Обычно в них входят:

- имя символа;
- docstring;
- описание из `knowledge.yaml`;
- связанные требования;
- часть контекста по модулю.

На выходе search дает shortlist кандидатов с объяснением причин выбора.

### Context pack

`context pack` — структурированный пакет контекста для выбранного target.

Обычно включает:

- target-символ со source-кодом;
- module outline;
- соседний модульный контекст;
- inbound/outbound relations;
- связанные требования;
- recommended tests;
- related tests.

### Reference library

`reference_library/` содержит дополнительные reference-артефакты, которые можно добавлять в context.

Сейчас они используются как вспомогательный слой для генерации, например:

- шаблоны функций;
- примеры паттернов;
- reference snippets.

### Вызов внешнего `codegenerator`

`codecollector` вызывает внешний генератор через файловый request и получает машинно-читаемый JSON-результат.

Используются три режима:

- `generate` — генерация production-кода;
- `generate-test` — генерация тестового файла;
- `repair` — исправление ранее сгенерированного артефакта.

### Staging workspace

Изменения применяются не в исходный проект, а во временный workspace.

Это позволяет:

- безопасно проверить артефакт;
- собрать diff;
- выполнить verification;
- оценить impact;
- подготовить dry-run merge plan.

### Verification

После применения артефакта запускаются проверки проекта.

Сейчас используются:

- `ast_parse` — проверка синтаксической корректности Python-файлов;
- `py_compile` — запуск `python -m compileall .`;
- `pytest_recommended` — запуск рекомендованных тестов.

Если был сгенерирован новый тест, он попадает в список рекомендуемых тестов.

### Run artifacts

Каждый запуск сохраняется в `.runs/`.

Там хранятся:

- request и result внешних вызовов;
- stderr внешнего генератора;
- итоговый `pipeline_run_*.json`;
- дополнительные метаданные шага.

Это основной материал для отладки, сравнения запусков и анализа качества пайплайна.

---

## Контекст и budget

`codecollector` не должен заниматься низкоуровневым символьным trimming.

В текущей реализации он уменьшает контекст **структурно**:

- отказом от передачи полного файла там, где достаточно target-symbol;
- выбором числа related tests;
- выбором числа reference-артефактов;
- исключением reference-контекста из `generate-test`;
- включением полного файла для `generate-test`, когда это полезно для import-контекста.

Основное ужатие prompt выполняется внутри `codegenerator`.

В `request_payload` и логах `codecollector` при этом сохраняются метрики размера контекста, например:

- `request_chars`;
- `target_source_chars`;
- `related_test_chars`;
- `reference_chars`;
- `estimated_context_chars`;
- `full_file_included`.

---

## Контракт вызова внешнего `codegenerator`

### `GenerationRequest`

Используется для режимов `generate` и `generate-test`.

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

#### `change_request`

Поля:

- `title`;
- `description`;
- `constraints`;
- `notes`.

#### `target`

Поля:

- `qualname`;
- `file_path`;
- `operation`.

#### `project_context`

Поля:

- `module_outline`;
- `full_file_source`;
- `target_symbol`;
- `related_tests`;
- `recommended_tests`.

#### `reference_context`

Поля:

- `reference_summary`;
- `reference_artifacts`.

#### `generated_code_artifact`

Используется в связанных сценариях, прежде всего в `generate-test`, когда тест строится уже по сгенерированному production-коду.

### `RepairRequest`

Используется для режима `repair`.

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

### `GenerationResult`

В ответ внешний генератор возвращает JSON со следующими основными полями:

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

## CLI

### Основной pipeline generate

```bash
python -m codecollector pipeline generate \
  --project demo_projects/sample_python_app \
  --title "Изменить текст уведомления о назначении тикета" \
  --description "Сделать уведомление на русском языке, но использовать формулировку «успешно назначен сотруднику»." \
  --constraint "Не менять внешний контракт API" \
  --constraint "Изменить только текст уведомления" \
  --selected-qualname support_app.services.notification_service.build_assignment_message
```

### Пример более подробного change request

```bash
python -m codecollector pipeline generate \
  --project demo_projects/sample_python_app \
  --title "Изменить текст уведомления о назначении тикета на русский язык" \
  --description "Сделать уведомление полностью русскоязычным и пригодным для UI." \
  --constraint "Не менять внешний контракт API" \
  --constraint "Не менять сигнатуру функции" \
  --constraint "Изменить только текст уведомления" \
  --constraint "Не использовать англоязычные слова" \
  --selected-qualname support_app.services.notification_service.build_assignment_message
```

### Отдельные режимы

В зависимости от текущего CLI проекта могут быть доступны и более низкоуровневые команды для поиска, индексации, контекста и применения.

README фиксирует только актуальный основной dry-run сценарий, на который сейчас опирается связка `codecollector` + `codegenerator`.

---

## Конфигурация

Основная конфигурация хранится в `config.yaml`.

Сейчас в ней находятся, среди прочего:

- параметры индексации;
- параметры semantic search;
- настройки graph/vector storage;
- правила структурного отбора контекста;
- настройки внешнего вызова `codegenerator`;
- настройки verification;
- настройки trace и логирования.

Принцип текущей реализации:

- проектные и runtime-настройки должны жить в конфиге;
- в коде не должно оставаться критичных hardcoded runtime-значений, завязанных на конкретную модель.

---

## Структура проекта

Ниже перечислены основные части `codecollector`.

### `config.yaml`
Основная конфигурация проекта.

### `.runs/`
Артефакты выполненных запусков пайплайна.

### `.workspaces/`
Временные staging workspace для dry-run применения изменений.

### `api/`
CLI и пользовательские точки входа.

### `orchestration/`
Основной orchestration-слой пайплайна.

### `indexing/`
Построение и обновление индекса проекта.

### `search/`
Поиск по проекту и shortlist кандидатов.

### `context/`
Сбор context pack для выбранного target.

### `external_codegen/`
Подготовка request и вызовы внешнего `codegenerator`.

### `patching/`
Применение артефактов к staging workspace.

### `workspace/`
Управление staging workspace, diff и impact summary.

### `reference_library/`
Reference-артефакты для дополнительного контекста.

### `vector_search/`
Embedding и semantic search слой.

### `verification/`
Проверки проекта после применения изменений.

### `codecollector_ui/`
Streamlit-приложение для просмотра логов запусков.

### `demo_projects/sample_python_app/`
Демонстрационный Python-проект `support_app`.

---

## Демонстрационный проект

В качестве demo-проекта используется `demo_projects/sample_python_app`.

Это небольшой Python-проект `support_app`, который нужен для проверки полного dry-run pipeline.

Структура demo-проекта включает:

- `support_app/api/controllers.py` — API-слой и точки входа use-case-ов;
- `support_app/services/ticket_service.py` — бизнес-логика по тикетам;
- `support_app/services/notification_service.py` — формирование уведомлений;
- `support_app/services/report_service.py` — отчетные функции и сводки;
- `support_app/storage/ticket_repository.py` — простой in-memory репозиторий;
- `support_app/domain/models.py` — модели предметной области;
- `tests/` — тесты, которые используются и как проверка, и как связанный контекст.

Рядом с demo-кодом лежит `.codecollector/knowledge.yaml`.

Он содержит описания модулей, символов, требований и архитектурных слоев, которые используются при поиске и выборе target.

Дополнительные демонстрационные материалы:

- `demo_change_requests/` — примеры structured change request;
- `demo_artifacts/` — вспомогательные артефакты для демонстраций;
- `tests/golden/` — наборы проверочных сценариев.

---

## Что считается текущим рабочим сценарием

Текущий рабочий сценарий — это dry-run pipeline для точечного изменения Python-кода:

- найти target;
- собрать контекст;
- сгенерировать артефакт через внешний `codegenerator`;
- применить его в staging workspace;
- при необходимости выполнить `repair`;
- сгенерировать тест;
- прогнать verification;
- подготовить dry-run merge plan.

Итоговое решение о merge по-прежнему принимает человек.

---

## Текущие ограничения

На текущем этапе проект имеет следующие ограничения:

- основная поддержка — Python;
- основная схема — локальные модели через Ollama;
- текущий storage/vector stack ориентирован на PostgreSQL/pgvector;
- merge выполняется как dry-run, без автоматического переноса в master;
- часть сценариев `generate` все еще может требовать `repair`;
- качество `generate-test` зависит от выбранной модели и доступного контекста.

---

## Ближайшие направления развития

Актуальные направления дальнейшего развития:

- onboarding нового проекта в работу;
- автоматическая первичная генерация `knowledge.yaml`;
- дальнейшее развитие language-agnostic orchestration core;
- поддержка новых языков через адаптеры;
- развитие правил структурного отбора контекста;
- улучшение стабильности `generate` и `generate-test`;
- постепенный переход от CLI-вызова внешнего генератора к локальному сервису/API с сохранением текущего JSON-контракта.
