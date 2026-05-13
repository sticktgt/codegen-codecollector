# Репозиторий codecollector

Этот файл предназначен для LLM-агентов и разработчиков, которые меняют код, конфигурацию, prompts или документацию `codecollector`. Он фиксирует текущее состояние проекта и правила сопровождения.

---

## Назначение проекта

`codecollector` — technical orchestrator нижнего уровня для controlled dry-run изменения кода в локальном проекте.

Проект отвечает за:

- регистрацию и onboarding локальных проектов;
- построение графового и поискового индекса проекта;
- автоматическую генерацию `.codecollector/knowledge.yaml`;
- поиск и shortlist target/anchor candidates;
- LLM-assisted analyze технического change request;
- выбор target для `replace_symbol` или anchor/container для `insert_after_symbol`;
- сбор context pack, `allowed_api_surface` и `contract_context`;
- подбор reference artifacts;
- подготовку structured request и вызов внешнего `codegenerator`;
- применение результата в staging workspace;
- project-level verification production-кода и generated tests;
- orchestration repair;
- подготовку dry-run merge plan;
- сохранение run artifacts в `.runs`.

`codecollector` не является верхнеуровневым planner-agent для бизнес-требований и не является web/control layer. UI, CR lifecycle и отображение результата относятся к `codeui`. Генерация production-кода, generated tests и repair artifact относится к `codegenerator`.

---

## Границы ответственности

### Делает codecollector

- Управляет lifecycle зарегистрированных проектов и session.
- Индексирует Python-код и строит graph/search/vector индексы.
- Строит и обновляет `.codecollector/knowledge.yaml` при onboarding.
- Анализирует технический запрос на изменение кода.
- Возвращает `request_quality`, operation, insert scope, candidates и target recommendation.
- Формирует project context для генератора.
- Вызывает `codegenerator` через файловый JSON-контракт.
- Применяет artifact только в staging workspace.
- Выполняет статические и runtime-проверки проекта.
- Различает production failures и generated-test failures.
- Готовит merge/apply plan для ручного review.

### Не делает codecollector

- Не редактирует requirements и CR как бизнес-сущности UI.
- Не хранит UI-состояние и не отображает run artifacts пользователю.
- Не генерирует production-код самостоятельно вместо `codegenerator`.
- Не переносит prompt assembly production/test/repair из `codegenerator`.
- Не принимает финальное решение о merge без человека.
- Не добавляет project-specific специальные случаи под demo-проекты.

---

## Основные файлы и зоны кода

- `config.yaml` — конфигурация индексации, поиска, LLM-assisted analyze, codegenerator, verification и onboarding.
- `codecollector/api/cli.py` — CLI-контракт.
- `codecollector/projects/` — регистрация проектов.
- `codecollector/onboarding/` — onboarding проекта и генерация knowledge.
- `codecollector/indexing/` — Python index builder и storage adapters.
- `codecollector/search/` — поиск кандидатов.
- `codecollector/analysis/` — LLM-assisted analyze и rerank.
- `codecollector/sessions/` — session-based workflow.
- `codecollector/context/` — context pack, related symbols/tests, contracts.
- `codecollector/external_codegen/` — подготовка request и вызов `codegenerator`.
- `codecollector/orchestration/` — pipeline, run artifacts, merge plan.
- `codecollector/validation/` — semantic checks и runtime verification.
- `codecollector/workspace/` — staging workspace, diff и apply.
- `codecollector/overlays/` — чтение knowledge overlay.
- `codecollector/prompts/` — prompt-шаблоны только для analyze/onboarding enrichment.
- `.runs/` — артефакты запусков pipeline.
- `.workspaces/` — staging workspaces.
- `.state/` — registry projects/sessions.

---

## Общие правила изменений

- Работай локально в сервисе, который отвечает за задачу.
- Не смешивай analyze, indexing, context, generation, validation и workspace logic без необходимости.
- Не добавляй скрытые ветки под конкретный demo/project/example.
- Все настраиваемые лимиты, thresholds и режимы должны приходить из `config.yaml`/`config.py`.
- Prompt-тексты должны храниться в шаблонах, а не в Python-коде.
- Новые prompt-шаблоны писать на русском языке.
- Ошибки внешних вызовов, LLM, subprocess и verification должны логироваться с достаточным контекстом для отладки.
- JSON-ответы CLI должны оставаться понятными для `codeui` и внешних клиентов. При ошибке CLI должен возвращать структурированный JSON payload, а не печатать traceback как основной результат.
- Если меняется публичный CLI/result contract, обновляй README.
- После каждого патча указывай измененные файлы и тестовые сценарии.

