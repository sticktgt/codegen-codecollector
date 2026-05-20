# codecollector

`codecollector` — orchestration-слой для управляемого dry-run изменения кода в локальном проекте по пользовательскому запросу.

Проект индексирует целевой код, помогает выбрать точку изменения, собирает проектный контекст, вызывает внешний `codegenerator`, применяет результат во временный workspace, запускает проверки и сохраняет артефакты запуска. Основной проект не изменяется автоматически: результат остается в staging workspace и применяется только после решения человека.

Текущая реализация ориентирована на Python-проекты, CLI-контракт, JSON-артефакты, локальные или совместимые с Ollama LLM-сервисы и human-in-the-loop review.

## Назначение

`codecollector` не генерирует код самостоятельно. Он подготавливает структурированный, проверяемый и воспроизводимый процесс изменения кода.

Основные функции:

- регистрация и удаление проектов;
- onboarding проекта из рабочей папки с кодом;
- построение и обновление индекса проекта;
- генерация и обновление `.codecollector/knowledge.yaml`;
- enrichment `knowledge.yaml` по архитектурному документу через LLM;
- поиск релевантных symbols;
- LLM-assisted analyze пользовательского запроса;
- выбор target для `replace_symbol` или anchor для `insert_after_symbol`;
- сбор context pack;
- формирование `allowed_api_surface`, `contract_context`, `required_contracts`, `required_class_members` и `model_surfaces`;
- подбор reference artifacts;
- подготовка request для внешнего `codegenerator`;
- вызов `codegenerator` в режимах `generate`, `generate-test`, `repair` и `review-generated-test-failure`;
- применение production artifact во временный workspace;
- применение generated test, если он создан и прошел статические проверки;
- запуск verification;
- выполнение repair для исправимых ошибок production-кода;
- выполнение рекомендательной проверки при ошибке сгенерированного теста;
- формирование diff, impact summary и dry-run merge plan;
- применение workspace в основной проект по явной команде;
- сохранение run artifacts для анализа.

## Границы ответственности

`codecollector` отвечает за orchestration, проектный контекст, проверки и workspace lifecycle.

`codecollector` не отвечает за:

- непосредственную генерацию production-кода;
- непосредственную генерацию тестового файла;
- сборку LLM-промптов для generation, test generation, repair и review;
- низкоуровневый вызов LLM в `codegenerator`;
- web-интерфейс и CR lifecycle в `codeui`.

Эти задачи выполняются соответственно в `codegenerator` и `codeui`.

## Основной сценарий dry-run

Типовой сценарий работы:

1. Пользователь регистрирует проект или выполняет onboarding нового проекта.
2. `codecollector` строит или обновляет индекс проекта.
3. Пользователь запускает `sessions analyze`.
4. Analyze оценивает запрос, определяет operation и находит кандидатов.
5. Пользователь принимает рекомендацию или выбирает target вручную.
6. Пользователь запускает `sessions generate`.
7. Pipeline собирает context pack и reference artifacts.
8. Pipeline формирует request для `codegenerator`.
9. `codegenerator` возвращает production artifact.
10. `codecollector` применяет artifact в staging workspace.
11. `codecollector` запускает статические проверки production patch.
12. Если production patch требует repair, вызывается `codegenerator repair`.
13. После валидного production patch вызывается `generate-test`.
14. Generated test применяется в workspace, если он прошел статические проверки.
15. Runtime verification запускает `compileall` и рекомендованные pytest targets.
16. Если падает только generated test, может быть выполнен advisory review.
17. Формируется `merge_plan` в режиме `dry_run`.
18. Итоговый run сохраняется в `.runs/`.
19. Пользователь принимает решение, применять workspace в основной проект или нет.

## Проекты

### Регистрация проекта

```bash
python -m codecollector projects register \
  --project-name sample_python_app \
  --project-root /path/to/project
```

Команда регистрирует проект с указанным `project_root`.

