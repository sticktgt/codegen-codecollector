# codecollector

`codecollector` — оркестратор пайплайна подготовки и проверки точечных изменений кода по change request.

Проект ориентирован на локальные LLM и ограниченный размер контекста. Его задача — не генерировать код внутри себя, а подготовить проектный контекст, выбрать или подтвердить target для изменения, вызвать внешний `codegenerator`, применить результат в staging workspace, запустить проверки и сохранить артефакты запуска для анализа и отладки.

Текущая реализация ориентирована прежде всего на Python-проекты и dry-run сценарий с ручным решением о переносе изменений в основной проект.

---

## Назначение проекта

`codecollector` нужен как внешний orchestration-слой вокруг LLM-генерации.

Он решает задачи, которые неудобно или рискованно перекладывать на модель напрямую:

- строит техническое представление проекта;
- ищет основное место изменения;
- собирает связанный контекст;
- подбирает reference-артефакты;
- формирует структурированный request для внешнего генератора;
- применяет результат не в основной проект, а в staging workspace;
- запускает автоматические проверки;
- формирует отчет по шагам пайплайна.

`codecollector` не занимается низкоуровневым trimming prompt по символам. Его зона ответственности — структурный отбор контекста. Основное runtime-ужатие prompt и контроль budget выполняет `codegenerator`.

---

## Что считается текущим рабочим сценарием

Текущий рабочий сценарий — dry-run pipeline для точечного изменения Python-кода:

1. обновить индекс проекта;
2. при необходимости обновить search documents и vector index;
3. найти shortlist кандидатов по change request;
4. выбрать или подтвердить target-символ;
5. собрать context pack;
6. подобрать reference-артефакты;
7. сформировать `GenerationRequest`;
8. вызвать внешний `codegenerator` в режиме `generate`;
9. применить артефакт в staging workspace;
10. при необходимости вызвать `repair` и повторно применить результат;
11. вызвать `generate-test`;
12. применить сгенерированный тест в staging workspace;
13. выполнить verification;
14. сформировать dry-run merge plan и итоговый run report.

На практике пайплайн может проходить по разным веткам:

- без `repair`, если `generate` сразу выдал корректный результат;
- с `repair`, если артефакт не применился или не прошел проверки;
- без применения сгенерированного теста, если тест не был создан или был пропущен;
- с применением сгенерированного теста, если тестовый артефакт успешно получен и включен в workspace.

Итоговое решение о переносе изменений в основной проект по-прежнему принимает человек или внешний управляющий слой.

---

## Requested operation

В analyze/generate flow операция изменения передается явно через `requested_operation`.

Сейчас поддерживаются две операции:

- `replace_symbol`
- `insert_after_symbol`

### Семантика операций

`replace_symbol`:
- выбранный target — это symbol, который должен быть заменен.

`insert_after_symbol`:
- выбранный target — это symbol-anchor;
- новый symbol вставляется после выбранного anchor;
- shortlist для analyze строится с учетом отдельного профиля ранжирования.

Для добавления нового метода в существующий класс используется `insert_after_symbol` с target на symbol класса. 

### Operation-aware analyze

Для `insert_after_symbol` анализ предпочитает:

- `class` и другие declaration-style symbol;
- domain/model модули для запросов на добавление dataclass, model, schema.

Для `replace_symbol` анализ по умолчанию предпочитает symbol, который наиболее прямо соответствует изменяемому поведению.

---

## Разделение ответственности между `codecollector` и `codegenerator`

Текущее разграничение ответственности следующее.

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
  - передавать ли reference-контекст для `generate-test`;
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
- нормализацию `code_artifact` и `test_artifact`;
- trace и метрики вызова модели.

Это разделение является текущей целевой моделью и должно сохраняться при дальнейших изменениях.

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

- поиска target по change request;
- построения графа связей;
- формирования `context pack`;
- impact analysis после применения изменения;
- выбора связанных тестов и соседнего контекста.

### knowledge.yaml

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
- затем может быть уточнен вручную.

### Semantic search и search documents

Для поиска target-символов по change request используются search documents — короткие текстовые представления символов.

Обычно в них входят:

- имя символа;
- docstring;
- описание из `knowledge.yaml`;
- связанные требования;
- часть контекста по модулю.

На выходе search возвращает shortlist кандидатов с объяснением причин выбора.

### Context pack

`context pack` — структурированный пакет контекста для выбранного target.

Обычно включает:

- target-символ со source-кодом;
- `module_outline`;
- соседний модульный контекст;
- inbound/outbound relations;
- связанные требования;
- `recommended_tests`;
- `related_tests`.

### Reference library

`reference_library/` содержит дополнительные reference-артефакты, которые можно добавлять в контекст.

Сейчас они используются как вспомогательный слой для генерации, например:

- шаблоны функций;
- примеры паттернов;
- reference snippets.

### Вызов внешнего codegenerator

`codecollector` вызывает внешний генератор через файловый request и получает машиночитаемый JSON-результат.

Сейчас используются три режима:

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
- дополнительные метаданные шагов.

Это основной материал для отладки, сравнения запусков и анализа качества пайплайна.

---

## Контекст и budget

`codecollector` не должен заниматься низкоуровневым символьным trimming.

В текущей реализации он уменьшает контекст структурно:

- не передает полный файл там, где достаточно target-symbol;
- ограничивает число related tests;
- ограничивает число reference-артефактов;
- исключает reference-контекст из `generate-test`;
- включает полный файл для `generate-test`, когда это полезно для import-контекста.

Основное ужатие prompt выполняется внутри `codegenerator`.

При этом в `request_payload` и логах `codecollector` сохраняются метрики размера контекста, например:

