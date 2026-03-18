# codecollector

`codecollector` — PoC для работы с master-кодом Python-проекта: найти основной фрагмент под изменение, собрать ограниченный context pack, передать его во внешний шаг генерации кода, применить полученный артефакт в staging и подготовить dry-run план merge в master.

## Scope

- только Python;
- один project root за запуск;
- один основной target на один pipeline run;
- человек подтверждает target из shortlist;
- внешний генератор пока работает в режиме replay по заранее подготовленному артефакту;
- merge в master пока только в режиме dry-run;
- поддерживаются операции `replace_symbol`, `insert_after_symbol`, `add_symbol`.

## Что уже реализовано

### 1. Описания в коде как основной источник смысла
Runtime использует module/class/function docstring как основной источник human-readable смысла.

Это нужно для того, чтобы:
- искать код не только по имени symbol-а, но и по описанию поведения;
- собирать понятный человеку context pack;
- в будущем передавать в LLM более полезный контекст, чем просто вырезка кода.

### 2. Структурный runtime-индекс
Из Python-кода строится индекс с файлами, symbol-ами и графом связей.

Это нужно для того, чтобы:
- быстро строить shortlist кандидатов;
- адресно находить диапазон кода для patch apply;
- делать context pack и impact summary поверх графа, а не поверх полного проекта целиком.

### 3. Единый knowledge-слой рядом с проектом
Рядом с индексируемым проектом хранится `.codecollector/knowledge.yaml`.

Он хранит:
- описания модулей и symbol-ов;
- связи с requirement-ами;
- architecture layers;
- human-readable knowledge, которое можно читать и править вручную.

### 4. Полный dry-run pipeline
Есть воспроизводимый pipeline:
- build/update index;
- primary target search;
- manual target selection;
- supporting context build;
- external code generation step в режиме replay;
- apply в staging workspace;
- structural validation;
- impact summary;
- merge to master в режиме dry-run.

Это нужно для того, чтобы прогонять весь сценарий изменения без настоящего merge и без обязательного вызова LLM.

## ADR

### ADR-001. SQLite хранит структурные факты runtime
SQLite используется для того, что извлекается автоматически и часто обновляется:
- `files`;
- `symbols`;
- `relations`;
- file hash для incremental indexing.

Почему не YAML:
- неудобно делать partial incremental indexing по измененному файлу;
- неудобно быстро получать inbound/outbound связи;
- неудобно делать адресные выборки по graph relations;
- неудобно безопасно обновлять структурный индекс после `apply`.

Пример неудобной YAML-only задачи:
> показать все symbols, связанные с `REQ-DEMO-001`, которые затрагиваются изменением `build_assignment_message`, имеют inbound callers и рекомендуемые тесты к прогону.

В SQLite это делается адресными выборками по `symbols/relations`, а в YAML-only варианте потребует полный обход и ручную сборку графа в памяти.

### ADR-002. `knowledge.yaml` хранит human-readable knowledge
YAML используется для того, что должен читать и редактировать человек:
- `project`;
- `modules`;
- `symbols`;
- `requirements`;
- `architecture`.

Почему не только SQLite:
- knowledge хуже читается в review;
- сложнее поддерживать его как проектный артефакт рядом с кодом;
- человеку неудобно вносить изменения в смысловые описания через SQL-таблицы.

### ADR-003. Логическая модель — графовая
Логическая модель системы — это не таблицы предметных данных, а узлы и связи:
- узлы: file / module / class / function / method / test / requirement;
- связи: `contains`, `calls`, `covered_by_test`, `belongs_to_layer`, `implements_requirement`, `exposed_by_controller`.

SQLite здесь только физически хранит граф.

Это важно, потому что вопросы системы уже графовые:
- кто вызывает этот symbol;
- какие тесты стоит прогнать после изменения;
- какие requirement-ы реализуются этим symbol-ом;
- что затронуто через 1–2 шага входящего графа.

### ADR-004. Одна актуальная схема
В проекте поддерживается одна текущая схема:
- один knowledge layer — `knowledge.yaml`;
 - knowledge-слой хранится в одном `knowledge.yaml`;
- README описывает только текущее состояние runtime.

## Почему одновременно SQLite и YAML

Они выполняют разные роли.

### SQLite
Хранит машинные структурные факты runtime:
- symbols;
- graph relations;
- hashes файлов;
- быстрые выборки для search/context/apply.

### YAML
Хранит human-readable knowledge:
- title/description;
- keywords;
- requirements;
- architecture layers.