Повторная регистрация проекта с тем же `project_root` блокируется. Индексные и vector-данные разделяются по `project_root`, поэтому один и тот же путь не должен быть зарегистрирован несколько раз.

### Onboarding существующего проекта

```bash
python -m codecollector projects onboard \
  --project-id <project_id> \
  --full
```

Команда строит индекс проекта и пересобирает `.codecollector/knowledge.yaml` из фактических modules и symbols.

### Onboarding нового проекта из папки

```bash
python -m codecollector projects onboard \
  --input-root /path/to/onboarding-folder \
  --project-name sample_python_app \
  --full
```

Ожидаемая структура входной папки:

```text
/path/to/onboarding-folder/
  src/
  ARCHITECT.md или ARCHITECTURE.md
```

`src/` используется как `project_root`.

Архитектурный документ ищется в корне входной папки по именам из `onboarding.knowledge.architecture_doc_names`. По умолчанию поддерживаются `ARCHITECT.md` и `ARCHITECTURE.md`.

Если архитектурный документ найден, `codecollector`:

1. регистрирует проект;
2. строит индекс кода;
3. создает базовый `knowledge.yaml` по коду;
4. выполняет LLM-enrichment `knowledge.yaml` по архитектурному документу.

Если архитектурный документ найден, но enrichment завершился ошибкой, onboarding считается неуспешным. Регистрация проекта и индексные данные откатываются.

Если архитектурный документ не найден, onboarding продолжается без LLM-enrichment и пишет warning в лог.

Чтобы отключить LLM-enrichment для конкретного запуска:

```bash
python -m codecollector projects onboard \
  --input-root /path/to/onboarding-folder \
  --project-name sample_python_app \
  --skip-architecture-enrichment
```

### Диагностика architecture enrichment

При подготовке prompt для enrichment `codecollector` сохраняет валидность контекста:

- сначала старается передать полный список файлов и symbols;
- при нехватке budget сжимает поля всего набора, а не выбирает произвольную часть проекта;
- компактный режим symbols передает полный список `qualname/kind/module`;
- архитектурный документ может быть урезан, но не ниже минимального полезного порога.

Если полный компактный список symbols и минимально полезный фрагмент архитектурного документа не укладываются в лимит, onboarding нужно запустить с большим лимитом или с `--skip-architecture-enrichment`.

Ответ LLM разбирается как JSON. При ошибке разбора или незавершенном ответе raw response сохраняется в `.runs/knowledge_enrichment_traces/` внутри проекта `codecollector`.

После нормализации LLM-ответа выполняется диагностика ссылок в `architecture.flows.steps`. Если шаг похож на ссылку на symbol или module, но не находится в индексе проекта, он возвращается в `knowledge_enrichment_unmatched_mentions` и `knowledge_enrichment_warnings`.

В `knowledge.yaml` отдельные `unresolved_steps` и `unmatched_mentions` не записываются. Эти данные остаются диагностикой onboarding.

### Переиндексация проекта

Для уже зарегистрированного проекта доступна отдельная переиндексация без повторного onboarding:

```bash
python -m codecollector projects reindex --project-id <project_id>
```

По умолчанию выполняется инкрементальная переиндексация.

Для полного graph rebuild:

```bash
python -m codecollector projects reindex --project-id <project_id> --full
```

`--full` выполняет полный graph rebuild, но не означает принудительный пересчет embeddings. Embeddings пересчитываются только если изменились search documents.

Если инкрементальная переиндексация не обнаружила измененных или удаленных файлов, search document sync и vector sync пропускаются.

Ответ содержит:

- `indexed_files`;
- `unchanged_files`;
- `deleted_files`;
- `search_documents_count`;
- `search_documents_changed`;
- `search_documents_change_reason`;
- `graph_indexing_ms`;
- `search_documents_sync_ms`;
- `vector_index_sync_ms`;
- `embedded_documents_count`;
- `vector_sync_mode`;
- reference sync metrics.

