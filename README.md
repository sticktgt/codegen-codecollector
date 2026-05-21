# codecollector

`codecollector` — технический оркестратор для управляемой доработки локального Python-проекта через анализ запроса, выбор места изменения, сбор контекста, вызов внешнего генератора кода, применение результата в staging workspace, проверки, repair и подготовку dry-run merge plan.

Проект работает как нижний слой общего решения. Он не занимается верхнеуровневой детализацией бизнес-требований и не является UI. Пользовательский workflow, CR и отображение результатов находятся во внешнем интерфейсе, например в `codeui`.

## Роль проекта

`codecollector` отвечает за:

- регистрацию локальных проектов;
- onboarding проекта из входной папки;
- построение graph/index по исходному коду;
- построение search documents;
- синхронизацию vector search;
- построение и обогащение `knowledge.yaml`;
- анализ пользовательского запроса;
- подбор target/anchor и operation;
- сбор context pack;
- формирование allowed API surface;
- формирование model surfaces;
- формирование contract context;
- формирование required/recommended contract context;
- вызов `codegenerator`;
- применение production artifact в staging workspace;
- статические и runtime-проверки;
- repair orchestration;
- generated-test generation и semantic validation;
- advisory review для generated-test failure;
- dry-run merge plan;
- применение workspace в основной проект после решения человека.

## Основные сущности

### Project

Проект — локальный каталог с исходным кодом. Внутри onboarding input ожидается папка `src/`. Архитектурный документ может присутствовать рядом с `src/` и использоваться для enrichment `knowledge.yaml`.

Один `project_root` не должен быть зарегистрирован повторно. Повторная регистрация возвращает структурированную ошибку duplicate root.

### Session

Session создается при `sessions analyze` и хранит:

- исходный CR;
- выбранный проект;
- requested operation;
- результат анализа;
- recommended target;
- selected target;
- связанные run ids;
- связанные workspace ids;
- последний run/workspace.

### Run

Run создается при `sessions generate` и хранится в `.runs`. Run содержит:

- входной change request;
- выбранный target;
- context pack;
- generation request/result;
- repair request/result, если repair был выполнен;
- generated-test request/result;
- generated-test review request/result, если review был выполнен;
- verification report;
- generated test apply status;
- merge plan;
- diff;
- steps timeline.

### Workspace

Workspace — staging-копия исходного проекта, куда применяется generated artifact. Workspace не применяется в основной проект автоматически. Применение выполняется отдельной командой после ручного review.

## Project onboarding

Команда:

```bash
python -m codecollector projects onboard \
  --input-root /path/to/project \
  --project-name example \
  --full
```

Ожидаемая структура:

```text
<input-root>/
  src/
  ARCHITECTURE.md или ARCHITECT.md
```

Во время onboarding:

1. Регистрируется проект.
2. Строится graph/index по `src`.
3. Формируются search documents.
4. Выполняется vector sync.
5. Создается базовый `knowledge.yaml`.
6. Если найден архитектурный документ, выполняется LLM-enrichment `knowledge.yaml`.
7. Если архитектурный документ не найден, onboarding продолжается без enrichment.
8. Если enrichment найденного архитектурного документа завершился ошибкой, onboarding считается неуспешным, регистрация и индексы откатываются.

## Reindex

Обычная переиндексация:

```bash
python -m codecollector projects reindex --project-id <project_id>
```

Полная переиндексация graph/index:

```bash
python -m codecollector projects reindex --project-id <project_id> --full
```

Текущая семантика:

- обычный reindex выполняет incremental build;
- `--full` выполняет полный graph rebuild;
- `--full` не означает обязательный force vector re-embedding;
- embeddings пересчитываются только при изменении search documents;
- если incremental reindex не нашел измененных или удаленных файлов, search document sync и vector sync пропускаются;
- текущий режим vector sync при изменениях — full project vector resync.

## Analyze

Команда:

```bash
python -m codecollector sessions analyze --project-id <project_id> --request-file <request.json>
```

Analyze выполняет:

- оценку качества запроса;
- выбор operation;
- выбор insert scope;
- поиск кандидатов;
- rerank кандидатов;
- рекомендацию target или anchor;
- формирование target role;
- формирование context summary;
- формирование подсказок по переиспользованию существующей проектной логики.

### Request quality

`request_quality.status` может быть:

- `processable` — запрос можно выполнять;
- `uncertain` — запрос можно выполнять после решения пользователя;
- `insufficient` — запрос недостаточен для генерации.

### Target role

`target_role` показывает роль выбранного symbol:

- `target` — изменяемый symbol;
- `anchor` — точка вставки;
- `parent_class` — класс, в который добавляется новый метод.

### Reuse existing logic

Analyze может вернуть поле `reuse_existing_logic`.

Назначение поля — подсказать generation/repair, какие существующие методы или проектные контракты стоит рассмотреть для переиспользования.

Пример структуры:

