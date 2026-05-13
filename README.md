# codecollector

`codecollector` — orchestration-слой для управляемого dry-run процесса точечного изменения кода по пользовательскому запросу.

Проект индексирует целевой проект, помогает выбрать точку изменения, собирает контекст, вызывает внешний `codegenerator`, применяет результат во временный workspace, запускает проверки и сохраняет артефакты запуска для анализа. Основной проект не изменяется автоматически: результат остается в staging workspace и готовится для ручного review.

Текущая реализация ориентирована на Python-проекты, локальные или совместимые с Ollama LLM-сервисы, CLI-контракт и JSON-артефакты запусков.

## Назначение

`codecollector` не генерирует код самостоятельно. Его задача — подготовить структурированный, проверяемый и воспроизводимый процесс изменения кода.

Основные функции:

- регистрация и сопровождение проектов;
- построение индекса проекта;
- поиск релевантных символов;
- LLM-assisted analyze пользовательского запроса;
- выбор target для `replace_symbol` или anchor для `insert_after_symbol`;
- сбор `context pack`;
- подбор reference artifacts;
- формирование `GenerationRequest`, `RepairRequest` и запроса на `generate-test`;
- вызов внешнего `codegenerator`;
- применение production-артефакта в staging workspace;
- применение generated test, если он создан и прошел статические проверки;
- запуск verification;
- выполнение repair для исправимых ошибок;
- формирование diff, impact summary и dry-run merge plan;
- сохранение run artifacts.

## Основной dry-run сценарий

Типовой сценарий работы:

1. Пользователь регистрирует проект или использует уже зарегистрированный проект.
2. `codecollector` строит или обновляет индекс проекта.
3. Пользователь запускает `sessions analyze`.
4. Analyze оценивает запрос, определяет operation и находит кандидатов.
5. Пользователь принимает рекомендацию или выбирает target вручную.
6. Пользователь запускает `sessions generate`.
7. Pipeline собирает context pack и reference artifacts.
8. Pipeline формирует request для `codegenerator`.
9. `codegenerator` генерирует production artifact.
10. `codecollector` применяет artifact в staging workspace.
11. `codecollector` запускает статические проверки production patch.
12. Если production patch требует repair, вызывается `codegenerator repair`.
13. После валидного production patch запускается `generate-test`.
14. Generated test проходит статические проверки и применяется в staging workspace.
15. Runtime verification запускает `compileall` и рекомендованные tests.
16. Формируется `merge_plan` в режиме `dry_run`.
17. Итоговый run сохраняется в `.runs/`.

## Операции изменения

### `replace_symbol`

`replace_symbol` заменяет существующий symbol. Выбранный target является объектом изменения.

Пример сценария: изменить текст уведомления в существующей функции.

```bash
python -m codecollector sessions analyze \
  --project-id <project_id> \
  --title "Изменить текст уведомления о назначении тикета" \
  --description "Сделать уведомление на русском языке и не менять сигнатуру функции." \
  --operation replace_symbol
```

### `insert_after_symbol`

`insert_after_symbol` добавляет новый symbol после существующего anchor. Выбранный target является точкой вставки.

Примеры:

- добавить top-level function после существующей функции;
- добавить class после существующего class;
- добавить method внутрь class, если target — class или method-anchor нормализован до parent class.

Для `insert_after_symbol` важно различать target как anchor и новый symbol как фактический объект генерации.

## Analyze

Команда `sessions analyze` создает session, оценивает пользовательский запрос, определяет operation, выполняет поиск и возвращает список кандидатов.

Пример:

```bash
python -m codecollector sessions analyze \
  --project-id <project_id> \
  --title "Добавь API-функцию для получения краткой статистики по тикетам." \
  --description "Функция должна использовать существующую сервисную функцию формирования отчета/статистики, а не дублировать бизнес-логику в API-слое."
```

Analyze может использовать два LLM-assisted этапа:

1. `search_plan` — оценивает качество запроса, operation, expected symbols и поисковый план.
2. `candidate_rerank` — ранжирует candidate cards и выбирает target или anchor.

