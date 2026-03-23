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

## Текущая архитектура

### 1. Описания в коде — основной источник смысла
Runtime использует module/class/function docstring как главный источник human-readable смысла.

### 2. Knowledge-слой рядом с проектом
Рядом с индексируемым проектом хранится `.codecollector/knowledge.yaml`.
Локальный файл индекса рядом с проектом не используется: runtime graph/index хранится в Postgres.
Он содержит:
- описания модулей и symbol-ов;
- связи с requirement-ами;
- architecture layers;
- human-readable knowledge для поиска и explainability.

### 3. Graph / index backend — Postgres
Структурные факты runtime хранятся в Postgres в собственных таблицах:
- `cc_files`
- `cc_symbols`
- `cc_relations`
- `cc_search_documents`

Postgres используется для:
- incremental indexing по hash файлов;
- адресных выборок по symbol-ам и relations;
- хранения graph relations и search documents metadata.

### 4. Semantic search backend — PGVector
Векторный поиск используется только по **human-readable search documents**:
- docstring из кода;
- `knowledge.yaml` descriptions;
- requirement texts.

Векторный поиск не заменяет graph retrieval, а помогает semantic retrieval по описаниям и требованиям.

### 5. Reference Library
Reference Library — отдельный индексируемый источник reference-кода.
Он не является частью изменяемого проекта и не участвует в primary target search.

Reference Library используется только для generation context:
- ищутся релевантные reference artifacts;
- затем они materialize-ятся в `full_file` или `snippet`;
- после этого попадают в generation context и run bundle.

### 6. Полный dry-run pipeline
Pipeline состоит из шагов:
- build/update index;
- primary target search;
- manual target selection;
- supporting context build;
- external code generation step: либо replay по артефакту, либо реальный вызов `codegenerator`;
- apply в staging workspace;
- structural validation;
- impact summary;
- merge to master в режиме dry-run.

## ADR

### ADR-001. Graph / index хранится в Postgres
Postgres используется для структурных фактов runtime:
- files;
- symbols;
- relations;
- search documents metadata.

Причина:
- нужны быстрые выборки по graph relations;
- нужен incremental update по измененным файлам;
- нужно единое persistent storage для graph и metadata.

### ADR-002. `knowledge.yaml` хранит curated human-readable knowledge
YAML используется для того, что должен читать и редактировать человек:
- project;
- modules;
- symbols;
- requirements;
- architecture.

Причина:
- knowledge должен быть reviewable;
- человеку неудобно редактировать смысловые описания через SQL-таблицы;
- knowledge должен жить рядом с кодом как проектный артефакт.

### ADR-003. Логическая модель — графовая
Логическая модель системы — это узлы и связи:
- узлы: file / module / class / function / method / test / requirement;
- связи: `contains`, `calls`, `covered_by_test`, `belongs_to_layer`, `implements_requirement`, `exposed_by_controller`.

Postgres здесь — физическое хранилище графа, а не предметной бизнес-БД.

### ADR-004. Semantic retrieval работает только по human-readable текстам
Vector search индексирует только:
- docstring;
- descriptions из knowledge;
- тексты требований.

Raw code не индексируется embeddings-ами.

### ADR-005. Project-level validation выполняет текущий PoC
Генератор изменений может вернуть optional test artifact, но project-level validation и execution тестов — ответственность текущего PoC.

### ADR-006. Reference Library индексируется отдельно от project code
Reference artifacts хранятся отдельно от master-кода проекта.

Причина:
- target всегда ищется только в коде проекта;
- reference artifacts нужны как supporting context для генерации;
- генератор получает materialized content, а не ссылки на файлы.

## Почему одновременно Postgres и YAML

Они выполняют разные роли.

### Postgres
Хранит машинные структурные факты runtime:
- symbols;
- graph relations;
- hashes файлов;
- search documents metadata;
- адресные выборки для search/context/apply.

### YAML
Хранит human-readable knowledge:
- title/description;
- keywords;
- requirements;
- architecture layers.

### Итог
- **Postgres** — runtime graph/index storage;
- **YAML** — project knowledge.

## Структура graph/index storage

