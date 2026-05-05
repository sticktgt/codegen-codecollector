# Репозиторий codecollector

Этот файл фиксирует правила сопровождения текущего состояния `codecollector`. Он предназначен для разработчиков и агентов, которые меняют код, конфигурацию, prompts или документацию проекта.

---

## Назначение проекта

`codecollector` отвечает за orchestration dry-run процесса изменения кода:

- индексацию проекта;
- поиск релевантных символов;
- LLM-assisted analyze;
- выбор target для `replace_symbol` или anchor для `insert_after_symbol`;
- сбор context pack;
- подбор reference artifacts;
- подготовку запросов для внешнего `codegenerator`;
- применение результата в staging workspace;
- верификацию production-кода и сгенерированных тестов;
- подготовку dry-run результата для ручного review;
- сохранение run artifacts.

---

## Основные принципы изменений

При изменении проекта нужно сохранять:

- простую и понятную orchestration-логику;
- предсказуемое поведение CLI;
- явное разделение этапов analyze, selection, generation, apply, verification и merge dry-run;
- устойчивую структуру run artifacts;
- локальность изменений в сервисах;
- читаемость логов и итоговых JSON-ответов.

Не нужно объединять несвязанные обязанности в одном сервисе. Бизнес-решения не должны переноситься в низкоуровневые утилиты. Новые абстракции добавляются только тогда, когда они упрощают текущий код или закрывают актуальный сценарий.

Скрытые специальные случаи для конкретных demo-проектов не допускаются.

---

## Analyze и LLM-assisted выбор

`sessions analyze` может использовать два LLM-assisted этапа:

1. `search_plan` — оценка качества запроса, определение operation, ожидаемых новых symbols и поискового плана;
2. `candidate_rerank` — выбор target или anchor по candidate cards.

Операция может быть передана пользователем или определена автоматически. Явный пользовательский выбор имеет приоритет.

Поддерживаются операции:

- `replace_symbol` — выбранный symbol является target для замены;
- `insert_after_symbol` — выбранный symbol является anchor для вставки нового кода.

Если запрос недостаточный, результат должен содержать `request_quality.status=insufficient`, `manual_review_required=true` и список `missing_information`. Для такого запроса генерация должна быть заблокирована. Правильное действие пользователя — переписать запрос и заново выполнить analyze.

`select-target` не используется для исправления недостаточного запроса. Он нужен для ручного выбора target или anchor в рамках достаточно конкретного запроса.

Prompt-тексты для analyze должны храниться в `codecollector/prompts`. Числовые лимиты, prompt budget и threshold должны храниться в `config.yaml`.

Подключение analyze к Ollama-compatible endpoint задается локально в `codecollector/config.yaml`. Настройки LLM не наследуются из `codegenerator`.

Новые эвристики определения operation не должны становиться основным механизмом выбора. Эвристики допустимы только как fallback при недоступности или неуверенности LLM.

---

## Кандидаты analyze

Внутренний recall-набор может быть шире списка, возвращаемого пользователю. Размеры управляются настройками:

- `analysis.recall.max_recall_candidates`;
- `analysis.candidate_context.max_candidate_cards`;
- `analysis.result.max_candidates`.

В LLM rerank передаются расширенные candidate cards. Через API возвращается top-N список для выбора пользователем.

Кандидаты, оцененные LLM, должны получать поля:

- `ranked_by_llm`;
- `llm_recommended`;
- `llm_rank`;
- `llm_reason`.

Если LLM rerank пропущен, эти поля должны явно отражать, что кандидат не был оценен LLM.

---

## Prompt budget и логирование analyze

Analyze должен логировать и возвращать в JSON:

- количество project files и project symbols в search plan context;
- размеры prompt по каждому LLM-этапу;
- `max_prompt_chars` для этапа;
- `trim_steps`;
- количество recall candidates;
- количество candidate cards;
- `prompt_tokens`;
- `output_tokens`;
- `total_tokens`;
- `duration_sec`;
- итоговое `analysis_usage`.

Если prompt превышает лимит, допустимо структурно уменьшать project map или candidate cards. Уменьшение должно быть отражено в `trim_steps`.

---

## Сбор контекста и prompt для codegenerator

`codecollector` отвечает за структурный отбор контекста, а не за низкоуровневое символьное сжатие prompt.

Допустимые способы уменьшения контекста:

