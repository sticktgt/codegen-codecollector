# AGENTS.md для codecollector

Этот файл предназначен для LLM-агентов и разработчиков, которые меняют код, конфигурацию, prompt-шаблоны или документацию `codecollector`.

Документ фиксирует текущее состояние проекта и правила сопровождения. Все изменения должны сохранять границы ответственности `codecollector`.

## Роль проекта

`codecollector` — технический оркестратор нижнего уровня для управляемого изменения локального Python-проекта.

Проект отвечает за:

- регистрацию проектов;
- onboarding проектов;
- построение graph/search/vector индексов;
- создание и обновление `.codecollector/knowledge.yaml`;
- анализ технического запроса;
- поиск и rerank target/anchor candidates;
- выбор target для `replace_symbol`;
- выбор anchor или parent container для `insert_after_symbol`;
- сбор context pack;
- сбор Allowed API Surface;
- сбор contract context;
- подготовку JSON-запросов для `codegenerator`;
- применение generated artifact в workspace;
- production verification;
- generated-test verification;
- repair orchestration;
- advisory review для generated-test failure;
- подготовку dry-run merge plan;
- применение workspace после ручного решения.

`codecollector` не является пользовательским интерфейсом, не управляет CR как UI-сущностями и не генерирует production-код самостоятельно. Эти обязанности относятся к `codeui` и `codegenerator`.

## Границы ответственности

### `codecollector` делает

- Управляет lifecycle проектов, sessions, runs и workspaces.
- Индексирует Python-код.
- Строит context pack для генератора.
- Вызывает внешний `codegenerator`.
- Выполняет проверки результата.
- Различает production failures и generated-test failures.
- Готовит merge plan для ручного review.

### `codecollector` не делает

- Не переносит в себя генерацию production/test/repair кода.
- Не собирает prompt production/test/repair вместо `codegenerator`.
- Не принимает автоматическое решение о merge.
- Не добавляет правила под конкретный demo-проект.
- Не подменяет project context reference artifacts.
- Не использует `select-target` как обход недостаточного запроса.

## Основные зоны кода

- `config.yaml` — настройки индексации, поиска, моделей, генератора, проверки и onboarding.
- `codecollector/api/cli.py` — CLI-контракт.
- `codecollector/projects/` — регистрация и удаление проектов.
- `codecollector/onboarding/` — onboarding и knowledge enrichment.
- `codecollector/indexing/` — построение индекса.
- `codecollector/search/` — поиск кандидатов.
- `codecollector/analysis/` — analyze, search plan, rerank.
- `codecollector/sessions/` — workflow session.
- `codecollector/context/` — context pack, related symbols, tests и contracts.
- `codecollector/external_codegen/` — JSON-контракт с `codegenerator`.
- `codecollector/orchestration/` — pipeline, run artifacts, merge plan.
- `codecollector/validation/` — semantic checks и runtime verification.
- `codecollector/workspace/` — staging workspace, diff и apply.
- `codecollector/overlays/` — чтение knowledge overlay.
- `codecollector/prompts/` — prompt-шаблоны для analyze и onboarding enrichment.
- `.runs/` — артефакты запусков.
- `.workspaces/` — рабочие области проверки.
- `.state/` — registry проектов и sessions.

## Общие правила изменений

- Меняй код в зоне, которая отвечает за задачу.
- Не смешивай analyze, indexing, context, generation, validation и workspace logic без необходимости.
- Не добавляй project-specific и demo-specific ветки.
- Настраиваемые лимиты, thresholds и режимы должны приходить из конфигурации.
- Prompt-тексты должны храниться в шаблонах.
- Новые prompt-шаблоны писать на русском языке.
- Ошибки внешних вызовов, subprocess, моделей и verification логировать со структурным контекстом.
- CLI при ошибке возвращает структурированный JSON payload.
- Если меняется публичный CLI/result contract, обновляй README.
- После каждого патча указывай измененные файлы и тестовые сценарии.

## Документация

Документация проекта должна:

- быть на русском языке;
- описывать только актуальное состояние проекта;
- не содержать changelog;
- не описывать историю изменений;
- не ссылаться на предыдущие версии;
- фиксировать фактический workflow, CLI, JSON-контракт, статусы, artifacts и ограничения;
- быть понятной разработчику, агенту и аналитику.

README обновляется при изменении пользовательского workflow, CLI, JSON-контракта, статусов, структуры artifacts, onboarding, конфигурации или apply-поведения.

## Onboarding и knowledge

Onboarding подключает проект к `codecollector` и создает `.codecollector/knowledge.yaml`.

Правила:

- Индекс и базовый knowledge строятся по фактическому коду.
- Архитектурный документ используется для enrichment, если он найден.
- Enrichment не должен создавать modules или symbols, которых нет в индексе.
- Упоминания из архитектурного документа, не найденные в коде, сохраняются как diagnostics или warnings.
- При ошибке обязательного enrichment регистрация и индексы нового проекта откатываются.
- Trace enrichment сохраняется в `.runs/knowledge_enrichment_traces/` проекта `codecollector`.

Ключи `project`, `modules`, `symbols`, `requirements`, `architecture.layers` в `knowledge.yaml` не переименовываются без миграции.

## Analyze и target selection

`sessions analyze` использует два модельных этапа:

1. `search_plan` — качество запроса, operation, expected symbols и план поиска.
2. `candidate_rerank` — выбор target, anchor или parent container.

Поддерживаемые операции:

- `replace_symbol`;
- `insert_after_symbol`.

Если запрос недостаточный, результат содержит:

- `request_quality.status=insufficient`;
- `manual_review_required=true`;
- `missing_information`;
- рекомендацию переписать запрос.

Generation для такого результата блокируется.

Кандидаты, оцененные rerank, должны содержать:

- `ranked_by_llm`;
- `llm_recommended`;
- `llm_rank`;
- `llm_reason`.

Если target выбран точным совпадением symbol из запроса, результат должен явно содержать источник и причину выбора.

## Context pack

`codecollector` отвечает за структурный отбор project context. Prompt production/test/repair собирается в `codegenerator`.

Context pack включает:

- target или anchor;
- parent symbol;
- module outline;
- class members;
- same-class methods;
- related tests;
- recommended tests;
- related production symbols;
- inbound/outbound relations;
- contract context;
- Allowed API Surface;
- model surfaces;
- reference artifacts;
- reuse hints.

Allowed API Surface строится консервативно:

- self-атрибуты берутся из видимого кода и `__init__`;
- методы dependency допускаются только при видимом подтверждении;
- цепочки access path допускаются только при видимом примере;
- неизвестные методы dependency не добавляются;
- standard library не запрещается самим фактом отсутствия в Allowed API Surface.

Contract context содержит фактические project contracts: import path, signatures, source excerpts и обязательные аргументы.

## Вызов codegenerator

`codecollector` вызывает `codegenerator` через structured JSON request.

Поддерживаемые режимы:

- `generate`;
- `generate-test`;
- `repair`.

Правила:

- `codecollector` не генерирует artifacts самостоятельно.
- `repair` получает previous artifact и исходные operation/target.
- Для `generate-test` при `insert_after_symbol` объектом тестирования считается сгенерированный symbol.
- `import_changes` применяются на стороне `codecollector` при staging patch.

## Verification

Production checks включают:

- AST/compile/runtime checks;
- `patch_static_semantics`;
- recommended tests;
- full tests, если они включены.

Generated test checks включают:

- `generated_test_static_semantics`;
- `generated_test_relevance`;
- runtime diagnostics, если ошибка относится к generated test.

Если падает только generated test, production artifact не описывается как некорректный без отдельного production-риска.

## Статусы

Основные статусы:

- `ready_for_merge_review`;
- `generated_test_verification_failed`;
- `verification_failed`;
- `repair_verification_failed`;
- `repair_no_effective_change`;
- `failed`;
- `needs_user_decision`;
- `applied`;
- `generated`;
- `incomplete`.

`ready_for_merge_review` означает готовность к ручной проверке, а не автоматическое применение.

## Workspace и apply

Generated artifact применяется в staging workspace. Workspace применяется в основной проект только отдельной командой.

Перед apply нужно проверить:

- workspace id;
- run id;
- changed files;
- diff;
- excluded files;
- generated test apply status;
- recommended tests;
- статус run.

Rejected generated tests не должны попадать в final workspace/merge.

## Run artifacts

В `.runs/` сохраняются:

- generation request/result;
- repair request/result;
- generated-test request/result;
- generated-test review request/result;
- stdout/stderr внешних вызовов;
- `pipeline_run_*.json`;
- usage и timing;
- verification report;
- merge plan;
- warnings и причины пропуска этапов.

Логи должны показывать target/anchor, operation, insert scope, prompt sizes, usage, trim steps, cwd, command, returncode, verification issues и repair result.

## Ограничения текущего режима

- Основной поддерживаемый язык проекта — Python.
- Основной сценарий — один CR для одного основного symbol.
- Multi-file generation не является основным режимом.
- Generated tests проходят semantic validation и могут быть отклонены.
- Reuse hints являются soft context, если они не переданы как required contracts.
- `ready_for_merge_review` требует ручной проверки.

## Что не делать

Не нужно:

- переносить обязанности `codeui` или `codegenerator` в `codecollector`;
- добавлять скрытые ветки под конкретный проект;
- хранить настраиваемые лимиты в коде;
- описывать production-код как ошибочный только из-за generated-test failure;
- менять JSON-контракт без обновления интеграции и документации;
- применять workspace без проверки merge plan и diff.