### `cc_files`
Хранит:
- `project_root`
- `file_path`
- `file_hash`
- `module_name`

Используется для incremental indexing.

### `cc_symbols`
Хранит:
- `module`, `class`, `function`, `method`;
- `qualname`, `kind`, `file_path`, `start_line`, `end_line`, `docstring`, `source_code`.

Используется для:
- shortlist;
- context pack;
- patch apply по конкретному symbol.

### `cc_relations`
Хранит graph relations:
- `source_qualname`
- `relation_kind`
- `target_ref`
- `target_qualname`
- `relation_source`
- `relation_confidence`

`target_ref` — текстовая цель, если нет полного resolution.

`target_qualname` — точная цель, если удалось разрешить.

`relation_confidence` — надежность связи:
- `high`
- `medium`
- `low`

### `cc_search_documents`
Хранит тексты и metadata для semantic retrieval:
- `doc_id`
- `qualname`
- `file_path`
- `source_kind`
- `title`
- `text`

## Конфигурация

Основные параметры в `config.yaml`:

```yaml
storage:
  backend: postgres

postgres:
  graph_connection: postgresql://postgres:postgres@localhost:5432/vector_db
  vector_connection: postgresql+psycopg://postgres:postgres@localhost:5432/vector_db
  schema: public
  collection_prefix: codecollector

embedding:
  provider: ollama
  timeout_sec: 420
  ollama:
    base_url: http://192.168.50.165:18081
    model: nomic-embed-text-v2-moe
    timeout_sec: 420

search:
  vector_enabled: true
  vector_backend: pgvector
  vector_weight: 4.0
```

## Запуск pgvector в Docker

Для сохранения данных между перезапусками используйте volume:

```bash
docker run --name pgvector \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=vector_db \
  -p 5432:5432 \
  -v pgvector_data:/var/lib/postgresql/data \
  -d pgvector/pgvector:pg17
```

## Примеры команд

### Построить индекс

```bash
python -m codecollector index build --project demo_projects/sample_python_app
```

### Найти primary target

```bash
python -m codecollector search \
  --project demo_projects/sample_python_app \
  --query "Изменить формирование текста уведомления о назначении тикета Сделать текст уведомления русскоязычным и использовать формулировку «теперь назначен на»."
```

Ожидаемый результат: в top-1 должен появляться `support_app.services.notification_service.build_assignment_message`.

### Собрать context pack

```bash
python -m codecollector context \
  --project demo_projects/sample_python_app \
  --qualname support_app.services.notification_service.build_assignment_message
```

### Прогнать полный dry-run pipeline

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

## Что дальше

Зафиксированные следующие шаги:
- optional LLM hooks для rewrite change request и rerank shortlist;
- контракт генератора изменений: `change packet -> code artifact -> optional test artifact`;
- запуск рекомендованных тестов и затем более широких project-level тестов в текущем PoC.


## Когда обновляются граф, semantic search и knowledge

- `knowledge.yaml` не перестраивается: это исходный human-readable knowledge-слой, который перечитывается сервисами при build/search/context.
- graph/index в Postgres обновляется при `index build` инкрементально по hash файлов и адресно после `apply` только для измененных файлов.
- semantic search documents пересчитываются после `index build` из docstring и `knowledge.yaml`. Синхронизация в Postgres/PGVector выполняется только если тексты документов реально изменились.


## Когда обновляются graph, search documents, vector index и `knowledge.yaml`

- **Graph / index** обновляется при `index build` инкрементально по hash файлов и после `apply` адресно по измененным файлам в staging.
- **Search documents** пересчитываются после `index build` из docstring, `knowledge.yaml` и requirement-текстов.
- **Vector index** синхронизируется только если тексты search documents реально изменились.
- **`knowledge.yaml`** не перестраивается автоматически: это curated source-of-truth для human-readable knowledge.

### Что происходит при появлении нового symbol

- новый публичный или значимый symbol должен иметь docstring;
- если symbol важен для поиска, requirements или повторного использования, должен формироваться **update/proposal** в `knowledge.yaml`;
- итоговое решение о сохранении такой записи принимает человек при review.

## Что означает `relation_confidence_summary`

