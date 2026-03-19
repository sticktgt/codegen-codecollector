# sample_python_app

`sample_python_app` — демонстрационный Python-проект для `codecollector`.

## Назначение

Проект нужен для того, чтобы показать полный dry-run pipeline:
- поиск основного места изменения по `change_request`;
- ручной выбор target;
- сбор context pack;
- replay внешнего шага генерации;
- применение изменения в staging workspace;
- structural validation;
- impact summary;
- dry-run подготовки merge в master.

## Почему в коде есть docstring

Для demo-проекта docstring — это не оформление, а часть индексации и поиска.

`codecollector` использует их как основной источник human-readable смысла:
- модульные docstring объясняют роль файла;
- docstring классов и методов объясняют бизнес-смысл symbol-ов;
- knowledge-layer дополняет код, но не заменяет его.

## Что лежит в `.codecollector/`

- `knowledge.yaml` — human-readable knowledge-слой: описания модулей и symbol-ов, requirements, architecture.
- runtime graph/index хранится в Postgres и не создаёт локальный `index.db` рядом с проектом.

## Структура demo-проекта

- `support_app/api/controllers.py` — API-слой и точки входа use-case-ов.
- `support_app/services/ticket_service.py` — основная бизнес-логика по тикетам.
- `support_app/services/notification_service.py` — формирование уведомления о назначении.
- `support_app/services/report_service.py` — отчетные функции и сводки.
- `support_app/storage/ticket_repository.py` — простой in-memory репозиторий.
- `support_app/domain/models.py` — dataclass-модели предметной области.
- `tests/` — тесты, которые используются как связанный контекст и как рекомендуемые сценарии для запуска.

## Связанные сценарии

- `docs/demo_cases.md` — описание demo-cases;
- `tests/golden/demo_cases.yaml` — базовый исполняемый набор;
- `tests/golden/demo_cases_v2.yaml` — усиленные кейсы для текущего change-request flow;
- `demo_change_requests/` — примеры входных structured change request.
