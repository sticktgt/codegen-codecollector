# CodeCollector CLI API

Текущая спецификация для интеграции

## 1. Область действия документа

Документ описывает текущее CLI API инструмента `codecollector`: доступные команды, входные параметры, типовые JSON-ответы и схемы основных объектов.

Основная цель документа — дать единый прикладной контракт для проектирования UI, orchestration-слоя и будущего REST API.

В документе сохранена терминология текущей реализации:

- project
- session
- workspace
- pipeline run
- apply
- repair
- merge plan

JSON-схемы ниже не являются formal JSON Schema draft. Это практическое структурное описание текущих объектов и текущего поведения CLI.

---

## 2. Реестр текущих CLI-команд

| Группа | Команда | Что делает | JSON-ответ |
|---|---|---|---|
| `projects` | `register` / `list` / `get` / `delete` / `onboard` | Регистрация проекта и подготовка knowledge/index | Да |
| `sessions` | `analyze` / `select-target` / `generate` / `repair` / `finalize` / `get` / `list` / `delete` | Пошаговый сценарий изменения | Да |
| `workspaces` | `get` / `diff` / `apply` | Просмотр и применение staging workspace | Да |
| `pipeline` | `generate` / `replay` | Прямой end-to-end запуск без session workflow | Да |
| `index` | `build` | Переиндексация проекта | Да |
| `search` | `search` | Поиск кандидатов символов | Да |
| `context` | `context` | Построение контекста вокруг symbol target | Да |
| `apply` | `apply` | Ручное применение prepared artifact | Да |
| `ui` | `ui` | Запуск Streamlit UI | Нет, это служебный запуск |

---

## 3. Команды CLI

### `projects register`

Назначение: регистрирует `project root` в локальном state-store.

```bash
python -m codecollector projects register --project-root <path> [--project-name <name>] [--language python] ...
```

| Argument | Type | Required | Notes |
|---|---|---:|---|
| `--project-root` | string | yes | Абсолютный или относительный путь к корню проекта |
| `--project-name` | string | no | Если не указан, берется имя каталога |
| `--language` | string[] | no | Повторяемый аргумент, по умолчанию `python` |
| `--verification-command` | string[] | no | Команды для будущих проверок проекта |
| `--index-exclude` | string[] | no | Исключения для индексации |
| `--reference-library-path` | string[] | no | Пути к reference library |

Основной JSON-ответ: объект `RegisteredProject`.

---

### `projects onboard`

Назначение: строит индекс, knowledge overlay и переводит проект в `status=ready`.

```bash
python -m codecollector projects onboard --project-id <project_id> [--full]
```

| Argument | Type | Required | Notes |
|---|---|---:|---|
| `--project-id` | string | yes | Идентификатор зарегистрированного проекта |
| `--full` | flag | no | Принудительный full rebuild |

Основной JSON-ответ: объект `ProjectOnboardingResult`.

---

### `sessions analyze`

Назначение: создает session, строит shortlist кандидатов и `context summary` для `recommended target`.

```bash
python -m codecollector sessions analyze --project-id <id> --title <title> --description <text> [--constraint <c>] [--operation replace_symbol|insert_after_symbol]
```

| Argument | Type | Required | Notes |
|---|---|---:|---|
| `--project-id` | string | yes | Проект, в котором выполняется анализ |
| `--change-request-file` | path | no | Альтернатива `title/description` |
| `--title` | string | cond | Нужен вместе с `--description`, если нет structured file |
| `--description` | string | cond | Описание change request |
| `--constraint` | string[] | no | Повторяемое ограничение |
| `--note` | string[] | no | Дополнительные notes; используются при построении `ChangeRequest` |
| `--limit` | int | no | Размер shortlist |
| `--operation` | enum | no | `requested_operation`, по умолчанию `replace_symbol` |

Основной JSON-ответ: объект `AnalyzeSessionResult`.

---

### `sessions generate`

Назначение: запускает end-to-end pipeline для выбранной session.

```bash
python -m codecollector sessions generate --session-id <session_id> [--selected-qualname <qualname>] [--operation ...]
```

| Argument | Type | Required | Notes |
|---|---|---:|---|
| `--session-id` | string | yes | Идентификатор session |
| `--selected-qualname` | string | no | Переопределяет target на момент generate |
| `--operation` | enum | no | Явное `requested_operation`; обычно берется из session |
| `--limit` | int | no | Лимит для внутреннего shortlist, если он используется |
| `--disable-vector-search` | flag | no | Отключает vector search при поиске |

