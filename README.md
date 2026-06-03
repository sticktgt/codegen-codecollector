# codecollector

`codecollector` — технический оркестратор для управляемой доработки локального Python-проекта. Он принимает технический запрос на изменение, находит место изменения, собирает контекст, вызывает внешний генератор кода, применяет результат в рабочей области проверки, выполняет проверки, запускает repair при ошибках и готовит план ручного merge review.

Проект является нижним слоем общего решения. Пользовательский интерфейс, жизненный цикл CR и отображение результатов относятся к `codeui`. Генерация production-кода, generated tests и repair-артефактов относится к `codegenerator`.

## Назначение

`codecollector` выполняет следующие задачи:

- регистрирует локальные проекты;
- выполняет onboarding проекта из входной папки;
- строит графовый индекс исходного кода;
- строит поисковые документы;
- синхронизирует векторный поиск;
- создает и обогащает `.codecollector/knowledge.yaml`;
- анализирует технический запрос на изменение;
- выбирает операцию изменения;
- выбирает изменяемый symbol или место вставки;
- собирает context pack для внешнего генератора;
- формирует Allowed API Surface;
- формирует model surfaces;
- формирует contract context;
- вызывает `codegenerator`;
- применяет generated artifact в рабочую область проверки;
- выполняет статические и runtime-проверки;
- запускает repair при ошибках production-кода;
- формирует generated test и проверяет его;
- выполняет advisory review при ошибке generated test;
- формирует dry-run merge plan;
- применяет выбранную рабочую область в основной проект после ручного решения.

`codecollector` не уточняет бизнес-требования вместо пользователя, не является интерфейсом пользователя и не генерирует код самостоятельно вместо `codegenerator`.

## Основные сущности

### Проект

Проект — локальный каталог с исходным кодом. При onboarding входная папка содержит каталог `src/`, который становится корнем индексируемого кода. Рядом с `src/` может находиться архитектурное описание проекта.

Один `project_root` регистрируется один раз. Повторная регистрация того же корня возвращает структурированную ошибку duplicate root.

### Session

Session создается командой `sessions analyze`. Она хранит исходный запрос, проект, выбранную операцию, результат анализа, рекомендованный target, выбранный target, связанные run ids, workspace ids и последний run/workspace.

### Run

Run создается командой `sessions generate` и сохраняется в `.runs`. Он содержит исходный запрос, выбранный target, context pack, запросы и ответы `codegenerator`, verification report, generated test diagnostics, diff, merge plan и timeline шагов.

### Workspace

Workspace — рабочая область проверки, куда применяется generated artifact. Workspace не применяется в основной проект автоматически. Применение выполняется отдельной командой после ручного review.

Workspace отражает состояние проекта на момент создания run. Перед применением пользователь проверяет merge plan, changed files, diff и список excluded files.

## Onboarding проекта

Команда:

```bash
python -m codecollector projects onboard \
  --input-root /path/to/project \
  --project-name example \
  --full
```

Ожидаемая структура входной папки:

```text
<input-root>/
  src/
  ARCHITECTURE.md
```

Допустимое имя архитектурного файла также задается конфигурацией, например `ARCHITECT.md`.

Во время onboarding выполняются шаги:

1. Регистрация проекта.
2. Построение графового индекса по `src/`.
3. Построение search documents.
4. Синхронизация vector search.
5. Создание базового `knowledge.yaml`.
6. Обогащение `knowledge.yaml` по архитектурному документу, если он найден.
7. Сохранение diagnostics и warnings по enrichment.

Если архитектурный документ не найден, onboarding продолжается без enrichment. Если архитектурный документ найден, но enrichment завершился ошибкой, onboarding считается неуспешным, а регистрация и индексы откатываются.

## Переиндексация

Обычная переиндексация:

```bash
python -m codecollector projects reindex --project-id <project_id>
```

Полная переиндексация графового индекса:

```bash
python -m codecollector projects reindex --project-id <project_id> --full
```

Обычный reindex выполняет incremental build. Режим `--full` выполняет полный rebuild графового индекса. Embeddings пересчитываются при изменении search documents. Если incremental reindex не находит измененных или удаленных файлов, sync поисковых документов и vector sync пропускаются.

## Analyze

Команда:

```bash
python -m codecollector sessions analyze \
  --project-id <project_id> \
  --request-file <request.json>
```

Analyze выполняет:

1. Оценку качества запроса.
2. Определение operation.
3. Определение insert scope.
4. Поиск кандидатов.
5. Rerank кандидатов.
6. Выбор target или anchor.
7. Формирование context summary.
8. Формирование reuse hints.

Пример запроса:

```json
{
  "title": "Сделать автосохранение безопасным",
  "description": "Заменить существующий метод auto_save так, чтобы он сохранял заметку только при наличии несохранённых изменений.",
  "constraints": [
    "Это замена существующего метода auto_save.",
    "Использовать существующий признак несохранённых изменений.",
    "Использовать существующее действие сохранения заметки.",
    "Не добавлять новый метод."
  ]
}
```