- `request_chars`;
- `target_source_chars`;
- `related_test_chars`;
- `reference_chars`;
- `estimated_context_chars`;
- `full_file_included`.

---

## Контракт вызова внешнего codegenerator

### GenerationRequest

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

#### change_request

Поля:

- `title`;
- `description`;
- `constraints`;
- `notes`.

#### target

Поля:

- `qualname`;
- `file_path`;
- `operation`.

#### project_context

Поля:

- `module_outline`;
- `full_file_source`;
- `target_symbol`;
- `related_tests`;
- `recommended_tests`.

#### reference_context

Поля:

- `reference_summary`;
- `reference_artifacts`.

#### generated_code_artifact

Используется в связанных сценариях, прежде всего в `generate-test`, когда тест строится уже по сгенерированному production-коду.

### RepairRequest

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

### GenerationResult

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

## Session, run и workspace

### Session

`session` — логическая единица работы по одному change request или по небольшому набору связанных требований.

Session хранит:

- входные требования;
- `requested_operation`;
- историю запусков;
- историю финальных workspace;
- текущий выбранный target;
- ссылки на последний run и последний workspace.

### Run

`run` — один запуск пайплайна внутри session.

Один run соответствует одному внешнему шагу изменения, например:

- `generate`;
- `repair`;
- в дальнейшем — отдельным ручным повторным действиям.

Для каждого run снаружи значим только один финальный workspace.

### Workspace

`workspace` — staging-копия проекта для конкретного run.

Внутри run могут создаваться промежуточные workspace, но после завершения run остается только один финальный workspace, который и используется для diff, review и dry-run merge plan.

---

## CLI

### Session-based flow для замены существующего symbol

```bash
python -m codecollector sessions analyze   --project-id <project_id>   --title "Изменить текст уведомления о назначении тикета"   --description "Сделать уведомление на русском языке, но использовать формулировку «успешно назначен сотруднику»."   --constraint "Не менять внешний контракт API"   --operation replace_symbol

python -m codecollector sessions select-target   --session-id <session_id>   --selected-qualname support_app.services.notification_service.build_assignment_message

python -m codecollector sessions generate   --session-id <session_id>
```

### Session-based flow для вставки нового symbol

```bash
python -m codecollector sessions analyze   --project-id <project_id>   --title "Добавить Dataclass модели адреса"   --description "Добавить Dataclass модели адреса с минимальным количеством полей"   --constraint "Добавляем только Dataclass описания модели"   --operation insert_after_symbol

python -m codecollector sessions select-target   --session-id <session_id>   --selected-qualname support_app.domain.models.AgentSummary

python -m codecollector sessions generate   --session-id <session_id>
```

### Низкоуровневый pipeline generate

```bash
python -m codecollector pipeline generate   --project demo_projects/sample_python_app   --title "Изменить текст уведомления о назначении тикета"   --description "Сделать уведомление полностью русскоязычным и пригодным для UI."   --constraint "Не менять внешний контракт API"   --constraint "Не менять сигнатуру функции"   --selected-qualname support_app.services.notification_service.build_assignment_message   --operation replace_symbol
```

### Другие команды

В зависимости от сценария доступны и более низкоуровневые команды:

- `projects register`
- `projects onboard`
- `projects list`
- `projects get`
- `projects delete`
- `sessions analyze`
- `sessions select-target`
- `sessions generate`
- `sessions repair`
- `sessions finalize`
- `sessions get`
- `sessions list`
- `sessions delete`
- `workspaces get`
- `workspaces diff`
- `workspaces apply`
- `pipeline generate`
- `pipeline replay`
- `search search`
- `context context`
- `index build`
- `apply apply`

README фиксирует актуальный dry-run сценарий как для `pipeline generate`, так и для session-based flow с явным `requested_operation`.

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

Базовый принцип текущей реализации:

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

## Текущие ограничения

На текущем этапе проект имеет следующие ограничения:

- основная поддержка — Python;
- основная схема — локальные модели через Ollama;
- текущий storage/vector stack ориентирован на PostgreSQL и pgvector;
- merge выполняется как dry-run, без автоматического переноса изменений в основной проект;
- часть сценариев `generate` все еще может требовать `repair`;
- качество `generate-test` зависит от выбранной модели и доступного контекста;
- текущий основной внешний контракт — CLI, а не HTTP API.

---

## TODO

Ниже перечислены направления следующих версий, которые важно сохранить в плане развития.

### По codecollector

- выделить полноценный HTTP API поверх текущего CLI-контракта;
- оформить завершенный lifecycle для проектов, session, run и workspace;
- сделать apply в основной проект отдельной полностью оформленной операцией;
- добавить повторный onboarding после успешного apply;
- развить onboarding нового проекта как стандартный входной сценарий;
- улучшить автоматическую первичную генерацию `knowledge.yaml`;
- расширить поддержку новых языков через адаптеры;
- развить language-agnostic orchestration core;
- продолжить улучшение стабильности `generate` и `generate-test`.

### По связке с codegenerator

- сохранить текущий JSON-контракт между проектами;
- при переходе с CLI на локальный сервис не менять семантику `GenerationRequest`, `RepairRequest` и `GenerationResult`;
- продолжить работу над качеством генерации тестов на более мощных моделях;
- отдельно доработать сценарии работы с дополнительными библиотеками и reference-контекстом в `codegenerator`.

---

## Итог

`codecollector` в текущем состоянии — это orchestration-слой для controlled dry-run изменения кода с явным выбором target, структурным отбором контекста, внешней генерацией через `codegenerator`, применением в staging workspace и обязательной проверкой результата.

Его ключевая роль — не заменить моделью весь процесс изменения кода, а сделать этот процесс управляемым, воспроизводимым и пригодным для анализа.