Основной JSON-ответ: wrapper-объект: metadata session + `pipeline_result` + `requested_operation`.

---

### `sessions repair`

Назначение: повторный repair для существующей session на базе предыдущего run result.

```bash
python -m codecollector sessions repair --session-id <session_id> [--selected-qualname <qualname>] [--note <text>]
```

| Argument | Type | Required | Notes |
|---|---|---:|---|
| `--session-id` | string | yes | Идентификатор session |
| `--selected-qualname` | string | no | Необязательное переопределение target |
| `--note` | string | no | Причина repair после review |
| `--disable-vector-search` | flag | no | Для совместимости CLI; в repair обычно не критично |

Основной JSON-ответ: wrapper-объект с полями `repair_outcome` / `repair_effective` и `pipeline_result`.

---

### `sessions finalize`

Назначение: закрывает session и, по умолчанию, удаляет последний workspace.

```bash
python -m codecollector sessions finalize --session-id <session_id> [--keep-workspace]
```

| Argument | Type | Required | Notes |
|---|---|---:|---|
| `--session-id` | string | yes | Идентификатор session |
| `--keep-workspace` | flag | no | Если указан, workspace не удаляется |

Основной JSON-ответ: короткий объект finalize result с вложенным `session`.

---

### `workspaces get` / `workspaces diff` / `workspaces apply`

Назначение: получение метаданных workspace, `unified diff` и применение изменений в `project root`.

```bash
python -m codecollector workspaces get --workspace-id <workspace_id>
python -m codecollector workspaces diff --workspace-id <workspace_id>
python -m codecollector workspaces apply --workspace-id <workspace_id>
```

| Argument | Type | Required | Notes |
|---|---|---:|---|
| `--workspace-id` | string | yes | Идентификатор staging workspace |

Основной JSON-ответ: разные объекты: `WorkspaceInfo`, `WorkspaceDiff`, `WorkspaceApplyResult`.

---

### `pipeline generate`

Назначение: прямой pipeline без sessions; удобен для локального smoke/regression testing.

```bash
python -m codecollector pipeline generate --project <path> --selected-qualname <qualname> --title <title> --description <text> [--operation ...]
```

| Argument | Type | Required | Notes |
|---|---|---:|---|
| `--project` | path | yes | Корень проекта |
| `--selected-qualname` | string | yes | Target symbol |
| `--operation` | enum | no | `replace_symbol` / `insert_after_symbol` |
| `--limit` | int | no | Размер shortlist при необходимости |
| `--change-request-file` | path | no | Альтернатива `title/description` |
| `--title` | string | cond | Нужен вместе с `--description` |
| `--description` | string | cond | Описание change request |
| `--constraint` | string[] | no | Повторяемые ограничения |
| `--note` | string[] | no | Дополнительные notes |
| `--disable-vector-search` | flag | no | Отключить vector search |

Основной JSON-ответ:
- при успехе — `PipelineRunResult payload`;
- при неуспехе — объект вида `{status:"failed", message, pipeline_result}`.

---

### `search` / `context` / `apply` / `index build`

Назначение: низкоуровневые технические команды для поиска, просмотра контекста, ручного apply и rebuild index.

```bash
python -m codecollector search search ...
python -m codecollector context context ...
python -m codecollector apply apply ...
python -m codecollector index build ...
```

| Команда | Required | Notes |
|---|---|---|
| `search --project/--query` | yes | Поиск кандидатов |
| `context --project/--qualname` | yes | Получение `ContextPack` |
| `apply --project/--qualname/--artifact-file` | yes | Ручное применение артефакта |
| `index build --project` | yes | Переиндексация проекта |

Основной JSON-ответ:
- `SearchCandidate[]`
- `ContextPack payload`
- `ApplyResult payload`
- `BuildReport`

---

## 4. Основные JSON-объекты

### 4.1 `RegisteredProject`