Если пользователь явно передал operation, она имеет приоритет над LLM-рекомендацией.

## Качество запроса

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
| `insufficient` | Запрос недостаточен для генерации. Нужно переписать запрос и заново запустить analyze. |

Если `request_quality.status=insufficient`, generation блокируется. В этом случае `sessions generate` возвращает JSON с `generation_blocked=true`, причиной блокировки и списком недостающей информации.

## Operation source

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

## Target recommendation

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

Если LLM сначала выбирает method-anchor для `insert_after_symbol`, а затем analyze нормализует anchor до parent class, `codecollector` сохраняет согласованную модель вставки `insert_scope=class_body` и ожидает новый method внутри класса.

Если ожидаемый новый symbol является `class` (например, dataclass-модель), вставка нормализуется в `insert_scope=module_body`, даже если anchor — существующий class. В этом случае выбранный class остается anchor, а `parent_qualname` указывает на module. `class_body` используется только для добавления новых methods внутрь класса.

## Кандидаты analyze

Analyze возвращает candidates для UI или CLI.

Пример:

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

Внутренний recall-набор может быть шире списка, возвращаемого пользователю. Размеры управляются настройками:

- `analysis.recall.max_recall_candidates`;
- `analysis.candidate_context.max_candidate_cards`;
- `analysis.result.max_candidates`.

## Prompt budget analyze

Analyze возвращает usage и budget-метрики.

```json
{
  "analysis_usage": {
    "calls": 2,
    "prompt_tokens": 4863,
    "output_tokens": 1061,
    "total_tokens": 5924,
    "prompt_chars": 19485,
    "duration_sec": 22.14,
    "steps": {
      "search_plan": {
        "prompt_chars": 6598,
        "total_tokens": 2135
      },
      "candidate_rerank": {
        "prompt_chars": 12887,
        "total_tokens": 3789
      }
    }
  }
}
```

Если prompt уменьшается, в `trim_steps` фиксируются конкретные действия:

```json
{
  "prompt_budget": {
    "max_prompt_chars": 15000,
    "prompt_chars": 12887,
    "trim_steps": ["candidate_cards:6->4"]
  }
}
```

Лимиты analyze задаются в `config.yaml`, в секциях `analysis.llm_assist` и `analysis.candidate_context`.

## Select target

Команда `sessions select-target` фиксирует ручной выбор target или anchor.

```bash
python -m codecollector sessions select-target \
  --session-id <session_id> \
  --selected-qualname support_app.api.controllers.TicketController \
  --operation insert_after_symbol
```

`select-target` не исправляет недостаточный запрос. Если analyze вернул `insufficient`, нужно переписать запрос и снова выполнить analyze.

## Generate

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

`context pack` содержит структурированный контекст выбранного target или anchor:

- target symbol;
- module outline;
- соседние symbols;
- inbound и outbound relations;
- related production symbols;
- related tests;
- recommended tests;
- requirement ids;
- reference summary;
- reference artifacts.

Для class-target при `insert_after_symbol` и `class_body` дополнительно учитываются relations child-methods. Это позволяет сохранять сервисные и доменные контракты, которые уже используются методами класса, даже если у самого класса нет прямых call-relations.

## Contract context

`contract_context` — набор связанных production-контрактов, который передается во внешний `codegenerator`.

Обычно в него входят:

- `qualname`;
- `file_path`;
- `module_name`;
- `name`;
- `kind`;
- `signature`;
- `source_excerpt`;
- relation metadata;
- `origin_qualname`.

`patch_static_semantics` сверяет вызовы production symbols из `contract_context` только внутри нового или заменяемого symbol. Это снижает риск ложных срабатываний на существующий код файла.

Проверяются:

- количество обязательных positional-аргументов;
- фиктивные литералы в обязательных аргументах;
- receiver для method-контрактов;
- поля результата production-контракта;
- небезопасный возврат `__dict__`.

## Allowed API Surface

`allowed_api_surface` — компактный список разрешенных вызовов, передаваемый в `GenerationRequest.project_context` и `RepairRequest.project_context`.