Текущий vector sync режим при изменениях — полный vector resync всех search documents проекта:

```text
full_project_documents_when_changed
```

Если изменений нет, vector sync пропускается.

### Удаление проекта

```bash
python -m codecollector projects delete --project-id <project_id>
```

Команда удаляет регистрацию проекта и очищает graph/vector index данные по `project_root`.

Если соответствующие graph/vector записи уже отсутствуют, это не считается ошибкой. Если количество удаленных graph search documents и vector documents различается, команда возвращает warning о возможном рассинхроне индекса.

Если в registry остались другие проекты с тем же `project_root`, очистка индекса пропускается, чтобы не удалить данные, которые еще используются другой регистрацией.

## Analyze

Команда `sessions analyze` создает session, оценивает пользовательский запрос, определяет operation, выполняет поиск и возвращает список кандидатов.

```bash
python -m codecollector sessions analyze \
  --project-id <project_id> \
  --title "Изменить текст уведомления о назначении тикета" \
  --description "Сделать уведомление на русском языке." \
  --constraint "Не менять внешний контракт API"
```

Analyze может использовать два LLM-assisted этапа:

1. `search_plan` — оценивает качество запроса, operation, expected symbols и поисковый план.
2. `candidate_rerank` — ранжирует candidate cards и выбирает target или anchor.

Если пользователь явно передал operation, она имеет приоритет над LLM-рекомендацией.

### Качество запроса

Analyze возвращает `request_quality`.

```json
{
  "request_quality": {
    "status": "processable",
    "reason": "Запрос достаточно конкретен.",
    "missing_information": []
  }
}
```

Возможные статусы:

| Статус | Значение |
|---|---|
| `processable` | Запрос достаточно конкретен для выбора target или anchor. |
| `uncertain` | Есть неопределенность, но система может сделать рабочую попытку. |
| `insufficient` | Запрос недостаточен для generation. Нужно переписать запрос и заново запустить analyze. |

Если `request_quality.status=insufficient`, generation блокируется.

Если первичный search plan считал запрос недостаточным, но candidate rerank уверенно выбрал target и operation, итоговый `request_quality` может быть повышен до `processable` с указанием исходного статуса и причины разрешения.

### Operation source

Итоговая operation возвращается в `requested_operation`.

```json
{
  "requested_operation": "insert_after_symbol",
  "operation_source": "llm_rerank",
  "operation_confidence": 0.9,
  "operation_reason": "Запрос требует добавить новый method."
}
```

Возможные источники:

| Источник | Значение |
|---|---|
| `user` | Operation явно задана пользователем. |
| `llm_search_plan` | Operation определена на этапе search plan. |
| `llm_rerank` | Operation уточнена на этапе rerank кандидатов. |
| `fallback` | Использовано техническое значение по умолчанию. |

### Target recommendation

Analyze возвращает `target_recommendation`.

```json
{
  "target_recommendation": {
    "recommended_operation": "insert_after_symbol",
    "insert_scope": {
      "value": "class_body",
      "confidence": 0.9,
      "reason": "API-логика реализована методами класса."
    },
    "expected_new_symbol_kind": "method",
    "parent_qualname": "support_app.api.controllers.TicketController",
    "recommended_target": "support_app.api.controllers.TicketController",
    "target_role": "anchor",
    "target_confidence": 0.95,
    "manual_review_required": false
  }
}
```

Возможные роли:

| Роль | Значение |
|---|---|
| `target` | Symbol заменяется. |
| `anchor` | Symbol используется как точка вставки. |
| `parent_class` | Target нормализован до класса для вставки method. |
| `unknown` | Роль не определена. |

Для `replace_symbol` выбранный target является объектом замены.

Для `insert_after_symbol` выбранный target является anchor. Новый symbol создается `codegenerator`.

Если LLM выбирает method-anchor для `insert_after_symbol`, analyze может нормализовать anchor до parent class и установить `insert_scope=class_body`.