### Итог
- **SQLite** — runtime index;
- **YAML** — project knowledge.

## Текущее устройство индекса

### Таблица `files`
Хранит:
- `project_root`
- `file_path`
- `file_hash`
- `module_name`

Используется для incremental indexing.

### Таблица `symbols`
Хранит:
- `module`, `class`, `function`, `method`;
- `qualname`, `kind`, `file_path`, `start_line`, `end_line`, `docstring`, `source_code`.

Используется для:
- shortlist;
- context pack без служебного поля `annotations`: knowledge и требования выводятся отдельными явными секциями;
- patch apply по конкретному symbol.

### Таблица `relations`
Хранит графовые связи:
- `source_qualname`
- `relation_kind`
- `target_ref`
- `target_qualname`
- `relation_source`

`target_ref` — текстовая цель связи, если нет полного resolution.

`target_qualname` — точная цель, если удалось разрешить.

### Какие связи уже есть
- `imports`
- `contains`
- `calls`
- `covered_by_test`
- `belongs_to_layer`
- `implements_requirement`
- `exposed_by_controller`

## Что хранится в `knowledge.yaml`

`knowledge.yaml` — единый knowledge-слой рядом с проектом.

Пример:

```yaml
version: 1

project:
  title: Support App Demo
  description: Демонстрационный Python-проект для поиска, контекста и dry-run изменений.

modules:
  support_app.services.ticket_service:
    title: Сервис работы с тикетами
    description: Создает, назначает и закрывает тикеты.
    layer: services

symbols:
  support_app.services.ticket_service.TicketService.assign_ticket:
    title: Назначение тикета агенту
    description: Назначает тикет агенту, сохраняет изменения и формирует уведомление.
    keywords:
      - назначение тикета
      - уведомление о назначении
    requirements:
      - REQ-DEMO-001

requirements:
  REQ-DEMO-001:
    title: Назначение тикета агенту
    description: Пользователь должен иметь возможность назначить тикет агенту и получить уведомление.
    linked_symbols:
      - support_app.services.ticket_service.TicketService.assign_ticket
      - support_app.services.notification_service.build_assignment_message
```

## Как обновляется индекс

### `index build`
Команда:
1. находит все `.py` файлы;
2. считает hash каждого файла;
3. сравнивает его с тем, что уже лежит в таблице `files`;
4. переиндексирует только измененные файлы;
5. удаляет из индекса файлы, которых больше нет.

То есть индекс **не перестраивается полностью каждый раз**.

Если в отчете `build_report` видно `initial_build: true`, это означает, что для проекта еще не было сохраненного runtime-индекса и первый прогон построил его с нуля. На следующем запуске без изменений ожидаются `indexed_files: 0` и ненулевой `unchanged_files`.

### После `apply`
После применения артефакта в staging:
1. изменяется один файл;
2. проходит structural validation;
3. выполняется targeted reindex только измененного файла.

## Как сейчас работает поиск

Поиск пока эвристический, без обязательного LLM и без embeddings.

Поисковый текст сравнивается с:
- `name`;
- `qualname`;
- `file_path`;
- `docstring`;
- `module_title` / `module_description`;
- `symbol_title` / `symbol_description`;
- `keywords`;
- `requirement text`;
- `layer`.

На выходе считаются:
- `score` — внутренний технический балл;
- `confidence` — нормализованная оценка от `0.0` до `1.0`;
- `relevance_category` — `высокая / средняя / низкая`.

## Текущий pipeline

### Вход pipeline
Pipeline принимает **structured change request**.

Минимальный набор полей:
- `title`
- `description`
- `project`

Опционально:
- `constraints`
- `notes`

Пример:

```yaml
change_request:
  title: Изменить формирование текста уведомления о назначении тикета
  description: Сделать текст уведомления русскоязычным и использовать формулировку «теперь назначен на».
  constraints:
    - Не менять внешний контракт API
    - Изменить только текст уведомления
```

### Шаги pipeline
1. Задать проект.
2. Построить или обновить индекс.
3. Передать `change_request`.
4. Выполнить **primary target search**.
5. Подтвердить target вручную.
6. Выполнить **supporting context build**.
7. Подготовить пакет для внешнего генератора.
8. В режиме replay взять заранее подготовленный артефакт ответа генератора.
9. Применить его в staging workspace.
10. Выполнить structural validation.
11. Построить impact summary и список рекомендуемых тестов.
12. Подготовить merge to master в режиме dry-run.
13. Сохранить единый JSON-артефакт запуска с таймингами и промежуточными результатами.