Поверхность строится консервативно:

- `self.<attr>` из `__init__`, если есть type annotation;
- методы типа, видимые в `related_symbols`, graph index или source excerpts;
- вложенные зависимости, если они уже встречаются в видимых вызовах или source excerpts;
- free functions из видимого контекста.

Если тип или метод неясен, он не добавляется в allowed surface.

Пример:

```json
{
  "dependencies": [
    {
      "access_path": "self.service.repository",
      "type_name": "TicketRepository",
      "source": "visible_call_path",
      "allowed_methods": [
        {
          "name": "list_by_agent",
          "signature": "def list_by_agent(self, agent_name: str) -> list[Ticket]:",
          "qualname": "support_app.storage.ticket_repository.TicketRepository.list_by_agent"
        }
      ],
      "origin_examples": [
        {
          "access_path": "self.service.repository",
          "method": "list_by_agent",
          "example": "self.service.repository.list_by_agent",
          "line": "24"
        }
      ]
    }
  ]
}
```

`codecollector` проверяет вложенные вызовы вроде `self.service.repository.list_all()`. Если метод не входит в видимые методы соответствующего типа, создается blocking issue `unknown_injected_dependency_method`.

## Вызов codegenerator

`codecollector` вызывает внешний `codegenerator` через файловый JSON request.

Режимы:

- `generate`;
- `generate-test`;
- `repair`.

Для `repair` сохраняются согласованные `operation`, `insert_scope`, `parent_qualname` и `expected_new_symbol_kind`. Если предыдущий artifact содержит невозможную комбинацию вроде `expected_new_symbol_kind=class` с `insert_scope=class_body`, metadata нормализуется до `module_body` перед repair/apply. Repair request получает `allowed_api_surface`; если доступен исходный generation request, surface переиспользуется из него, иначе пересобирается по текущему context pack. Для `insert_after_symbol` / `class_body` repair также получает source текущего файла, если это разрешено конфигурацией и укладывается в лимит.

### GenerationRequest

Ключевые поля:

- `request_id`;
- `mode`;
- `change_request`;
- `target`;
- `project_context`;
- `reference_context`;
- `generated_code_artifact`;
- `options`;
- `context_metrics`.

Для `generate-test` с `insert_after_symbol` объектом тестирования считается сгенерированный symbol, а выбранный target остается anchor.

### RepairRequest

Ключевые поля:

- `request_id`;
- `mode`;
- `previous_generation_request_id`;
- `change_request`;
- `target`;
- `error_context`;
- `previous_artifact`;
- `project_context`;
- `reference_context`;
- `options`.

В `previous_artifact.operation` сохраняется исходная operation. Repair не должен превращать вставку нового symbol в замену anchor-symbol.

### GenerationResult

Основные поля:

- `request_id`;
- `status`;
- `code_artifact`;
- `test_artifact`;
- `planner_result`;
- `test_planner_result`;
- `warnings`;
- `trace_path`;
- `llm_usage`;
- `error_type`;
- `message`.

## Применение в staging workspace

`codecollector` создает временный workspace в `.workspaces/` и применяет туда production artifact.

Применение включает:

- копирование проекта;
- применение `code_artifact.code`;
- применение `import_changes`;
- применение generated test, если он принят;
- targeted reindex измененных файлов;
- diff между исходным проектом и workspace;
- impact summary.

Основной проект не изменяется автоматически.

## Verification

После применения запускаются проверки.

Используются:

- `runtime_ast_parse`;
- `runtime_py_compile`;
- `runtime_pytest_recommended`;
- статические проверки production artifact;
- статические проверки generated test;
- проверка релевантности generated test.

Статусы verification:

| Статус | Значение |
|---|---|
| `ready_for_merge_review` | Production-код и проверки прошли. |
| `verification_failed` | Ошибка в production-коде или основных проверках. |
| `generated_test_verification_failed` | Production-код корректен, но generated test не прошел verification. |
| `repair_verification_failed` | Repair выполнен, но verification не прошел. |
| `repair_no_effective_change` | Repair не дал полезного изменения. |