Если ожидаемый новый symbol является class, вставка нормализуется в `insert_scope=module_body`, даже если anchor — существующий class.

### Candidates

Analyze возвращает candidates для UI или CLI.

```json
{
  "qualname": "support_app.api.controllers.TicketController.agent_summary_endpoint",
  "name": "agent_summary_endpoint",
  "kind": "method",
  "file_path": "support_app/api/controllers.py",
  "score": 23.31,
  "confidence": 1.0,
  "relevance_category": "высокая",
  "reasons": [
    "Существующий API-метод с похожей логикой"
  ],
  "ranked_by_llm": true,
  "llm_recommended": false,
  "llm_rank": 2,
  "llm_reason": "Подходит как anchor для новой функции статистики."
}
```

Основные поля:

| Поле | Значение |
|---|---|
| `qualname` | Полное имя symbol. |
| `kind` | `module`, `class`, `function`, `method`. |
| `file_path` | Файл symbol. |
| `score` | Поисковый score. |
| `confidence` | Нормализованная уверенность. |
| `relevance_category` | Категория релевантности. |
| `reasons` | Причины попадания в candidates. |
| `ranked_by_llm` | Кандидат участвовал в LLM rerank. |
| `llm_recommended` | Кандидат рекомендован LLM. |
| `llm_rank` | Ранг по LLM. |
| `llm_reason` | Причина ранга. |

## Выбор target

Команда `sessions select-target` фиксирует ручной выбор target или anchor.

```bash
python -m codecollector sessions select-target \
  --session-id <session_id> \
  --selected-qualname support_app.services.notification_service.build_assignment_message \
  --operation replace_symbol
```

`select-target` не исправляет недостаточный запрос. Если analyze вернул `insufficient`, нужно переписать запрос и снова выполнить analyze.

## Generate pipeline

Команда `sessions generate` запускает pipeline по session.

```bash
python -m codecollector sessions generate --session-id <session_id>
```

Если session не готова к generation, команда возвращает JSON с причиной блокировки.

```json
{
  "result_summary": {
    "status": "needs_user_decision",
    "generation_blocked": true,
    "block_reason": "insufficient_request",
    "request_quality_status": "insufficient",
    "missing_information": [
      "конкретное ожидаемое поведение"
    ],
    "recommended_action": "rewrite_request_and_run_analyze_again"
  }
}
```

## Context pack

Context pack содержит структурированный контекст выбранного target или anchor:

- target symbol;
- parent symbol;
- class members;
- module outline;
- related tests;
- recommended tests;
- related production symbols;
- inbound/outbound relations;
- requirement ids;
- reference summary;
- reference artifacts.

Для class-target при `insert_after_symbol/class_body` дополнительно учитываются relations child methods. Это помогает сохранить контракты, которые используются методами класса.

## Contract context

`contract_context` содержит связанные production symbols, выбранные `codecollector`.

Обычно включает:

- `qualname`;
- `file_path`;
- `module_name`;
- `name`;
- `kind`;
- `signature`;
- `source_excerpt`;
- relation metadata;
- `origin_qualname`.

`contract_context` считается фактическим проектным контекстом. Его сигнатуры, import path и source excerpts используются как источник истины для вызовов соседних компонентов.

## Allowed API Surface

`allowed_api_surface` — компактный список разрешенных вызовов, передаваемый в `codegenerator`.

Поверхность строится консервативно:

- `self.<attr>` из `__init__`, если есть type annotation;
- методы типа, видимые в related symbols, graph index или source excerpts;
- вложенные зависимости, если они уже встречаются в видимых вызовах или source excerpts;
- free functions из видимого контекста.

Если тип или метод неясен, он не добавляется в allowed surface.

Если generated code вызывает метод зависимости, которого нет в allowed surface, проверка может вернуть issue `unknown_injected_dependency_method`.

## Required contracts

Если пользователь явно просит использовать существующую сервисную функцию, production contract или не дублировать бизнес-логику, `codecollector` может сформировать блок `required_contracts`.