| Field | Type | Required | Description |
|---|---|---:|---|
| `project_id` | string | yes | Стабильный идентификатор проекта |
| `project_name` | string | yes | Человекочитаемое имя проекта |
| `project_root` | string | yes | Путь к корню проекта |
| `languages` | string[] | yes | Список языков проекта |
| `verification_commands` | string[] | yes | Команды верификации, если заданы |
| `index_excludes` | string[] | yes | Исключения для индексации |
| `reference_library_ids` | string[] | yes | Подключенные reference libraries по id |
| `reference_library_paths` | string[] | yes | Подключенные reference libraries по path |
| `status` | string | yes | `registered` / `ready` / ... |
| `created_at` | ISO datetime | yes | Время регистрации |
| `updated_at` | ISO datetime | yes | Последнее обновление записи |
| `knowledge_path` | string\|null | no | Файл knowledge overlay после onboarding |
| `knowledge_updated_at` | ISO datetime\|null | no | Когда knowledge последний раз обновлялся |

Пример:

```json
{
  "project_id": "proj-20260401T124114819654Z-107054",
  "project_name": "sample_python_app",
  "project_root": "/path/to/sample_python_app",
  "languages": ["python"],
  "verification_commands": [],
  "index_excludes": [],
  "reference_library_ids": [],
  "reference_library_paths": [],
  "status": "ready",
  "created_at": "2026-04-01T12:41:14.819511+00:00",
  "updated_at": "2026-04-03T15:56:45.571635+00:00",
  "knowledge_path": "/path/to/.codecollector/knowledge.yaml",
  "knowledge_updated_at": "2026-04-03T15:56:45.571000+00:00"
}
```

---

### 4.2 `AnalyzeSessionResult`

| Field | Type | Required | Description |
|---|---|---:|---|
| `session_id` | string | yes | Созданная session |
| `project_id` | string | yes | Проект анализа |
| `input_requirements` | Requirement[] | yes | Нормализованный список требований |
| `requested_operation` | string | yes | `replace_symbol` / `insert_after_symbol` |
| `candidates` | SearchCandidate[] | yes | Shortlist кандидатов |
| `recommended_target` | string\|null | yes | Лучший кандидат по ranking profile |
| `context_summary` | object\|null | yes | Короткий контекст для `recommended target` |

Пример:

```json
{
  "session_id": "sess-20260403T094049580788Z-724102",
  "project_id": "proj-20260401T124114819654Z-107054",
  "input_requirements": [
    {
      "title": "Изменить текст уведомления о назначении тикета",
      "description": "Сделать уведомление на русском языке и использовать формулировку успешно назначен сотруднику.",
      "constraints": [
        "Не менять внешний контракт API"
      ]
    }
  ],
  "requested_operation": "replace_symbol",
  "candidates": [
    {
      "qualname": "support_app.services.ticket_service.TicketService.assign_ticket",
      "score": 36.24
    }
  ],
  "recommended_target": "support_app.services.ticket_service.TicketService.assign_ticket",
  "context_summary": {
    "target": {
      "qualname": "support_app.services.ticket_service.TicketService.assign_ticket"
    },
    "recommended_tests": [
      "tests.test_ticket_service.test_assign_ticket_returns_message"
    ]
  }
}
```

---

### 4.3 `SearchCandidate`

| Field | Type | Required | Description |
|---|---|---:|---|
| `qualname` | string | yes | Полное квалифицированное имя symbol |
| `name` | string | yes | Короткое имя symbol |
| `kind` | string | yes | `module` / `class` / `function` / `method` |
| `file_path` | string | yes | Путь файла внутри проекта |
| `score` | number | yes | Итоговый ranking score |
| `confidence` | number | yes | Эвристическая confidence оценки |
| `relevance_category` | string | yes | Например: высокая / средняя / низкая |
| `reasons` | string[] | yes | Краткие причины ранжирования |
| `docstring` | string | yes | Docstring symbol |
| `knowledge_title` | string | yes | Title из knowledge-слоя, если найден |
| `requirements` | string[] | yes | Связанные requirement ids |

---

### 4.4 `Session`

| Field | Type | Required | Description |
|---|---|---:|---|
| `session_id` | string | yes | Идентификатор session |
| `project_id` | string | yes | Связанный проект |
| `input_requirements` | Requirement[] | yes | Исходные требования пользователя |
| `requested_operation` | string | yes | Операция по умолчанию для generate |
| `status` | string | yes | `analyzed` / `target_selected` / `generated` / `repaired` / `applied` / `finalized` / ... |
| `created_at` | ISO datetime | yes | Время создания session |
| `updated_at` | ISO datetime | yes | Время последнего изменения |
| `run_ids` | string[] | yes | История pipeline run ids |
| `workspace_ids` | string[] | yes | Активные или сохраненные workspace ids |
| `recommended_target` | string\|null | yes | Target, предложенный analyze |
| `selected_target` | string\|null | no | Target, подтвержденный пользователем |
| `last_run_id` | string\|null | no | Последний run |
| `last_workspace_id` | string\|null | no | Последний workspace |