Если падает только generated test, production-код не описывается как некорректный и не отправляется в repair.

## Проверка generated test

Generated test проверяется до runtime-запуска.

Проверяются:

- синтаксис;
- наличие тестовых функций;
- наличие assert;
- отсутствие `pytest-mock`/`mocker`;
- отсутствие неразрешенных имен;
- использование сгенерированного symbol;
- отсутствие неизвестных keyword-аргументов при создании видимых project classes/dataclass-like models;
- релевантность теста изменению.

Если test artifact не проходит статические проверки, он не применяется к финальному workspace.

Если generated test применен, но падает только он, результат получает статус `generated_test_verification_failed`.


## Contract attribute requirements для generated tests

При сборке generation/generate-test request `codecollector` пытается извлечь из видимых production-контрактов структурный блок `contract_attribute_requirements`.

Блок строится только из фактического project context:

- source excerpt production-контракта;
- сигнатуры параметров;
- чтения атрибутов внутри контракта, например `ticket.assigned_to`;
- видимые поля class/dataclass-like модели из contract context.

Если связь между параметром, item type и моделью не восстанавливается надежно, requirement не добавляется. `ARCHITECTURE.md`, reference artifacts и догадки модели не используются для добавления таких требований.

Пример:

```json
{
  "contract_qualname": "support_app.services.report_service.build_agent_summary",
  "parameter": "tickets",
  "item_type": "Ticket",
  "required_fields": ["assigned_to", "priority", "status"],
  "constructor_fields": ["ticket_id", "title", "description", "priority", "status", "assigned_to", "created_at", "tags"],
  "source": "contract_source_attribute_reads"
}
```

`codegenerator` использует этот блок как явное ограничение для fake/stub/test data. Статическая проверка generated test дополнительно блокирует keyword-аргументы конструкторов project models, которых нет в видимых полях или `__init__`.


## Required contracts для production-кода

Если пользователь явно просит использовать существующую сервисную функцию, production contract или не дублировать бизнес-логику, `codecollector` может сформировать блок `required_contracts`. Блок строится только по видимому project context и `allowed_api_surface`; несуществующие contracts не добавляются.

Пример:

```json
{
  "qualname": "support_app.services.report_service.build_agent_summary",
  "name": "build_agent_summary",
  "reason": "user_requested_existing_contract_reuse",
  "source": "allowed_api_surface.free_functions"
}
```

`required_contracts` передается в generation, repair и generate-test request. `patch_static_semantics` проверяет, что новый generated symbol действительно вызывает обязательный contract. Если generated code заменяет обязательный contract ручной реализацией логики, проверка возвращает issue `required_contract_not_used`, и результат может быть отправлен в repair.

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

Последний формат должен резолвиться в pytest node id:

```text
tests/test_generated_generate_test_TicketController.py::TestClass::test_method
```

Если точный dotted target не резолвится, но найден fallback file path, это диагностируется как fallback, а не как ошибка проверки. Если среди целей одновременно есть pytest node id и путь к тому же файлу, `ValidationService` оставляет более точный node id и не запускает весь файл повторно.

## Repair

Repair используется, если ошибка считается исправимой.

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
- исправлять предыдущий artifact, а не генерировать новый сценарий с нуля;
- не придумывать новые методы зависимостей;
- не подставлять фиктивные литералы вместо обязательных аргументов;
- использовать видимый параметризованный контракт, если широкий сценарий недоступен;
- возвращать результат в формате `GenerationResult`.

## Run artifacts

Каждый pipeline run сохраняется в `.runs/`.

Сохраняются:

- request и result внешних вызовов;
- stderr внешнего генератора;
- итоговый `pipeline_run_*.json`;
- шаги pipeline;
- usage и timing внешних LLM-вызовов;
- diff;
- verification report;
- generated test diagnostics;
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