Блок строится только по видимому project context и `allowed_api_surface`.

`patch_static_semantics` проверяет, что новый generated symbol действительно вызывает обязательный contract. Если generated code заменяет обязательный contract ручной реализацией логики, проверка возвращает issue `required_contract_not_used`.

## Required class members

Для `replace_symbol`, когда target является классом, `codecollector` формирует `required_class_members`.

Блок строится из фактического исходного класса и включает существующий публичный API класса:

- `__init__`;
- публичные методы без префикса `_`;
- явно видимые public members.

Назначение блока — не дать модели при замене класса случайно удалить существующие публичные методы.

Если обязательный метод исчез, `patch_static_semantics` возвращает issue `class_replacement_missing_existing_member`.

`codecollector` не считает фразу вида `убрать NotImplementedError из save_note` запросом на удаление метода. Такая формулировка трактуется как требование сохранить метод и реализовать его тело.

## Model surfaces

Для связанных классов-моделей из видимого project context `codecollector` формирует `model_surfaces`.

Блок содержит:

- имя модели;
- qualname;
- видимые поля;
- constructor fields;
- required constructor fields;
- источник.

`model_surfaces` используется для:

- генерации production-кода без выдуманных полей;
- генерации тестов без неверных constructor kwargs;
- статической проверки generated tests;
- статической проверки production-кода в пределах доступного контекста.

## Verification

После применения запускаются проверки.

Используются:

- `runtime_ast_parse`;
- `runtime_py_compile`;
- `runtime_pytest_recommended`;
- `patch_static_semantics`;
- `generated_test_static_semantics`;
- `generated_test_relevance`.

Статусы verification:

| Статус | Значение |
|---|---|
| `ready_for_merge_review` | Production-код и проверки прошли. |
| `verification_failed` | Ошибка в production-коде или основных проверках. |
| `generated_test_verification_failed` | Production-код прошел основные проверки, но generated test не прошел verification. |
| `repair_verification_failed` | Repair выполнен, но verification не прошел. |
| `repair_no_effective_change` | Repair не дал полезного изменения. |

Если падает только generated test, production-код не описывается автоматически как некорректный и не отправляется в repair.

### patch_static_semantics

`patch_static_semantics` проверяет только новый или заменяемый symbol.

Проверяются:

- дубликаты symbols;
- вызовы production-contract symbols;
- обязательные аргументы production contracts;
- фиктивные literals в обязательных аргументах;
- receiver для method-контрактов;
- неизвестные поля результата production-контракта;
- небезопасный возврат `__dict__`;
- неизвестные dependency methods;
- required contracts usage;
- required class members;
- неизвестные annotation names;
- model surface usage.

Проверка может возвращать advisory warnings. Пример:

```json
{
  "possible_existing_method_contract_lost": {
    "warnings": [
      {
        "code": "possible_existing_method_contract_lost",
        "severity": "warning",
        "method": "note.note_model.Note.is_modified",
        "message": "...",
        "missing_exception_contracts": ["TypeError"]
      }
    ]
  }
}
```

Такие warnings не блокируют pipeline, но должны учитываться при ручном review.

### generated_test_static_semantics

Generated test проверяется до runtime-запуска.

Проверяются:

- синтаксис;
- наличие test functions;
- наличие assert;
- отсутствие `pytest-mock`/`mocker`;
- отсутствие неразрешенных имен;
- использование тестируемого symbol;
- отсутствие неизвестных keyword-аргументов при создании видимых project classes/dataclass-like models.

Если test artifact не проходит статические проверки, он не применяется к финальному workspace.

Если generated test применен, но падает только он, результат получает статус `generated_test_verification_failed`.

Rejected generated tests считаются excluded files. `workspaces apply` не должен копировать такие файлы в основной проект.

## Repair

Repair используется, если ошибка production artifact считается исправимой.

Repair получает:

- исходный change request;
- target;
- previous artifact;
- verification error context;
- project context;
- reference context.

