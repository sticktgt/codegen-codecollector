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
Локальный `index.db` рядом с проектом не используется: runtime graph/index хранится в Postgres.
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

### 5. Полный dry-run pipeline
Pipeline состоит из шагов:
- build/update index;
- primary target search;
- manual target selection;
- supporting context build;
- external code generation step в режиме replay;
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