CLI возвращает JSON как для успешных команд, так и для ошибок. При известной бизнес-ошибке payload содержит `status=failed`, `error_type`, `message` и `details`, чтобы UI мог показать бизнес-состояние без разбора текстового traceback. Структурные поля ошибки находятся в `details` и не дублируются на верхнем уровне payload. `traceback` добавляется только для непредусмотренных ошибок, у которых нет структурированного `details`.

### Регистрация проекта

```bash
python -m codecollector projects register \
  --project-name sample_python_app \
  --project-root /path/to/project
```

### Onboarding зарегистрированного проекта

```bash
python -m codecollector projects onboard \
  --project-id <project_id> \
  --full
```

Команда строит индекс проекта и пересобирает `.codecollector/knowledge.yaml` из фактических modules/symbols. Новый проект нельзя зарегистрировать повторно с тем же `project_root`: индексные и vector-данные разделяются по `project_root`, поэтому такой дубль блокируется на этапе регистрации.

### Onboarding нового проекта из папки с src и архитектурным описанием

```bash
python -m codecollector projects onboard \
  --input-root /path/to/onboarding-folder \
  --project-name sample_python_app \
  --full
```

В `/path/to/onboarding-folder` должна находиться `src/` с индексируемым кодом. Архитектурное описание ищется в этой же папке по именам из `onboarding.knowledge.architecture_doc_names`, по умолчанию `ARCHITECT.md` и `ARCHITECTURE.md`. Если архитектурный файл найден, `codecollector` регистрирует новый проект с `project_root=/path/to/onboarding-folder/src`, строит базовый knowledge по коду и затем дополняет его данными из архитектурного файла через LLM-assisted enrichment. Для найденного архитектурного файла enrichment является обязательным: если LLM-enrichment не выполнен, регистрация проекта и индексные данные откатываются, а команда завершается ошибкой. Если архитектурный файл не найден, onboarding продолжается по базовой процедуре без LLM-enrichment и пишет warning в лог.

Чтобы отключить LLM-enrichment для конкретного запуска:

```bash
python -m codecollector projects onboard \
  --input-root /path/to/onboarding-folder \
  --skip-architecture-enrichment
```

При подготовке prompt для enrichment `codecollector` сохраняет валидность контекста: сначала передает полный список файлов и symbols, затем при необходимости сжимает поля всего набора, а не выбирает произвольную часть проекта. Компактный режим symbols передает полный список `qualname/kind/module` без docstring, `file_path` и `parent_qualname`. Список symbols не отбрасывается ради prompt budget: если полный компактный список symbols и минимально полезный фрагмент архитектурного документа не укладываются в лимит, onboarding нужно запустить с большим лимитом или с `--skip-architecture-enrichment`. Архитектурный документ режется только до порога `onboarding.knowledge.architecture_doc_min_chars` / `architecture_doc_min_ratio`; ниже этого порога enrichment считается малополезным. Ответ LLM разбирается как JSON-объект; при ошибке разбора или незавершенном ответе LLM, например `done_reason=length` или `done_reason=abort`, raw response сохраняется в `.runs/knowledge_enrichment_traces/` внутри проекта codecollector для диагностики. Лимит генерации для этого вызова задается через `onboarding.knowledge.architecture_enrichment_num_predict`.

После нормализации LLM-ответа onboarding выполняет диагностическую проверку ссылок в `architecture.flows.steps`. Если шаг похож на ссылку на symbol или module, но не находится в индексе проекта, такая ссылка возвращается в `knowledge_enrichment_unmatched_mentions` и `knowledge_enrichment_warnings`. Короткие ссылки вида `Class.method` резолвятся по реальным symbols; если найден один вариант, ссылка считается валидной, если вариантов несколько — возвращается диагностика `ambiguous_reference`. Список `unmatched_mentions`, который могла вернуть LLM, не используется как источник диагностики: расхождения определяются детерминированно по индексу. В `knowledge.yaml` отдельные `unresolved_steps` и `unmatched_mentions` не записываются: flow остается человекочитаемым описанием, а расхождения между архитектурным текстом и кодом остаются диагностикой onboarding.

### Удаление проекта

```bash
python -m codecollector projects delete --project-id <project_id>
```

