# Текущая графовая модель runtime

## Почему модель графовая

Система отвечает на вопросы вида:
- кто вызывает этот symbol;
- какие тесты связаны с ним;
- какие requirement-ы он реализует;
- что будет затронуто при изменении.

Это естественно описывается как **узлы и связи**, а Postgres выступает физическим хранилищем этого графа.

## Узлы

- `file`
- `module`
- `class`
- `function`
- `method`
- `test_case`
- `requirement`

## Связи

- `contains`
- `imports`
- `calls`
- `covered_by_test`
- `belongs_to_layer`
- `implements_requirement`
- `exposed_by_controller`

## Разрешение целей связей

Для связи могут храниться:
- `target_ref` — текстовая цель;
- `target_qualname` — точная цель, если resolution удался;
- `relation_confidence` — `high`, `medium`, `low`.

## Почему не graph DB

На текущем этапе Postgres достаточно:
- проект локальный и небольшой;
- важнее стабилизировать extractor и pipeline;
- multi-hop анализ пока ограничен.