---

## Onboarding и knowledge.yaml

Onboarding отвечает за подключение проекта к `codecollector` и подготовку `.codecollector/knowledge.yaml`.

Текущий сценарий:

1. Проект регистрируется с `project_root`, указывающим на корень индексируемого кода.
2. `projects onboard --project-id ...` строит индекс и базовый knowledge из фактических модулей и symbols.
3. Для нового проекта можно передать папку onboarding-пакета через `projects onboard --input-root ...`. В этой папке ожидается `src/` — индексируемый код проекта. Архитектурное описание ищется рядом с `src/` по именам из `onboarding.knowledge.architecture_doc_names`, например `ARCHITECT.md` или `ARCHITECTURE.md`.
4. При наличии архитектурного файла codecollector вызывает LLM из собственного analysis endpoint и дополняет базовый `knowledge.yaml` архитектурной информацией. Если архитектурный файл не найден, onboarding продолжается без LLM-enrichment и пишет warning в лог.

Правила enrichment:

- Индекс и базовый knowledge строятся детерминированно по коду.
- Информация из архитектурного файла имеет приоритет над автоматически созданными заголовками, описаниями и слоями.
- LLM-enrichment не должен создавать реальные entries для modules/symbols, которых нет в индексе.
- Упоминания из архитектурного файла, не найденные в коде, сохраняются как diagnostics/warnings, а не маскируются под существующий код.
- Для `projects onboard --input-root ...` enrichment по найденному архитектурному файлу является обязательным, если пользователь явно не передал `--skip-architecture-enrichment`. При ошибке enrichment нужно откатить регистрацию нового проекта и индексные данные.
- Prompt enrichment должен сохранять валидность контекста: сначала передается полный список файлов, затем по возможности полный список symbols; сжатие уменьшает поля всего набора, а не выбирает произвольный top-N.
- Если валидный контекст не помещается в prompt budget даже после безопасного сжатия, нужно вернуть ошибку и предложить увеличить лимит или отключить enrichment для запуска.
- В логах должны быть размер prompt, trim steps, `num_predict` и usage LLM: prompt/output/total tokens, duration и `done_reason`.
- Trace/warnings enrichment должны помогать понять, какие данные были применены или отброшены. Диагностические trace-файлы LLM-enrichment нужно хранить в `.runs/knowledge_enrichment_traces/` проекта codecollector, а не внутри подключаемого проекта.

`knowledge.yaml` должен оставаться совместимым с `OverlayService`: ключи `project`, `modules`, `symbols`, `requirements`, `architecture.layers` не переименовывать без отдельной миграции.

---

## Удаление проекта

`projects delete` удаляет регистрацию проекта и очищает graph/vector данные по `project_root`. Очистка должна быть идемпотентной: отсутствие строк в базе не считается ошибкой. Дополнительный ключ удаления не вводится, даже если несколько регистраций используют один и тот же `project_root`.

## Analyze и target selection

`sessions analyze` может использовать два LLM-assisted этапа:

1. `search_plan` — оценка качества запроса, operation, expected new symbols и поисковый план;
2. `candidate_rerank` — выбор target/anchor по candidate cards.

Поддерживаемые операции:

- `replace_symbol` — выбранный symbol является изменяемым target;
- `insert_after_symbol` — выбранный symbol является anchor или parent container, новый symbol еще не существует.

Если запрос недостаточный, результат должен содержать:

- `request_quality.status=insufficient`;
- `manual_review_required=true`;
- `missing_information`;
- рекомендацию переписать запрос и заново выполнить analyze.

Для такого запроса generation должен быть заблокирован. `select-target` не используется как обход недостаточного запроса.

---

## Candidates и LLM rerank

Внутренний recall-набор может быть шире списка, возвращаемого наружу. Размеры управляются конфигурацией:

- `analysis.recall.max_recall_candidates`;
- `analysis.candidate_context.max_candidate_cards`;
- `analysis.result.max_candidates`.

Кандидаты, оцененные LLM, должны явно содержать признаки:

- `ranked_by_llm`;
- `llm_recommended`;
- `llm_rank`;
- `llm_reason`.

Если LLM rerank пропущен, поля должны явно отражать, что candidate не был оценен LLM.

---

## Context pack, Allowed API Surface и Contract context

`codecollector` отвечает за структурный отбор project context, но не за generation prompt `codegenerator`.

Context pack включает target/anchor, parent, class members, module outline, related tests, recommended tests, related production symbols, inbound/outbound relations, contract context, allowed API surface и reference artifacts.

`Allowed API Surface` строится консервативно:

- `self.x` из `__init__` учитывается, если тип понятен;
- методы dependency допускаются только если они видны в related symbols, graph index или source excerpts;
- цепочки вроде `self.service.repository` допускаются только при видимом подтверждении;
- неизвестные методы dependency не добавляются.

`Contract context` — это фактический project context, а не reference artifact. Он нужен для сигнатур, import path, source excerpts, проверки обязательных аргументов и подсказок repair.

---

## Вызов codegenerator

`codecollector` вызывает внешний `codegenerator` через structured JSON request.

Поддерживаемые режимы внешнего генератора:

- `generate`;
- `generate-test`;
- `repair`.

Правила:

- `codecollector` не должен генерировать production/test artifact самостоятельно.
- Для `repair` нужно сохранять исходные `operation`, `insert_scope`, `parent_qualname` и previous artifact.
- Для `generate-test` при `insert_after_symbol` объектом тестирования считается сгенерированный symbol, а выбранный target остается anchor/container.
- `import_changes` применяются на стороне codecollector при staging patch.

---

## Verification и статусы

Структурные проверки обязательны. Отчет должен быть понятен без чтения исходного кода.

Production checks включают AST/compile/runtime checks и `patch_static_semantics`.
Generated test checks включают `generated_test_static_semantics`, `generated_test_relevance` и runtime failure, если ошибка относится только к generated test.

Нужно различать статусы:

- `ready_for_merge_review` — результат готов к ручному review;
- `verification_failed` — ошибка production-кода или основных проверок;
- `generated_test_verification_failed` — production artifact может быть корректным, но generated test не прошел проверки;
- `repair_verification_failed`;
- `repair_no_effective_change`;
- `failed`;
- `needs_user_decision`;
- `applied`;
- `generated`;
- `incomplete`.

Если падает только generated test, production-код не должен описываться как некорректный.

---

## Блокировка generation

Если analyze вернул `request_quality.status=insufficient`, `sessions generate` должен возвращать business-state JSON:

- `generation_blocked=true`;
- `block_reason=insufficient_request`;
- `request_quality_status=insufficient`;
- `missing_information`;
- `recommended_action=rewrite_request_and_run_analyze_again`.

Pipeline не запускается для такой session.

---

## Run artifacts и диагностика

В `.runs/` должны сохраняться:

- generation/repair/test request и result;
- stderr/stdout внешних вызовов;
- итоговый `pipeline_run_*.json`;
- usage и timing;
- verification report;
- generated test diagnostics;
- merge plan;
- warnings и причины пропуска этапов.

В логах должны быть видны:

- выбранный target/anchor и его роль;
- operation, insert_scope и источники выбора;
- контекст и reference artifacts;
- prompt sizes, token usage и trim steps;
- command, cwd, duration и returncode subprocess;
- verification issues;
- причины repair и результат repair.

---

## Документация

Документация проекта должна:

- описывать только текущее состояние as-is;
- быть на русском языке;
- не содержать changelog и истории инкрементов;
- не ссылаться на предыдущие версии;
- фиксировать фактический CLI/result contract;
- оставаться понятной следующему разработчику или агенту.

README обновляется, если меняется пользовательский workflow, CLI, JSON-контракт, структура artifacts, onboarding или конфигурация.

---

## Что не делать

Не нужно:

- переносить обязанности `codeui` или `codegenerator` в `codecollector`;
- использовать `select-target` как исправление недостаточного запроса;
- подменять project context reference-контекстом;
- добавлять demo-specific rules;
- хранить лимиты и prompt-фрагменты в коде, если они должны быть настраиваемыми;
- описывать production-код как некорректный, если ошибка относится только к generated test;
- менять JSON-контракт без обновления интеграции и документации.