Команда удаляет регистрацию проекта и очищает graph/vector index данные по `project_root`. Если в базе нет соответствующих записей, это не считается ошибкой. Если количество удаленных `cc_search_documents` и vector documents различается, команда добавляет warning в результат cleanup и пишет предупреждение в лог, потому что это может означать уже существовавший рассинхрон индекса. Если в registry остались другие проекты с тем же `project_root`, очистка индекса пропускается, чтобы не удалить данные, которые еще используются другой регистрацией.

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
python -m codecollector sessions generate \
  --session-id <session_id>
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
- LLM-assisted analyze.

Настройки LLM для analyze задаются в `codecollector/config.yaml` и не наследуются из `codegenerator`.

Числовые лимиты, prompt budget и thresholds должны задаваться через конфигурацию.

## Trace и логирование

Логи и trace должны показывать:

- выбранный target или anchor;
- operation и источник operation;
- related tests;
- example test source, если есть;
- full file usage;
- reference artifacts;
- context metrics;
- trim steps;
- размеры request и prompt;
- проверки и их результаты;
- generated tests и причины их применения или отклонения;
- repair input и repair output.

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
| `workspace/` | Workspace, diff и impact summary. |
| `reference_library/` | Reference artifacts. |
| `vector_search/` | Embedding и semantic search. |
| `verification/` | Проверки после применения. |
| `sessions/` | Session-based flow. |
| `codecollector_ui/` | Streamlit UI. |
| `demo_projects/sample_python_app/` | Демонстрационный Python-проект. |

## Демонстрационный проект

Демонстрационный проект находится в `demo_projects/sample_python_app`.

Основные модули:

- `support_app/api/controllers.py` — API-слой;
- `support_app/services/ticket_service.py` — бизнес-логика по тикетам;
- `support_app/services/notification_service.py` — уведомления;
- `support_app/services/report_service.py` — отчеты и сводки;
- `support_app/storage/ticket_repository.py` — in-memory repository;
- `support_app/domain/models.py` — доменные модели;
- `tests/` — тесты.

Файл `.codecollector/knowledge.yaml` содержит описания модулей, symbols, требований и архитектурных слоев.

## Текущие проблемы и направления дальнейших изменений

Текущие ограничения:

- primary generation может требовать repair;
- LLM иногда выбирает широкий сценарий, которого нет в `Allowed API Surface`;
- качество target selection зависит от analyze и project context;
- generated tests могут быть отклонены статическими проверками или runtime verification;
- target selection может выбрать технически проходящий, но архитектурно менее подходящий слой;
- apply в основной проект и lifecycle workspace остаются отдельными областями развития;
- поддержка других языков требует отдельных адаптеров.

Направления дальнейших изменений:

- усилить выбор API-layer target для запросов про API-функции;
- улучшить стабильность primary planner/coder без зависимости от repair;
- развивать `Allowed API Surface` как обязательный структурный контекст;
- улучшить repair planner и repair request для ошибок неизвестных dependency methods;
- улучшить генерацию тестов через более точный контекст атрибутов fake/stub объектов;
- оформить HTTP API поверх текущего CLI-контракта;
- доработать lifecycle проектов, session, run и workspace;
- добавить контролируемый apply в основной проект;
- расширить диагностику onboarding и enrichment `knowledge.yaml`;
- расширить поддержку языков через адаптеры.

## Что не нужно делать

- Не подгонять prompt под один demo-case.
- Не переносить project-specific logic в низкоуровневые утилиты.
- Не использовать `select-target` как обходной путь для недостаточного запроса.
- Не описывать production-код как некорректный, если упал только generated test.
- Не подменять project context reference-контекстом.
- Не переносить генерацию кода из `codegenerator` в `codecollector`.

## Итоговое состояние

`codecollector` — orchestration-слой controlled dry-run изменения кода. Он обеспечивает анализ запроса, выбор target или anchor, структурный отбор контекста, вызов внешнего `codegenerator`, применение результата в staging workspace, verification и подготовку результата к ручному review.