- ограничить количество related tests;
- ограничить количество reference artifacts;
- не включать полный файл, если достаточно target symbol;
- включать полный файл для `generate-test`, когда это нужно для импортов и проверки теста;
- передавать ограниченный reference-контекст в `generate-test`, если он есть и разрешен конфигурацией.

Не нужно удалять полезный контекст заранее без превышения разумного лимита.

Если есть example test source, он может использоваться как fallback или дополнительная опора. Он не должен дублировать related tests и не должен доминировать над реальным контекстом проекта.

Связность prompt важнее агрессивного уменьшения размера.

---

## Вызов codegenerator

`codecollector` вызывает внешний `codegenerator` через файловый JSON request.

Поддерживаются режимы:

- `generate`;
- `generate-test`;
- `repair`.

При подготовке `repair` нужно сохранять исходную operation, включая `insert_after_symbol`, в `previous_artifact.operation`. Repair не должен превращать вставку нового symbol в замену anchor-symbol.

Для `generate-test` с `insert_after_symbol` объектом тестирования считается сгенерированный symbol, а выбранный target остается anchor.

---

## Верификация

Структурные проверки обязательны.

Отчет о verification должен быть понятен без чтения исходного кода. Ошибки production-кода и ошибки generated test должны различаться.

Нужно различать статусы:

- `verification_failed`;
- `generated_test_verification_failed`.

Это различие должно быть согласовано в:

- `result_summary.status`;
- `session.status`;
- итоговом payload run;
- `merge_plan.summary_lines`;
- `apply_result.impact.notes`.

Если после всех проверок и попытки `repair` падает только сгенерированный тест, результат должен явно отражать ошибку generated test. Production-код не должен описываться как некорректный, если проблема подтверждена только в generated test.

Если `generate-test` завершился ошибкой и не вернул тестовый артефакт, это должно сохраняться в `generated_test_apply` и `warnings`. Такой случай не должен маскироваться под обычный `no_generated_tests`.

---

## Генерация и блокировка недостаточных запросов

Если analyze вернул `request_quality.status=insufficient`, session не готова к generation.

`sessions generate` должен возвращать JSON с:

- `generation_blocked=true`;
- `block_reason=insufficient_request`;
- `request_quality_status=insufficient`;
- `missing_information`;
- `recommended_action=rewrite_request_and_run_analyze_again`.

Pipeline не должен запускаться для такой session. UI может блокировать кнопку генерации по этому ответу.

---

## Логирование pipeline

В логах должно быть видно:

- какой target или anchor выбран;
- какая operation используется;
- источник operation;
- какие related tests переданы;
- есть ли example test source;
- использовался ли full file;
- какие части контекста были урезаны;
- какой итоговый размер request собран;
- почему выбран конкретный режим контекста;
- какие reference artifacts выбраны;
- какие проверки были запущены;
- какие generated tests применены или почему они не применены.

Логирование должно помогать разбирать реальные ошибки генерации тестов и кода, а не только фиксировать факт вызова внешнего генератора.

---

## Run artifacts

Структура run artifacts должна оставаться устойчивой.

В `.runs/` должны сохраняться:

- request и result внешних вызовов;
- stderr внешнего генератора;
- итоговый `pipeline_run_*.json`;
- usage и timing LLM-вызовов;
- verification report;
- generated test diagnostics;
- merge plan;
- предупреждения и причины пропущенных этапов.

Run artifacts являются основным материалом для отладки и анализа качества pipeline.

---

## Документация

При обновлении документации по `codecollector` нужно соблюдать следующие правила:

- описывать только текущее состояние проекта;
- писать по-русски;
- не добавлять историю изменений между версиями;
- не ссылаться на предыдущие версии;
- держать одну тему в одном месте;
- сохранять значимые факты при реструктуризации;
- оставлять только актуальные примеры;
- явно описывать ограничения и открытые задачи, если они относятся к текущему состоянию;
- описывать статусы `session` и `result_summary` консистентно и в одном месте для каждой сущности.

Документация не должна содержать рассуждения о возможных вариантах поведения. В ней фиксируется фактический текущий контракт.

---

## Что не нужно делать

Не нужно:

- усложнять prompt assembly ради одного кейса;
- размазывать одну и ту же логику по нескольким классам без причины;
- добавлять новые абстракции без текущей пользы;
- добавлять скрытые специальные случаи для demo-проектов;
- подменять project context reference-контекстом;
- использовать `select-target` как обходной путь для недостаточного запроса;
- описывать production-код как некорректный, если упал только generated test.