```json
{
  "reuse_existing_logic": {
    "mode": "recommended",
    "confidence": 0.82,
    "reason": "...",
    "contracts": [
      {
        "qualname": "note.note_storage.NoteStorage.find_note_file_by_id",
        "role": "same_class_helper",
        "reason": "..."
      }
    ]
  }
}
```

`reuse_existing_logic` является контекстной подсказкой. Само по себе это поле не превращается в hard `required_contracts`. Hard constraints передаются отдельно через required contracts.

## Context pack

Context pack содержит данные, которые нужны `codegenerator` для генерации и repair.

Основные элементы:

- target symbol;
- parent symbol;
- module outline;
- class members;
- same-class methods;
- related tests;
- recommended tests;
- related production symbols;
- inbound/outbound relations;
- contract context;
- allowed API surface;
- model surfaces;
- reference artifacts;
- reuse hints.

### Same-class methods

Для method-target `codecollector` передает видимые методы того же класса.

Цель — дать `codegenerator` доступ к уже существующим helper-методам класса, чтобы generated code не дублировал поведение и не придумывал новые методы.

Same-class methods передаются как контекст:

- имя метода;
- qualname;
- сигнатура;
- краткое описание;
- короткий source excerpt.

Same-class methods используются в generation и repair. Они не являются обязательными контрактами, если не переданы как required contracts.

## Allowed API Surface

Allowed API Surface — компактный список разрешенных вызовов для generated code.

Он строится консервативно:

- self-атрибуты берутся из видимого кода и `__init__`;
- методы зависимостей разрешаются только если они видимы в project context;
- вложенные access path добавляются только при наличии видимых примеров;
- free functions берутся из видимого project context;
- неизвестные методы dependency/helper не считаются допустимыми.

Allowed API Surface ограничивает проектные зависимости и внутренние проектные контракты. Он не запрещает стандартную библиотеку Python и обычные методы стандартных типов, если они нужны для задачи и не добавляют внешних зависимостей.

## Model surfaces

Model surfaces описывают видимые модели и их поля.

Для каждой модели могут передаваться:

- имя;
- qualname;
- поля;
- constructor fields;
- required constructor fields;
- типы полей, если они видимы;
- источник.

Model surfaces используются для:

- production generation;
- generated-test generation;
- проверки constructor keyword arguments;
- проверки неизвестных model attributes;
- проверки восстановления моделей из JSON/dict/файлов;
- проверки default behavior при отсутствующих serialized fields.

## Generation

Команда:

```bash
python -m codecollector sessions generate --session-id <session_id>
```

Основной pipeline:

1. Обновить индекс проекта.
2. Собрать context pack.
3. Подобрать reference artifacts.
4. Подготовить generation request.
5. Вызвать `codegenerator generate`.
6. Проверить generated artifact статическими правилами.
7. Применить artifact в staging workspace.
8. Выполнить `patch_static_semantics`.
9. При production failure вызвать repair.
10. Повторно применить repair artifact.
11. Повторно выполнить static semantics.
12. Сгенерировать generated test.
13. Проверить generated test semantic/relevance rules.
14. Применить generated test, если он прошел semantic checks.
15. Выполнить runtime verification.
16. При generated-test failure выполнить advisory review.
17. Подготовить merge plan.

## Repair

Repair вызывается, если production artifact не прошел применение, static semantics или runtime checks.

Repair request содержит:

- previous artifact;
- исходный target;
- error context;
- failed verification blocks;
- visible self attributes;
- visible self methods;
- same-class methods;
- suggested replacements;
- model surfaces;
- contract context;
- allowed API surface;
- full file excerpt, если доступен.

Repair должен исправлять previous artifact, а не генерировать новый сценарий с нуля.

## Verification report

`verification_report` содержит:

- `verdict`;
- `passed`;
- `blocks`;
- `summary`.

Каждый block содержит:

- `name`;
- `ok`;
- `severity`;
- `issues`;
- `details`.

Основные production checks:

- `patch_static_semantics`;
- `runtime_ast_parse`;
- `runtime_py_compile`;
- `runtime_ruff`, если включен;
- `runtime_pytest_recommended`;
- `runtime_pytest_full`, если включен.

Generated test checks:

- `generated_test_static_semantics`;
- `generated_test_relevance`;
- `runtime_pytest_recommended`, если failure относится к generated test.

## Semantic checks

`patch_static_semantics` проверяет generated production code перед merge review.

Текущие группы проверок:

- дубликаты symbols;
- обязательные project contract calls;
- число обязательных аргументов project contract calls;
- несовместимые типы аргументов project contract calls;
- неизвестные methods dependency/helper objects;
- неизвестные self-attributes;
- неизвестные self-methods;
- suggested replacements для похожих self-attributes/self-methods;
- неизвестные runtime names;
- неизвестные annotation names;
- неизвестные model attributes;
- неизвестные constructor keyword arguments;
- несовместимые типы constructor fields при восстановлении модели из serialized source;
- передача `None` в constructor field, если поле имеет visible default/default factory;
- небезопасный `__dict__` для dict-return;
- unknown contract result attributes;
- required class members для class replace;
- advisory warning о возможной потере поведения существующего public method.