Пример:

```json
{
  "session_id": "sess-20260403T143500545354Z-2bc957",
  "project_id": "proj-20260401T124114819654Z-107054",
  "input_requirements": [
    {
      "title": "Изменить текст уведомления о назначении тикета",
      "description": "...",
      "constraints": [
        "Не менять внешний контракт API"
      ]
    }
  ],
  "requested_operation": "replace_symbol",
  "status": "generated",
  "created_at": "2026-04-03T14:35:00.545483+00:00",
  "updated_at": "2026-04-03T15:56:45.571635+00:00",
  "run_ids": [
    "pipeline-20260403T154547.397300Z-7dd1d5"
  ],
  "workspace_ids": [
    "sample_python_app-20260403T155644.397413Z-125c02"
  ],
  "recommended_target": "support_app.services.ticket_service.TicketService.assign_ticket",
  "selected_target": "support_app.services.notification_service.build_assignment_message",
  "last_run_id": "pipeline-20260403T154547.397300Z-7dd1d5",
  "last_workspace_id": "sample_python_app-20260403T155644.397413Z-125c02"
}
```

---

### 4.5 `PipelineRunResult payload`

Это основной детализированный объект для `generate`, `repair` и `pipeline` команд.

Он специально остается подробным: содержит `build report`, `step-by-step trace`, `context`, внешние LLM-вызовы, `verification` и `merge plan`.

| Field | Type | Required | Description |
|---|---|---:|---|
| `run_id` | string | yes | Идентификатор pipeline run |
| `run_label` | string | yes | Timestamp label run |
| `run_dir` | string | yes | Каталог run artifacts |
| `change_request` | ChangeRequest | yes | Нормализованный запрос на изменение |
| `selected_target` | string | yes | Финальный target для run |
| `build_report` | object\|null | yes | Метрики build/index stage |
| `steps` | PipelineStepRecord[] | yes | Пошаговая телеметрия pipeline |
| `search_candidates` | SearchCandidate[] | yes | Shortlist кандидатов, если был поисковый шаг |
| `context_pack` | object\|null | yes | Расширенный контекст target |
| `external_code_generation` | ExternalGenerationCall\|null | yes | Запрос/ответ `codegenerator generate` |
| `external_test_generation` | ExternalGenerationCall\|null | yes | Запрос/ответ `codegenerator generate-test` |
| `repair_generation` | ExternalGenerationCall\|null | yes | Запрос/ответ `codegenerator repair` |
| `generated_test_apply` | object\|null | yes | Какие тесты были добавлены в workspace |
| `verification_report` | object\|null | yes | Результаты `ast` / `compile` / `pytest` и спецпроверок |
| `apply_result` | object\|null | yes | `diff` / `validation` / `impact` для workspace |
| `merge_plan` | MergePlan\|null | yes | Dry-run оценка готовности к merge |

Пример:

```json
{
  "run_id": "pipeline-20260403T154547.397300Z-7dd1d5",
  "selected_target": "support_app.services.notification_service.build_assignment_message",
  "steps": [
    {
      "step_name": "external_generate",
      "status": "ok"
    },
    {
      "step_name": "apply_staging",
      "status": "error"
    },
    {
      "step_name": "external_repair",
      "status": "ok"
    }
  ],
  "verification_report": {
    "passed": true
  },
  "merge_plan": {
    "mode": "dry_run",
    "ready_for_manual_merge_review": true
  }
}
```

---

### 4.6 `ExternalGenerationCall`

| Field | Type | Required | Description |
|---|---|---:|---|
| `mode` | string | yes | Обычно `cli_json` |
| `command` | string[] | yes | Фактическая команда запуска external generator |
| `request_path` | string | yes | Файл запроса |
| `result_path` | string | yes | Файл ответа |
| `trace_path` | string\|null | yes | Относительный trace path внутри generator |
| `request_payload` | object | yes | Полный JSON request, переданный generator |
| `result_payload` | object | yes | Полный JSON result от generator |

---

### 4.7 `ApplyResult` / `WorkspaceDiff` / `WorkspaceApplyResult`