## Run artifacts

Каждый полный pipeline запуск создает отдельную папку:

```text
.runs/
  pipeline-20260318T120101.123456Z-abc123/
    pipeline_run_20260318T120101.123456Z.json
```

Внутри сохраняется один объединенный JSON-бандл со всем запуском:
- `change_request`;
- shortlist;
- выбранный target;
- context pack без служебного поля `annotations`: knowledge и требования выводятся отдельными явными секциями;
- generation replay input/output;
- apply result;
- impact summary;
- recommended tests;
- merge dry-run;
- timings по шагам.

## CLI

### Обновить индекс
```bash
python -m codecollector index build --project demo_projects/sample_python_app
```

### Найти shortlist по change request / запросу
```bash
python -m codecollector search --project demo_projects/sample_python_app --query "Изменить формирование текста уведомления о назначении тикета"
```

### Собрать context pack
```bash
python -m codecollector context --project demo_projects/sample_python_app --qualname support_app.services.ticket_service.TicketService.assign_ticket
```

### Применить готовый артефакт в staging
```bash
python -m codecollector apply \
  --project demo_projects/sample_python_app \
  --qualname support_app.services.notification_service.build_assignment_message \
  --artifact-file demo_artifacts/build_assignment_message_v2.py \
  --operation replace_symbol
```

### Запустить полный dry-run pipeline
```bash
python -m codecollector pipeline replay \
  --project demo_projects/sample_python_app \
  --title "Изменить формирование текста уведомления о назначении тикета" \
  --description "Сделать текст уведомления русскоязычным и использовать формулировку «теперь назначен на»." \
  --constraint "Не менять внешний контракт API" \
  --constraint "Изменить только текст уведомления" \
  --selected-qualname support_app.services.notification_service.build_assignment_message \
  --artifact-file demo_artifacts/build_assignment_message_v2.py \
  --operation replace_symbol
```

Ожидаемый результат:
- создается `.runs/.../pipeline_run_*.json`;
- в output видны timings, generation replay, apply result и merge dry-run;
- `merge_plan` показывает только готовность к **ручному review**, а не автоматический merge.

## UI

Запуск:

```bash
python -m codecollector ui
```

Через UI можно:
- обновить индекс;
- получить shortlist;
- вручную выбрать target;
- собрать context pack;
- отдельно применить артефакт в staging;
- пройти **полный dry-run pipeline** от `change_request` до merge plan;
- увидеть timings, промежуточные результаты и рекомендуемые тесты.

## Структура проекта

```text
codecollector/
├── codecollector/
│   ├── api/                  # CLI
│   ├── context/              # context pack
│   ├── domain/               # dataclass-модели
│   ├── indexing/             # runtime-индекс и SQLite storage
│   ├── orchestration/        # pipeline, run artifacts, service layer
│   ├── overlays/             # knowledge.yaml loader
│   ├── patching/             # apply в staging
│   ├── search/               # shortlist и scoring
│   ├── ui/                   # Streamlit UI
│   ├── validation/           # structural validation
│   └── workspace/            # staging + diff
├── demo_artifacts/           # replay-ответы внешнего генератора
├── demo_change_requests/     # примеры structured change request
├── demo_projects/            # demo Python-проект
├── tests/                    # unit/integration/golden
├── docs/                     # пояснения к модели и ограничениям
├── config.yaml
└── README.md
```

## Основные файлы

- `config.yaml` — текущая конфигурация runtime.
- `codecollector/orchestration/services.py` — единый service layer для CLI и UI.
- `codecollector/orchestration/pipeline_service.py` — полный dry-run pipeline.
- `codecollector/orchestration/run_artifacts.py` — сохранение run-бандлов.
- `codecollector/overlays/service.py` — чтение `knowledge.yaml`.
- `codecollector/indexing/storage_sqlite.py` — SQLite storage.
- `codecollector/search/service.py` — scoring shortlist.
- `codecollector/context/service.py` — context pack и рекомендуемые тесты.
- `codecollector/patching/apply_service.py` — apply в staging.
- `codecollector/ui/streamlit_app.py` — UI демонстрации.

## Demo project

- `demo_projects/sample_python_app/README.md`
- `demo_projects/sample_python_app/.codecollector/knowledge.yaml`
- `demo_change_requests/` — примеры входных change request

## Проверка

```bash
pytest
```

Smoke-check текущей версии:
1. `index build`
2. `search`
3. `context`
4. `pipeline replay`
5. `ui`