Repair должен:

- сохранять исходную operation;
- сохранять insert scope;
- исправлять previous artifact, а не генерировать новый сценарий с нуля;
- не придумывать новые методы зависимостей;
- не подставлять фиктивные литералы вместо обязательных аргументов;
- использовать видимый параметризованный контракт, если широкий сценарий недоступен;
- возвращать результат в формате `GenerationResult`.

## Advisory review при ошибке generated test

Если production-код прошел основные проверки, но generated test завершился со статусом `generated_test_verification_failed`, pipeline может выполнить рекомендательную проверку через `codegenerator`.

Review не изменяет код, не запускает repair и не принимает решение автоматически.

Review получает компактный контекст:

- исходный CR;
- target;
- production artifact;
- production diff;
- generated test artifact;
- failed verification blocks;
- issues;
- stdout/stderr excerpt;
- advisory warnings.

Review возвращает рекомендацию для человека:

- является ли тест вероятно ошибочным;
- нашел ли тест возможную production-регрессию;
- есть ли риск в production-коде;
- стоит ли сохранять production-код, отклонить его или отправить на ручной review.

## Generated test apply

`generated_test_apply` описывает применение сгенерированного теста.

Пример принятого теста:

```json
{
  "applied_tests": [
    "tests/test_generated_generate_test_Note.py"
  ],
  "count": 1,
  "skipped": false
}
```

Пример отклоненного теста:

```json
{
  "applied_tests": [],
  "count": 0,
  "skipped": true,
  "reason": "generated_test_semantic_checks_failed",
  "candidate_test_files": [
    "tests/test_generated_generate_test_Note.py"
  ],
  "verification_failed": true,
  "merge_recommended": false,
  "excluded_files": []
}
```

## Pytest target resolver

`ValidationService` преобразует рекомендованные тестовые цели в pytest paths.

Поддерживаются форматы:

```text
tests/test_generated_generate_test_TicketController.py
```

```text
tests.test_generated_generate_test_TicketController.test_function
```

```text
tests.test_generated_generate_test_TicketController.TestClass.test_method
```

Последний формат резолвится в pytest node id:

```text
tests/test_generated_generate_test_TicketController.py::TestClass::test_method
```

Если точный dotted target не резолвится, но найден fallback file path, это диагностируется как fallback.

Если среди целей одновременно есть pytest node id и путь к тому же файлу, `ValidationService` оставляет более точный node id и не запускает весь файл повторно.

## Применение workspace

Основной проект изменяется только явной командой:

```bash
python -m codecollector workspaces apply --workspace-id <workspace_id>
```

Команда применяет production changes из workspace в основной проект и пропускает excluded files.

Ответ содержит диагностику:

- `applied_files`;
- `copied_files`;
- `deleted_files`;
- `skipped_excluded_files`;
- `counts`;
- `timings`;
- `index_refresh`.

`index_refresh` содержит:

- `graph_indexing_ms`;
- `search_documents_sync_ms`;
- `vector_index_sync_ms`;
- `embedded_documents_count`;
- `vector_sync_mode`;
- `embedding_usage`.

## Run artifacts

Каждый pipeline run сохраняется в `.runs/`.

Сохраняются:

- request и result внешних вызовов;
- stderr/stdout внешнего генератора;
- итоговый `pipeline_run_*.json`;
- шаги pipeline;
- usage и timing внешних LLM-вызовов;
- diff;
- verification report;
- generated test diagnostics;
- generated test failure review;
- merge plan;
- warnings и причины пропущенных этапов.

Run artifacts — основной материал для отладки качества генерации.

## Merge plan

`merge_plan` формируется в режиме `dry_run`.

Он содержит:

- workspace path;
- список changed files;
- symbols in changed files;
- linked requirements;
- recommended tests;
- recommended test commands;
- excluded files;
- summary lines;
- признак `ready_for_manual_merge_review`.

Автоматический merge в основной проект не выполняется.