Generated test static semantics проверяет:

- синтаксис;
- наличие test functions;
- наличие assert;
- отсутствие `mocker` и pytest-mock fixtures;
- отсутствие unresolved names;
- использование target symbol;
- constructor keyword arguments;
- использование project symbols только через видимый import или локальный fake/stub;
- соответствие expected values видимым типам model fields.

## Generated tests

Generated tests создаются внешним `codegenerator`, но применяются только после semantic validation в `codecollector`.

Если generated test не прошел static semantics или relevance check:

- он считается rejected;
- он не должен попадать в final workspace/merge;
- он не должен участвовать в runtime verification;
- `generated_test_apply.skipped` должен быть `true`;
- `generated_test_apply.verification_failed` должен быть `true`;
- `generated_test_apply.merge_recommended` должен быть `false`;
- `candidate_test_files`, `removed_files` и `excluded_files` используются для UI-диагностики.

Если generated test прошел semantic checks, он применяется во workspace и участвует в recommended runtime tests.

## Advisory review generated-test failure

Advisory review запускается, если production checks прошли, но generated test failed.

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

Review возвращает рекомендацию, но не принимает решение автоматически.

Основные verdict значения:

- `production_likely_ok_test_likely_bad`;
- `production_likely_bad_test_valid`;
- `both_uncertain`;
- `environment_or_import_issue`;
- `insufficient_context`.

## Merge plan

Merge plan формируется в режиме dry-run.

Он содержит:

- mode;
- ready_for_manual_merge_review;
- workspace path;
- changed files;
- symbols in changed files;
- linked requirements;
- recommended tests;
- recommended test commands;
- excluded files;
- summary lines.

`ready_for_manual_merge_review=true` означает, что результат готов к ручной проверке. Это не означает автоматическое применение.

## Apply workspace

Workspace применяется отдельной командой:

```bash
python -m codecollector workspaces apply --workspace-id <workspace_id>
```

Apply должен учитывать merge plan и исключать rejected generated tests.

## Статусы run/session

Основные статусы:

- `ready_for_merge_review` — production checks прошли, результат готов к ручному review;
- `generated_test_verification_failed` — production checks прошли, generated test failed или был отклонен;
- `verification_failed` — production checks или runtime production verification не прошли;
- `repair_verification_failed` — repair выполнен, но результат не прошел verification;
- `repair_no_effective_change` — repair не дал полезного изменения;
- `failed` — pipeline упал технически;
- `needs_user_decision` — требуется решение человека;
- `applied` — workspace применен.

`generated_test_verification_failed` не означает, что production-код корректен или некорректен автоматически. Это статус для ручного review.

## Trace и диагностика

Run artifacts находятся в `.runs`.

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
- stdout/stderr внешних вызовов;
- trace `codegenerator`.

Для анализа ошибок важны:

- steps timeline;
- verification report;
- diff;
- generated_test_apply;
- repair_generation;
- codegenerator trace prompt/result;
- stdout/stderr runtime checks.

## Конфигурация

Основной файл настроек — `config.yaml`.

Через конфигурацию задаются:

- пути хранения state/runs/workspaces;
- параметры индексации;
- параметры vector search;
- параметры reference library;
- команды и timeout внешнего `codegenerator`;
- включение runtime checks;
- настройки generated tests;
- лимиты контекста;
- параметры моделей и endpoints.

## Текущие ограничения

- Основной поддерживаемый язык — Python.
- Multi-file generation в рамках одного CR не является основной рабочей схемой.
- Generated tests имеют нестабильное качество и часто отклоняются semantic checks.
- Generated tests могут неверно создавать project classes через constructor kwargs.
- Reuse hints являются soft context, а не hard requirement.
- Частичный vector sync по измененным search documents не реализован.
- Некоторые связи между соседними методами класса требуют дальнейшего улучшения context selection.
- `ready_for_merge_review` требует ручного review.
- Структурный trace секций prompt пока ограничен; полный prompt доступен через trace `codegenerator`.

## Ближайшие доработки

- Улучшить generated-test generation для создания project classes по видимой сигнатуре конструктора.
- Добавить структурный trace prompt-секций: какие блоки вошли в prompt, какие были обрезаны, какие symbols реально попали в prompt.
- Улучшить отображение rejected generated tests в UI: candidate files, excluded files, removed files, reason.
- Улучшить выбор reuse contracts в analyze без превращения soft hints в hard required contracts без высокой уверенности.
- Добавить более точное определение required contracts для случаев явного переиспользования существующей логики.
- Снизить шум reference artifact retrieval по общим словам.
- Поддержать частичный vector sync по измененным search documents.
- Расширить поддержку языков и адаптеров вне Python.
- Добавить HTTP API поверх текущих CLI/JSON контрактов.