| Field | Type | Required | Description |
|---|---|---:|---|
| `workspace_path` | string | yes | Путь к staging workspace |
| `diff.changed_files` | string[] | yes | Список измененных файлов |
| `diff.unified_diff` | string | yes | Unified diff текст |
| `validation.is_valid` | boolean | yes | Признак структурной валидности |
| `validation.issues` | ValidationIssue[] | yes | Проблемы / warnings / info |
| `impact` | ImpactSummary | yes | Измененные symbols, inbound callers, tests, requirements |

Пример:

```json
{
  "workspace_id": "sample_python_app-20260403T155644.397413Z-125c02",
  "changed_files": [
    "support_app/services/notification_service.py",
    "tests/test_generated_generate_test_build_assignment_message.py"
  ],
  "unified_diff": "--- before\n+++ after\n@@ ...",
  "validation": {
    "is_valid": true,
    "issues": [
      {
        "severity": "info",
        "check_name": "structural_review"
      }
    ]
  },
  "impact": {
    "recommended_tests": [
      "pytest tests/test_generated_generate_test_build_assignment_message.py",
      "pytest tests/test_ticket_service.py"
    ]
  }
}
```

---

### 4.8 `VerificationReport`

| Field | Type | Required | Description |
|---|---|---:|---|
| `passed` | boolean | yes | Итоговый статус проверки |
| `results.ast_parse` | object | no | Проверка parseability Python AST |
| `results.py_compile` | object | no | Результат `compileall` |
| `results.pytest_recommended` | object | no | Результат рекомендованных тестов |
| `results.repair_intent` | object | no | Спецпроверка `no-op repair` |
| `failure_summary.failed_checks` | string[] | yes | Список проваленных checks |
| `failure_summary.repairable` | boolean | yes | Можно ли пытаться repair повторно |
| `failure_summary.messages` | string[] | no | Краткие сообщения об ошибках |
| `failure_summary.stage` | string | no | Этап, где возникла проблема |

---

### 4.9 `MergePlan`

| Field | Type | Required | Description |
|---|---|---:|---|
| `mode` | string | yes | Сейчас `dry_run` |
| `ready_for_manual_merge_review` | boolean | yes | Готов ли run к ручному review/merge |
| `workspace_path` | string | yes | Итоговый workspace |
| `changed_files` | string[] | yes | Файлы для review |
| `symbols_in_changed_files` | string[] | yes | Символы в измененных файлах |
| `linked_requirements` | string[] | yes | Связанные requirement ids |
| `recommended_tests` | string[] | yes | Рекомендуемые тесты |
| `recommended_test_commands` | string[] | yes | Команды прогонов |
| `summary_lines` | string[] | yes | Готовый текстовый summary для человека |

---

## 5. Практические замечания для UI и будущего REST

- `requested_operation` уже является частью `session/analyze/generate` contract и должен быть сохранен в UI-модели как отдельное поле.
- `sessions` workflow и `pipeline` workflow возвращают близкие, но не одинаковые payloads. UI-слою удобно нормализовать их до общего `Run View Model`.
- `pipeline_result` нужно трактовать как основной объект телеметрии и диагностики. Краткий status-wrapper удобен только для верхнего уровня UX.
- `workspaces diff/apply` работают с последним staging state. Для REST это логично маппить в `/workspaces/{id}` и `/workspaces/{id}:apply`.
- UI должен ожидать длинные операции (`generate`, `repair`, `generate-test`) и показывать run-level state, а не только финальный boolean.

---

## 6. Команды и направления, которые можно ожидать в будущем

Ниже перечислены направления, которые упоминались в проектировании, но не входят в текущий CLI API и не должны использоваться как действующий контракт:

- REST API поверх текущих CLI/service contracts
- подключение и управление reference libraries через отдельный user-facing API
- более формальный schema contract для external generator и UI integration
- дополнительные operation types помимо `replace_symbol` и `insert_after_symbol`

---

## 7. Что важно считать текущим состоянием

На текущий момент для интеграции нужно опираться именно на следующие факты:

- базовые рабочие операции: `replace_symbol` и `insert_after_symbol`;
- `sessions` и `pipeline` — это два разных пользовательских сценария поверх одного orchestration-слоя;
- `PipelineRunResult` — основной диагностический объект;
- `merge_plan` всегда имеет смысл dry-run оценки, а не автоматического merge;
- внешний `codegenerator` является частью рабочего контура и его request/result входят в run payload как first-class данные.

---

CodeCollector CLI API • рабочая спецификация для интеграции