## CLI

CLI возвращает JSON как для успешных команд, так и для ошибок.

При известной бизнес-ошибке payload содержит:

- `status=failed`;
- `error_type`;
- `message`;
- `details`.

`traceback` добавляется только для непредусмотренных ошибок, у которых нет структурированного `details`.

### Analyze

```bash
python -m codecollector sessions analyze \
  --project-id <project_id> \
  --title "Изменить текст уведомления о назначении тикета" \
  --description "Сделать уведомление на русском языке." \
  --constraint "Не менять внешний контракт API"
```

### Select target

```bash
python -m codecollector sessions select-target \
  --session-id <session_id> \
  --selected-qualname support_app.services.notification_service.build_assignment_message \
  --operation replace_symbol
```

### Generate

```bash
python -m codecollector sessions generate --session-id <session_id>
```

### Pipeline generate

```bash
python -m codecollector pipeline generate \
  --project demo_projects/sample_python_app \
  --title "Изменить текст уведомления о назначении тикета" \
  --description "Сделать уведомление полностью русскоязычным." \
  --selected-qualname support_app.services.notification_service.build_assignment_message \
  --operation replace_symbol
```

## Конфигурация

Основная конфигурация хранится в `config.yaml`.

Ключевые разделы:

- параметры индексации;
- semantic search;
- graph storage;
- vector storage;
- внешний `codegenerator`;
- verification;
- trace и логирование;
- context selection;
- LLM-assisted analyze;
- onboarding и knowledge enrichment.

Настройки LLM для analyze задаются в `codecollector/config.yaml` и не наследуются из `codegenerator`.

Числовые лимиты, prompt budget и thresholds должны задаваться через конфигурацию.

## Логирование и диагностика

Логи и trace должны показывать:

- выбранный target или anchor;
- operation и источник operation;
- related tests;
- full file usage;
- reference artifacts;
- context metrics;
- trim steps;
- размеры request и prompt;
- проверки и их результаты;
- generated tests и причины их применения или отклонения;
- repair input и repair output;
- embedding batch start/end;
- vector sync mode;
- embedded documents count.

## Структура проекта

| Путь | Назначение |
|---|---|
| `config.yaml` | Основная конфигурация. |
| `.runs/` | Артефакты запусков pipeline. |
| `.workspaces/` | Staging workspace. |
| `api/` | CLI и точки входа. |
| `analysis/` | LLM-assisted analyze. |
| `prompts/` | Prompt-тексты analyze. |
| `orchestration/` | Pipeline orchestration. |
| `indexing/` | Индексация проекта. |
| `search/` | Поиск кандидатов. |
| `context/` | Сбор context pack. |
| `external_codegen/` | Подготовка request и вызов `codegenerator`. |
| `patching/` | Применение artifact. |
| `workspace/` | Workspace, diff, apply и impact summary. |
| `reference_library/` | Reference artifacts. |
| `vector_search/` | Embedding и semantic search. |
| `validation/` | Проверки после применения. |
| `sessions/` | Session-based flow. |
| `projects/` | Registry, cleanup и сопровождение проектов. |
| `onboarding/` | Onboarding и enrichment `knowledge.yaml`. |

## Ограничения текущей реализации

- основная поддержка ориентирована на Python;
- HTTP API поверх CLI-контракта не является основным интерфейсом;
- vector sync при изменении search documents пересинхронизирует весь набор search documents проекта;
- multi-target orchestration не является основным режимом;
- advisory review не принимает автоматическое решение за пользователя;
- generated test может быть отклонен или исключен из apply/merge;
- решение о применении workspace принимает человек.

## Итог

`codecollector` — orchestration-слой controlled dry-run изменения кода. Он обеспечивает анализ запроса, выбор target или anchor, структурный отбор контекста, вызов внешнего `codegenerator`, применение результата в staging workspace, verification, repair, advisory review и подготовку результата к ручному review. Основной проект изменяется только по явной команде пользователя.