`relation_confidence_summary` — это краткая сводка надежности найденных связей в `context_pack`.
Она показывает, сколько входящих и исходящих relations имеют confidence `high`, `medium` и `low`.
Это помогает быстро понять, насколько context опирается на точно разрешенные связи, а насколько — на более эвристические.

## Модель embeddings

Для semantic search используется embedding-модель `nomic-embed-text-v2-moe` через локальный Ollama endpoint.
Она выбрана как multilingual-модель для поиска по русскоязычным требованиям и human-readable описаниям.


## Build report

`build_report` показывает не только количество переиндексированных файлов, но и время по основным фазам:
- `graph_indexing_ms` — построение/обновление graph/index по Python-файлам;
- `search_documents_sync_ms` — запись human-readable search documents в Postgres;
- `vector_index_sync_ms` — синхронизация embedding/vector index;
- `search_documents_count` — количество search documents;
- `search_documents_changed` — изменились ли search documents по сравнению с уже сохраненными.

## Reference Library MVP

### Reference Library lifecycle
Reference Library is a separate curated artifact and is not updated as part of normal project work.
During regular `pipeline replay` and `index build`, the system only ensures that reference artifacts are available in the stores. If reference documents are already indexed, resync is skipped.
A full resync of Reference Library should be done only after explicit changes in `reference_library/`.


Структура MVP:

```text
reference_library/
  library.yaml
  python/
    templates/
    reusable/
```

### Типы reference artifacts
- `template` — пример/образец для адаптации;
- `reusable_component` — почти готовый код для прямого переиспользования.

### Retrieval stage
Reference-кандидаты ищутся:
- по change request;
- по выбранному target;
- по knowledge descriptions.

### Materialization stage
Перед передачей в generation context reference artifacts materialize-ятся в:
- `full_file`, если файл небольшой;
- `snippet`, если файл больше лимита.

### Что попадает в generation context
Для каждого reference artifact передается:
- title;
- artifact type;
- usage mode;
- why selected;
- content mode;
- actual content.

Генератору не нужен прямой доступ к Reference Library.

## Build report timing fields
- `graph_indexing_ms` — project graph/index update time.
- `search_documents_sync_ms` — sync time for project search documents.
- `vector_index_sync_ms` — sync time for project vector index.
- `reference_sync_ms` — sync time for Reference Library documents when explicit sync happens.
- `reference_vector_sync_ms` — vector sync time for Reference Library when explicit sync happens.


### ADR-007. Внешний генератор вызывается через CLI и файловый request/result contract
На текущем этапе `codecollector` вызывает `codegenerator` как отдельный процесс (`python -m codegenerator ...`) и передает request/result через JSON/YAML-файлы в run bundle.

Причина:
- проще отлаживать содержимое generation packet;
- не нужен отдельный HTTP lifecycle;
- trace и run artifacts остаются прозрачными и повторяемыми.

## Интеграция с codegenerator
Команда `pipeline generate` собирает `generation request`, вызывает внешний `codegenerator`, сохраняет request/result в `.runs/`, затем применяет возвращенный `code_artifact` в staging workspace.


## Verification and repair

After `external_generate` and `apply_staging`, `codecollector` runs post-apply verification. The current checks are AST parsing, `compileall`, optional `ruff`, recommended pytest targets, and optional full-project pytest. When verification fails and repair is enabled, `codecollector` builds a compact verification summary and calls `codegenerator repair`, then applies the repaired artifact into a fresh staging workspace and reruns verification.

## Context budgeting

Before invoking `codegenerator`, `codecollector` measures and reduces the outgoing generation packet. For small symbol-level edits it prefers the target symbol source and omits `full_file_source`. It also limits related tests and reference artifacts. Final packet size metrics are stored in `context_metrics` inside the run bundle.


## Проверки после apply

После применения generated artifact pipeline выполняет: `ast_parse`, `py_compile`, optional `ruff`, `pytest_recommended` и optional full-project pytest. Если apply падает до verification, это считается `apply_failed` и может запускать repair. После repair pipeline повторяет apply и затем повторно запускает проверки. Если repaired artifact не дает итогового diff, результат помечается как потеря intent изменения.