### Качество запроса

`request_quality.status` принимает значения:

- `processable` — запрос можно выполнять;
- `uncertain` — требуется решение пользователя;
- `insufficient` — запрос недостаточен для генерации.

Если запрос недостаточен, generation блокируется. Ответ содержит `generation_blocked=true`, `block_reason=insufficient_request`, `missing_information` и рекомендацию переписать запрос.

### Операции

Поддерживаемые операции:

- `replace_symbol` — выбранный symbol является изменяемой целью;
- `insert_after_symbol` — выбранный symbol является anchor или parent container для нового symbol.

Для `replace_symbol` target является существующим symbol. Для `insert_after_symbol` target является точкой вставки, а новый symbol создается генератором.

### Кандидаты и rerank

Analyze возвращает кандидатов из recall-набора. Размеры набора управляются конфигурацией:

- `analysis.recall.max_recall_candidates`;
- `analysis.candidate_context.max_candidate_cards`;
- `analysis.result.max_candidates`.

Кандидат, оцененный rerank-моделью, содержит поля:

- `ranked_by_llm`;
- `llm_recommended`;
- `llm_rank`;
- `llm_reason`.

Если target выбран точным совпадением symbol из запроса, результат содержит `operation_source` или post-processing блок с причиной выбора.

## Context pack

Context pack — структурный контекст для `codegenerator`.

Он включает:

- target или anchor;
- parent symbol;
- module outline;
- class members;
- same-class methods;
- related tests;
- recommended tests;
- related production symbols;
- inbound и outbound relations;
- contract context;
- Allowed API Surface;
- model surfaces;
- reference artifacts;
- reuse hints.

### Same-class methods

Для method-target `codecollector` передает видимые методы того же класса. Это дает генератору доступ к уже существующим действиям и helper-методам класса.

Same-class methods передаются как контекст. Они становятся обязательными только если отдельно переданы как required contracts.

### Allowed API Surface

Allowed API Surface — компактный список разрешенных вызовов для generated code.

Он строится консервативно:

- self-атрибуты берутся из видимого кода и `__init__`;
- типизированные self-атрибуты учитываются, если тип понятен;
- методы зависимостей допускаются только если они видимы в related symbols, graph index или source excerpts;
- цепочки access path допускаются только при видимом подтверждении;
- unknown dependency methods не считаются допустимыми;
- standard library и обычные методы стандартных типов не запрещаются, если они не добавляют внешних зависимостей и нужны для задачи.

Пример Allowed API Surface:

```json
{
  "dependencies": [
    {
      "access_path": "self.storage",
      "type_name": "NoteStorage",
      "allowed_methods": [
        {
          "name": "save",
          "signature": "def save(self, note: Note) -> str:",
          "qualname": "note.note_storage.NoteStorage.save"
        }
      ]
    }
  ],
  "free_functions": []
}
```

### Model surfaces

Model surfaces описывают видимые модели и их поля:

- имя;
- qualname;
- fields;
- constructor fields;
- required constructor fields;
- типы полей, если они видимы;
- источник.

Model surfaces используются при generation, repair, generated-test generation и semantic checks.

### Contract context

Contract context содержит фактический project context: сигнатуры, import path, source excerpts, обязательные аргументы и видимые project contracts. Он не является reference artifact.

## Generation

Команда:

```bash
python -m codecollector sessions generate --session-id <session_id>
```

Pipeline выполняет шаги:

1. Обновление индекса проекта.
2. Сбор context pack.
3. Подбор reference artifacts.
4. Подготовка generation request.
5. Вызов `codegenerator generate`.
6. Проверка generated artifact.
7. Применение artifact в workspace.
8. Выполнение `patch_static_semantics`.
9. Repair при production failure.
10. Повторная проверка после repair.
11. Генерация generated test.
12. Проверка generated test.
13. Применение generated test, если он прошел проверки.
14. Runtime verification.
15. Advisory review при generated-test failure.
16. Подготовка merge plan.

## Repair

Repair запускается, если production artifact не прошел применение, static semantics или runtime checks.

Repair request содержит:

- previous artifact;
- исходные operation, insert scope и target;
- error context;
- failed verification blocks;
- visible self attributes;
- visible self methods;
- same-class methods;
- suggested replacements;
- model surfaces;
- contract context;
- Allowed API Surface;
- full file excerpt, если он доступен.

Repair исправляет previous artifact. Он не заменяет исходный CR новым сценарием.

## Verification

`verification_report` содержит:

- `verdict`;
- `passed`;
- `blocks`;
- `summary`.

Production checks включают:

- `patch_static_semantics`;
- `runtime_ast_parse`;
- `runtime_py_compile`;
- `runtime_ruff`, если он включен;
- `runtime_pytest_recommended`;
- `runtime_pytest_full`, если он включен.

Generated test checks включают:

- `generated_test_static_semantics`;
- `generated_test_relevance`;
- runtime failure diagnostics, если ошибка относится только к generated test.

### Semantic checks

`patch_static_semantics` проверяет production artifact перед merge review.

Проверяются:

- дубликаты symbols;
- обязательные project contract calls;
- число обязательных аргументов project contract calls;
- типы аргументов project contract calls;
- unknown dependency methods;
- unknown self attributes;
- unknown self methods;
- suggested replacements для похожих self attributes и self methods;
- unknown runtime names;
- unknown annotation names;
- unknown model attributes;
- unknown constructor keyword arguments;
- типы constructor fields при восстановлении модели из serialized source;
- передача `None` в constructor field, если поле не допускает `None`;
- небезопасный `__dict__`;
- unknown contract result attributes;
- required class members для class replace;
- advisory warning о возможной потере поведения public method.

Generated test static semantics проверяет:

- синтаксис;
- наличие test functions;
- наличие assert;
- отсутствие `mocker` и pytest-mock fixtures;
- отсутствие unresolved names;
- использование target symbol;
- constructor keyword arguments;
- использование project symbols через видимый import или локальный fake/stub;
- соответствие expected values видимым типам model fields.

## Generated tests

Generated tests создаются внешним `codegenerator` и применяются только после semantic validation.

Если generated test не прошел проверки:

- он считается rejected;
- он не попадает в final workspace;
- он не участвует в runtime verification;
- `generated_test_apply.skipped=true`;
- `generated_test_apply.verification_failed=true`;
- `generated_test_apply.merge_recommended=false`;
- rejected files отображаются через candidate, removed и excluded files.

Если generated test прошел проверки, он применяется во workspace и участвует в recommended runtime tests.

## Advisory review generated-test failure

Advisory review запускается, если production checks прошли, а generated test не прошел проверки.

Review context содержит:

- исходный CR;
- target;
- production artifact;
- production diff;
- generated test artifact;
- failed verification blocks;
- issues;
- stderr/stdout/traceback excerpts;
- advisory warnings.

Verdict значения:

- `production_likely_ok_test_likely_bad`;
- `production_likely_bad_test_valid`;
- `both_uncertain`;
- `environment_or_import_issue`;
- `insufficient_context`.

Review возвращает рекомендацию. Решение о merge принимает человек.

## Merge plan

Merge plan формируется в режиме dry-run.

Он содержит:

- mode;
- `ready_for_manual_merge_review`;
- workspace path;
- changed files;
- symbols in changed files;
- linked requirements;
- recommended tests;
- recommended test commands;
- excluded files;
- summary lines.

`ready_for_manual_merge_review=true` означает готовность результата к ручной проверке. Это не автоматическое применение.

## Apply workspace

Команда:

```bash
python -m codecollector workspaces apply --workspace-id <workspace_id>
```

Apply применяет выбранный workspace в основной проект. Команда учитывает merge plan и исключает rejected generated tests.

Перед apply пользователь проверяет:

- workspace id;
- run id;
- changed files;
- diff;
- excluded files;
- recommended tests;
- статус run.

## Статусы

Основные статусы run/session:

- `ready_for_merge_review` — production checks прошли, результат готов к ручному review;
- `generated_test_verification_failed` — production checks прошли, generated test failed или был отклонен;
- `verification_failed` — production checks или runtime production verification не прошли;
- `repair_verification_failed` — repair выполнен, но результат не прошел verification;
- `repair_no_effective_change` — repair не дал полезного изменения;
- `failed` — pipeline упал технически;
- `needs_user_decision` — требуется решение человека;
- `applied` — workspace применен.

`generated_test_verification_failed` не означает автоматическую ошибку production-кода. Этот статус требует ручного review результата.

## Run artifacts и диагностика

Run artifacts сохраняются в `.runs`.

Основные файлы:

- `pipeline_run_*.json`;
- `generation_request.json`;
- `generation_result.json`;
- `repair_request.json`;
- `repair_result.json`;
- `generation_test_request.json`;
- `generation_test_result.json`;
- `generated_test_review_request.json`;
- `generated_test_review_result.json`;
- stdout и stderr внешних вызовов;
- trace `codegenerator`;
- verification report;
- merge plan.

Для анализа ошибок используются:

- steps timeline;
- verification report;
- diff;
- generated_test_apply;
- repair_generation;
- prompts и results из trace `codegenerator`;
- stdout/stderr runtime checks.

## Конфигурация

Основной файл настроек — `config.yaml`.

Через конфигурацию задаются:

- пути state, runs и workspaces;
- параметры индексации;
- параметры search documents;
- параметры vector search;
- параметры reference library;
- команды и timeout внешнего `codegenerator`;
- runtime checks;
- настройки generated tests;
- лимиты контекста;
- параметры моделей и endpoints.

## Текущие ограничения

- Основной поддерживаемый язык проекта — Python.
- Основная рабочая схема — один CR для одного основного symbol.
- Multi-file generation в рамках одного CR не является основным режимом.
- Generated tests проходят semantic validation перед применением и могут быть отклонены.
- Reuse hints являются soft context, если они не переданы как required contracts.
- Partial vector sync по отдельным search documents не используется как основной режим.
- `ready_for_merge_review` требует ручной проверки.
